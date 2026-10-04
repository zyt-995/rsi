"""Versioned single-chain recovery using directory COPY verification."""
import argparse
import os
from pathlib import Path

from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_storage import storage
from . import build
from .infrastructure_v2 import resolve_entry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--chain', required=True)
    args = parser.parse_args()
    os.environ.update(runner_environment(8041))
    storage.activate()
    print('repair_stage=preparing_chain', flush=True)
    build.resolve_entry = resolve_entry
    result = build.prepare(args.root.resolve(), args.chain)
    print({'chain': args.chain, 'passed': result['passed'], 'model_calls': result['model_calls']}, flush=True)


if __name__ == '__main__':
    main()
