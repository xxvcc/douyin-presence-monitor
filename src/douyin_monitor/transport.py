"""Bounded stdlib HTTP. Direct collection, explicit notification proxy, no redirects."""
from __future__ import annotations
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .domain import Problem, business_check, parse_presence
from .messages import telegram_visible_text

API = 'https://www.douyin.com/aweme/v1/web/im/user/active/status/?aid=6383'
SELF_API = 'https://www.douyin.com/aweme/v1/web/user/profile/self/?aid=6383'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify_transport(exc):
    root = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(root, (socket.timeout, TimeoutError)):
        return Problem('network_timeout')
    if isinstance(root, socket.gaierror):
        return Problem('dns_failure')
    if isinstance(root, ssl.SSLError):
        return Problem('tls_error')
    return Problem('network_error')


def _retry_after(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 120


def json_request(url, data=None, headers=None, proxy=None, timeout=12):
    # An empty ProxyHandler prevents HTTP_PROXY/HTTPS_PROXY inheritance.
    proxies = {'http': proxy, 'https': proxy} if proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect())
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(65537)
    except urllib.error.HTTPError as exc:
        code = exc.code
        retry = _retry_after(exc.headers.get('Retry-After', '120')) if code == 429 else 0
        exc.close()
        reason = {429: 'rate_limited', 401: 'login_expired', 403: 'access_denied'}.get(code, 'http_error')
        if 300 <= code < 400:
            reason = 'redirect_blocked'
        raise Problem(reason, f'http={code}', retry) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise classify_transport(exc) from None
    if len(raw) > 65536:
        raise Problem('response_too_large')
    if not raw.strip():
        raise Problem('empty_response')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Problem('non_json_response') from None
    if not isinstance(body, dict):
        raise Problem('non_json_response')
    return body


class DouyinClient:
    def __init__(self, token, timeout=12):
        self.token = token
        self.timeout = timeout

    def request(self, url, data=None):
        return json_request(url, data, {
            'Content-Type': 'application/x-www-form-urlencoded; utf-8',
            'Accept': 'application/x-www-form-urlencoded', 'Referer': 'https://www.douyin.com/',
            'User-Agent': UA, 'Cookie': 'sessionid=' + self.token,
        }, timeout=self.timeout)

    def identity(self):
        body = self.request(SELF_API)
        business_check(body)
        user = body.get('user')
        uid = user.get('uid') if isinstance(user, dict) else None
        if isinstance(uid, bool) or not isinstance(uid, (str, int)) or not str(uid):
            raise Problem('identity_unverified')
        return str(uid)

    def verify_identity(self, expected_uid):
        if self.identity() != str(expected_uid):
            raise Problem('account_mismatch')

    def presence(self, target):
        data = urllib.parse.urlencode({'sec_user_ids': json.dumps([target], separators=(',', ':')), 'source': 'heartbeat'}).encode()
        return parse_presence(self.request(API, data), target, time.time())


class TelegramClient:
    def __init__(self, token, proxy=None, timeout=12):
        self.token = token
        self.proxy = proxy
        self.timeout = timeout
        self.last_receipt = None
        self.last_receipt_summary = None

    def _call(self, method, payload=None):
        try:
            body = json_request('https://api.telegram.org/bot' + self.token + '/' + method,
                                json.dumps(payload or {}).encode(), {'Content-Type': 'application/json'},
                                self.proxy, timeout=self.timeout)
        except Problem as error:
            # HTTP failures contain only a controlled code, not raw descriptions/URLs.
            code = int(error.detail.removeprefix('http=')) if error.detail.startswith('http=') else 0
            error.permanent = 400 <= code < 500 and code not in (408, 429)
            raise
        if body.get('ok') is not True:
            code = body.get('error_code')
            parameters = body.get('parameters')
            retry = parameters.get('retry_after', 0) if isinstance(parameters, dict) else 0
            raise Problem('rate_limited' if code == 429 else 'http_error',
                          f'telegram_code={code}' if type(code) is int else 'telegram_api_error',
                          retry, permanent=type(code) is int and 400 <= code < 500 and code not in (408, 429))
        result = body.get('result')
        if not isinstance(result, dict):
            raise Problem('http_error', 'telegram_invalid_result')
        return result

    def get_me(self):
        result = self._call('getMe')
        if type(result.get('id')) is not int or result.get('is_bot') is not True:
            raise Problem('http_error', 'telegram_identity_invalid')
        return result

    def get_chat(self, chat_id):
        result = self._call('getChat', {'chat_id': chat_id})
        if str(result.get('id')) != str(chat_id):
            raise Problem('http_error', 'telegram_chat_mismatch', permanent=True)
        return result

    def send(self, payload):
        self.last_receipt = self.last_receipt_summary = None
        result = self._call('sendMessage', payload)
        expected = telegram_visible_text(payload['text']) if payload.get('parse_mode') == 'HTML' else payload['text']
        chat = result.get('chat')
        if (not isinstance(chat, dict) or str(chat.get('id')) != str(payload['chat_id'])
                or result.get('message_thread_id') != payload.get('message_thread_id')
                or result.get('text') != expected or type(result.get('message_id')) is not int
                or result['message_id'] <= 0):
            # Delivery is ambiguous. Block for explicit operator resolution, never reroute.
            raise Problem('http_error', 'telegram_receipt_mismatch', permanent=True)
        self.last_receipt = result
        self.last_receipt_summary = {
            'message_id': result['message_id'], 'chat_id': chat['id'],
            'thread_id': result.get('message_thread_id'),
            'entity_types': sorted({e.get('type', '') for e in result.get('entities', []) if isinstance(e, dict)}),
            'profile_button': bool(result.get('reply_markup', {}).get('inline_keyboard')),
        }
        return result['message_id']
