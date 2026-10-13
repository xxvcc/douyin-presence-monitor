"""Root setup must not strand a service-owned state directory behind a root-only lock."""
import os
from pathlib import Path
import tempfile
import unittest

from douyin_monitor.locking import InstanceLock


class LockSafetyTests(unittest.TestCase):
    def test_hardlinked_lock_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); victim=root/'private-file'; victim.write_text('SYNTHETIC')
            path=root/'monitor.lock'; os.link(victim,path)
            with self.assertRaises(ValueError):
                with InstanceLock(path):
                    pass
            self.assertEqual(victim.read_text(),'SYNTHETIC')

    @unittest.skipUnless(os.geteuid()==0 and hasattr(os,'chroot'), 'requires real root/nonroot permission boundary')
    def test_root_initialization_preserves_service_owner_and_nonroot_can_lock(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); root.chmod(0o755)
            state=root/'state'; state.mkdir(mode=0o700); os.chown(state,65534,65534)
            path=state/'monitor.lock'
            with InstanceLock(path):
                self.assertEqual(path.stat().st_uid,65534)
                self.assertEqual(path.stat().st_gid,65534)
                self.assertEqual(path.stat().st_mode&0o777,0o600)
            pid=os.fork()
            if pid==0:
                try:
                    os.chroot(root); os.chdir('/'); os.setgroups([]); os.setgid(65534); os.setuid(65534)
                    with InstanceLock('/state/monitor.lock'):
                        pass
                except BaseException:
                    os._exit(1)
                os._exit(0)
            _,status=os.waitpid(pid,0)
            self.assertEqual(os.waitstatus_to_exitcode(status),0)


if __name__=='__main__':
    unittest.main()
