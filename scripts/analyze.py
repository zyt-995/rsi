"""Summarize recorded session metrics; also renders historical exported results."""
import argparse
import csv
import json
from pathlib import Path

from _bootstrap import REPO


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', type=Path)
    p.add_argument('--input', type=Path, default=REPO / 'results/deepseek/metrics.json')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    metadata = {}
    if args.session:
        m = json.loads((args.session / 'manifest.json').read_text())
        roots = [Path(m['reuse_controls'][0]['root'])] + [Path(r['root']) for r in m['runs']]
        rows = []
        for root in roots:
            report = json.loads((root / 'metrics.json').read_text())
            for arm in report['arms']:
                rows.append({'method': arm['method'], 'completed': arm['completed'], 'passed': arm['passed'],
                    'step_accuracy': arm.get('step_accuracy'), 'chain_accuracy': arm.get('chain_accuracy'),
                    'task_tokens': sum(arm['task_tokens'].values()), 'rsi_tokens': sum(arm['rsi_tokens'].values()),
                    'total_tokens': sum(arm['total_tokens'].values()), 'known_cost_usd': arm['cost_usd'],
                    'usage_complete': arm.get('usage_complete'), 'unknown_calls': len(arm.get('unknown_usage_calls', []))})
    else:
        report = json.loads(args.input.read_text())
        rows = report['methods']
        metadata = {k: v for k, v in report.items() if k != 'methods'}
    fields = ['method', 'completed', 'passed', 'step_accuracy', 'chain_accuracy', 'task_tokens', 'rsi_tokens', 'total_tokens', 'known_cost_usd', 'unknown_calls', 'unknown_token_upper_bound']
    with (args.output / 'summary.csv').open('w') as f:
        writer = csv.DictWriter(f, fields, lineterminator='\n'); writer.writeheader()
        writer.writerows({k: r.get(k) for k in fields} for r in rows)
    (args.output / 'observations.json').write_text(json.dumps({'metadata': metadata, 'methods': rows}, indent=2) + '\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    for metric in ['task_tokens', 'total_tokens']:
        fig, ax = plt.subplots(figsize=(8, 5))
        for row in rows:
            accuracy = row.get('step_accuracy')
            if accuracy is None:
                continue
            x, y = row[metric] / 1e6, 100 * accuracy
            ax.scatter(x, y); ax.annotate(row['method'], (x, y), xytext=(5, 5), textcoords='offset points', fontsize=8)
        ax.set(xlabel=metric.replace('_', ' ').title() + ' (million)', ylabel='Step Accuracy (%)',
               title='Recorded accuracy–token observations (not a matched-budget sweep)')
        ax.grid(alpha=.2)
        fig.text(.02, .01, 'Known tokens only; see observations.json for missing usage and cohort limitations.', fontsize=8)
        fig.tight_layout(rect=(0, .04, 1, 1)); fig.savefig(args.output / (metric + '.png'), dpi=180); plt.close(fig)
    print(args.output / 'summary.csv')


if __name__ == '__main__':
    main()
