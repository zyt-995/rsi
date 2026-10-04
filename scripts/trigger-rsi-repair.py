"""A minute-level incident trigger for the existing serialized Codex service."""
import json
from pathlib import Path
import subprocess
import time

PROJECT=Path('/home/ytzhang/rsi-harness')
MONITOR=Path('/data2/ytzhang/rsi-harness-runs/codex-monitor')


def read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def main():
    active=read(PROJECT/'runs/ACTIVE_SELF_SKILL_RSI.json');root=Path(active.get('root','/nonexistent'))
    if not root.is_relative_to('/data2/ytzhang/rsi-harness-runs'):return
    policy=read(root/'recovery/autorepair-policy.json')
    if not policy.get('enabled') or policy.get('root')!=str(root):return
    if read(root/'recovery/manual-repair.json').get('until',0)>time.time():return
    status=read(root/'status.json')
    state=status.get('state')
    stale=time.time()-read(root/'heartbeat.json').get('time',0)>120
    if state in ('complete','paused','pausing'):return
    if state not in ('awaiting_environment','awaiting_inference','needs_attention') and not stale:return
    if time.time()-read(MONITOR/'latest.json').get('started',0)<300:return
    running=subprocess.run(['systemctl','--user','is-active','clean-rsi-codex-review.service'],capture_output=True,text=True)
    if running.stdout.strip() in ('active','activating'):return
    subprocess.run(['systemctl','--user','start','--no-block','clean-rsi-codex-review.service'],check=True)


if __name__=='__main__':main()
