"""Repair missing public Go packages in PRIVATE graders, never solver images.

Only a module proxy URL emitted by an actual offline compile failure triggers
this resolver. An empty host workspace runs `go mod download` (no package code
execution), with the official proxy and checksum database and a fixed local Go
toolchain. No benchmark source/test/patch or credential is supplied to it.
The resulting dependency cache is copied ONLY into the current private grader;
no solver, proposer or skill workspace can access the reference's dependencies.
"""
import fcntl
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from urllib.parse import unquote
from .common import atomic, docker, sha

from rsi_portable.paths import WORKSPACE
CACHE = WORKSPACE / "private-go-mod-cache"
import shutil
TOOL = Path(os.environ.get("RSI_GO_BINARY") or shutil.which("go") or "go")

def missing_modules(log):
    if not any(s in log for s in ('network is unreachable', 'i/o timeout', 'no such host', 'connection refused')):
        return []
    result = set()
    for path, version in re.findall(r'https://proxy\.golang\.org/([A-Za-z0-9.!_~/+-]+)/@v/([A-Za-z0-9.+_-]+)\.(?:zip|mod|info)', log):
        path, version = unquote(path), unquote(version)
        if '..' in path.split('/') or not re.fullmatch(r'[A-Za-z0-9.!_~/-]+', path):
            raise ValueError('unsafe module proxy path')
        path = re.sub(r'!([a-z])', lambda m: m[1].upper(), path)
        if '.' not in path.split('/')[0] or not version.startswith('v'):
            raise ValueError('invalid public Go module identity')
        result.add(path + '@' + version)
    return sorted(result)

def hydrate(container, modules, output, deadline):
    CACHE.mkdir(parents=True, exist_ok=True)
    env = {k:v for k,v in os.environ.items() if k in (
        'PATH','HOME','LANG','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','SSL_CERT_FILE','SSL_CERT_DIR')}
    env.update(GOTOOLCHAIN='local', GOENV='off', GOWORK='off', GO111MODULE='on',
        GOPROXY='https://proxy.golang.org', GOSUMDB='sum.golang.org', GOPRIVATE='',
        GONOPROXY='', GONOSUMDB='', GOMODCACHE=str(CACHE))
    rows=[]
    with CACHE.with_suffix('.resolver.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for module in modules:
            remaining=deadline-time.monotonic()
            if remaining<=1:raise TimeoutError('original verifier deadline expired during public dependency resolution')
            proc=subprocess.run([str(TOOL),'mod','download','-json',module], cwd='/tmp',env=env,
                capture_output=True,text=True,timeout=min(90,remaining))
            if proc.returncode:
                raise RuntimeError('public Go dependency download failed: '+module+'; '+proc.stderr[-500:])
            import json
            row=json.loads(proc.stdout)
            if row.get('Error') or not row.get('Sum') or not row.get('GoModSum'):
                raise RuntimeError('public dependency lacks authoritative Go checksums: '+module)
            rows.append({k:row.get(k) for k in ['Path','Version','Sum','GoModSum']})
        cache_dir=docker('exec','--user','0',container,'sh','-c','cd /testbed && go env GOMODCACHE',timeout=20).stdout.decode().strip()
        if not cache_dir.startswith('/') or '..' in Path(cache_dir).parts:raise ValueError('invalid grader cache path')
        docker('exec','--user','0',container,'mkdir','-p',cache_dir,timeout=20)
        stream=io.BytesIO()
        def root_owned(member):
            member.uid=member.gid=0;member.uname=member.gname='';return member
        with tarfile.open(fileobj=stream,mode='w') as tar:
            for p in sorted(CACHE.rglob('*')):
                if p.is_symlink():raise RuntimeError('unexpected symlink in public Go download cache')
                tar.add(p,arcname=str(p.relative_to(CACHE)),recursive=False,filter=root_owned)
        docker('exec','--interactive','--user','0',container,'tar','--no-same-owner','-xf','-','-C',cache_dir,
               input=stream.getvalue(),timeout=max(1,min(60,deadline-time.monotonic())))
    receipt={'time':time.time(),'modules':rows,'toolchain_sha256':sha(TOOL),
        'resolver_sha256':sha(Path(__file__)),'proxy':'https://proxy.golang.org','sumdb':'sum.golang.org',
        'package_code_executed':False,'benchmark_or_prediction_given_to_downloader':False,
        'private_grader_only':True,'solver_or_proposer_cache_modified':False,
        'test_commands_changed':False,'grader_network':'none','original_deadline_extended':False}
    atomic(Path(output)/('offline-go-dependencies-'+str(time.time_ns())+'.json'),receipt)
    return receipt
