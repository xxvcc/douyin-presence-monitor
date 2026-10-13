"""Regression/failure injection tests for private offline operations."""
from contextlib import closing
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

if __package__:
    from . import test_operations as base
else:
    import test_operations as base
CFG, TARGET = base.CFG, base.TARGET
from douyin_monitor import maintenance as m, observability as o


class LoggingFailureTests(unittest.TestCase):
    logs: Path
    root: Path
    setUp = base.LoggingTests.setUp
    tearDown = base.LoggingTests.tearDown

    def test_multithread_records_are_complete_and_not_duplicated(self):
        logger = o.setup_logging(self.logs)
        threads = [threading.Thread(target=lambda n=n: [logger.info('record', extra={'fields': {'n': n, 'i': i}})
                                                        for i in range(25)]) for n in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        records = o.read_logs(self.logs, limit=1000)
        self.assertEqual(len(records), 100)
        self.assertEqual(len({(r['fields']['n'], r['fields']['i']) for r in records}), 100)

    def test_reader_since_and_incomplete_lines(self):
        logger = o.setup_logging(self.logs)
        logger.info('known')
        with (self.logs / 'app.log').open('a') as f:
            f.write('not json\n[]\n{"timestamp":null}\n{"unfinished":')
        self.assertEqual(len(o.read_logs(self.logs, since='2000-01-01T00:00:00Z')), 1)
        self.assertEqual(o.read_logs(self.logs, since='2999-01-01T00:00:00Z'), [])
        self.assertEqual(o.read_logs(self.root / 'missing'), [])

    def test_nested_exception_chains_stack_and_object_are_redacted(self):
        secret = 'known-secret'
        logger = o.setup_logging(self.logs, secrets=(secret,))
        class Hidden:
            def __str__(self): return secret
        try:
            try: raise ValueError('Cookie: sessionid=not-registered')
            except ValueError as exc: raise RuntimeError(secret) from exc
        except RuntimeError:
            logger.exception(Hidden(), stack_info=True, extra={'fields': {'nested': Hidden()}})
        data = (self.logs / 'app.log').read_text()
        self.assertNotIn(secret, data)
        self.assertNotIn('not-registered', data)
        self.assertIn('Traceback', data)

    def test_oversize_fallback_does_not_break_cap_or_closed_stderr(self):
        logger = o.setup_logging(self.logs, max_bytes=250, total_bytes=500)
        err = io.StringIO()
        with patch('sys.stderr', err): logger.error('x' * 1000)
        self.assertIn('record_exceeds_size_limit', err.getvalue())
        self.assertLessEqual(sum(p.stat().st_size for p in self.logs.iterdir()), 500)
        closed = io.StringIO(); closed.close()
        with patch('sys.stderr', closed), patch.object(o, '_append_private', side_effect=OSError()):
            logger.error('short')
        logger.info('fine')
        self.assertEqual(o.read_logs(self.logs)[-1]['message'], 'fine')

    def test_quoted_headers_in_exception_text_and_proxy_url(self):
        for raw, secret in [('{"Cookie": "foo=private-cookie"}', 'private-cookie'),
                            ("{'Authorization': 'Digest opaque-credential'}", 'opaque-credential'),
                            ('proxy=http://user:private-password@example.invalid:8080', 'private-password')]:
            self.assertNotIn(secret, o.redact(raw))

    def test_partial_disk_write_does_not_poison_next_healthy_record(self):
        logger = o.setup_logging(self.logs)
        (self.logs / 'app.log').write_bytes(b'{"partial":')
        logger.info('healthy-after-partial-write')
        self.assertEqual(o.read_logs(self.logs)[-1]['message'], 'healthy-after-partial-write')


class MaintenanceFailureTests(unittest.TestCase):
    root: Path
    dbpath: Path
    config: Path
    out: Path
    lock: Path
    db: sqlite3.Connection
    setUp = base.MaintenanceTests.setUp
    tearDown = base.MaintenanceTests.tearDown
    backup = base.MaintenanceTests.backup

    def test_legacy_snapshot_and_export_preserve_ids_without_migration(self):
        self.db.close(); self.dbpath.unlink()
        self.db = sqlite3.connect(self.dbpath)
        self.db.executescript('''
        CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
        CREATE TABLE observations(id INTEGER PRIMARY KEY, observed_at REAL, payload TEXT NOT NULL);
        CREATE TABLE events(id INTEGER PRIMARY KEY, created_at REAL, kind TEXT, payload TEXT NOT NULL);
        CREATE TABLE outbox(id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE, payload TEXT NOT NULL,
          created_at REAL, next_attempt REAL, attempts INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
          message_id INTEGER, last_error TEXT, sent_at REAL);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        ''')
        cfg = {'target': TARGET, 'label': 'legacy', 'observer_uid': '12345', 'chat_id': 1, 'thread_id': None}
        self.config.write_text(json.dumps(cfg))
        self.db.execute('INSERT INTO meta VALUES (?,?)', ('binding', json.dumps({k: str(cfg[k]) for k in ('target','observer_uid','chat_id','thread_id')})))
        payload = {'target': TARGET, 'observer_uid': '12345', 'state': {'mode': 'online'}}
        self.db.execute('INSERT INTO events VALUES (?,?,?,?)', (19, 1700000000, 'online', json.dumps(payload))); self.db.commit()
        for version in (0, 1):
            self.db.execute(f'PRAGMA user_version={version}')
            self.assertEqual(m.verify_backup(self.backup())['db_user_version'], version)
            self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], version)
        out = self.root / 'legacy.json'
        m.export_data(self.dbpath, out, format='json', target=TARGET)
        self.assertEqual(json.loads(out.read_text())[0]['id'], 19)

    def test_backup_rejects_persisted_credentials_and_unrecognized_columns(self):
        self.db.execute("UPDATE events SET payload=?", (json.dumps({'target': TARGET, 'sessionid': 'x' * 32}),)); self.db.commit()
        with self.assertRaises(ValueError): self.backup()
        self.assertFalse(any(self.out.iterdir()))
        self.db.execute("UPDATE events SET payload='{}'")
        self.db.execute('ALTER TABLE events ADD COLUMN secret TEXT'); self.db.commit()
        with self.assertRaises(ValueError): self.backup()

    def test_export_redacts_legacy_secrets_without_mutating_and_atomic_failure(self):
        self.db.execute('UPDATE events SET payload=?', (json.dumps({'target': TARGET, 'token': 'private-token'}),)); self.db.commit()
        out = self.root / 'export.json'
        m.export_data(self.dbpath, out, format='json')
        self.assertNotIn('private-token', out.read_text())
        self.assertIn('private-token', self.db.execute('SELECT payload FROM events').fetchone()[0])
        existing = out.read_bytes()
        with patch.object(m, '_replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): m.export_data(self.dbpath, out, format='json')
        self.assertEqual(out.read_bytes(), existing)
        self.assertFalse(list(self.root.glob('.dpm-export-*')))

    def test_backup_failure_cleans_partial_and_detects_config_race(self):
        with patch.object(m, '_snapshot', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.backup()
        self.assertFalse(any(self.out.iterdir()))
        snapshot = m._snapshot
        def raced(*args):
            snapshot(*args)
            self.config.write_text(json.dumps({**CFG, 'retention_days': 99}))
        with patch.object(m, '_snapshot', side_effect=raced):
            with self.assertRaises(ValueError): self.backup()
        self.assertFalse(any(self.out.iterdir()))

    def test_verify_rechecks_schema_even_with_recomputed_hash(self):
        backup = self.backup()
        with closing(sqlite3.connect(backup / 'monitor.sqlite3')) as db:
            db.execute('CREATE VIEW attacker AS SELECT * FROM events')
        manifest = json.loads((backup / 'manifest.json').read_text())
        manifest['files']['monitor.sqlite3'] = m._hash(backup / 'monitor.sqlite3')
        (backup / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): m.verify_backup(backup)

    def test_sqlite_prefix_lookalike_table_is_not_a_system_table(self):
        self.db.execute('CREATE TABLE sqliteX_private(value TEXT)')
        self.db.commit()
        with self.assertRaises(ValueError): self.backup()

    def test_export_cannot_alias_a_sidecar_through_parent_symlink(self):
        alias = self.root / 'alias'; alias.symlink_to(self.root, target_is_directory=True)
        (self.root / 'nested').mkdir()
        with self.assertRaises(ValueError):
            m.export_data(self.dbpath, alias / 'nested' / '..' / 'monitor.sqlite3-wal', format='json')
        self.assertFalse(Path(str(self.dbpath) + '-wal').exists())

    def test_restore_before_swap_failure_no_changes_no_marker(self):
        backup = self.backup(); self.db.close()
        cfg = self.config.read_bytes(); db = self.dbpath.read_bytes()
        with patch.object(m, '_stage', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): m.restore_backup(backup, self.config, self.dbpath, self.lock)
        self.assertEqual(self.config.read_bytes(), cfg); self.assertEqual(self.dbpath.read_bytes(), db)
        self.assertFalse(m._marker_path(self.dbpath).exists())

    def test_restore_marker_fsync_failure_rolls_back_without_dangling_marker(self):
        backup = self.backup(); self.db.close()
        cfg = self.config.read_bytes(); db = self.dbpath.read_bytes()
        real = m._replace
        def fail_after_marker(src, dst):
            real(src, dst)
            if Path(dst) == m._marker_path(self.dbpath): raise OSError('marker fsync failure')
        with patch.object(m, '_replace', side_effect=fail_after_marker):
            with self.assertRaises(OSError): m.restore_backup(backup, self.config, self.dbpath, self.lock)
        self.assertFalse(m._marker_path(self.dbpath).exists())
        self.assertEqual(self.config.read_bytes(), cfg); self.assertEqual(self.dbpath.read_bytes(), db)

    def test_real_process_crash_recovers_old_pair_before_next_verification(self):
        backup = self.backup()
        self.db.execute("UPDATE events SET kind='later'"); self.db.commit(); self.db.close()
        self.config.write_text(json.dumps({**CFG, 'retention_days': 88}))
        old_db = self.dbpath.read_bytes(); old_cfg = self.config.read_bytes()
        script = '''import os, sys
from pathlib import Path
from douyin_monitor import maintenance as m
real = m._replace
def crash(src, dst):
    real(src, dst)
    if Path(dst) == Path(sys.argv[3]): os._exit(77)
m._replace = crash
m.restore_backup(*sys.argv[1:])
'''
        result = subprocess.run([sys.executable, '-c', script, str(backup), str(self.config), str(self.dbpath), str(self.lock)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertTrue(m._marker_path(self.dbpath).exists())
        self.assertNotEqual(self.dbpath.read_bytes(), old_db)
        (backup / 'config.json').write_text('{}')
        with self.assertRaises(ValueError): m.restore_backup(backup, self.config, self.dbpath, self.lock)
        self.assertEqual(self.dbpath.read_bytes(), old_db)
        self.assertEqual(self.config.read_bytes(), old_cfg)
        self.assertFalse(m._marker_path(self.dbpath).exists())

    def test_rollback_failure_keeps_journal_for_retry(self):
        backup = self.backup(); self.db.close()
        real = m._replace
        def fail_config(src, dst):
            if Path(dst) == self.config: raise OSError('persistent disk error')
            real(src, dst)
        with patch.object(m, '_replace', side_effect=fail_config):
            with self.assertRaises(OSError): m.restore_backup(backup, self.config, self.dbpath, self.lock)
        self.assertTrue(m._marker_path(self.dbpath).exists())
        result = m.restore_backup(backup, self.config, self.dbpath, self.lock)
        self.assertIsNotNone(result['recovered'])
        self.assertFalse(m._marker_path(self.dbpath).exists())

    def test_reject_symlink_paths_and_never_touch_victim(self):
        backup = self.backup(); self.db.close()
        victim = self.root / 'victim'; victim.write_text('unchanged')
        self.config.unlink(); self.config.symlink_to(victim)
        with self.assertRaises(ValueError): m.restore_backup(backup, self.config, self.dbpath, self.lock)
        with self.assertRaises((ValueError, OSError)): self.backup()
        self.assertEqual(victim.read_text(), 'unchanged')

    def test_csv_all_formula_prefixes_and_empty_target_configuration(self):
        for formula in ('=2+2', '+2+2', '-1+2', '@evil', ' \t=evil', '\tevil', '\revil'):
            self.assertTrue(m._csv_safe(formula).startswith("'"))
        self.config.write_text(json.dumps({**CFG, 'targets': []}))
        m.verify_backup(self.backup())

    def test_backup_scrubs_deleted_page_content(self):
        secret = 'deleted-secret-material-' * 1000
        self.db.execute('PRAGMA secure_delete=OFF')
        self.db.execute('INSERT INTO observations(observed_at,payload,target) VALUES (?,?,?)', (1, secret, TARGET))
        self.db.commit()
        self.db.execute('DELETE FROM observations'); self.db.commit()
        self.assertIn(b'deleted-secret-material-', self.dbpath.read_bytes())
        backup = self.backup()
        self.assertFalse(b'deleted-secret-material-' in (backup / 'monitor.sqlite3').read_bytes())


if __name__ == '__main__': unittest.main()
