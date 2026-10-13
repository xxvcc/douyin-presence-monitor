"""Strict, secret-free schema v2 configuration; v1 is normalized in memory."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from .credentials import _atomic_write

DEFAULTS = {
    'thread_id': None, 'telegram_proxy': None,
    'poll_seconds': 60, 'offline_after_seconds': 600, 'request_timeout': 12,
    'timeout_alert_after': 3, 'unknown_reason_cooldown': 300, 'retention_days': 30,
    'log_retention_days': 14, 'log_max_bytes': 10485760, 'log_total_bytes': 104857600,
}
# Only these keys may be edited through config set. Routes require notify setup.
SAFE_RULES = frozenset(DEFAULTS) - {'thread_id', 'telegram_proxy'}
TARGET_RE = re.compile(r'[A-Za-z0-9_-]{10,300}')


def parse_target(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('invalid target')
    if TARGET_RE.fullmatch(value):
        return value
    # Validate the ORIGINAL string/path: urlsplit silently removes some controls.
    if any(c.isspace() or unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('invalid target URL')
    match = re.fullmatch(r'https://www\.douyin\.com/user/([A-Za-z0-9_-]{10,300})(?:\?[^#]*)?', value)
    if not match:
        raise ValueError('expected sec_uid or exact HTTPS Douyin profile URL')
    return match.group(1)


def _number(cfg, key, minimum, maximum, *, integer=False):
    value = cfg[key]
    if type(value) not in ((int,) if integer else (int, float)):
        raise ValueError('invalid numeric configuration: ' + key)
    if (type(value) is float and not math.isfinite(value)) or not minimum <= value <= maximum:
        raise ValueError('numeric configuration out of range: ' + key)


def _proxy(value):
    if value is None:
        return
    if not isinstance(value, str) or any(c.isspace() or unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('invalid proxy')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                 parsed.username is None and parsed.password is None and
                 parsed.path in ('', '/') and not parsed.query and not parsed.fragment and
                 parsed.port != 0 and '\\' not in value and '%' not in parsed.netloc)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('invalid proxy; HTTP(S) without credentials required')


def validate_config(data: dict) -> dict:
    if type(data) is not dict:
        raise ValueError('configuration must be an object')
    allowed = set(DEFAULTS) | {'schema_version', 'observer_uid', 'targets', 'target', 'label', 'chat_id'}
    if set(data) - allowed:
        # Never echo unknown names: a pasted token may be an object key.
        raise ValueError('unknown configuration key')
    version = data.get('schema_version', 1 if 'target' in data else 2)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('unsupported configuration schema')
    cfg = {**DEFAULTS, **data}
    if 'targets' in data:
        if version != 2 or 'target' in data or 'label' in data:
            raise ValueError('mixed configuration schemas')
        targets = data['targets']
    else:
        if version != 1 or not {'target', 'label'} <= data.keys():
            raise ValueError('missing targets')
        targets = [{'target': data['target'], 'label': data['label'], 'enabled': True}]
    if type(targets) is not list or len(targets) > 100:
        raise ValueError('targets must be an array of at most 100 entries')
    normalized, seen = [], set()
    for target in targets:
        if type(target) is not dict or set(target) != {'target', 'label', 'enabled'}:
            raise ValueError('invalid target entry fields')
        identifier = target['target']
        # URLs accepted at command input, not persisted configuration.
        if not isinstance(identifier, str) or not TARGET_RE.fullmatch(identifier):
            raise ValueError('invalid sec_uid')
        label = target['label']
        if not isinstance(label, str) or not 1 <= len(label) <= 100 or not label.strip() or any(unicodedata.category(c).startswith('C') for c in label):
            raise ValueError('invalid label')
        if type(target['enabled']) is not bool:
            raise ValueError('enabled must be a boolean')
        if identifier in seen:
            raise ValueError('duplicate target')
        seen.add(identifier)
        normalized.append(dict(target))
    if not isinstance(cfg.get('observer_uid'), str) or not re.fullmatch(r'[1-9][0-9]{0,29}', cfg['observer_uid']):
        raise ValueError('invalid observer_uid')
    if type(cfg.get('chat_id')) is not int or not 0 < abs(cfg['chat_id']) < 2**63:
        raise ValueError('invalid chat_id')
    if cfg['thread_id'] is not None and (type(cfg['thread_id']) is not int or not 0 < cfg['thread_id'] < 2**63):
        raise ValueError('invalid thread_id')
    _proxy(cfg['telegram_proxy'])
    for key, lower, upper, integer in [
        ('poll_seconds', 30, 86400, False), ('offline_after_seconds', 600, 2592000, False),
        ('request_timeout', 1, 120, False), ('timeout_alert_after', 1, 10, True),
        ('unknown_reason_cooldown', 0, 86400, False), ('retention_days', 1, 36500, True),
        ('log_retention_days', 1, 3650, True), ('log_max_bytes', 1024, 10737418240, True),
        ('log_total_bytes', 1024, 107374182400, True),
    ]:
        _number(cfg, key, lower, upper, integer=integer)
    if cfg['log_total_bytes'] < cfg['log_max_bytes']:
        raise ValueError('log_total_bytes must cover log_max_bytes')
    cfg.pop('target', None)
    cfg.pop('label', None)
    cfg.update(schema_version=2, targets=normalized)
    return cfg


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def load_config(path) -> dict:
    try:
        with Path(path).open(encoding='utf-8') as source:
            text = source.read(1048577)
        if len(text) > 1048576:
            raise ValueError('configuration too large')
        data = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('invalid configuration JSON') from None
    return validate_config(data)


def target_config(cfg, target: dict) -> dict:
    return {key: value for key, value in cfg.items() if key != 'targets'} | dict(target)


def atomic_write_config(path, cfg):
    value = json.dumps(validate_config(cfg), ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    _atomic_write(path, value, preserve=True)
