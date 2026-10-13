"""Single-root CLI layout and safe offline maintenance regressions."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core_helpers import canonical
from douyin_monitor import cli
from douyin_monitor.config import atomic_write_config, load_config
from douyin_monitor.locking import InstanceLock
from douyin_monitor.storage import Store


class SingleRootCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg_path = self.root / 'config/config.json'
        self.cfg_path.parent.mkdir()
        self.cfg = canonical({'log_retention_days': 30, 'log_max_bytes': 2048, 'log_total_bytes': 8192})
        atomic_write_config(self.cfg_path, self.cfg)
        self.state = self.root / 'data'
        self.state.mkdir()
        store = Store(self.state / 'monitor.sqlite3')
        store.bind(self.cfg)
        store.close()
        self.args = ['--config', str(self.cfg_path), '--credentials', str(self.cfg_path.parent),
                     '--state-dir', str(self.state), '--log-dir', str(self.root / 'logs')]

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*self.args, *args])
        return code, out.getvalue(), err.getvalue()

    def test_default_layout_is_single_opt_root(self):
        with patch.dict(os.environ, {}, clear=True):
            args = cli.build_parser().parse_args(['status'])
        self.assertEqual(args.config, '/opt/douyin-presence-monitor/config/config.json')
        self.assertEqual(args.credentials, '/opt/douyin-presence-monitor/config')
        self.assertEqual(args.state_dir, '/opt/douyin-presence-monitor/data')
        self.assertEqual(args.log_dir, '/opt/douyin-presence-monitor/logs')

    def test_home_changes_all_defaults_but_explicit_env_and_flags_win(self):
        with patch.dict(os.environ, {'DPM_HOME': str(self.root)}, clear=True):
            args = cli.build_parser().parse_args(['status'])
            self.assertEqual(args.config, str(self.cfg_path))
            self.assertEqual(args.state_dir, str(self.state))
            self.assertEqual(args.log_dir, str(self.root / 'logs'))
        with patch.dict(os.environ, {'DPM_HOME': str(self.root), 'DPM_STATE_DIR': str(self.root/'custom')}, clear=True):
            self.assertEqual(cli.build_parser().parse_args(['status']).state_dir, str(self.root/'custom'))
            self.assertEqual(cli.build_parser().parse_args(['--state-dir', str(self.state), 'status']).state_dir, str(self.state))

    def test_export_cannot_replace_lock_while_service_owns_inode(self):
        lock_path = self.state / 'monitor.lock'
        with InstanceLock(lock_path):
            before = lock_path.stat().st_ino
            code, _, _ = self.invoke('export', '--format', 'json', '--output', str(lock_path))
            self.assertNotEqual(code, 0)
            self.assertEqual(lock_path.stat().st_ino, before)
            with self.assertRaises(RuntimeError):
                with InstanceLock(lock_path):
                    pass

    def test_export_refuses_configuration_credentials_and_recovery_paths(self):
        secret = self.cfg_path.parent / 'douyin_session'
        secret.write_text('0'*32)
        secret.chmod(0o600)
        candidates = [self.cfg_path, secret, self.cfg_path.parent/'telegram_token', self.state/'notification-recovery.json', self.root/'logs/app.log']
        for target in candidates:
            before = target.read_bytes() if target.exists() else None
            with self.subTest(target=target):
                code, _, _ = self.invoke('export', '--format', 'json', '--output', str(target))
                self.assertNotEqual(code, 0)
                self.assertEqual(target.read_bytes() if target.exists() else None, before)
        self.assertEqual(load_config(self.cfg_path), self.cfg)

    def test_audit_uses_actual_retention_and_limits(self):
        args = SimpleNamespace(config=str(self.cfg_path), log_dir=str(self.root/'logs'))
        with patch('douyin_monitor.observability.setup_logging') as setup:
            cli.audit(args, 'fixture')
        self.assertEqual(setup.call_args.kwargs['retention_days'], 30)
        self.assertEqual(setup.call_args.kwargs['max_bytes'], 2048)
        self.assertEqual(setup.call_args.kwargs['total_bytes'], 8192)

    def test_completed_backup_not_reported_failed_when_audit_is_unavailable(self):
        with patch('douyin_monitor.observability.setup_logging', side_effect=OSError('SYNTHETIC_DISK_FAILURE')), patch.object(cli, 'read_secret') as read_secret:
            code, out, err = self.invoke('backup', '--output', str(self.root/'backups'))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertTrue(Path(result['backup']).is_dir())
        self.assertIn('audit_log_unavailable', err)
        read_secret.assert_not_called()

    def test_health_probe_uses_installation_root_data_directory(self):
        from douyin_monitor.health import main
        import time
        store = Store(self.state / 'monitor.sqlite3')
        store.runtime_metadata(status='running', heartbeat_at=time.time(), stopped_at=None)
        store.close()
        with patch.dict(os.environ, {'DPM_HOME': str(self.root)}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)

    @unittest.skipUnless(os.geteuid() == 0, 'root-owned service group permission fixture')
    def test_config_preserves_service_group_read_but_secret_stays_private(self):
        os.chown(self.cfg_path.parent, 0, 12345)
        self.cfg_path.parent.chmod(0o750)
        os.chown(self.cfg_path, 0, 12345)
        self.cfg_path.chmod(0o640)
        atomic_write_config(self.cfg_path, self.cfg)
        self.assertEqual(self.cfg_path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.cfg_path.stat().st_gid, 12345)
        fresh = self.cfg_path.parent/'fresh.json'
        atomic_write_config(fresh, self.cfg)
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o640)
        self.assertEqual(fresh.stat().st_gid, 12345)
        from douyin_monitor.credentials import atomic_private_write
        secret = self.cfg_path.parent/'douyin_session'
        atomic_private_write(secret, '0'*32)
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
