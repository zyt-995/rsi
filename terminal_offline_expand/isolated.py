"""Keep the existing network isolation, adding the variants' SSH loopback port."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading


def isolate():
    spec = importlib.util.spec_from_file_location('original_loopback_bridge', '/opt/rsi-offline-v1/original_bridge.py')
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    old.Path = Path
    task = sys.argv[1]
    variant = task.startswith('configure-git-webserver-EVO-')
    try:
        if variant: sys.argv[1] = 'configure-git-webserver'
        old.isolate()
    finally:
        sys.argv[1] = task
    if not variant: return
    # Variant tests set GIT_SSH_COMMAND themselves and use the standard port.
    # Bind in the isolated namespace only; forward to the already-audited bridge.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 22)); listener.listen(16)
    def serve():
        while True:
            client, _ = listener.accept()
            try: upstream = socket.create_connection(('127.0.0.1', 2022), timeout=5)
            except OSError: client.close(); continue
            upstream.settimeout(None)
            threading.Thread(target=old.relay, args=(client, upstream), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    os.environ.pop('GIT_SSH_COMMAND', None)
    path = Path('/logs/verifier/network.json')
    value = json.loads(path.read_text())
    value['solver_loopback_routes']['127.0.0.1:22'] = 'solver-loopback:22'
    path.write_text(json.dumps(value))
