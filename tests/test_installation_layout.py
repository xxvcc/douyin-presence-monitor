"""Offline installer guard and deployment layout tests; never register a service."""
import importlib.util
import json
import os
import subprocess
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'packaging/native/install.py'


class InstallationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('dpm_installer', INSTALLER)
        assert spec is not None and spec.loader is not None
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.wheel = self.base / 'douyin_presence_monitor-0.1.0-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel, 'w') as z:
            z.writestr('douyin_presence_monitor-0.1.0.dist-info/METADATA',
                       'Name: douyin-presence-monitor\nVersion: 0.1.0\n')

    def args(self, *extra):
        return ['--wheel', str(self.wheel), '--root', str(self.base/'instance'), *extra]

    def test_dry_run_never_writes_or_creates_account(self):
        with patch.object(self.installer.subprocess, 'run') as run:
            self.assertEqual(self.installer.main(self.args('--dry-run', '--systemd')), 0)
            run.assert_not_called()
        self.assertFalse((self.base/'instance').exists())

    def test_existing_root_is_never_overwritten_even_empty(self):
        (self.base/'instance').mkdir()
        self.assertEqual(self.installer.main(self.args('--no-systemd')), 1)
        self.assertEqual(list((self.base/'instance').iterdir()), [])

    def test_symlink_root_and_ancestor_refused(self):
        destination = self.base/'destination'
        destination.mkdir()
        (self.base/'instance').symlink_to(destination, target_is_directory=True)
        self.assertEqual(self.installer.main(self.args('--dry-run')), 1)
        self.assertEqual(self.installer.main(['--wheel', str(self.wheel), '--root',
                                              str(self.base/'instance'/'child'), '--dry-run']), 1)
        self.assertEqual(list(destination.iterdir()), [])

    def test_traversal_and_unit_injection_refused(self):
        for value in (str(self.base)+'/../escape', str(self.base)+'/bad%name',
                      str(self.base)+'/bad\nExecStart=evil', 'relative/root', '/'):
            with self.subTest(value=value):
                self.assertEqual(self.installer.main(['--wheel', str(self.wheel), '--root', value, '--dry-run']), 1)

    def test_hostile_wheel_member_refused(self):
        with zipfile.ZipFile(self.wheel, 'a') as z:
            z.writestr('../escape', 'no')
        self.assertEqual(self.installer.main(self.args('--dry-run')), 1)
        self.assertFalse((self.base/'instance').exists())

    def test_live_or_existing_release_refused_without_mutation(self):
        root = self.base/'instance'
        (root/'data').mkdir(parents=True)
        (root/'data/monitor.lock').write_text('sentinel lock')
        (root/'releases/0.1.0').mkdir(parents=True)
        (root/'config').mkdir()
        (root/'config/config.json').write_text('do not replace')
        with patch.object(self.installer, 'install') as install:
            self.assertEqual(self.installer.main(self.args('--no-systemd')), 1)
            install.assert_not_called()
        self.assertEqual((root/'config/config.json').read_text(), 'do not replace')
        self.assertEqual((root/'data/monitor.lock').read_text(), 'sentinel lock')

    def test_writable_parent_refused(self):
        self.base.chmod(0o777)
        self.assertEqual(self.installer.main(self.args('--dry-run')), 1)
        self.base.chmod(0o700)

    def test_home_systemd_install_refused_before_writes(self):
        with patch.object(self.installer.os, 'geteuid', return_value=0), \
                patch.object(self.installer, 'install') as install:
            self.assertEqual(self.installer.main(self.args('--systemd')), 1)
            install.assert_not_called()
        self.assertFalse((self.base/'instance').exists())

    @unittest.skipUnless(os.environ.get('DPM_TEST_WHEEL'), 'set DPM_TEST_WHEEL to a locally built wheel')
    def test_real_offline_wheel_install_and_reinstall_guard(self):
        wheel = os.environ['DPM_TEST_WHEEL']
        root = self.base/'instance'
        args = ['--wheel', wheel, '--root', str(root), '--no-systemd']
        with patch.object(self.installer, 'service_account') as account:
            old_umask = os.umask(0o077)
            try:
                self.assertEqual(self.installer.main(args), 0)
            finally:
                os.umask(old_umask)
            account.assert_not_called()
        self.assertEqual(os.readlink(root/'current'), 'releases/0.1.0')
        for name in ('config', 'data', 'logs', 'backups', 'exports'):
            self.assertEqual(stat.S_IMODE((root/name).stat().st_mode), 0o700)
            self.assertEqual(list((root/name).iterdir()), [])
        for name in ('bin', 'releases', 'docs', 'systemd'):
            self.assertEqual(stat.S_IMODE((root/name).stat().st_mode), 0o755)
        result = subprocess.run([str(root/'bin/douyin-monitor'), '--version'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '0.1.0')
        subprocess.run(['/bin/sh', '-n', str(root/'bin/douyin-monitor')], check=True)
        self.assertEqual(self.installer.main(args), 1)
        self.assertTrue((root/'current/venv/bin/python').is_file())

    def test_loaded_systemd_unit_refused_with_mocked_os_registration(self):
        # Never invoke the host service manager, even when testing its guards.
        from types import SimpleNamespace
        with patch.object(self.installer.os.path, 'lexists', return_value=False), \
                patch.object(Path, 'glob', return_value=iter(())), \
                patch.object(self.installer.os, 'access', return_value=True), \
                patch.object(self.installer.subprocess, 'run', return_value=SimpleNamespace(
                    stdout='LoadState=loaded\nActiveState=active\n')) as run:
            with self.assertRaises(ValueError):
                self.installer.check_systemd_registration()
            self.assertEqual(run.call_args.args[0][1], 'show')

    def test_systemd_dropin_refused_before_manager_call(self):
        with patch.object(self.installer.os.path, 'lexists', side_effect=lambda p: str(p).endswith('.d')), \
                patch.object(self.installer.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                self.installer.check_systemd_registration()
            run.assert_not_called()

    def test_native_template_only_loads_secrets_and_real_config(self):
        unit = self.installer.render_unit(self.base/'instance')
        self.assertNotIn('@DPM_HOME@', unit)
        self.assertNotIn('LoadCredential=config', unit)
        self.assertIn('--config '+str(self.base/'instance/config/config.json'), unit)
        for forbidden in ('DynamicUser=', 'StateDirectory=', 'LogsDirectory=', '/var/lib/', '/etc/douyin'):
            self.assertNotIn(forbidden, unit)
        self.assertIn('User=douyin-monitor', unit)
        self.assertIn('Group=douyin-monitor', unit)

    def test_docker_one_root_and_setup_override(self):
        text = (ROOT/'packaging/docker/compose.yaml').read_text()
        self.assertIn('DPM_HOME: /app', text)
        self.assertIn('DPM_INSTANCE_ROOT', text)
        self.assertIn('/app/config', text)
        self.assertNotIn('ports:', text)
        self.assertIn('read_only: true', text)
        setup = (ROOT/'packaging/docker/compose.setup.yaml').read_text()
        self.assertIn('read_only: false', setup)
        self.assertIn('target: /app/config', setup)


if __name__ == '__main__':
    unittest.main()
