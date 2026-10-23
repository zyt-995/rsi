"""Freeze and check the common Terminal-Bench-Evo comparison contract.

This tool is deliberately separate from the running Self-Skill campaign. It
reads its sealed manifest but never changes its state or launches model calls.
Method adapters must emit a run manifest and a call-level ledger that pass
``check-run`` before their results are combined.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from rsi_portable.paths import RUNTIME as PROJECT
DEFAULT_SOURCE = PROJECT / 'legacy-terminal-source'
DEFAULT_CONTRACT = PROJECT / 'methods/comparison.json'
METHODS = {
    'meta-harness': PROJECT / 'methods/vendor/meta-harness',
    'modularrsi': PROJECT / 'methods/vendor/ModularRSI',
}
COMPARISON_START_UID = 's000127'
RECOVERY_VERIFIER = 'terminal_offline_recover.harbor:RecoveryVerifier'


def read(path: Path) -> dict:
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    return digest(path.read_bytes())


def canonical_digest(value: object) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode())


def git_head(path: Path) -> str:
    return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()


def stream_fields(entry: dict) -> dict:
    return {key: entry[key] for key in ('uid', 'chain', 'version', 'task', 'path',
                                        'task_checksum', 'environment_fingerprint',
                                        'image_id')}


def registered_grader(manifest: dict, entry: dict) -> dict:
    """Read the frozen grader registered by the source campaign for a task."""
    source = Path(manifest['capacity_repair']['source'])
    receipt_path = source / 'environment-ready' / (entry['chain'] + '.json')
    receipt = read(receipt_path)
    acceptance_path = Path(receipt['acceptance'])
    if file_digest(acceptance_path) != receipt['sha256']:
        raise ValueError('environment_acceptance_changed:' + entry['chain'])
    accepted = read(acceptance_path)
    if not accepted['passed'] or entry['task'] not in {e['task'] for e in accepted['entries']}:
        raise ValueError('task_not_registered_with_grader:' + entry['task'])
    grader_path = Path(accepted['grader'])
    if file_digest(grader_path) != accepted['grader_sha256']:
        raise ValueError('grader_manifest_changed:' + entry['chain'])
    return {'verifier_import': RECOVERY_VERIFIER,
            'grader_manifest': str(grader_path),
            'grader_sha256': accepted['grader_sha256']}


def validate_stream(stream: list[dict]) -> None:
    if not stream:
        raise ValueError('empty_stream')
    seen_uids, seen_tasks, versions = set(), set(), {}
    for entry in stream:
        uid, chain, task, version = (entry[key] for key in ('uid', 'chain', 'task', 'version'))
        if uid in seen_uids or task in seen_tasks:
            raise ValueError('duplicate_uid_or_task:' + uid)
        expected = versions.get(chain, 0)
        if version != expected:
            raise ValueError(f'nonsequential_version:{chain}:{version}:expected={expected}')
        versions[chain] = expected + 1
        seen_uids.add(uid)
        seen_tasks.add(task)
        if not Path(entry['path']).is_dir():
            raise ValueError('missing_task_path:' + task)


def snapshot(source: Path) -> dict:
    manifest_path = source / 'manifest.json'
    seal = read(source / 'seal.json')
    manifest_sha = file_digest(manifest_path)
    if seal['manifest_sha256'] != manifest_sha:
        raise ValueError('source_manifest_seal_mismatch')
    manifest = read(manifest_path)
    stream = [stream_fields(entry) for entry in manifest['stream']]
    validate_stream(stream)
    boundary = next((i for i, entry in enumerate(stream)
                     if entry['uid'] == COMPARISON_START_UID), None)
    if boundary != 127:
        raise ValueError('unexpected_comparison_boundary')
    for entry in stream[boundary:]:
        entry.update(registered_grader(manifest, entry))
    if manifest['heldout']:
        raise ValueError('unexpected_heldout_stream; review split before freezing')
    if manifest['model'] != 'Qwen/Qwen3.8-27B':
        raise ValueError('unexpected_active_model')
    return {
        'schema': 'rsi-method-comparison-v1',
        'source': {'root': str(source.resolve()), 'manifest_sha256': manifest_sha,
                   'protocol': manifest['schema'], 'cohort_role': manifest['request']['cohort_role']},
        'benchmark': 'Terminal-Bench-Evo',
        'model': manifest['model'],
        'updater_model': manifest['model'],
        'sampling': {'temperature': None},
        'solve': manifest['solve'],
        'verifier_timeout': manifest['verifier_timeout'],
        'stream': stream,
        'stream_sha256': canonical_digest(stream),
        'comparison_start_index': boundary,
        'scoring': {'formal_attempts_per_task': 1, 'reward_values': [0, 1],
                    'order': 'exact listed order; versions increase within each chain',
                    'update_timing': 'after immutable formal score; past tasks only',
                    'verifier': 'per-task registered grader for comparison suffix',
                    'future_task_access': False},
        'accounting': {'unit': 'provider input and output tokens in disjoint model calls or rollout aggregates',
                       'task_phases': ['formal'], 'rsi_phases': ['update', 'validation'],
                       'cache_tokens': 'reported separately; included in provider input tokens',
                       'total': 'task + rsi, including failed proposals and validation rollouts',
                       'missing_usage': 'reject; do not impute zero'},
        'methods': {
            **{name: {'source': str(path), 'source_kind': 'git', 'revision': git_head(path)}
               for name, path in METHODS.items()},
            **{name: {'source': str(source.resolve()), 'source_kind': 'sealed_run',
                      'revision': manifest_sha}
               for name in ('static', 'skill-rsi', 'self-skill')},
        },
        'limitations': ['development_seen cohort; no untouched held-out evaluation in source',
                        'first 127 tasks are common experience, not a comparable accuracy cohort',
                        'source self-skill history includes inherited records and a capacity repair at s000126',
                        'equal-total-token claim requires a separately matched more-compute baseline'],
    }


def check_contract(contract: dict) -> None:
    if contract.get('schema') != 'rsi-method-comparison-v1':
        raise ValueError('contract_schema_mismatch')
    source = Path(contract['source']['root'])
    if file_digest(source / 'manifest.json') != contract['source']['manifest_sha256']:
        raise ValueError('source_manifest_changed')
    if canonical_digest(contract['stream']) != contract['stream_sha256']:
        raise ValueError('contract_stream_changed')
    validate_stream(contract['stream'])
    boundary = contract['comparison_start_index']
    if boundary != 127 or contract['stream'][boundary]['uid'] != COMPARISON_START_UID:
        raise ValueError('comparison_boundary_changed')
    for entry in contract['stream'][boundary:]:
        if file_digest(Path(entry['grader_manifest'])) != entry['grader_sha256']:
            raise ValueError('grader_manifest_changed:' + entry['task'])
    for name, value in contract['methods'].items():
        if value['source_kind'] == 'git':
            current = git_head(Path(value['source']))
        elif value['source_kind'] == 'sealed_run':
            current = file_digest(Path(value['source']) / 'manifest.json')
        else:
            raise ValueError('unknown_method_source_kind:' + name)
        if current != value['revision']:
            raise ValueError('method_revision_changed:' + name)


def usage(call: dict, expected_phases: set[str], seen: set[str], model: str) -> tuple[int, int]:
    call_id = call.get('call_id')
    if not isinstance(call_id, str) or not call_id or call_id in seen:
        raise ValueError('missing_or_duplicate_call_id:' + str(call_id))
    seen.add(call_id)
    if call.get('phase') not in expected_phases:
        raise ValueError('incorrect_token_phase:' + call_id)
    if call.get('model') != model:
        raise ValueError('token_model_mismatch:' + call_id)
    values = (call.get('input_tokens'), call.get('output_tokens'))
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError('missing_or_invalid_token_usage:' + call_id)
    return values


def check_run(contract: dict, run: dict, ledger: dict, allow_prefix: bool = False) -> dict:
    check_contract(contract)
    if run.get('schema') != 'rsi-method-run-v1':
        raise ValueError('run_schema_mismatch')
    if run.get('method') not in contract['methods']:
        raise ValueError('unknown_method')
    if run.get('source_revision') != contract['methods'][run['method']]['revision']:
        raise ValueError('method_revision_mismatch')
    harness = run.get('initial_harness') or {}
    if not harness.get('path') or file_digest(Path(harness['path'])) != harness.get('sha256'):
        raise ValueError('initial_harness_not_pinned')
    for path, expected in (run.get('adapter_source') or {}).items():
        if file_digest(Path(path)) != expected:
            raise ValueError('adapter_source_changed:' + path)
    for key in ('model', 'updater_model', 'sampling', 'solve', 'verifier_timeout',
                'stream_sha256', 'scoring'):
        if run.get(key) != contract[key]:
            raise ValueError('run_config_mismatch:' + key)
    if ledger.get('schema') != 'rsi-call-ledger-v1':
        raise ValueError('ledger_schema_mismatch')
    rows = ledger.get('tasks')
    if not isinstance(rows, list):
        raise ValueError('missing_task_rows')
    expected = contract['stream']
    if len(rows) > len(expected) or (not allow_prefix and len(rows) != len(expected)):
        raise ValueError(f'incomplete_task_stream:{len(rows)}/{len(expected)}')
    seen_calls, seen_trials = set(), set()
    task_in = task_out = rsi_in = rsi_out = passed = 0
    eval_task_in = eval_task_out = eval_rsi_in = eval_rsi_out = 0
    eval_passed = 0
    agent_exceptions = []
    chain_rows: dict[str, list[dict]] = {}
    for index, (entry, row) in enumerate(zip(expected, rows)):
        if any(row.get(key) != entry[key] for key in ('uid', 'task', 'task_checksum')):
            raise ValueError('task_order_or_checksum_mismatch:' + entry['uid'])
        trial = row.get('formal_trial_id')
        if not isinstance(trial, str) or not trial or trial in seen_trials:
            raise ValueError('missing_or_duplicate_formal_trial:' + entry['uid'])
        seen_trials.add(trial)
        if row.get('reward') not in (0, 1) or row.get('status') != 'complete':
            raise ValueError('missing_official_score:' + entry['uid'])
        result_path = Path(row['formal_result'])
        if file_digest(result_path) != row.get('formal_result_sha256'):
            raise ValueError('formal_result_changed:' + entry['uid'])
        result = read(result_path)
        agent = result['config']['agent']
        verifier = result['config']['verifier']
        raw_reward = (result.get('verifier_result') or {}).get('rewards', {}).get('reward')
        if row.get('score_origin') == 'verifier':
            score_ok = raw_reward == row['reward']
        elif row.get('score_origin') == 'agent_exception':
            exception = result.get('exception_info') or {}
            score_ok = (raw_reward is None and row['reward'] == 0
                        and '_run_agent_phase' in exception.get('exception_traceback', ''))
            if score_ok:
                agent_exceptions.append({'uid': entry['uid'], 'task': entry['task'],
                                         'type': exception.get('exception_type')})
        else:
            score_ok = False
        if (result.get('id') != trial or result.get('task_checksum') != entry['task_checksum']
                or result.get('task_name') != entry['task'] or not score_ok
                or agent.get('model_name') != 'openai/' + contract['model']
                or verifier.get('override_timeout_sec') != contract['verifier_timeout']):
            raise ValueError('formal_result_config_mismatch:' + entry['uid'])
        kwargs = agent.get('kwargs') or {}
        if kwargs.get('temperature') != contract['sampling']['temperature']:
            raise ValueError('formal_sampling_mismatch:' + entry['uid'])
        for key in ('max_turns', 'max_output_retries'):
            if key == 'max_output_retries' and run['method'] == 'modularrsi':
                if run.get('alignment_exceptions') != [
                        'ModularRSI has its own output recovery implementation; max_output_retries is not configurable']:
                    raise ValueError('modular_output_recovery_exception_not_declared')
                continue
            if key in contract['solve'] and kwargs.get(key) != contract['solve'][key]:
                raise ValueError('formal_solver_budget_mismatch:' + entry['uid'] + ':' + key)
        if ('max_tokens' in contract['solve'] and
                (kwargs.get('llm_call_kwargs') or {}).get('max_tokens') != contract['solve']['max_tokens']):
            raise ValueError('formal_solver_budget_mismatch:' + entry['uid'] + ':max_tokens')
        if index >= contract['comparison_start_index']:
            for key in ('verifier_import', 'grader_sha256', 'image_id'):
                if row.get(key) != entry[key]:
                    raise ValueError('formal_environment_mismatch:' + entry['uid'] + ':' + key)
            if (verifier.get('import_path') != entry['verifier_import'] or
                    (verifier.get('kwargs') or {}).get('manifest_sha256') != entry['grader_sha256']):
                raise ValueError('formal_grader_evidence_mismatch:' + entry['uid'])
            eval_passed += row['reward']
            chain_rows.setdefault(entry['chain'], []).append(row)
        model_calls, commands = row.get('model_calls'), row.get('commands')
        if type(model_calls) is not int or not 1 <= model_calls <= contract['solve']['max_model_calls']:
            raise ValueError('task_call_budget_exceeded:' + entry['uid'])
        if (index >= contract['comparison_start_index'] or commands is not None) and (
                type(commands) is not int or not 0 <= commands <= contract['solve']['max_commands']):
            raise ValueError('task_command_budget_exceeded:' + entry['uid'])
        calls = row.get('task_calls')
        if not isinstance(calls, list) or not calls:
            raise ValueError('missing_formal_calls:' + entry['uid'])
        recorded_input = recorded_output = 0
        for call in calls:
            tin, tout = usage(call, {'formal'}, seen_calls, contract['model'])
            task_in += tin
            task_out += tout
            if index >= contract['comparison_start_index']:
                eval_task_in += tin
                eval_task_out += tout
            recorded_input += tin
            recorded_output += tout
        agent_result = result.get('agent_result') or {}
        if (recorded_input != agent_result.get('n_input_tokens') or
                recorded_output != agent_result.get('n_output_tokens')):
            raise ValueError('formal_token_evidence_mismatch:' + entry['uid'])
        rsi_calls = row.get('rsi_calls')
        if not isinstance(rsi_calls, list):
            raise ValueError('missing_rsi_call_list:' + entry['uid'])
        for call in rsi_calls:
            rin, rout = usage(call, {'update', 'validation'}, seen_calls,
                              contract['updater_model'])
            rsi_in += rin
            rsi_out += rout
            if index >= contract['comparison_start_index']:
                eval_rsi_in += rin
                eval_rsi_out += rout
        passed += row['reward']
    initial_calls = ledger.get('initial_rsi_calls')
    if not isinstance(initial_calls, list):
        raise ValueError('missing_initial_rsi_call_list')
    for call in initial_calls:
        rin, rout = usage(call, {'update', 'validation'}, seen_calls,
                          contract['updater_model'])
        rsi_in += rin
        rsi_out += rout
    whole_chains = {chain for chain, chain_entries in
                    ((chain, [e for e in expected if e['chain'] == chain]) for chain in chain_rows)
                    if all(e['uid'] in {r['uid'] for r in chain_rows[chain]} for e in chain_entries)}
    chains_passed = sum(all(row['reward'] == 1 for row in chain_rows[chain]) for chain in whole_chains)
    comparison_tasks = max(0, len(rows) - contract['comparison_start_index'])
    return {'tasks': len(rows), 'passed': passed,
            'complete': len(rows) == len(expected),
            'agent_exceptions_scored_zero': agent_exceptions,
            'comparison_tasks': comparison_tasks,
            'comparison_passed': eval_passed,
            'step_accuracy': eval_passed / comparison_tasks if comparison_tasks else None,
            'whole_chains': len(whole_chains), 'chains_passed': chains_passed,
            'chain_accuracy': chains_passed / len(whole_chains) if whole_chains else None,
            'task_tokens': {'input': task_in, 'output': task_out, 'total': task_in + task_out},
            'rsi_tokens': {'input': rsi_in, 'output': rsi_out, 'total': rsi_in + rsi_out},
            'total_tokens': task_in + task_out + rsi_in + rsi_out,
            'comparison_task_tokens': {'input': eval_task_in, 'output': eval_task_out,
                                       'total': eval_task_in + eval_task_out},
            'comparison_rsi_tokens': {'input': eval_rsi_in, 'output': eval_rsi_out,
                                      'total': eval_rsi_in + eval_rsi_out}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    snap = sub.add_parser('snapshot')
    snap.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    snap.add_argument('--output', type=Path, default=DEFAULT_CONTRACT)
    audit = sub.add_parser('audit')
    audit.add_argument('--contract', type=Path, default=DEFAULT_CONTRACT)
    check = sub.add_parser('check-run')
    check.add_argument('--contract', type=Path, default=DEFAULT_CONTRACT)
    check.add_argument('--run', type=Path, required=True)
    check.add_argument('--ledger', type=Path, required=True)
    check.add_argument('--allow-prefix', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'snapshot':
            contract = snapshot(args.source)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            print(json.dumps({'contract': str(args.output), 'tasks': len(contract['stream']),
                              'sha256': file_digest(args.output)}, ensure_ascii=False))
        elif args.command == 'audit':
            contract = read(args.contract)
            check_contract(contract)
            print(json.dumps({'status': 'contract_valid', 'model': contract['model'],
                              'tasks': len(contract['stream']), 'cohort': contract['source']['cohort_role'],
                              'method_revisions': {k: v['revision'] for k, v in contract['methods'].items()},
                              'scope': 'contract only; use check-run and deep_audit for actual runs',
                              'remaining': [
                                  'Equal-total-token comparison still needs a matched more-compute baseline',
                                  'Algorithm-only attribution needs corresponding static initial-harness controls',
                              ]},
                             ensure_ascii=False, indent=2))
        else:
            print(json.dumps(check_run(read(args.contract), read(args.run), read(args.ledger),
                                       allow_prefix=args.allow_prefix),
                             ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print(f'comparison_check_failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
