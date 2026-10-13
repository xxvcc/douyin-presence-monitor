"""Transactional multi-target state and immutable, ordered notification outbox.

SQLite connections belong to a single worker thread. The notifier opens its own
connection; state/event/outbox writes share one transaction in the collector.
"""
from __future__ import annotations
import dataclasses
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from .domain import State, REASONS
from .messages import render_event, telegram_payload

SCHEMA_VERSION = 2
LEGACY_COLUMNS = {
    'state': ['id', 'payload'],
    'observations': ['id', 'observed_at', 'payload'],
    'events': ['id', 'created_at', 'kind', 'payload'],
    'outbox': ['id', 'event_id', 'payload', 'created_at', 'next_attempt', 'attempts', 'status', 'message_id', 'last_error', 'sent_at'],
    'meta': ['key', 'value'],
}
V2_COLUMNS = {**LEGACY_COLUMNS,
    'state': ['target', 'payload'],
    'observations': LEGACY_COLUMNS['observations'] + ['target'],
    'events': LEGACY_COLUMNS['events'] + ['target', 'scope'],
    'outbox': LEGACY_COLUMNS['outbox'] + ['bot_id'],
}
_SCHEMA = (
    'CREATE TABLE state(target TEXT PRIMARY KEY,payload TEXT NOT NULL)',
    'CREATE TABLE observations(id INTEGER PRIMARY KEY,observed_at REAL,payload TEXT NOT NULL,target TEXT NOT NULL)',
    'CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at REAL,kind TEXT,payload TEXT NOT NULL,target TEXT,scope TEXT NOT NULL DEFAULT \'target\')',
    "CREATE TABLE outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER UNIQUE,payload TEXT NOT NULL,created_at REAL,next_attempt REAL,attempts INTEGER DEFAULT 0,status TEXT DEFAULT 'pending',message_id INTEGER,last_error TEXT,sent_at REAL,bot_id TEXT)",
    'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)',
)

_INDEXES = (
    'CREATE INDEX IF NOT EXISTS observations_time ON observations(observed_at)',
    'CREATE INDEX IF NOT EXISTS events_time ON events(created_at)',
    'CREATE INDEX IF NOT EXISTS events_target ON events(target,id)',
    'CREATE INDEX IF NOT EXISTS outbox_queue ON outbox(status,id)',
    'CREATE INDEX IF NOT EXISTS outbox_time ON outbox(created_at)',
)


def _schema(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    expected = LEGACY_COLUMNS if version in (0, 1) else V2_COLUMNS if version == 2 else None
    if expected is None or tables != set(expected):
        raise ValueError('unsupported database schema')
    # Reject unexpected views/triggers as well as similarly named arbitrary files.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone():
        raise ValueError('unsupported database objects')
    for table, columns in expected.items():
        if [r[1] for r in db.execute(f'PRAGMA table_info({table})')] != columns:
            raise ValueError('unsupported database schema')
    return version


def _binding(cfg):
    return {key: str(cfg.get(key)) for key in ('observer_uid', 'chat_id', 'thread_id')}


def _state(payload):
    data = json.loads(payload)
    data.setdefault('unknown_alerted', data.get('mode') == 'unknown' and bool(data.get('notified_reason')))
    return State(**data)


def _meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def _put(db, key, value):
    db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def _readonly(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('database symlink refused')
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        _schema(db)
    except BaseException:
        db.close()
        raise
    return db


def _status(db, cfg=None):
    version = _schema(db)
    runtime = json.loads(_meta(db, 'runtime', '{}'))
    runtime = {'heartbeat_at': None, 'started_at': None, 'stopped_at': None, 'pid': None, 'status': 'stopped', **runtime}
    heartbeat = runtime.get('heartbeat_at')
    runtime['stale'] = runtime['status'] == 'running' and (not heartbeat or time.time() - heartbeat > 90)
    if version == 2:
        targets = {r['target']: dataclasses.asdict(_state(r['payload'])) for r in db.execute('SELECT * FROM state')}
    else:
        binding = json.loads(_meta(db, 'binding', '{}'))
        row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        targets = {binding.get('target', ''): dataclasses.asdict(_state(row[0]))} if row else {}
    if cfg:
        for target in cfg.get('targets', []):
            value = targets.setdefault(target['target'], dataclasses.asdict(State()))
            value['enabled'] = target['enabled']
            value['label'] = target['label']
    samples = db.execute('SELECT count(*) FROM observations').fetchone()[0]
    return {'initialized': True, 'schema_version': version, 'targets': targets,
            'outbox': dict(db.execute('SELECT status,count(*) FROM outbox GROUP BY status').fetchall()),
            'samples': samples, 'total_samples': int(_meta(db, 'total_samples', samples)), 'runtime': runtime,
            'sender': json.loads(_meta(db, 'notification_sender', '{"status":"ready"}'))}


def status_snapshot(path, cfg=None):
    db = _readonly(path)
    if db is None:
        return {'initialized': False, 'schema_version': None, 'targets': {}, 'outbox': {}, 'samples': 0,
                'total_samples': 0, 'runtime': {'heartbeat_at': None, 'started_at': None, 'stopped_at': None,
                                              'pid': None, 'status': 'stopped', 'stale': False}}
    try:
        return _status(db, cfg)
    finally:
        db.close()


def list_events(path, *, target=None, since=None, limit=100):
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('invalid event limit')
    if since is not None and (isinstance(since, bool) or not isinstance(since, (int, float)) or not math.isfinite(since)):
        raise ValueError('invalid event time')
    db = _readonly(path)
    if db is None:
        return []
    try:
        version = _schema(db)
        where, args = [], []
        if since is not None:
            where.append('created_at>=?'); args.append(since)
        if target is not None:
            if version == 2:
                where.append('target=?'); args.append(target)
            elif json.loads(_meta(db, 'binding', '{}')).get('target') != target:
                return []
        args.append(limit)
        rows = db.execute('SELECT * FROM events' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY id DESC LIMIT ?', args)
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]
    finally:
        db.close()


class Store:
    def __init__(self, path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError('database symlink refused')
        new = not path.exists()
        if new:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self._closed = False
        try:
            if new:
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    for sql in _SCHEMA:
                        self.db.execute(sql)
                    self.db.execute('PRAGMA user_version=2')
            self.version = _schema(self.db)
            # Do not mutate legacy before bind verifies and migrates transactionally.
            if self.version == 2:
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        if not self._closed:
            self.db.close()
            self._closed = True

    def bind(self, cfg):
        expected = _binding(cfg)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            existing = _meta(self.db, 'binding')
            old = json.loads(existing) if existing else None
            if old and any(old.get(k) != v for k, v in expected.items()):
                raise ValueError('database binding mismatch; explicit migration required')
            if self.version != 2:
                if not old or old.get('target') not in {t['target'] for t in cfg.get('targets', [])}:
                    raise ValueError('legacy database target binding mismatch')
                self._migrate(old['target'])
            _put(self.db, 'binding', json.dumps(expected, sort_keys=True))
            for sql in _INDEXES:
                self.db.execute(sql)
        self.version = 2
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')

    def _migrate(self, target):
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row:
            legacy_state = _state(row[0])
            if legacy_state.mode == 'unknown' and legacy_state.reason in ('login_expired', 'account_mismatch', 'identity_unverified'):
                _put(self.db, 'account_state', json.dumps(dataclasses.asdict(legacy_state)))
        for table in ('events', 'outbox'):
            _put(self.db, table + '_highwater', self.db.execute(f'SELECT coalesce(max(id),0) FROM {table}').fetchone()[0])
        self.db.execute('ALTER TABLE state RENAME TO legacy_state')
        self.db.execute(_SCHEMA[0])
        self.db.execute('INSERT INTO state(target,payload) SELECT ?,payload FROM legacy_state', (target,))
        self.db.execute('DROP TABLE legacy_state')
        self.db.execute('ALTER TABLE observations ADD COLUMN target TEXT')
        self.db.execute('UPDATE observations SET target=?', (target,))
        self.db.execute('ALTER TABLE events ADD COLUMN target TEXT')
        self.db.execute("ALTER TABLE events ADD COLUMN scope TEXT NOT NULL DEFAULT 'target'")
        self.db.execute('UPDATE events SET target=?', (target,))
        self.db.execute('ALTER TABLE outbox ADD COLUMN bot_id TEXT')
        self.db.execute('UPDATE outbox SET bot_id=?', (_meta(self.db, 'notification_bot'),))
        if _meta(self.db, 'announced'):
            _put(self.db, 'announced:' + target, '1')
        _put(self.db, 'total_samples', self.db.execute('SELECT count(*) FROM observations').fetchone()[0])
        self.db.execute('PRAGMA user_version=2')

    def notification_sender(self):
        return json.loads(_meta(self.db, 'notification_sender', '{"status":"ready"}'))

    def check_notification_bot(self, bot_id):
        """A credential prefix is only a constraint, never a verified bot identity."""
        bot_id = str(bot_id)
        if not re.fullmatch(r'[1-9][0-9]*', bot_id):
            raise ValueError('invalid bot identity')
        old = _meta(self.db, 'notification_bot')
        expected = self.notification_sender().get('expected_bot_id')
        if (old and old != bot_id) or (expected and expected != bot_id):
            raise ValueError('notification bot changed; explicit switch required')

    def block_notification_sender(self, bot_id, error, now):
        """Persist permanent getMe failure and atomically make the queue manageable.

        New events consult this same state in their insert transaction. Unverified
        bots remain NULL on rows until getMe verifies the original credential's
        identity; explicit discard+switch can release that constraint.
        """
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.check_notification_bot(bot_id)
            _put(self.db, 'notification_sender', json.dumps({
                'status': 'blocked', 'reason': 'sender_auth_permanent',
                'error': error.code, 'detail': error.detail, 'blocked_at': now,
                'expected_bot_id': str(bot_id)}))
            self.db.execute("UPDATE outbox SET status='blocked',last_error='sender_auth_permanent' WHERE status='pending'")

    def sender_retry_requested(self):
        # Normal enqueue while blocked always creates blocked rows. A pending row
        # therefore represents an explicit management retry, including NULL-bot rows.
        return self.db.execute("SELECT 1 FROM outbox WHERE status='pending' LIMIT 1").fetchone() is not None

    def bind_notification_bot(self, bot_id):
        bot_id = str(bot_id)
        if not re.fullmatch(r'[1-9][0-9]*', bot_id):
            raise ValueError('invalid bot identity')
        with self.db:
            self.check_notification_bot(bot_id)
            _put(self.db, 'notification_bot', bot_id)
            # Terminal history must never inherit a later replacement bot.
            self.db.execute("UPDATE outbox SET bot_id=? WHERE bot_id IS NULL AND status IN ('pending','blocked')", (bot_id,))
            # Verification clears sender health, not explicitly blocked event states.
            _put(self.db, 'notification_sender', json.dumps({'status': 'ready'}))

    def rebind_notifications(self, cfg, bot_id):
        if not re.fullmatch(r'[1-9][0-9]*', str(bot_id)):
            raise ValueError('invalid bot identity')
        with self.db:
            old = json.loads(_meta(self.db, 'binding', '{}'))
            if not old or old.get('observer_uid') != str(cfg['observer_uid']):
                raise ValueError('notification switch cannot change observer')
            if self.db.execute("SELECT 1 FROM outbox WHERE status IN ('pending','blocked') LIMIT 1").fetchone():
                raise ValueError('unresolved notifications prevent switching')
            _put(self.db, 'binding', json.dumps(_binding(cfg), sort_keys=True))
            _put(self.db, 'notification_bot', str(bot_id))
            _put(self.db, 'notification_sender', json.dumps({'status': 'ready'}))
            self.db.execute("DELETE FROM meta WHERE key='announced' OR key LIKE 'announced:%'")

    def load(self, target):
        if self.version != 2:
            raise ValueError('bind and migrate before loading state')
        row = self.db.execute('SELECT payload FROM state WHERE target=?', (target,)).fetchone()
        return _state(row[0]) if row else State()

    def _check_route(self, cfg):
        old = _meta(self.db, 'binding')
        if old is None:
            _put(self.db, 'binding', json.dumps(_binding(cfg), sort_keys=True))
        elif json.loads(old) != _binding(cfg):
            raise ValueError('immutable notification destination mismatch')

    def _next_id(self, table):
        high = int(_meta(self.db, table + '_highwater', 0))
        current = self.db.execute(f'SELECT coalesce(max(id),0) FROM {table}').fetchone()[0]
        value = max(high, current) + 1
        _put(self.db, table + '_highwater', value)
        return value

    def _events(self, state, events, cfg, now, scope='target'):
        for event in events:
            if event.get('kind') not in ('baseline', 'started', 'unknown', 'recovered', 'online', 'offline') or 'at' not in event:
                raise ValueError('invalid event')
            target = cfg.get('target') if scope == 'target' else None
            record = {**event, 'target': target, 'scope': scope, 'observer_uid': cfg.get('observer_uid'), 'state': dataclasses.asdict(state)}
            cur = self.db.execute('INSERT INTO events(id,created_at,kind,payload,target,scope) VALUES(?,?,?,?,?,?)',
                                  (self._next_id('events'), now, event['kind'], json.dumps(record), target, scope))
            if event.get('notify'):
                payload = telegram_payload(cfg, render_event({**event, 'id': cur.lastrowid}, state, cfg), html=True)
                blocked = self.notification_sender().get('status') == 'blocked'
                self.db.execute('INSERT INTO outbox(id,event_id,payload,created_at,next_attempt,bot_id,status,last_error) VALUES(?,?,?,?,?,?,?,?)',
                                (self._next_id('outbox'), cur.lastrowid, json.dumps(payload, ensure_ascii=False), now, now,
                                 _meta(self.db, 'notification_bot'), 'blocked' if blocked else 'pending',
                                 'sender_auth_permanent' if blocked else None))

    def save(self, state, observation, events, flat_target_cfg, retention_days=30, announce=False):
        cfg = flat_target_cfg
        target = cfg['target']
        now = observation['observed_at']
        events = list(events)
        with self.db:
            self._check_route(cfg)
            if announce and not _meta(self.db, 'announced:' + target):
                events.append({'kind': 'started', 'at': now, 'reason': '', 'notify': True})
                _put(self.db, 'announced:' + target, '1')
            self.db.execute('INSERT INTO state(target,payload) VALUES(?,?) ON CONFLICT(target) DO UPDATE SET payload=excluded.payload',
                            (target, json.dumps(dataclasses.asdict(state))))
            self.db.execute('INSERT INTO observations(observed_at,payload,target) VALUES(?,?,?)', (now, json.dumps(observation), target))
            _put(self.db, 'total_samples', int(_meta(self.db, 'total_samples', 0)) + 1)
            self._events(state, events, cfg, now)
            cutoff = now - retention_days * 86400
            self.db.execute('DELETE FROM observations WHERE observed_at<?', (cutoff,))
            self.db.execute("DELETE FROM outbox WHERE status IN ('sent','discarded') AND created_at<?", (cutoff,))
            self.db.execute('DELETE FROM events WHERE created_at<? AND id NOT IN (SELECT event_id FROM outbox)', (cutoff,))

    def load_account(self):
        return _state(_meta(self.db, 'account_state', '{}'))

    def save_account(self, state, events, cfg, now):
        with self.db:
            self._check_route(cfg)
            _put(self.db, 'account_state', json.dumps(dataclasses.asdict(state)))
            self._events(state, events, {**cfg, 'label': '观察账号', 'target': ''}, now, scope='account')

    def runtime_metadata(self, **updates):
        with self.db:
            value = json.loads(_meta(self.db, 'runtime', '{}'))
            value.update(updates)
            _put(self.db, 'runtime', json.dumps(value))

    def pending(self, now):
        rows = self.db.execute("SELECT * FROM outbox WHERE status IN ('pending','blocked') ORDER BY id LIMIT 100").fetchall()
        due = []
        for row in rows:
            if row['status'] == 'blocked' or row['next_attempt'] > now or not row['bot_id']:
                break
            due.append(row)
        return due

    def mark_sent(self, id, message_id, now):
        with self.db:
            self.db.execute("UPDATE outbox SET status='sent',message_id=?,sent_at=?,last_error=NULL WHERE id=? AND status='pending'", (message_id, now, id))

    def mark_failed(self, id, error, now, delay, permanent=False):
        # Persist only controlled reason codes, not arbitrary network exception text.
        safe = error if error in REASONS else 'internal_error'
        with self.db:
            self.db.execute("UPDATE outbox SET attempts=attempts+1,last_error=?,next_attempt=?,status=? WHERE id=? AND status='pending'",
                            (safe, now + max(0, delay), 'blocked' if permanent else 'pending', id))

    def status(self):
        return _status(self.db)
