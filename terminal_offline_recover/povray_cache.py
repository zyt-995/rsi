"""Byte-pinned replacement transport for unavailable public POV-Ray archives.

Hashes and identity snapshot URLs come from terminal-bench-2 PR 77. No code or
reference solutions are added to the task beyond the archives it requests.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import functools
import http.server
import os
from pathlib import Path
import subprocess
import time

from rsi_harness.terminal_infra import atomic_json, file_hash

ORIGIN='http://www.povray.org/ftp/pub/povray/Old-Versions/Official-2.2'
ARCHIVES={
 'POVDOC.TAR.Z':('20180330013814','e70e44d1fe8835c4dff7c7a55bd6629b15e6a15b2ab7f2f49ee9e2dc016cc470'),
 'POVSCN.TAR.Z':('20180330013815','4272e2d4724d8dfd916d68827194577221d17b733d99e84e7040f3a9f7eb92a7'),
 'POVSRC.TAR.Z':('20180330013813','4d8a7073fadaca82827f1354428393cd13e4d3f71a5a3149fd7d6fffd77293d4'),
}


def fetch(root):
    folder=root/'external-resources/povray-2.2';folder.mkdir(parents=True,exist_ok=True)
    def one(item):
        name,(stamp,expected)=item;target=folder/name
        url='https://web.archive.org/web/'+stamp+'id_/'+ORIGIN+'/'+name
        if not target.exists():
            part=folder/(name+'.part')
            subprocess.run(['curl','--fail','--location','--silent','--show-error','--retry','2',
                '--retry-all-errors','--connect-timeout','10','--max-time','90','--proxy','http://127.0.0.1:17898',
                '--output',str(part),url],check=True,timeout=290)
            if file_hash(part)!=expected:raise RuntimeError('upstream_archive_sha256_mismatch:'+name)
            part.replace(target)
        if file_hash(target)!=expected:raise RuntimeError('archive_cache_corrupt:'+name)
        return {'name':name,'sha256':expected,'source':url,'size':target.stat().st_size}
    with ThreadPoolExecutor(max_workers=3) as pool:rows=list(pool.map(one,ARCHIVES.items()))
    atomic_json(folder/'receipt.json',{'archives':rows,'time':time.time(),'upstream_fix':'https://github.com/harbor-framework/terminal-bench-2/pull/77',
        'source_contents_modified':False,'official_assertions_modified':False,'model_calls':0})
    return folder


def serve(root):
    folder=fetch(root)
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.lstrip('/') not in ARCHIVES:self.send_error(404);return
            super().do_GET()
        def do_HEAD(self):
            if self.path.lstrip('/') not in ARCHIVES:self.send_error(404);return
            super().do_HEAD()
    server=http.server.ThreadingHTTPServer(('127.0.0.1',18781),functools.partial(Handler,directory=str(folder)))
    server.serve_forever()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path);p.add_argument('--serve',action='store_true')
    a=p.parse_args();serve(a.root) if a.serve else print(fetch(a.root))
