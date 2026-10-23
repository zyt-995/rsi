"""Exercise session assembly in a different workspace with explicitly synthetic grader receipts."""
import json
import os
from pathlib import Path
import subprocess
import sys
from conftest import REPO


def test_five_arm_configuration_and_source_pins_in_new_workspace(tmp_path):
    env = dict(os.environ, RSI_WORKSPACE=str(tmp_path / 'workspace'))
    subprocess.run([sys.executable, 'scripts/bootstrap.py'], cwd=REPO, env=env, check=True, capture_output=True)
    # These are fake receipts for testing config assembly, never real environment readiness.
    script = r'''
import sys
sys.path.insert(0, 'scripts')
import _bootstrap
from rsi_portable.paths import WORKSPACE, REPO
from rsi_portable import configure
from swe_chain_evo.common import atomic, sha, read
from swe_chain_evo import offline_go_dependencies
from within_chain.contracts import source_pins, check_pins, validate
from formal_swe_chain.campaign import source_pins as control_pins
import shutil
# Pin this interpreter as a synthetic Go binary. No grader/Go command executes in this test.
offline_go_dependencies.TOOL = __import__('pathlib').Path(sys.executable)
chain = 'synthetic-only'
folder = WORKSPACE / 'preparation/chains' / chain
stream = []
for i in range(15):
    task = 'synthetic-task-' + str(i)
    entry = dict(task=task, instance_id=task, chain_id=chain, version_chain_index=i,
                 image_id='sha256:synthetic', instruction='Public task ' + str(i))
    stream.append(entry)
    mapping = WORKSPACE / 'runs/grader-preflight' / task / 'test-id-map.json'
    atomic(mapping, {'synthetic': True})
    atomic(WORKSPACE / 'preparation/tasks' / (task + '.json'),
           dict(passed=True, oracle_reward=1, noop_reward=0,
                grader_sha256=sha(WORKSPACE / 'swe_chain_evo/grading.py'), entry=entry,
                test_id_map_sha256=sha(mapping)))
atomic(folder / 'task-stream.json', stream)
atomic(folder / 'zero-model-probe.json', {'passed': True, 'synthetic': True})
(WORKSPACE / 'data/tokenizers').mkdir(parents=True)
shutil.copy2(REPO / 'configs/gpt/tokenizer.json', WORKSPACE / 'data/tokenizers/gpt.json')
configure.MODEL = 'gpt'
configure.BUDGET = '50.00'
configure.configure('synthetic', chain, WORKSPACE / 'sessions/test', 19300)
m = configure.build()
control = __import__('pathlib').Path(m['reuse_controls'][0]['root'])
assert read(control / 'native-source-pins.json') == control_pins(control)
assert not (WORKSPACE / 'sessions/test/RUN_AUTHORIZATION.json').exists()
assert (WORKSPACE / 'sessions/test/HOLD').exists()
for row in m['runs']:
    root = __import__('pathlib').Path(row['root'])
    cfg, arm, actual = validate(root, require_ready=False)
    assert actual == stream
    assert cfg['solve']['max_agent_turns'] == 100
    assert cfg['solve']['max_input_plus_output_tokens'] is None
    assert cfg['model']['id'] == 'openai/gpt-6.1-sol'
    assert cfg['arms'][arm]['update_every_completed_tasks'] == 4
    atomic(root / 'native-source-pins.json', source_pins(root))
    check_pins(root)
    assert (root / 'initial/note.md').read_text() == ''
assert len(m['runs']) == 3
'''
    proc = subprocess.run([sys.executable, '-c', script], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert not list((tmp_path / 'workspace').rglob('provider-calls'))


def test_manifest_contains_seven_complete_chronological_chains():
    entries = json.loads((REPO / 'configs/benchmark/seven-chains.json').read_text())['entries']
    chains = {e['chain_id'] for e in entries}
    assert len(chains) == 7 and len({e['instance_id'] for e in entries}) == 105
    for chain in chains:
        selected = [e for e in entries if e['chain_id'] == chain]
        assert [e['version_chain_index'] for e in selected] == list(range(15))
