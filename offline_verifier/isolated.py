"""Trusted entrypoint: a fresh Linux network namespace with only loopback.

Launched via host nsenter into the target's user/mount/PID namespaces. This
does not relax the solver container's seccomp/capabilities or its network.
All setup privileges are dropped before running the actual grading process.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import sys

PREFIX = '/opt/rsi-offline-v1'


def isolate():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(0x40000000) != 0:  # CLONE_NEWNET
        raise OSError(ctypes.get_errno(), 'cannot isolate grading network')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    req = struct.pack('16sH14s', b'lo', 0x49, b'')  # UP | LOOPBACK | RUNNING
    fcntl.ioctl(sock, 0x8914, req)  # SIOCSIFFLAGS
    sock.close()
    if socket.if_nameindex() != [(1, 'lo')]:
        raise RuntimeError('grader_network_not_loopback_only')
    # Remove bounding + effective capabilities, prevent regaining them on exec.
    for cap in range(41):
        if libc.prctl(24, cap, 0, 0, 0) != 0 and ctypes.get_errno() != 22:
            raise OSError(ctypes.get_errno(), 'cannot drop capability')
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), 'cannot set no_new_privs')
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
    if libc.capset(ctypes.byref(Header(0x20080522, 0)), ctypes.byref((Data * 2)())) != 0:
        raise OSError(ctypes.get_errno(), 'cannot clear capabilities')
    probe = socket.socket()
    probe.settimeout(1)
    try:
        probe.connect(('1.1.1.1', 443))
    except OSError as exc:
        blocked = str(exc)
    else:
        raise RuntimeError('grader_has_external_network')
    finally:
        probe.close()
    # Verify loopback works; ChromeDriver and EVO-4 HTTP tests depend on it.
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1):
            client, _ = server.accept()
            client.close()
    Path('/logs/verifier/network.json').write_text(json.dumps({
        'network': 'isolated_loopback_only', 'interfaces': socket.if_nameindex(),
        'external_connect': blocked, 'loopback': 'passed', 'capabilities_dropped': True,
        'net_namespace': os.readlink('/proc/self/ns/net')}))


if __name__ == '__main__':
    # Do not inherit API credentials / host Python settings into the grader.
    original = Path('/proc/1/environ').read_bytes().decode().split('\0')
    env = dict(x.split('=', 1) for x in original if '=' in x)
    env = {k: v for k, v in env.items() if not any(s in k.lower() for s in ('proxy', 'token', 'key', 'secret'))
           and k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(PATH=env.get('PATH', '/usr/local/bin:/usr/bin:/bin'), HOME='/root',
               PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', UV_OFFLINE='1',
               UV_PYTHON_DOWNLOADS='never', NO_PROXY='*', no_proxy='*')
    os.environ.clear()
    os.environ.update(env)
    os.chdir('/app')
    Path('/logs/verifier').mkdir(parents=True, exist_ok=True)
    isolate()
    os.execv(PREFIX + '/python/bin/python3', [PREFIX + '/python/bin/python3', PREFIX + '/grade.py', *sys.argv[1:]])
