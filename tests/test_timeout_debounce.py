"""Synthetic regression tests: never cause a live outage or send Telegram alerts."""
import dataclasses
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from douyin_monitor import domain, messages, transport, storage
m = SimpleNamespace(**{name: getattr(module, name) for module in (domain, messages, transport, storage) for name in dir(module) if not name.startswith('_')})


NOW=1791739000
CFG={'target':'MS4wLjABAAAA_FIXTURE','label':'fixture','observer_uid':'123','chat_id':1,'thread_id':None,'poll_seconds':60,'timeout_alert_after':3}

class TimeoutDebounceTests(unittest.TestCase):
    def setUp(self):
        self.s=m.State()
        m.advance(self.s,m.Observation(NOW,NOW),None,NOW)
    def error_sample(self,number,code='network_timeout'):
        return m.advance(self.s,None,m.Problem(code),NOW+number*60)
    def recover(self,number,active=None):
        now=NOW+number*60
        return m.advance(self.s,m.Observation(now if active is None else active,now),None,now)
    def test_first_two_timeouts_are_silent_but_unknown(self):
        for i in (1,2):
            events=self.error_sample(i)
            self.assertFalse(any(e['notify'] for e in events))
            self.assertEqual(self.s.mode,'unknown')
            self.assertEqual(self.s.timeout_streak,i)
            self.assertFalse(self.s.unknown_alerted)
            self.assertEqual(self.s.last_success_at,NOW)
    def test_third_timeout_alerts_once_and_later_timeouts_are_quiet(self):
        self.error_sample(1); self.error_sample(2)
        events=self.error_sample(3)
        self.assertEqual(len(events),1)
        self.assertTrue(events[0]['notify'])
        self.assertEqual(events[0]['consecutive_timeouts'],3)
        self.assertTrue(self.s.unknown_alerted)
        for i in (4,5,10): self.assertFalse(any(e['notify'] for e in self.error_sample(i)))
    def test_short_timeout_episode_recovers_silently(self):
        self.error_sample(1); self.error_sample(2)
        events=self.recover(3)
        self.assertFalse(any(e['notify'] for e in events))
        self.assertEqual(self.s.mode,'recent')
        self.assertEqual(self.s.timeout_streak,0)
        self.assertFalse(self.s.unknown_alerted)
        self.assertFalse(any(e['notify'] for e in self.error_sample(4)))
    def test_silent_episode_cannot_emit_offline_on_recovery(self):
        self.error_sample(1); self.error_sample(2)
        events=self.recover(20,active=NOW)
        self.assertEqual(self.s.mode,'inactive')
        self.assertFalse(any(e['notify'] for e in events))
        self.assertNotIn('offline',[e['kind'] for e in events])
    def test_alerted_episode_sends_one_recovery(self):
        for i in (1,2,3): self.error_sample(i)
        events=self.recover(4)
        self.assertEqual(events[0]['kind'],'recovered'); self.assertTrue(events[0]['notify'])
        self.assertEqual(self.recover(5),[])
    def test_severe_errors_notify_immediately_during_silent_timeouts(self):
        self.error_sample(1); self.error_sample(2)
        events=self.error_sample(3,'login_expired')
        self.assertTrue(events[0]['notify']); self.assertEqual(events[0]['reason'],'login_expired')
        self.assertEqual(self.s.timeout_streak,0)
    def test_severe_error_change_bypasses_unknown_cooldown(self):
        for i in (1,2,3): self.error_sample(i)
        events=self.error_sample(4,'account_mismatch')
        self.assertTrue(events[0]['notify']); self.assertEqual(events[0]['reason'],'account_mismatch')
    def test_non_timeout_breaks_timeout_streak(self):
        self.error_sample(1); self.error_sample(2)
        self.error_sample(3,'dns_failure')
        self.assertEqual(self.s.timeout_streak,0)
        self.error_sample(4)
        self.assertEqual(self.s.timeout_streak,1)
    def test_counter_and_alert_marker_survive_restart_during_unknown(self):
        self.error_sample(1); self.error_sample(2)
        self.s=m.State(**dataclasses.asdict(self.s)); self.s.reset_confidence()
        self.assertEqual(self.s.mode,'unknown')
        self.assertTrue(self.error_sample(3)[0]['notify'])
        self.s=m.State(**dataclasses.asdict(self.s)); self.s.reset_confidence()
        self.assertFalse(any(e['notify'] for e in self.error_sample(4)))
        self.assertTrue(self.recover(5)[0]['notify'])
    def test_initial_timeout_then_success_does_not_announce_recovery(self):
        self.s=m.State(); self.error_sample(1)
        self.assertFalse(any(e['notify'] for e in self.recover(2)))
    def test_sampling_gap_without_alert_does_not_send_recovery(self):
        events=self.recover(20)
        self.assertFalse(any(e['notify'] for e in events))
    def test_cooldown_applies_to_noncritical_changes(self):
        self.error_sample(1,'dns_failure')
        self.assertFalse(any(e['notify'] for e in self.error_sample(2,'network_error')))
        self.assertTrue(self.error_sample(7,'network_error')[0]['notify'])
    def test_timeout_alert_message_contains_count(self):
        for i in (1,2): self.error_sample(i)
        event=self.error_sample(3)[0]
        text=m.telegram_visible_text(m.render_event(event,self.s,CFG))
        self.assertIn('连续超时',text); self.assertIn('3 次',text)
    def test_schedule_and_request_timeout_are_distinct(self):
        delays=[m.retry_delay('network_timeout',i,60) for i in range(1,6)]
        self.assertEqual(delays,[60,60,120,240,300])
        self.assertEqual(inspect.signature(m.json_request).parameters['timeout'].default,12)
    def test_explicit_timeout_threshold_is_configurable(self):
        s=m.State()
        event=m.advance(s,None,m.Problem('network_timeout'),NOW,timeout_alert_after=1)
        self.assertTrue(event[0]['notify'])
    def test_legacy_unknown_state_migrates_notification_marker(self):
        with tempfile.TemporaryDirectory() as d:
            db=m.Store(Path(d)/'state.db'); db.bind_notification_bot('123')
            old={'mode':'unknown','reason':'network_timeout','notified_reason':'network_timeout','notified_at':NOW,'failures':1}
            with db.db: db.db.execute('insert into state(target,payload) values(?,?)',(CFG['target'],json.dumps(old)))
            s=db.load(CFG['target']); self.assertTrue(s.unknown_alerted)
            s.reset_confidence()
            events=m.advance(s,m.Observation(NOW+60,NOW+60),None,NOW+60)
            self.assertTrue(events[0]['notify']); db.close()
    def test_config_threshold_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json'
            try:
                from douyin_monitor.config import load_config
            except ModuleNotFoundError as error:
                if error.name != 'douyin_monitor.config': raise
                self.skipTest('configuration worker integration required')
            path.write_text(json.dumps(CFG)); self.assertEqual(load_config(path)['timeout_alert_after'],3)
            for value in (0,False,'3',11):
                path.write_text(json.dumps({**CFG,'timeout_alert_after':value}))
                with self.subTest(value=value),self.assertRaises(ValueError): load_config(path)

class CLIRegression(unittest.TestCase):
    def test_four_real_entrypoint_runs_queue_only_third_timeout_and_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'config.json').write_text(json.dumps(CFG)); (root/'douyin_session').write_text('0'*32)
            (root/'douyin_session').chmod(0o600)
            for attempt in range(1,5):
                behavior="raise m.Problem('network_timeout')" if attempt<4 else "return m.Observation(time.time(),time.time())"
                from core_helpers import run_local
                code="import time\nfrom douyin_monitor import runtime as m\nclass Stub:\n def verify_identity(self,x): pass\n def presence(self,x): "+behavior+"\nm.DouyinClient=lambda *a,**kw:Stub()"
                proc=run_local(root,CFG,code)
                self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                db=sqlite3.connect(root/'state/monitor.sqlite3')
                count=db.execute('select count(*) from outbox').fetchone()[0]
                self.assertEqual(count,0 if attempt<3 else attempt-2)
                state=json.loads(db.execute('select payload from state').fetchone()[0])
                self.assertEqual(state['timeout_streak'],attempt if attempt<4 else 0)
                if attempt<3: self.assertEqual(state['next_delay'],60)
                db.close()

if __name__=='__main__': unittest.main(verbosity=2)
