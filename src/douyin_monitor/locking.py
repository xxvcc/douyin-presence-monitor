"""One stable inode for all operations requiring exclusive ownership."""
import fcntl
import os
from pathlib import Path
import stat


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        if self.fd is not None:
            raise RuntimeError('lock already held by this instance')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('invalid instance lock')
            if os.geteuid() == 0:
                parent = self.path.parent.stat()
                if info.st_uid not in (0, parent.st_uid):
                    raise ValueError('instance lock owner mismatch')
                if (info.st_uid, info.st_gid) != (parent.st_uid, parent.st_gid):
                    # Root init/maintenance must not strand a non-root daemon.
                    # Keep the SAME inode so held locks and waiters remain valid.
                    os.fchown(fd, parent.st_uid, parent.st_gid)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError('another monitor or maintenance operation is running') from None
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        # Never unlink: waiters and the next owner must lock the same inode.
