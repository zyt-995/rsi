"""Explicit allowlist. Unknown or changed benchmark bootstraps fail closed."""
from pathlib import Path
import hashlib
import re

SCHEMA = 'terminal-offline-verifier-v1'
PREFIX = '/opt/rsi-offline-v1'
REQUIREMENTS = {
    'html': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5', 'selenium==4.35.0', 'beautifulsoup4==4.13.5'],
    'stats': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5', 'pandas==2.3.2', 'scipy==1.16.1'],
    'plain': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5'],
    'native': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5'],
    'cython': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5'],
}
GIT_URL = 'https://github.com/SPOCKnots/pyknotid.git'
ZIP_URL = 'https://github.com/SPOCKnots/pyknotid/archive/refs/tags/0.5.3.zip'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def test_hashes(task):
    return {str(p.relative_to(task / 'tests')): sha(p)
            for p in sorted((task / 'tests').rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


def profile(task):
    name = task.name
    if re.fullmatch(r'break-filter-js-from-html(?:-EVO-[1-4])?', name):
        value = 'html'
    elif re.fullmatch(r'bn-fit-modify(?:-EVO-[1-3])?', name):
        value = 'stats'
    elif name == 'chess-best-move':
        value = 'plain'
    elif re.fullmatch(r'chess-best-move-EVO-[1-4]', name):
        value = 'native'
    elif re.fullmatch(r'build-cython-ext(?:-EVO-[1-4])?', name):
        value = 'cython'
    else:
        raise ValueError('unsupported_offline_task:' + name)
    script = (task / 'tests/test.sh').read_text()
    pins = set(re.findall(r'[\w-]+==[\w.]+', script))
    expected = set() if value == 'native' else set(REQUIREMENTS[value])
    if pins != expected or '--ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA' not in script:
        raise ValueError('unrecognized_verifier_bootstrap:' + name)
    return value
