"""One bounded functional test of the exact scheduled repair command, no trials."""
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import time

PROJECT=Path('/home/ytzhang/rsi-harness')


def main():
    m=runpy.run_path(str(PROJECT/'scripts/review-active-self-skill.py'))
    active=m['read_json'](PROJECT/'runs/ACTIVE_SELF_SKILL_RSI.json')
    root=m['active_root'](active)
    folder=root/'recovery/permission-smoke'/str(time.time_ns());folder.mkdir(parents=True)
    prompt='''这是一个有严格边界的自动化能力 smoke 测试，不是实验修复。
只允许两件事：
1. 只读执行 /home/ytzhang/budget-rl/.deps/docker-static/docker --host unix:///tmp/rsi-harness-docker-overlay2/docker.sock info --format '{{.Driver}}'。
2. 用 apply_patch 在以下唯一目标路径新建 JSON：TARGET，内容包含 docker_driver 的实际检查值，以及 write_succeeded:true。
不修改其他文件，不运行实验或模型，不读取凭据，不访问外网，不使用子 agent，不等待。
如果沙箱无法完成，如实说明，不绕过沙箱。完成后简短报告。
'''.replace('TARGET',str(folder/'capability.json'))
    env=os.environ.copy()
    for key in ('CODEX_THREAD_ID','CODEX_INTERNAL_ORIGINATOR_OVERRIDE'):env.pop(key,None)
    with (folder/'events.jsonl').open('w') as output,(folder/'stderr.log').open('w') as errors:
        child=subprocess.Popen(m['codex_command'](folder/'summary.txt',True),stdin=subprocess.PIPE,stdout=output,
            stderr=errors,text=True,cwd=PROJECT,env=env,start_new_session=True)
        try:child.communicate(prompt,timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
    value=m['read_json'](folder/'capability.json',{})
    passed=child.returncode==0 and value.get('docker_driver')=='overlay2' and value.get('write_succeeded') is True
    m['write_json'](folder/'receipt.json',{'passed':passed,'exit_code':child.returncode,'time':time.time(),'model_trial_calls':0})
    print(json.dumps({'passed':passed,'folder':str(folder),'exit_code':child.returncode}),flush=True)
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
