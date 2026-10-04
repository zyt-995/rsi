#!/usr/bin/env python3
"""Register one free TP4 service and continue the unchanged skill method."""
import argparse
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json,file_hash
from self_skill_rsi_continue import campaign,guardian


def main(root,inference,offline):
    deployment = inference/'deployment.json'
    if deployment.exists():
        raise RuntimeError('deployment_already_registered; inspect_before_relaunch')
    smoke = inference/'smoke-8041.json'
    if read_json(smoke).get('passed') is not True:
        raise RuntimeError('inference_smoke_not_passed')
    audit = Path('/data2/ytzhang/rsi-harness-runs/inference-acceleration-20260930')
    launch = sorted(audit.glob('launch-8041-*.json'),key=lambda p:p.stat().st_mtime)[-1]
    service = read_json(launch)
    if service['cards'] != '4,5,6,7' or not campaign.base.legacy.alive(service):
        raise RuntimeError('registered_model_service_not_live')
    argv = Path(f"/proc/{service['pid']}/cmdline").read_bytes().decode().split('\0')
    if 'vllm.entrypoints.openai.api_server' not in argv or argv[argv.index('--port')+1] != '8041':
        raise RuntimeError('inference_process_identity_mismatch')
    paths = [launch,smoke,PROJECT/'scripts/run-qwen38-accelerated-service.sh',
        PROJECT/'scripts/rsi-inference-service-control.py',
        Path('/data3/ytzhang/models/Qwen3.8-27B/generation_config.json')]
    atomic_json(deployment, {'time':time.time(),'ports':[8041],'gpu_groups':[[4,5,6,7]],
        'services':[service],'functional_checks_passed':True,'model':'Qwen/Qwen3.8-27B',
        'evidence_hashes':{str(p):file_hash(p) for p in paths},
        'reason':'GPU 0 belongs to another user; do not preempt; sequential candidate checks on unchanged TP4 inference'})
    campaign.prepare(root.resolve(),deployment,offline/'grader-manifest.json',offline/'acceptance.json')
    guardian.install(root.resolve())


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--inference',type=Path,required=True)
    p.add_argument('--offline',type=Path,required=True)
    main(**vars(p.parse_args()))
