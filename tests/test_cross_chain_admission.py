import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from cross_chain_rsi import admission as a, recovery as r, campaign as c
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json

TRACE = '''def test_output():
    out_path = Path("/app/output.json")
>   assert out_path.exists(), f"File {out_path} does not exist"
E   AssertionError: File /app/output.json does not exist
E   assert False
E    +  where False = exists()
E    +    where exists = PosixPath('/app/output.json').exists
/tests/test_output.py:5: AssertionError'''
PRELUDE = 'E: Unable to locate package curl\nInstalled locally cached uv 0.9.5\n'
SESSION = '============================= test session starts ==============================\n'


@pytest.fixture
def trial(tmp_path, monkeypatch):
    dataset = tmp_path / 'dataset'
    task = dataset / 'task'
    task.mkdir(parents=True)
    (task / 'instruction.md').write_text('Create `/app/output.json`.')
    monkeypatch.setattr(a.core, 'DATASET', dataset)
    folder = tmp_path / 'stage'
    path = folder / 'trials/one/result.json'
    atomic_json(path, {'config': {'task': {'path': str(task)}}, 'verifier_result': {'rewards': {'reward': 0}}, 'exception_info': None})
    atomic_json(path.parent / 'verifier/ctrf.json', {'results': {
        'summary': {'tests': 1, 'passed': 0, 'failed': 1, 'other': 0, 'pending': 0, 'skipped': 0},
        'tests': [{'name': 'test_output', 'status': 'failed', 'raw_status': 'call_failed', 'trace': TRACE}]}})
    (path.parent / 'verifier/test-stdout.txt').write_text(PRELUDE + SESSION + TRACE)
    (path.parent / 'verifier/reward.txt').write_text('0\n')
    atomic_json(folder / 'launch.json', {'before': {'revision': 2}})
    atomic_json(path.parent / 'agent/rsi_start.json', {'version': {'revision': 2}})
    return path


def test_proven_missing_output_recovered_without_changing_raw_score(trial, tmp_path):
    before = trial.read_bytes()
    assert not a.ORIGINAL_SCORED_RESULT(trial)[0]
    v = a.assess(trial)
    assert v['valid'] and v['override'] and v['raw_reward_unchanged'] == 0
    assert v['missing_required_outputs'][0]['required_output'] == '/app/output.json'
    assert a.scored_result(trial, tmp_path / 'audit') == (True, None)
    assert trial.read_bytes() == before
    assert len(list((tmp_path / 'audit/result_admissions').glob('*.json'))) == 1
    with a.legacy_adapter(None):
        assert a.core.scored_result(trial) == (True, None)
    assert not a.core.scored_result(trial)[0]


@pytest.mark.parametrize('mutation', ['exception', 'missing_report', 'partial', 'skipped', 'counts', 'reward', 'reward_file', 'import_failure', 'arbitrary_assert', 'no_contract', 'no_cache', 'during_tests', 'wrong_package', 'disk', 'missing_trace', 'setup_failure'])
def test_real_infra_or_ambiguous_failure_stays_quarantined(trial, mutation):
    value = read_json(trial)
    rp = trial.parent / 'verifier/ctrf.json'
    report = read_json(rp)
    lp = trial.parent / 'verifier/test-stdout.txt'
    if mutation == 'exception': value['exception_info'] = {'exception_type': 'VerifierTimeoutError'}
    elif mutation == 'missing_report': report = {}
    elif mutation == 'partial': report['results']['summary']['pending'] = 1
    elif mutation == 'skipped': report['results']['summary']['skipped'] = 1
    elif mutation == 'counts': report['results']['summary']['tests'] = 2
    elif mutation == 'reward': value['verifier_result']['rewards']['reward'] = 1
    elif mutation == 'reward_file': (trial.parent / 'verifier/reward.txt').write_text('1')
    elif mutation == 'import_failure': report['results']['tests'][0]['trace'] = 'ModuleNotFoundError: selenium'
    elif mutation == 'arbitrary_assert': report['results']['tests'][0]['trace'] = 'assert subprocess.returncode == 0\nAssertionError'
    elif mutation == 'no_contract': (Path(value['config']['task']['path']) / 'instruction.md').write_text('No output file required.')
    elif mutation == 'no_cache': lp.write_text('E: Unable to locate package curl\n' + SESSION + TRACE)
    elif mutation == 'during_tests': lp.write_text(PRELUDE + SESSION + 'curl: command not found\n' + TRACE)
    elif mutation == 'wrong_package': lp.write_text(PRELUDE + 'E: Unable to locate package chromium\n' + SESSION + TRACE)
    elif mutation == 'disk': lp.write_text(PRELUDE + 'No space left on device\n' + SESSION + TRACE)
    elif mutation == 'missing_trace': report['results']['tests'][0]['trace'] = ''
    elif mutation == 'setup_failure': report['results']['tests'][0]['raw_status'] = 'setup_failed'
    atomic_json(trial, value)
    atomic_json(rp, report)
    assert not a.assess(trial)['valid']


def test_bootstrap_warning_with_all_tests_passed_is_recoverable(trial):
    value = read_json(trial)
    value['verifier_result']['rewards']['reward'] = 1
    atomic_json(trial, value)
    rp = trial.parent / 'verifier/ctrf.json'
    report = read_json(rp)
    report['results']['summary'].update(passed=1, failed=0)
    report['results']['tests'] = [{'name': 'test_output', 'status': 'passed'}]
    atomic_json(rp, report)
    (trial.parent / 'verifier/reward.txt').write_text('1')
    assert a.assess(trial)['all_tests_passed']


def test_recovery_preserves_blocked_record_and_never_repeats_trial(trial, tmp_path):
    path = trial.parents[2] / 'record.json'
    atomic_json(path, {'status': 'blocked', 'reason': 'RuntimeError:invalid_result:verifier_infrastructure:Unable to locate package'})
    old = path.read_bytes()
    with patch('subprocess.Popen') as solve:
        result = r.recover_record(tmp_path / 'root', path, 'task', 'meta')
        assert result and not path.exists()
        archive = next((tmp_path / 'root/recoveries').glob('*/*/*/record.before.json'))
        assert archive.read_bytes() == old
        atomic_json(path, {'status': 'blocked', 'reason': 'worker_interrupted again'})
        assert r.recover_record(tmp_path / 'root', path, 'task', 'meta') is None
        solve.assert_not_called()


def test_recovery_does_not_move_complete_or_live_child_records(trial, tmp_path, monkeypatch):
    path = trial.parents[2] / 'record.json'
    atomic_json(path, {'status': 'complete', 'reward': 0})
    assert r.recover_record(tmp_path / 'root', path, 'task', 'meta') is None
    atomic_json(path, {'status': 'blocked', 'reason': 'worker_interrupted'})
    monkeypatch.setattr(r, 'orphaned_child', lambda p: {'pid': 123})
    assert r.recover_record(tmp_path / 'root', path, 'task', 'meta') is None
    assert path.exists()


def test_recovery_requires_same_memory_version(trial, tmp_path):
    path = trial.parents[2] / 'record.json'
    atomic_json(path, {'status': 'blocked', 'reason': 'worker_interrupted'})
    atomic_json(trial.parent / 'agent/rsi_start.json', {'version': {'revision': 100}})
    assert r.recover_record(tmp_path / 'root', path, 'task', 'meta') is None


def test_source_wrapper_scopes_only_registered_cohort(tmp_path, monkeypatch):
    m = {'source_order': ['s'], 'source_tasks': {'s': ['s']}, 'ports': [8040]}
    monkeypatch.setattr(c, 'verify', lambda root: m)
    with patch.object(c.legacy, 'run_stage') as stage:
        with pytest.raises(ValueError):
            c.source_stage(tmp_path, 'outside', 'outside', 8040)
        stage.assert_not_called()


def test_adopts_worker_launched_before_checkpoint(tmp_path, monkeypatch):
    proc = tmp_path / 'proc/123'
    proc.mkdir(parents=True)
    root = tmp_path / 'run'
    args = ['python', '-u', '-m', 'cross_chain_rsi.campaign', '--root', str(root), '--source-stage', '--chain', 's', '--task', 's', '--port', '8040']
    (proc / 'cmdline').write_bytes(('\0'.join(args) + '\0').encode())
    monkeypatch.setattr(c.core, 'process_identity', lambda pid: 'id123')
    m = {'ports': [8040], 'source_tasks': {'s': ['s']}}
    active = r.adopt_untracked(root, m, {}, tmp_path / 'proc')
    assert active['8040']['pid'] == 123 and active['8040']['kind'] == 'source'
    assert r.adopt_untracked(root, m, active, tmp_path / 'proc') == active
    args[args.index('--root') + 1] = '/unrelated-root'
    (proc / 'cmdline').write_bytes(('\0'.join(args) + '\0').encode())
    assert not r.adopt_untracked(root, m, {}, tmp_path / 'proc')


def test_watchdog_restart_is_bounded():
    from cross_chain_rsi.watchdog import restart_allowed
    assert restart_allowed([], 10000)
    assert not restart_allowed([9990], 10000)
    assert not restart_allowed([9100, 9200, 9300], 10000)
    assert restart_allowed([100, 200, 300], 10000)


def test_needs_review_controller_keeps_heartbeat(tmp_path, monkeypatch):
    m = {'source_order': ['s'], 'source_tasks': {'s': ['s']}, 'source_roots': {'skill-rsi': str(tmp_path/'control'), 'meta-skill-rsi': str(tmp_path/'source')},
         'target_order': ['t'], 'target_tasks': {'t': ['t']}, 'ports': [], 'interpretation': 'unit test'}
    monkeypatch.setattr(c, 'SOURCE_ROOT', tmp_path/'source')
    c.SOURCE_ROOT.mkdir()
    (c.SOURCE_ROOT/'PAUSE').write_text('old queue paused')
    monkeypatch.setattr(c, 'verify', lambda root: m)
    monkeypatch.setattr(c.recovery, 'adopt_untracked', lambda root,m,active: active)
    monkeypatch.setattr(c.recovery, 'recover_available', lambda *args: [])
    monkeypatch.setattr(c.core, 'docker_disk_status', lambda root: {'ok': True})
    monkeypatch.setattr(c.core, 'live_chain_workers', lambda root: [])
    atomic_json(c.record_path(c.SOURCE_ROOT,'s','meta-skill-rsi','s'),{'status':'blocked','reason':'unknown infrastructure failure'})
    class PollObserved(BaseException): pass
    def sleep(seconds):
        assert seconds == 20
        raise PollObserved()
    monkeypatch.setattr(c.time, 'sleep', sleep)
    with pytest.raises(PollObserved):
        c.run(tmp_path/'run')
    status=read_json(tmp_path/'run/status.json')
    assert status['terminal']=='needs_review' and status['phase']=='source'
    assert (tmp_path/'run/alerts/latest.json').exists()
