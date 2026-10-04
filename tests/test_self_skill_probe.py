import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_probe import agent, campaign, guardian


def fake_agent():
    a = object.__new__(agent.BudgetAwareAgent)
    a.rsi_calls = 15
    a.rsi_commands = 12
    a.rsi_stop_reason = None
    a._n_episodes = a._max_episodes = 15
    a._trajectory_steps = []
    a.event = lambda *args, **kwargs: None
    return a


def test_notice_counts_current_call_and_preserves_cap():
    assert '15 model calls remaining' in agent.budget_notice(0, 0)
    assert '1 model calls remaining' in agent.budget_notice(14, 119)
    assert 'INCLUDING this response' in agent.budget_notice(14, 119)


def test_prompt_disclosure_does_not_change_instruction_for_retrieval(monkeypatch):
    a = fake_agent()
    a.rsi_calls = 4
    calls = []
    async def handle(self, chat, prompt, original_instruction, session):
        calls.append((prompt, original_instruction))
        return 'ok'
    monkeypatch.setattr(agent.CleanTerminalAgent, '_handle_llm_interaction', handle)
    assert asyncio.run(a._handle_llm_interaction(None, 'terminal-output', 'task-only')) == 'ok'
    assert calls[0][0].endswith('terminal-output')
    assert calls[0][1] == 'task-only'
    assert a.rsi_calls == 4


@pytest.mark.parametrize('marks,reason', [([], 'budget_exhausted'),
    ([True], 'budget_exhausted'), ([True, False], 'budget_exhausted'),
    ([True, True], 'agent_confirmed_complete')])
def test_normal_loop_limit_gets_explicit_reason(monkeypatch, marks, reason):
    a = fake_agent()
    a._trajectory_steps = [SimpleNamespace(source='agent', tool_calls=(
        [SimpleNamespace(function_name='mark_task_complete')] if mark else [])) for mark in marks]
    async def loop(*args, **kwargs):
        pass
    monkeypatch.setattr(agent.CleanTerminalAgent, '_run_agent_loop', loop)
    asyncio.run(a._run_agent_loop())
    assert a.rsi_stop_reason == reason


def test_existing_exception_reason_not_overwritten(monkeypatch):
    a = fake_agent()
    async def loop(*args, **kwargs):
        a.rsi_stop_reason = 'command_budget_exhausted'
    monkeypatch.setattr(agent.CleanTerminalAgent, '_run_agent_loop', loop)
    asyncio.run(a._run_agent_loop())
    assert a.rsi_stop_reason == 'command_budget_exhausted'


def test_only_agent_import_and_grading_changes(monkeypatch):
    old = campaign.base.core.harbor_command
    monkeypatch.setattr(campaign.source_campaign.prior.offline, 'ORIGINAL_COMMAND',
        lambda *a: ['harbor', '--agent-import-path', 'old', '--agent-kwarg', 'max_turns=15'])
    with campaign.adapter({'grader': '/g', 'grader_sha256': 'hash'}):
        cmd = campaign.base.core.harbor_command()
        assert cmd[:5] == ['harbor', '--agent-import-path', 'self_skill_rsi_probe.agent:BudgetAwareAgent',
                          '--agent-kwarg', 'max_turns=15']
    assert campaign.base.core.harbor_command is old


def test_report_pairs_only_complete_matches_and_preserves_zeros(tmp_path):
    jobs = [{'uid': f'p{i:02d}-{a}', 'arm': a, 'repeat': i} for i in range(3) for a in ('old', 'new')]
    for e, reward in zip(jobs[:3], [0, 1, 0]):
        atomic_json(tmp_path / 'stream' / e['uid'] / 'solve/record.json', {
            'hashes': {}, 'reward': reward, 'solve_seconds': 2, 'runtime': {'stop_reason': 'budget_exhausted'},
            'result': '/evidence'})
    r = campaign.report(tmp_path, {'stream': jobs})
    assert r['scored_tasks'] == 3 and r['unique_tasks'] == 1
    assert r['paired'] == [{'repeat': 0, 'old': 0, 'new': 1, 'delta': 1}]
    assert r['arms']['old'] == {'completed': 2, 'passed': 0}


def test_guardian_keeps_complete_terminal_without_new_model_calls(tmp_path, monkeypatch):
    atomic_json(tmp_path / 'status.json', {'state': 'complete', 'time': 1000})
    monkeypatch.setattr(guardian.time, 'time', lambda: 1100)
    monkeypatch.setattr(guardian.c.base.legacy, 'alive', lambda _: False)
    monkeypatch.setattr(guardian.existing, 'endpoint', lambda _: {'healthy': True, 'idle': True})
    monkeypatch.setattr(guardian.c, 'report', lambda *a: {})
    monkeypatch.setattr(guardian, 'launch', lambda *a: pytest.fail('completed work must not relaunch'))
    _, state = guardian.tick(tmp_path, {'ports': [8040, 8041]}, {}, None)
    assert state['action'] == 'terminal' and not state['automatic_model_replay']
