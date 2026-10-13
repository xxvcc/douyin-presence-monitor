"""Cross-module regressions found during parent integration, synthetic only."""
from contextlib import contextmanager
import importlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from core_helpers import canonical
from douyin_monitor import runtime
from douyin_monitor.domain import Observation, Problem
from douyin_monitor.locking import InstanceLock
from douyin_monitor.observability import read_logs
from douyin_monitor.storage import Store, status_snapshot


class IntegrationHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = canonical()
        (self.root / 'douyin_session').write_text('0' * 32)
        (self.root / 'douyin_session').chmod(0o600)

    def test_recovery_marker_appearing_after_lock_blocks_direct_runtime(self):
        state = self.root / 'state'
        @contextmanager
        def late_marker(path):
            with InstanceLock(path):
                (state / '.monitor.sqlite3.dpm-restore.json').write_text('{}')
                yield
        class Client:
            def verify_identity(self, uid):
                raise AssertionError('must not reach network boundary')
        with patch.object(runtime, 'InstanceLock', late_marker), patch.object(runtime, 'DouyinClient', return_value=Client()):
            with self.assertRaisesRegex(ValueError, 'recovery'):
                runtime.run(self.cfg, self.root, state, self.root / 'logs', once=True, no_notify=True)
        self.assertFalse((state / 'monitor.sqlite3').exists())

    def test_sqlite_reserved_prefix_lookalike_rejected_by_core(self):
        path = self.root / 'monitor.sqlite3'
        store = Store(path)
        store.bind(self.cfg)
        store.close()
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE sqliteX_private(value TEXT)')
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ValueError):
            status_snapshot(path)

    def test_silent_timeout_still_has_warning_in_error_log(self):
        class Client:
            def verify_identity(self, uid):
                pass
            def presence(self, target):
                raise Problem('network_timeout')
        with patch.object(runtime, 'DouyinClient', return_value=Client()):
            runtime.run(self.cfg, self.root, self.root / 'state', self.root / 'logs', once=True, no_notify=True)
        records = read_logs(self.root / 'logs', stream='error')
        self.assertTrue(any(row['message'] == 'sample' and row['fields']['timeout_streak'] == 1 for row in records))
        status = status_snapshot(self.root / 'state/monitor.sqlite3')
        self.assertEqual(status['outbox'].get('pending', 0), 0)

    def test_service_lifecycle_is_logged(self):
        class Client:
            def verify_identity(self, uid):
                pass
            def presence(self, target):
                return Observation(time.time(), time.time())
        with patch.object(runtime, 'DouyinClient', return_value=Client()):
            runtime.run(self.cfg, self.root, self.root / 'state', self.root / 'logs', once=True, no_notify=True)
        messages = [row['message'] for row in read_logs(self.root / 'logs')]
        self.assertIn('service_started', messages)
        self.assertIn('service_stopped', messages)

    def test_only_trusted_managed_aliases_are_canonicalized(self):
        self.assertIsNotNone(importlib.util.find_spec('douyin_monitor.paths'))
        paths = importlib.import_module('douyin_monitor.paths')
        actual = self.root / 'private/monitor'
        actual.mkdir(parents=True)
        alias = self.root / 'monitor'
        alias.symlink_to(actual, target_is_directory=True)
        with patch.object(paths, '_MANAGED_ALIASES', {alias: actual}), patch.object(paths, '_trusted_alias_parent', return_value=True):
            self.assertEqual(paths.managed_path(alias / 'monitor.sqlite3'), actual / 'monitor.sqlite3')
            self.assertEqual(paths.managed_path(actual), actual)
        with patch.object(paths, '_MANAGED_ALIASES', {alias: actual}), patch.object(paths, '_trusted_alias_parent', return_value=False):
            with self.assertRaises(ValueError):
                paths.managed_path(alias)
        wrong = self.root / 'elsewhere'
        wrong.mkdir()
        alias.unlink()
        alias.symlink_to(wrong, target_is_directory=True)
        with patch.object(paths, '_MANAGED_ALIASES', {alias: actual}), patch.object(paths, '_trusted_alias_parent', return_value=True):
            with self.assertRaises(ValueError):
                paths.managed_path(alias)


if __name__ == '__main__':
    unittest.main()
