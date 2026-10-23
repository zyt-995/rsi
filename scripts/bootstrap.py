"""Initialize portable directories and verify/apply the pinned Harbor patch. No model calls."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from _bootstrap import REPO
from rsi_portable.paths import WORKSPACE, RUNTIME


def link(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() != source.resolve():
            raise RuntimeError('Existing workspace link points elsewhere: ' + str(target))
    elif target.exists():
        raise RuntimeError('Refusing to replace existing workspace content: ' + str(target))
    else:
        target.symlink_to(source.resolve(), target_is_directory=source.is_dir())


def bootstrap(checkout_upstreams=False):
    manifest = json.loads((REPO / 'upstream/manifest.json').read_text())
    if checkout_upstreams:
        subprocess.run(['git', 'submodule', 'update', '--init', '--recursive'], cwd=REPO, check=True)
    for entry in manifest['repositories']:
        path = REPO / entry['path']
        actual = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != entry['commit']:
            raise RuntimeError('Upstream revision differs: ' + entry['name'])
    harbor = REPO / 'upstream/harbor'
    patch = REPO / manifest['harbor_patch']
    reverse = subprocess.run(['git', '-C', str(harbor), 'apply', '--reverse', '--check', str(patch)], capture_output=True)
    if reverse.returncode:
        subprocess.run(['git', '-C', str(harbor), 'apply', '--check', str(patch)], check=True)
        subprocess.run(['git', '-C', str(harbor), 'apply', str(patch)], check=True)
    locations = {
        'swe_chain_evo': 'src/benchmark/swe_chain_evo', 'formal_swe_chain': 'src/runner/formal_swe_chain',
        'within_chain': 'src/agents/within_chain', 'evoskill_swe': 'src/agents/evoskill_swe',
        'rsi_portable': 'src/runner/rsi_portable', 'prompts': 'prompts', 'scripts': 'scripts',
        'tests': 'tests', 'upstream': 'upstream', 'plans': 'configs/benchmark'}
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    for dest, source in locations.items():
        link(REPO / source, WORKSPACE / dest)
    link(REPO / 'src/methods', RUNTIME / 'methods')
    link(REPO / 'src/runner/rsi_portable', RUNTIME / 'rsi_portable')
    link(Path(sys.prefix), RUNTIME / '.venv-terminal')
    for folder in ('data/image-cache', 'data/image-catalog', 'prepared/tasks', 'preparation/tasks', 'runs', 'tmp'):
        (WORKSPACE / folder).mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / 'configs/benchmark/image-catalog.json', WORKSPACE / 'data/image-catalog/catalog.json')
    print(json.dumps({'workspace': str(WORKSPACE), 'interpreter': sys.executable, 'upstream_pins_checked': True, 'paid_calls': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstreams', action='store_true')
    bootstrap(parser.parse_args().upstreams)
