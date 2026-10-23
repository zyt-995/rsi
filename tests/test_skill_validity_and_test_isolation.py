import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evoskill_swe.skills import hashes, inventory, normalize_generated, require_inventory, verify_discovery
from swe_chain_evo.go_test_isolation import declarations, isolate_sources


def skill(root, text):
    folder = root / 'record-support'
    folder.mkdir(parents=True)
    path = folder / 'SKILL.md'
    path.write_text(text)
    return path


def test_real_unquoted_description_failure_is_normalized_without_body_changes(tmp_path):
    body = '\n# RR support\n\n  Preserve indentation.\nRFC edge cases.\n'
    path = skill(tmp_path, '---\nname: record-support\ndescription: Use for plugin/file: expand RR support.\n---\n' + body)
    assert inventory(tmp_path)[1]
    result = normalize_generated(tmp_path, 'fallback should not replace model description')
    assert result['skills'][0]['description'] == 'Use for plugin/file: expand RR support.'
    assert path.read_text().split('---\n', 2)[2] == body
    assert result['normalizations'][0]['body_unchanged']
    before = path.read_bytes()
    assert normalize_generated(tmp_path, 'other')['normalizations'] == []
    assert path.read_bytes() == before


def test_valid_skill_bytes_are_never_rewritten(tmp_path):
    path = skill(tmp_path, '---\nname: record-support\ndescription: "Already valid: quoted."\n---\n\n  body\n')
    before = path.read_bytes()
    assert normalize_generated(tmp_path, 'other')['normalizations'] == []
    assert before == path.read_bytes()


def test_unrepairable_header_blocks_before_paid_rollout(tmp_path):
    from evoskill_swe.native import TaskAgent
    root = tmp_path / '.claude/skills'
    skill(root, 'not a skill header')
    calls = []
    runner = SimpleNamespace(config={'model': {'id': 'offline'}}, stream=[{'task': 'past'}],
                             run=lambda *a, **kw: calls.append((a, kw)))
    agent = TaskAgent(runner, 0, tmp_path, tmp_path / 'output')
    with pytest.raises(ValueError, match='undiscoverable'):
        asyncio.run(agent.run('past'))
    assert calls == []
    assert agent.counter == 0
    with pytest.raises(ValueError, match='malformed'):
        normalize_generated(root, 'fallback')


def test_invalid_skills_cannot_reach_kira_llm(tmp_path, monkeypatch):
    from evoskill_swe.agents import KiraSkills
    from swe_chain_evo.agents import KiraAudited
    root = tmp_path / 'skills'
    skill(root, '---\nname: record-support\ndescription: invalid: colon\n---\nbody')
    called = []
    async def solver(*a, **kw):
        called.append(True)
    monkeypatch.setattr(KiraAudited, 'run', solver)
    agent = object.__new__(KiraSkills)
    agent._host_skills_dir = root
    agent.logs_dir = tmp_path
    with pytest.raises(ValueError, match='undiscoverable'):
        asyncio.run(agent.run('task', None, None))
    assert not called
    assert json.loads((tmp_path / 'skill-discovery.json').read_text())['invalid_skills']


def test_success_score_cannot_hide_missing_or_wrong_skill_inventory(tmp_path):
    root = tmp_path / 'skills'
    skill(root, '---\nname: record-support\ndescription: valid\n---\nbody')
    result = tmp_path / 'trial/result.json'
    agent = result.parent / 'agent'
    agent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match='missing'):
        verify_discovery(result, root)
    discovery = agent / 'skill-discovery.json'
    discovery.write_text(json.dumps({'skills': [], 'invalid_skills': []}))
    with pytest.raises(RuntimeError, match='expected'):
        verify_discovery(result, root)
    discovery.write_text(json.dumps({'skills': require_inventory(root), 'invalid_skills': []}))
    assert verify_discovery(result, root)['passed']


def test_cached_deployment_cannot_bypass_skill_validity(tmp_path):
    from evoskill_swe.native import update
    skills = tmp_path / 'deployed'
    skill(skills, '---\nname: record-support\ndescription: bad: yaml\n---\nbody')
    (tmp_path / 'outcome.json').write_text(json.dumps({'execution': {'passed': True},
        'skills': str(skills), 'skills_hashes': hashes(skills)}))
    with pytest.raises(ValueError, match='undiscoverable'):
        asyncio.run(update(None, 3, None, tmp_path))


def test_go_isolation_fixes_real_test_and_helper_collisions_only_in_candidate_tests():
    private = {'plugin/transfer/transfer_test.go': '''package transfer
func TestLongestMatchMostSpecificZone(t *testing.T) {}
func udpAddr() string { return "private" }
'''}
    candidate = {'plugin/transfer/select_test.go': '''package transfer
// TestLongestMatchMostSpecificZone is the agent's regression test.
func TestLongestMatchMostSpecificZone(t *testing.T) { _ = udpAddr(); _ = other.udpAddr() }
func udpAddr() string { return "udpAddr TestLongestMatchMostSpecificZone" }
'''}
    result = isolate_sources(private, candidate, {})
    fixed = result['plugin/transfer/select_test.go']['source']
    _, names = declarations(fixed)
    assert 'TestLongestMatchMostSpecificZone' not in names
    assert 'udpAddr' not in names
    assert '// TestLongestMatchMostSpecificZone' in fixed
    assert '"udpAddr TestLongestMatchMostSpecificZone"' in fixed
    assert 'other.udpAddr()' in fixed
    assert private['plugin/transfer/transfer_test.go'].startswith('package transfer\nfunc TestLongest')


def test_go_isolation_preserves_noncolliding_and_preexisting_tests():
    private = {'p/private_test.go': 'package p\nfunc TestTarget(t *testing.T) {}'}
    candidate = {'p/agent_test.go': 'package p\nfunc TestTarget(t *testing.T) {}'}
    assert isolate_sources(private, candidate, candidate) == {}
    assert isolate_sources(private, {'other/agent_test.go': candidate['p/agent_test.go']}, {}) == {}
    assert isolate_sources(private, {'p/agent_test.go': 'package p_test\nfunc TestTarget(t *testing.T) {}'}, {}) == {}


def test_go_declarations_ignore_literals_comments_and_methods():
    source = '''package p
// func Fake() {}
var text = `func Fake2() {}`
func (x T) TestMethod() {}
func Real() { _ = "{" }
'''
    assert declarations(source) == ('p', {'Real'})


def test_normalized_skill_is_advertised_by_actual_kira_adapter(tmp_path, monkeypatch):
    from evoskill_swe.agents import KiraSkills
    from swe_chain_evo.agents import KiraAudited
    root = tmp_path / 'skills'
    skill(root, '---\nname: record-support\ndescription: Use for plugin/file: expand RR support.\n---\nBODY NOT IN INVENTORY')
    normalize_generated(root, 'fallback')
    captured = []
    async def solver(self, instruction, environment, context):
        captured.append(instruction)
        return 'offline'
    monkeypatch.setattr(KiraAudited, 'run', solver)
    agent = object.__new__(KiraSkills)
    agent._host_skills_dir = root
    agent.logs_dir = tmp_path / 'agent'
    assert asyncio.run(agent.run('task', None, None)) == 'offline'
    assert '/skills/record-support/SKILL.md' in captured[0]
    assert 'Use for plugin/file: expand RR support.' in captured[0]
    assert 'BODY NOT IN INVENTORY' not in captured[0]
    assert verify_discovery(tmp_path / 'result.json', root)['passed']


def test_go_testmain_collision_does_not_create_wrong_test_signature():
    source = 'package p\nfunc TestMain(m *testing.M) {}'
    fixed = isolate_sources({'p/private_test.go': source}, {'p/agent_test.go': source}, {})
    _, names = declarations(fixed['p/agent_test.go']['source'])
    assert all(not name.startswith('Test') for name in names)


def test_new_grader_contract_cannot_reuse_legacy_scores():
    from evoskill_swe.trial import TrialRunner
    from evoskill_swe.recovery import compatible_contract
    runner = object.__new__(TrialRunner)
    runner.config = {'model': {}, 'solve': {}}
    current = runner._execution_contract()
    legacy = {k: v for k, v in current.items() if k not in ('grader_sha256', 'go_test_isolation_sha256')}
    assert not compatible_contract(legacy, current, {'source': {'skills_hashes': {}}})
