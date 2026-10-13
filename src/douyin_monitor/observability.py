"""Private, bounded JSON logs. No root logger changes or third-party dependencies.

One handler owns all three streams so failures are rate limited once, not once
per destination. Files are reopened on each write: rotation never leaves stale
file descriptors in the collector or sender threads. Only our exact namespace
is ever considered for retention. Logging must not stop presence collection.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import heapq
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import uuid
from typing import Any

REDACTED = '[REDACTED]'
_STREAMS = ('app', 'error', 'audit')
_OWN_FILE = re.compile(r'^(app|error|audit)\.log(?:\.\d{8}T\d{12}Z\.[0-9a-f]{16})?$')
_SECRET_KEY = re.compile(r'^(?:authorization|proxy_authorization|cookie|set_cookie|sessionid|sessionid_ss|session_id|douyin_session|telegram_token|bot_token|access_token|refresh_token|token|password|passwd|secret|api_key|credentials)$', re.I)
_PATTERNS = (
    # Whole headers may hold several credentials; hiding the complete value is deliberate.
    (re.compile(r'''(?i)\b((?:proxy[-_ ]?)?authorization|set-cookie|cookie)["']?\s*[=:]\s*[^\r\n]+'''), r'\1: ' + REDACTED),
    (re.compile(r'(?i)(https?://)[^\s/@]+@'), r'\1' + REDACTED + '@'),
    (re.compile(r'(?i)(https?://[^\s/]+/(?:file/)?bot)[^/\s\"\'<>]+'), r'\1' + REDACTED),
    (re.compile(r'(?<![\w])\d{5,}:[A-Za-z0-9_-]{20,}'), REDACTED),
    (re.compile(r'''(?ix)(["']?\b(?:sessionid(?:_ss)?|session_id|douyin_session|telegram_token|bot_token|access_token|refresh_token|password|passwd|secret|api_key|token)\b["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s;,}\]]+)'''), r'\1' + REDACTED),
    (re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+'), r'\1 ' + REDACTED),
)
_SETUP_LOCK = threading.RLock()

from .paths import managed_path


def _text(value):
    try:
        return str(value)
    except Exception:
        return '<unprintable>'


def redact(value, secrets=()) -> Any:
    """Return JSON-safe data; scrub keys, strings, tracebacks, cycles and objects.

    Exact values are replaced longest-first, including occurrences within larger
    strings. Register runtime credential values even when they are not in a
    recognizable cookie/URL. This function never mutates the input.
    """
    if isinstance(secrets, str):
        secrets = (secrets,)
    exact = sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True)
    seen = set()

    def text(s):
        for secret in exact:
            s = s.replace(secret, REDACTED)
        for pattern, replacement in _PATTERNS:
            s = pattern.sub(replacement, s)
        return s

    def walk(obj, depth=0):
        if isinstance(obj, str):
            return text(obj)
        if obj is None or isinstance(obj, (bool, int)):
            return obj
        if isinstance(obj, float):
            return obj if math.isfinite(obj) else text(str(obj))
        if isinstance(obj, bytes):
            return text(obj.decode('utf-8', errors='replace'))
        if depth > 30:
            return '<max-depth>'
        if isinstance(obj, (dict, list, tuple, set, frozenset)):
            if id(obj) in seen:
                return '<cycle>'
            seen.add(id(obj))
            try:
                if isinstance(obj, dict):
                    return {text(_text(k)): REDACTED if _SECRET_KEY.fullmatch(_text(k).replace('-', '_')) else walk(v, depth + 1)
                            for k, v in obj.items()}
                return [walk(v, depth + 1) for v in obj]
            finally:
                seen.remove(id(obj))
        return text(_text(obj))

    return walk(value)


@contextmanager
def _private_directory_fd(path):
    """Walk without following *any* untrusted component, then pin the directory.

    Only managed_path's explicit trusted aliases are expanded. All creation,
    permission changes and writer operations use descriptors, so a parent rename
    or symlink substitution cannot redirect them to an external directory.
    """
    path = managed_path(path)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError('log directory components must be real directories') from None
                raise
            os.close(fd)
            fd = child
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def _private_dir(path):
    path = managed_path(path)
    with _private_directory_fd(path):
        return path


def _owned_private(fd, directory_fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError('log path is not a private regular file')
    owner = os.fstat(directory_fd)
    if (info.st_uid, info.st_gid) != (owner.st_uid, owner.st_gid):
        os.fchown(fd, owner.st_uid, owner.st_gid)
    os.fchmod(fd, 0o600)
    return info


@contextmanager
def _writer_lock(directory_fd):
    """Stable independent flock: never rotate, prune or unlink .writer.lock.

    Publish a pre-owned inode, already locked, rather than exposing a transient
    root:root 0600 lock to the daemon. Readers wait for staging-link cleanup
    before checking the link count. No permissions are widened for sharing.
    """
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open('.writer.lock', flags, dir_fd=directory_fd)
    except FileNotFoundError:
        stage = '.writer-stage-' + uuid.uuid4().hex
        staging_fd = os.open(stage, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        try:
            _owned_private(staging_fd, directory_fd)
            fcntl.flock(staging_fd, fcntl.LOCK_EX)
            try:
                os.link(stage, '.writer.lock', src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(stage, dir_fd=directory_fd)
            finally:
                os.close(staging_fd)
        fd = os.open('.writer.lock', flags, dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('writer lock is not a regular file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        _owned_private(fd, directory_fd)
        yield
    finally:
        os.close(fd)


def _append_private(path, data, *, directory_fd=None):
    if directory_fd is None:
        path = Path(path)
        with _private_directory_fd(path.parent) as directory_fd, _writer_lock(directory_fd):
            return _append_private(path.name, data, directory_fd=directory_fd)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=directory_fd)
    try:
        info = _owned_private(fd, directory_fd)
        if info.st_size and os.pread(fd, 1, info.st_size - 1) != b'\n':
            # A disk-full/crash may have left a partial JSON line. Delimit it so
            # a later healthy record remains independently parseable.
            data = b'\n' + data
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short log write')
            view = view[written:]
    finally:
        os.close(fd)


def _level(value):
    if isinstance(value, str):
        value = logging.getLevelNamesMapping().get(value.upper())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('invalid logging level')
    return value


def _since(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
    elif isinstance(value, str):
        try:
            if value == 'today':
                result = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            else:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                result = dt.astimezone().timestamp()
        except (ValueError, OverflowError):
            raise ValueError('since must be today or ISO8601') from None
    else:
        raise ValueError('invalid since value')
    if not math.isfinite(result):
        raise ValueError('invalid since value')
    return result


class _JSONHandler(logging.Handler):
    _dpm_handler = True

    def __init__(self, directory, secrets, retention_days, max_bytes, total_bytes):
        super().__init__()
        self.directory = managed_path(directory)
        self.secrets = tuple(secrets) if not isinstance(secrets, str) else (secrets,)
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.total_bytes = total_bytes
        self._last_fallback = float('-inf')
        self._suppressed = 0

    def _fallback(self, entry):
        now = time.monotonic()
        if now - self._last_fallback < 60:
            self._suppressed += 1
            return
        self._last_fallback = now
        fallback = {**entry, 'logging_fallback': True, 'suppressed': self._suppressed}
        self._suppressed = 0
        try:
            # No raw OSError, path or exception from the logger itself is disclosed.
            sys.stderr.write(json.dumps(fallback, ensure_ascii=False, allow_nan=False) + '\n')
            sys.stderr.flush()
        except Exception:
            pass  # Closed stderr must not turn a disk failure into a collector failure.

    def _files(self, directory_fd):
        result = []
        for name in os.listdir(directory_fd):
            if _OWN_FILE.fullmatch(name):
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    result.append((name, info))
        return result

    def _prune(self, now, directory_fd):
        files = self._files(directory_fd)
        cutoff = now - timedelta(days=self.retention_days).total_seconds()
        for name, info in files:
            if '.log.' in name and info.st_mtime < cutoff:
                os.unlink(name, dir_fd=directory_fd)
        files = sorted(self._files(directory_fd), key=lambda pair: (pair[1].st_mtime_ns, pair[0]))
        total = sum(info.st_size for _, info in files)
        for name, info in files:
            if total <= self.total_bytes:
                break
            os.unlink(name, dir_fd=directory_fd)
            total -= info.st_size

    def _write(self, stream, data, now, directory_fd):
        name = stream + '.log'
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            info = None
        else:
            try:
                # Validate and fix ownership through the inode, never a path
                # chmod that could follow an external symlink during rotation.
                info = _owned_private(fd, directory_fd)
            finally:
                os.close(fd)
        if info is not None:
            old_day = datetime.fromtimestamp(info.st_mtime).astimezone().date()
            new_day = datetime.fromtimestamp(now).astimezone().date()
            if info.st_size and (info.st_size + len(data) + 1 > self.max_bytes or old_day != new_day):
                suffix = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.' + uuid.uuid4().hex[:16]
                os.replace(name, name + '.' + suffix, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        _append_private(name, data, directory_fd=directory_fd)

    def emit(self, record):
        entry: dict[str, Any] = {'timestamp': datetime.fromtimestamp(record.created).astimezone().isoformat(timespec='milliseconds'),
                 'level': record.levelname, 'logger': record.name, 'message': '<unformattable>'}
        try:
            try:
                entry['message'] = record.getMessage()
            except Exception:
                entry['message'] = _text(record.msg)
            fields = getattr(record, 'fields', {})
            entry['fields'] = fields if isinstance(fields, dict) else {'value': fields}
            if record.exc_info:
                entry['exception'] = logging.Formatter().formatException(record.exc_info)
            elif record.exc_text:
                entry['exception'] = record.exc_text
            if record.stack_info:
                entry['stack'] = record.stack_info
            entry = redact(entry, self.secrets)
            data = (json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
            if len(data) > min(self.max_bytes, self.total_bytes):
                self._fallback({**entry, 'logging_reason': 'record_exceeds_size_limit'})
                return
            with _private_directory_fd(self.directory) as directory_fd, _writer_lock(directory_fd):
                now = time.time()
                self._prune(now, directory_fd)
                try:
                    self._write('app', data, now, directory_fd)
                    if record.levelno >= logging.WARNING:
                        self._write('error', data, now, directory_fd)
                    if record.name == 'douyin_monitor.audit' or record.name.startswith('douyin_monitor.audit.'):
                        self._write('audit', data, now, directory_fd)
                finally:
                    # Even a short/disk-full write must not bypass the disk cap.
                    self._prune(now, directory_fd)
        except Exception:
            self._fallback(redact(entry, self.secrets))


def setup_logging(log_dir, *, secrets=(), retention_days=14, max_bytes=10485760,
                  total_bytes=104857600, level='INFO'):
    """Configure only the package logger; repeat calls replace our own handler.

    Disk unavailability is handled lazily on emit, with a once/minute stderr
    fallback (captured by journald under systemd). Foreign handlers are retained.
    A record larger than max_bytes goes to the same rate-limited fallback rather
    than breaking the disk cap. Archive retention uses filesystem mtime.
    """
    for name, value in (('retention_days', retention_days), ('max_bytes', max_bytes), ('total_bytes', total_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(name + ' must be a positive integer')
    level = _level(level)
    try:
        _private_dir(log_dir)
    except OSError:
        pass  # Emit reports disk failures through the rate-limited fallback.
    with _SETUP_LOCK:
        logger = logging.getLogger('douyin_monitor')
        for handler in list(logger.handlers):
            if getattr(handler, '_dpm_handler', False):
                logger.removeHandler(handler)
                handler.close()
        logger.addHandler(_JSONHandler(log_dir, secrets, retention_days, max_bytes, total_bytes))
        logger.setLevel(level)
        logger.propagate = False
        return logger


def read_logs(log_dir, *, stream='app', level=None, target=None, since=None, limit=100):
    """Read the newest matching records in chronological order, including rotations.

    Invalid/incomplete lines are skipped; readers can race safely with rotation.
    A missing directory returns an empty list. Results are bounded to 10,000.
    """
    if stream not in _STREAMS:
        raise ValueError('stream must be app, error or audit')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError('limit must be between 1 and 10000')
    minimum = _level(level) if level is not None else None
    cutoff = _since(since)
    directory = managed_path(log_dir)
    if directory.is_symlink():
        raise ValueError('log directory must not be a symlink')
    try:
        paths = []
        for p in directory.iterdir():
            if _OWN_FILE.fullmatch(p.name) and p.name.startswith(stream + '.log'):
                try:
                    info = p.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        paths.append((info.st_mtime_ns, p.name, p))
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        return []
    result = []
    sequence = 0
    for _, _, path in sorted(paths):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                continue
            for line in file:
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict) or 'timestamp' not in item:
                        continue
                    if minimum is not None and _level(item.get('level', 'NOTSET')) < minimum:
                        continue
                    fields = item.get('fields', {})
                    if target is not None and (not isinstance(fields, dict) or fields.get('target') != target):
                        continue
                    stamp = _since(item['timestamp'])
                    if stamp is None or (cutoff is not None and stamp < cutoff):
                        continue
                    sequence += 1
                    candidate = (stamp, sequence, item)
                    if len(result) < limit:
                        heapq.heappush(result, candidate)
                    elif candidate[:2] > result[0][:2]:
                        heapq.heapreplace(result, candidate)
                except (ValueError, TypeError, OverflowError):
                    continue
    return [item for _, _, item in sorted(result)]
