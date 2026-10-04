"""Add the next control-matched chain to the existing cached offline grader.

Only an exact, already understood bootstrap is mapped to the plain profile.
No hidden test assertions, native images, learner prompts or skills are changed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

from offline_verifier import build, profiles, runner
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_resilient import grading
from . import campaign as c

REUSE = Path('/data2/ytzhang/rsi-harness-static-data/offline-verifier-v4')


def bootstrap_profile(path):
    if path.name in c.base.core.tasks('build-pmars'):
        template = c.base.core.DATASET/'chess-best-move/tests/test.sh'
        if (path/'tests/test.sh').read_bytes() != template.read_bytes():
            raise RuntimeError('pmars_bootstrap_differs_from_accepted_plain_profile')
        return 'plain'
    return profiles.profile(path)


def prepare(output):
    output = output.resolve()
    if output.exists():
        raise RuntimeError('offline_preparation_target_exists')
    source = c.old.verify(c.SOURCE)
    inventory = c.matched_tasks(c.CONTROL, {e['task'] for e in source['stream']})
    addition = [r for r in inventory['additional'] if r['chain'] == 'build-pmars']
    if [r['task'] for r in addition] != c.base.core.tasks('build-pmars'):
        raise RuntimeError('next_control_chain_not_complete')
    new = c.entries(addition, len(source['stream']))
    for e in new:
        c.runtime.original.check_entry(e)
        bootstrap_profile(Path(e['path']))
    output.mkdir(parents=True)
    source_manifest = output/'task-registration.json'
    atomic_json(source_manifest, {'stream': source['stream']+new,
        'source':str(c.SOURCE), 'model_calls':0, 'new_tasks':[e['task'] for e in new]})
    previous = build.profile
    try:
        build.profile = bootstrap_profile
        build.build(source_manifest, output/'bundle', reuse=REUSE)
    finally:
        build.profile = previous
    grader = output/'grader-manifest.json'
    grading.prepare(output/'bundle/manifest.json', grader)
    manifest = grading.load_manifest(grader, file_hash(grader))
    def check(entry):
        folder = output/'acceptance'/entry['task']
        cid = runner.docker(['run','-d','--network','none','--entrypoint','/bin/sh',
                             entry['image_id'],'-c','sleep infinity']).stdout.strip()
        atomic_json(folder/'container.json', {'container':cid,'image_id':entry['image_id'],
                    'purpose':'unmodified_image_grader_smoke','formal_score':False,'model_calls':0})
        try:
            result = grading.grade_container(cid, entry['task'], manifest, folder/'grade')
            if result['reward'] != 0:
                raise RuntimeError('unexpected_unmodified_image_pass')
            summary = {'task':entry['task'],'passed':True,'negative_control_reward':0,
                       'tests':result['summary']['tests'],'new_model_calls':0}
            atomic_json(folder/'summary.json', summary)
            return summary
        finally:
            runner.docker(['stop','--time','2',cid])
            # Evidence has been copied; remove only this function's disposable
            # smoke container. There is no agent output or experimental score.
            runner.docker(['rm',cid])
            atomic_json(folder/'cleanup.json', {'container':cid,'removed':True,'time':time.time()})
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(check, new))
    atomic_json(output/'acceptance.json', {'passed':True,'grader_sha256':file_hash(grader),
        'tasks':rows,'positive_control_run':False,'assertions_changed':False,
        'model_calls':0,'formal_scores':0,'network':'none'})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    prepare(p.parse_args().output)
