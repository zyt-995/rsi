"""Resume exactly the completed prefix; change only grader coverage and service PIDs."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_dual import campaign as source_campaign
from self_skill_rsi_continue import campaign as runner

base, v2, runtime, POLICY = runner.base, runner.v2, runner.runtime, runner.POLICY
PORTS, GROUPS = source_campaign.PORTS, source_campaign.GROUPS
SOURCE = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261001-dual-v1')


def code():
    return {str(p.resolve()):file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def changed_manifest(old, deployment, grader, ready):
    m=copy.deepcopy(old)
    m['inference_migration']['deployment']=str(deployment)
    m['resilience_migration']['grader']=str(grader)
    m['resilience_migration']['grader_sha256']=file_hash(grader)
    m['continuation']['ready_grader_tasks']=ready
    return m


def prepare(root, deployment, offline):
    root, deployment, offline=root.resolve(),deployment.resolve(),offline.resolve()
    if root==SOURCE or root in SOURCE.parents or SOURCE in root.parents:
        raise RuntimeError('separate_run_directory_required')
    with base.legacy.exclusive(root/'prepare.lock'):
        if (root/'manifest.json').exists():return verify(root)
        if any(p.name!='prepare.lock' for p in root.iterdir()):raise RuntimeError('target_not_empty')
        v2.check_source_stopped(SOURCE)
        old=source_campaign.verify(SOURCE)
        completed=runner.old.prior.boundary(SOURCE,old)
        if len(completed)!=24:raise RuntimeError('unexpected_completed_prefix')
        checks=read_json(offline/'acceptance.json')
        grader=offline/'grader-manifest.json'
        if not checks.get('passed') or checks['grader_sha256']!=file_hash(grader):
            raise RuntimeError('all_remaining_graders_must_pass_acceptance')
        bridge=read_json(offline/'bridge-check.json',{})
        if not bridge.get('passed') or bridge.get('grader_sha256')!=file_hash(grader):
            raise RuntimeError('solver_loopback_positive_control_missing')
        safe=runner.grading.load_manifest(grader,file_hash(grader))
        pending={e['task'] for e in old['stream'][len(completed):]}
        if not pending <= set(safe['tasks']) or pending != {r['task'] for r in checks['tasks'] if r['passed']}:
            raise RuntimeError('remaining_tasks_not_fully_accepted')
        d=read_json(deployment)
        if d['ports']!=PORTS or d['gpu_groups']!=GROUPS or d['model']!=old['model'] or not d['functional_checks_passed']:
            raise RuntimeError('inference_deployment_not_accepted')
        base.verify_files(d['evidence_hashes'])
        frozen=runner.old.prior.clone_prefix(SOURCE,root,completed)
        shutil.copy2(SOURCE/'matched-controls.json',root/'matched-controls.json')
        for p in (SOURCE/'manifest.json',SOURCE/'seal.json',grader,offline/'acceptance.json',offline/'bridge-check.json',deployment,root/'matched-controls.json'):
            frozen[str(p)]=file_hash(p)
        frozen.update(d['evidence_hashes'])
        ready=[e['task'] for e in old['stream'][old['continuation']['preserved_count']:]]
        m=changed_manifest(old,deployment,grader,ready)
        m['resume_migration']={'source_root':str(SOURCE),'created':time.time(),
            'preserved_count':len(completed),'first_new_task':old['stream'][len(completed)]['task'],
            'implementation':code(),'frozen_files':frozen,'algorithm_changed':False,'formal_replays':0,
            'remaining_environment_acceptance':str(offline/'acceptance.json'),
            'interpretation':'Existing images and unchanged official test workflows; dependency bootstrap cached offline. '
                'All 24 prior scores and skill history retained. Same agent, model, budgets and update rule.'}
        atomic_json(root/'manifest.json',m)
        atomic_json(root/'seal.json',{'manifest_sha256':file_hash(root/'manifest.json'),'code':v2.snapshot()})
        atomic_json(root/'status.json',{'state':'prepared_not_running','time':time.time(),'completed':len(completed)})
        return verify(root)


def verify(root):
    m,seal=read_json(root/'manifest.json'),read_json(root/'seal.json')
    change=m['resume_migration']
    if seal['manifest_sha256']!=file_hash(root/'manifest.json') or seal['code']!=v2.snapshot() or change['implementation']!=code():
        raise RuntimeError('resume_implementation_or_manifest_changed')
    v2.check_source_stopped(SOURCE)
    old=source_campaign.verify(SOURCE)
    expected=changed_manifest(old,Path(m['inference_migration']['deployment']),Path(m['resilience_migration']['grader']),
        [e['task'] for e in old['stream'][old['continuation']['preserved_count']:]])
    if {k:v for k,v in m.items() if k!='resume_migration'}!=expected:
        raise RuntimeError('resume_changed_algorithm_or_task_order')
    base.verify_files(change['frozen_files'])
    safe=runner.grading.load_manifest(m['resilience_migration']['grader'],m['resilience_migration']['grader_sha256'])
    if any(e['task'] not in safe['tasks'] for e in m['stream']):raise RuntimeError('incomplete_grader_coverage')
    return m


def report(root,m):
    value=source_campaign.report(root,m)
    value['resume']={k:m['resume_migration'][k] for k in ('preserved_count','first_new_task','algorithm_changed','formal_replays')}
    return value


def run(root):
    saved_verify,saved_report=runner.verify,runner.report
    try:
        runner.verify,runner.report=verify,report
        runner.run(root)
    finally:
        runner.verify,runner.report=saved_verify,saved_report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--prepare',action='store_true');p.add_argument('--deployment',type=Path);p.add_argument('--offline',type=Path)
    p.add_argument('--run',action='store_true');p.add_argument('--allow-native-runs',action='store_true');p.add_argument('--report',action='store_true')
    a=p.parse_args()
    if a.prepare:prepare(a.root,a.deployment,a.offline)
    elif a.report:print(json.dumps(report(a.root,verify(a.root)),indent=2))
    elif a.run and a.allow_native_runs:run(a.root)
    else:p.error('explicit action required')
