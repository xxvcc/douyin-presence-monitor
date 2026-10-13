"""Pure presence rules. Unknown is not evidence of departure."""
from __future__ import annotations
import dataclasses
import math
import re

REASONS = {
 'login_expired': ('登录失效', '抖音返回“用户未登录”。请重新登录并安全更新本机凭据。'),
 'account_mismatch': ('登录账号不匹配', '当前登录账号与配置绑定账号不同；已停止查询目标，需核对凭据。'),
 'identity_unverified': ('无法验证登录账号', '本人资料接口没有返回有效账号标识，暂不查询目标。'),
 'network_timeout': ('请求超时', '网络或接口响应过慢；将自动退避重试。'),
 'dns_failure': ('域名解析失败', '无法解析抖音域名；请检查网络和 DNS。'),
 'tls_error': ('TLS/证书错误', '无法建立可信的 HTTPS 连接；不绕过证书检查。'),
 'network_error': ('网络连接失败', '连接未能完成；将自动退避重试。'),
 'rate_limited': ('接口限流', 'HTTP 429；已降低查询频率并尊重 Retry-After。'),
 'access_denied': ('接口拒绝访问', 'HTTP 403：可能与登录、权限或风控有关，无法仅凭此确定原因。'),
 'http_error': ('HTTP 服务错误', '服务返回非成功 HTTP 状态；不会据此判定下线。'),
 'redirect_blocked': ('接口发生跳转', '为防止凭据被转发，已拒绝自动跳转。'),
 'empty_response': ('接口返回空内容', '本次没有可解析的数据；不会据此判定下线。'),
 'non_json_response': ('返回内容不是 JSON', '可能是页面、验证提示或格式变化；需检查官方网页。'),
 'response_too_large': ('响应超出大小限制', '已停止读取异常响应，避免保存非预期内容。'),
 'platform_error': ('抖音业务错误', '接口返回非成功业务码；具体码会显示在安全详情中。'),
 'target_missing': ('目标记录缺失', '响应中没有该账号，可能与可见性、关系或接口变化有关，不能确定是离线。'),
 'status_unavailable': ('活跃时间不可用', '目标时间为 0 或缺失；可能是未公开状态或数据暂不可用，不能确定具体隐私设置。'),
 'invalid_active_time': ('活跃时间格式异常', '时间戳格式、范围或类型不符合预期。'),
 'future_active_time': ('活跃时间异常超前', '返回的活跃时间明显晚于服务器时间，暂不采用。'),
 'server_clock_invalid': ('服务器时间缺失或陈旧', '无法可靠判断十分钟阈值；请检查本机时间或响应缓存。'),
 'ambiguous_target': ('目标返回多条记录', '无法唯一确认要采用的目标数据。'),
 'timestamp_regression': ('活跃时间倒退', '新返回的时间早于已确认时间，可能是缓存或乱序；保留旧记录。'),
 'response_out_of_order': ('响应时间倒退', '服务器响应时间早于之前有效响应，暂不采用。'),
 'sampling_gap': ('采样发生中断', '没有连续的新鲜观测；恢复后不补造期间的上下线事件。'),
 'internal_error': ('程序内部异常', '已保留最后有效记录；请查看本机状态和日志排查。'),
}
LABELS = {'initial':'尚未建立基线', 'recent':'近期活跃（估算）', 'inactive':'疑似离线', 'unknown':'状态未知'}

class Problem(Exception):
    def __init__(self, code, detail='', retry_after=0, permanent=False):
        self.code = code if code in REASONS else 'internal_error'
        # Only controlled numeric/status details, never raw exception messages or response text.
        safe_detail = r'(?:http=[0-9]{3}|status_code=-?[0-9]{1,12}|telegram_code=[0-9]{3}|telegram_(?:api_error|invalid_result|identity_invalid|chat_mismatch|receipt_mismatch))'
        self.detail = detail if isinstance(detail, str) and re.fullmatch(safe_detail, detail) else ''
        try:
            retry = float(retry_after or 0)
        except (ValueError, TypeError):
            retry = 0
        self.retry_after = max(0, min(retry, 3600)) if math.isfinite(retry) else 0
        self.permanent = bool(permanent)
        super().__init__(self.code)

@dataclasses.dataclass
class Observation:
    active_at: float
    server_at: float

@dataclasses.dataclass
class State:
    mode: str = 'initial'
    active_at: float = 0
    server_at: float = 0
    last_success_at: float = 0
    last_attempt_at: float = 0
    unknown_since: float = 0
    reason: str = ''
    detail: str = ''
    notified_reason: str = ''
    notified_at: float = 0
    failures: int = 0
    timeout_streak: int = 0
    # True once this unknown episode has queued an alert; outbox preserves delivery order.
    unknown_alerted: bool = False
    next_delay: float = 30
    def reset_confidence(self):
        # Unknown already carries no presence confidence. Preserve its alert/counter
        # across restarts so short process restarts cannot suppress or duplicate it.
        if self.mode=='unknown': return
        self.mode = 'initial'
        self.reason = ''
        self.unknown_since = 0
        self.notified_reason = ''
        self.notified_at = 0
        self.failures = 0
        self.timeout_streak = 0
        self.unknown_alerted = False

def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)

def business_check(body):
    if not isinstance(body, dict): raise Problem('non_json_response')
    code = body.get('status_code', 0)
    if code == 8 or code == '8': raise Problem('login_expired')
    if code not in (0, '0', None):
        detail = f'status_code={code}' if isinstance(code, int) else ''
        raise Problem('platform_error', detail)

def parse_presence(body, target, observed_at):
    business_check(body)
    extra = body.get('extra')
    server_ms = numeric(extra.get('now')) if isinstance(extra, dict) else None
    if server_ms is None or server_ms < 946684800000 or abs(server_ms/1000-observed_at) > 300:
        raise Problem('server_clock_invalid')
    server_at = server_ms/1000
    rows = body.get('data')
    matches = [r for r in rows if isinstance(r, dict) and r.get('sec_user_id') == target] if isinstance(rows, list) else []
    if not matches: raise Problem('target_missing')
    if len(matches) != 1: raise Problem('ambiguous_target')
    raw = matches[0].get('last_active_time')
    if raw is None or (not isinstance(raw, bool) and raw == 0): raise Problem('status_unavailable')
    active = numeric(raw)
    if active is None or active < 946684800: raise Problem('invalid_active_time')
    if active > server_at+60: raise Problem('future_active_time')
    return Observation(active, server_at)

def advance(s, observation, problem, now, threshold=600, unknown_cooldown=300, max_gap=90, timeout_alert_after=3, *, notify_allowed=True):
    previous = s.mode
    s.last_attempt_at = now
    if problem is None and observation is not None:
        if observation.server_at < s.server_at:
            problem = Problem('response_out_of_order')
        elif observation.active_at < s.active_at:
            problem = Problem('timestamp_regression')
    if problem is not None:
        previous_reason=s.reason
        if previous != 'unknown': s.unknown_since = now
        s.timeout_streak=(s.timeout_streak+1 if previous=='unknown' and previous_reason=='network_timeout' else 1) if problem.code=='network_timeout' else 0
        s.mode = 'unknown'; s.reason = problem.code; s.detail = problem.detail
        s.failures += 1
        if problem.code=='network_timeout':
            notice=s.timeout_streak>=timeout_alert_after and (not s.unknown_alerted or s.notified_reason!=problem.code)
        else:
            immediate=problem.code in ('login_expired','account_mismatch','identity_unverified')
            notice=not s.unknown_alerted or (s.notified_reason!=problem.code and (immediate or now-s.notified_at>=unknown_cooldown))
        # Suppressed account-owned alerts must never consume target deduplication.
        notice = notice and notify_allowed
        if notice:
            s.notified_reason = problem.code; s.notified_at = now
            s.unknown_alerted=True
        if notice or previous!='unknown' or previous_reason!=problem.code:
            return [{'kind':'unknown','at':now,'reason':problem.code,'detail':problem.detail,
                     'consecutive_timeouts':s.timeout_streak,'notify':notice}]
        return []
    if observation is None: raise ValueError('observation or problem required')
    gap = previous not in ('initial','unknown') and s.last_success_at > 0 and now-s.last_success_at > max_gap
    previous_active = s.active_at
    was_reason = s.reason or ('sampling_gap' if gap else '')
    was_alerted=s.unknown_alerted
    s.active_at = observation.active_at; s.server_at = observation.server_at
    s.last_success_at = now; s.failures = 0; s.reason = ''; s.detail = ''
    s.timeout_streak=0; s.unknown_alerted=False
    s.mode = 'recent' if max(0, observation.server_at-observation.active_at) <= threshold else 'inactive'
    s.unknown_since = 0; s.notified_reason = ''; s.notified_at = 0
    if previous == 'initial':
        return [{'kind':'baseline','at':now,'reason':'','notify':False}]
    if previous == 'unknown' or gap:
        return [{'kind':'recovered','at':now,'reason':was_reason,'notify':was_alerted}]
    if previous == 'inactive' and s.mode == 'recent' and observation.active_at > previous_active:
        return [{'kind':'online','at':now,'reason':'','notify':True}]
    if previous == 'recent' and s.mode == 'inactive':
        return [{'kind':'offline','at':now,'reason':'','notify':True}]
    return []

def retry_delay(code, failures, normal=30, retry_after=0, timeout_alert_after=3):
    if code in ('login_expired','account_mismatch','identity_unverified'): return max(600,retry_after)
    if code=='network_timeout':
        if failures<timeout_alert_after: return min(3600,max(normal,retry_after))
        exponent=failures-timeout_alert_after+1
    else: exponent=failures-1
    base=min(300,normal*2**min(max(0,exponent),8))
    if code=='rate_limited': base=max(120,base)
    return min(3600,max(base,retry_after))
