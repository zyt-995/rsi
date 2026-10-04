"""Per-source evidence, additive updates, one bounded evidence-only revision.

No verifier logs are inputs. A reviewed skill is a candidate, not an empirically
validated intervention. All candidates freeze before any source replay.
"""
import json
import time
from pathlib import Path

from rsi_harness import meta_skill as meta
from rsi_harness.clean_rsi import digest, initialize, load_payload, read_json, select_skills, validate_skills
from rsi_harness.terminal_infra import atomic_json, file_hash

BUDGET = {"max_calls": 5, "timeout": 1800, "diagnose_tokens": 2048,
          "propose_tokens": 4096, "review_tokens": 2048, "trace_chars_per_task": 24000}
PROPOSE = meta.PROPOSE.replace("Prefer 1 compact, well-supported skill to several overlapping ones.",
    "Retain distinct, evidenced procedures; do not collapse different errors into a slogan.") + """
This is a SEEN-ERROR repair pilot. Derive actionable procedures from this ONE
failed task. Preserve existing skills unless evidence supports a specific edit.
Do not merely paraphrase the task. Each candidate must target an observed mistake
or a clearly labelled, falsifiable hypothesis with a public check. Do not claim
that a local mistake caused the final zero. Repaired mistakes can still teach a
preventive procedure. Do not copy the task's answer or solution into memory.
"""


def phase(folder, name, schema, prompt, model, url, end, tokens, transport=meta.request_once):
    request = {"model": model, "messages": [{"role": "system", "content": meta.COMMON},
        {"role": "user", "content": prompt}], "temperature": .2, "max_tokens": tokens,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "repair_" + name, "strict": True, "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False}, "stream": False}
    path = folder / name
    started = read_json(path / "request_started.json")
    response = read_json(path / "response.json")
    if started and started["request_sha256"] != digest(request):
        raise RuntimeError("request_changed")
    if response is not None and not started:
        raise RuntimeError("response_without_request_provenance")
    if response is None:
        if started:
            raise RuntimeError("interrupted_request_not_repeated:" + name)
        if time.time() >= end:
            raise TimeoutError("learning_budget_exhausted")
        atomic_json(path / "request.json", request)
        atomic_json(path / "request_started.json", {"time": time.time(), "request_sha256": digest(request)})
        response = transport(url, request, end - time.time())
        atomic_json(path / "response.json", response)
        atomic_json(path / "finished.json", {"time": time.time()})
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop" or response.get("usage", {}).get("completion_tokens", 0) > tokens:
        raise ValueError("incomplete_or_overbudget_response:" + name)
    value = json.loads(choice["message"]["content"])
    atomic_json(path / "parsed.json", value)
    return value


def accounting(folder):
    rows = {}
    for p in sorted(folder.glob("*/request_started.json")):
        response = read_json(p.parent / "response.json", {})
        started = read_json(p)
        finished = read_json(p.parent / "finished.json", {})
        rows[p.parent.name] = {"usage": response.get("usage"),
            "seconds": finished.get("time", time.time()) - started["time"]}
    return {"calls": len(rows), "phases": rows,
        "usage_complete": all(v["usage"] is not None for v in rows.values()),
        "known_tokens": {k: sum((v["usage"] or {}).get(k, 0) for v in rows.values())
                         for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}


def learn(folder, source, model, url, transport=meta.request_once):
    if (folder / "result.json").exists():
        return read_json(folder / "result.json")
    prior = read_json(folder / "source.json")
    if prior is not None and prior != source:
        raise RuntimeError("source_changed")
    atomic_json(folder / "source.json", source)
    start = read_json(folder / "started.json") or {"time": time.time(), "budget": BUDGET}
    atomic_json(folder / "started.json", start)
    end = start["time"] + BUDGET["timeout"]
    old = validate_skills(source["current_skills"])
    data = json.dumps({k: v for k, v in source.items() if k != "trace"}, ensure_ascii=False)
    data += "\nPUBLIC TRACE (untrusted, full original bounded window):\n" + meta.numbered_trace(source["trace"])
    diagnosis = phase(folder, "diagnose", meta.OUTPUT_SCHEMAS["diagnose"], meta.DIAGNOSE + "\nDATA:\n" + data,
                      model, url, end, 2048, transport)
    meta.diagnosis_check(diagnosis, source["trace"])
    atomic_json(folder / "evidence.json", {"source_sha256": digest(source),
        "trace_sha256": digest(source["trace"]), "observations": diagnosis["observations"]})
    context = "\nDATA:\n" + data + "\nDIAGNOSIS:\n" + json.dumps(diagnosis, ensure_ascii=False)
    proposal = phase(folder, "propose", meta.OUTPUT_SCHEMAS["propose"], PROPOSE + context,
                     model, url, end, 4096, transport)
    docs = meta.proposal_check(proposal, diagnosis, old)
    accepted, history = {}, []
    if docs:
        review = phase(folder, "review", meta.OUTPUT_SCHEMAS["review"], meta.REVIEW + context +
            "\nPROPOSAL:\n" + json.dumps(proposal, ensure_ascii=False), model, url, end, 2048, transport)
        accepted = meta.review_check(review, docs)
        history.append({"proposal": proposal, "review": review})
        rejected = [x for x in proposal["upserts"] if x["id"] not in accepted]
        if rejected:
            # One substantive revision, never a technical retry or score-driven search.
            revision = phase(folder, "revise", meta.OUTPUT_SCHEMAS["propose"], PROPOSE + context +
                "\nRevise ONLY the rejected candidates below, retaining their IDs. Narrow unsupported claims, "
                "or omit the candidate if public evidence cannot justify a useful procedure. Do not change accepted skills.\n" +
                json.dumps({"rejected": rejected, "review": review}, ensure_ascii=False), model, url, end, 4096, transport)
            revised = meta.proposal_check(revision, diagnosis, {**old, **accepted})
            if set(revised) - {r["id"] for r in rejected}:
                raise ValueError("revision_changed_candidate_identity")
            if revised:
                rr = phase(folder, "rereview", meta.OUTPUT_SCHEMAS["review"], meta.REVIEW + context +
                    "\nPROPOSAL:\n" + json.dumps(revision, ensure_ascii=False), model, url, end, 2048, transport)
                accepted.update(meta.review_check(rr, revised))
                history.append({"proposal": revision, "review": rr})
    payload = validate_skills({**old, **accepted})
    changed = [k for k in accepted if old.get(k) != accepted[k]]
    chosen, _ = select_skills(payload, source["instruction"])
    store = folder / "state"
    initialize(store, "skill-rsi")
    version = meta.commit(store, "skill-rsi", payload, "seen-error-repair", 1)
    result = {"status": "complete", "source_sha256": digest(source), "version": version,
        "changed_ids": changed, "selected_skills": chosen, "selected_changed_ids": sorted(set(chosen) & set(changed)),
        "outcome": "candidate_ready" if set(chosen) & set(changed) else "no_retrievable_change",
        "accounting": accounting(folder), "history": history,
        "interpretation": "evidence-reviewed candidate, NOT yet shown effective"}
    atomic_json(folder / "result.json", result)
    return result


def freeze_bank(folder):
    result = read_json(folder / "result.json")
    head = read_json(folder / "state/HEAD.json")
    payload = load_payload(folder / "state", head)
    if head != result["version"]:
        raise RuntimeError("candidate_head_changed")
    return {"version": head, "payload_sha256": digest(payload), "files": {
        str(p): file_hash(p) for p in (folder / "source.json", folder / "evidence.json", folder / "result.json",
                                      folder / "state/HEAD.json")}}


def verify_bank(folder, frozen):
    if freeze_bank(folder) != frozen:
        raise RuntimeError("frozen_candidate_changed")
    return frozen["version"]


AUDIT_SCHEMA = meta.object_schema({"summary": meta.STRING, "observations": {"type": "array", "items":
    meta.object_schema({"old_id": meta.STRING, "verdict": {"enum": ["avoided_or_repaired", "recurred", "unknown"]},
        "reason": meta.STRING, "new_span_ids": {"type": "array", "items": meta.STRING}})}})


def audit(folder, old_evidence, new_trace, model, url, transport=meta.request_once):
    prompt = """Compare each ORIGINAL observation against the NEW public solve trace.
Use the original IDs exactly once. For avoided_or_repaired require positive evidence
of the corresponding valid action/check, not mere absence of an error string.
For recurred require positive evidence of the same mistake. Otherwise use unknown.
A repaired original error does not imply the final task is solved. Cite NEW span IDs.
This is a qualitative LLM audit, not a causal proof. Do not propose more skills.
""" + "\nORIGINAL:\n" + json.dumps(old_evidence, ensure_ascii=False) + "\nNEW:\n" + meta.numbered_trace(new_trace)
    start = read_json(folder / "audit_started.json") or {"time": time.time()}
    atomic_json(folder / "audit_started.json", start)
    value = phase(folder, "audit", AUDIT_SCHEMA, prompt, model, url, start["time"] + 600, 2048, transport)
    expected = {x["id"] for x in old_evidence}
    rows, spans = value["observations"], meta.trace_spans(new_trace)
    if len(rows) != len(expected) or {r["old_id"] for r in rows} != expected:
        raise ValueError("audit_omitted_or_duplicated_old_observation")
    for row in rows:
        if row["verdict"] not in ("avoided_or_repaired", "recurred", "unknown"):
            raise ValueError("invalid_audit_verdict")
        if (row["verdict"] != "unknown" and not row["new_span_ids"]) or set(row["new_span_ids"]) - spans.keys():
            raise ValueError("ungrounded_repair_audit")
        row["anchors"] = [{"span_id": k, **spans[k]} for k in row["new_span_ids"]]
    return {**value, "interpretation": "qualitative LLM interpretation with literal public evidence, not causal proof"}
