"""Offline maintenance, with private artifacts and no credential/network access.

Backups contain sensitive target and activity history even though credential
files are never included. The format is deliberately small and allowlisted.
Restore is a journaled *two-file* transaction, not cross-filesystem atomicity:
under the service's flock it saves original bytes, persists a recovery marker,
then replaces the DB and config. An interrupted restore is rolled back on the
next restore invocation before attempting the requested restore. Never remove
monitor.lock; a running process holds its inode. Nonempty/live SQLite sidecars
are refused, not unlinked or transplanted to a different database.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import csv
import fcntl
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from .observability import _since, redact

_FORMAT = 'douyin-presence-monitor-backup'
_MEMBERS = {'config.json', 'monitor.sqlite3', 'manifest.json'}
_CONFIG_KEYS = {'schema_version', 'observer_uid', 'targets', 'target', 'label', 'chat_id', 'thread_id',
                'telegram_proxy', 'poll_seconds', 'offline_after_seconds', 'request_timeout',
                'timeout_alert_after', 'unknown_reason_cooldown', 'retention_days',
                'log_retention_days', 'log_max_bytes', 'log_total_bytes'}
_LEGACY_COLUMNS = {
    'meta': ['key', 'value'], 'state': ['id', 'payload'],
    'observations': ['id', 'observed_at', 'payload'],
    'events': ['id', 'created_at', 'kind', 'payload'],
    'outbox': ['id', 'event_id', 'payload', 'created_at', 'next_attempt', 'attempts',
               'status', 'message_id', 'last_error', 'sent_at'],
}
_V2_COLUMNS = {**_LEGACY_COLUMNS,
    'state': ['target', 'payload'],
    'observations': _LEGACY_COLUMNS['observations'] + ['target'],
    'events': _LEGACY_COLUMNS['events'] + ['target', 'scope'],
    'outbox': _LEGACY_COLUMNS['outbox'] + ['bot_id'],
}


def _regular(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('expected a regular file with one link (no symlinks or hardlinks)')
    return info


def _directory(path, *, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('expected a real directory')
    return path


def _json_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate JSON key')
        obj[key] = value
    return obj


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_json_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (UnicodeError, ValueError, TypeError):
        raise ValueError('invalid JSON document') from None


def _read(path, maximum=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('expected a private regular file')
        raw = f.read(maximum + 1) if maximum is not None else f.read()
        if maximum is not None and len(raw) > maximum:
            raise ValueError('document exceeds size limit')
        return raw


def _credential_free(value):
    if redact(value) != value:
        raise ValueError('credential-like content is not permitted in maintenance artifacts')


def _config(raw):
    data = _json(raw)
    if not isinstance(data, dict) or set(data) - _CONFIG_KEYS:
        raise ValueError('unsupported or secret-bearing configuration')
    _credential_free(data)
    # This is a format/binding inspector, not a substitute for CLI validation.
    # Load the authoritative validator lazily so operations work independently.
    try:
        module = importlib.import_module('.config', __package__)
    except ModuleNotFoundError as exc:
        if exc.name != 'douyin_monitor.config':
            raise
    else:
        module.validate_config(data)
    schema = data.get('schema_version', 1 if 'target' in data else 2)
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError('unsupported configuration version')
    observer = data.get('observer_uid')
    if not isinstance(observer, str) or not re.fullmatch(r'[0-9]+', observer):
        raise ValueError('invalid observer binding')
    if type(data.get('chat_id')) is not int or data['chat_id'] == 0:
        raise ValueError('invalid notification binding')
    thread = data.get('thread_id')
    if thread is not None and (type(thread) is not int or thread <= 0):
        raise ValueError('invalid notification binding')
    targets = data.get('targets')
    if targets is None and schema == 1:
        targets = [{'target': data.get('target'), 'label': data.get('label', ''), 'enabled': True}]
    if not isinstance(targets, list) or len(targets) > 100:
        raise ValueError('invalid targets')
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) - {'target', 'label', 'enabled'}:
            raise ValueError('invalid target fields')
        ident = target.get('target')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,300}', ident) or ident in seen:
            raise ValueError('invalid or duplicate target')
        seen.add(ident)
        label = target.get('label', '')
        if not isinstance(label, str) or len(label) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ValueError('invalid label')
        if type(target.get('enabled', True)) is not bool:
            raise ValueError('invalid enabled flag')
    for key in ('poll_seconds', 'offline_after_seconds', 'request_timeout'):
        if key in data and (type(data[key]) not in (int, float) or not math.isfinite(data[key]) or data[key] <= 0):
            raise ValueError('invalid numeric configuration')
    if 'unknown_reason_cooldown' in data:
        value = data['unknown_reason_cooldown']
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400:
            raise ValueError('invalid cooldown')
    for key in ('timeout_alert_after', 'retention_days', 'log_retention_days', 'log_max_bytes', 'log_total_bytes'):
        if key in data and (type(data[key]) is not int or data[key] <= 0):
            raise ValueError('invalid integer configuration')
    if data.get('poll_seconds', 60) < 30 or data.get('offline_after_seconds', 600) < 600:
        raise ValueError('unsafe polling configuration')
    if data.get('telegram_proxy') is not None:
        proxy = data['telegram_proxy']
        if not isinstance(proxy, str):
            raise ValueError('invalid proxy')
        parsed = urlsplit(proxy)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError('invalid or credential-bearing proxy')
    return data


@contextmanager
def _readonly(path, *, immutable=False):
    path = Path(path)
    _regular(path)
    uri = path.absolute().as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        yield db
    finally:
        db.close()


def _inspect(db, cfg=None, *, scan_secrets=True):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0, 1, 2):
        raise ValueError('unsupported database schema version')
    entries = db.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
    tables = {r['name'] for r in entries if r['type'] == 'table'}
    expected = _V2_COLUMNS if version == 2 else _LEGACY_COLUMNS
    if tables != set(expected) or any(r['type'] not in ('table', 'index') or
                                 (r['sql'] and 'VIRTUAL TABLE' in r['sql'].upper()) for r in entries):
        raise ValueError('unrecognized database schema')
    columns = {r['name']: [col['name'] for col in db.execute('PRAGMA table_info("' + r['name'] + '")')]
               for r in entries if r['type'] == 'table'}
    for name, required in expected.items():
        if required != columns[name]:
            raise ValueError('incomplete database schema')
    if [row[0] for row in db.execute('PRAGMA quick_check')] != ['ok']:
        raise ValueError('database integrity check failed')
    row = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
    if row is None:
        raise ValueError('database has no observer binding')
    binding = _json(row[0])
    if not isinstance(binding, dict) or not {'observer_uid', 'chat_id', 'thread_id'} <= set(binding):
        raise ValueError('invalid database binding')
    if cfg is not None:
        for key in ('observer_uid', 'chat_id', 'thread_id'):
            if str(binding[key]) != str(cfg.get(key)):
                raise ValueError('configuration and database bindings differ')
        if version < 2:
            targets = cfg.get('targets', [])
            target = cfg.get('target') or (targets[0]['target'] if targets else None)
            if binding.get('target') != target or len(cfg.get('targets', [target])) != 1:
                raise ValueError('legacy target binding differs')
    if scan_secrets:
        for table in sorted(tables):
            for row in db.execute('SELECT * FROM "' + table + '"'):
                for value in row:
                    if isinstance(value, str):
                        _credential_free(value)
                        if value[:1] in ('{', '['):
                            _credential_free(_json(value))
                    elif isinstance(value, bytes):
                        raise ValueError('unexpected binary data in monitor database')
    return {'schema_version': 2 if version == 2 else 1, 'db_user_version': version, 'binding': binding}


def _hash(path):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe backup member')
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
        return {'sha256': digest.hexdigest(), 'size': info.st_size}


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _snapshot(db_path, destination):
    _write_private(destination, b'')
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic() - started > 30:
            raise TimeoutError('SQLite snapshot deadline exceeded')
    with _readonly(db_path) as source:
        target = sqlite3.connect(destination)
        try:
            source.backup(target, pages=256, progress=progress, sleep=0.05)
            target.row_factory = sqlite3.Row
            target.execute('PRAGMA trusted_schema=OFF')
            _inspect(target, scan_secrets=False)
            target.execute('PRAGMA journal_mode=DELETE')
            # The backup API copies freelist pages too. Compact only the snapshot
            # so deleted history/credentials do not survive as recoverable bytes.
            target.execute('PRAGMA secure_delete=ON')
            target.execute('VACUUM')
        finally:
            target.close()
    fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_backup(config_path, db_path, output_dir):
    """Take an online SQLite snapshot; include only config, DB and hashed manifest.

    Unknown schema, absent binding, credential-like persisted data and invalid
    configuration fail closed. A failure removes the incomplete private folder.
    SHA256 detects corruption, not authenticity: keep backups in trusted storage.
    """
    raw = _read(config_path, 1024 * 1024)
    cfg = _config(raw)
    _regular(db_path)
    output = _directory(output_dir, create=True)
    directory = Path(tempfile.mkdtemp(prefix='dpm-backup-', dir=output))
    try:
        _write_private(directory / 'config.json', raw)
        _snapshot(db_path, directory / 'monitor.sqlite3')
        with _readonly(directory / 'monitor.sqlite3', immutable=True) as db:
            inspected = _inspect(db, cfg)
        if _read(config_path, 1024 * 1024) != raw:
            raise ValueError('configuration changed during backup; retry while config is stable')
        manifest = {'format': _FORMAT, 'format_version': 1,
                    'created_at': datetime.now(timezone.utc).isoformat(), **inspected,
                    'files': {name: _hash(directory / name) for name in ('config.json', 'monitor.sqlite3')}}
        _write_private(directory / 'manifest.json', _encode(manifest))
        _fsync_dir(directory)
        _fsync_dir(output)
        verify_backup(directory)
        return directory
    except BaseException as exc:
        shutil.rmtree(directory)
        if isinstance(exc, sqlite3.DatabaseError):
            raise ValueError('invalid monitor database') from None
        raise


def verify_backup(backup_dir):
    """Strict validation without modifying the archive (including SQLite files)."""
    directory = _directory(backup_dir)
    if {p.name for p in directory.iterdir()} != _MEMBERS:
        raise ValueError('backup must contain exactly the fixed three members')
    for name in _MEMBERS:
        _regular(directory / name)
    manifest = _json(_read(directory / 'manifest.json', 1024 * 1024))
    required = {'format', 'format_version', 'created_at', 'schema_version', 'db_user_version', 'binding', 'files'}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError('invalid manifest fields')
    if manifest['format'] != _FORMAT or type(manifest['format_version']) is not int or manifest['format_version'] != 1:
        raise ValueError('unsupported backup format')
    try:
        if datetime.fromisoformat(manifest['created_at']).tzinfo is None:
            raise ValueError('timestamp must have timezone')
    except (ValueError, TypeError):
        raise ValueError('invalid manifest timestamp') from None
    if not isinstance(manifest['files'], dict) or set(manifest['files']) != {'config.json', 'monitor.sqlite3'}:
        raise ValueError('invalid manifest members')
    for name, expected in manifest['files'].items():
        if (not isinstance(expected, dict) or set(expected) != {'sha256', 'size'} or
                not isinstance(expected['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', expected['sha256']) or
                type(expected['size']) is not int or expected['size'] < 0 or expected != _hash(directory / name)):
            raise ValueError('backup hash or size mismatch')
    cfg = _config(_read(directory / 'config.json', 1024 * 1024))
    try:
        with _readonly(directory / 'monitor.sqlite3', immutable=True) as db:
            inspected = _inspect(db, cfg)
    except sqlite3.DatabaseError:
        raise ValueError('invalid backup database') from None
    if any(type(manifest[key]) is not type(value) or manifest[key] != value for key, value in inspected.items()):
        raise ValueError('manifest disagrees with database schema or binding')
    return manifest


@contextmanager
def _instance_lock(path):
    # Same Linux flock inode/protocol as locking.InstanceLock, with no dependency
    # on the collector module and no unlink on release.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe monitor lock file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('monitor is running; stop it before restore') from None
        yield
    finally:
        os.close(fd)


def _sidecars_absent(path):
    # Even an apparently empty WAL can belong to a writer. Do not checkpoint or
    # unlink it here: only its owning connection can safely close it.
    if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
           for suffix in ('-wal', '-shm', '-journal')):
        raise RuntimeError('SQLite sidecars present; close all database connections before restore')


def _replace(source, destination):
    os.replace(source, destination)
    _fsync_dir(Path(destination).parent)


def _stage(source, destination, metadata):
    """Copy into destination filesystem; preserve existing owner and permissions."""
    fd, name = tempfile.mkstemp(prefix='.dpm-stage-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            src_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(src_fd, 'rb') as src:
                if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                    raise ValueError('unsafe staged source')
                shutil.copyfileobj(src, f)
            if metadata is not None:
                existing = os.fstat(f.fileno())
                if (existing.st_uid, existing.st_gid) != (metadata['uid'], metadata['gid']):
                    os.fchown(f.fileno(), metadata['uid'], metadata['gid'])
                os.fchmod(f.fileno(), metadata['mode'])
            f.flush()
            os.fsync(f.fileno())
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _metadata(path):
    try:
        info = _regular(path)
    except FileNotFoundError:
        return None
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode)}


def _marker_path(db_path):
    return db_path.parent / ('.' + db_path.name + '.dpm-restore.json')


def _rollback(marker, config_path, db_path):
    """Recover original bytes, never external paths supplied by a forged marker."""
    journal = _json(_read(marker, 1024 * 1024))
    keys = {'format', 'config_path', 'db_path', 'safety_backup', 'originals'}
    if (not isinstance(journal, dict) or set(journal) != keys or journal['format'] != 'dpm-restore-1' or
            journal['config_path'] != str(config_path) or journal['db_path'] != str(db_path)):
        raise ValueError('invalid recovery marker; manual review required')
    safety = Path(journal['safety_backup'])
    if safety.parent != db_path.parent or not re.fullmatch(r'\.dpm-restore-safety-[0-9a-f]{32}', safety.name):
        raise ValueError('invalid recovery safety path')
    _directory(safety)
    originals = journal['originals']
    if not isinstance(originals, dict) or set(originals) != {'config.json', 'monitor.sqlite3'}:
        raise ValueError('invalid recovery metadata')
    # Fully verify all rollback data before changing either destination.
    for name, item in originals.items():
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != {'metadata', 'hash'}:
            raise ValueError('invalid recovery metadata')
        meta = item['metadata']
        if (not isinstance(meta, dict) or set(meta) != {'uid', 'gid', 'mode'} or
                any(type(v) is not int or v < 0 for v in meta.values()) or meta['mode'] > 0o7777 or
                _hash(safety / name) != item['hash']):
            raise ValueError('invalid recovery data')
    _sidecars_absent(db_path)
    for name, path in (('monitor.sqlite3', db_path), ('config.json', config_path)):
        item = originals[name]
        if item is None:
            if path.exists() or path.is_symlink():
                _regular(path)
                path.unlink()
                _fsync_dir(path.parent)
        else:
            staged = _stage(safety / name, path, item['metadata'])
            try:
                _replace(staged, path)
            finally:
                staged.unlink(missing_ok=True)
    marker.unlink()
    _fsync_dir(marker.parent)
    return safety


def restore_backup(backup_dir, config_path, db_path, lock_path):
    """Restore while stopped, retaining a private byte-exact rollback directory.

    For crash recovery rerun this function with the same destination paths. It
    rolls back an outstanding marker under the lock first. Do not start the
    service while a marker exists. Credentials are intentionally not restored;
    the caller must verify account and bot identity before resuming collection.
    """
    config_path = Path(config_path).absolute()
    db_path = Path(db_path).absolute()
    lock_path = Path(lock_path).absolute()
    if len({p.resolve() for p in (config_path, db_path, lock_path)}) != 3:
        raise ValueError('config, database and lock must have distinct paths')
    for parent in {config_path.parent, db_path.parent, lock_path.parent}:
        _directory(parent)
    marker = _marker_path(db_path)
    with _instance_lock(lock_path):
        _sidecars_absent(db_path)
        recovered = None
        if marker.exists() or marker.is_symlink():
            recovered = str(_rollback(marker, config_path, db_path))
        manifest = verify_backup(backup_dir)
        backup = Path(backup_dir).absolute()
        members = {(backup / n).resolve() for n in _MEMBERS}
        if config_path.resolve() in members or db_path.resolve() in members:
            raise ValueError('restore destinations must not overwrite the backup')
        safety = db_path.parent / ('.dpm-restore-safety-' + uuid.uuid4().hex)
        safety.mkdir(mode=0o700)
        originals = {}
        staged = []
        journal = None
        journal_written = False
        try:
            for name, path in (('config.json', config_path), ('monitor.sqlite3', db_path)):
                meta = _metadata(path)
                if meta is None:
                    originals[name] = None
                else:
                    saved = _stage(path, safety / name, None)
                    _replace(saved, safety / name)
                    originals[name] = {'metadata': meta, 'hash': _hash(safety / name)}
            _fsync_dir(safety)
            _fsync_dir(safety.parent)
            for name, path in (('monitor.sqlite3', db_path), ('config.json', config_path)):
                item = originals[name]
                stage = _stage(backup / name, path, item['metadata'] if item else None)
                staged.append((stage, path))
                if _hash(stage) != manifest['files'][name]:
                    raise ValueError('backup changed during restore')
            journal = {'format': 'dpm-restore-1', 'config_path': str(config_path), 'db_path': str(db_path),
                       'safety_backup': str(safety), 'originals': originals}
            # Atomic publication avoids a partially written marker after power loss.
            pending_marker = safety / 'recovery.json'
            _write_private(pending_marker, _encode(journal))
            _replace(pending_marker, marker)
            journal_written = True
            _sidecars_absent(db_path)
            for source, destination in staged:
                _replace(source, destination)
            # Read back exact targets before committing the cross-file transaction.
            for name, path in (('config.json', config_path), ('monitor.sqlite3', db_path)):
                if _hash(path) != manifest['files'][name]:
                    raise OSError('restore read-back mismatch')
            marker.unlink()
            _fsync_dir(marker.parent)
            journal_written = False
        except Exception:
            if journal_written or marker.exists():
                # A rename can succeed then fail its directory fsync; keep the
                # safety snapshot whenever the marker was actually published.
                if not marker.exists():
                    if journal is None:
                        raise RuntimeError('recovery journal unavailable; retain safety snapshot')
                    _write_private(marker, _encode(journal))
                    _fsync_dir(marker.parent)
                _rollback(marker, config_path, db_path)
            else:
                shutil.rmtree(safety)
            raise
        finally:
            for source, _ in staged:
                source.unlink(missing_ok=True)
        return {'restored': True, 'schema_version': manifest['schema_version'],
                'safety_backup': str(safety), 'recovery_marker': str(marker), 'recovered': recovered,
                'credentials_restored': False}


def _csv_safe(value):
    text = str(value) if value is not None else ''
    # Spreadsheet importers may trim whitespace before evaluating formulas.
    if text.lstrip(' \t\r\n\ufeff').startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def export_data(db_path, output_path, *, format='csv', target=None, since=None):
    """Stream event history through a read-only SQLite transaction, never migrate.

    JSON records retain immutable IDs and complete event payloads. CSV has a JSON
    payload column and formula-safe cells. Output is published atomically only
    if absent; existing files, symlinks and hardlinks are never replaced.
    Unexpected credential-like legacy text is redacted rather than exported.
    """
    if format not in ('csv', 'json'):
        raise ValueError('format must be csv or json')
    cutoff = _since(since)
    if target is not None and not isinstance(target, str):
        raise ValueError('target must be a string')
    db_path = Path(db_path).absolute()
    output = Path(output_path).absolute()
    protected = {db_path.resolve()} | {Path(str(db_path) + s).resolve() for s in ('-wal', '-shm', '-journal')}
    if output.resolve() in protected:
        raise ValueError('export cannot overwrite the database or its sidecars')
    _directory(output.parent)
    if output.exists() or output.is_symlink():
        raise FileExistsError('export output already exists; choose a new path')
    count = 0
    temp = None
    try:
        with _readonly(db_path) as db:
            db.execute('BEGIN')
            _inspect(db, scan_secrets=False)
            query = 'SELECT * FROM events'
            args = []
            if cutoff is not None:
                query += ' WHERE created_at >= ?'
                args.append(cutoff)
            query += ' ORDER BY id'
            fd, temp = tempfile.mkstemp(prefix='.dpm-export-', dir=output.parent)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f) if format == 'csv' else None
                if writer:
                    writer.writerow(('id', 'created_at', 'kind', 'target', 'observer_uid', 'payload'))
                else:
                    f.write('[\n')
                for row in db.execute(query, args):
                    payload = _json(row['payload'])
                    if not isinstance(payload, dict):
                        raise ValueError('invalid event payload')
                    event_target = payload.get('target', row['target'] if 'target' in row.keys() else None)
                    if target is not None and event_target != target:
                        continue
                    record = redact({'id': row['id'], 'created_at': row['created_at'], 'kind': row['kind'],
                                     'target': event_target, 'observer_uid': payload.get('observer_uid'), 'payload': payload})
                    if writer:
                        writer.writerow([_csv_safe(record[key]) for key in ('id', 'created_at', 'kind', 'target', 'observer_uid')] +
                                        [_csv_safe(json.dumps(record['payload'], ensure_ascii=False, allow_nan=False))])
                    else:
                        if count:
                            f.write(',\n')
                        f.write(json.dumps(record, ensure_ascii=False, allow_nan=False))
                    count += 1
                if not writer:
                    f.write('\n]\n')
                f.flush()
                os.fsync(f.fileno())
            # link() publishes the complete fsynced inode only if the destination
            # is absent. Unlike exists()+replace(), a concurrent creator wins
            # without its inode (including locks and symlinks) being replaced.
            os.link(temp, output, follow_symlinks=False)
            Path(temp).unlink()
            temp = None
            _fsync_dir(output.parent)
        return {'path': str(output), 'format': format, 'count': count}
    except sqlite3.DatabaseError:
        raise ValueError('invalid monitor database') from None
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)
