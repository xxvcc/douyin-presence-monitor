"""Locked management transactions, immutable outbox actions and recovery.

Notification switching spans SQLite and two files, so it is NOT magically
atomic: a private durable recovery marker blocks startup until rollback. The
normal backup API never includes credentials. Notification recovery material
DOES contain the old token, is 0700/0600, and stays local for operator recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import uuid

from .config import atomic_write_config, load_config, validate_config
from .credentials import atomic_private_write, ensure_plain_path, fsync_directory, validate_telegram_token

RECOVERY_MARKER = 'notification-recovery.json'
RESTORE_MARKER = '.monitor.sqlite3.dpm-restore.json'


def revision(path):
    path = ensure_plain_path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_no_recovery(state_dir, *, allow_restore=False):
    names = (RECOVERY_MARKER, 'init-recovery.json', 'restore-recovery.json')
    if not allow_restore:
        names += (RESTORE_MARKER,)
    for name in names:
        if (Path(state_dir)/name).exists() or (Path(state_dir)/name).is_symlink():
            raise ValueError('unresolved recovery marker; recover before running or changing configuration')


def private_directory(path):
    from .paths import managed_path
    path = managed_path(path)
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('symlink directory refused')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError('directory required')
    return path


def mutation_lock(state_dir):
    from .locking import InstanceLock
    directory = private_directory(state_dir)
    return InstanceLock(directory/'monitor.lock')


def mutate_config(config_path, state_dir, transform, expected_revision=None):
    expected_revision = expected_revision or revision(config_path)
    with mutation_lock(state_dir):
        assert_no_recovery(state_dir)
        if revision(config_path) != expected_revision:
            raise ValueError('concurrent configuration modification')
        cfg = load_config(config_path)
        updated = validate_config(transform(cfg))
        if updated['observer_uid'] != cfg['observer_uid'] or any(updated[k] != cfg[k] for k in ('chat_id', 'thread_id', 'telegram_proxy')):
            raise ValueError('identity and route changes require explicit migration')
        if revision(config_path) != expected_revision:
            raise ValueError('concurrent configuration modification')
        atomic_write_config(config_path, updated)
        return updated


def change_outbox(connection, identifier, action, reason=None):
    """Caller holds monitor.lock; only blocked rows are explicitly releasable.

    Routes/payload/event linkage and attempt count are untouched. A durable audit
    record in meta commits in the same transaction, in addition to CLI logging.
    """
    if type(identifier) is not int or identifier <= 0 or action not in ('retry', 'discard'):
        raise ValueError('invalid outbox action')
    if action == 'discard':
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or any(unicodedata.category(c).startswith('C') for c in reason):
            raise ValueError('a bounded discard reason is required')
        if re.search(r'\d+:[A-Za-z0-9_-]+|[0-9a-fA-F]{32}|sessionid|authorization|cookie', reason, re.I):
            raise ValueError('discard reason must not contain credentials')
    if connection.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise ValueError('v2 database required')
    if connection.in_transaction:
        raise ValueError('outbox action requires a fresh transaction')
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT status FROM outbox WHERE id=?', (identifier,)).fetchone()
        if row is None or row[0] != 'blocked':
            raise ValueError('only an existing blocked notification may be retried or discarded')
        now = time.time()
        status = 'pending' if action == 'retry' else 'discarded'
        connection.execute('UPDATE outbox SET status=?, next_attempt=?, last_error=? WHERE id=? AND status=\'blocked\'',
                           (status, now, None if action == 'retry' else 'operator_discarded', identifier))
        audit = {'action': action, 'outbox_id': identifier, 'reason': reason, 'at': now, 'status': status}
        connection.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                           ('outbox_action:' + str(time.time_ns()) + '_' + uuid.uuid4().hex,
                            json.dumps(audit, ensure_ascii=False)))
    return audit


def prepare_restore(config_path, db_path, state_dir):
    """Close/checkpoint stale WAL safely for an explicit stopped-service restore.

    Read-only SQLite readers can leave empty sidecars after they exit. Never
    unlink them by guesswork: SQLite must checkpoint and remove its own files.
    A busy checkpoint or surviving connection fails closed in maintenance.
    Caller verifies the backup first; maintenance reacquires the same lock for
    the actual restore, so a service starting between phases still blocks it.
    """
    with mutation_lock(state_dir):
        assert_no_recovery(state_dir, allow_restore=True)
        private_directory(Path(config_path).parent)
        db_path = ensure_plain_path(db_path)
        if db_path.exists():
            from .storage import status_snapshot
            status_snapshot(db_path)  # schema recognition without binding/migration
            connection = sqlite3.connect(db_path.as_uri()+'?mode=rw', uri=True, timeout=1)
            try:
                result = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if result and result[0]:
                    raise ValueError('database connections still active; restore refused')
            finally:
                connection.close()


def require_empty_queue(db):
    if db.execute("SELECT count(*) FROM outbox WHERE status IN ('pending','blocked')").fetchone()[0]:
        raise ValueError('pending or blocked notifications prevent migration')


def _snapshot_db(connection, destination):
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    target = sqlite3.connect(destination)
    try:
        connection.backup(target)
        if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('recovery snapshot integrity failure')
    finally:
        target.close()
    with open(destination, 'rb') as source:
        os.fsync(source.fileno())


def _write_marker(path, value):
    atomic_private_write(path, json.dumps(value, sort_keys=True) + '\n')


def apply_switch(config_path, token_path, store, cfg, token, bot_id, *, state_dir,
                 expected_config=None, expected_token=None):
    """Switch using a caller-held lock and already verified new bot/destination.

    All validation and snapshots precede writes. A crash leaves the marker;
    notify recover repeats an idempotent rollback. Success retains the recovery
    directory (sensitive old token) but clears the startup-blocking marker.
    """
    config_path, token_path = ensure_plain_path(config_path), ensure_plain_path(token_path)
    cfg = validate_config(cfg)
    validate_telegram_token(token)
    if str(bot_id) != token.split(':', 1)[0]:
        raise ValueError('notification identity mismatch')
    old_cfg = load_config(config_path)
    if cfg['observer_uid'] != old_cfg['observer_uid'] or cfg['targets'] != old_cfg['targets']:
        raise ValueError('notification switch cannot change observer or targets')
    if any(cfg[k] != old_cfg[k] for k in cfg if k not in ('chat_id', 'thread_id', 'telegram_proxy')):
        raise ValueError('notification switch cannot change monitoring rules')
    directory = private_directory(state_dir)
    assert_no_recovery(directory)
    if expected_config is not None and revision(config_path) != expected_config:
        raise ValueError('concurrent configuration modification')
    if expected_token is not None and revision(token_path) != expected_token:
        raise ValueError('concurrent credential modification')
    require_empty_queue(store.db)
    if store.db.in_transaction:
        raise ValueError('notification switch requires a fresh transaction')
    original_config = config_path.read_bytes().decode('utf-8')
    original_token = token_path.read_bytes().decode('utf-8')
    recovery = directory/('notification-recovery-' + uuid.uuid4().hex)
    recovery.mkdir(mode=0o700)
    atomic_private_write(recovery/'config.json', original_config)
    atomic_private_write(recovery/'telegram_token', original_token)
    _snapshot_db(store.db, recovery/'monitor.sqlite3')
    fsync_directory(recovery)
    if revision(config_path) != hashlib.sha256(original_config.encode('utf-8')).hexdigest() or revision(token_path) != hashlib.sha256(original_token.encode('utf-8')).hexdigest():
        raise ValueError('concurrent configuration modification')
    database_path = str(Path(store.db.execute('PRAGMA database_list').fetchone()[2]).absolute())
    hashes = {name: revision(recovery/name) for name in ('config.json', 'telegram_token', 'monitor.sqlite3')}
    marker = directory/RECOVERY_MARKER
    _write_marker(marker, {'version': 1, 'recovery_dir': recovery.name, 'hashes': hashes,
                           'config_path': str(config_path), 'token_path': str(token_path), 'database_path': database_path})
    try:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            require_empty_queue(store.db)
            # Some Store versions commit inside rebind. The held lock and durable
            # journal still prevent observation of a half-switch; rollback restores
            # the SQLite snapshot even when that inner transaction committed.
            store.rebind_notifications(cfg, str(bot_id))
            atomic_private_write(config_path, json.dumps(cfg, ensure_ascii=False, indent=2)+'\n')
            atomic_private_write(token_path, token+'\n')
        marker.unlink()
        fsync_directory(directory)
    except BaseException:
        try:
            store.db.rollback()
            recover_switch(config_path, token_path, store.db, state_dir=directory)
        except BaseException:
            # Deliberately preserve the durable marker if rollback itself fails.
            pass
        raise
    return {'switched': True, 'bot_id': str(bot_id), 'recovery_directory': str(recovery)}


def recover_switch(config_path, token_path, connection, *, state_dir):
    """Explicit rollback from locally generated, hash-checked recovery material.

    Caller holds the same monitor.lock. Never follows paths supplied in a marker
    outside the caller's exact configured destinations and recovery directory.
    """
    directory = Path(state_dir).absolute()
    marker = ensure_plain_path(directory/RECOVERY_MARKER)
    data = json.loads(marker.read_text())
    name = data.get('recovery_dir', '')
    if data.get('version') != 1 or not re.fullmatch(r'notification-recovery-[0-9a-f]{32}', name):
        raise ValueError('invalid recovery marker')
    config_path, token_path = ensure_plain_path(config_path), ensure_plain_path(token_path)
    if data.get('config_path') != str(config_path) or data.get('token_path') != str(token_path):
        raise ValueError('recovery destination mismatch')
    database_path = str(Path(connection.execute('PRAGMA database_list').fetchone()[2]).absolute())
    if data.get('database_path') != database_path:
        raise ValueError('recovery database destination mismatch')
    recovery = directory/name
    if recovery.is_symlink() or not recovery.is_dir():
        raise ValueError('invalid recovery directory')
    for member in ('config.json', 'telegram_token', 'monitor.sqlite3'):
        if revision(recovery/member) != data.get('hashes', {}).get(member):
            raise ValueError('recovery hash mismatch')
    load_config(recovery/'config.json')
    validate_telegram_token((recovery/'telegram_token').read_text().rstrip('\r\n'))
    source = sqlite3.connect((recovery/'monitor.sqlite3').as_uri()+'?mode=ro', uri=True)
    try:
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('recovery database integrity failure')
        connection.rollback()
        source.backup(connection)
    finally:
        source.close()
    atomic_private_write(config_path, (recovery/'config.json').read_bytes().decode('utf-8'))
    atomic_private_write(token_path, (recovery/'telegram_token').read_bytes().decode('utf-8'))
    marker.unlink()
    fsync_directory(directory)
    return {'recovered': True, 'credentials_restored': True}
