"""Prepare and smoke all remaining original images without model rollouts."""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
from urllib.parse import unquote,urlparse

from offline_verifier import build, profiles, runner
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_resilient import grading
from self_skill_rsi_dual import campaign as prior
from .bootstrap import translate, OCAML_URL, OCAML_TAG

SOURCE = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261001-dual-v1')
REUSE = Path('/data2/ytzhang/rsi-harness-static-data/offline-verifier-v4')


def apt_prepare(container, arguments, log):
    log.parent.mkdir(parents=True,exist_ok=True)
    command=['docker','exec',container,'apt-get','-o','APT::Sandbox::User=root',
             '-o','Acquire::http::Timeout=20','-o','Acquire::https::Timeout=20',
             '-o','Acquire::Retries=0',*arguments]
    with log.open('ab',buffering=0) as stream:
        subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=300)


def cache_apt_packages(container, packages, destination):
    """Resolve against the image's signed apt index, download on the faster host path."""
    result=runner.docker(['exec',container,'apt-get','-o','APT::Sandbox::User=root',
        '--print-uris','--download-only','--yes','install',*packages],timeout=30)
    destination.mkdir(parents=True,exist_ok=True)
    atomic_json(destination/'apt-resolution.json',{'stdout':result.stdout,'stderr':result.stderr})
    rows=[]
    for line in result.stdout.splitlines():
        if line.startswith("'"):
            match=re.fullmatch(r"'([^']+)' (\S+) (\d+)\s*(.*)",line)
            if not match:raise RuntimeError('unparsed_apt_archive_uri:'+line)
            url,name,size,checksum=match.groups()
            if Path(name).name!=name or not name.endswith('.deb'):
                raise RuntimeError('unsafe_apt_archive_name')
            checked=re.fullmatch(r'(SHA256|SHA512|MD5Sum):([a-fA-F0-9]+)',checksum)
            if checked:
                kind,digest=checked.groups();algorithm={'SHA256':'sha256','SHA512':'sha512','MD5Sum':'md5'}[kind]
            else:
                # New security indexes may omit MD5; apt --print-uris can then
                # print an empty checksum. Obtain SHA256 from the signed index.
                package,version,_=unquote(name).split('_',2)
                metadata=runner.docker(['exec',container,'apt-cache','show',package+'='+version],timeout=20).stdout
                candidates=[dict(x.split(': ',1) for x in stanza.splitlines() if ': ' in x and not x.startswith(' '))
                            for stanza in metadata.split('\n\n')]
                matches=[x for x in candidates if unquote(Path(x.get('Filename','')).name)==unquote(Path(urlparse(url).path).name)
                         and x.get('Size')==size and x.get('SHA256')]
                if not matches:raise RuntimeError('no_signed_archive_checksum:'+name)
                algorithm,digest='sha256',matches[0]['SHA256']
            rows.append({'url':url,'name':name,'size':int(size),'algorithm':algorithm,'digest':digest})
    if not rows:raise RuntimeError('apt_did_not_return_hash_checked_archives')
    destination.mkdir(parents=True,exist_ok=True)
    def fetch(row):
        path=destination/row['name']
        def valid(p):
            return p.is_file() and p.stat().st_size==row['size'] and hashlib.new(row['algorithm'],p.read_bytes()).hexdigest()==row['digest']
        if valid(path):return
        temporary=path.with_name(path.name+'.part')
        urls=[row['url'],row['url'].replace('http://','https://',1)]
        if row['url'].startswith('http://deb.debian.org/debian-security/'):
            urls.insert(0,row['url'].replace('http://deb.debian.org/debian-security/',
                'https://mirrors.tuna.tsinghua.edu.cn/debian-security/',1))
        for url in dict.fromkeys(urls):
            result=subprocess.run(['curl','--fail','--location','--silent','--show-error','--connect-timeout','10',
                '--max-time','35','--proxy','http://127.0.0.1:17897','--output',str(temporary),url],timeout=45)
            if result.returncode==0 and valid(temporary):
                row['download_url']=url
                break
        else:raise RuntimeError('apt_archive_unavailable_or_hash_mismatch:'+row['name'])
        temporary.replace(path)
    with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(fetch,rows))
    atomic_json(destination/'archive-provenance.json',{'signed_index_resolved':True,'all_uris_accounted':True,'packages':packages,'archives':rows})


def prepare(output, resume=False, reuse_preparation=None):
    if output.exists() and (not resume or (output/'prepared.json').exists()):
        raise RuntimeError('new_offline_bundle_directory_required')
    output.mkdir(parents=True,exist_ok=True)
    if reuse_preparation is not None:
        for name in ('bundle','system-dependencies'):
            shutil.copytree(reuse_preparation/name,output/name,symlinks=True)
    m = prior.verify(SOURCE)
    old = grading.load_manifest(m['resilience_migration']['grader'], m['resilience_migration']['grader_sha256'])
    entries = [e for e in m['stream'] if e['task'] not in old['tasks']]
    plans = {e['task']: translate((Path(e['path'])/'tests/test.sh').read_text()) for e in entries}
    for e in entries:
        if any('curl' in line for line in plans[e['task']]['bootstrap_replacements']):
            for path in (Path(e['path'])/'tests').rglob('*.py'):
                if 'curl' in path.read_text():
                    raise RuntimeError('tests_require_curl:' + e['task'])
    atomic_json(output/'task-registration.json', {'stream':m['stream'], 'model_calls':0})
    atomic_json(output/'bootstrap-plans.json', plans)
    original = build.profile
    try:
        build.profile = lambda p: old['tasks'][p.name]['profile'] if p.name in old['tasks'] else 'official-script'
        if not (output/'bundle/manifest.json').exists():
            build.build(output/'task-registration.json', output/'bundle', reuse=REUSE)
    finally:
        build.profile = original
    prefix = output/'bundle/staging/opt/rsi-offline-v1'
    if not (prefix/'legacy_grade.py').exists():
        shutil.copy2(prefix/'grade.py', prefix/'legacy_grade.py')
    if not (prefix/'legacy_isolated.py').exists():
        shutil.copy2(prefix/'isolated.py',prefix/'legacy_isolated.py')
    for name in ('grade.py', 'bootstrap.py', 'pytest_entry.py', 'isolated.py'):
        shutil.copy2(Path(__file__).parent/name, prefix/name)
    with tarfile.open(prior.base.core.PROJECT/'.cache/tmux/tmux-runtime-singleuid-amd64.tar.gz') as archive:
        member=next(m for m in archive.getmembers() if m.name.endswith('/usr/local/lib/rsi-singleuid-compat.so'))
        with archive.extractfile(member) as src,(prefix/'singleuid-compat.so').open('wb') as dst:
            shutil.copyfileobj(src,dst)
    if not list((prefix/'wheels').glob('numpy-2.3.0-*.whl')) or not list((prefix/'wheels').glob('setuptools-80.9.0-*.whl')):
        subprocess.run([str(prefix/'python/bin/python3'), '-m', 'pip', 'download', '--disable-pip-version-check',
            '--no-cache-dir', '--only-binary=:all:', '--no-deps', '--dest', str(prefix/'wheels'),
            'numpy==2.3.0', 'setuptools==80.9.0'], check=True, timeout=180)
    repo = prefix/'resources/ocaml.git'
    if not repo.exists():
        subprocess.run(['git', 'clone', '--bare', '--depth', '1', '--single-branch', '--branch', OCAML_TAG,
                        OCAML_URL, str(repo)], check=True, timeout=240)
    commit = subprocess.check_output(['git','-C',str(repo),'rev-parse',OCAML_TAG+'^{commit}'],text=True).strip()
    # Cache missing original apt dependencies in disposable preparation containers.
    for e in entries:
        packages = plans[e['task']]['apt_packages']
        if e['task']=='configure-git-webserver':packages=packages+['expect']
        if not packages:
            continue
        if (output/'system-dependencies'/f"{e['task']}.json").exists():
            provenance=prefix/'debs'/e['task']/'archive-provenance.json'
            if not provenance.exists() or read_json(provenance).get('all_uris_accounted'):
                continue
        cid = runner.docker(['run','-d','--network','host',
            '-e','http_proxy=http://10.0.2.2:17897','-e','https_proxy=http://10.0.2.2:17897',
            '--entrypoint','/bin/sh',e['image_id'],'-c','sleep infinity']).stdout.strip()
        try:
            missing=[]
            for package in packages:
                p=subprocess.run(['docker','exec',cid,'dpkg-query','-W','-f=${Status}',package],capture_output=True,text=True,timeout=20)
                if p.returncode or p.stdout != 'install ok installed': missing.append(package)
            if missing:
                apt_prepare(cid,['update'],output/'preparation-logs'/f"{e['task']}-update.log")
                dest=prefix/'debs'/e['task'];dest.mkdir(parents=True,exist_ok=True)
                cache_apt_packages(cid,missing,dest)
                if not list(dest.glob('*.deb')): raise RuntimeError('apt_download_cache_empty')
            atomic_json(output/'system-dependencies'/f"{e['task']}.json",{'image':e['image_id'],'packages':packages,'missing':missing})
        finally:
            runner.docker(['stop','--time','1',cid]); runner.docker(['rm',cid])
    payload = read_json(prefix/'payload.json')
    payload['extra_resources']={'ocaml_commit':commit,'ocaml_tag':OCAML_TAG,'ocaml_url':OCAML_URL}
    for task,plan in plans.items():payload['tasks'][task]['bootstrap']=plan
    payload['files']={str(p.relative_to(prefix)):file_hash(p) for p in sorted(prefix.rglob('*'))
                      if p.is_file() and not p.is_symlink() and p != prefix/'payload.json'}
    atomic_json(prefix/'payload.json',payload)
    bundle=output/'bundle/runtime.tar.gz'
    with tarfile.open(bundle,'w:gz') as archive:
        def owner(info):
            info.uid=info.gid=0;info.uname=info.gname='';return info
        archive.add(prefix.parents[0],arcname='opt',filter=owner)
    manifest=read_json(output/'bundle/manifest.json')
    manifest.update(bundle=str(bundle.resolve()),bundle_sha256=file_hash(bundle),
        payload_sha256=file_hash(prefix/'payload.json'),tasks=payload['tasks'],
        source_manifest=str((output/'task-registration.json').resolve()),
        source_manifest_sha256=file_hash(output/'task-registration.json'))
    manifest['code'].update({str((Path(__file__).parent/name).resolve()):file_hash(Path(__file__).parent/name)
                             for name in ('grade.py','bootstrap.py','pytest_entry.py','isolated.py')})
    atomic_json(output/'bundle/manifest.json',manifest)
    grading.prepare(output/'bundle/manifest.json',output/'grader-manifest.json')
    atomic_json(output/'prepared.json',{'time':time.time(),'registered_tasks':len(m['stream']),'new_tasks':len(entries),
                                     'model_calls':0,'assertions_changed':False})


def smoke(output, workers=3, fail_fast=True):
    m=prior.verify(SOURCE)
    safe=grading.load_manifest(output/'grader-manifest.json',file_hash(output/'grader-manifest.json'))
    entries=[e for e in m['stream'] if safe['tasks'][e['task']]['profile']=='official-script']
    def check(entry):
        folder=output/'acceptance'/entry['task']
        if (folder/'summary.json').exists():return read_json(folder/'summary.json')
        if folder.exists():raise RuntimeError('unfinished_smoke_evidence_requires_inspection:'+entry['task'])
        cid=runner.docker(['run','-d','--network','none','--entrypoint','/bin/sh',entry['image_id'],'-c','sleep infinity']).stdout.strip()
        atomic_json(folder/'container.json',{'container':cid,'image':entry['image_id'],'model_calls':0,'formal_score':False})
        try:
            result=grading.grade_container(cid,entry['task'],safe,folder/'grade')
            value={'task':entry['task'],'passed':True,'unmodified_reward':result['reward'],'tests':result['summary']['tests']}
        except Exception as exc:
            value={'task':entry['task'],'passed':False,'error':type(exc).__name__+':'+str(exc)[-1800:]}
        finally:
            runner.docker(['stop','--time','1',cid]);runner.docker(['rm',cid])
            atomic_json(folder/'cleanup.json',{'removed':True,'container':cid,'time':time.time()})
        atomic_json(folder/'summary.json',value)
        print(json.dumps(value),flush=True)
        return value
    rows=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(check,e) for e in entries]
        for future in as_completed(futures):
            rows.append(future.result())
            atomic_json(output/'smoke-progress.json',{'time':time.time(),'completed':len(rows),'total':len(entries),
                'passed':sum(r['passed'] for r in rows),'failed':[r for r in rows if not r['passed']]})
            if not rows[-1]['passed'] and fail_fast:
                for pending in futures:pending.cancel()
                break
    atomic_json(output/'acceptance.json',{'passed':len(rows)==len(entries) and all(r['passed'] for r in rows),
        'grader_sha256':file_hash(output/'grader-manifest.json'),'tasks':rows,
        'model_calls':0,'formal_scores':0,'assertions_changed':False,'positive_control_run':False,'network':'none'})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--prepare',action='store_true');p.add_argument('--smoke',action='store_true')
    p.add_argument('--resume-preparation',action='store_true')
    p.add_argument('--reuse-preparation',type=Path)
    p.add_argument('--keep-checking',action='store_true')
    a=p.parse_args()
    if a.prepare:prepare(a.output.resolve(),resume=a.resume_preparation,reuse_preparation=a.reuse_preparation)
    if a.smoke:smoke(a.output.resolve(),fail_fast=not a.keep_checking)
