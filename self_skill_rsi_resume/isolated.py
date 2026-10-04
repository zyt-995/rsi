"""Offline grading, retaining only required loopback access to solver services."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import queue
import select
import socket
import sys
import threading


def relay(client, upstream):
    try:
        while True:
            readable, _, _ = select.select([client, upstream], [], [], 60)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is client else client).sendall(data)
    except OSError:
        pass
    finally:
        client.close()
        upstream.close()


def serve(listener, sockets, destination):
    while True:
        client, _ = listener.accept()
        try:
            upstream = sockets.get_nowait()
        except queue.Empty:
            client.close()
            continue
        try:
            upstream.settimeout(5)
            # Socket was created in the solver network namespace. Destination
            # is a fixed loopback service, never a caller-provided remote host.
            upstream.connect(('127.0.0.1', destination))
            upstream.settimeout(None)
        except OSError:
            client.close()
            upstream.close()
            continue
        threading.Thread(target=relay, args=(client, upstream), daemon=True).start()


def isolate():
    spec = importlib.util.spec_from_file_location('legacy_isolation', '/opt/rsi-offline-v1/legacy_isolated.py')
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    legacy.Path = Path  # Respect the trusted entrypoint's per-attempt log path.
    bridge = len(sys.argv) > 1 and sys.argv[1] == 'configure-git-webserver'
    banks = {}
    if bridge:
        fd = os.open('/proc/1/ns/net', os.O_RDONLY)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.setns(fd, 0x40000000) != 0:
                raise OSError(ctypes.get_errno(), 'cannot enter exact solver network namespace')
        finally:
            os.close(fd)
        for destination in (22, 8080):
            banks[destination] = queue.Queue()
            for _ in range(32):
                banks[destination].put(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    legacy.isolate()
    if bridge:
        for port, destination in ((2022, 22), (8080, 8080)):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', port))
            listener.listen(16)
            threading.Thread(target=serve, args=(listener, banks[destination], destination), daemon=True).start()
        os.environ['GIT_SSH_COMMAND'] = 'ssh -p 2022'
        path = Path('/logs/verifier/network.json')
        value = json.loads(path.read_text())
        value['solver_loopback_routes'] = {'127.0.0.1:2022': 'solver-loopback:22',
                                           '127.0.0.1:8080': 'solver-loopback:8080'}
        path.write_text(json.dumps(value))
