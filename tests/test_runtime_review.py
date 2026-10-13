"""Review regressions: real SQLite, real management and local client boundaries."""
from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core_helpers import canonical
from douyin_monitor import runtime
from douyin_monitor.config import atomic_write_config, load_config
from douyin_monitor.domain import Observation, Problem, State
from douyin_monitor.locking import InstanceLock
from douyin_monitor.management import change_outbox, mutate_config
from douyin_monitor.storage import Store, list_events, status_snapshot

A = 'MS4wLjABAAAA_FIXTURE'
B = 'MS4wLjABAAAA_SECOND'


class LocalCollector:
    def __init__(self, identity=None, targets=None):
        self.identity = identity
        self.targets = targets or {}
        self.seen = []

    def verify_identity(self, uid):
        if self.identity:
            raise Problem(self.identity)

    def presence(self, target):
        self.seen.append(target)
        if self.targets.get(target):
            raise Problem(self.targets[target])
        now = time.time()
        return Observation(now, now)


class RuntimeReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.path = self.state / 'monitor.sqlite3'
        self.cfg = canonical({'targets': [
            {'target': A, 'label': 'a', 'enabled': True},
            {'target': B, 'label': 'b', 'enabled': True}]})
        for name, value in [('douyin_session', '0' * 32), ('telegram_token', '123:SYNTHETIC')]:
            (self.root / name).write_text(value)
            (self.root / name).chmod(0o600)

    def run_once(self, client, **kwargs):
        with patch.object(runtime, 'DouyinClient', return_value=client), patch.object(runtime, 'REQUEST_SPACING', 0), redirect_stdout(io.StringIO()):
            return runtime.run(self.cfg, self.root, self.state, self.root / 'logs', once=True, no_notify=True, **kwargs)

    def store(self):
        db = Store(self.path)
        self.addCleanup(db.close)
        return db

    def notices(self):
        return [e for e in reversed(list_events(self.path)) if e['payload'].get('notify')]

    def test_config_pause_and_remove_between_initial_load_and_lock(self):
        for action in ('pause', 'remove'):
            with self.subTest(action=action):
                config = self.root / 'config.json'
                atomic_write_config(config, self.cfg)
                stale = load_config(config)
                reached, resume = threading.Event(), threading.Event()
                errors = []
                client = LocalCollector()

                @contextmanager
                def barrier_lock(path):
                    reached.set()
                    if not resume.wait(3):
                        raise AssertionError('management barrier not released')
                    with InstanceLock(path):
                        yield

                def run():
                    try:
                        with redirect_stdout(io.StringIO()):
                            runtime.run(stale, self.root, self.state, self.root / 'logs', once=True,
                                        no_notify=True, config_path=config)
                    except BaseException as error:
                        errors.append(error)
                        reached.set()

                with patch.object(runtime, 'InstanceLock', barrier_lock), patch.object(runtime, 'DouyinClient', return_value=client), patch.object(runtime, 'REQUEST_SPACING', 0):
                    worker = threading.Thread(target=run)
                    worker.start()
                    try:
                        self.assertTrue(reached.wait(3))
                        def edit(cfg):
                            if action == 'remove':
                                cfg['targets'] = [cfg['targets'][1]]
                            else:
                                cfg['targets'][0]['enabled'] = False
                            return cfg
                        mutate_config(config, self.state, edit)
                    finally:
                        resume.set()
                        worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(client.seen, [B])
                self.assertNotIn(A, status_snapshot(self.path)['targets'])

    def test_third_timeout_presence_to_identity_hands_over_to_one_account(self):
        for _ in range(2):
            self.run_once(LocalCollector(targets={A: 'network_timeout', B: 'network_timeout'}))
        self.assertEqual(self.notices(), [])
        self.run_once(LocalCollector(identity='network_timeout'))
        notices = self.notices()
        self.assertEqual([(e['scope'], e['kind']) for e in notices], [('account', 'unknown')])
        self.assertEqual(notices[0]['payload']['consecutive_timeouts'], 3)
        self.run_once(LocalCollector(identity='network_timeout'))
        self.assertEqual(len(self.notices()), 1)
        self.run_once(LocalCollector())
        self.assertEqual([(e['scope'], e['kind']) for e in self.notices()],
                         [('account', 'unknown'), ('account', 'recovered')])

    def test_shared_timeout_recovery_releases_unnotified_target(self):
        for _ in range(3):
            self.run_once(LocalCollector(identity='network_timeout'))
        self.assertEqual(len(self.notices()), 1)
        self.run_once(LocalCollector(targets={B: 'network_timeout'}))
        notices = self.notices()
        self.assertEqual([(e['scope'], e['kind'], e['target']) for e in notices],
                         [('account', 'unknown', None), ('account', 'recovered', None), ('target', 'unknown', B)])
        self.run_once(LocalCollector(targets={B: 'network_timeout'}))
        self.assertEqual(len(self.notices()), 3)
        self.run_once(LocalCollector())
        self.assertEqual([(e['scope'], e['kind'], e['target']) for e in self.notices()][-1], ('target', 'recovered', B))
        self.assertFalse(any(e['kind'] == 'recovered' and e['target'] == A for e in self.notices()))

    def test_reverse_interface_third_timeout_still_alerts_per_target(self):
        for _ in range(2):
            self.run_once(LocalCollector(identity='network_timeout'))
        self.run_once(LocalCollector(targets={A: 'network_timeout', B: 'network_timeout'}))
        self.assertEqual([(e['scope'], e['target']) for e in self.notices()], [('target', A), ('target', B)])
        self.run_once(LocalCollector())
        self.assertEqual([(e['scope'], e['target'], e['kind']) for e in self.notices()][-2:],
                         [('target', A, 'recovered'), ('target', B, 'recovered')])
        self.assertFalse(any(e['scope'] == 'account' for e in self.notices()))

    def test_alternating_interfaces_do_not_reset_consecutive_timeout(self):
        self.run_once(LocalCollector(identity='network_timeout'))
        self.run_once(LocalCollector(targets={A: 'network_timeout', B: 'network_timeout'}))
        self.assertEqual(self.notices(), [])
        self.run_once(LocalCollector(identity='network_timeout'))
        self.assertEqual([(e['scope'], e['kind']) for e in self.notices()], [('account', 'unknown')])

    def test_login_health_recovers_on_identity_even_when_all_targets_missing(self):
        self.run_once(LocalCollector(identity='login_expired'))
        self.run_once(LocalCollector(targets={A: 'target_missing', B: 'target_missing'}))
        self.run_once(LocalCollector(targets={A: 'target_missing', B: 'target_missing'}))
        notices = self.notices()
        self.assertEqual([(e['scope'], e['kind']) for e in notices if e['scope'] == 'account'],
                         [('account', 'unknown'), ('account', 'recovered')])
        self.assertEqual({e['target'] for e in notices if e['scope'] == 'target'}, {A, B})
        db = self.store()
        self.assertNotEqual(db.load_account().mode, 'unknown')
        self.assertEqual(db.load(A).reason, 'target_missing')
        self.assertFalse(any(e['scope'] == 'target' and e['kind'] == 'recovered' for e in notices))

    def test_legacy_copied_account_marker_does_not_emit_target_recovery(self):
        db = self.store()
        db.bind(self.cfg)
        now = time.time()
        state = State(mode='unknown', reason='login_expired', notified_reason='login_expired', unknown_alerted=True)
        db.save_account(state, [{'kind': 'unknown', 'at': now, 'reason': 'login_expired', 'notify': True}], self.cfg, now)
        # Legacy migration copies its single login episode into both account and target.
        db.save(state, {'observed_at': now}, [], {**self.cfg, **self.cfg['targets'][0]})
        self.run_once(LocalCollector())
        self.assertEqual([(e['scope'], e['kind']) for e in self.notices()],
                         [('account', 'unknown'), ('account', 'recovered')])

    def test_non_timeout_target_result_breaks_consecutive_timeout_rule(self):
        for _ in range(2):
            self.run_once(LocalCollector(identity='network_timeout'))
        self.run_once(LocalCollector(targets={A: 'target_missing', B: 'target_missing'}))
        self.run_once(LocalCollector(identity='network_timeout'))
        self.assertFalse(any(e['payload']['reason'] == 'network_timeout' for e in self.notices()))
        db = self.store()
        self.assertEqual(db.load(A).timeout_streak, 1)
        self.assertEqual(db.load_account().timeout_streak, 1)

    def test_silent_identity_timeout_has_no_false_recovery(self):
        self.run_once(LocalCollector(identity='network_timeout'))
        self.run_once(LocalCollector())
        self.assertEqual(self.notices(), [])

    def queue(self, db):
        now = time.time()
        db.save(State(mode='unknown', reason='dns_failure'), {'observed_at': now},
                [{'kind': 'unknown', 'at': now, 'reason': 'dns_failure', 'notify': True}],
                {**self.cfg, **self.cfg['targets'][0]})

    def sender_pass(self, client):
        # A bounded local sender turn: SQLite work finishes before the first idle wait.
        class Stop:
            stopped = False
            def is_set(self):
                return self.stopped
            def set(self):
                self.stopped = True
            def wait(self, delay):
                self.stopped = True
                return True
        fatal = threading.Event()
        with patch.object(runtime, 'TelegramClient', return_value=client):
            runtime.sender_loop(self.path, self.cfg, self.root, Stop(), fatal)
        self.assertFalse(fatal.is_set())

    def test_getme_permanent_auth_blocks_existing_and_future_queue_and_retries_original_route(self):
        for code in (401, 403):
            with self.subTest(code=code):
                db = self.store()
                db.bind(self.cfg)
                db.bind_notification_bot('123')
                self.queue(db)
                class Telegram:
                    calls = 0
                    sent = []
                    broken = True
                    def get_me(self):
                        self.calls += 1
                        if self.broken:
                            raise Problem('http_error', f'telegram_code={code}', permanent=True)
                        return {'id': 123, 'is_bot': True}
                    def send(self, payload):
                        self.sent.append(payload)
                        return 42
                client = Telegram()
                self.sender_pass(client)
                self.assertEqual(db.status()['outbox'].get('blocked'), 1)
                self.assertEqual(db.status()['sender']['status'], 'blocked')
                original = dict(db.db.execute('SELECT * FROM outbox ORDER BY id DESC LIMIT 1').fetchone())
                self.assertEqual(original['last_error'], 'sender_auth_permanent')
                self.run_once(LocalCollector(identity='login_expired'))
                self.assertEqual(db.status()['samples'], 3)
                self.assertEqual(db.status()['outbox'].get('blocked'), 2)
                self.sender_pass(client)  # restarted worker must not retry permanent getMe
                self.assertEqual(client.calls, 1)
                with InstanceLock(self.state / 'monitor.lock'):
                    change_outbox(db.db, original['id'], 'retry')
                client.broken = False
                self.sender_pass(client)
                row = dict(db.db.execute('SELECT * FROM outbox WHERE id=?', (original['id'],)).fetchone())
                self.assertEqual(row['status'], 'sent')
                self.assertEqual((row['bot_id'], row['payload']), (original['bot_id'], original['payload']))
                self.assertEqual(client.sent[-1]['chat_id'], self.cfg['chat_id'])
                # Explicit discard is the only way to release remaining blocked history for switch.
                with InstanceLock(self.state / 'monitor.lock'):
                    with self.assertRaises(ValueError):
                        db.rebind_notifications({**self.cfg, 'chat_id': 2}, '456')
                    for row in db.db.execute("SELECT id FROM outbox WHERE status='blocked'").fetchall():
                        change_outbox(db.db, row['id'], 'discard', 'fixture operator resolution')
                    db.rebind_notifications({**self.cfg, 'chat_id': 2}, '456')
                self.assertNotEqual(db.status()['sender']['status'], 'blocked')
                db.close()
                # Use a clean isolated database for the other error code.
                for path in self.state.glob('monitor.sqlite3*'):
                    path.unlink()

    def test_unverified_bot_failure_is_manageable_without_blind_binding(self):
        db = self.store()
        db.bind(self.cfg)
        self.queue(db)
        class Telegram:
            def get_me(self):
                raise Problem('login_expired', 'http=401', permanent=True)
        self.sender_pass(Telegram())
        row = db.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(row['status'], 'blocked')
        self.assertIsNone(row['bot_id'])
        self.assertIsNone(db.db.execute("SELECT value FROM meta WHERE key='notification_bot'").fetchone())
        with self.assertRaises(ValueError):
            db.bind_notification_bot('456')
        class Repaired:
            def get_me(self):
                return {'id': 123, 'is_bot': True}
            def send(self, payload):
                return 42
        with InstanceLock(self.state / 'monitor.lock'):
            change_outbox(db.db, row['id'], 'retry')
        self.sender_pass(Repaired())
        row = db.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['status'], row['bot_id']), ('sent', '123'))

    def test_unverified_auth_discard_switch_never_relabels_original_history(self):
        db = self.store()
        db.bind(self.cfg)
        class Broken:
            def get_me(self):
                raise Problem('login_expired', 'http=401', permanent=True)
        self.sender_pass(Broken())  # getMe can fail before any collector event exists
        self.queue(db)
        old = dict(db.db.execute('SELECT * FROM outbox').fetchone())
        self.assertEqual(old['status'], 'blocked')
        self.assertIsNone(old['bot_id'])
        with InstanceLock(self.state / 'monitor.lock'):
            change_outbox(db.db, old['id'], 'discard', 'unverified sender abandoned')
            cfg = {**self.cfg, 'chat_id': 2}
            db.rebind_notifications(cfg, '456')
        db.bind_notification_bot('456')
        historical = dict(db.db.execute('SELECT * FROM outbox WHERE id=?', (old['id'],)).fetchone())
        self.assertEqual(historical['status'], 'discarded')
        self.assertEqual((historical['payload'], historical['bot_id']), (old['payload'], old['bot_id']))
        self.cfg = cfg
        self.queue(db)
        new = dict(db.db.execute('SELECT * FROM outbox ORDER BY id DESC LIMIT 1').fetchone())
        self.assertEqual((new['status'], new['bot_id']), ('pending', '456'))
        self.assertEqual(json.loads(new['payload'])['chat_id'], 2)

    def test_permanent_getme_failure_does_not_stop_concurrent_collector(self):
        db = self.store()
        db.bind(self.cfg)
        db.bind_notification_bot('123')
        self.queue(db)
        path = self.path
        class Collector(LocalCollector):
            def presence(self, target):
                deadline = time.monotonic() + 3
                while status_snapshot(path)['sender']['status'] != 'blocked':
                    if time.monotonic() >= deadline:
                        raise AssertionError('sender failed to persist permanent auth state')
                    time.sleep(.01)
                return super().presence(target)
        class Telegram:
            def get_me(self):
                raise Problem('access_denied', 'http=403', permanent=True)
        with patch.object(runtime, 'DouyinClient', return_value=Collector()), patch.object(runtime, 'TelegramClient', return_value=Telegram()), patch.object(runtime, 'REQUEST_SPACING', 0), redirect_stdout(io.StringIO()):
            result = runtime.run(self.cfg, self.root, self.state, self.root / 'logs', once=True)
        self.assertEqual(result, 0)
        self.assertEqual(db.status()['samples'], 3)
        self.assertEqual(db.status()['outbox'], {'blocked': 3})
        self.assertEqual(db.load(A).mode, 'recent')
        from douyin_monitor.observability import read_logs
        errors = read_logs(self.root / 'logs', stream='error')
        self.assertTrue(any(e['message'] == 'notification_sender_blocked' and e['level'] == 'ERROR' for e in errors))


if __name__ == '__main__':
    unittest.main()
