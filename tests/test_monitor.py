import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace
from douyin_monitor import domain, messages, transport, storage
m = SimpleNamespace(**{name: getattr(module, name) for module in (domain, messages, transport, storage) for name in dir(module) if not name.startswith('_')})


NOW = 1791735000
TARGET = 'MS4wLjABAAAA_TEST_TARGET'

def body(active=NOW-20, server=NOW):
    return {'data':[{'sec_user_id':TARGET,'last_active_time':active}], 'extra':{'now':server*1000}, 'not_friend_data':[{'sec_user_id':TARGET,'last_active_time':0}]}

def obs(active=NOW-20, server=NOW):
    return m.parse_presence(body(active,server), TARGET, server)

class ParserTests(unittest.TestCase):
    def test_positive_and_exact_target(self):
        o=obs(); self.assertEqual(o.active_at,NOW-20)
    def test_optional_success_code(self):
        b=body(); b['status_code']=0
        self.assertEqual(m.parse_presence(b,TARGET,NOW).server_at,NOW)
    def test_zero_is_unknown(self):
        with self.assertRaises(m.Problem) as c: obs(0)
        self.assertEqual(c.exception.code,'status_unavailable')
    def test_missing_target_is_unknown(self):
        with self.assertRaises(m.Problem) as c: m.parse_presence(body(),'other',NOW)
        self.assertEqual(c.exception.code,'target_missing')
    def test_expired_login(self):
        with self.assertRaises(m.Problem) as c: m.parse_presence({'status_code':8},TARGET,NOW)
        self.assertEqual(c.exception.code,'login_expired')
    def test_future_timestamp(self):
        with self.assertRaises(m.Problem): obs(NOW+100)
    def test_stale_server_response(self):
        with self.assertRaises(m.Problem) as c: m.parse_presence(body(),TARGET,NOW+500)
        self.assertEqual(c.exception.code,'server_clock_invalid')
    def test_duplicates_rejected(self):
        b=body(); b['data']*=2
        with self.assertRaises(m.Problem): m.parse_presence(b,TARGET,NOW)
    def test_bool_and_nan_rejected(self):
        for value in [True,float('nan'),float('inf'),'wrong']:
            with self.subTest(value=value), self.assertRaises(m.Problem): obs(value)
    def test_missing_server_time(self):
        b=body(); del b['extra']
        with self.assertRaises(m.Problem): m.parse_presence(b,TARGET,NOW)

class StateTests(unittest.TestCase):
    def setUp(self): self.s=m.State()
    def feed(self,active,server): return m.advance(self.s,obs(active,server),None,server,600,300)
    def inject_error(self,code,now): return m.advance(self.s,None,m.Problem(code),now,600,300)
    def test_initial_baseline_no_online_alert(self):
        e=self.feed(NOW-20,NOW)
        self.assertEqual(self.s.mode,'recent'); self.assertEqual(e[0]['kind'],'baseline')
        self.assertFalse(e[0]['notify'])
    def test_old_initial_baseline(self):
        self.feed(NOW-900,NOW); self.assertEqual(self.s.mode,'inactive')
    def test_update_still_old_does_not_go_online(self):
        self.feed(NOW-1000,NOW); e=self.feed(NOW-900,NOW+30)
        self.assertEqual(self.s.mode,'inactive'); self.assertEqual(e,[])
    def test_online_transition_once(self):
        self.feed(NOW-900,NOW)
        self.assertEqual(self.feed(NOW+20,NOW+30)[0]['kind'],'online')
        self.assertEqual(self.feed(NOW+50,NOW+60),[])
    def test_threshold_exactly_600_is_recent(self):
        self.feed(NOW,NOW)
        for elapsed in range(30,600,30): self.assertEqual(self.feed(NOW,NOW+elapsed),[])
        self.assertEqual(self.feed(NOW,NOW+600),[])
        self.assertEqual(self.s.mode,'recent')
        self.assertEqual(self.feed(NOW,NOW+601)[0]['kind'],'offline')
    def test_timeout_never_causes_offline(self):
        self.feed(NOW,NOW)
        self.assertEqual(self.inject_error('network_timeout',NOW+1000)[0]['kind'],'unknown')
        self.assertEqual(self.s.mode,'unknown')
    def test_same_unknown_not_spammed(self):
        self.inject_error('network_timeout',NOW)
        self.assertEqual(self.inject_error('network_timeout',NOW+3600),[])
    def test_changed_unknown_rate_limited(self):
        self.inject_error('dns_failure',NOW)
        silent=self.inject_error('network_error',NOW+30)
        self.assertFalse(any(e['notify'] for e in silent))
        self.assertTrue(self.inject_error('network_error',NOW+301)[0]['notify'])
    def test_recovered_not_backfilled_online(self):
        self.feed(NOW-900,NOW); self.inject_error('network_timeout',NOW+30)
        self.assertEqual(self.feed(NOW+40,NOW+60)[0]['kind'],'recovered')
    def test_out_of_order_active_time(self):
        self.feed(NOW,NOW)
        self.assertEqual(self.feed(NOW-20,NOW+30)[0]['reason'],'timestamp_regression')
        self.assertEqual(self.s.active_at,NOW)
    def test_out_of_order_server_time(self):
        self.feed(NOW,NOW)
        self.assertEqual(self.feed(NOW,NOW-1)[0]['reason'],'response_out_of_order')
    def test_sampling_gap_becomes_recovery_not_offline_event(self):
        self.feed(NOW,NOW)
        self.assertEqual(self.feed(NOW,NOW+700)[0]['kind'],'recovered')
        self.assertEqual(self.s.mode,'inactive')
    def test_restart_resets_confidence(self):
        self.feed(NOW,NOW); self.s.reset_confidence()
        self.assertEqual(self.feed(NOW,NOW+700)[0]['kind'],'baseline')
    def test_errors_do_not_reset_last_success(self):
        self.feed(NOW,NOW); self.inject_error('network_timeout',NOW+30)
        self.assertEqual(self.s.last_success_at,NOW)

class StoreTests(unittest.TestCase):
    def test_retry_keeps_unknown_before_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            db=m.Store(Path(d)/'test.sqlite3'); db.bind_notification_bot('123'); s=m.State()
            cfg={'label':'fixture','target':TARGET,'chat_id':123,'thread_id':456}
            e=m.advance(s,None,m.Problem('network_timeout'),NOW,timeout_alert_after=1)
            db.save(s,{'observed_at':NOW},e,cfg)
            first=db.pending(NOW)[0]
            db.mark_failed(first['id'],'network_timeout',NOW,120)
            e=m.advance(s,obs(NOW+20,NOW+30),None,NOW+30)
            db.save(s,{'observed_at':NOW+30},e,cfg)
            self.assertEqual(db.pending(NOW+30),[])
            self.assertEqual(db.pending(NOW+120)[0]['id'],first['id'])
            db.close()
    def test_database_binding_allows_target_changes_but_rejects_identity_or_destination_change(self):
        with tempfile.TemporaryDirectory() as d:
            db=m.Store(Path(d)/'test.sqlite3'); db.bind_notification_bot('123')
            cfg={'targets':[{'target':TARGET,'label':'fixture','enabled':True}],'observer_uid':'123','chat_id':1,'thread_id':2}
            db.bind(cfg); db.bind(cfg)
            db.bind({**cfg,'targets':[{'target':'OTHER_SYNTHETIC','label':'other','enabled':True}]})
            for change in [{'observer_uid':'456'},{'thread_id':3}]:
                with self.assertRaises(ValueError): db.bind({**cfg,**change})
            db.close()
    def test_atomic_events_and_bound_destination(self):
        with tempfile.TemporaryDirectory() as d:
            db=m.Store(Path(d)/'test.sqlite3'); db.bind_notification_bot('123')
            s=m.State(); event=m.advance(s,None,m.Problem('network_timeout'),NOW,600,300,timeout_alert_after=1)[0]
            cfg={'label':'fixture','target':TARGET,'chat_id':123,'thread_id':456}
            db.save(s,{'observed_at':NOW,'mode':s.mode},[event],cfg,30)
            queued=db.pending(NOW)
            self.assertEqual(len(queued),1)
            payload=json.loads(queued[0]['payload'])
            self.assertEqual(payload['chat_id'],123); self.assertEqual(payload['message_thread_id'],456)
            self.assertIn('请求超时',payload['text']); self.assertNotIn('sessionid=',payload['text'])
            self.assertEqual(db.load(cfg['target']).mode,'unknown')
            db.mark_sent(queued[0]['id'],999,NOW)
            self.assertEqual(db.pending(NOW),[])
            db.close()
    def test_delivery_retry_preserves_event(self):
        with tempfile.TemporaryDirectory() as d:
            db=m.Store(Path(d)/'test.sqlite3'); db.bind_notification_bot('123')
            s=m.State(); e=m.advance(s,None,m.Problem('network_timeout'),NOW,600,300,timeout_alert_after=1)
            db.save(s,{'observed_at':NOW,'mode':s.mode},e,{'label':'fixture','target':TARGET,'chat_id':1,'thread_id':2},30)
            row=db.pending(NOW)[0]; db.mark_failed(row['id'],'telegram_timeout',NOW,60)
            self.assertEqual(db.pending(NOW+30),[])
            self.assertEqual(db.pending(NOW+60)[0]['attempts'],1)
            db.close()

class NetworkTests(unittest.TestCase):
    def test_transport_error_classification(self):
        import socket,ssl,urllib.error
        for err,code in [(socket.timeout(),'network_timeout'),(urllib.error.URLError(socket.gaierror()),'dns_failure'),(urllib.error.URLError(ssl.SSLError()),'tls_error')]:
            with self.subTest(code=code): self.assertEqual(m.classify_transport(err).code,code)
    def test_backoff(self):
        self.assertEqual(m.retry_delay('network_timeout',1,30),30)
        self.assertEqual(m.retry_delay('network_timeout',9,30),300)
        self.assertEqual(m.retry_delay('login_expired',1,30),600)
        self.assertGreaterEqual(m.retry_delay('rate_limited',1,30,180),180)
    def test_message_flags_explicit(self):
        p=m.telegram_payload({'chat_id':1,'thread_id':2},'test')
        self.assertFalse(p['disable_notification']); self.assertTrue(p['protect_content'])
        self.assertTrue(p['link_preview_options']['is_disabled'])
    def test_verifies_observer_uid(self):
        client=m.DouyinClient('TEST_ONLY')
        with patch.object(client,'request',return_value={'status_code':0,'user':{'uid':'different'}}):
            with self.assertRaises(m.Problem) as c: client.verify_identity('expected')
            self.assertEqual(c.exception.code,'account_mismatch')

if __name__=='__main__': unittest.main(verbosity=2)
