"""Read-only validation, source pins, and the explicit pre-launch hold."""
import json
from pathlib import Path

from swe_chain_evo.common import read, sha
from evoskill_swe.skills import hashes
from . import ARMS, RELEASE

COMMON = ("model", "solve", "accounting", "spend", "evaluation_corrections", "chain_protocol")


def validate_spend(root, cfg, source):
    if cfg["spend"] == source["spend"]:
        return
    session = Path(cfg["session_root"]).resolve()
    grant_path = session / "budget-authorization.json"
    if not grant_path.is_file() or cfg.get("campaign_budget_authorization_sha256") != sha(grant_path):
        raise ValueError("comparability contract differs: spend; missing campaign budget authorization")
    grant = read(grant_path)
    evidence = session / "provider-balance-before-launch.json"
    expected = {**source["spend"], "ledger_path": str(session / "spend.sqlite"),
                "scope": "shared campaign $50 across six within-chain runs; historical ledger retained separately"}
    if (cfg["spend"] != expected or expected["global_limit"] != "50.00"
            or grant.get("authorized") is not True or not grant.get("user_instruction")
            or grant.get("session_root") != str(session) or grant.get("limit_usd") != 50
            or grant.get("historical_ledger") != source["spend"]["ledger_path"]
            or grant.get("provider_evidence_sha256") != sha(evidence)
            or read(evidence)["key"]["limit_remaining"] < 50):
        raise ValueError("invalid authorized campaign spend contract")


def validate(root, require_ready=True):
    root = Path(root).resolve()
    cfg = read(root / "configuration.json")
    old = Path(cfg["comparison_source"]["run"])
    source = read(old / "configuration.json")
    if cfg.get("schema") != "within-chain-experiment-v1" or len(cfg["arms"]) != 1:
        raise ValueError("expected a single-method within-chain run")
    arm = next(iter(cfg["arms"]))
    if arm not in ARMS:
        raise ValueError("unsupported within-chain method")
    for name in COMMON:
        if name == "spend":
            validate_spend(root, cfg, source)
            continue
        if cfg[name] != source[name]:
            raise ValueError("comparability contract differs: " + name)
    if sha(old / "configuration.json") != cfg["comparison_source"]["configuration_sha256"]:
        raise ValueError("comparison configuration changed")
    stream = read(root / "task-stream.json")
    if stream != read(old / "task-stream.json") or len(stream) != 15:
        raise ValueError("task stream differs from its fixed control")
    if len({e["chain_id"] for e in stream}) != 1 or [e["version_chain_index"] for e in stream] != list(range(15)):
        raise ValueError("one complete ordered chain required")
    if cfg["solve"]["max_input_plus_output_tokens"] is not None:
        raise ValueError("unexpected cumulative token cutoff")
    if cfg["arms"][arm]["update_every_completed_tasks"] != 4 or cfg["arms"][arm]["validation_task_limit"] != 2:
        raise ValueError("update schedule/support window differs")
    if hashes(root / "initial/skills") or (root / "initial/note.md").read_bytes():
        raise ValueError("new chains must start with empty learning state")
    if sha(root / "tokenizer.json") != cfg["model"]["tokenizer_sha256"]:
        raise ValueError("tokenizer changed")
    initial = read(root / "initial-state.json")
    if initial.get("cross_chain_transfer") is not False or initial.get("chain_id") != stream[0]["chain_id"]:
        raise ValueError("chain isolation provenance invalid")
    for i, item in enumerate(stream):
        if read(root / "entries" / f"{i}.json") != item:
            raise ValueError("frozen entry differs from task stream")
    if require_ready:
        checks = read(root / "preparation/readiness.json")
        if not checks.get("passed") or checks.get("paid_model_calls") != 0:
            raise RuntimeError("offline preparation has not passed")
        if checks["configuration_sha256"] != sha(root / "configuration.json"):
            raise ValueError("prepared configuration changed")
        check_pins(root)
    return cfg, arm, stream


def source_pins(root):
    root = Path(root)
    cfg = read(root / "configuration.json")
    inherited = read(Path(cfg["comparison_source"]["run"]) / "native-source-pins.json")
    paths = []
    for name in ("within_chain", "evoskill_swe", "formal_swe_chain", "swe_chain_evo", "tests"):
        paths += list((RELEASE / name).glob("*.py"))
    paths += [p for p in (RELEASE / "scripts").iterdir() if p.is_file()]
    paths += list((RELEASE / "prompts").glob("*.txt"))
    paths += list((RELEASE / "runtime-project/methods/evo_fifty").glob("*.py"))
    paths += [root / f for f in ("configuration.json", "task-stream.json", "tokenizer.json", "initial-state.json",
                                 "source-provenance.json", "initial/note.md")]
    paths += list((root / "entries").glob("*.json"))
    if cfg.get("campaign_budget_authorization_sha256"):
        paths += [Path(cfg["session_root"]) / "budget-authorization.json",
                  Path(cfg["session_root"]) / "provider-balance-before-launch.json"]
    for item in read(root / "task-stream.json"):
        paths.append(RELEASE / "runs/grader-preflight" / item["task"] / "test-id-map.json")
    from evoskill_swe import VENDOR, SDK_SITE
    paths += [p for p in VENDOR.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    paths += list((SDK_SITE / "claude_agent_sdk").rglob("*.py"))
    import shutil
    paths += [Path(shutil.which("claude")).resolve()]
    return {**inherited, **{str(p): sha(p) for p in paths}}


def check_pins(root):
    for name, expected in read(Path(root) / "native-source-pins.json").items():
        if not Path(name).is_file() or sha(name) != expected:
            raise RuntimeError("pinned source/input changed: " + name)


def require_authorization(root):
    """Called before reading a credential, constructing a broker, or forking."""
    root = Path(root).resolve()
    session = Path(read(root / "configuration.json")["session_root"])
    if (session / "HOLD").exists() or (root / "PAUSE").exists():
        raise RuntimeError("Experiments are held at the user's request; awaiting explicit start instruction")
    path = session / "RUN_AUTHORIZATION.json"
    if not path.exists():
        raise RuntimeError("Explicit user launch authorization has not been recorded")
    grant = read(path)
    if (grant.get("authorized") is not True or not grant.get("user_instruction")
            or grant.get("session_root") != str(session)
            or grant.get("configuration_hashes", {}).get(str(root)) != sha(root / "configuration.json")):
        raise RuntimeError("Launch authorization does not cover this prepared run")


def budget_snapshot(config):
    """The preparation path must never instantiate the writable billing ledger."""
    import sqlite3
    db = sqlite3.connect("file:" + config["spend"]["ledger_path"] + "?mode=ro", uri=True)
    try:
        from methods.evo_fifty.spend import SpendLedger
        limit = db.execute("SELECT limit_units FROM settings WHERE id=1").fetchone()[0]
        charged, reserved, count, unattributed = SpendLedger._totals(None, db)
        return {"limit_usd": limit / 1e9, "charged_usd": charged / 1e9,
                "unresolved_reserved_usd": reserved / 1e9,
                "available_usd": max(0, limit - charged - reserved) / 1e9,
                "calls": count, "unattributed_usd": unattributed / 1e9,
                "source": "read-only local ledger; not live provider balance"}
    finally:
        db.close()
