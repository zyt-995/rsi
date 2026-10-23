"""Report only matched formal Evo tasks; charge every method call once."""
import csv
import json
import re
from pathlib import Path

from .trial import atomic

ARMS=('kira-static','meta-harness','modular-static','modularrsi')


def in_prefix(receipt, arm, formal_ids, n):
    if receipt['arm'] != arm:
        return False
    if receipt['phase'] == 'formal':
        return receipt['id'] in formal_ids
    name = receipt.get('scope', '')
    match = re.search(r'/steps/(\d+)/', name) or re.match(r'native-meta-workspace/jobs/evo_(\d{6})/', name)
    if not match:
        raise RuntimeError('unassigned RSI receipt: ' + name)
    return int(match.group(1)) < n


def report(root, state, broker):
    root=Path(root)
    n=min(len(state['arms'][a]['rows']) for a in ARMS)
    stream=json.loads((root/'task-stream.json').read_text())
    chains={}
    for i,e in enumerate(stream):chains.setdefault(e['chain'],[]).append(i)
    complete=[ids for ids in chains.values() if max(ids)<n]
    receipts=[]
    uncertain=[]
    for p in (root/'provider-calls').glob('*/request.json'):
        r=json.loads(p.read_text())
        if r['state']=='settled':receipts.append(r)
        elif r['state']=='requires_review':uncertain.append(r)
    table=[]
    for arm in ARMS:
        rows=state['arms'][arm]['rows'][:n]
        formal_ids={cid for r in rows for cid in r['call_ids']}
        charged=[r for r in receipts if r['id'] in formal_ids]
        rsi=[]
        for r in receipts:
            if r['arm']!=arm or r['phase']=='formal':continue
            name=r.get('scope','')
            if '/steps/' in name:
                index=int(name.split('/steps/',1)[1].split('/',1)[0])
            else:
                match=re.match(r'native-meta-workspace/jobs/evo_(\d{6})/',name)
                if not match:
                    raise RuntimeError('unassigned RSI receipt: '+name)
                index=int(match.group(1))
            if index<n:rsi.append(r)
        ids=[r['id'] for r in charged+rsi]
        if len(ids)!=len(set(ids)):raise RuntimeError('duplicated token receipt')
        task_in=sum(r['usage']['prompt_tokens'] for r in charged)
        task_out=sum(r['usage']['completion_tokens'] for r in charged)
        rsi_in=sum(r['usage']['prompt_tokens'] for r in rsi)
        rsi_out=sum(r['usage']['completion_tokens'] for r in rsi)
        unknown=[r for r in uncertain if in_prefix(r, arm, formal_ids, n)]
        table.append({'method':arm,'common_tasks':n,'completed_tasks':len(state['arms'][arm]['rows']),
             'step_accuracy':sum(r['reward'] for r in rows)/n if n else None,
             'complete_chains':len(complete),'chain_accuracy':sum(all(rows[i]['reward']==1 for i in ids) for ids in complete)/len(complete) if complete else None,
             'task_input_tokens':task_in,'task_output_tokens':task_out,'task_tokens':task_in+task_out,
             'rsi_input_tokens':rsi_in,'rsi_output_tokens':rsi_out,'rsi_tokens':rsi_in+rsi_out,
             'total_tokens':task_in+task_out+rsi_in+rsi_out,'cost_usd':sum(r['usage']['cost'] for r in charged+rsi),
             'usage_complete':not unknown,'uncertain_calls':len(unknown),
             'uncertain_cost_upper_usd':sum(float(r['reserved_usd']) for r in unknown)})
    value={'common_prefix':n,'rows':table,'spend':broker.ledger.snapshot(),
           'protocol':'online Evo; current-prefix updates are included in RSI cost, including incomplete candidates',
           'shared_smoke_cost_usd':sum(r['usage']['cost'] for r in receipts if r['arm']=='smoke'),
           'equal_compute_claim':False}
    value['token_totals_policy']='settled receipts only; lower bounds when usage_complete is false'
    atomic(root/'metrics.json',value)
    with (root/'metrics.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(table[0]));writer.writeheader();writer.writerows(table)
    if n:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        history=root/'accuracy-token-history.json'
        points=json.loads(history.read_text()) if history.exists() else {}
        points[str(n)]=table
        atomic(history,points)
        for axis,label in [('task_tokens','Task Tokens'),('total_tokens','Total Tokens')]:
            fig,ax=plt.subplots(figsize=(7,4.5))
            for arm in ARMS:
                values=[next(r for r in points[k] if r['method']==arm) for k in sorted(points,key=int)]
                if axis=='total_tokens':values=[r for r in values if r.get('usage_complete',True)]
                ax.plot([r[axis] for r in values],[r['step_accuracy'] for r in values],marker='o',label=arm)
            ax.set(xlabel=label,ylabel='Step Accuracy',ylim=(0,1.05),title='Terminal-Bench-Evo: common task prefix')
            ax.legend();ax.grid(alpha=.2);fig.tight_layout()
            fig.savefig(root/('accuracy-vs-'+axis.replace('_','-')+'.png'),dpi=160)
            plt.close(fig)
    return value
