"""Runs after core/CLI integration; isolated operations worktree needs no stubs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from douyin_monitor import maintenance as m

READY = all(importlib.util.find_spec('douyin_monitor.' + name) is not None
            for name in ('storage', 'domain', 'config', 'locking'))


@unittest.skipUnless(READY, 'core/CLI modules are not present in this isolated worktree')
class IntegratedOperationsTests(unittest.TestCase):
    def test_real_store_config_lock_backup_export_restore(self):
        from douyin_monitor.config import validate_config, target_config
        from douyin_monitor.domain import State
        from douyin_monitor.locking import InstanceLock
        from douyin_monitor.storage import Store, status_snapshot
        target = 'MS4wLjABAAAA_SYNTHETIC'
        cfg = validate_config({'schema_version': 2, 'observer_uid': '12345', 'chat_id': 1,
                               'targets': [{'target': target, 'label': '合成数据', 'enabled': True}]})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.json'; config.write_text(json.dumps(cfg))
            dbpath = root / 'monitor.sqlite3'
            lock = root / 'monitor.lock'
            store = Store(dbpath)
            try:
                store.bind(cfg)
                state = State()
                event = {'kind': 'started', 'at': 1700000000, 'reason': '', 'notify': False}
                store.save(state, {'observed_at': 1700000000, 'kind': 'online'}, [event],
                           target_config(cfg, cfg['targets'][0]))
                backup = m.create_backup(config, dbpath, root / 'backups')
                self.assertEqual(m.verify_backup(backup)['schema_version'], 2)
                result = m.export_data(dbpath, root / 'events.json', format='json')
                self.assertEqual(result['count'], 1)
            finally:
                store.close()
            with InstanceLock(lock):
                with self.assertRaises(RuntimeError):
                    m.restore_backup(backup, config, dbpath, lock)
            result = m.restore_backup(backup, config, dbpath, lock)
            self.assertTrue(result['restored'])
            self.assertEqual(status_snapshot(dbpath, cfg)['schema_version'], 2)


if __name__ == '__main__': unittest.main()
