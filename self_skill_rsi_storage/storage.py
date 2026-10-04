"""Admission against actual writable filesystems, with a host emergency reserve."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path('/data2/ytzhang/rsi-harness-project-runtime-20261003')
DOCKER = Path('/data2/ytzhang/rsi-harness-docker-overlay2')
POLICY = {'data2_min_free_gib': 100, 'host_min_free_gib': 1,
          'writes': 'run artifacts, temporary files and caches on data2',
          'no_score_or_algorithm_change': True}


def environment():
    paths = {'TMPDIR': ROOT / 'tmp', 'TMP': ROOT / 'tmp', 'TEMP': ROOT / 'tmp',
             'XDG_CACHE_HOME': ROOT / 'cache', 'PIP_CACHE_DIR': ROOT / 'cache/pip',
             'UV_CACHE_DIR': ROOT / 'cache/uv', 'TORCHINDUCTOR_CACHE_DIR': ROOT / 'cache/torchinductor'}
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return {**{k: str(v) for k, v in paths.items()}, 'PYTHONDONTWRITEBYTECODE': '1'}


def activate():
    os.environ.update(environment())
    tempfile.tempdir = str(ROOT / 'tmp')


def disk_status(run_root):
    try:
        result = subprocess.run(['docker', 'info', '--format', '{{.DockerRootDir}}'],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode or result.stdout.strip() != str(DOCKER):
            return {'ok': False, 'error': 'unexpected_docker_data_root'}
        paths = [DOCKER, Path(run_root), ROOT / 'tmp', ROOT / 'cache']
        device = Path('/data2').stat().st_dev
        if any(not p.resolve().is_relative_to('/data2') or p.stat().st_dev != device for p in paths):
            return {'ok': False, 'error': 'writable_path_not_on_data2'}
        free = {str(p): shutil.disk_usage(p).free / 1024**3 for p in paths}
        host = shutil.disk_usage('/').free / 1024**3
        return {'ok': min(free.values()) > POLICY['data2_min_free_gib']
                and host > POLICY['host_min_free_gib'],
                'data2_free_gib': {k: round(v, 2) for k, v in free.items()},
                'host_free_gib': round(host, 2), 'policy': POLICY}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'ok': False, 'error': type(exc).__name__ + ':' + str(exc)}
