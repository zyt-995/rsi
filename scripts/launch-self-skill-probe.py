#!/usr/bin/env python3
"""Register already-checked exact inference services and start the held-in probe."""
import argparse
import json
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_probe import campaign, guardian


def main(root, inference):
    if (inference / 'deployment.json').exists():
        raise RuntimeError('deployment_record_exists; do_not_blindly_relaunch')
    audit = Path('/data2/ytzhang/rsi-harness-runs/inference-acceleration-20260930')
    services, hashes = [], {}
    for port, cards in [(8040, '0,1,2,3'), (8041, '4,5,6,7')]:
        smoke = inference / f'smoke-{port}.json'
        if read_json(smoke).get('passed') is not True:
            raise RuntimeError('model_smoke_not_passed')
        hashes[str(smoke)] = file_hash(smoke)
        launch = sorted(audit.glob(f'launch-{port}-*.json'), key=lambda p: p.stat().st_mtime)[-1]
        service = read_json(launch)
        if service['cards'] != cards or not campaign.base.legacy.alive(service):
            raise RuntimeError('service_identity_changed')
        argv = Path(f"/proc/{service['pid']}/cmdline").read_bytes().decode().split('\0')
        if ('vllm.entrypoints.openai.api_server' not in argv
                or argv[argv.index('--port') + 1] != str(port)):
            raise RuntimeError('not_registered_vllm_process')
        services.append(service)
        hashes[str(launch)] = file_hash(launch)
    for name in ['run-qwen38-accelerated-service.sh', 'rsi-inference-service-control.py']:
        p = PROJECT / 'scripts' / name
        hashes[str(p)] = file_hash(p)
    config = Path('/data3/ytzhang/models/Qwen3.8-27B/generation_config.json')
    if config.exists():
        hashes[str(config)] = file_hash(config)
    deployment = inference / 'deployment.json'
    atomic_json(deployment, {'time': time.time(), 'ports': [8040, 8041],
        'gpu_groups': [[0, 1, 2, 3], [4, 5, 6, 7]], 'services': services,
        'functional_checks_passed': True, 'model': 'Qwen/Qwen3.8-27B', 'evidence_hashes': hashes})
    source = Path('/data2/ytzhang/rsi-harness-runs/self-skill-rsi-20261001-resilient-v1')
    campaign.prepare(root.resolve(), source, deployment)
    guardian.install(root.resolve())
    print(json.dumps({'root': str(root), 'solves': 6, 'unique_tasks': 1, 'ports': [8040, 8041]}))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--inference', type=Path, required=True)
    main(**vars(p.parse_args()))
