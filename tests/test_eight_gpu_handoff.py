"""Exercise orchestration order without touching real services or tasks."""
import importlib.util
from pathlib import Path
import socket

import pytest

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json


def test_handoff_waits_then_switches_then_launches(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('handoff_test', project / 'scripts/handoff-eight-gpu-self-skill.py')
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    audit, source, target = tmp_path / 'audit', tmp_path / 'source', tmp_path / 'target'
    audit.mkdir()
    atomic_json(source / 'PAUSE', {'reason': 'test'})
    atomic_json(audit / 'tp2-eager.json', {'median_decode_tokens_per_second': 10})
    atomic_json(audit / 'tp4-decode-graph.json', {'median_decode_tokens_per_second': 40})
    atomic_json(audit / 'shadow-functional.json', {'passed': True})
    monkeypatch.setattr(h, 'AUDIT', audit)
    events = []
    monkeypatch.setattr(h, 'wait_boundary', lambda root: events.append('boundary_stopped'))
    monkeypatch.setattr(h.c.offline, 'verify', lambda root: {'model': 'Qwen/Qwen3.8-27B'})
    monkeypatch.setattr(h.c, 'boundary', lambda *a: [({'uid': 's000005'}, {})])
    monkeypatch.setattr(h.control, 'stop', lambda args: events.append(('stop', args.port)))
    monkeypatch.setattr(h.subprocess, 'check_output', lambda *a, **kw: '\n'.join(f'{i}, 0' for i in range(10)))
    def start(args):
        events.append(('launch_service', args.port))
        return {'port': args.port, 'cards': args.cards, 'pid': 1, 'identity': 'test', 'log': 'test'}
    monkeypatch.setattr(h.control, 'launch', start)
    monkeypatch.setattr(h, 'ready', lambda service: events.append(('healthy', service['port'])))
    def smoke(port):
        path = audit / f'final-{port}-functional.json'
        atomic_json(path, {'passed': True})
        events.append(('smoke', port))
        return path
    def bench(port):
        path = audit / f'final-{port}-speed.json'
        atomic_json(path, {'median_decode_tokens_per_second': 35})
        events.append(('bench', port))
        return path
    monkeypatch.setattr(h, 'smoke', smoke)
    monkeypatch.setattr(h, 'bench', bench)
    monkeypatch.setattr(h.c, 'prepare', lambda *a: events.append('prepared'))
    monkeypatch.setattr(h.subprocess, 'run', lambda *a, **kw: events.append('launch_campaign'))
    h.execute(source, target)
    assert events[0] == 'boundary_stopped'
    assert events[1:4] == [('stop', 8040), ('stop', 8041), ('stop', 8050)]
    assert events[4:6] == [('launch_service', 8040), ('launch_service', 8041)]
    assert events[-2:] == ['prepared', 'launch_campaign']
    deployed = read_json(audit / 'deployment.json')
    assert deployed['gpu_groups'] == [[0,1,2,3],[4,5,6,7]]
    assert deployed['formal_task_replays'] == 0
    assert deployed['median_decode_speedup'] == 3.5
    assert read_json(audit / 'handoff-status.json')['state'] == 'accelerated_campaign_running'


def test_port_probe_rejects_active_listener_and_allows_time_wait():
    project = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('control_test', project / 'scripts/rsi-inference-service-control.py')
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        port = server.getsockname()[1]
        server.listen(1)
        with pytest.raises(OSError):
            control.available_port(port)
        with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
            peer, _ = server.accept()
            peer.close()  # Server initiates closure and retains TIME_WAIT.
            assert client.recv(1) == b''
    control.available_port(port)
