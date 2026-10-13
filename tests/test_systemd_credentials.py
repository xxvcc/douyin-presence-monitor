"""systemd 0440 read-only credential mounts are not ordinary group-readable secrets."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from douyin_monitor import credentials


class SystemdCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mount_root = self.root/'credentials'
        self.directory = self.mount_root/'fixture.service'
        self.directory.mkdir(parents=True)
        self.path = self.directory/'douyin_session'
        self.path.write_text('0'*32+'\n')
        self.path.chmod(0o440)

    def test_ordinary_group_readable_source_remains_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                credentials.read_secret(self.path)

    def test_exact_readonly_systemd_mount_is_accepted(self):
        with patch.object(credentials, '_SYSTEMD_CREDENTIAL_ROOT', self.mount_root, create=True), \
             patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(self.directory)}), \
             patch.object(os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(credentials.read_secret(self.path), '0'*32)

    def test_writable_or_mismatched_credential_scope_is_refused(self):
        with patch.object(credentials, '_SYSTEMD_CREDENTIAL_ROOT', self.mount_root, create=True), \
             patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(self.directory)}), \
             patch.object(os, 'fstatvfs', return_value=SimpleNamespace(f_flag=0)):
            with self.assertRaises(ValueError):
                credentials.read_secret(self.path)
        with patch.object(credentials, '_SYSTEMD_CREDENTIAL_ROOT', self.mount_root, create=True), \
             patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(self.mount_root/'other.service')}), \
             patch.object(os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            with self.assertRaises(ValueError):
                credentials.read_secret(self.path)

    def test_environment_cannot_trust_an_arbitrary_readonly_directory(self):
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(self.directory)}), \
             patch.object(os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            with self.assertRaises(ValueError):
                credentials.read_secret(self.path)


if __name__ == '__main__':
    unittest.main()
