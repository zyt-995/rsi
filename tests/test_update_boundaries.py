"""Regressions for the online update boundary and parsed long Harbor traces.

All solvers and meta agents here are local fakes. Fixtures are synthetic; no provider credentials or paid calls are used.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evoskill_swe import native
from evoskill_swe.execution import audit_execution
from evoskill_swe.skills import hashes
from evoskill_swe.traces import HarborTaskTrace
from src.harness import AgentTrace
from src.loop.config import LoopConfig
from src.loop.helpers import build_proposer_query
from src.schemas import AgentResponse, SkillProposerResponse, ToolGeneratorResponse


from conftest import configuration


def trace(output, result='public trajectory'):
    return AgentTrace(duration_ms=1, total_cost_usd=0., num_turns=1, usage={},
                      result=result, messages=[], is_error=False, output=output)


def runner_at(root):
    (root / 'initial/skills').mkdir(parents=True)
    return SimpleNamespace(root=root, config=configuration('evoskill'),
        stream=[{'task': f'task-{i}', 'instruction': f'public task {i}'} for i in range(12)])


@pytest.mark.parametrize('create_first', [False, True])
def test_failed_proposer_cannot_skip_the_next_chronological_window(tmp_path, monkeypatch, create_first):
    runner = runner_at(tmp_path)
    seen, meta_calls = [], []

    class Task(native.TaskAgent):
        async def run(self, query):
            self.counter += 1
            active = bool(hashes(self.workspace / '.claude/skills'))
            reward = int(self.index == 11 or (self.index == 3 and active))
            seen.append((self.index, self.phase, query))
            self.traces.append({'sequence': self.counter, 'task': query, 'phase': self.phase})
            return trace(AgentResponse(final_answer=json.dumps({'reward': reward}), reasoning='verifier'))

    class Meta:
        def __init__(self, runner, workspace, output, role, options, response_model):
            self.workspace, self.role = workspace, role

        async def run(self, query):
            meta_calls.append((runner.current_index, self.role))
            if self.role == 'skill_proposer':
                if runner.current_index != 3 or not create_first:
                    return trace(None)
                return trace(SkillProposerResponse(action='create', proposed_skill='Review changes.',
                                                   justification='Public trace gap.'))
            folder = self.workspace / '.claude/skills/review'
            folder.mkdir()
            (folder / 'SKILL.md').write_text('---\nname: review\ndescription: Review changes.\n---\nrule\n')
            return trace(ToolGeneratorResponse(generated_skill=str(folder / 'SKILL.md'), reasoning='Reusable.'))

    monkeypatch.setattr(native, 'TaskAgent', Task)
    monkeypatch.setattr(native, 'MetaAgent', Meta)
    previous, outcomes = None, []
    for index in (3, 7, 11):
        runner.current_index = index
        out = tmp_path / 'steps' / f'{index:06d}' / 'update'
        value = asyncio.run(native.update(runner, index, previous, out))
        assert value['execution']['passed']
        assert value['execution']['executed_iterations'] == 1
        samples = [r for r in seen if r[0] == index and r[1] == 'extra_rollout']
        assert {r[2] for r in samples} == {f'task-{index-1}', f'task-{index}'}
        assert len(samples) == 2
        if previous:
            original = Path(previous) / '.evoskill/loop_checkpoint.json'
            assert (out / 'inherited-loop-checkpoint.json').read_bytes() == original.read_bytes()
        previous = value['workspace']
        outcomes.append(value)
    second_marker = Path(outcomes[1]['workspace']) / '.evoskill/loop_checkpoint.json'
    assert json.loads(second_marker.read_text())['iteration'] == (2 if create_first else 1)
    assert outcomes[1]['execution']['iterations'][0]['outcome'] == 'no_candidate'
    assert outcomes[2]['execution']['iterations'][0]['outcome'] == 'all_samples_passed'
    assert not any(i == 11 for i, role in meta_calls)
    assert outcomes[2]['skills_hashes'] == outcomes[0]['skills_hashes']
    assert native.SelfImprovingLoop.__module__ == 'src.loop.runner'


def test_new_window_archives_only_resume_marker_and_same_window_keeps_it(tmp_path):
    prior = native.prepare_workspace(tmp_path / 'prior')
    marker = prior / '.evoskill/loop_checkpoint.json'
    marker.write_text('{"iteration": 2, "category_offset": 1, "per_cat_offset": {"click": 2}}\n')
    feedback = prior / '.evoskill/feedback_history.md'
    feedback.write_text('Keep learned feedback.\n')
    frontier = prior / '.evoskill/retained-frontier.json'
    frontier.write_text('{"base": 0.5}\n')
    native.git(prior, 'branch', 'retained-learned-branch')
    before = {p.name: p.read_bytes() for p in (marker, feedback, frontier)}
    copied = native.prepare_workspace(tmp_path / 'next', previous=prior)
    assert not (copied / '.evoskill/loop_checkpoint.json').exists()
    assert (tmp_path / 'next/inherited-loop-checkpoint.json').read_bytes() == before[marker.name]
    assert (copied / '.evoskill/feedback_history.md').read_bytes() == before[feedback.name]
    assert (copied / '.evoskill/retained-frontier.json').read_bytes() == before[frontier.name]
    assert native.git(copied, 'rev-parse', 'retained-learned-branch') == native.git(prior, 'rev-parse', 'HEAD')
    assert {p.name: p.read_bytes() for p in (marker, feedback, frontier)} == before

    current_marker = copied / '.evoskill/loop_checkpoint.json'
    current_marker.write_text('{"iteration": 3}\n')
    assert native.prepare_workspace(tmp_path / 'next', previous=prior) == copied
    assert current_marker.read_text() == '{"iteration": 3}\n'


def test_silent_nominal_completion_is_blocked_before_deployment(tmp_path, monkeypatch):
    runner = runner_at(tmp_path)

    async def skipped(loop):
        loop._emit('loop_done', best='base', best_score=0., iterations=1)
        return SimpleNamespace(best_program='base', iterations_completed=1)

    monkeypatch.setattr(native.SelfImprovingLoop, 'run', skipped)
    out = tmp_path / 'update'
    with pytest.raises(RuntimeError, match='observed 0'):
        asyncio.run(native.update(runner, 3, None, out))
    check = json.loads((out / 'execution-check.json').read_text())
    assert check['passed'] is False and check['executed_iterations'] == 0
    assert not (out / 'outcome.json').exists()
    assert not (out / 'deployed-skills').exists()


def test_cached_unverified_outcome_cannot_be_relabelled_as_repaired(tmp_path):
    out = tmp_path / 'update'
    out.mkdir()
    (out / 'outcome.json').write_text('{"iterations_completed": 1}\n')
    before = (out / 'outcome.json').read_bytes()
    with pytest.raises(RuntimeError, match='lacks execution evidence'):
        asyncio.run(native.update(None, 3, None, out))
    assert (out / 'outcome.json').read_bytes() == before


def test_sample_check_rejects_missing_duplicate_and_future_samples():
    cfg = LoopConfig(max_iterations=1, samples_per_category=2)
    def report(questions):
        events = [{'event': 'iter_start', 'data': {'iteration': 1}}]
        events += [{'event': 'sample', 'data': {'question': q, 'passed': True}} for q in questions]
        events += [{'event': 'loop_done', 'data': {'iterations': 1}}]
        return audit_execution(events, cfg, ['past-a', 'past-b'])
    assert report(['past-a', 'past-b'])['passed']
    for questions in (['past-a'], ['past-a', 'past-a'], ['past-a', 'future']):
        assert not report(questions)['passed']


def test_parsed_reward_failure_shrinks_only_fallback_view():
    original = trace(AgentResponse(final_answer='{"reward":0}', reasoning='verified'),
                     'BEGIN' + ('long public trajectory ' * 15_000) + 'END')
    converted = HarborTaskTrace.model_validate(original.model_dump())
    before = converted.model_dump()
    assert converted.summarize() == original.summarize()
    medium = converted.summarize(head_chars=20_000, tail_chars=10_000)
    short = converted.summarize(head_chars=5_000, tail_chars=2_000)
    assert len(short) < len(medium) < len(converted.summarize())
    assert 'chars omitted' in medium and 'Parse Error:' not in medium
    assert 'BEGIN' in short and 'END' in short
    assert converted.model_dump() == before and converted.parse_error is None


def test_long_failures_reduce_query_without_mutating_raw_traces():
    raw = trace(AgentResponse(final_answer='{"reward":0}', reasoning='verified'),
                'BEGIN' + 'public execution ' * 50_000 + 'END').model_dump_json()
    old = AgentTrace[AgentResponse].model_validate_json(raw)
    fixed = HarborTaskTrace.model_validate_json(raw)
    snapshots = fixed.model_dump()
    queries = [build_proposer_query([(fixed, fixed.output.final_answer, '1.0', 'click')],
                'No previous attempts.', truncation_level=i) for i in (0, 1, 2)]
    assert queries[0] == build_proposer_query([(old, old.output.final_answer, '1.0', 'click')],
                                            'No previous attempts.', truncation_level=0)
    assert len(queries[0]) > len(queries[1]) > len(queries[2])
    assert fixed.model_dump() == snapshots


def test_new_and_cached_task_traces_both_use_reduced_harbor_view(tmp_path):
    workspace = native.prepare_workspace(tmp_path / 'workspace-owner')
    result = tmp_path / 'public-trial/result.json'
    (result.parent / 'agent').mkdir(parents=True)
    result.write_text('{}')
    (result.parent / 'agent/skill-discovery.json').write_text(json.dumps({'skills': [], 'invalid_skills': []}))
    (result.parent / 'agent/trajectory.json').write_text(json.dumps({'text': 'trace ' * 50_000}))
    row = {'result': str(result), 'reward': 0, 'cost_usd': 0., 'call_ids': [],
           'input_tokens': 0, 'output_tokens': 0, 'result_sha256': 'offline'}
    runner = SimpleNamespace(config={'model': {'id': 'offline'}},
        stream=[{'task': 'past', 'instruction': 'public instruction'}], run=lambda *a, **k: row)
    out = tmp_path / 'update'
    for _ in range(2):
        agent = native.TaskAgent(runner, 0, workspace, out)
        value = asyncio.run(agent.run('past'))
        assert isinstance(value, HarborTaskTrace) and value.parse_error is None
        assert len(value.summarize(head_chars=5_000, tail_chars=2_000)) < 10_000
        assert len(value.result) > 250_000
        archived = json.loads((out / 'rollouts/0001-extra_rollout/native-trace.json').read_text())
        assert archived['result'] == value.result and archived['parse_error'] is None


def test_official_loop_uses_reduced_fallback_then_native_generation_and_selection(tmp_path, monkeypatch):
    runner = runner_at(tmp_path)
    sizes = []

    class Task(native.TaskAgent):
        async def run(self, query):
            self.counter += 1
            reward = int(bool(hashes(self.workspace / '.claude/skills')))
            self.traces.append({'sequence': self.counter, 'task': query, 'phase': self.phase})
            original = trace(AgentResponse(final_answer=json.dumps({'reward': reward}), reasoning='verifier'),
                             'BEGIN' + 'public execution ' * 20_000 + 'END')
            return HarborTaskTrace.model_validate(original.model_dump())

    class Meta:
        def __init__(self, runner, workspace, output, role, options, response_model):
            self.workspace, self.role = workspace, role

        async def run(self, query):
            if self.role == 'skill_proposer':
                sizes.append(len(query))
                if len(query) > 100_000:
                    return trace(None)
                return trace(SkillProposerResponse(action='create', proposed_skill='Review.', justification='Gap.'))
            folder = self.workspace / '.claude/skills/review'
            folder.mkdir()
            (folder / 'SKILL.md').write_text('---\nname: review\ndescription: Review.\n---\nrule\n')
            return trace(ToolGeneratorResponse(generated_skill=str(folder / 'SKILL.md'), reasoning='Reusable.'))

    monkeypatch.setattr(native, 'TaskAgent', Task)
    monkeypatch.setattr(native, 'MetaAgent', Meta)
    value = asyncio.run(native.update(runner, 3, None, tmp_path / 'update'))
    assert len(sizes) == 2 and sizes[0] > 600_000 and sizes[1] < 70_000
    assert value['best_program'] == 'iter-skill-1'
    assert value['execution']['iterations'][0]['outcome'] == 'candidate_evaluated'
    assert len(value['rollouts']) == 6
