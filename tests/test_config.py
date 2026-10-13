import copy
import json
import tempfile
import unittest
from pathlib import Path

from douyin_monitor.config import (validate_config, load_config, parse_target,
                                   target_config, atomic_write_config)

CFG = {'observer_uid': '123', 'target': 'MS4wLjABAAAA_FIXTURE', 'label': 'fixture',
       'chat_id': 1, 'thread_id': 2}


class ConfigTests(unittest.TestCase):
    def test_legacy_normalizes_without_mutating(self):
        old = copy.deepcopy(CFG)
        cfg = validate_config(CFG)
        self.assertEqual(CFG, old)
        self.assertEqual(cfg['schema_version'], 2)
        self.assertEqual(cfg['targets'], [{'target': CFG['target'], 'label': 'fixture', 'enabled': True}])
        self.assertEqual([cfg[k] for k in ('poll_seconds', 'request_timeout', 'offline_after_seconds', 'timeout_alert_after')], [60, 12, 600, 3])
        self.assertEqual(target_config(cfg, cfg['targets'][0])['target'], CFG['target'])

    def test_strict_types_ranges_and_unknown_keys(self):
        for key, value in [('chat_id', True), ('chat_id', '1'), ('thread_id', 0),
                           ('observer_uid', 123), ('observer_uid', ' 123'),
                           ('poll_seconds', True), ('poll_seconds', 29),
                           ('poll_seconds', float('nan')), ('request_timeout', float('inf')),
                           ('request_timeout', 0), ('offline_after_seconds', 599),
                           ('timeout_alert_after', 1.5), ('retention_days', 0),
                           ('log_total_bytes', 1), ('label', 'x\x7f'),
                           ('telegram_token', 'secret'), ('schema_version', True),
                           ('schema_version', 3)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_config({**CFG, key: value})

    def test_targets_strict_duplicates_and_all_paused(self):
        cfg = validate_config(CFG)
        cfg['targets'][0]['enabled'] = False
        self.assertFalse(validate_config(cfg)['targets'][0]['enabled'])
        for targets in [[{**cfg['targets'][0], 'enabled': 1}], [cfg['targets'][0]] * 2,
                        [{**cfg['targets'][0], 'typo': 1}], 'bad']:
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                validate_config({**cfg, 'targets': targets})
        self.assertEqual(validate_config({**cfg, 'targets': []})['targets'], [])

    def test_url_parser_original_path_and_authority(self):
        target = CFG['target']
        self.assertEqual(parse_target(target), target)
        self.assertEqual(parse_target('https://www.douyin.com/user/' + target + '?foo=1'), target)
        for value in [' '+target, target+'\n', 'https://evil.invalid/user/'+target,
                      'http://www.douyin.com/user/'+target,
                      'https://www.douyin.com:443/user/'+target,
                      'https://u@www.douyin.com/user/'+target,
                      'https://www.douyin.com/user/x/../'+target,
                      'https://www.douyin.com/user/%4d'+target[1:],
                      'https://www.douyin.com/user/'+target+'/',
                      'https://www.douyin.com/user/'+target+'#x',
                      'https://www.douyin.com/\nuser/'+target]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_target(value)

    def test_proxy_restrictions(self):
        for value in ['socks5://localhost:1080', 'http://u:p@localhost:8',
                      'https://localhost/x', 'http://localhost/#x', 'http://localhost:99999',
                      'http://local host', 'http://localhost\n']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({**CFG, 'telegram_proxy': value})
        self.assertEqual(validate_config({**CFG, 'telegram_proxy': 'http://localhost:8'})['telegram_proxy'], 'http://localhost:8')

    def test_atomic_write_and_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            atomic_write_config(path, CFG)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                atomic_write_config(path, {**CFG, 'poll_seconds': 0})
            self.assertEqual(path.read_bytes(), original)
            self.assertIsNone(load_config_from_null_thread(path)['thread_id'])
            path.write_text('{"chat_id":1,"chat_id":2}')
            with self.assertRaises(ValueError):
                load_config(path)


def load_config_from_null_thread(path):
    path.write_text(json.dumps({**CFG, 'thread_id': None}))
    return load_config(path)
