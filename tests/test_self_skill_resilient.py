import json
from pathlib import Path
import subprocess
import sys

import pytest

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_resilient import campaign, guardian, grading, runtime, safe_entry
from test_self_skill_rsi_v2 import Native, protocol, proposer
from test_self_skill_accel import prepared_source


def test_safe_path_excludes_poisoned_stdlib_without_changing_cwd(tmp_path):
    (tmp_path / 'inspect.py').write_text('raise RuntimeError("poisoned")')
    (tmp_path / 'json.py').write_text('raise RuntimeError("poisoned")')
    command = [sys.executable, '-c', 'import inspect,json,os; assert hasattr(inspect,"getmro"); print(os.getcwd())']
    bad = subprocess.run(command, cwd=tmp_path, capture_output=True)
    assert bad.returncode != 0
    good = subprocess.run(safe_entry.safe_command(command), cwd=tmp_path, capture_output=True, text=True)
    assert good.returncode == 0 and good.stdout.strip() == str(tmp_path)


def test_safe_command_only_modifies_python_entrypoints():
    assert safe_entry.safe_command(['/usr/bin/python3', '-m', 'pytest']) == ['/usr/bin/python3', '-P', '-m', 'pytest']
    assert safe_entry.safe_command(['git', 'clone', 'repo']) == ['git', 'clone', 'repo']
    assert safe_entry.safe_command(['uv', 'pip', 'install']) == ['uv', 'pip', 'install']


@pytest.mark.parametrize('reward', [0, 1])
def test_valid_result_and_zero_are_never_resolved(tmp_path, monkeypatch, reward):
    entry, head = {'uid': 's0', 'task': 'task'}, {'revision': 1}
    folder = tmp_path / 'solve'
    native = Native(reward)
    monkeypatch.setattr(runtime.original, 'native_solve', native)
    monkeypatch.setattr(runtime, 'clean_success', lambda _: None)
    a = runtime.native_solve(entry, None, head, folder, 8040, 'stream', manifest={})
    b = runtime.native_solve(entry, None, head, folder, 8040, 'stream', manifest={})
    assert a == b and a['reward'] == reward and len(native.calls) == 1


def test_invalid_native_is_not_zero_or_blindly_replayed(tmp_path, monkeypatch):
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError('missing_or_ambiguous_trial')
    monkeypatch.setattr(runtime.original, 'native_solve', fail)
    with pytest.raises(RuntimeError):
        runtime.native_solve({'uid': 's0'}, None, {}, tmp_path / 'solve', 8040, 'stream', manifest={})
    assert len(calls) == 1
    assert not (tmp_path / 'solve/record.json').exists()
    assert read_json(tmp_path / 'solve/failure.json')['score'] is None


def test_only_post_agent_unscored_verifier_error_is_regradable():
    data = {'verifier_result': None, 'agent_execution': {'finished_at': 'done'},
            'verifier': {'started_at': 'yes'}, 'exception_info': {'exception_message': 'offline_grading_process_exit:1'}}
    assert runtime.grading_only_failure(data)
    assert not runtime.grading_only_failure({**data, 'verifier_result': {'rewards': {'reward': 0}}})
    assert not runtime.grading_only_failure({**data, 'agent_execution': {}})
    assert not runtime.grading_only_failure({**data, 'exception_info': {'exception_message': 'model_refused'}})


def test_regrade_restart_never_runs_an_arbitrary_shell_program():
    assert runtime.passive_entrypoint(['sh', '-c', 'sleep infinity'])
    assert runtime.passive_entrypoint(['tail', '-f', '/dev/null'])
    assert not runtime.passive_entrypoint(['sh', '-c', 'setup.sh; sleep infinity'])


def test_invalid_proposal_retains_bank_without_resampling(tmp_path, monkeypatch):
    calls = []
    def bad(*args):
        calls.append(1)
        raise ValueError('invalid_candidate_json')
    monkeypatch.setattr(runtime.learning, 'propose', bad)
    args = (tmp_path / 'update', {'uid': 's0'}, {'skill': 'old'}, {'skill': 'evidence'}, [], 'model', 'url')
    a = runtime.propose(*args)
    assert a['status'] == 'no_change' and a['payload'] == {'skill': 'old'}
    assert runtime.propose(*args) == a and len(calls) == 1


def test_changed_learning_input_is_never_waived(tmp_path, monkeypatch):
    def bad(*args):
        raise RuntimeError('learning_inputs_changed')
    monkeypatch.setattr(runtime.learning, 'propose', bad)
    with pytest.raises(RuntimeError, match='learning_inputs_changed'):
        runtime.propose(tmp_path, {}, {}, {}, [], 'm', 'u')


@pytest.mark.parametrize('state', ['complete', 'needs_attention', 'paused'])
def test_guardian_terminal_means_keep_monitoring_without_restarting(state):
    assert guardian.action({'state': state}, False, 1, 999, 0) == 'terminal'


def test_controller_crashes_have_backoff_and_finite_budget():
    s = {'state': 'running'}
    assert guardian.action(s, False, 0, 100, 0) == 'launch'
    assert guardian.action(s, True, 1, 100, 0) == 'monitor'
    assert guardian.action(s, False, 1, 10, 0) == 'backoff'
    assert guardian.action(s, False, 1, 40, 0) == 'launch'
    assert guardian.action(s, False, 2, 40, 0) == 'backoff'
    assert guardian.action(s, False, 4, 10000, 0) == 'exhausted'


def test_unchanged_solver_command_except_infrastructure(monkeypatch):
    m = {'resilience_migration': {'grader': '/g', 'grader_sha256': 'hash'}}
    monkeypatch.setattr(campaign.prior.offline, 'ORIGINAL_COMMAND', lambda: ['harbor', '--model', 'unchanged'])
    old = campaign.base.core.harbor_command
    with campaign.adapter(m):
        command = campaign.base.core.harbor_command()
        assert command[:3] == ['harbor', '--model', 'unchanged']
        assert command[-2:] == ['--environment-kwarg', 'keep_containers=true']
        assert '--agent-kwarg' not in command[3:]
    assert campaign.base.core.harbor_command is old


def test_completed_step_resume_does_not_call_solver(protocol):
    root, m = protocol
    native = Native(0)
    first = campaign.v2.step(root, m, 0, solve=native, proposer=proposer, checker=lambda _: None)
    calls = len(native.calls)
    assert campaign.v2.step(root, m, 0, solve=native, proposer=proposer, checker=lambda _: None) == first
    assert len(native.calls) == calls


def test_grading_attempt_cannot_be_overwritten(tmp_path):
    atomic_json(tmp_path / 'attempt.json', {'prior': True})
    with pytest.raises(RuntimeError, match='never_overwrite'):
        grading.grade_container('none', 'task', {}, tmp_path)


def test_release_refuses_while_native_trial_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(guardian, 'live_children', lambda _: [{'pid': 1}])
    assert guardian.release(tmp_path, {}, {}) is False


def test_guardian_stays_alive_and_writes_alert_for_blocked_run(tmp_path, monkeypatch):
    atomic_json(tmp_path / 'status.json', {'state': 'needs_attention', 'time': 1000, 'reason': 'grading_failed'})
    monkeypatch.setattr(guardian.time, 'time', lambda: 1100)
    monkeypatch.setattr(campaign.base.legacy, 'alive', lambda _: False)
    monkeypatch.setattr(guardian, 'endpoint', lambda _: {'healthy': True, 'idle': True})
    monkeypatch.setattr(campaign, 'report', lambda *a: {'scored_tasks': 8})
    child, value = guardian.tick(tmp_path, {'ports': [8040,8041]}, {})
    assert child is None and value['action'] == 'terminal'
    assert read_json(tmp_path / 'alerts/latest.json')['alert'] == 'grading_failed'
    assert read_json(tmp_path / 'watchdog/status.json')['guardian_independent_of_controller']


def test_guardian_releases_only_after_idle_grace(tmp_path, monkeypatch):
    atomic_json(tmp_path / 'status.json', {'state': 'complete', 'time': 1000})
    monkeypatch.setattr(guardian.time, 'time', lambda: 1400)
    monkeypatch.setattr(campaign.base.legacy, 'alive', lambda _: False)
    monkeypatch.setattr(guardian, 'endpoint', lambda _: {'healthy': True, 'idle': True})
    monkeypatch.setattr(campaign, 'report', lambda *a: {})
    calls=[]
    monkeypatch.setattr(guardian, 'release', lambda *a: calls.append(1) or True)
    _, value = guardian.tick(tmp_path, {'ports': [8040]}, {})
    assert calls == [1] and value['resource_release_requested']


def test_migration_preserves_prefix_and_only_replaces_unscored_failure(prepared_source, tmp_path, monkeypatch):
    source, original, _ = prepared_source
    failed = source / 'stream/s1/solve/trials/failure/result.json'
    atomic_json(failed, {'verifier_result': None, 'agent_execution': {'finished_at': 'done'},
        'verifier': {'started_at': 'yes'}, 'exception_info': {'exception_message': 'offline_grading_process_exit:1'}})
    monkeypatch.setattr(campaign.prior, 'verify', lambda root: read_json(root / 'manifest.json'))
    monkeypatch.setattr(campaign.prior.offline, 'stopped', lambda _: None)
    monkeypatch.setattr(grading, 'load_manifest', lambda *a: {})
    grader, acceptance = tmp_path / 'grader.json', tmp_path / 'acceptance.json'
    atomic_json(grader, {'fixed': 'safe_imports'})
    atomic_json(acceptance, {'passed': True, 'grader_sha256': file_hash(grader)})
    root = tmp_path / 'continued'
    m = campaign.prepare(root, source, grader, acceptance)
    assert m['resilience_migration']['preserved_official_uids'] == ['s0']
    assert m['resilience_migration']['explicit_replacement_uid'] == 's1'
    assert read_json(source / 'stream/s0/scored.json') == read_json(root / 'stream/s0/scored.json')
    assert not (root / 'stream/s1').exists()
    assert failed.exists() and (source / 'PAUSE').exists()
    assert {k:v for k,v in m.items() if k != 'resilience_migration'} == original
    campaign.verify(root)
    atomic_json(root / 'stream/s0/scored.json', {'reward': 0})
    with pytest.raises(RuntimeError, match='recorded_evidence_changed'):
        campaign.verify(root)


def test_regrade_crash_after_scoring_reuses_grade_without_model_calls(tmp_path, monkeypatch):
    attempt = tmp_path / 'attempt'
    raw = attempt / 'trials/test/result.json'
    atomic_json(raw, {'exception_info': {'error': 'grader'}, 'verifier_result': None})
    atomic_json(raw.parent / 'agent/trajectory.json', {'steps': []})
    atomic_json(raw.parent / 'verifier/retained-container.json', {'container': 'retained'})
    atomic_json(attempt / 'launch.json', {'started': 1})
    dest = attempt / 'regrade'
    atomic_json(dest / 'started.json', {'source_sha256': file_hash(raw)})
    monkeypatch.setattr(runtime, 'validate_result', lambda *a: {'reward': 0})
    monkeypatch.setattr(grading, 'grade_container', lambda *a: pytest.fail('must not regrade twice'))
    monkeypatch.setattr(runtime.original, 'native_solve', lambda *a: {'reward': 0, 'cached': True})
    result = runtime.regrade({'task': 'test'}, None, {}, attempt, raw, 8040, 'validation', {})
    assert result == {'reward': 0, 'cached': True}
    derived = read_json(dest / 'trials/test/result.json')
    assert derived['recovery_provenance']['new_model_calls'] == 0
    assert read_json(raw)['exception_info'] is not None
