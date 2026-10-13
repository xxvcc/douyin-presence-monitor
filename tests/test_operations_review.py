"""Concrete operations review regressions; synthetic data, portable TMPDIR only."""
from contextlib import redirect_stderr
import fcntl
import io
import json
import logging
import multiprocessing
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from douyin_monitor import maintenance as m, observability as o, paths
if __package__:
    from . import test_operations as base
else:
    import test_operations as base


def _logging_worker(directory, worker, barrier, connection, jail=None, uid=None):
    try:
        if jail is not None:
            os.chroot(jail)
            os.chdir('/')
        if uid is not None:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            logger = o.setup_logging(directory, max_bytes=450, total_bytes=20_000_000)
            barrier.wait(timeout=15)
            for i in range(100):
                logger.warning('worker-record', extra={'fields': {'worker': worker, 'i': i, 'padding': 'x' * 80}})
        connection.send({'stderr': stderr.getvalue()})
    except BaseException as exc:
        connection.send({'error': repr(exc)})
    finally:
        connection.close()


class LoggingReviewTests(unittest.TestCase):
    root: Path
    logs: Path
    setUp = base.LoggingTests.setUp
    tearDown = base.LoggingTests.tearDown

    def test_parent_symlink_does_not_chmod_or_write_external_directory(self):
        external = self.root / 'external'
        external.mkdir(mode=0o755)
        (external / 'logs').mkdir(mode=0o755)
        alias = self.root / 'alias'
        alias.symlink_to(external, target_is_directory=True)
        before = (external / 'logs').stat()
        with self.assertRaises((OSError, ValueError)):
            o._private_dir(alias / 'logs')
        after = (external / 'logs').stat()
        self.assertEqual((before.st_mode, before.st_uid, before.st_gid),
                         (after.st_mode, after.st_uid, after.st_gid))
        self.assertEqual(list((external / 'logs').iterdir()), [])
        with self.assertRaises((OSError, ValueError)):
            o._private_dir(alias / 'not-created' / 'logs')
        self.assertFalse((external / 'not-created').exists())

    def test_managed_alias_remains_narrowly_supported(self):
        actual = self.root / 'private' / 'monitor'
        actual.mkdir(parents=True)
        alias = self.root / 'monitor'
        alias.symlink_to(actual, target_is_directory=True)
        with patch.object(paths, '_MANAGED_ALIASES', {alias: actual}), \
                patch.object(paths, '_trusted_alias_parent', return_value=True):
            logger = o.setup_logging(alias / 'logs')
            logger.info('managed-alias')
        self.assertEqual(o.read_logs(actual / 'logs')[0]['message'], 'managed-alias')

    def _workers(self, *, mixed_uid=False):
        self.logs.mkdir(mode=0o700)
        if mixed_uid:
            # Only this throwaway chroot root is traversable, not the real /root
            # or TMPDIR parents; the owned log directory stays 0700.
            self.root.chmod(0o755)
            os.chown(self.logs, 65534, 65534)
        ctx = multiprocessing.get_context('fork')
        barrier = ctx.Barrier(4)
        workers = []
        for worker in range(4):
            parent, child = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=_logging_worker,
                args=('/logs' if mixed_uid else str(self.logs), worker, barrier, child,
                      str(self.root) if mixed_uid else None,
                      65534 if mixed_uid and worker % 2 else None))
            proc.start()
            child.close()
            workers.append((proc, parent))
        try:
            for proc, connection in workers:
                self.assertTrue(connection.poll(45), 'logging child timed out')
                result = connection.recv()
                self.assertNotIn('error', result, result)
                self.assertEqual(result['stderr'], '', result)
                proc.join(timeout=5)
                self.assertEqual(proc.exitcode, 0)
        finally:
            for proc, connection in workers:
                if proc.is_alive():
                    proc.kill()
                proc.join(timeout=5)
                connection.close()
        for stream in ('app', 'error'):
            rows = o.read_logs(self.logs, stream=stream, limit=10000)
            self.assertEqual(len(rows), 400)
            self.assertEqual({(r['fields']['worker'], r['fields']['i']) for r in rows},
                             {(w, i) for w in range(4) for i in range(100)})
        if mixed_uid:
            for path in self.logs.iterdir():
                info = path.stat()
                self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                                 (65534, 65534, 0o600), path.name)

    def test_real_multiprocess_rotation_has_no_missing_records_or_fallback(self):
        self._workers()

    @unittest.skipUnless(os.geteuid() == 0, 'requires root to exercise real root/nonroot UID ownership and chroot isolation')
    def test_root_and_nonroot_writers_preserve_daemon_ownership(self):
        self._workers(mixed_uid=True)

    def test_writer_lock_is_stable_separate_private_and_not_pruned(self):
        logger = o.setup_logging(self.logs, max_bytes=400, total_bytes=800)
        logger.info('first')
        lock = self.logs / '.writer.lock'
        self.assertTrue(lock.exists(), 'dedicated stable writer lock is required')
        info = lock.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_size, 0)
        os.utime(lock, (1, 1))
        for i in range(20):
            logger.warning('record %s', i)
        self.assertEqual(lock.stat().st_ino, info.st_ino)
        self.assertFalse((self.logs / 'monitor.lock').exists())
        self.assertLessEqual(sum(p.stat().st_size for p in self.logs.iterdir()), 800)

    def test_unsafe_lock_symlink_and_hardlink_leave_external_file_untouched(self):
        logger = o.setup_logging(self.logs)
        victim = self.root / 'victim'
        victim.write_text('untouched')
        victim.chmod(0o644)
        lock = self.logs / '.writer.lock'
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    lock.symlink_to(victim)
                else:
                    os.link(victim, lock)
                handler = next(h for h in logger.handlers if isinstance(h, o._JSONHandler))
                handler._last_fallback = float('-inf')
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    logger.info('must not persist')
                self.assertIn('logging_fallback', stderr.getvalue())
                self.assertFalse((self.logs / 'app.log').exists())
                self.assertEqual(victim.read_text(), 'untouched')
                self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
                lock.unlink()

    def test_failed_append_releases_writer_lock_and_keeps_cap(self):
        logger = o.setup_logging(self.logs, max_bytes=400, total_bytes=800, secrets=('hidden-value',))
        stderr = io.StringIO()
        with patch.object(o, '_append_private', side_effect=OSError('hidden-value')), redirect_stderr(stderr):
            logger.error('hidden-value')
        self.assertNotIn('hidden-value', stderr.getvalue())
        self.assertIn('logging_fallback', stderr.getvalue())
        with (self.logs / '.writer.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info('recovered')
        self.assertEqual(o.read_logs(self.logs)[-1]['message'], 'recovered')
        self.assertLessEqual(sum(p.stat().st_size for p in self.logs.iterdir()), 800)


    def test_directory_swap_after_validation_never_redirects_writer(self):
        logger = o.setup_logging(self.logs)
        external = self.root / 'external'
        external.mkdir(mode=0o755)
        saved = self.root / 'pinned-logs'
        real_lock = o._writer_lock
        def swap(directory_fd):
            self.logs.rename(saved)
            self.logs.symlink_to(external, target_is_directory=True)
            return real_lock(directory_fd)
        with patch.object(o, '_writer_lock', side_effect=swap):
            logger.info('pinned-directory')
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
        self.assertEqual(o.read_logs(saved)[0]['message'], 'pinned-directory')

    def test_partial_append_failure_still_prunes_cap_and_recovers(self):
        logger = o.setup_logging(self.logs, max_bytes=400, total_bytes=450)
        logger.info('first')
        (self.logs / 'audit.log').write_bytes(b'x' * 250)
        real_write = os.write
        calls = 0
        def disk_full(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:100])
            raise OSError('disk full')
        stderr = io.StringIO()
        with patch.object(o.os, 'write', side_effect=disk_full), redirect_stderr(stderr):
            logger.warning('interrupted')
        self.assertIn('logging_fallback', stderr.getvalue())
        self.assertLessEqual(sum(p.stat().st_size for p in self.logs.iterdir()), 450)
        logger.info('healthy-after-short-write')
        self.assertEqual(o.read_logs(self.logs)[-1]['message'], 'healthy-after-short-write')
        self.assertLessEqual(sum(p.stat().st_size for p in self.logs.iterdir()), 450)


class ExportReviewTests(unittest.TestCase):
    root: Path
    config: Path
    lock: Path
    dbpath: Path
    setUp = base.MaintenanceTests.setUp
    tearDown = base.MaintenanceTests.tearDown

    def test_existing_config_and_lock_are_never_replaced(self):
        self.lock.write_bytes(b'lock sentinel')
        for output in (self.config, self.lock):
            with self.subTest(path=output.name):
                before = output.stat()
                contents = output.read_bytes()
                with self.assertRaises((ValueError, FileExistsError)):
                    m.export_data(self.dbpath, output, format='json')
                after = output.stat()
                self.assertEqual(contents, output.read_bytes())
                self.assertEqual((before.st_ino, before.st_mode, before.st_uid, before.st_gid),
                                 (after.st_ino, after.st_mode, after.st_uid, after.st_gid))

    def test_existing_output_aliases_are_never_replaced(self):
        self.lock.write_bytes(b'lock sentinel')
        for kind in ('symlink', 'dangling', 'hardlink'):
            with self.subTest(kind=kind):
                output = self.root / kind
                if kind == 'hardlink':
                    os.link(self.lock, output)
                else:
                    output.symlink_to(self.lock if kind == 'symlink' else self.root / 'absent')
                info = output.lstat()
                with self.assertRaises((ValueError, FileExistsError)):
                    m.export_data(self.dbpath, output, format='json')
                self.assertEqual(output.lstat().st_ino, info.st_ino)
                self.assertEqual(self.lock.read_bytes(), b'lock sentinel')

    def test_output_created_during_export_is_not_replaced(self):
        output = self.root / 'raced.json'
        real_inspect = m._inspect
        def create_during_export(*args, **kwargs):
            result = real_inspect(*args, **kwargs)
            output.write_bytes(b'created by another process')
            return result
        with patch.object(m, '_inspect', side_effect=create_during_export):
            with self.assertRaises(FileExistsError):
                m.export_data(self.dbpath, output, format='json')
        self.assertEqual(output.read_bytes(), b'created by another process')
        self.assertEqual(list(self.root.glob('.dpm-export-*')), [])

    def test_symlink_or_hardlink_created_during_export_is_unchanged(self):
        self.lock.write_bytes(b'held lock')
        real_inspect = m._inspect
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                output = self.root / ('raced-' + kind)
                def create_during_export(*args, **kwargs):
                    result = real_inspect(*args, **kwargs)
                    if kind == 'symlink':
                        output.symlink_to(self.lock)
                    else:
                        os.link(self.lock, output)
                    return result
                with patch.object(m, '_inspect', side_effect=create_during_export):
                    with self.assertRaises(FileExistsError):
                        m.export_data(self.dbpath, output, format='json')
                self.assertEqual(self.lock.read_bytes(), b'held lock')
                self.assertEqual(output.is_symlink(), kind == 'symlink')
                self.assertEqual(output.stat().st_ino, self.lock.stat().st_ino)
                self.assertEqual(list(self.root.glob('.dpm-export-*')), [])

    def test_publish_failure_cleans_stage_without_creating_output(self):
        output = self.root / 'failed.json'
        with patch.object(m.os, 'link', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                m.export_data(self.dbpath, output, format='json')
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob('.dpm-export-*')), [])

    def test_successful_publication_is_private_complete_and_single_link(self):
        output = self.root / 'new.json'
        result = m.export_data(self.dbpath, output, format='json')
        self.assertEqual(result['count'], 1)
        self.assertEqual(json.loads(output.read_text())[0]['id'], 7)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(output.stat().st_nlink, 1)
        self.assertEqual(list(self.root.glob('.dpm-export-*')), [])


if __name__ == '__main__':
    unittest.main()
