import json
import runpy
from pathlib import Path


def test_requeue_does_not_reset_running_workers_or_completed_results(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts/supervise-terminal-full.py'
    api = runpy.run_path(str(script))
    state = api['fresh_state'](['demo'])
    work = state['work']
    work['demo/baseline'].update(status='complete', attempts=1)
    work['demo/fixed-loop'].update(status='running', attempts=1)
    work['demo/self-action'].update(status='incomplete', attempts=1)
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state))
    loaded = api['load_state'](path, ['demo'], True)['work']
    assert loaded['demo/baseline']['status'] == 'complete'
    assert loaded['demo/baseline']['attempts'] == 1
    assert loaded['demo/fixed-loop']['status'] == 'pending'
    assert loaded['demo/fixed-loop']['attempts'] == 1
    assert loaded['demo/self-action']['status'] == 'pending'
    assert loaded['demo/self-action']['attempts'] == 0
