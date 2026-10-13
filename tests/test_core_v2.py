"""V2 contracts: synthetic SQLite/network boundaries; no social API calls."""
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from douyin_monitor.domain import State, Observation, Problem, advance
from douyin_monitor.storage import Store, status_snapshot, list_events
from douyin_monitor.locking import InstanceLock
from douyin_monitor import runtime, transport
from core_helpers import canonical, install_boundaries

NOW=1791739000
A='MS4wLjABAAAA_FIXTURE'
B='MS4wLjABAAAA_SECOND'


def legacy(path, cfg, *, bot='123', pending=True):
    db=sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
    CREATE TABLE observations(id INTEGER PRIMARY KEY,observed_at REAL,payload TEXT NOT NULL);
    CREATE TABLE events(id INTEGER PRIMARY KEY,created_at REAL,kind TEXT,payload TEXT NOT NULL);
    CREATE TABLE outbox(id INTEGER PRIMARY KEY,event_id INTEGER UNIQUE,payload TEXT NOT NULL,
    created_at REAL,next_attempt REAL,attempts INTEGER DEFAULT 0,status TEXT DEFAULT 'pending',
    message_id INTEGER,last_error TEXT,sent_at REAL);
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
    ''')
    old={k:str(cfg.get(k)) for k in ('observer_uid','chat_id','thread_id')}; old['target']=A
    state=State(mode='unknown',reason='network_timeout',timeout_streak=2,failures=2)
    with db:
        db.execute('INSERT INTO meta VALUES(?,?)',('binding',json.dumps(old,sort_keys=True)))
        if bot: db.execute('INSERT INTO meta VALUES(?,?)',('notification_bot',bot))
        db.execute('INSERT INTO state VALUES(1,?)',(json.dumps(dataclasses.asdict(state)),))
        db.execute('INSERT INTO observations VALUES(7,?,?)',(NOW,json.dumps({'observed_at':NOW})))
        db.execute('INSERT INTO events VALUES(9,?,?,?)',(NOW,'unknown',json.dumps({'target':A})))
        db.execute('INSERT INTO outbox VALUES(11,9,?,?,?,2,?,42,NULL,?)',(json.dumps({'chat_id':cfg['chat_id'],'text':'immutable legacy plain text'}),NOW,NOW,'pending' if pending else 'sent',None if pending else NOW))
    db.close()


class StorageV2(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'monitor.sqlite3'
        self.cfg=canonical({'targets':[{'target':A,'label':'a','enabled':True},{'target':B,'label':'b','enabled':True}]})
    def open(self):
        db=Store(self.path); self.addCleanup(db.close); db.bind(self.cfg); return db
    def save(self,db,target=A,now=NOW,events=None):
        s=State(mode='unknown',reason='dns_failure')
        db.save(s,{'observed_at':now},events if events is not None else [{'kind':'unknown','at':now,'reason':'dns_failure','notify':True}],{**self.cfg,'target':target,'label':target})
    def test_multitarget_history_survives_config_remove(self):
        db=self.open(); db.bind_notification_bot('123')
        self.save(db,A); self.save(db,B)
        db.bind({**self.cfg,'targets':[]})
        self.assertEqual(set(db.status()['targets']),{A,B})
        self.assertEqual(db.status()['samples'],2)
        self.assertEqual(len(list_events(self.path,target=B)),1)
        self.assertEqual(db.load(A).reason,'dns_failure')
    def test_queue_stops_at_first_future_or_blocked_row_not_just_first_row(self):
        db=self.open(); db.bind_notification_bot('123')
        for t in range(3): self.save(db,now=NOW+t)
        ids=[r['id'] for r in db.pending(NOW+3)]
        db.mark_failed(ids[1],'rate_limited',NOW,500)
        self.assertEqual([r['id'] for r in db.pending(NOW+3)],[ids[0]])
        db.mark_sent(ids[0],1,NOW)
        self.assertEqual(db.pending(NOW+3),[])
        db.mark_failed(ids[1],'access_denied',NOW,0,permanent=True)
        self.assertEqual(db.status()['outbox']['blocked'],1)
        self.assertEqual(db.pending(NOW+999),[])
        with self.assertRaises(ValueError): db.rebind_notifications(self.cfg,'456')
    def test_route_and_bot_are_snapshotted_and_immutable(self):
        db=self.open(); db.bind_notification_bot('123'); self.save(db)
        row=db.pending(NOW)[0]
        self.assertEqual(row['bot_id'],'123')
        self.assertEqual(json.loads(row['payload'])['chat_id'],1)
        with self.assertRaises(ValueError): db.bind_notification_bot('456')
        with self.assertRaises(ValueError): self.save(db,events=[{'kind':'bad','notify':True}])
        self.assertEqual(db.status()['samples'],1)
        db.mark_sent(row['id'],7,NOW)
        db.rebind_notifications({**self.cfg,'chat_id':2},'456')
        self.cfg['chat_id']=2; self.save(db,now=NOW+1)
        self.assertEqual(db.pending(NOW+1)[0]['bot_id'],'456')
    def test_save_cannot_bypass_immutable_route(self):
        db=self.open()
        with self.assertRaises(ValueError):
            db.save(State(),{'observed_at':NOW},[],{**self.cfg,'target':A,'label':'x','chat_id':999})
    def test_legacy_migration_preserves_ids_payload_counters_destination(self):
        legacy(self.path,self.cfg)
        original=sqlite3.connect(self.path)
        payload=original.execute('SELECT payload FROM outbox').fetchone()[0]; original.close()
        db=self.open()
        self.assertEqual(db.db.execute('PRAGMA user_version').fetchone()[0],2)
        self.assertEqual(db.load(A).timeout_streak,2)
        self.assertEqual(db.status()['samples'],1)
        row=db.pending(NOW)[0]
        self.assertEqual((row['id'],row['event_id'],row['payload'],row['attempts'],row['bot_id']),(11,9,payload,2,'123'))
    def test_legacy_sent_status_preserved_and_bad_binding_rolls_back(self):
        legacy(self.path,self.cfg,pending=False)
        db=Store(self.path); self.addCleanup(db.close)
        with self.assertRaises(ValueError): db.bind({**self.cfg,'observer_uid':'999'})
        self.assertEqual(db.db.execute('PRAGMA user_version').fetchone()[0],0)
        self.assertIn('id',[r[1] for r in db.db.execute('PRAGMA table_info(state)')])
        db.bind(self.cfg)
        self.assertEqual(db.db.execute('SELECT status,message_id FROM outbox').fetchone()[:],('sent',42))
    def test_unbound_legacy_queue_requires_explicit_verified_bot(self):
        legacy(self.path,self.cfg,bot=None)
        db=self.open()
        self.assertEqual(db.pending(NOW),[])
        db.bind_notification_bot('123')
        self.assertEqual(db.pending(NOW)[0]['bot_id'],'123')
    def test_unknown_database_is_not_mutated(self):
        db=sqlite3.connect(self.path)
        with db: db.execute('CREATE TABLE unrelated(secret TEXT)')
        db.close()
        before=self.path.read_bytes()
        with self.assertRaises(ValueError): Store(self.path)
        self.assertEqual(self.path.read_bytes(),before)
    def test_newer_schema_rejected_readonly(self):
        db=sqlite3.connect(self.path)
        with db: db.execute('PRAGMA user_version=99')
        db.close()
        before=self.path.read_bytes()
        with self.assertRaises(ValueError): Store(self.path)
        self.assertEqual(before,self.path.read_bytes())
        with self.assertRaises(ValueError): status_snapshot(self.path)
    def test_status_missing_does_not_create_anything(self):
        result=status_snapshot(self.path)
        self.assertFalse(result['initialized']); self.assertFalse(self.path.exists())
        self.assertEqual(list_events(self.path),[])
    def test_atomic_save_rollback_and_retention_protects_unresolved(self):
        db=self.open(); db.bind_notification_bot('123'); self.save(db)
        self.save(db,now=NOW+86400*40,events=[])
        self.assertEqual(db.status()['samples'],1)
        self.assertEqual(len(list_events(self.path)),1)
        self.assertEqual(len(db.pending(NOW+86400*40)),1)
    def test_sample_lifetime_count_persists_retention(self):
        db=self.open(); self.save(db,events=[]); self.save(db,now=NOW+86400*40,events=[])
        self.assertEqual(db.status()['total_samples'],2)
    def test_migration_error_rolls_back_all_schema_changes(self):
        legacy(self.path,self.cfg)
        db=Store(self.path); self.addCleanup(db.close)
        original=db._migrate
        def fail(target):
            original(target)
            raise RuntimeError('synthetic interruption')
        with patch.object(db,'_migrate',side_effect=fail),self.assertRaises(RuntimeError): db.bind(self.cfg)
        self.assertEqual(db.db.execute('PRAGMA user_version').fetchone()[0],0)
        self.assertEqual([r[1] for r in db.db.execute('PRAGMA table_info(state)')],['id','payload'])
        db.bind(self.cfg)
        self.assertEqual(db.load(A).timeout_streak,2)
    def test_legacy_account_alert_is_adopted_not_duplicated(self):
        legacy(self.path,self.cfg)
        connection=sqlite3.connect(self.path)
        with connection:
            state=State(mode='unknown',reason='login_expired',notified_reason='login_expired',unknown_alerted=True,failures=3)
            connection.execute('UPDATE state SET payload=?',(json.dumps(dataclasses.asdict(state)),))
        connection.close()
        db=self.open()
        self.assertTrue(db.load_account().unknown_alerted)
        self.assertEqual(advance(db.load_account(),None,Problem('login_expired'),NOW+60),[])
    def test_event_numbers_never_reused_after_legacy_retention(self):
        legacy(self.path,self.cfg,pending=False)
        db=self.open()
        self.save(db,now=NOW+86400*40,events=[])
        self.save(db,now=NOW+86400*40+1)
        self.assertGreater(db.pending(NOW+86400*40+1)[0]['event_id'],9)
    def test_lock_contends_without_unlink_and_releases(self):
        path=Path(self.tmp.name)/'monitor.lock'
        with InstanceLock(path):
            inode=path.stat().st_ino
            with self.assertRaises(RuntimeError):
                with InstanceLock(path): pass
        with InstanceLock(path): self.assertEqual(path.stat().st_ino,inode)


class TransportV2(unittest.TestCase):
    def test_problem_details_are_controlled_not_merely_printable(self):
        for detail in ('sessionid='+'a'*32,'123:SYNTHETIC_SECRET','a'*32,None):
            with self.subTest(detail_type=type(detail).__name__):
                error=Problem('http_error',detail)
                self.assertEqual(error.detail,'')
                self.assertEqual(str(error),'http_error')
        self.assertEqual(Problem('http_error','http=403').detail,'http=403')
        self.assertEqual(Problem('platform_error','status_code=-123').detail,'status_code=-123')
    def test_notification_http_failures_are_classified_without_response_leaks(self):
        import urllib.error
        from io import BytesIO
        for code in (400,401,403,404,408,413,429,500):
            raw=BytesIO(b'TEST_SECRET_RESPONSE')
            error=urllib.error.HTTPError('https://api.telegram.org/botTEST_SECRET/sendMessage',code,'ignored',{},raw)
            with self.subTest(code=code),patch.object(transport.urllib.request.OpenerDirector,'open',side_effect=error):
                with self.assertRaises(Problem) as cm: transport.TelegramClient('TEST').send({'chat_id':1,'text':'x'})
                self.assertEqual(cm.exception.permanent,400<=code<500 and code not in (408,429))
                self.assertNotIn('TEST_SECRET',str(cm.exception)); self.assertTrue(raw.closed)
    def test_configured_timeout_and_no_proxy_inheritance(self):
        with patch.object(transport,'json_request',return_value={'user':{'uid':'123'}}) as req:
            transport.DouyinClient('SYNTHETIC',timeout=7).verify_identity('123')
            self.assertEqual(req.call_args.kwargs['timeout'],7)
            self.assertIsNone(req.call_args.kwargs.get('proxy'))
    def test_telegram_permanent_and_retryable_codes(self):
        for code in (400,401,403,404,429,500):
            with self.subTest(code=code),patch.object(transport,'json_request',return_value={'ok':False,'error_code':code,'description':'NEVER_LOG','parameters':{'retry_after':180}}):
                with self.assertRaises(Problem) as cm: transport.TelegramClient('TEST').send({'chat_id':1,'text':'x'})
                self.assertEqual(cm.exception.permanent,code in (400,401,403,404))
                self.assertNotIn('NEVER_LOG',str(cm.exception))
                if code==429: self.assertEqual(cm.exception.retry_after,180)
    def test_identity_methods_and_receipt_exposure(self):
        client=transport.TelegramClient('123:SYNTHETIC')
        with patch.object(transport,'json_request',return_value={'ok':True,'result':{'id':123,'is_bot':True}}):
            self.assertEqual(client.get_me()['id'],123)
        with patch.object(transport,'json_request',return_value={'ok':True,'result':{'id':1}}):
            self.assertEqual(client.get_chat(1)['id'],1)
        result={'message_id':3,'chat':{'id':1},'text':'x'}
        with patch.object(transport,'json_request',return_value={'ok':True,'result':result}):
            self.assertEqual(client.send({'chat_id':1,'text':'x'}),3)
            self.assertEqual(client.last_receipt,result)


class RuntimeV2(unittest.TestCase):
    def setUp(self):
        install_boundaries()
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'douyin_session').write_text('0'*32)
        (self.root/'douyin_session').chmod(0o600)
        (self.root/'telegram_token').write_text('123:SYNTHETIC')
        (self.root/'telegram_token').chmod(0o600)
        self.cfg=canonical({'targets':[{'target':A,'label':'a','enabled':True},{'target':B,'label':'b','enabled':True}]})
    def run_once(self, client, **kw):
        with patch.object(runtime,'DouyinClient',return_value=client):
            return runtime.run(self.cfg,self.root,self.root/'state',self.root/'logs',once=True,**kw)
    def store(self):
        db=Store(self.root/'state'/'monitor.sqlite3'); self.addCleanup(db.close); return db
    def test_one_account_alert_across_targets_and_restart_one_recovery(self):
        class Client:
            failed=True
            def verify_identity(self,uid):
                if self.failed: raise Problem('login_expired')
            def presence(self,target): return Observation(time.time(),time.time())
        client=Client()
        self.run_once(client,no_notify=True); self.run_once(client,no_notify=True)
        db=self.store()
        self.assertEqual(db.status()['outbox'].get('pending'),1)
        self.assertEqual(set(db.status()['targets']),{A,B})
        client.failed=False; self.run_once(client,no_notify=True)
        self.assertEqual(db.status()['outbox'].get('pending'),2)
        kinds=[json.loads(r['payload'])['kind'] for r in db.db.execute('SELECT payload FROM events WHERE scope="account" ORDER BY id')]
        self.assertEqual(kinds,['unknown','recovered'])
    def test_one_target_failure_does_not_block_other_and_pause(self):
        seen=[]
        class Client:
            def verify_identity(self,uid): pass
            def presence(self,target):
                seen.append(target)
                if target==A: raise Problem('dns_failure')
                return Observation(time.time(),time.time())
        self.run_once(Client(),no_notify=True)
        db=self.store(); self.assertEqual(db.load(A).next_delay,60)
        self.assertEqual(db.load(B).mode,'recent'); self.assertEqual(seen,[A,B])
        self.cfg['targets'][0]['enabled']=False; seen.clear()
        self.run_once(Client(),no_notify=True); self.assertEqual(seen,[B])
    def test_notification_startup_failure_does_not_block_sampling(self):
        class Client:
            def verify_identity(self,uid): pass
            def presence(self,target): return Observation(time.time(),time.time())
        (self.root/'telegram_token').unlink()
        self.assertEqual(self.run_once(Client()),0)
        self.assertEqual(self.store().status()['samples'],2)
    def test_independent_sender_runs_while_collector_waits(self):
        started=threading.Event(); delivered=threading.Event()
        class Client:
            def verify_identity(self,uid): pass
            def presence(self,target):
                if target==B:
                    started.set()
                    if not delivered.wait(5): raise AssertionError('sender blocked by collector')
                return Observation(time.time(),time.time())
        class Telegram:
            def get_me(self): return {'id':123,'is_bot':True}
            def send(self,payload): delivered.set(); return 1
        with patch.object(runtime,'TelegramClient',return_value=Telegram()): self.run_once(Client())
        self.assertTrue(started.is_set()); self.assertTrue(delivered.is_set())
    def child(self,setup,cfg=None,no_notify=True):
        code='from core_helpers import install_boundaries; install_boundaries()\nfrom douyin_monitor import runtime\n'+setup+'\n'
        code+='raise SystemExit(runtime.run('+repr(cfg or self.cfg)+','+repr(str(self.root))+','+repr(str(self.root/'state'))+','+repr(str(self.root/'logs'))+',no_notify='+repr(no_notify)+'))'
        repo=Path(__file__).resolve().parents[1]
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(repo/'src'),str(repo/'tests')])}
        proc=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def clean():
            if proc.poll() is None: proc.kill()
            proc.communicate(timeout=10)
        self.addCleanup(clean)
        return proc
    def wait_for(self,predicate):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            try:
                result=predicate()
                if result: return result
            except (sqlite3.Error,ValueError): pass
            time.sleep(.02)
        self.fail('fixture did not reach expected state')
    def test_heartbeat_updates_during_long_backoff_and_sigterm_is_clean(self):
        proc=self.child("class Client:\n def verify_identity(self,uid): raise runtime.Problem('login_expired')\n def presence(self,target): raise AssertionError('identity gate')\nruntime.DouyinClient=lambda *a,**kw:Client()\nruntime.HEARTBEAT_SECONDS=.05")
        path=self.root/'state'/'monitor.sqlite3'
        first=self.wait_for(lambda: r if (r:=status_snapshot(path)).get('samples')==2 else None)
        heartbeat=first['runtime']['heartbeat_at']
        later=self.wait_for(lambda: r if (r:=status_snapshot(path))['runtime']['heartbeat_at'] and r['runtime']['heartbeat_at']>heartbeat else None)
        self.assertEqual(later['samples'],2)
        self.assertEqual(later['runtime']['status'],'running')
        self.assertEqual(later['runtime']['pid'],proc.pid)
        self.assertIsNone(later['runtime']['stopped_at'])
        with self.assertRaises(RuntimeError):
            with InstanceLock(self.root/'state'/'monitor.lock'): pass
        proc.terminate(); out,err=proc.communicate(timeout=10)
        self.assertEqual(proc.returncode,0,out+err)
        self.assertNotIn('0'*32,out+err)
        stopped=status_snapshot(path)['runtime']
        self.assertEqual(stopped['status'],'stopped'); self.assertIsNotNone(stopped['stopped_at'])
        self.assertGreaterEqual(stopped['stopped_at'],stopped['started_at'])
        with InstanceLock(self.root/'state'/'monitor.lock'): pass
    def test_target_backoff_does_not_throttle_healthy_target(self):
        cfg={**self.cfg,'poll_seconds':.1}
        proc=self.child("class Client:\n def verify_identity(self,uid): pass\n def presence(self,target):\n  if target=="+repr(A)+": raise runtime.Problem('dns_failure')\n  return runtime.Observation(runtime.time.time(),runtime.time.time())\nruntime.DouyinClient=lambda *a,**kw:Client()\nruntime.REQUEST_SPACING=0\nruntime.HEARTBEAT_SECONDS=.02",cfg)
        path=self.root/'state'/'monitor.sqlite3'
        self.wait_for(lambda: status_snapshot(path)['samples']>=10)
        proc.terminate(); out,err=proc.communicate(timeout=10)
        self.assertEqual(proc.returncode,0,out+err)
        db=self.store()
        counts=dict(db.db.execute('SELECT target,count(*) FROM observations GROUP BY target'))
        self.assertGreater(counts[B],counts[A])
        self.assertGreater(db.load(A).next_delay,db.load(B).next_delay)
    def test_notification_bot_mismatch_fails_before_sampling(self):
        (self.root/'state').mkdir()
        db=self.store(); db.bind(self.cfg); db.bind_notification_bot('456')
        class Client:
            def verify_identity(self,uid): raise AssertionError('must not query')
        with self.assertRaises(ValueError): self.run_once(Client())
        self.assertEqual(db.status()['samples'],0)
    def test_sender_permanent_error_blocks_without_sampling_failure(self):
        stop=threading.Event(); fatal=threading.Event()
        (self.root/'state').mkdir()
        db=self.store(); db.bind(self.cfg); db.bind_notification_bot('123')
        for offset in (0,1):
            db.save(State(mode='unknown'),{'observed_at':NOW+offset},[{'kind':'unknown','at':NOW+offset,'reason':'dns_failure','notify':True}],{**self.cfg,**self.cfg['targets'][0]})
        class Telegram:
            calls=0
            def get_me(self): return {'id':123,'is_bot':True}
            def send(self,payload):
                self.calls+=1
                raise Problem('access_denied',permanent=True)
        client=Telegram()
        with patch.object(runtime,'TelegramClient',return_value=client):
            worker=threading.Thread(target=runtime.sender_loop,args=(self.root/'state'/'monitor.sqlite3',self.cfg,self.root,stop,fatal))
            worker.start()
            try: self.wait_for(lambda: db.status()['outbox'].get('blocked')==1)
            finally: stop.set(); worker.join(5)
        self.assertFalse(worker.is_alive()); self.assertFalse(fatal.is_set())
        self.assertEqual(client.calls,1); self.assertEqual(db.pending(time.time()),[])
    def test_probe_is_identity_gated_without_persistence(self):
        class Client:
            def verify_identity(self,uid): raise Problem('account_mismatch')
            def presence(self,target): raise AssertionError('must not sample')
        with patch.object(runtime,'DouyinClient',return_value=Client()): result=runtime.probe(self.cfg,self.root)
        self.assertFalse(result['identity_verified']); self.assertEqual(result['reason'],'account_mismatch')
        self.assertFalse((self.root/'state').exists())
