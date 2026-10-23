"""One configurable workspace; no source-host paths are required."""
import os
from pathlib import Path
import shutil

REPO = Path(__file__).resolve().parents[3]
WORKSPACE = Path(os.environ.get('RSI_WORKSPACE', str(REPO / '.work'))).expanduser().resolve()
RUNTIME = WORKSPACE / 'runtime-project'


def docker_env():
    env = dict(os.environ)
    binary = os.environ.get('RSI_DOCKER_BINARY') or shutil.which('docker')
    if not binary:
        raise RuntimeError('Docker CLI missing; run scripts/doctor.py for setup instructions.')
    env['PATH'] = str(Path(binary).parent) + os.pathsep + env.get('PATH', '')
    return env


def runner_environment(port):
    env = docker_env()
    env['PYTHONPATH'] = os.pathsep.join([str(WORKSPACE), str(RUNTIME), env.get('PYTHONPATH', '')])
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
    return env
