"""Official ModularRSI investigation, two-lane editing and gates on past Evo.

Each of the five module types evolves independently from the same parent.
The official rebase operator combines their changes, followed by library checks,
an integration editor/reviewer and execution gates on already-completed tasks.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from methods.evo_agents import VENDOR_AGENTS
from methods.modular_update import tree_hashes
from .trial import atomic
from .spend import SpendBlocked


class NativeUpdateStop(BaseException):
    """Escape native Exception handlers while retaining infrastructure cause."""
    def __init__(self, cause):
        super().__init__(str(cause))
        self.original_type = type(cause).__name__


def resumed_scope_name(scopes, base):
    names = {scope['name'] for scope in scopes.values()}
    name = base
    suffix = 1
    while name in names:
        name = f'{base}-resume-{suffix:03d}'
        suffix += 1
    return name


def update(runner, index, parent, rows, output):
    import litellm
    from harbor.agents.terminus_2_modular.self_evo import online_evo as native
    from harbor.agents.terminus_2_modular.self_evo.task_runner import HarborTaskResult
    from harbor.agents.terminus_2_modular.self_evo.rebase import rebase_onto
    from harbor.agents.terminus_2_modular.self_evo.staging import initialize_gen_0, prepare_staging
    from harbor.agents.terminus_2_modular.self_evo.run_editor import run_editor

    output = Path(output)
    if (output/'decision.json').exists(): return json.loads((output/'decision.json').read_text())
    output.mkdir(parents=True, exist_ok=True)
    seen = {row['task']:row['index'] for row in rows}
    cfg = runner.config['arms']['modularrsi']
    batch = rows[-cfg.get('max_evolution_tasks', 10):]
    parent = Path(parent)
    atomic(output/'adaptation.json', {'source':'official ModularRSI Phase0', 'parent_hashes':tree_hashes(parent),
           'evidence_tasks':[row['task'] for row in batch], 'allowed_validation_tasks':list(seen),
           'contrastive_attempts':cfg['contrastive_attempts_per_task'], 'formal_rollout_reused':False,
           'reason':'module-locked contrastive bundles are composed independently; all three fresh rolls are RSI cost',
           'independent_module_evolution':cfg['modules'], 'external_tasks':False})
    # Original gates judge implementation breakage on a small battery. Replace
    # the hardcoded TB2 battery with completed Evo tasks, preserving the gates.
    passed = [row['task'] for row in rows if row['reward'] == 1]
    sanity_tasks = list(dict.fromkeys([*(passed[-1:] or []), batch[-1]['task']]))
    stage_scopes = {}
    original_completion = litellm.acompletion
    api_base = f'http://127.0.0.1:{runner.port}/v1'
    skills = VENDOR_AGENTS/'terminus_2_modular/self_evo/editor_skills'
    model = 'openai/' + runner.config['model']['id']

    async def completion(*args, **kwargs):
        names = ' '.join(frame.function for frame in inspect.stack())
        phase = ('review' if 'review' in names else 'analysis' if any(s in names for s in ('investigat','consolidat','evidence','diagnos'))
                 else 'integration' if 'integrate' in names else 'editing')
        if phase not in stage_scopes:
            name = resumed_scope_name(runner.broker.scopes,
                    str(output.relative_to(runner.root)) + '/' + phase)
            stage_scopes[phase] = runner.broker.scope('modularrsi', phase,
                    name)
        kwargs.update(model=model, api_base=api_base, api_key=stage_scopes[phase])
        ceiling = runner.config['model']['max_output_tokens']
        kwargs['max_tokens'] = min(kwargs.get('max_tokens') or ceiling, ceiling)
        try:
            return await original_completion(*args, **kwargs)
        except BaseException as exc:
            if getattr(exc,'status_code',None) in (402,503):
                cause = SpendBlocked(str(exc)) if exc.status_code == 402 else exc
                raise NativeUpdateStop(cause) from exc
            raise

    def run_harbor_task(task, staging_modules_dir, output_dir, **kwargs):
        if task not in seen: raise NativeUpdateStop('native validation tried an unseen task: '+task)
        out=Path(output_dir)
        phase='extra_rollout' if '/solver/' in str(out) else 'validation'
        try:
            result=runner.run('modularrsi',seen[task],out,phase=phase,modules=staging_modules_dir,
                 composer_name=kwargs.get('composer_name') or 'llm_dynamic',
                 locked_module_type=kwargs.get('locked_module_type'),
                 composer_scope=kwargs.get('composer_scope') or 'all')
        except (SpendBlocked,RuntimeError,ValueError) as exc:
            raise NativeUpdateStop(exc) from exc
        return HarborTaskResult(task=task,reward=result['reward'],error=result['exception'])

    async def evolve():
        evolved=[]
        for module_type in cfg['modules']:
            archive=output/module_type
            if (archive/'online-result.json').exists():
                data=json.loads((archive/'online-result.json').read_text())
                evolved.append(Path(data['final_gen'])/'modules'); continue
            initialize_gen_0(archive, parent)
            inherited = parent.parent.parent/'archive.json'
            if inherited.exists() and not (archive/'archive.json').exists():
                shutil.copy2(inherited, archive/'archive.json')
            outcome=await native.run_online_evo(
                archive_root=archive,tasks=[row['task'] for row in batch],
                parent_modules_for_gen0=parent,model_name=model,api_base=api_base,api_key='LOCAL_SCOPE',
                reflect_every=len(batch),task_concurrency=runner.config['runtime'].get('validation_concurrency',1),max_tasks=len(batch),epochs=cfg.get('epochs',3),
                shuffle_tasks=False,task_seed=0,max_editor_turns=cfg['max_editor_turns'],
                max_solver_turns=runner.config['solve']['max_agent_turns'],
                solver_timeout_sec=8400,agent_timeout_multiplier=1,editor_timeout_sec=7200,
                editor_call_timeout_sec=2700,review_max_turns=cfg['review_max_turns'],
                sanity_tasks=sanity_tasks,environment='docker',max_gate_repairs=cfg['max_gate_repairs'],
                sanity_concurrency=runner.config['runtime'].get('validation_concurrency',1),support_task_dir=Path(runner.stream[0]['path']).parent,
                locked_module_type=module_type,composer_scope='locked',attempts=cfg['contrastive_attempts_per_task'],
                max_lanes=cfg['max_lanes'],skills_dir=skills)
            atomic(archive/'online-result.json',{'final_gen':str(outcome.final_gen),'n_promotions':outcome.n_promotions})
            evolved.append(outcome.final_gen/'modules')
        before=tree_hashes(parent)
        if all(tree_hashes(p)==before for p in evolved):
            return {'promoted':False,'reason':'no_independent_module_promoted','candidate':None}
        integration=output/'integration'
        initialize_gen_0(integration,parent)
        inherited = parent.parent.parent/'archive.json'
        if inherited.exists() and not (integration/'archive.json').exists():
            shutil.copy2(inherited, integration/'archive.json')
        native._seed_archive(integration)
        staging=prepare_staging(parent,integration/'staging')
        current=staging
        merge_records=[]
        for module_type, source in zip(cfg['modules'],evolved):
            rebased=rebase_onto(staging=source,old_parent=parent,new_parent=current,
                               dest=integration/'rebases'/module_type/'modules')
            current=rebased.dest
            merge_records.append({'module':module_type,'moved':rebased.files_moved,'conflicts':rebased.conflicts,
                                  'source':str(source)})
        # The official editor performs the actual integration edits from the
        # unchanged parent. A preview is evidence, not an automatic promotion.
        # This preserves the native committed/no-edits decision and edit count.
        atomic(integration/'merge-records.json',merge_records)
        editor=await run_editor(staging_dir=staging,archive_path=integration,
            instruction='Integrate the independently evolved modules into this unchanged parent stage. Read the supplied source variants and the automatic rebase preview at '+str(current)+'. Apply the chosen integration using your editing tools; do not merely commit without editing. Inspect module interfaces; resolve registration and cross-module conflicts. Preserve reusable behavior, the fixed model, budgets and verifier. No task-specific answers. Run validation and emit <commit_patch/>. Merge records: '+json.dumps(merge_records),
            model_name=model,api_base=api_base,api_key='LOCAL_SCOPE',logs_dir=integration/'editor_logs',
            skills_dir=skills,trajectory_root=output,max_turns=cfg['max_editor_turns'],locked_module_type=None,
            llm_call_kwargs={'max_tokens':runner.config['model']['max_output_tokens'],'timeout':2700})
        summaries=[]
        from harbor.agents.terminus_2_modular.self_evo.trajectory_analysis import summarize_trial, changed_module_files
        for row in batch:
            summary=summarize_trial(Path(row['result']).parent)
            if summary:summaries.append(summary)
        outcome=await native._gate_and_promote(editor_outcome=editor,staging_root=staging.parent,
            staging_modules=staging,diff_base=parent,_changed=lambda stage: changed_module_files(parent, stage),
            candidate_n=1,step_id='cross_module_integration',api_base=api_base,api_key='LOCAL_SCOPE',
            archive_root=integration,current_gen=integration/'gen_0',model_name=model,
            pending_summaries=summaries,skills_dir=skills,locked_module_type=None,
            max_editor_turns=cfg['max_editor_turns'],review_max_turns=cfg['review_max_turns'],max_gate_repairs=cfg['max_gate_repairs'],environment='docker',
            sanity_tasks=sanity_tasks,sanity_concurrency=runner.config['runtime'].get('validation_concurrency',1),max_solver_turns=runner.config['solve']['max_agent_turns'],
            solver_timeout_sec=8400,editor_timeout_sec=7200,editor_call_timeout_sec=2700)
        # In this release _gate_and_promote only judges the gates. Promotion
        # belongs to the window coordinator; reuse its original staging and
        # archive operators after all gates pass.
        if outcome.gates_passed:
            from harbor.agents.terminus_2_modular.self_evo.staging import atomic_promote, next_gen_number
            promoted=atomic_promote(staging.parent,integration/f'gen_{next_gen_number(integration)}')
            native._sync_archive_after_promote(integration,promoted,parent,outcome.variant_meta_text)
            outcome.promoted_gen=promoted
        atomic(integration/'gates.json',dataclasses.asdict(outcome))
        return {'promoted':outcome.promoted_gen is not None,
                'reason':'native_integration_gates_passed' if outcome.promoted_gen else outcome.discard_reason,
                'candidate':str(outcome.promoted_gen/'modules') if outcome.promoted_gen else None,
                'independent_module_archives':[str(p.parent) for p in evolved]}

    profile={'max_input_tokens':runner.config['model']['context_tokens']-runner.config['model']['max_output_tokens'],
             'max_output_tokens':runner.config['model']['max_output_tokens'],'input_cost_per_token':0.0,'output_cost_per_token':0.0}
    # These are process-local adapter seams; upstream source files stay intact.
    env={**os.environ,'HARBOR_MODEL_INFO':json.dumps(profile),
         'PYTHONPATH':str(VENDOR_AGENTS.parent.parent),
         'OPENAI_API_BASE':api_base,'OPENAI_BASE_URL':api_base}
    with patch.dict(os.environ,env,clear=True), patch.object(litellm,'acompletion',completion), \
         patch.object(native,'run_harbor_task',run_harbor_task), patch.object(native,'DEFAULT_SANITY_TASKS',sanity_tasks):
        decision=asyncio.run(evolve())
    atomic(output/'decision.json',decision)
    return decision
