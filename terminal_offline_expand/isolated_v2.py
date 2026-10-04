"""Variant loopback services bound before dropping all grader capabilities."""
import ctypes
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import struct
import sys
import threading


def isolate():
    spec = importlib.util.spec_from_file_location('original_loopback_bridge', '/opt/rsi-offline-v1/original_bridge.py')
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old); old.Path = Path
    if not sys.argv[1].startswith('configure-git-webserver-EVO-'):
        return old.isolate()
    libc = ctypes.CDLL(None, use_errno=True)
    fd = os.open('/proc/1/ns/net', os.O_RDONLY)
    try:
        if libc.setns(fd, 0x40000000) != 0: raise OSError(ctypes.get_errno(), 'solver_network_namespace')
    finally: os.close(fd)
    banks = {}
    for port in (22, 8080):
        banks[port] = queue.Queue()
        for _ in range(128): banks[port].put(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    if libc.unshare(0x40000000) != 0: raise OSError(ctypes.get_errno(), 'grading_network_namespace')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        fcntl.ioctl(sock, 0x8914, struct.pack('16sH14s', b'lo', 0x49, b''))
    if socket.if_nameindex() != [(1, 'lo')]: raise RuntimeError('non_loopback_interface')
    listeners = {}
    for port in (22, 8080):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port)); listener.listen(16); listeners[port] = listener
    for cap in range(41):
        if libc.prctl(24, cap, 0, 0, 0) != 0 and ctypes.get_errno() != 22:
            raise OSError(ctypes.get_errno(), 'drop_capability')
    if libc.prctl(38, 1, 0, 0, 0) != 0: raise OSError(ctypes.get_errno(), 'no_new_privs')
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
    if libc.capset(ctypes.byref(Header(0x20080522, 0)), ctypes.byref((Data * 2)())) != 0:
        raise OSError(ctypes.get_errno(), 'clear_capabilities')
    with socket.socket() as probe:
        probe.settimeout(1)
        try: probe.connect(('1.1.1.1', 443))
        except OSError as exc: blocked = str(exc)
        else: raise RuntimeError('grader_has_external_network')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); probe.listen(1)
        with socket.create_connection(probe.getsockname(), timeout=1):
            client, _ = probe.accept(); client.close()
    for port, listener in listeners.items():
        threading.Thread(target=old.serve, args=(listener, banks[port], port), daemon=True).start()
    Path('/logs/verifier/network.json').write_text(json.dumps({'network': 'isolated_loopback_only',
        'interfaces': socket.if_nameindex(), 'external_connect': blocked, 'loopback': 'passed',
        'capabilities_dropped': True, 'net_namespace': os.readlink('/proc/self/ns/net'),
        'solver_loopback_routes': {f'127.0.0.1:{port}': f'solver-loopback:{port}' for port in listeners}}))
