"""Self-contained launcher environment, pinned to the existing overlay2 daemon."""
import json
import os
from pathlib import Path
import subprocess

from self_skill_rsi import campaign as base


def runner_environment(port):
    env = base.core.runner_environment(port)
    project = base.core.PROJECT
    binary = Path('/home/ytzhang/budget-rl/.deps/docker-static/docker')
    socket = Path('/tmp/rsi-harness-docker-overlay2/docker.sock')
    if not (project / '.docker/active-overlay2').is_file() or not binary.is_file() or not socket.is_socket():
        raise RuntimeError('registered_overlay2_environment_unavailable')
    env['PATH'] = str(binary.parent) + os.pathsep + env.get('PATH', '')
    env['DOCKER_HOST'] = 'unix://' + str(socket)
    env['DOCKER_CONFIG'] = str(project / '.docker')
    info = json.loads(subprocess.check_output([str(binary), 'info', '--format', '{{json .}}'],
                                              env=env, text=True, timeout=15))
    if info.get('Driver') != 'overlay2' or info.get('DockerRootDir') != '/data2/ytzhang/rsi-harness-docker-overlay2':
        raise RuntimeError('wrong_docker_daemon_or_storage_driver')
    return env
