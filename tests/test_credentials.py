import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from douyin_monitor.credentials import (read_secret, atomic_private_write,
                                        validate_sessionid, validate_telegram_token, prompt_secret)


class CredentialTests(unittest.TestCase):
    def test_validation_does_not_repair_or_leak(self):
        self.assertEqual(validate_sessionid('a'*32), 'a'*32)
        self.assertEqual(validate_telegram_token('123:TEST_token'), '123:TEST_token')
        for fn, values in [(validate_sessionid, ['a'*31, 'a'*32+' ', 'a'*32+'\n', 'x'*32]),
                           (validate_telegram_token, ['123:SECRET bad', '123:SECRET\n', '0:SECRET', 'secret'])]:
            for value in values:
                with self.assertRaises(ValueError) as raised:
                    fn(value)
                self.assertNotIn(value, str(raised.exception))

    def test_atomic_permissions_readonly_and_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'douyin_session'
            atomic_private_write(path, 'a'*32+'\r\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_secret(path), 'a'*32)
            path.chmod(0o400)
            self.assertEqual(read_secret(path), 'a'*32)
            atomic_private_write(path, 'b'*32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_secret(path)

    def test_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'
            atomic_private_write(source, 'a'*32)
            link = Path(tmp)/'douyin_session'
            link.symlink_to(source)
            with self.assertRaises((ValueError, OSError)):
                atomic_private_write(link, 'b'*32)
            with self.assertRaises((ValueError, OSError)):
                read_secret(link)
            self.assertEqual(source.read_text(), 'a'*32)

    def test_prompt_refuses_headless_before_getpass(self):
        with patch('sys.stdin.isatty', return_value=False), patch('getpass.getpass') as prompt:
            with self.assertRaises(ValueError):
                prompt_secret('douyin_session')
            prompt.assert_not_called()

    def test_real_tty_getpass_hides_secret(self):
        import pty
        import select
        import signal
        import time
        secret = 'a' * 32
        pid, master = pty.fork()
        if pid == 0:
            try:
                value = prompt_secret('douyin_session')
                os.write(1, b'VALID\n' if value == secret else b'INVALID\n')
                os._exit(0 if value == secret else 2)
            except BaseException:
                os._exit(3)
        output, sent, status = b'', False, None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try: chunk = os.read(master, 4096)
                    except OSError: break
                    output += chunk
                    if b'(hidden): ' in output and not sent:
                        os.write(master, (secret+'\n').encode())
                        sent = True
                child, status = os.waitpid(pid, os.WNOHANG)
                if child:
                    pid = None
                    break
            if pid is not None:
                child, status = os.waitpid(pid, os.WNOHANG)
                if child: pid = None
            self.assertTrue(sent, output)
            self.assertIn(b'VALID', output)
            self.assertNotIn(secret.encode(), output)
            self.assertIsNone(pid, 'prompt process did not exit')
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            os.close(master)
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def test_unknown_credential_newlines_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'arbitrary'
            atomic_private_write(path, ' value \r\n')
            self.assertEqual(read_secret(path), ' value ')
