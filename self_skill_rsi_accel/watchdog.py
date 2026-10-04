"""Reuse the existing read-only monitor with the extended deployment seal."""
import argparse
from pathlib import Path
import urllib.request
from rsi_harness.clean_rsi import read_json
from self_skill_rsi_offline import watchdog
from . import campaign

original_check = watchdog.check


def check(root):
    value = original_check(root)
    manifest = campaign.verify(root)
    value['metrics'] = campaign.report(root, manifest)
    deployment = read_json(Path(manifest['inference_migration']['deployment']))
    value['inference_services'] = {}
    if value.get('terminal'):
        return value
    for service in deployment['services']:
        port = service['port']
        alive = campaign.base.legacy.alive(service)
        health = {'alive': alive, 'cards': service['cards'], 'healthy': False}
        if alive:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as response:
                    health['healthy'] = response.status == 200
            except OSError as exc:
                health['error'] = type(exc).__name__
        value['inference_services'][port] = health
        if not health['healthy']:
            value.setdefault('alert', f'inference_service_unhealthy:{port}; no_trial_retry')
    return value


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    args = p.parse_args()
    watchdog.c = campaign
    watchdog.check = check
    watchdog.run(args.root)
