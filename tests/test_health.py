"""Health-check semantics use no real processes, credentials or network."""
import importlib
import importlib.util
import unittest


class HealthTests(unittest.TestCase):
    def test_requires_fresh_running_heartbeat(self):
        self.assertIsNotNone(importlib.util.find_spec('douyin_monitor.health'))
        healthy = importlib.import_module('douyin_monitor.health').healthy
        for runtime, expected in [
            ({'status': 'running', 'heartbeat_at': 1000, 'stopped_at': None}, True),
            ({'status': 'running', 'heartbeat_at': 800, 'stopped_at': None}, False),
            ({'status': 'stopped', 'heartbeat_at': 1000, 'stopped_at': 1000}, False),
            ({'status': 'running', 'heartbeat_at': 2000, 'stopped_at': None}, False),
            ({'status': 'running', 'heartbeat_at': float('nan')}, False),
            ({'status': 'running', 'heartbeat_at': True}, False),
            ({}, False),
        ]:
            with self.subTest(runtime=runtime):
                self.assertEqual(healthy({'runtime': runtime}, now=1001), expected)
        self.assertFalse(healthy({}, now=1001))


if __name__ == '__main__':
    unittest.main()
