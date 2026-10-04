"""Versioned whole-skill edits grounded in current and retrieved past evidence.

The updater is fixed. No harness, weights, evaluator or update-rule edits are
allowed. Generation/review cannot see native validation traces or future tasks.
"""
import copy
import json
import re
import time

from rsi_harness import meta_skill as meta
from rsi_harness.clean_rsi import digest, public_trace, read_json, select_skills
from rsi_harness.terminal_infra import atomic_json, file_hash
from skill_repair_v2.learning import phase, accounting
from . import bank

BUDGET = {'max_calls': 5, 'timeout': 1800, 'diagnose_tokens': 3072, 'propose_tokens': 8192,
          'review_tokens': 3072, 'max_upserts': 8, 'max_retirements': 16,
          'trace_chars': 24000, 'past_sources': 3, 'editable_document_chars': 16000,
          'max_prompt_chars': 100000}
SKILL = copy.deepcopy(meta.OUTPUT_SCHEMAS['propose']['properties']['upserts']['items'])
PATCH_SCHEMA = meta.object_schema({'reason': meta.STRING,
    'upserts': {'type': 'array', 'maxItems': 8, 'items': SKILL},
    'retire': {'type': 'array', 'maxItems': 16, 'items': meta.object_schema({
        'id': meta.STRING, 'reason': meta.STRING, 'evidence_ids': meta.STRINGS,
        'replaced_by': {'type': 'array', 'items': meta.STRING}})}})
REVIEW_SCHEMA = meta.object_schema({'accept': {'type': 'boolean'}, 'reason': meta.STRING,
    'unsupported_claims': {'type': 'array', 'items': meta.STRING}})
PROPOSE = """Produce ONE incremental patch to a persistent cross-chain skill library.
You may add complete skills, substantially revise a skill, merge multiple skills
into a conditional procedure, or retire obsolete skills. This is NOT a tiny-line
edit constraint. Keep useful task procedures; do not force all skills into vague
meta slogans. Scope each procedure to observable applicability conditions, include
counterexamples and a concrete public check. Generalization remains a hypothesis.
Return reason, upserts, retire using the schema. A merge is an upsert plus retire
entries whose replaced_by lists point to the surviving/new skills. Do not omit or
rewrite unrelated documents. You may edit/retire ONLY old IDs shown in FULL_OLD_SKILLS.
New IDs are allowed if absent from INVENTORY. All edits and retirements cite exact
evidence IDs provided in OBSERVATIONS. Do not invent evidence or copy task answers,
solution bodies, fixed task paths, credentials, or hidden tests into a skill.
At most 8 upserts and 16 retirements; library cap 64 documents / 128 KiB. Each
rendered document <=6000 chars. Returning an empty patch is valid.
"""
REVIEW = """Audit this entire skill patch against the literal public observations.
Check unsupported causal claims, over-generalization, copied solutions, conditions,
retirements, and conflicts with the old skills. A failure score does not prove its
cause. A locally repaired mistake need not explain final failure. Reusable specific
procedures are welcome; genericness/shortness is NOT evidence of quality. Allow
testable scoped hypotheses without treating them as established truths.
Return accept, reason, unsupported_claims. Accept only if unsupported_claims is empty.
This checks evidence/plausibility ONLY. Actual native execution is a separate gate.
"""


def source_from_record(entry, record):
    if record.get('status') != 'complete' or record.get('purpose') != 'stream':
        raise ValueError('only_valid_official_stream_attempts_may_teach')
    from pathlib import Path
    return {'uid': entry['uid'], 'task': entry['task'], 'chain': entry['chain'],
        'instruction': Path(entry['path']).joinpath('instruction.md').read_text()[:8000],
        'score': record['reward'], 'trace': public_trace(Path(record['result']).parent),
        'consumed_version': record['before']}


def diagnose(folder, source, model, url, end, transport):
    value = phase(folder, 'diagnose', meta.OUTPUT_SCHEMAS['diagnose'],
        meta.DIAGNOSE + '\nSOURCE:\n' + json.dumps({k: v for k, v in source.items() if k != 'trace'}, ensure_ascii=False)
        + '\nPUBLIC TRACE:\n' + meta.numbered_trace(source['trace']), model, url, end, 3072, transport)
    meta.diagnosis_check(value, source['trace'])
    for row in value['observations']:
        row['id'] = source['uid'] + ':' + row['id']
        row['source_uid'] = source['uid']
    return value


def history_bundle(history, source):
    """History is a verified, completed stream prefix, never a filesystem-wide scan."""
    words = set(re.findall(r'[a-z0-9]+', source['instruction'].lower()))
    ranked = sorted(history, key=lambda row: (-len(words & set(re.findall(r'[a-z0-9]+', row['source']['instruction'].lower()))),
                                              -int(row['source']['uid'][1:])))
    chosen = ranked[:BUDGET['past_sources']]
    return [{'uid': row['source']['uid'], 'chain': row['source']['chain'],
             'summary': row['diagnosis']['summary'], 'observations': row['diagnosis']['observations']}
            for row in chosen]


def context(source, diagnosis, old, history):
    selected, _ = select_skills(old, source['instruction'], limit=8, char_budget=BUDGET['editable_document_chars'])
    past = history_bundle(history, source)
    observations = diagnosis['observations'] + [x for row in past for x in row['observations']]
    ids = [o['id'] for o in observations]
    if len(ids) != len(set(ids)):
        raise ValueError('evidence_ids_not_globally_unique')
    value = {'CURRENT_SOURCE': {k: v for k, v in source.items() if k != 'trace'},
        'CURRENT_PUBLIC_TRACE': meta.numbered_trace(source['trace']), 'CURRENT_DIAGNOSIS': diagnosis['summary'],
        'PAST_SOURCES': past, 'OBSERVATIONS': observations,
        'INVENTORY': {k: doc.splitlines()[0][:160] for k, doc in old.items()},
        'FULL_OLD_SKILLS': {k: old[k] for k in selected}}
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > BUDGET['max_prompt_chars']:
        raise ValueError('evidence_context_budget_exceeded; no_silent_truncation')
    return value, text


def apply_patch(proposal, old, provenance, evidence, editable, origin):
    if not isinstance(proposal, dict) or set(proposal) != {'reason', 'upserts', 'retire'}:
        raise ValueError('patch_schema_mismatch')
    meta.nonempty(proposal['reason'], 'patch reason')
    upserts, retire = proposal['upserts'], proposal['retire']
    if not isinstance(upserts, list) or len(upserts) > 8 or not isinstance(retire, list) or len(retire) > 16:
        raise ValueError('patch_operation_budget_exceeded')
    known = {e['id']: e for e in evidence}
    docs, evidence_map = {}, {}
    # Render one at a time using the existing strict skill schema; global budget
    # is our explicit larger library budget, not the old 16-document cap.
    for item in upserts:
        if not isinstance(item, dict):
            raise ValueError('candidate_schema_mismatch')
        key = item.get('id')
        if key in docs or (key in old and key not in editable):
            raise ValueError('duplicate_or_unseen_edit')
        rendered = meta.proposal_check({'reason': proposal['reason'], 'upserts': [item]},
                                      {'observations': evidence}, {})
        docs.update(rendered)
        evidence_map[key] = {'origin': origin, 'evidence': [known[k] for k in item['evidence_ids']]}
    removed = set()
    for item in retire:
        if not isinstance(item, dict) or set(item) != {'id', 'reason', 'evidence_ids', 'replaced_by'}:
            raise ValueError('retirement_schema_mismatch')
        key = item['id']
        if key not in old or key not in editable or key in removed or key in docs:
            raise ValueError('invalid_retirement_target')
        meta.nonempty(item['reason'], 'retirement reason')
        refs = item['evidence_ids']
        if not isinstance(refs, list) or not refs or any(r not in known for r in refs):
            raise ValueError('ungrounded_retirement')
        if not isinstance(item['replaced_by'], list) or any(k not in (set(old) | set(docs)) for k in item['replaced_by']):
            raise ValueError('unknown_merge_target')
        removed.add(key)
    if any(set(x['replaced_by']) & removed for x in retire):
        raise ValueError('merge_target_is_retired')
    payload = {k: v for k, v in old.items() if k not in removed}
    payload.update(docs)
    lineage = {k: v for k, v in provenance.items() if k not in removed}
    lineage.update(evidence_map)
    return bank.validate(payload), lineage


def check_review(review):
    if (not isinstance(review, dict) or set(review) != {'accept', 'reason', 'unsupported_claims'}
            or type(review['accept']) is not bool or not isinstance(review['unsupported_claims'], list)
            or any(not isinstance(x, str) for x in review['unsupported_claims'])):
        raise ValueError('invalid_review')
    meta.nonempty(review['reason'], 'review reason')
    if review['accept'] and review['unsupported_claims']:
        raise ValueError('review_accepted_unsupported_claims')
    return review['accept']


def propose(folder, source, old, provenance, history, model, url, transport=meta.request_once):
    input_value = {'source': source, 'old': old, 'provenance': provenance, 'history': history,
                   'model': model, 'url': url}
    prior = read_json(folder / 'input.json')
    if prior is not None and prior != input_value:
        raise RuntimeError('learning_inputs_changed')
    final = read_json(folder / 'result.json')
    if final:
        return final
    atomic_json(folder / 'input.json', input_value)
    start = read_json(folder / 'started.json') or {'time': time.time(), 'budget': BUDGET}
    atomic_json(folder / 'started.json', start)
    end = start['time'] + BUDGET['timeout']
    diagnosis = diagnose(folder, source, model, url, end, transport)
    atomic_json(folder / 'diagnosis.json', diagnosis)
    bundle, text = context(source, diagnosis, old, history)
    atomic_json(folder / 'evidence.json', {'source_sha256': digest(source), 'observations': bundle['OBSERVATIONS']})
    proposal = phase(folder, 'propose', PATCH_SCHEMA, PROPOSE + '\nDATA:\n' + text,
                     model, url, end, 8192, transport)
    accepted, review = False, None
    payload, lineage = apply_patch(proposal, old, provenance, bundle['OBSERVATIONS'], bundle['FULL_OLD_SKILLS'], source['uid'])
    if payload != old:
        review = phase(folder, 'review', REVIEW_SCHEMA, REVIEW + '\nDATA:\n' + text + '\nPATCH:\n' +
                       json.dumps(proposal, ensure_ascii=False), model, url, end, 3072, transport)
        accepted = check_review(review)
        if not accepted:
            proposal = phase(folder, 'revise', PATCH_SCHEMA, PROPOSE + '\nDATA:\n' + text +
                '\nRevise this one patch using the evidence review; this is NOT a new score-guided search.\n' +
                json.dumps({'patch': proposal, 'review': review}, ensure_ascii=False), model, url, end, 8192, transport)
            payload, lineage = apply_patch(proposal, old, provenance, bundle['OBSERVATIONS'], bundle['FULL_OLD_SKILLS'], source['uid'])
            if payload != old:
                review = phase(folder, 'rereview', REVIEW_SCHEMA, REVIEW + '\nDATA:\n' + text + '\nPATCH:\n' +
                    json.dumps(proposal, ensure_ascii=False), model, url, end, 3072, transport)
                accepted = check_review(review)
    result = {'status': 'candidate' if accepted and payload != old else 'no_change', 'proposal': proposal,
        'review': review, 'payload': payload if accepted else old, 'provenance': lineage if accepted else provenance,
        'diagnosis': diagnosis, 'accounting': accounting(folder),
        'input_sha256': file_hash(folder / 'input.json'), 'evidence_sha256': file_hash(folder / 'evidence.json'),
        'selection': 'LLM evidence review only; native execution gate still required'}
    atomic_json(folder / 'result.json', result)
    return result
