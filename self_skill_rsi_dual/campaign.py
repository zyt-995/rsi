"""Task-boundary migration: same algorithm, two candidate-validation lanes."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json,file_hash
from self_skill_rsi_continue import campaign as prior

base,v2,runtime,POLICY = prior.base,prior.v2,prior.runtime,prior.POLICY
PRIOR_VERIFY,PRIOR_REPORT = prior.verify,prior.report
PORTS = [8041,8040]
GROUPS = [[4,5,6,7],[1,2,3,8]]


def code():
    return {str(p.resolve()):file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def prepare(root,source,deployment):
    root,source,deployment = root.resolve(),source.resolve(),deployment.resolve()
    if root == source or root in source.parents or source in root.parents:
        raise RuntimeError('separate_deployment_directory_required')
    with base.legacy.exclusive(root/'prepare.lock'):
        if (root/'manifest.json').exists():
            return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('migration_target_not_empty')
        v2.check_source_stopped(source)
        old = PRIOR_VERIFY(source)
        completed = prior.old.prior.boundary(source,old)
        d = read_json(deployment)
        if (d['ports'] != PORTS or d['gpu_groups'] != GROUPS
                or d['model'] != old['model'] or not d['functional_checks_passed']):
            raise RuntimeError('dual_deployment_not_accepted')
        base.verify_files(d['evidence_hashes'])
        frozen = prior.old.prior.clone_prefix(source,root,completed)
        shutil.copy2(source/'matched-controls.json',root/'matched-controls.json')
        for p in [source/'manifest.json',source/'seal.json',deployment,root/'matched-controls.json']:
            frozen[str(p)] = file_hash(p)
        frozen.update(d['evidence_hashes'])
        m = copy.deepcopy(old)
        m['ports'] = PORTS
        m['inference_migration']['deployment'] = str(deployment)
        m['continuation']['candidate_validation_parallelism'] = 2
        m['dual_inference_migration'] = {'source_root':str(source),'created':time.time(),
            'preserved_count':len(completed),'first_new_task':old['stream'][len(completed)]['task'],
            'implementation':code(),'frozen_files':frozen,'algorithm_changed':False,
            'task_order_changed':False,'scored_task_replays':0,
            'gpu_groups':GROUPS,'interpretation':'Same TP4 model settings; candidate checks now parallel on '
                'two identical model replicas. Physical GPU group changed; numerical identity not guaranteed.'}
        atomic_json(root/'manifest.json',m)
        atomic_json(root/'seal.json',{'manifest_sha256':file_hash(root/'manifest.json'),'code':v2.snapshot()})
        atomic_json(root/'status.json',{'state':'prepared_not_running','time':time.time(),'completed':len(completed)})
        return verify(root)


def verify(root):
    m,seal = read_json(root/'manifest.json'),read_json(root/'seal.json')
    change = m['dual_inference_migration']
    if (seal['manifest_sha256'] != file_hash(root/'manifest.json') or seal['code'] != v2.snapshot()
            or change['implementation'] != code()):
        raise RuntimeError('dual_deployment_seal_changed')
    source = Path(change['source_root'])
    v2.check_source_stopped(source)
    old = PRIOR_VERIFY(source)
    expected = copy.deepcopy(old)
    expected['ports'] = PORTS
    expected['inference_migration']['deployment'] = m['inference_migration']['deployment']
    expected['continuation']['candidate_validation_parallelism'] = 2
    if {k:v for k,v in m.items() if k != 'dual_inference_migration'} != expected:
        raise RuntimeError('more_than_inference_parallelism_changed')
    base.verify_files(change['frozen_files'])
    return m


def report(root,m):
    result = PRIOR_REPORT(root,m)
    result['inference'] = {'ports':m['ports'],'gpu_groups':GROUPS,
        'candidate_validation_parallelism':2,'official_stream_parallelism':1,
        'preserved_at_handoff':m['dual_inference_migration']['preserved_count']}
    return result


def run(root):
    old_verify,old_report = prior.verify,prior.report
    try:
        prior.verify,prior.report = verify,report
        prior.run(root)
    finally:
        prior.verify,prior.report = old_verify,old_report


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--source',type=Path)
    p.add_argument('--deployment',type=Path)
    p.add_argument('--prepare',action='store_true')
    p.add_argument('--report',action='store_true')
    p.add_argument('--run',action='store_true')
    p.add_argument('--allow-native-runs',action='store_true')
    a = p.parse_args()
    if a.prepare:
        prepare(a.root,a.source,a.deployment)
    elif a.report:
        print(json.dumps(report(a.root,verify(a.root)),indent=2))
    elif a.run and a.allow_native_runs:
        run(a.root)
    else:
        p.error('explicit action required')
