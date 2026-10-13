"""Offline operation tests; all state lives in portable temporary directories."""
from contextlib import closing
import csv
import fcntl

import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from douyin_monitor import maintenance as m, observability as o

TARGET = 'MS4wLjABAAAA_SYNTHETIC'
CFG = {'schema_version': 2, 'observer_uid': '12345',
       'targets': [{'target': TARGET, 'label': 'fixture', 'enabled': True}],
       'chat_id': 1, 'thread_id': None}


def fixture(path, version=2):
    db = sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE state(target TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE observations(id INTEGER PRIMARY KEY, observed_at REAL, payload TEXT NOT NULL, target TEXT NOT NULL);
    CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, kind TEXT, payload TEXT NOT NULL,
      target TEXT, scope TEXT NOT NULL DEFAULT 'target');
    CREATE TABLE outbox(id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE, payload TEXT NOT NULL,
      created_at REAL, next_attempt REAL, attempts INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
      message_id INTEGER, last_error TEXT, sent_at REAL, bot_id TEXT);
    ''')
    db.execute(f'PRAGMA user_version={version}')
    db.execute('INSERT INTO meta VALUES (?,?)', ('binding', json.dumps({
        'observer_uid': '12345', 'chat_id': '1', 'thread_id': 'None'})))

    payload = {'target': TARGET, 'observer_uid': '12345', 'kind': 'online',
               'state': {'mode': 'online'}, 'note': '=HYPERLINK("bad")'}
    db.execute('INSERT INTO events(id,created_at,kind,payload,target) VALUES (?,?,?,?,?)',
               (7, 1700000000, 'online', json.dumps(payload), TARGET))
    db.commit()
    return db


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = self.root / 'logs'

    def tearDown(self):
        logger = logging.getLogger('douyin_monitor')
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        self.tmp.cleanup()

    def test_redaction_recursive_patterns_cycles_and_exact(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
        raw = {'password': 'unregistered', 'nested': ['exact-value',
               'https://api.telegram.org/bot' + token + '/sendMessage',
               'Cookie: sessionid=abcdef; other=secret', 'Authorization: Bearer abc-def'],
               'sessionid': 'a' * 32}
        clean = json.dumps(o.redact(raw, ('exact-value',)))
        for secret in ('unregistered', 'exact-value', token, 'abcdef', 'abc-def', 'a' * 32):
            self.assertNotIn(secret, clean)
        loop = []; loop.append(loop)
        self.assertIn('cycle', str(o.redact(loop)))

    def test_routing_exception_redaction_permissions_and_repeat_setup(self):
        original_root = list(logging.getLogger().handlers)
        o.setup_logging(self.logs, secrets=('classified',))
        logger = o.setup_logging(self.logs, secrets=('classified',))
        logger.info('sample %s', 'classified', extra={'fields': {'target': TARGET}})
        try:
            raise ValueError('classified Authorization: Bearer private-token')
        except ValueError:
            logger.exception('failure')
        logging.getLogger('douyin_monitor.audit').info('changed', extra={'fields': {'token': 'secret'}})
        app = o.read_logs(self.logs)
        self.assertEqual(len(app), 3)
        self.assertEqual(len(o.read_logs(self.logs, stream='error')), 1)
        self.assertEqual(len(o.read_logs(self.logs, stream='audit')), 1)
        self.assertEqual(len(o.read_logs(self.logs, target=TARGET)), 1)
        self.assertEqual(len(o.read_logs(self.logs, level='warning')), 1)
        self.assertRegex(app[0]['timestamp'], r'[+-]\d\d:\d\d$')
        for p in self.logs.iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            text = p.read_text()
            self.assertNotIn('classified', text)
            self.assertNotIn('private-token', text)
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o700)
        self.assertEqual(logging.getLogger().handlers, original_root)

    def test_size_day_rotation_retention_cap_and_namespace(self):
        logger = o.setup_logging(self.logs, max_bytes=450, total_bytes=1400, retention_days=1)
        outsider = self.logs / 'other.log.20000101'; outsider.write_text('keep')
        stale = self.logs / 'app.log.20000101T000000000000Z.0123456789abcdef'
        stale.write_text('{}\n'); os.utime(stale, (1, 1))
        logger.info('old')
        os.utime(self.logs / 'app.log', (1, 1))
        logger.info('new day')
        with patch.object(o.os, 'replace', wraps=os.replace) as rotated:
            for i in range(12):
                logger.warning('x' * 80, extra={'fields': {'i': i}})
            self.assertGreater(rotated.call_count, 0)
        own = [p for p in self.logs.iterdir() if p.name != outsider.name]
        self.assertFalse(stale.exists())
        self.assertEqual(outsider.read_text(), 'keep')

        self.assertLessEqual(sum(p.stat().st_size for p in own), 1400)
        self.assertLessEqual(len(o.read_logs(self.logs, limit=2)), 2)

    def test_disk_failure_fallback_is_sanitized_rate_limited_and_recovers(self):
        logger = o.setup_logging(self.logs, secrets=('classified',))
        err = io.StringIO()
        with patch.object(o, '_append_private', side_effect=OSError('classified disk error')), patch('sys.stderr', err):
            for _ in range(10):
                logger.error('classified')
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertNotIn('classified', err.getvalue())
        self.assertIn('fallback', err.getvalue())
        logger.info('recovered')
        self.assertEqual(o.read_logs(self.logs)[-1]['message'], 'recovered')

    def test_symlinks_and_invalid_read_arguments(self):
        self.logs.mkdir()
        victim = self.root / 'victim'; victim.write_text('untouched')
        (self.logs / 'app.log').symlink_to(victim)
        logger = o.setup_logging(self.logs)
        with patch('sys.stderr', io.StringIO()): logger.info('do not write')
        self.assertEqual(victim.read_text(), 'untouched')
        for kw in ({'stream': '../victim'}, {'limit': 0}, {'since': 'garbage'}, {'level': 'bad'}):
            with self.assertRaises(ValueError): o.read_logs(self.logs, **kw)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(CFG)); self.config.chmod(0o640)
        self.dbpath = self.root / 'monitor.sqlite3'
        self.db = fixture(self.dbpath); self.dbpath.chmod(0o640)
        self.out = self.root / 'backups'
        self.lock = self.root / 'monitor.lock'

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def backup(self):
        return m.create_backup(self.config, self.dbpath, self.out)

    def test_online_wal_snapshot_manifest_private_no_credentials(self):
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute("INSERT INTO meta VALUES ('committed','yes')"); self.db.commit()
        (self.root / 'douyin_session').write_text('real-secret-not-for-backup')
        backup = self.backup()
        self.assertEqual({p.name for p in backup.iterdir()}, {'config.json', 'monitor.sqlite3', 'manifest.json'})
        manifest = m.verify_backup(backup)
        self.assertEqual(manifest['schema_version'], 2)
        with closing(sqlite3.connect(backup / 'monitor.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='committed'").fetchone()[0], 'yes')
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        for p in backup.iterdir(): self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertNotEqual(backup, self.backup())

    def test_reject_unrelated_future_binding_secret_config_and_corruption(self):
        self.db.execute('PRAGMA user_version=3'); self.db.commit()
        with self.assertRaises(ValueError): self.backup()
        self.db.execute('PRAGMA user_version=2')
        self.db.execute("UPDATE meta SET value=? WHERE key='binding'", (json.dumps({'observer_uid': 'other', 'chat_id': '1', 'thread_id': 'None'}),)); self.db.commit()
        with self.assertRaises(ValueError): self.backup()
        self.db.execute("UPDATE meta SET value=? WHERE key='binding'", (json.dumps({'observer_uid': '12345', 'chat_id': '1', 'thread_id': 'None'}),)); self.db.commit()
        self.config.write_text(json.dumps({**CFG, 'token': 'private'}))
        with self.assertRaises(ValueError): self.backup()
        self.config.write_text(json.dumps(CFG))
        backup = self.backup()
        with (backup / 'monitor.sqlite3').open('ab') as f: f.write(b'tamper')
        with self.assertRaises(ValueError): m.verify_backup(backup)
        other = self.root / 'unrelated.db'
        with closing(sqlite3.connect(other)) as db: db.execute('CREATE TABLE private(value TEXT)')
        with self.assertRaises(ValueError): m.create_backup(self.config, other, self.out)

    def test_strict_members_symlinks_and_manifest(self):
        backup = self.backup()
        extra = backup / 'secret'; extra.write_text('forbidden')
        with self.assertRaises(ValueError): m.verify_backup(backup)
        extra.unlink()
        member = backup / 'config.json'; saved = member.read_bytes(); member.unlink(); member.symlink_to(self.config)
        with self.assertRaises(ValueError): m.verify_backup(backup)
        member.unlink(); member.write_bytes(saved)
        manifest = json.loads((backup / 'manifest.json').read_text()); manifest['format_version'] = 999
        (backup / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): m.verify_backup(backup)

    def test_restore_lock_rollback_and_preserve_modes(self):
        backup = self.backup()
        self.db.close()
        original_db = self.dbpath.read_bytes(); original_cfg = self.config.read_bytes()
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises((RuntimeError, BlockingIOError)):
                m.restore_backup(backup, self.config, self.dbpath, self.lock)
        changed = {**CFG, 'targets': [{**CFG['targets'][0], 'label': 'later'}]}
        self.config.write_text(json.dumps(changed))
        real_replace = m._replace
        calls = 0
        def fail_config(src, dst):
            nonlocal calls
            if Path(dst) == self.config:
                calls += 1
                if calls == 1: raise OSError('injected config swap failure')
            return real_replace(src, dst)
        with patch.object(m, '_replace', side_effect=fail_config):
            with self.assertRaises(OSError): m.restore_backup(backup, self.config, self.dbpath, self.lock)
        self.assertEqual(json.loads(self.config.read_text()), changed)
        self.assertEqual(self.dbpath.read_bytes(), original_db)
        result = m.restore_backup(backup, self.config, self.dbpath, self.lock)
        self.assertEqual(json.loads(self.config.read_text()), CFG)
        self.assertEqual(stat.S_IMODE(self.dbpath.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertTrue(Path(result['safety_backup']).is_dir())
        self.assertFalse(Path(result['recovery_marker']).exists())
        self.assertEqual(self.dbpath.stat().st_uid, os.getuid())

    def test_restore_refuses_live_wal_and_missing_db_export_read_only(self):
        backup = self.backup()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute("INSERT INTO meta VALUES ('live','yes')"); self.db.commit()
        with self.assertRaises((RuntimeError, ValueError)):
            m.restore_backup(backup, self.config, self.dbpath, self.lock)
        missing = self.root / 'missing.db'
        with self.assertRaises((ValueError, FileNotFoundError)):
            m.export_data(missing, self.root / 'missing.json', format='json')
        self.assertFalse(missing.exists())

    def test_export_csv_json_filters_formula_and_no_mutation(self):
        before = self.dbpath.read_bytes()
        csvpath = self.root / 'events.csv'
        result = m.export_data(self.dbpath, csvpath)
        self.assertEqual(result['count'], 1)
        with csvpath.open(newline='') as f: rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]['id'], '7')
        self.assertIn('state', json.loads(rows[0]['payload']))
        out = self.root / 'events.json'
        m.export_data(self.dbpath, out, format='json', target=TARGET, since='2020-01-01T00:00:00Z')
        self.assertEqual(json.loads(out.read_text())[0]['id'], 7)
        out = self.root / 'missing-events.json'
        m.export_data(self.dbpath, out, format='json', target='missing')
        self.assertEqual(json.loads(out.read_text()), [])
        self.db.execute("UPDATE events SET kind='=1+1'"); self.db.commit()
        csvpath = self.root / 'formula-events.csv'
        m.export_data(self.dbpath, csvpath)
        with csvpath.open(newline='') as f: self.assertEqual(list(csv.DictReader(f))[0]['kind'], "'=1+1")
        stable = self.dbpath.read_bytes()
        csvpath = self.root / 'stable-events.csv'
        m.export_data(self.dbpath, csvpath)
        self.assertEqual(self.dbpath.read_bytes(), stable)
        self.assertEqual(stat.S_IMODE(csvpath.stat().st_mode), 0o600)
        with self.assertRaises(ValueError): m.export_data(self.dbpath, out, format='xml')
        with self.assertRaises(ValueError): m.export_data(self.dbpath, self.dbpath, format='json')


if __name__ == '__main__': unittest.main()
