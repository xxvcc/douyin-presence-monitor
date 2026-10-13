"""Escaped HTML notifications with explicit immutable delivery payloads."""
import datetime as dt
from html import escape
from html.parser import HTMLParser
import re
from .domain import REASONS, LABELS

TZ = dt.timezone(dt.timedelta(hours=8))

def stamp(value):
    return dt.datetime.fromtimestamp(value,TZ).strftime('%Y-%m-%d %H:%M:%S') if value else '暂无'

def duration_label(seconds):
    seconds=max(0,int(seconds))
    hours,remainder=divmod(seconds,3600)
    minutes,seconds=divmod(remainder,60)
    if hours:
        return f'{hours} 小时'+(f' {minutes} 分' if minutes else '')
    if minutes:
        return f'{minutes} 分 {seconds} 秒' if seconds else f'{minutes} 分钟'
    return f'{seconds} 秒'

def telegram_visible_text(value):
    class TextOnly(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts=[]
        def handle_data(self,data): self.parts.append(data)
    parser=TextOnly(); parser.feed(value); parser.close()
    return ''.join(parser.parts)

def render_event(event, s, cfg):
    kind=event['kind']
    headings={'unknown':('⚠️','状态未知'),'recovered':('✅','监测已恢复'),
              'online':('🟢','检测到近期活跃'),'offline':('⚪','疑似离线'),
              'started':('📡','监测已启动'),'baseline':('📍','监测基线'),
              'preview':('🎨','通知排版预览')}
    icon,title=headings.get(kind,('📡','抖音监测通知'))
    lines=[f'{icon} <b>{title}</b>',f"<b>{escape(cfg['label'])}</b>"]
    if kind=='preview': lines += ['<blockquote>仅预览当前记录，不是新的状态事件。</blockquote>']
    if kind=='unknown':
        title,advice=REASONS[event['reason']]
        lines += ['', '<b>异常原因</b>', f'{escape(title)} · <code>{escape(event["reason"])}</code>',
                  f'<blockquote>{escape(advice)}</blockquote>']
        count=event.get('consecutive_timeouts',s.timeout_streak)
        if event['reason']=='network_timeout' and count:
            lines.append(f'连续超时　<b>{int(count)} 次</b>')
        if event.get('detail'): lines.append(f'详情　<code>{escape(event["detail"])}</code>')
        lines += ['', '<b>最近记录</b>',
                  f'最后活跃　<code>{stamp(s.active_at)}</code>',
                  f'成功查询　<code>{stamp(s.last_success_at)}</code>',
                  f'发现异常　<code>{stamp(event["at"])}</code>',
                  '', '<b>后续处理</b>',
                  f'约 <b>{duration_label(s.next_delay)}</b>后重试，恢复后通知。',
                  '<i>本次不判定下线；同一原因持续出现不重复提醒。</i>']
    else:
        clock_label='预览时间' if kind=='preview' else '检测时间'
        lines += ['', '<b>时间记录</b>',
                  f'最近活跃　<code>{stamp(s.active_at)}</code>',
                  f'{clock_label}　<code>{stamp(event["at"])}</code>',
                  '', '<b>状态说明</b>']
        if kind=='online':
            lines += ['检测到新的活跃记录，当前判断为近期活跃。', '<i>不代表准确的上线时刻或连续在线时长。</i>']
        elif kind=='offline':
            lines += [f'超过 <b>{duration_label(cfg.get("offline_after_seconds",600))}</b>未见活跃时间更新。',
                      '<i>这是阈值判断，不是准确的下线时刻。</i>']
        else:
            lines += [f'当前判断：<b>{escape(LABELS[s.mode])}</b>']
            if kind=='started':
                lines += [f'采样间隔 {duration_label(cfg.get("poll_seconds",30))} · 离线阈值 {duration_label(cfg.get("offline_after_seconds",600))}',
                          '<i>首次读取仅建基线，不算一次新上线。</i>']
            elif kind=='recovered':
                if event.get('reason') in REASONS: lines += ['此前异常：'+escape(REASONS[event['reason']][0])]
                lines += ['已恢复有效采样。', '<i>中断期间的上下线不作推断。</i>']
            else: lines += ['<i>活跃状态为估算，不代表准确在线时长。</i>']
    footer='北京时间 UTC+8 · 抖音活动监测'
    if event.get('id') is not None: footer+=f' · 事件 #{escape(str(event["id"]))}'
    lines += ['',f'<i>{footer}</i>']
    return '\n'.join(lines)

def telegram_payload(cfg, text, *, html=False):
    payload={'chat_id':int(cfg['chat_id']), 'text':text,
             'disable_notification':False,'protect_content':True,'allow_paid_broadcast':False,'link_preview_options':{'is_disabled':True}}
    if cfg.get('thread_id') is not None: payload['message_thread_id']=int(cfg['thread_id'])
    if html:
        payload['parse_mode']='HTML'
        target=cfg.get('target','')
        if re.fullmatch(r'[A-Za-z0-9_-]{10,300}',target):
            payload['reply_markup']={'inline_keyboard':[[{'text':'查看抖音主页 ↗','url':'https://www.douyin.com/user/'+target}]]}
    return payload
