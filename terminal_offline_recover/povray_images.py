"""Transport-only image repair for the upstream-confirmed POV-Ray outage."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from harbor.models.task.task import Task
from harbor.environments.base import environment_content_hash
from offline_verifier.runner import docker
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_storage import storage
from .povray_cache import ARCHIVES, ORIGIN, fetch
from .infrastructure import image_id


def wrapper(tool):
    cases='|'.join(ARCHIVES)
    return '''#!/bin/bash
# Infrastructure-only mirror of byte-verified, publicly requested archives.
args=()
for arg in "$@"; do
  case "$arg" in
    http://www.povray.org/ftp/pub/povray/Old-Versions/Official-2.2/*|https://www.povray.org/ftp/pub/povray/Old-Versions/Official-2.2/*)
      name="${arg##*/}"
      case "$name" in
        '''+cases+''') arg="http://10.0.2.2:18781/$name" ;;
      esac ;;
  esac
  args+=("$arg")
done
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}10.0.2.2"
export no_proxy="${no_proxy:+$no_proxy,}10.0.2.2"
exec /usr/bin/'''+tool+''' "${args[@]}"
'''


def prepare(root):
    os.environ.update(runner_environment(8041));storage.activate();cache=fetch(root)
    entries=[e for e in read_json(root/'queue.json')['remaining'] if e['chain']=='build-pov-ray']
    ready=root/'environment-ready/build-pov-ray.json'
    if ready.exists():raise RuntimeError('refuse_to_change_accepted_task_images')
    for e in entries:
        p=Path(e['path']);original=(p/'environment/Dockerfile').read_text()
        output=root/'transport-repair/povray-2.2'/e['task'];receipt=output/'receipt.json'
        if receipt.exists():
            r=read_json(receipt)
            if image_id(r['image'])!=r['image_id']:raise RuntimeError('transport_repaired_image_changed')
            continue
        context=output/'context'
        if not context.exists():shutil.copytree(p/'environment',context)
        modified=original
        # Only the archive transport for the task's pre-baked files changes.
        # For versions without pre-baked archives, do NOT add any archives or wget.
        for name in ARCHIVES:
            source=ORIGIN.replace('http:','https:')+'/'+name
            modified=modified.replace(source,'http://10.0.2.2:18781/'+name)
        for tool in ('curl','wget'):
            (context/('rsi-cache-'+tool)).write_text(wrapper(tool))
            modified+='\nCOPY rsi-cache-'+tool+' /tmp/rsi-cache-'+tool+'\n'
            modified+='RUN if [ -x /usr/bin/'+tool+' ]; then cp /tmp/rsi-cache-'+tool+' /usr/local/bin/'+tool+'; chmod 755 /usr/local/bin/'+tool+'; fi; rm /tmp/rsi-cache-'+tool+'\n'
        (context/'Dockerfile').write_text(modified)
        image=Task(p).config.environment.docker_image or 'hb__'+environment_content_hash(p/'environment',docker_image=None)+':rsi-cache'
        previous=image_id(image)
        if previous:docker(['tag',previous,'rsi-recovery-backup/'+e['task'].lower()+':'+previous[7:19]])
        args=['docker','build','--network','host','-t',image]
        for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):args+=['--build-arg',key+'=http://10.0.2.2:17898']
        args+=['--build-arg','NO_PROXY=10.0.2.2','--build-arg','no_proxy=10.0.2.2',str(context)]
        with (output/'build.log').open('ab') as log:subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1200)
        current=image_id(image)
        checks=[]
        for name,(_,expected) in ARCHIVES.items():
            raw=subprocess.run(['docker','run','--rm','--network','host','--entrypoint','/usr/local/bin/curl',current,
                '--fail','--silent','--max-time','15',ORIGIN.replace('http:','https:')+'/'+name],capture_output=True,check=True,timeout=30).stdout
            if hashlib.sha256(raw).hexdigest()!=expected:raise RuntimeError('cached_download_transport_mismatch')
            checks.append({'name':name,'sha256':expected,'passed':True})
        if e['task'].endswith(('EVO-1','EVO-2')):
            result=subprocess.run(['docker','run','--rm','--network','none','--entrypoint','/bin/bash',current,'-lc','command -v wget'],capture_output=True)
            if result.returncode==0:raise RuntimeError('wget_absence_constraint_changed')
        atomic_json(receipt,{'image':image,'image_id':current,'previous_image_id':previous,'time':time.time(),
            'original_dockerfile_sha256':file_hash(p/'environment/Dockerfile'),'adapted_dockerfile_sha256':file_hash(context/'Dockerfile'),
            'download_checks':checks,'official_tests_changed':False,'archives_preloaded_only_if_originally_specified':True,
            'scope':'only curl/wget transport of three exact official URLs; no task-solving changes',
            'original_files_unchanged':True,'upstream_issue':'https://github.com/harbor-framework/terminal-bench-2/issues/76'})
        print(json.dumps({'task':e['task'],'transport_ready':True,'image':current}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path);a=p.parse_args();prepare(a.root)
