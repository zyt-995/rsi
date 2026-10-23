"""Critical note, accounting and replay contracts, with a mocked local provider."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe_chain_evo.common import atomic, read, sha
from formal_swe_chain.budget import Broker
from evoskill_swe.trial import TrialRunner
from conftest import configuration, REPO
from within_chain import notes
from within_chain.agents import KiraNotes, append_note
from swe_chain_evo.agents import KiraAudited


def fixture(root, failure_only=False, response='Inspect the current API before changing callers.'):
    arm = 'failure-only-notes' if failure_only else 'experience-notes'
    cfg = configuration(arm)
    cfg['spend']['ledger_path'] = str(root / 'mock-spend.sqlite')
    cfg['runtime']['model_transport_retries'] = 0
    atomic(root / 'configuration.json', cfg)
    (root / 'initial').mkdir()
    note = root / 'initial/note.md'
    note.write_text('Earlier useful lesson.')
    (root / 'tokenizer.json').symlink_to(REPO / 'configs/gpt/tokenizer.json')
    stream = [{'task': f'task-{i}', 'instruction': f'public instruction {i}'} for i in range(15)]
    rows = []
    for i in range(8):
        result = root / f'public/{i}/result.json'
        atomic(result, {'private_result_field': 'DO_NOT_EXPOSE_PRIVATE_RESULT'})
        atomic(result.parent / 'agent/trajectory.json', {'commands': 'past-only ' * 10000 + f' task-{i}'})
        rows.append({'index': i, 'task': stream[i]['task'], 'reward': i % 2,
                     'result': str(result), 'result_sha256': sha(result), 'exception': None})
    sent = []
    def provider(payload):
        sent.append(payload)
        return {'id': f'offline-{len(sent)}', 'model': cfg['model']['id'],
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': response}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'cost': 0.}}
    broker = Broker(root, 'offline-fake-key', cfg, sender=provider)
    runner = SimpleNamespace(root=root, config=cfg, stream=stream, broker=broker)
    return runner, arm, rows, note, sent


def test_note_generation_uses_only_current_public_window_and_is_charged_once(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path)
    out = tmp_path / 'arms' / arm / 'steps/000003/update'
    value = notes.update(runner, arm, 3, rows[:4], note, out)
    assert value['status'] == 'generated' and value['deployed_changed']
    assert value['selected_indices'] == [2, 3] and len(sent) == 1
    body = sent[0]
    assert body['model'] == runner.config['model']['id']
    assert body['reasoning'] == {'effort': 'high'}
    assert 'top_p' not in body and 'temperature' not in body
    assert body['max_tokens'] == 65536 and body['provider']['only'] == ['openai']
    payload = json.loads(body['messages'][1]['content'])
    assert [t['task_id'] for t in payload['completed_tasks']] == ['task-2', 'task-3']
    assert 'DO_NOT_EXPOSE_PRIVATE_RESULT' not in json.dumps(body)
    assert all(len(t['trace_excerpt']) < 31000 for t in payload['completed_tasks'])
    receipt = read(tmp_path / 'provider-calls' / value['call_ids'][0] / 'request.json')
    assert receipt['phase'] == 'analysis' and receipt['arm'] == arm
    assert value['input_tokens'] == 100 and value['output_tokens'] == 30
    assert notes.update(runner, arm, 3, rows[:4], note, out) == value
    assert len(sent) == 1


def test_failure_only_uses_failed_formal_traces(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path, failure_only=True)
    value = notes.update(runner, arm, 3, rows[:4], note, tmp_path / 'update')
    payload = json.loads(sent[0]['messages'][1]['content'])
    assert value['selected_indices'] == [2]
    assert [t['reward'] for t in payload['completed_tasks']] == [0]


def test_failure_only_skips_all_passed_without_any_provider_scope(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path, failure_only=True)
    for row in rows:
        row['reward'] = 1
    value = notes.update(runner, arm, 3, rows[:4], note, tmp_path / 'update')
    assert value['status'] == 'skipped_no_failure' and value['call_ids'] == []
    assert not sent and not runner.broker.scopes and value['note'] == str(note)


def test_oversize_unicode_note_is_bounded_without_a_second_model_call(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path, response='检查仓库变化，验证推断。' * 5000)
    value = notes.update(runner, arm, 3, rows[:4], note, tmp_path / 'update')
    assert value['truncated'] and value['note_tokens'] <= 2048 and len(sent) == 1
    assert 'truncated' in Path(value['note']).read_text()


def test_empty_generation_retains_old_note_but_accounts_for_the_call(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path, response='')
    value = notes.update(runner, arm, 3, rows[:4], note, tmp_path / 'update')
    assert value['status'] == 'generation_failed' and value['note'] == str(note)
    assert not value['deployed_changed'] and len(value['call_ids']) == 1 and len(sent) == 1


@pytest.mark.parametrize('choices', [[None], [{'message': 'invalid'}], {'message': 'invalid'}])
def test_malformed_decoded_response_retains_note_and_paid_receipt(tmp_path, monkeypatch, choices):
    runner, arm, rows, note, sent = fixture(tmp_path)
    execute = runner.broker.execute
    def decoded(token, payload):
        raw = execute(token, payload)
        raw['choices'] = choices
        return raw
    monkeypatch.setattr(runner.broker, 'execute', decoded)
    value = notes.update(runner, arm, 3, rows[:4], note, tmp_path / 'update')
    assert value['status'] == 'generation_failed' and value['note'] == str(note)
    assert value['input_tokens'] == 100 and value['output_tokens'] == 30
    assert len(value['call_ids']) == len(sent) == 1


def test_interrupted_update_recovers_saved_paid_response_without_replay(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path)
    out = tmp_path / 'update'
    first = notes.update(runner, arm, 3, rows[:4], note, out)
    (out / 'outcome.json').unlink()  # Simulated crash after recording the response and note.
    resumed = notes.update(runner, arm, 3, rows[:4], note, out)
    assert len(sent) == 1 and first['call_ids'] == resumed['call_ids']
    assert first['note_sha256'] == resumed['note_sha256']


def test_partial_paid_scope_is_retained_without_blind_replay(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path)
    out = tmp_path / 'update'
    notes.update(runner, arm, 3, rows[:4], note, out)
    (out / 'outcome.json').unlink()
    next(iter(runner.broker.scopes.values()))['blocked'] = True
    with pytest.raises(RuntimeError, match='partial paid'):
        notes.update(runner, arm, 3, rows[:4], note, out)
    assert len(sent) == 1


def test_nonchronological_or_infrastructure_support_is_rejected_before_generation(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path)
    with pytest.raises(ValueError, match='chronological'):
        notes.update(runner, arm, 3, rows[:3] + [rows[4]], note, tmp_path / 'update')
    rows[3]['exception'] = 'ContainerError'
    with pytest.raises(ValueError, match='normal scored'):
        notes.update(runner, arm, 3, rows[:4], note, tmp_path / 'update')
    assert not sent


def test_later_update_inherits_previous_note_and_only_new_window(tmp_path):
    runner, arm, rows, note, sent = fixture(tmp_path)
    first = notes.update(runner, arm, 3, rows[:4], note, tmp_path / 'update-first')
    second = notes.update(runner, arm, 7, rows, first['note'], tmp_path / 'update-second')
    payload = json.loads(sent[-1]['messages'][1]['content'])
    assert payload['previous_note'] == Path(first['note']).read_text()
    assert [t['task_id'] for t in payload['completed_tasks']] == ['task-6', 'task-7']
    assert len(sent) == 2 and second['execution']['extra_task_rollouts'] == 0


@pytest.mark.parametrize('text', ['', 'Verify assumptions against this version.'])
def test_real_note_adapter_passes_note_to_fixed_kira(tmp_path, monkeypatch, text):
    path = tmp_path / 'note.md'
    path.write_text(text)
    monkeypatch.setenv('WITHIN_CHAIN_NOTE', str(path))
    monkeypatch.setenv('WITHIN_CHAIN_NOTE_SHA256', sha(path))
    seen = []
    async def parent(self, instruction, environment, context):
        seen.append(instruction)
    monkeypatch.setattr(KiraAudited, 'run', parent)
    agent = KiraNotes.__new__(KiraNotes)
    agent.logs_dir = tmp_path
    asyncio.run(agent.run('original task', object(), None))
    assert seen == [append_note('original task', text)]
    assert read(tmp_path / 'note-injection.json')['note_sha256'] == sha(path)
    if not text:
        assert seen == ['original task']


def test_task_command_uses_same_limits_with_only_note_adapter_added(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv('RSI_DOCKER_BINARY', sys.executable)  # Docker dispatch is mocked below.
    from harbor.models.task.task import Task
    from evoskill_swe import trial as module
    arm = 'experience-notes'
    config = configuration(arm)
    task = tmp_path / 'task'
    (task / 'environment').mkdir(parents=True)
    (task / 'tests').mkdir()
    (task / 'tests/test.sh').write_text('#!/bin/sh\nexit 0\n')
    (task / 'instruction.md').write_text('Implement public API.')
    (task / 'task.toml').write_text('version = "1.0"\n[environment]\ndocker_image = "offline:test"\n')
    entry = {'task': 'task-0', 'path': str(task), 'task_checksum': Task(task).checksum,
             'image_id': 'sha256:offline', 'chain_id': 'offline-chain', 'version_chain_index': 0}
    atomic(tmp_path / 'task-stream.json', [entry])
    atomic(tmp_path / 'entries/0.json', entry)
    note = tmp_path / 'note.md'
    note.write_text('Use a reusable lesson.')
    broker = SimpleNamespace(config=config, scope=lambda *a, **k: 'offline-local-token')
    runner = TrialRunner(tmp_path, broker, 0)
    runner.check_pins = lambda: None
    runner._collect = lambda *a: {'collected': True}
    seen = []
    def fake_run(args, **kwargs):
        if args[0] == 'docker':
            return SimpleNamespace(stdout=runner.stream[0]['image_id'] + '\n', returncode=0)
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', fake_run)
    assert runner._run_once(arm, 0, tmp_path / 'formal', note=note) == {'collected': True}
    cmd, kw = seen[0]
    assert cmd[cmd.index('--agent-import-path') + 1] == 'within_chain.agents:KiraNotes'
    assert kw['env']['WITHIN_CHAIN_NOTE_SHA256'] == sha(note)
    assert kw['env']['WITHIN_CHAIN_NOTE'] == str(note)
    assert 'max_turns=100' in cmd
    assert cmd[cmd.index('--agent-timeout') + 1] == '7200'
    assert read(tmp_path / 'formal/launch.json')['trial_identity']['source']['note_sha256'] == sha(note)
