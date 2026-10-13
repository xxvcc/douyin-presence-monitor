import unittest
from unittest.mock import patch
from types import SimpleNamespace
from douyin_monitor import domain, messages, transport, storage
m = SimpleNamespace(**{name: getattr(module, name) for module in (domain, messages, transport, storage) for name in dir(module) if not name.startswith('_')})


CFG={'label':'测试账号','target':'MS4wLjABAAAA_TEST_TARGET','chat_id':1,'thread_id':None,'poll_seconds':30,'offline_after_seconds':600}
NOW=1791736200
class MessageFormattingTests(unittest.TestCase):
    def setUp(self):
        self.s=m.State(mode='unknown',active_at=NOW-300,last_success_at=NOW-30,next_delay=30)
    def event(self,kind='unknown',**kw):
        return {'kind':kind,'at':NOW,'reason':'network_timeout','notify':True,'id':42,**kw}
    def test_unknown_has_sections_and_html(self):
        text=m.render_event(self.event(),self.s,CFG)
        self.assertIn('<b>状态未知</b>',text)
        self.assertIn('<code>network_timeout</code>',text)
        for word in ['最近记录','最后活跃','成功查询','30 秒','不判定下线','北京时间','42']:
            self.assertIn(word,text)
    def test_untrusted_labels_are_escaped(self):
        label='甲 <b>不是标签</b> & "乙"'
        text=m.render_event(self.event(),self.s,{**CFG,'label':label})
        self.assertIn('&lt;b&gt;不是标签&lt;/b&gt;',text)
        self.assertIn(label,m.telegram_visible_text(text))
    def test_html_payload_is_explicit_and_has_profile_button(self):
        p=m.telegram_payload(CFG,'<b>test</b>',html=True)
        self.assertEqual(p['parse_mode'],'HTML')
        self.assertNotIn('message_thread_id',p)
        self.assertEqual(p['reply_markup']['inline_keyboard'][0][0]['url'],'https://www.douyin.com/user/'+CFG['target'])
    def test_plain_payload_remains_plain(self):
        p=m.telegram_payload(CFG,'plain <test>')
        self.assertNotIn('parse_mode',p)
    def test_visible_text_preserves_linebreaks_and_emoji(self):
        text='<b>⚠️ 状态未知</b>\n<code>A &amp; B</code>\n<i>&lt;test&gt;</i>'
        self.assertEqual(m.telegram_visible_text(text),'⚠️ 状态未知\nA & B\n<test>')
    def test_telegram_receipt_compares_rendered_text(self):
        text=m.render_event(self.event(),self.s,CFG)
        result={'ok':True,'result':{'message_id':123,'chat':{'id':1},'text':m.telegram_visible_text(text),'entities':[{'type':'bold'},{'type':'code'}]}}
        with patch.object(transport,'json_request',return_value=result):
            client=m.TelegramClient('TEST',None)
            self.assertEqual(client.send(m.telegram_payload(CFG,text,html=True)),123)
            self.assertIn('bold',client.last_receipt_summary['entity_types'])
    def test_empty_times_do_not_become_epoch(self):
        text=m.render_event(self.event(),m.State(mode='unknown'),CFG)
        self.assertNotIn('1970',text)
        self.assertIn('暂无',text)
    def test_preview_is_explicit_not_a_fake_transition(self):
        text=m.render_event(self.event('preview'),self.s,CFG)
        self.assertIn('排版预览',text)
        self.assertIn('不是新的状态事件',text)
    def test_all_kinds_are_bounded(self):
        for kind in ['online','offline','unknown','recovered','started','baseline','preview']:
            with self.subTest(kind=kind):
                text=m.render_event(self.event(kind),self.s,CFG)
                self.assertLess(len(m.telegram_visible_text(text)),4096)
                self.assertIn('<b>',text)
    def test_threshold_and_retry_duration_follow_config(self):
        self.s.next_delay=330
        text=m.render_event(self.event(),self.s,CFG)
        self.assertIn('5 分 30 秒',text)
        text=m.render_event(self.event('offline'),self.s,{**CFG,'offline_after_seconds':1200})
        self.assertIn('20 分钟',text)

if __name__=='__main__': unittest.main()
