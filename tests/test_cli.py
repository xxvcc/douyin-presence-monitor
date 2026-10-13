"""CLI tests exercise the real module launcher and inject only absent siblings."""
import contextlib
import fcntl
import io
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from douyin_monitor import cli
from douyin_monitor.config import atomic_write_config, load_config
from douyin_monitor.management import change_outbox, mutate_config, revision

CFG = {'observer_uid': '123', 'target': 'MS4wLjABAAAA_FIXTURE', 'label': 'fixture', 'chat_id': 1}
ROOT = Path(__file__).resolve().parents[1]


class FixtureLock:
    def __init__(self, path): self.path = Path(path)
    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try: fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise
        return self
    def __exit__(self, *args): os.close(self.fd)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'config.json'
        atomic_write_config(self.config, CFG)
        self.state = self.root/'state'
        self.logs = self.root/'logs'
        self.flags = ['--config', str(self.config), '--state-dir', str(self.state),
                      '--credentials', str(self.root/'credentials'), '--log-dir', str(self.logs), '--json']

    def subprocess(self, *args):
        return subprocess.run([sys.executable, '-m', 'douyin_monitor', *args], cwd=ROOT,
                              env={**os.environ, 'PYTHONPATH': str(ROOT/'src')},
                              capture_output=True, text=True)

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*self.flags, *args])
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        result = self.subprocess('--version')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '0.1.0')

    def test_real_subprocess_help_and_config(self):
        result = self.subprocess('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('init', 'run', 'probe', 'status', 'doctor', 'config', 'targets', 'logs', 'events', 'notify', 'credentials', 'backup', 'restore', 'export', 'outbox'):
            self.assertIn(command, result.stdout)
        for action in ('show', 'validate'):
            result = self.subprocess(*self.flags, 'config', action)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('telegram_token', result.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.logs.exists())

    def test_headless_secret_commands_fail_without_files(self):
        for command in [('init',), ('credentials', 'update'), ('notify', 'setup')]:
            result = self.subprocess(*self.flags, *command)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())

    def test_unknown_secret_flags_are_never_echoed(self):
        result = self.subprocess('notify', 'setup', '--token', '123:SECRET_fixture')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('SECRET_fixture', result.stdout+result.stderr)

    def test_status_doctor_missing_db_nonmutating(self):
        for command in [('status',), ('doctor',)]:
            result = self.subprocess(*self.flags, *command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('uninitialized', result.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.logs.exists())

    def test_config_revision_and_live_lock(self):
        self.state.mkdir()
        fake = types.SimpleNamespace(InstanceLock=FixtureLock)
        with patch.dict(sys.modules, {'douyin_monitor.locking': fake}):
            expected = revision(self.config)
            with FixtureLock(self.state/'monitor.lock'), self.assertRaises(BlockingIOError):
                mutate_config(self.config, self.state, lambda c: {**c, 'poll_seconds': 90}, expected)
            self.assertEqual(load_config(self.config)['poll_seconds'], 60)
            mutate_config(self.config, self.state, lambda c: {**c, 'poll_seconds': 90}, expected)
            self.assertEqual(load_config(self.config)['poll_seconds'], 90)
            with self.assertRaises(ValueError):
                mutate_config(self.config, self.state, lambda c: {**c, 'poll_seconds': 120}, expected)

    def test_commands_dispatch_real_mutations(self):
        fake_lock = types.SimpleNamespace(InstanceLock=FixtureLock)
        fake_obs = types.SimpleNamespace(setup_logging=lambda *a, **kw: None)
        with patch.dict(sys.modules, {'douyin_monitor.locking': fake_lock, 'douyin_monitor.observability': fake_obs}):
            self.assertEqual(self.call('config', 'set', 'poll_seconds', '90')[0], 0)
            self.assertEqual(load_config(self.config)['poll_seconds'], 90)
            self.assertNotEqual(self.call('config', 'set', 'chat_id', '2')[0], 0)
            target = 'MS4wLjABAAAA_SECOND'
            self.assertEqual(self.call('targets', 'add', target, '--label', 'second')[0], 0)
            self.assertEqual(self.call('targets', 'pause', target)[0], 0)
            self.assertFalse(load_config(self.config)['targets'][1]['enabled'])
            self.assertEqual(self.call('targets', 'resume', target)[0], 0)
            self.assertEqual(self.call('targets', 'remove', target)[0], 0)
            self.assertEqual(len(load_config(self.config)['targets']), 1)
            self.assertNotEqual(self.call('targets', 'remove', target)[0], 0)

    def test_run_probe_ops_are_lazy_real_dispatch(self):
        calls = []
        runtime = types.SimpleNamespace(run=lambda *a, **kw: calls.append(('run', kw)) or 0,
                                        probe=lambda *a: {'identity_verified': True})
        maintenance = types.SimpleNamespace(export_data=lambda *a, **kw: calls.append(('export', kw)) or {'rows': 0})
        with patch.dict(sys.modules, {'douyin_monitor.runtime': runtime, 'douyin_monitor.maintenance': maintenance}):
            self.assertEqual(self.call('run', '--once', '--no-notify')[0], 0)
            self.assertEqual(calls[0], ('run', {'once': True, 'no_notify': True, 'config_path': str(self.config)}))
            self.assertEqual(self.call('probe')[0], 0)
            self.assertEqual(self.call('export', '--format', 'json', '--output', str(self.root/'out.json'))[0], 0)

    def test_raw_exception_secret_is_not_returned(self):
        runtime = types.SimpleNamespace(probe=lambda *a: (_ for _ in ()).throw(ValueError('123:SECRET_raw')))
        with patch.dict(sys.modules, {'douyin_monitor.runtime': runtime}):
            code, out, err = self.call('probe')
        self.assertEqual(code, 1)
        self.assertNotIn('SECRET', out+err)

    def test_init_creates_private_setup_without_overwrite(self):
        self.config.unlink()
        calls = []
        class Douyin:
            def __init__(self, *a, **kw): pass
            def identity(self): return '123'
        class Telegram:
            def __init__(self, *a, **kw): pass
            def get_me(self): return {'id': 100, 'is_bot': True}
            def get_chat(self, chat): return {'id': chat}
            def send(self, payload): calls.append(payload); return 55
        modules = {'douyin_monitor.transport': types.SimpleNamespace(DouyinClient=Douyin, TelegramClient=Telegram),
                   'douyin_monitor.messages': types.SimpleNamespace(telegram_payload=lambda c, t: {'chat_id': c['chat_id'], 'text': t}),
                   'douyin_monitor.locking': types.SimpleNamespace(InstanceLock=FixtureLock),
                   'douyin_monitor.observability': types.SimpleNamespace(setup_logging=lambda *a, **kw: None)}
        with patch.dict(sys.modules, modules), patch.object(cli, 'require_tty'), \
             patch.object(cli, 'prompt_secret', side_effect=['a'*32, '100:TEST']), \
             patch('builtins.input', side_effect=['yes', CFG['target'], 'fixture', '1', '', '', 'yes']):
            code, out, err = self.call('init')
        self.assertEqual(code, 0, err)
        self.assertTrue(self.config.is_file())
        self.assertEqual(load_config(self.config)['observer_uid'], '123')
        self.assertEqual((self.root/'credentials'/'douyin_session').read_text(), 'a'*32+'\n')
        self.assertEqual((self.root/'credentials'/'telegram_token').stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(calls), 1)
        self.assertIn('不是实际', calls[0]['text'])
        self.assertNotIn('100:TEST', out+err)
        self.assertFalse((self.state/'monitor.sqlite3').exists())
        with patch.object(cli, 'require_tty'), patch.object(cli, 'prompt_secret') as prompt:
            self.assertNotEqual(self.call('init')[0], 0)
            prompt.assert_not_called()

    def test_credentials_update_same_observer_and_revision(self):
        from douyin_monitor.credentials import atomic_private_write
        directory = self.root/'credentials'
        directory.mkdir()
        path = directory/'douyin_session'
        atomic_private_write(path, 'a'*32)
        verified = []
        class Client:
            def __init__(self, *a, **kw): pass
            def verify_identity(self, uid): verified.append(uid)
        modules = {'douyin_monitor.transport': types.SimpleNamespace(DouyinClient=Client),
                   'douyin_monitor.locking': types.SimpleNamespace(InstanceLock=FixtureLock),
                   'douyin_monitor.observability': types.SimpleNamespace(setup_logging=lambda *a, **kw: None)}
        with patch.dict(sys.modules, modules), patch.object(cli, 'require_tty'), \
             patch.object(cli, 'prompt_secret', return_value='b'*32):
            self.assertEqual(self.call('credentials', 'update')[0], 0)
        self.assertEqual(verified, ['123'])
        self.assertEqual(path.read_text(), 'b'*32+'\n')
        with patch.dict(sys.modules, modules), patch.object(cli, 'require_tty'), \
             patch.object(cli, 'prompt_secret', return_value='c'*32), \
             patch.object(Client, 'verify_identity', side_effect=ValueError('wrong account')):
            self.assertNotEqual(self.call('credentials', 'update')[0], 0)
        self.assertEqual(path.read_text(), 'b'*32+'\n')

    def test_recovery_markers_block_run_and_config_changes(self):
        from douyin_monitor.management import RECOVERY_MARKER, RESTORE_MARKER
        self.state.mkdir()
        runtime = types.SimpleNamespace(run=lambda *a, **kw: self.fail('must not run'))
        for name in (RECOVERY_MARKER, RESTORE_MARKER):
            marker = self.state/name
            marker.write_text('{}')
            with patch.dict(sys.modules, {'douyin_monitor.runtime': runtime}):
                self.assertNotEqual(self.call('run', '--once')[0], 0)
            marker.unlink()

    def test_notify_test_is_explicit_and_creates_no_state(self):
        from douyin_monitor.credentials import atomic_private_write
        (self.root/'credentials').mkdir()
        atomic_private_write(self.root/'credentials'/'telegram_token', '100:TEST')
        sent = []
        class Telegram:
            def __init__(self, *a, **kw): pass
            def get_me(self): return {'id': 100, 'is_bot': True}
            def get_chat(self, chat): return {'id': chat}
            def send(self, payload): sent.append(payload); return 5
        modules = {'douyin_monitor.transport': types.SimpleNamespace(TelegramClient=Telegram),
                   'douyin_monitor.messages': types.SimpleNamespace(telegram_payload=lambda c, t: {'chat_id': c['chat_id'], 'text': t}),
                   'douyin_monitor.observability': types.SimpleNamespace(setup_logging=lambda *a, **kw: None)}
        with patch.dict(sys.modules, modules):
            self.assertEqual(self.call('notify', 'test')[0], 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['text'], cli.TEST_MESSAGE)
        self.assertFalse(self.state.exists())

    def test_since_and_limits(self):
        self.assertGreater(cli.parse_since('today'), 0)
        self.assertEqual(cli.parse_since('2020-01-01T00:00:00+00:00'), 1577836800)
        with self.assertRaises(ValueError): cli.parse_since('yesterday')
        with self.assertRaises(ValueError): cli.parse_since('2020-01-01T00:00:00')


@unittest.skipUnless(all(importlib.util.find_spec('douyin_monitor.'+name) is not None for name in ('storage', 'maintenance', 'observability')),
                     'core/ops siblings not merged; run again after integration')
class CLIIntegratedSubprocessTests(unittest.TestCase):
    setUp = CLITests.setUp
    subprocess = CLITests.subprocess
    call = CLITests.call

    def fixture(self):
        from douyin_monitor.storage import Store
        from douyin_monitor.domain import State, Problem, advance
        from douyin_monitor.config import target_config
        self.state.mkdir()
        cfg = load_config(self.config)
        store = Store(self.state/'monitor.sqlite3')
        store.bind(cfg)
        store.bind_notification_bot('100')
        state = State()
        events = advance(state, None, Problem('login_expired'), 1791735000)
        store.save(state, {'observed_at': 1791735000}, events, target_config(cfg, cfg['targets'][0]))
        row = store.pending(1791735000)[0]
        store.mark_failed(row['id'], 'access_denied', 1791735000, 0, permanent=True)
        identifier = row['id']
        store.close()
        return identifier

    def test_real_status_events_outbox_backup_export_restore(self):
        identifier = self.fixture()
        db_path = self.state/'monitor.sqlite3'
        before = db_path.read_bytes()
        for command in [('status',), ('doctor',), ('events', '--limit', '1')]:
            result = self.subprocess(*self.flags, *command)
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)
            self.assertEqual(db_path.read_bytes(), before)
        self.assertFalse((self.root/'credentials').exists())
        result = self.subprocess(*self.flags, 'outbox', 'discard', str(identifier), '--reason', 'operator approved')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'discarded')
        result = self.subprocess(*self.flags, 'logs', '--stream', 'audit')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))
        result = self.subprocess(*self.flags, 'backup', '--output', str(self.root/'backups'))
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = Path(json.loads(result.stdout)['backup'])
        self.assertEqual({p.name for p in backup.iterdir()}, {'config.json', 'monitor.sqlite3', 'manifest.json'})
        for format in ('csv', 'json'):
            destination = self.root/('export.'+format)
            result = self.subprocess(*self.flags, 'export', '--format', format, '--output', str(destination))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(destination.read_text())
        result = self.subprocess(*self.flags, 'config', 'set', 'poll_seconds', '90')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_config(self.config)['poll_seconds'], 90)
        result = self.subprocess(*self.flags, 'restore', str(backup))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_config(self.config)['poll_seconds'], 60)

    def test_notify_setup_real_storage_backup_and_immutable_history(self):
        from douyin_monitor.credentials import atomic_private_write
        from douyin_monitor import transport
        identifier = self.fixture()
        result = self.subprocess(*self.flags, 'outbox', 'discard', str(identifier), '--reason', 'operator approved')
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.root/'credentials'
        directory.mkdir()
        atomic_private_write(directory/'telegram_token', '100:OLD')
        with patch.object(cli, 'require_tty'), patch.object(cli, 'prompt_secret', return_value='200:NEW'), \
             patch('builtins.input', side_effect=['2', '', '', 'yes']), patch.object(transport, 'TelegramClient') as client:
            client.return_value.get_me.return_value = {'id': 200, 'is_bot': True}
            client.return_value.get_chat.return_value = {'id': 2}
            client.return_value.send.return_value = 7
            code, out, err = self.call('notify', 'setup')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertTrue(result['switched'])
        self.assertEqual(load_config(self.config)['chat_id'], 2)
        self.assertEqual((directory/'telegram_token').read_text(), '200:NEW\n')
        from douyin_monitor.storage import Store
        store = Store(self.state/'monitor.sqlite3')
        try:
            store.bind(load_config(self.config))
            store.bind_notification_bot('200')
            row = store.db.execute('SELECT payload, bot_id FROM outbox WHERE id=?', (identifier,)).fetchone()
            self.assertEqual(json.loads(row['payload'])['chat_id'], 1)
            self.assertEqual(row['bot_id'], '100')
        finally: store.close()
        self.assertEqual({p.name for p in Path(result['backup']).iterdir()}, {'config.json', 'monitor.sqlite3', 'manifest.json'})
        self.assertEqual((Path(result['recovery_directory'])/'telegram_token').read_text(), '100:OLD')

    def test_real_retry_preserves_order_and_destination(self):
        identifier = self.fixture()
        result = self.subprocess(*self.flags, 'outbox', 'retry', str(identifier))
        self.assertEqual(result.returncode, 0, result.stderr)
        from douyin_monitor.storage import Store
        store = Store(self.state/'monitor.sqlite3')
        try:
            row = store.pending(9999999999)[0]
            self.assertEqual(row['id'], identifier)
            self.assertEqual(row['bot_id'], '100')
            self.assertEqual(json.loads(row['payload'])['chat_id'], CFG['chat_id'])
        finally: store.close()


class OutboxTests(unittest.TestCase):
    def test_explicit_blocked_only_preserves_routes_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp)/'fixture.db')
            db.executescript('''CREATE TABLE outbox(id INTEGER PRIMARY KEY, status TEXT,
                next_attempt REAL, last_error TEXT, payload TEXT, attempts INTEGER, event_id INTEGER);
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); PRAGMA user_version=2;
                INSERT INTO outbox VALUES(1,'blocked',99,'forbidden','immutable-route',3,5);
                INSERT INTO outbox VALUES(2,'pending',99,NULL,'other-route',0,6);''')
            result = change_outbox(db, 1, 'retry')
            self.assertEqual(result['status'], 'pending')
            self.assertEqual(db.execute('SELECT payload,attempts FROM outbox WHERE id=1').fetchone(), ('immutable-route', 3))
            with self.assertRaises(ValueError): change_outbox(db, 2, 'discard', 'skip')
            db.execute("UPDATE outbox SET status='blocked' WHERE id=1"); db.commit()
            with self.assertRaises(ValueError): change_outbox(db, 1, 'discard', '')
            result = change_outbox(db, 1, 'discard', 'operator approved')
            self.assertEqual(result['status'], 'discarded')
            audit = db.execute("SELECT value FROM meta WHERE key LIKE 'outbox_action:%' ORDER BY key").fetchall()
            self.assertEqual(len(audit), 2)
            self.assertIn('operator approved', audit[-1][0])
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], 2)
            db.close()
