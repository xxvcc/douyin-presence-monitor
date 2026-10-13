"""Recognize only systemd's two root-managed private-directory aliases.

Do not globally resolve arbitrary operator-supplied symlinks: maintenance and
credential writers must continue rejecting those. DynamicUser creates these
specific aliases legitimately, including for root's offline management CLI.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

_MANAGED_ALIASES = {
    Path('/var/lib/douyin-presence-monitor'): Path('/var/lib/private/douyin-presence-monitor'),
    Path('/var/log/douyin-presence-monitor'): Path('/var/log/private/douyin-presence-monitor'),
}


def _trusted_alias_parent(alias: Path) -> bool:
    try:
        if alias.lstat().st_uid != 0:
            return False
        for parent in (alias.parent, *alias.parent.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return False
        return True
    except OSError:
        return False


def installation_root() -> Path:
    value = Path(os.environ.get('DPM_HOME', '/opt/douyin-presence-monitor'))
    if not value.is_absolute():
        raise ValueError('DPM_HOME must be an absolute installation directory')
    return value


def managed_path(value) -> Path:
    path = Path(os.path.abspath(value))
    for alias, destination in _MANAGED_ALIASES.items():
        if path == alias or alias in path.parents:
            if not alias.is_symlink():
                return path
            if not _trusted_alias_parent(alias) or alias.resolve() != destination:
                raise ValueError('untrusted managed-directory alias')
            return destination / path.relative_to(alias)
    return path
