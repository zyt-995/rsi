"""Prepare selected chain images, oracle/noop grading and five-method config without model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

from _bootstrap import REPO
from rsi_portable.paths import WORKSPACE as ROOT, RUNTIME

CHAINS = {'click': 'pallets/click', 'coredns': 'coredns/coredns', 'nats': 'nats-io/nats-server',
          'fiber': 'gofiber/fiber', 'loki': 'grafana/loki', 'istio': 'istio/istio', 'prometheus': 'prometheus/prometheus'}


def selected(chain):
    entries = json.loads((REPO / 'configs/benchmark/seven-chains.json').read_text())['entries']
    rows = sorted([r for r in entries if r['repo'] == CHAINS[chain]], key=lambda r: r['version_chain_index'])
    assert len(rows) == 15 and [r['version_chain_index'] for r in rows] == list(range(15))
    return rows


def tokenizer(model):
    from swe_chain_evo.common import sha
    target = ROOT / 'data/tokenizers' / (model + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    if model == 'gpt':
        shutil.copy2(REPO / 'configs/gpt/tokenizer.json', target)
    else:
        expected = (REPO / 'configs/deepseek/tokenizer.sha256').read_text().strip()
        if not target.exists():
            url = os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/') + '/deepseek-ai/DeepSeek-V4.1-Flash/resolve/main/tokenizer.json'
            with urllib.request.urlopen(url, timeout=60) as response:
                target.write_bytes(response.read())
        if sha(target) != expected:
            raise RuntimeError('DeepSeek tokenizer differs from recorded SHA256; use the original pinned tokenizer or record a new experiment revision.')


def prepare_entry(entry):
    from swe_chain_evo.common import atomic, read, sha, instance
    from swe_chain_evo.assets import ensure_image
    from swe_chain_evo.task_builder import prepare_task
    from swe_chain_evo.grading import grade
    from formal_swe_chain.grading import freeze_map, normalize
    from harbor.models.task.task import Task
    if sha(ROOT / 'data/SWE-Chain-Evo' / entry['instance_path']) != entry['instance_sha256']:
        raise RuntimeError('Benchmark metadata changed: ' + entry['instance_id'])
    receipt = ROOT / 'preparation/tasks' / (entry['instance_id'] + '.json')
    if receipt.exists():
        prior = read(receipt)
        if prior.get('passed') and prior['grader_sha256'] == sha(ROOT / 'swe_chain_evo/grading.py'):
            from swe_chain_evo.common import docker
            e = prior['entry']
            actual = docker('image', 'inspect', e['image_id'], '--format', '{{.Id}}', check=False)
            if actual.returncode == 0 and actual.stdout.decode().strip() == e['image_id'] and Task(Path(e['path'])).checksum == e['task_checksum']:
                return e
    ensure_image(entry)
    prepared = prepare_task(entry)
    item = instance(entry)
    folders = {}
    for kind in ('oracle', 'noop'):
        out = ROOT / 'runs/grader-preflight' / entry['instance_id'] / kind
        out.mkdir(parents=True, exist_ok=True)
        patch = out / 'prediction.patch'
        patch.write_text(item['raw']['patch'] if kind == 'oracle' else '')
        grade(entry, patch, out)
        folders[kind] = out
    freeze_map(entry, folders['oracle'])
    for kind, expected in [('oracle', 1), ('noop', 0)]:
        if normalize(entry, folders[kind])['reward'] != expected:
            raise RuntimeError('Grader calibration failed: ' + str(folders[kind]))
    prepared.update(task_index=entry['version_chain_index'], cohort_task_index=entry['version_chain_index'],
                    task_checksum=Task(Path(prepared['path'])).checksum)
    atomic(receipt, {'passed': True, 'model_calls': 0, 'entry': prepared, 'oracle_reward': 1, 'noop_reward': 0,
        'grader_sha256': sha(ROOT / 'swe_chain_evo/grading.py'),
        'test_id_map_sha256': sha(ROOT / 'runs/grader-preflight' / entry['instance_id'] / 'test-id-map.json')})
    return prepared


def probe(stream):
    from swe_chain_evo.common import atomic, read, sha, docker_env
    from formal_swe_chain.grading import normalize
    folder = ROOT / 'preparation/chains' / stream[0]['chain_id']
    entryfile = folder / 'probe-entry.json'
    atomic(entryfile, stream[0])
    env = docker_env()
    for key in list(env):
        if any(word in key.upper() for word in ('API_KEY', 'AUTH_TOKEN')):
            env.pop(key, None)
    env.update(PYTHONPATH=str(ROOT) + os.pathsep + str(RUNTIME), PYTHONDONTWRITEBYTECODE='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    results = list((folder / 'probe/trials').glob('*/result.json'))
    if not results:
        cmd = [str(RUNTIME / '.venv-terminal/bin/harbor'), 'trials', 'start', '--path', stream[0]['path'],
            '--trials-dir', str(folder / 'probe/trials'), '--env', 'swe_chain_evo.environment:OfflineDocker',
            '--agent-import-path', 'swe_chain_evo.preflight:NoopAgent', '--verifier', 'swe_chain_evo.verifier:SWEVerifier',
            '--verifier-kwarg', 'entry_file=' + str(entryfile), '--verifier-kwarg', 'entry_sha256=' + sha(entryfile)]
        with (folder / 'probe.log').open('ab') as log:
            subprocess.run(cmd, env=env, stdout=log, stderr=log, check=True, timeout=900)
        results = list((folder / 'probe/trials').glob('*/result.json'))
    if len(results) != 1 or read(results[0]).get('exception_info'):
        raise RuntimeError('Harbor zero-model probe failed; inspect ' + str(folder))
    result = normalize(stream[0], results[0].parent / 'verifier/grading')
    if result['reward'] != 0:
        raise RuntimeError('Noop task unexpectedly passed')
    atomic(folder / 'zero-model-probe.json', {'passed': True, 'model_calls': 0, 'result_sha256': sha(results[0])})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--chain', choices=CHAINS, required=True)
    p.add_argument('--model', choices=['deepseek', 'gpt'], default='deepseek')
    p.add_argument('--budget-usd', default='50.00')
    p.add_argument('--session', type=Path)
    p.add_argument('--port', type=int, default=18300)
    p.add_argument('--plan', action='store_true', help='Print selected image archive size; do not download or run Docker')
    p.add_argument('--environment-only', action='store_true')
    p.add_argument('--indices', help='Prepare a subset for environment troubleshooting; cannot launch a formal chain')
    args = p.parse_args()
    rows = selected(args.chain)
    if args.indices:
        rows = [rows[i] for i in map(int, args.indices.split(','))]
    if args.plan:
        cat = json.loads((REPO / 'configs/benchmark/image-catalog.json').read_text())
        names = {'images/' + e['image'].split(':')[0].removeprefix('sweb.eval.x86_64.') + '.tar' for e in rows}
        size = sum(e['size'] for e in cat['files'] if e['path'] in names)
        print(json.dumps({'chain': args.chain, 'tasks': len(rows), 'archives_gib': size / 1024**3,
                          'disk_note': 'Also allow space for Docker unpacking, solver images and concurrent workspaces.', 'paid_calls': 0}, indent=2))
        return
    from bootstrap import bootstrap
    bootstrap()
    from swe_chain_evo.common import atomic
    stream = [prepare_entry(e) for e in rows]
    if args.indices:
        print(json.dumps({'prepared_subset': len(stream), 'formal_chain_ready': False, 'paid_calls': 0}))
        return
    folder = ROOT / 'preparation/chains' / stream[0]['chain_id']
    atomic(folder / 'task-stream.json', stream)
    probe(stream)
    if args.environment_only:
        return
    tokenizer(args.model)
    from rsi_portable import configure
    configure.MODEL, configure.BUDGET = args.model, args.budget_usd
    session = (args.session or ROOT / 'sessions' / (args.model + '-' + args.chain)).resolve()
    configure.configure(args.chain, stream[0]['chain_id'], session, args.port)
    configure.build()
    print(json.dumps(configure.verify(), indent=2))


if __name__ == '__main__':
    main()
