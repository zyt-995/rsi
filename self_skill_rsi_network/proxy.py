"""A separate experiment-only proxy; never depends on an SSH forwarding session."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

import yaml
from rsi_harness.terminal_infra import atomic_json, file_hash

PORT = 17898
HOST_URL = f'http://127.0.0.1:{PORT}'
CONTAINER_URL = f'http://10.0.2.2:{PORT}'
TARGETS = ('https://github.com', 'https://compcert.org',
           'https://mirrors.tuna.tsinghua.edu.cn/ubuntu/dists/noble/InRelease')


def probe(url, proxy=HOST_URL):
    command = ['curl', '-I', '-sS', '--connect-timeout', '5', '--max-time', '12',
               '--proxy', proxy, '--noproxy', '', '-o', '/dev/null', '-w', '%{http_code}', url]
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=15)
        return {'url': url, 'ok': p.returncode == 0 and p.stdout[:1] in ('2', '3'),
                'http_status': p.stdout, 'exit_code': p.returncode, 'error': p.stderr[-300:]}
    except subprocess.TimeoutExpired:
        return {'url': url, 'ok': False, 'error': 'probe_timeout'}


def health():
    with ThreadPoolExecutor(max_workers=3) as pool:
        checks = list(pool.map(probe, TARGETS))
    return {'time': time.time(), 'passed': all(v['ok'] for v in checks), 'checks': checks}


def install(root):
    root = root.resolve()
    if root.exists():
        raise RuntimeError('new_private_proxy_directory_required')
    with socket.socket() as s:
        s.bind(('127.0.0.1', PORT))
    source = Path('/home/ytzhang/.config/mihomo/config.yaml')
    c = yaml.safe_load(source.read_text())
    address = c.get('external-controller', '')
    if not address.startswith('127.0.0.1:'):
        raise RuntimeError('local_controller_required')
    req = urllib.request.Request('http://' + address + '/proxies',
        headers={'Authorization': 'Bearer ' + c.get('secret', '')})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        groups = json.loads(response.read())['proxies']
    name = groups['♻️ OpenAI 可用自动选择']['now']
    node = next(p for p in c['proxies'] if p['name'] == name)
    if node.get('dialer-proxy'):
        raise RuntimeError('chained_proxy_needs_separate_review')
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    config = root / 'config.yaml'
    # This generated runtime file contains credentials and is never printed.
    payload = {'mixed-port': PORT, 'allow-lan': False, 'bind-address': '127.0.0.1',
        'mode': 'rule', 'ipv6': True, 'log-level': 'warning',
        'proxies': [node], 'rules': ['MATCH,' + name]}
    with os.fdopen(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=False)
    binary = '/home/ytzhang/.local/bin/mihomo'
    check = subprocess.run([binary, '-t', '-d', str(root), '-f', str(config)],
                           capture_output=True, text=True, timeout=30)
    if check.returncode:
        raise RuntimeError('private_proxy_configuration_not_valid')
    unit = 'rsi-experiment-network-20261002'
    subprocess.run(['systemd-run', '--user', '--unit', unit,
        '--property=Restart=on-failure', '--property=RestartSec=5',
        '--property=UMask=0077', binary, '-d', str(root), '-f', str(config)], check=True, timeout=20)
    deadline = time.monotonic() + 40
    result = health()
    while not result['passed'] and time.monotonic() < deadline:
        time.sleep(2)
        result = health()
    atomic_json(root / 'deployment.json', {'unit': unit + '.service', 'port': PORT,
        'source_config_sha256': file_hash(source), 'runtime_config_sha256': file_hash(config),
        'runtime_config': str(config), 'node': name, 'host_proxy': HOST_URL,
        'container_proxy': CONTAINER_URL, 'ssh_session_dependency': False,
        'existing_proxy_configuration_changed': False, 'acceptance': result})
    if not result['passed']:
        raise RuntimeError('independent_proxy_acceptance_failed')
    print(json.dumps({'passed': True, 'port': PORT, 'unit': unit, 'checks': result['checks']}))


def check_container(root, container):
    """Read-only probes in an already-owned solver container; no task files changed."""
    from offline_verifier.runner import docker
    info = json.loads(docker(['inspect', container]).stdout)[0]
    if not info['State']['Running'] or info['HostConfig']['NetworkMode'] != 'host':
        raise RuntimeError('live_rootless_host_network_required')
    script = '''import json,urllib.request
p=urllib.request.build_opener(urllib.request.ProxyHandler({'http':PROXY,'https':PROXY}))
for url in TARGETS:
 try:
  r=p.open(urllib.request.Request(url,method='HEAD'),timeout=10)
  print(json.dumps({'url':url,'ok':200<=r.status<400,'http_status':r.status}))
 except Exception as e:print(json.dumps({'url':url,'ok':False,'error':type(e).__name__}))
'''.replace('PROXY', repr(CONTAINER_URL)).replace('TARGETS', repr(TARGETS))
    result = docker(['exec', info['Id'], 'python3', '-c', script])
    checks = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
    acceptance = {'time': time.time(), 'passed': len(checks) == len(TARGETS) and all(x['ok'] for x in checks),
                  'container': info['Id'], 'network_mode': 'host', 'proxy': CONTAINER_URL, 'checks': checks}
    path = root / 'deployment.json'
    value = json.loads(path.read_text()); value['container_acceptance'] = acceptance
    atomic_json(path, value)
    print(json.dumps(acceptance))
    if not acceptance['passed']: raise RuntimeError('container_proxy_acceptance_failed')


def accept_container(root):
    from offline_verifier.runner import docker
    from rsi_harness.clean_rsi import read_json
    source = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261002-resume-v1/manifest.json')
    entry = read_json(source)['stream'][39]
    image = json.loads(docker(['image', 'inspect', entry['image']]).stdout)[0]
    if image['Id'] != entry['image_id']: raise RuntimeError('acceptance_image_changed')
    container = docker(['run', '-d', '--network', 'host', '--entrypoint', 'sleep',
        '--label', 'rsi-purpose=network-acceptance-20261002', entry['image'], 'infinity']).stdout.strip()
    atomic_json(root / 'container-probe.json', {'container': container, 'image_id': entry['image_id'],
        'model_calls': 0, 'formal_scores': 0, 'purpose': 'disposable network probe only'})
    try:
        check_container(root, container)
    finally:
        docker(['stop', '--time', '2', container])
        docker(['rm', container])
        atomic_json(root / 'container-probe-cleanup.json', {'container': container, 'removed': True})


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--install', type=Path); p.add_argument('--check', action='store_true')
    p.add_argument('--deployment-root', type=Path); p.add_argument('--container')
    p.add_argument('--accept-container', action='store_true')
    a = p.parse_args()
    if a.install: install(a.install)
    elif a.check: print(json.dumps(health()))
    elif a.container: check_container(a.deployment_root, a.container)
    elif a.accept_container: accept_container(a.deployment_root)
    else: p.error('action required')
