"""Non-scoring positive control for the variants' SSH and HTTP loopback routes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from offline_verifier import runner
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_resilient import grading
from self_skill_rsi_accel.environment import runner_environment


def check(offline):
    os.environ.update(runner_environment(8041))
    output = offline / 'bridge-check.json'
    if output.exists(): return
    safe = grading.load_manifest(offline / 'grader-manifest.json', file_hash(offline / 'grader-manifest.json'))
    task = 'configure-git-webserver-EVO-1'
    cfg = safe['tasks'][task]
    cid = runner.docker(['run', '-d', '--network', 'none', '--entrypoint', '/bin/sh', cfg['image_id'], '-c', 'sleep infinity']).stdout.strip()
    try:
        with Path(safe['bundle']).open('rb') as data:
            subprocess.run(['docker', 'exec', '-i', cid, 'tar', '--no-same-owner', '-xzf', '-', '-C', '/'],
                           stdin=data, capture_output=True, check=True, timeout=120)
        server = '''import http.server,threading,time
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'RSI_VARIANT_LOOPBACK_CONTROL')
for port in (22,8080):
 server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler)
 threading.Thread(target=server.serve_forever,daemon=True).start()
time.sleep(180)
'''
        runner.docker(['exec', '-d', cid, '/opt/rsi-offline-v1/python/bin/python3', '-I', '-c', server])
        runner.docker(['exec', cid, 'mkdir', '-p', '/logs/bridge-control'])
        info = grading.inspect_container(cid, cfg['image_id']); pid = runner.host_pid(info['State']['Pid'])
        if Path(f'/proc/{pid}/root/etc/hostname').read_text().strip() != info['Config']['Hostname']:
            raise RuntimeError('bridge_container_pid_mismatch')
        probe = '''import importlib.util,json,sys,urllib.request
from pathlib import Path
spec=importlib.util.spec_from_file_location('bridge','/opt/rsi-offline-v1/isolated.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
mod.Path=lambda p: Path('/logs/bridge-control/network.json') if p=='/logs/verifier/network.json' else Path(p)
sys.argv=['probe','configure-git-webserver-EVO-1'];mod.isolate()
for port in (22,8080):
 with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/',timeout=10) as r:
  assert r.read()==b'RSI_VARIANT_LOOPBACK_CONTROL'
print(json.dumps({'passed':True,'ports':[22,8080],'attestation':json.loads(Path('/logs/bridge-control/network.json').read_text())}))
'''
        result = subprocess.run(['nsenter', '--preserve-credentials', '--target', str(pid), '--user', '--mount',
            '--pid', '--root', '--wd', '--', '/opt/rsi-offline-v1/python/bin/python3', '-I', '-c', probe],
            capture_output=True, text=True, timeout=40)
        if result.returncode: raise RuntimeError('bridge_control_failed:' + result.stderr[-2500:])
        value = json.loads(result.stdout)
        value.update(time=time.time(), model_calls=0, formal_scores=0, image=cfg['image_id'],
                     grader_sha256=file_hash(offline / 'grader-manifest.json'))
        atomic_json(output, value); print(json.dumps(value))
    finally:
        runner.docker(['stop', '--time', '1', cid]); runner.docker(['rm', cid])


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--offline', type=Path, required=True)
    check(p.parse_args().offline.resolve())
