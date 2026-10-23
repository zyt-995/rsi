"""One logical note-generation call, with exact checkpoints and no task rollout."""
import json
import time
from pathlib import Path

from methods.evo_fifty.model_codec import load_codec
from swe_chain_evo.common import atomic, read, sha
from evoskill_swe.native import digest
from . import RELEASE


def tokenizer(root):
    return load_codec(Path(root) / "tokenizer.json")


def excerpt(text, head, tail):
    if len(text) <= head + tail:
        return text
    return (text[:head] + f"\n[... {len(text)-head-tail} characters omitted ...]\n"
            + (text[-tail:] if tail else ""))


def limit_note(text, codec, limit):
    ids = codec.encode(text).ids
    if len(ids) <= limit:
        return text, False
    marker = "\n[Note truncated to the configured token limit.]"
    keep = limit - len(codec.encode(marker).ids) - 8
    while keep >= 0:
        value = codec.decode(ids[:keep]).rstrip() + marker
        if len(codec.encode(value).ids) <= limit:
            return value, True
        keep -= 1
    raise ValueError("note token limit cannot hold truncation marker")


def update(runner, arm, index, rows, previous_note, output):
    output, previous_note = Path(output), Path(previous_note)
    cfg = runner.config["arms"][arm]
    if [r["index"] for r in rows] != list(range(index + 1)):
        raise ValueError("note update requires the exact completed chronological prefix")
    window = rows[-cfg["validation_task_limit"]:]
    if len(window) != 2 or any(r.get("exception") for r in window):
        raise ValueError("note support window must contain two normal scored trials")
    selected = [r for r in window if r["reward"] == 0] if cfg["failure_only"] else window
    identity = {"arm": arm, "index": index, "previous_note_sha256": sha(previous_note),
                "window": [{"index": r["index"], "result_sha256": r["result_sha256"],
                            "reward": r["reward"]} for r in window],
                "configuration_sha256": sha(runner.root / "configuration.json"),
                "prompt_sha256": sha(RELEASE / "prompts/experience-update.txt")}
    output.mkdir(parents=True, exist_ok=True)
    saved = output / "outcome.json"
    if saved.exists():
        value = read(saved)
        if value["identity"] != identity or sha(value["note"]) != value["note_sha256"]:
            raise RuntimeError("cached note update changed")
        return value
    codec = tokenizer(runner.root)
    old = previous_note.read_text()
    if len(codec.encode(old).ids) > cfg["note_max_tokens"]:
        raise ValueError("previous note violates the frozen size limit")
    value = {"identity": identity, "index": index, "support_indices": [r["index"] for r in window],
             "selected_indices": [r["index"] for r in selected], "triggered": bool(selected),
             "note": str(previous_note), "note_sha256": sha(previous_note),
             "note_tokens": len(codec.encode(old).ids), "deployed_changed": False,
             "execution": {"passed": True, "extra_task_rollouts": 0, "logical_generations": 0}}
    if not selected:
        value.update(status="skipped_no_failure", call_ids=[], usage_complete=True,
                     input_tokens=0, output_tokens=0, cost_usd=0., finished=time.time())
        atomic(saved, value)
        return value
    samples = []
    for row in selected:
        result = Path(row["result"])
        if sha(result) != row["result_sha256"]:
            raise RuntimeError("formal trajectory result changed before note update")
        path = result.parent / "agent/trajectory.json"
        if not path.is_file():
            raise RuntimeError("public task trajectory missing; inspect before generating experience")
        text = path.read_text()
        samples.append({"task_id": row["task"], "instruction": runner.stream[row["index"]]["instruction"],
                        "reward": row["reward"], "trace_sha256": sha(path),
                        "trace_excerpt": excerpt(text, cfg["trace_head_chars"], cfg["trace_tail_chars"])})
    payload = {"model": runner.config["model"]["id"],
               "max_tokens": runner.config["model"]["max_output_tokens"],
               "messages": [{"role": "system", "content": (RELEASE / "prompts/experience-update.txt").read_text()},
                            {"role": "user", "content": json.dumps({"previous_note": old,
                                "strategy": arm, "completed_tasks": samples}, ensure_ascii=False)}]}
    admission = len(codec.encode(json.dumps({"messages": payload["messages"], "tools": []}, ensure_ascii=False)).ids)
    admission += 1024 + 64 * len(payload["messages"]) + payload["max_tokens"]
    if admission > runner.config["model"]["context_tokens"]:
        raise ValueError("frozen note excerpt exceeds common context admission")
    checkpoint = output / "started.json"
    scope_name = str((output / "analysis").relative_to(runner.root))
    if checkpoint.exists():
        recorded = read(checkpoint)
        if recorded != {"identity": identity, "payload_sha256": digest(payload), "scope": scope_name}:
            raise RuntimeError("interrupted note-generation inputs changed")
    else:
        atomic(output / "input.json", payload)
        atomic(checkpoint, {"identity": identity, "payload_sha256": digest(payload), "scope": scope_name})
    scopes = [(t, s) for t, s in runner.broker.scopes.items() if s["name"] == scope_name]
    if len(scopes) > 1:
        raise RuntimeError("ambiguous note-generation scope")
    if scopes:
        token, scope = scopes[0]
        if scope["pending_tokens"] or scope["blocked"]:
            raise RuntimeError("partial paid note generation retained; review before continuing")
    else:
        token = runner.broker.scope(arm, "analysis", scope_name)
        scope = runner.broker.scopes[token]
    calls = [read(runner.root / "provider-calls" / cid / "request.json") for cid in scope["calls"]]
    settled = [c for c in calls if c["state"] == "settled"]
    if scope["calls"] and not settled:
        raise RuntimeError("unfinished paid note generation retained; no blind replay")
    if settled:
        raw = read(runner.root / "provider-calls" / settled[-1]["id"] / "response.json")
    else:
        raw = runner.broker.execute(token, payload)
    if scope["pending_tokens"] or scope["blocked"]:
        raise RuntimeError("note-generation accounting requires review")
    calls = [read(runner.root / "provider-calls" / cid / "request.json") for cid in scope["calls"]]
    settled = [c for c in calls if c["state"] == "settled"]
    atomic(output / "response.json", raw)
    choices = raw.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    message = message if isinstance(message, dict) else {}
    text = message.get("content")
    valid = isinstance(text, str) and bool(text.strip()) and not message.get("tool_calls")
    value["execution"]["logical_generations"] = 1
    value.update(status="generation_failed", call_ids=list(scope["calls"]),
                 usage_complete=not any(c["state"] == "requires_review" for c in calls),
                 input_tokens=sum(c["usage"]["prompt_tokens"] for c in settled),
                 output_tokens=sum(c["usage"]["completion_tokens"] for c in settled),
                 cost_usd=sum(c["usage"]["cost"] for c in settled), truncated=False,
                 input_admission_with_output=admission)
    if valid:
        text, truncated = limit_note(text.strip(), codec, cfg["note_max_tokens"])
        deployed = output / "note.md"
        if deployed.exists() and deployed.read_text() != text:
            raise RuntimeError("unfinished immutable note snapshot changed")
        deployed.write_text(text)
        value.update(status="generated", note=str(deployed), note_sha256=sha(deployed),
                     note_tokens=len(codec.encode(text).ids), truncated=truncated,
                     deployed_changed=sha(deployed) != sha(previous_note))
    value["finished"] = time.time()
    atomic(saved, value)
    return value
