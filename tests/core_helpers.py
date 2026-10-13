"""Portable fixtures exercising the integrated package and real CLI entrypoint."""
import json
import os
from pathlib import Path
import subprocess
import sys


def canonical(cfg=None):
    value = {'schema_version':2,'observer_uid':'123','targets':[{'target':'MS4wLjABAAAA_FIXTURE','label':'fixture','enabled':True}],
             'chat_id':1,'thread_id':None,'telegram_proxy':None,'poll_seconds':60,
             'offline_after_seconds':600,'request_timeout':12,'timeout_alert_after':3,
             'unknown_reason_cooldown':300,'retention_days':30,'log_retention_days':14,
             'log_max_bytes':10485760,'log_total_bytes':104857600}
    if cfg:
        value.update(cfg)
        if 'target' in cfg:
            value['targets']=[{'target':value.pop('target'),'label':value.pop('label'),'enabled':True}]
    return value


def install_boundaries():
    # Assert that integrated production implementations exist; no fallback doubles.
    import douyin_monitor.credentials
    import douyin_monitor.observability


def run_local(root,cfg,setup):
    code='from douyin_monitor import runtime, transport\n'+setup+'\n'
    code+='import json\nfrom pathlib import Path\n'
    code+='Path('+repr(str(root/'config.json'))+').write_text(json.dumps('+repr(canonical(cfg))+'))\n'
    code+='from douyin_monitor.cli import main\nraise SystemExit(main('+repr(['--config',str(root/'config.json'),'--credentials',str(root),'--state-dir',str(root/'state'),'--log-dir',str(root/'logs'),'run','--once','--no-notify'])+'))'
    repo=Path(__file__).resolve().parents[1]
    env={**os.environ,'PYTHONPATH':str(repo/'src')}
    return subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=20)
