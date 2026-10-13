#!/usr/bin/env python3
"""Fresh Linux installation from a trusted local wheel; no network, init or start.

Run from the unpacked source distribution (contains unit template and docs).
Existing roots, including incomplete installations, are deliberately refused.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import grp
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import sys
import venv
import zipfile

SOURCE = Path(__file__).resolve().parents[2]
UNIT = 'douyin-presence-monitor.service'
ACCOUNT = 'douyin-monitor'
DEFAULT_ROOT = '/opt/douyin-presence-monitor'
DIRECTORIES = ('bin', 'releases', 'config', 'data', 'logs', 'backups', 'exports', 'systemd', 'docs')


def plain_path(value: str) -> Path:
    # Validate the ORIGINAL spelling, before pathlib removes dot components.
    if (not value.startswith('/') or value == '/' or
            not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or
            any(p in ('.', '..', '') for p in value[1:].split('/'))):
        raise ValueError('use an absolute path with simple ASCII components; no dot segments')
    path = Path(value)
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise ValueError('symlink path refused')
    return path


def trusted_parent(root: Path) -> None:
    if not root.parent.is_dir():
        raise ValueError('parent must already exist')
    for parent in (root.parent, *root.parent.parents):
        info = parent.stat()
        if info.st_uid not in (0, os.geteuid()):
            raise ValueError('parent is owned by another user')
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise ValueError('parent is writable by other users')


def wheel_version(wheel: Path) -> str:
    if not wheel.is_file() or wheel.suffix != '.whl':
        raise ValueError('a local wheel is required')
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('duplicate wheel members')
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.filename or
                    stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError('unsafe wheel member')
        metadata = [n for n in names if n.endswith('.dist-info/METADATA')]
        if len(metadata) != 1:
            raise ValueError('wheel metadata missing or ambiguous')
        data = BytesParser().parsebytes(archive.read(metadata[0]))
        version = data.get('Version', '')
        if (data.get('Name') != 'douyin-presence-monitor' or
                not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.]+)?', version) or
                data.get_all('Requires-Dist')):
            raise ValueError('expected a standard-library-only douyin-presence-monitor wheel')
        if wheel.name != f'douyin_presence_monitor-{version}-py3-none-any.whl':
            raise ValueError('wheel filename does not match metadata')
        return version


def render_unit(root: Path) -> str:
    return (SOURCE/'packaging/systemd'/UNIT).read_text().replace('@DPM_HOME@', str(root))


def write_new(path: Path, text: str, mode: int = 0o644) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(mode)


def service_account(root: Path) -> tuple[int, int]:
    """Called ONLY on explicitly requested real systemd installation."""
    try:
        group = grp.getgrnam(ACCOUNT)
    except KeyError:
        subprocess.run(['/usr/sbin/groupadd', '--system', ACCOUNT], check=True)
        group = grp.getgrnam(ACCOUNT)
    try:
        user = pwd.getpwnam(ACCOUNT)
    except KeyError:
        subprocess.run(['/usr/sbin/useradd', '--system', '--gid', ACCOUNT,
                        '--home-dir', str(root), '--no-create-home',
                        '--shell', '/usr/sbin/nologin', ACCOUNT], check=True)
        user = pwd.getpwnam(ACCOUNT)
    if not 0 < user.pw_uid < 1000 or user.pw_gid != group.gr_gid or user.pw_shell not in ('/usr/sbin/nologin', '/sbin/nologin', '/bin/false'):
        raise ValueError('existing service account is not a dedicated system account')
    return user.pw_uid, group.gr_gid


def install(root: Path, wheel: Path, version: str, systemd: bool) -> None:
    # Exclusive creation is the overwrite/live-instance guard. No force option.
    root.mkdir(mode=0o700)
    try:
        for name in DIRECTORIES:
            (root/name).mkdir(mode=0o700 if name in ('config', 'data', 'logs', 'backups', 'exports') else 0o755)
        release = root/'releases'/version
        release.mkdir(mode=0o755)
        scratch = release/'.tmp'
        scratch.mkdir(mode=0o700)
        # pip cache/temp and the input wheel stay in the installation root.
        local_wheel = release/wheel.name
        with wheel.open('rb') as source, local_wheel.open('xb') as dest:
            shutil.copyfileobj(source, dest)
        local_wheel.chmod(0o644)
        venv.EnvBuilder(with_pip=True, symlinks=False).create(release/'venv')
        python = release/'venv/bin/python'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'PIP_', 'DPM_'))}
        env.update(TMPDIR=str(scratch), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        subprocess.run([str(python), '-I', '-m', 'pip', '--isolated', 'install',
                        '--no-index', '--no-deps', '--no-cache-dir', '--no-compile', str(local_wheel)],
                       env=env, check=True)
        subprocess.run([str(python), '-I', '-m', 'douyin_monitor', '--version'], env=env, check=True)
        shutil.rmtree(scratch)
        # Root-derived stable launcher. No shell eval, no /usr/local/bin link.
        write_new(root/'bin/douyin-monitor', '#!/bin/sh\nset -eu\n'
                  'DPM_HOME=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
                  'export DPM_HOME\nexport PYTHONDONTWRITEBYTECODE=1\n'
                  'exec "$DPM_HOME/current/venv/bin/python" -I -B -u -m douyin_monitor "$@"\n', 0o755)
        (root/'current').symlink_to('releases/'+version, target_is_directory=True)
        write_new(root/'systemd'/UNIT, render_unit(root))
        for src, name in ((SOURCE/'README.md', 'README.md'), (SOURCE/'SECURITY.md', 'SECURITY.md'),
                          (SOURCE/'CHANGELOG.md', 'CHANGELOG.md'), (SOURCE/'LICENSE', 'LICENSE'),
                          (SOURCE/'docs/operations.md', 'operations.md')):
            write_new(root/'docs'/name, src.read_text())
        if systemd:
            uid, gid = service_account(root)
            os.chown(root/'config', 0, gid)
            (root/'config').chmod(0o750)
            for name in ('data', 'logs', 'backups', 'exports'):
                os.chown(root/name, uid, gid)
        root.chmod(0o755)
        if systemd:
            subprocess.run(['/usr/bin/systemctl', 'link', str(root/'systemd'/UNIT)], check=True)
            subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True)
            # Read back the exact link; never enable/start as an installation side effect.
            link = Path('/etc/systemd/system')/UNIT
            if not link.is_symlink() or link.resolve() != root/'systemd'/UNIT:
                raise ValueError('systemd registration link verification failed')
        print(json.dumps({'installed': str(root), 'version': version, 'systemd_registered': systemd,
                          'initialized': False, 'started': False}))
    except BaseException:
        # Never recursively remove a partial install automatically. Preserve for review.
        print('Installation incomplete; inspect the new root before removing or retrying.', file=sys.stderr)
        raise


def check_systemd_registration() -> None:
    # Do not replace an existing vendor/local unit or live legacy deployment.
    for directory in ('/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system'):
        if (os.path.lexists(Path(directory)/UNIT) or
                os.path.lexists(Path(directory)/(UNIT+'.d')) or
                any(Path(directory).glob('*.wants/'+UNIT)) or
                any(Path(directory).glob('*.requires/'+UNIT))):
            raise ValueError('existing systemd registration/drop-ins refused; follow migration instructions')
    for binary in ('/usr/bin/systemctl', '/usr/sbin/useradd', '/usr/sbin/groupadd'):
        if not os.access(binary, os.X_OK):
            raise ValueError('required system tool unavailable')
    # Catch active/transient or still-loaded units even if their file was removed.
    state = subprocess.run(['/usr/bin/systemctl', 'show', UNIT,
                            '--property=LoadState,ActiveState'],
                           capture_output=True, text=True, check=True)
    values = dict(line.split('=', 1) for line in state.stdout.splitlines() if '=' in line)
    if values.get('LoadState') != 'not-found' or values.get('ActiveState') != 'inactive':
        raise ValueError('existing or active systemd unit refused')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True, help='absolute path to trusted offline wheel')
    parser.add_argument('--root', default=os.environ.get('DPM_HOME', DEFAULT_ROOT))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--systemd', action='store_true', help='root only: create account and register unit; NEVER start')
    mode.add_argument('--no-systemd', action='store_true', help='default: no account or OS registration; nonroot supported')
    parser.add_argument('--dry-run', action='store_true', help='validate and print plan; no writes or subprocesses')
    args = parser.parse_args(argv)
    try:
        root, wheel = plain_path(args.root), plain_path(args.wheel)
        trusted_parent(root)
        if root.exists():
            raise ValueError('existing installation root refused (including empty, live or partial roots)')
        version = wheel_version(wheel)
        unit = render_unit(root)  # Check bundled assets before any writes.
        if '@DPM_HOME@' in unit:
            raise ValueError('unrendered unit')
        for name in ('README.md', 'SECURITY.md', 'CHANGELOG.md', 'LICENSE', 'docs/operations.md'):
            if not (SOURCE/name).is_file():
                raise ValueError('run installer from complete source distribution')
        if args.dry_run:
            print(json.dumps({'dry_run': True, 'root': str(root), 'release': str(root/'releases'/version),
                              'directories': list(DIRECTORIES), 'systemd_requested': args.systemd,
                              'initialize': False, 'start': False}))
            return 0
        if args.systemd:
            if os.geteuid() != 0:
                raise ValueError('--systemd requires root')
            if not any(parent in root.parents for parent in (Path('/opt'), Path('/srv'))):
                raise ValueError('--systemd roots must be below /opt or /srv (ProtectHome=yes)')
            for parent in (root.parent, *root.parent.parents):
                info = parent.stat()
                if info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o001:
                    raise ValueError('systemd ancestors must be root-owned, traversable and not writable by others')
            check_systemd_registration()
        previous_umask = os.umask(0o022)
        try:
            install(root, wheel, version, args.systemd)
        finally:
            os.umask(previous_umask)
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print('Install refused/failed: '+str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
