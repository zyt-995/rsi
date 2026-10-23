"""Export whitelisted task/update summaries from an external inventory, without raw conversations or credentials."""
import argparse
import csv
import json
from pathlib import Path


def export(inventory, output):
    plan = json.loads(inventory.read_text())
    rows, updates = [], []
    for chain in plan['chains']:
        for method, location in chain['methods'].items():
            root = Path(location['root'])
            statefile = root / 'native-campaign.json'
            state = json.loads(statefile.read_text())['arms'][method]
            for row in state['rows']:
                item = {k: row.get(k) for k in ('index', 'task', 'reward', 'input_tokens', 'output_tokens',
                    'cost_usd', 'usage_complete', 'result_sha256', 'grade_sha256')}
                item.update(chain=chain['chain'], method=method, metric_status=chain['metric_status'],
                    has_exception=bool(row.get('exception')), recorded_call_count=len(row.get('call_ids', [])))
                rows.append(item)
            for update in state.get('updates', []):
                item = {k: update[k] for k in ('index', 'promoted', 'candidate_sha256', 'reason', 'best_program',
                    'skills_hashes', 'iterations_completed', 'support_tasks', 'support_indices', 'selected_indices',
                    'triggered', 'note_sha256', 'note_tokens', 'deployed_changed', 'status', 'usage_complete',
                    'input_tokens', 'output_tokens', 'cost_usd') if k in update}
                execution = update.get('execution', {})
                item['execution'] = {k: execution[k] for k in ('passed', 'executed_iterations',
                    'expected_iterations', 'logical_generations', 'extra_task_rollouts') if k in execution}
                item.update(chain=chain['chain'], method=method, metric_status=chain['metric_status'])
                updates.append(item)
    output.mkdir(parents=True, exist_ok=True)
    for filename, value in [('task-results.json', rows), ('update-summaries.json', updates)]:
        (output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    if rows:
        with (output / 'task-results.csv').open('w') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator='\n'); writer.writeheader(); writer.writerows(rows)
    print(json.dumps({'tasks': len(rows), 'updates': len(updates), 'output': str(output)}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args(); export(args.inventory, args.output)
