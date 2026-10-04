"""Independent synthetic acceptance, never formal target data."""
import argparse
import json
from pathlib import Path

from rsi_harness.clean_rsi import digest, initialize, read_json
from rsi_harness.terminal_infra import atomic_json
from . import campaign as c, memory


def synthetic_sources(root):
    # This public synthetic fixture is intentionally unrelated to the target tasks.
    entries = []
    for i in range(2):
        chain = f'synthetic-source-{i}'
        folder = root / chain
        store = folder / 'state'
        initialize(store, 'skill-rsi')
        memory.meta.commit(store, 'skill-rsi', {'json-output-check':
            '# Check JSON output\n## When\nThe task explicitly requires JSON.\n## Steps\nParse the actual output using json.load, then assert the required keys and values.\n## Cautions\nDo not impose JSON on plain-text tasks. Parsing alone does not verify the requested values.'},
            chain, 1)
        trial = folder / 'trial'
        atomic_json(trial / 'agent/trajectory.json', {'steps': [
            {'source': 'agent', 'message': 'The requested output is JSON. I wrote a file, parsed it with Python json.load, and asserted the required keys and values.'},
            {'source': 'agent', 'message': 'Public check: json.load succeeded; the required schema and values assertion passed. Parsing alone would not check the required values.'}]})
        atomic_json(trial / 'result.json', {'synthetic': True})
        atomic_json(folder / 'record.json', {'status': 'complete', 'result': str(trial / 'result.json'), 'reward': 1})
        entries.append({'chain': chain, 'store': str(store), 'tasks': [{'task': chain,
            'record': str(folder / 'record.json'), 'instruction': str(c.PROJECT / 'benchmarks/smoke/clean-rsi-a/instruction.md')}]})
    return memory.collect_sources(entries)[0]


def run(root, port):
    if (root / 'acceptance.json').exists():
        raise RuntimeError('use a fresh numbered acceptance directory')
    with c.legacy.exclusive(c.PROJECT / 'runs' / f'meta-port-{port}.lock'), c.legacy.exclusive(root / 'acceptance.lock'):
        if not c.legacy.endpoint_idle(port):
            raise RuntimeError('endpoint occupied')
        code = digest(c.code_snapshot())
        source = synthetic_sources(root / 'source-fixtures')
        results = []
        for arm in memory.ARMS:
            bank = root / 'banks' / arm
            update = memory.consolidate(bank, arm, source, f'http://127.0.0.1:{port}/v1')
            frozen = memory.freeze(bank)
            if not update['accepted']:
                atomic_json(root / 'progress.json', {'formal': False, 'arm': arm, 'consolidation': update})
                raise RuntimeError('synthetic consolidation did not accept:' + update['reason'])
            result = c.execute_trial(root / 'smoke' / arm, c.PROJECT / 'benchmarks/smoke/clean-rsi-b',
                                     'skill-rsi', bank / 'state', port, frozen['version'], smoke=True)
            result.update(arm=arm, consolidation=update, unchanged=memory.verify_frozen(bank) == frozen)
            results.append(result)
            atomic_json(root / 'progress.json', {'formal': False, 'results': results})
        # Exercise the exact common target trial path with no memory as well.
        result = c.execute_trial(root / 'smoke/baseline', c.PROJECT / 'benchmarks/smoke/clean-rsi-b',
                                 'baseline', root / 'unused', port, None, smoke=True)
        results.append({**result, 'arm': 'baseline'})
        passed = all(r['reward'] == 1 for r in results) and all(
            r.get('unchanged') and r['selected_skills'] and r['consolidation']['accounting']['calls'] <= 3
            for r in results if r['arm'] != 'baseline') and digest(c.code_snapshot()) == code
        report = {'formal': False, 'passed': passed, 'code_sha256': code, 'results': results,
                  'scope': 'real-model source consolidation, bank consumption and immutability on synthetic tasks; NOT transfer efficacy'}
        atomic_json(root / 'acceptance.json', report)
        print(json.dumps({'passed': passed, 'rewards': [r['reward'] for r in results]}), flush=True)
        return 0 if passed else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--port', type=int, default=8042)
    a = p.parse_args()
    raise SystemExit(run(a.root.resolve(), a.port))
