"""Private file operations and hidden, TTY-only credential input.

Exceptions contain controlled diagnostics, never credential values.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import warnings


def validate_sessionid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', value):
        raise ValueError('invalid sessionid format')
    return value


def validate_telegram_token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}:[A-Za-z0-9_-]{1,256}', value):
        raise ValueError('invalid bot token format')
    return value


def ensure_plain_path(path):
    """Refuse symlinks in destination or existing parent components."""
    from .paths import managed_path
    path = managed_path(path)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('symlink path refused')
    if path.exists() and not path.is_file():
        raise ValueError('regular file required')
    return path


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, value: str, *, preserve=False):
    if not isinstance(value, str):
        raise ValueError('text required')
    path = ensure_plain_path(path)
    previous = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as output:
            # Preserve owner when permitted; never broaden secret permissions.
            if previous:
                current = os.fstat(output.fileno())
                if (current.st_uid, current.st_gid) != (previous.st_uid, previous.st_gid):
                    os.fchown(output.fileno(), previous.st_uid, previous.st_gid)
            mode = (stat.S_IMODE(previous.st_mode) & 0o640) if preserve and previous else 0o600
            if preserve and previous is None and os.geteuid() == 0:
                parent = path.parent.stat()
                if parent.st_uid == 0 and parent.st_gid != 0 and parent.st_mode & 0o040 and not parent.st_mode & 0o022:
                    # Native installer grants only its service group access to ordinary config.
                    # Secret writes never set preserve and remain owner-only.
                    os.fchown(output.fileno(), 0, parent.st_gid)
                    mode = 0o640
            os.fchmod(output.fileno(), mode or 0o600)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        ensure_plain_path(path)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_private_write(path, value: str):
    """Atomically write exact text, mode 0600; caller supplies final newline."""
    _atomic_write(path, value)


_SYSTEMD_CREDENTIAL_ROOT = Path('/run/credentials')


def _readonly_systemd_credential(path, descriptor, info):
    """Trust 0440 only inside the exact systemd credential scope on a read-only mount."""
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory or path.name not in ('douyin_session', 'telegram_token'):
        return False
    scope = Path(directory)
    return (scope.is_absolute() and scope.parent == _SYSTEMD_CREDENTIAL_ROOT
            and path.parent == scope and info.st_uid in (0, os.geteuid())
            and info.st_nlink == 1 and bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY))


def read_secret(path) -> str:
    path = ensure_plain_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8', newline='') as source:
        info = os.fstat(source.fileno())
        mode = stat.S_IMODE(info.st_mode)
        private = mode in (0o400, 0o600) or (mode == 0o440 and _readonly_systemd_credential(path, source.fileno(), info))
        if not stat.S_ISREG(info.st_mode) or not private:
            raise ValueError('credential must be a private regular file')
        raw = source.read(4097)
        value = raw.rstrip('\r\n')
    if not value or len(raw) > 4096:
        raise ValueError('invalid credential file')
    if path.name == 'douyin_session':
        return validate_sessionid(value)
    if path.name == 'telegram_token':
        return validate_telegram_token(value)
    return value


def require_tty():
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError('interactive TTY required; secrets are never accepted through argv or pipes')
    # getpass may otherwise silently fall back to echoed stdin.
    fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
    os.close(fd)


def prompt_secret(kind: str) -> str:
    require_tty()
    validators = {'douyin_session': validate_sessionid, 'telegram_token': validate_telegram_token}
    if kind not in validators:
        raise ValueError('unknown credential kind')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        value = getpass.getpass(kind + ' (hidden): ')
    return validators[kind](value)
