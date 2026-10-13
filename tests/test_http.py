"""Local HTTP fixture integration checks; never query Douyin or Telegram."""
import http.server
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
from types import SimpleNamespace
from douyin_monitor import domain, messages, transport, storage
m = SimpleNamespace(**{name: getattr(module, name) for module in (domain, messages, transport, storage) for name in dir(module) if not name.startswith('_')})


class Fixture(http.server.BaseHTTPRequestHandler):
    calls=[]
    def log_message(self,*args): pass
    def do_GET(self): self.respond()
    def do_POST(self): self.respond()
    def respond(self):
        data=self.rfile.read(int(self.headers.get('Content-Length',0)))
        self.calls.append((self.path,self.headers.get('Cookie'),data))
        route=self.path.split('?')[0]
        status=200; headers={}; value={'ok':True}
        if route=='/empty': raw=b''
        elif route=='/html': raw=b'<html>verification required</html>'
        elif route=='/large': raw=b'x'*65537
        else:
            if route=='/limit': status=429; headers['Retry-After']='180'
            elif route=='/denied': status=403
            elif route=='/redirect': status=302; headers['Location']='/should-not-be-visited'
            elif route=='/self': value={'status_code':0,'user':{'uid':'12345'}}
            elif route=='/presence': value={'data':[{'sec_user_id':'MS4wLjABAAAA_FIXTURE','last_active_time':int(time.time())-1000}], 'extra':{'now':int(time.time())*1000}}
            elif route=='/telegram':
                payload=json.loads(data)
                value={'ok':True,'result':{'message_id':123,'chat':{'id':payload['chat_id']},'message_thread_id':payload['message_thread_id'],'text':payload['text']}}
            raw=json.dumps(value).encode()
        self.send_response(status)
        for k,v in headers.items(): self.send_header(k,v)
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

class HTTPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Fixture)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def test_classified_real_http_errors(self):
        for path,code in [('empty','empty_response'),('html','non_json_response'),('large','response_too_large'),('limit','rate_limited'),('denied','access_denied'),('redirect','redirect_blocked')]:
            with self.subTest(path=path),self.assertRaises(m.Problem) as c:
                m.json_request(self.base+'/'+path,headers={'Cookie':'sessionid=LOCAL_TEST_ONLY'})
            self.assertEqual(c.exception.code,code)
            self.assertNotIn('LOCAL_TEST_ONLY',str(c.exception))
        self.assertFalse(any(path=='/should-not-be-visited' for path,_,_ in Fixture.calls))
    def test_retry_after_from_real_http(self):
        with self.assertRaises(m.Problem) as c: m.json_request(self.base+'/limit')
        self.assertEqual(c.exception.retry_after,180)
    def test_real_entrypoint_once_against_local_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'douyin_session').write_text('0'*32)
            (root/'douyin_session').chmod(0o600)
            cfg={'target':'MS4wLjABAAAA_FIXTURE','label':'合成测试账号','observer_uid':'12345','chat_id':1,'thread_id':2}
            (root/'config.json').write_text(json.dumps(cfg))
            from core_helpers import run_local
            proc=run_local(root,cfg, "transport.API="+repr(self.base+'/presence')+"; transport.SELF_API="+repr(self.base+'/self'))
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            self.assertNotIn('0'*32,proc.stdout+proc.stderr)
            result=json.loads(proc.stdout.strip().splitlines()[-1]); self.assertEqual(result['mode'],'inactive')
            db=sqlite3.connect(root/'state/monitor.sqlite3')
            self.assertEqual(db.execute('select kind from events').fetchall(),[('baseline',)])
            self.assertEqual(db.execute('select count(*) from outbox').fetchone()[0],0)
            db.close()
    def test_telegram_message_receipt_checked_using_local_transport(self):
        real_request=m.json_request
        def reroute(url,data,headers,proxy,timeout=12):
            self.assertIn('/sendMessage',url)
            return real_request(self.base+'/telegram',data,headers)
        with patch.object(transport,'json_request',side_effect=reroute):
            mid=m.TelegramClient('LOCAL_TEST_ONLY',None).send(m.telegram_payload({'chat_id':1,'thread_id':2},'合成通知，未发往Telegram'))
            self.assertEqual(mid,123)
    def test_wrong_destination_receipt_is_not_success(self):
        with patch.object(transport,'json_request',return_value={'ok':True,'result':{'message_id':1,'chat':{'id':999},'message_thread_id':2,'text':'test'}}):
            with self.assertRaises(m.Problem): m.TelegramClient('TEST',None).send(m.telegram_payload({'chat_id':1,'thread_id':2},'test'))
    def test_unknown_then_recovery_atomic_outbox(self):
        now=time.time()
        with tempfile.TemporaryDirectory() as tmp:
            db=m.Store(Path(tmp)/'test.db'); db.bind_notification_bot('123'); s=m.State()
            cfg={'target':'MS4wLjABAAAA_FIXTURE','label':'fixture','chat_id':1,'thread_id':2}
            for offset in (0,60,120):
                events=m.advance(s,None,m.Problem('network_timeout'),now+offset)
                db.save(s,{'observed_at':now+offset},events,cfg)
            events=m.advance(s,m.Observation(now+170,now+180),None,now+180)
            db.save(s,{'observed_at':now+180},events,cfg)
            rows=db.pending(now+180)
            self.assertEqual(len(rows),2)
            self.assertIn('请求超时',json.loads(rows[0]['payload'])['text'])
            self.assertIn('监测已恢复',json.loads(rows[1]['payload'])['text'])
            self.assertEqual(db.db.execute('select kind from events order by id desc limit 1').fetchone()['kind'],'recovered')
            db.close()

if __name__=='__main__': unittest.main(verbosity=2)
