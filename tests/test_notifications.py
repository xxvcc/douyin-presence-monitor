"""Port of baseline test_notifications.py, plus crash/queue safeguards.

The SQL fixture exercises REAL management transactions before sibling core
modules are merged. Core behavioral ports run against production core APIs
when available; no fallback module is installed into production.
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from douyin_monitor.config import validate_config, target_config, load_config
from douyin_monitor.credentials import atomic_private_write
from douyin_monitor import management as sw

CFG = validate_config({'target': 'MS4wLjABAAAA_FIXTURE', 'label': 'fixture',
                       'observer_uid': '123', 'chat_id': 1, 'thread_id': 2})


class SQLFixtureStore:
    """Test-only implementation of just the rebind contract using real SQLite."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE outbox(id INTEGER PRIMARY KEY, status TEXT, payload TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, payload TEXT);
            PRAGMA user_version=2;''')
        with self.db:
            self.db.execute('INSERT INTO meta VALUES(?,?)', ('binding', json.dumps(CFG)))
            self.db.execute('INSERT INTO meta VALUES(?,?)', ('notification_bot', '100'))
            self.db.execute('INSERT INTO events VALUES(7,?)', ('old-event',))
            self.db.execute('INSERT INTO outbox VALUES(9,?,?)', ('sent', 'immutable-old-route'))

    def rebind_notifications(self, cfg, bot_id):
        sw.require_empty_queue(self.db)
        self.db.execute("UPDATE meta SET value=? WHERE key='binding'", (json.dumps(cfg),))
        self.db.execute("UPDATE meta SET value=? WHERE key='notification_bot'", (str(bot_id),))

    def close(self): self.db.close()


class NotificationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'config.json'
        self.token = self.root/'telegram_token'
        atomic_private_write(self.config, json.dumps(CFG))
        atomic_private_write(self.token, '100:OLD')
        self.store = SQLFixtureStore(self.root/'monitor.sqlite3')
        self.addCleanup(self.store.close)
        self.new = {**CFG, 'thread_id': None}

    def switch(self):
        return sw.apply_switch(self.config, self.token, self.store, self.new, '200:NEW', '200', state_dir=self.root)

    def test_switch_commit_keeps_private_permissions_and_history(self):
        result = self.switch()
        self.assertEqual(self.token.read_text(), '200:NEW\n')
        self.assertIsNone(load_config(self.config)['thread_id'])
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.db.execute("SELECT value FROM meta WHERE key='notification_bot'").fetchone()[0], '200')
        self.assertEqual(self.store.db.execute('SELECT * FROM events').fetchall(), [(7, 'old-event')])
        self.assertEqual(self.store.db.execute('SELECT * FROM outbox').fetchall(), [(9, 'sent', 'immutable-old-route')])
        recovery = Path(result['recovery_directory'])
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
        self.assertEqual((recovery/'telegram_token').read_text(), '100:OLD')
        self.assertFalse((self.root/sw.RECOVERY_MARKER).exists())

    def test_switch_write_failure_rolls_back_files_and_sql(self):
        original = self.config.read_text()
        real = sw.atomic_private_write
        failed = False
        def flaky(path, value):
            nonlocal failed
            if Path(path) == self.token and not failed:
                failed = True
                raise OSError('fixture disk failure')
            return real(path, value)
        with patch.object(sw, 'atomic_private_write', side_effect=flaky), self.assertRaises(OSError):
            self.switch()
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(self.token.read_text(), '100:OLD')
        self.assertEqual(self.store.db.execute("SELECT value FROM meta WHERE key='notification_bot'").fetchone()[0], '100')
        self.assertFalse((self.root/sw.RECOVERY_MARKER).exists())

    def test_interrupted_rollback_leaves_marker_and_explicit_recovery(self):
        original = self.config.read_text()
        real = sw.atomic_private_write
        def fail_token(path, value):
            if Path(path) == self.token: raise OSError('fixture disk failure')
            return real(path, value)
        with patch.object(sw, 'atomic_private_write', side_effect=fail_token), self.assertRaises(OSError):
            self.switch()
        self.assertTrue((self.root/sw.RECOVERY_MARKER).is_file())
        with self.assertRaises(ValueError): sw.assert_no_recovery(self.root)
        result = sw.recover_switch(self.config, self.token, self.store.db, state_dir=self.root)
        self.assertTrue(result['recovered'])
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(self.token.read_text(), '100:OLD')
        self.assertFalse((self.root/sw.RECOVERY_MARKER).exists())

    def test_real_process_death_after_sql_commit_and_first_file_recovers(self):
        original = self.config.read_bytes()
        self.store.close()
        pid = os.fork()
        if pid == 0:
            self.store.db = sqlite3.connect(self.root/'monitor.sqlite3')
            real_write = sw.atomic_private_write
            rebind = self.store.rebind_notifications
            def commit_rebind(cfg, bot_id):
                rebind(cfg, bot_id)
                self.store.db.commit()
            self.store.rebind_notifications = commit_rebind
            def crash_after_config(path, value):
                real_write(path, value)
                if Path(path) == self.config:
                    os._exit(73)  # no finally/except/rollback handlers run
            sw.atomic_private_write = crash_after_config
            try: self.switch()
            except BaseException: os._exit(74)
            os._exit(75)
        _, status = os.waitpid(pid, 0)
        self.store.db = sqlite3.connect(self.root/'monitor.sqlite3')
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        self.assertTrue((self.root/sw.RECOVERY_MARKER).exists())
        self.assertIsNone(load_config(self.config)['thread_id'])
        self.assertEqual(self.token.read_text(), '100:OLD')
        self.assertEqual(self.store.db.execute("SELECT value FROM meta WHERE key='notification_bot'").fetchone()[0], '200')
        sw.recover_switch(self.config, self.token, self.store.db, state_dir=self.root)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.token.read_text(), '100:OLD')
        self.assertEqual(self.store.db.execute("SELECT value FROM meta WHERE key='notification_bot'").fetchone()[0], '100')
        self.assertFalse((self.root/sw.RECOVERY_MARKER).exists())

    def test_pending_and_blocked_both_prevent_switch_before_writes(self):
        for status in ('pending', 'blocked'):
            with self.store.db:
                self.store.db.execute('UPDATE outbox SET status=?', (status,))
            with self.assertRaises(ValueError): self.switch()
            self.assertEqual(self.token.read_text(), '100:OLD')
            self.assertFalse((self.root/sw.RECOVERY_MARKER).exists())

    def test_revisions_and_target_changes_refused(self):
        with self.assertRaises(ValueError):
            sw.apply_switch(self.config, self.token, self.store, self.new, '200:NEW', '200', state_dir=self.root,
                            expected_config='incorrect')
        changed = {**self.new, 'targets': [{**CFG['targets'][0], 'label': 'changed'}]}
        with self.assertRaises(ValueError):
            sw.apply_switch(self.config, self.token, self.store, changed, '200:NEW', '200', state_dir=self.root)

    def test_recovery_tamper_refused(self):
        real = sw.atomic_private_write
        def fail_token(path, value):
            if Path(path) == self.token: raise OSError('fixture failure')
            return real(path, value)
        with patch.object(sw, 'atomic_private_write', side_effect=fail_token), self.assertRaises(OSError): self.switch()
        marker = json.loads((self.root/sw.RECOVERY_MARKER).read_text())
        recovery = self.root/marker['recovery_dir']
        (recovery/'telegram_token').write_text('999:TAMPERED')
        with self.assertRaises(ValueError):
            sw.recover_switch(self.config, self.token, self.store.db, state_dir=self.root)
        self.assertTrue((self.root/sw.RECOVERY_MARKER).exists())
        self.assertEqual(self.token.read_text(), '100:OLD')


CORE_AVAILABLE = all(importlib.util.find_spec('douyin_monitor.'+name) is not None for name in ('storage', 'transport', 'messages', 'domain'))


@unittest.skipUnless(CORE_AVAILABLE, 'core sibling modules not merged; run again after integration')
class NotificationCorePortTests(unittest.TestCase):
    def test_real_store_switch_and_file_failure_rollback(self):
        from douyin_monitor.storage import Store
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                config_path, token_path = directory/'config.json', directory/'telegram_token'
                original = json.dumps(CFG)
                atomic_private_write(config_path, original)
                atomic_private_write(token_path, '100:OLD')
                store = Store(directory/'monitor.sqlite3')
                try:
                    store.bind(CFG)
                    store.bind_notification_bot('100')
                    real = sw.atomic_private_write
                    failed = False
                    def flaky(path, value):
                        nonlocal failed
                        if fail and Path(path) == token_path and not failed:
                            failed = True
                            raise OSError('fixture disk failure')
                        return real(path, value)
                    new = {**CFG, 'thread_id': None}
                    with patch.object(sw, 'atomic_private_write', side_effect=flaky):
                        if fail:
                            with self.assertRaises(OSError):
                                sw.apply_switch(config_path, token_path, store, new, '200:NEW', '200', state_dir=tmp)
                        else:
                            sw.apply_switch(config_path, token_path, store, new, '200:NEW', '200', state_dir=tmp)
                    store.bind(CFG if fail else new)
                    store.bind_notification_bot('100' if fail else '200')
                    self.assertEqual(token_path.read_text(), '100:OLD' if fail else '200:NEW\n')
                    self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
                    if fail: self.assertEqual(config_path.read_text(), original)
                finally: store.close()

    def test_root_dm_omits_thread(self):
        from douyin_monitor.messages import telegram_payload
        self.assertNotIn('message_thread_id', telegram_payload({**CFG, 'thread_id': None}, 'test'))

    def test_config_allows_null_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            path.write_text(json.dumps({**CFG, 'thread_id': None}))
            self.assertIsNone(load_config(path)['thread_id'])

    def test_root_dm_receipt_validation(self):
        from douyin_monitor import transport
        from douyin_monitor.messages import telegram_payload
        with patch.object(transport, 'json_request', return_value={'ok': True, 'result': {'message_id': 5, 'chat': {'id': 1}, 'text': 'test'}}):
            self.assertEqual(transport.TelegramClient('TEST', None).send(telegram_payload({**CFG, 'thread_id': None}, 'test')), 5)

    def test_bot_binding_refuses_token_only_swap(self):
        from douyin_monitor.storage import Store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'test.db')
            try:
                store.bind(CFG)
                store.bind_notification_bot('100')
                store.bind_notification_bot('100')
                with self.assertRaises(ValueError): store.bind_notification_bot('200')
            finally: store.close()

    def test_explicit_rebind_preserves_target_and_existing_events(self):
        from douyin_monitor.storage import Store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'test.db')
            try:
                store.bind(CFG)
                store.bind_notification_bot('100')
                with store.db: store.rebind_notifications({**CFG, 'thread_id': None}, '200')
                store.bind({**CFG, 'thread_id': None})
                store.bind_notification_bot('200')
                changed = {**CFG, 'thread_id': None, 'targets': [{**CFG['targets'][0], 'target': 'MS4wLjABAAAA_OTHER'}]}
                # v2 targets are mutable config, not immutable Store binding;
                # the notification MANAGEMENT boundary retains this safeguard.
                config_path, token_path = Path(tmp)/'config.json', Path(tmp)/'telegram_token'
                atomic_private_write(config_path, json.dumps({**CFG, 'thread_id': None}))
                atomic_private_write(token_path, '200:OLD')
                with self.assertRaises(ValueError):
                    sw.apply_switch(config_path, token_path, store, changed, '300:NEW', '300', state_dir=tmp)
            finally: store.close()

    def test_rebind_refuses_pending_notifications(self):
        from douyin_monitor.storage import Store
        from douyin_monitor.domain import State, Problem, advance
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'test.db')
            try:
                store.bind(CFG)
                store.bind_notification_bot('100')
                state = State()
                events = advance(state, None, Problem('login_expired'), 1791735000)
                store.save(state, {'observed_at': 1791735000}, events, target_config(CFG, CFG['targets'][0]))
                with self.assertRaises(ValueError), store.db:
                    store.rebind_notifications({**CFG, 'thread_id': None}, '200')
                store.bind(CFG)
                store.bind_notification_bot('100')
                self.assertEqual(len(store.pending(1791735000)), 1)
            finally: store.close()
