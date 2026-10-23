"""Prepare five comparable arms for a calibrated 15-task chain, without paid calls."""
import argparse
import os
import shutil
import copy
from pathlib import Path
import time

from swe_chain_evo.common import atomic, read, sha

from rsi_portable.paths import REPO, WORKSPACE as RELEASE
MODEL = "deepseek"
BUDGET = "50.00"
ENV_CHAIN = SESSION = CONTROL = None
CHAIN = None
PORT = None

def configure(chain, chain_id, session, port):
    global ENV_CHAIN, SESSION, CONTROL, CHAIN, PORT
    CHAIN, PORT = chain, port
    ENV_CHAIN = RELEASE / "preparation/chains" / chain_id
    SESSION = Path(session).resolve()
    CONTROL = SESSION / "kira-meta" / chain
def method_config(arm):
    value = read(REPO / 'configs/methods' / (arm + '.json'))
    return {'official_method': value['official_method'], 'adaptations': value['adaptations'], 'arms': {arm: value['parameters']}}

METHODS = ("experience-notes", "failure-only-notes", "evoskill")


def build():
    from within_chain.contracts import COMMON, validate
    if (SESSION / "RUN_AUTHORIZATION.json").exists():
        raise RuntimeError("authorized session cannot be rebuilt")
    proof = read(ENV_CHAIN / "zero-model-probe.json")
    stream = read(ENV_CHAIN / "task-stream.json")
    if not proof.get("passed") or len(stream) != 15 or [e["version_chain_index"] for e in stream] != list(range(15)):
        raise RuntimeError("selected repository environment calibration or ordering incomplete")
    receipts = [read(RELEASE / "preparation/tasks" / (e["task"] + ".json")) for e in stream]
    grader_hash = sha(RELEASE / "swe_chain_evo/grading.py")
    if any(not r.get("passed") or r.get("oracle_reward") != 1 or r.get("noop_reward") != 0
           or r.get("grader_sha256") != grader_hash or r["entry"] != e for r, e in zip(receipts, stream)):
        raise RuntimeError("selected repository grader receipts differ from frozen task stream")
    SESSION.mkdir(parents=True, exist_ok=True)
    (SESSION / "HOLD").write_text("Preparing selected repository; paid experiments held until audited launch.\n")
    CONTROL.mkdir(parents=True, exist_ok=True)
    if (CONTROL / "native-campaign.json").exists():
        raise RuntimeError("control already has paid execution state")
    cfg = read(REPO / 'configs/templates/control.json')
    cfg.update(read(REPO / 'configs' / MODEL / 'model.json'))
    cfg['arms'] = {arm: read(REPO / 'configs/methods' / (arm + '.json')) for arm in ('kira-static', 'meta-harness')}
    cfg['spend'] = {'global_limit': BUDGET, 'ledger_path': str(SESSION / 'spend.sqlite'), 'scope': 'one session shared across all five methods'}
    cfg['credential_file'] = str(SESSION / '.private/model-key')
    cfg['runtime'].update(model_http_proxy=os.environ.get('RSI_MODEL_PROXY', ''), host_outbound_proxy=os.environ.get('RSI_HOST_PROXY', ''), task_outbound_proxy='')
    cfg['model']['tokenizer_sha256'] = sha(RELEASE / 'data/tokenizers' / (MODEL + '.json'))
    from methods.evo_fifty.spend import SpendLedger
    SpendLedger(Path(cfg['spend']['ledger_path']), BUDGET, allow_uncertain=True)
    cfg.update(configuration_revision="seven-chain-expanded-20261020-v1", chain_id=stream[0]["chain_id"], session_root=str(SESSION),
               experiment_purpose="five independent methods on one official 15-step selected repository chain",
               research_result_status="prepared; no paid task started",
               initial_state_policy="empty chain-local learning state; official KIRA base",
               spend=cfg["spend"])
    cfg["reporting"].update(cohort=CHAIN + "-15-kira-meta", scope="one complete official selected repository chain")
    cfg["runtime"].update(gateway_port=PORT, arm_concurrency=2,
                          validation_transport_epoch="swe-expanded-" + CHAIN + "-v1")
    cfg["evaluation_corrections"] = {
        "reference_test_id_mapping": "frozen full IDs; every mapped test must pass",
        "grader_protocol": "skill-validity-grader-isolation-20261020-v1",
        "test_symbol_isolation": "namespace agent-added test functions only in disposable grader",
        "skill_discovery_required": True,
        "official_patches_and_required_tests_changed": False,
        "raw_results_and_commands_retained": True,
        "private_grader_public_go_dependency_recovery": {
            "trigger": "offline Go module cache miss during private grading only",
            "downloader": "go mod download for official public modules",
            "grader_network": "none", "solver_network": "none",
            "solver_or_proposer_cache_modified": False,
            "original_test_commands_and_required_tests_changed": False,
            "source": str(RELEASE / "swe_chain_evo/offline_go_dependencies.py")}}
    atomic(CONTROL / "configuration.json", cfg)
    atomic(CONTROL / "task-stream.json", stream)
    if not (CONTROL / "tokenizer.json").exists():
        shutil.copy2(RELEASE / "data/tokenizers" / (MODEL + ".json"), CONTROL / "tokenizer.json")
    for i, (entry, receipt) in enumerate(zip(stream, receipts)):
        atomic(CONTROL / "entries" / f"{i}.json", entry)
        atomic(CONTROL / "preparation/tasks" / f"{i}.json", {
            "passed": True, "task": entry["task"], "grader_sha256": grader_hash,
            "oracle_reward": 1, "noop_reward": 0,
            "environment_receipt": str(RELEASE / "preparation/tasks" / (entry["task"] + ".json")),
            "environment_receipt_sha256": sha(RELEASE / "preparation/tasks" / (entry["task"] + ".json")),
            "test_id_map_sha256": receipt["test_id_map_sha256"]})
    atomic(CONTROL / "initial-state.json", {
        "chain_id": stream[0]["chain_id"], "cross_chain_transfer": False,
        "learned_state_reused": False, "development_results_reused": False,
        "initial_harness": "official baseline_kira", "selected_arms": ["kira-static", "meta-harness"]})
    atomic(CONTROL / "inheritance-provenance.json", {
        "chain_id": stream[0]["chain_id"], "cross_chain_transfer": False,
        "learned_artifacts_imported": False, "paid_formal_results_copied": False,
        "official_base_harness": str(RELEASE / "runtime-project/methods/vendor/meta-harness/reference_examples/terminal_bench_2/agents/baseline_kira.py")})
    atomic(CONTROL / "harbor-preflight.json", {
        "passed": True, "model_calls": 0, "source": str(ENV_CHAIN / "zero-model-probe.json"),
        "source_sha256": sha(ENV_CHAIN / "zero-model-probe.json"), "chain_id": stream[0]["chain_id"]})
    atomic(CONTROL / "formal-score-preflight.json", {
        "passed": True, "model_calls": 0, "oracle_reward": 1, "noop_reward": 0,
        "tasks_calibrated": len(receipts), "normalizer_sha256": sha(RELEASE / "formal_swe_chain/grading.py"),
        "harbor_probe": str(ENV_CHAIN / "zero-model-probe.json")})
    atomic(CONTROL / "launch-gates.json", {
        "passed": True, "offline_environment_checks_only": True,
        "native_paid_integration_pending": True,
        "new_chain_gates": "15 oracle/noop receipts and actual zero-model Harbor probe; first-task/update integration gates remain enabled"})
    atomic(CONTROL / "image-provenance.json", [
        {"task": e["task"], "image_id": e["image_id"],
         "source_receipt": str(RELEASE / "preparation/tasks" / (e["task"] + ".json")),
         "source_receipt_sha256": sha(RELEASE / "preparation/tasks" / (e["task"] + ".json"))}
        for e in stream])
    protocol = {"chain_id": stream[0]["chain_id"], "selected_arms": ["kira-static", "meta-harness"],
                "fresh_initial_state": True, "model": cfg['model'], "solve": cfg['solve'], "chain_protocol": cfg['chain_protocol']}
    atomic(CONTROL / "protocol-frozen.json", protocol)
    (CONTROL / "PAUSE").write_text("selected repository preparation held.\n")
    from formal_swe_chain.campaign import validate as validate_control
    validate_control(CONTROL, cfg)
    from formal_swe_chain.campaign import source_pins as control_pins
    atomic(CONTROL / "native-source-pins.json", control_pins(CONTROL))
    runs = []
    for n, arm in enumerate(METHODS):
        root = SESSION / arm / CHAIN
        root.mkdir(parents=True, exist_ok=True)
        if (root / "native-campaign.json").exists():
            raise RuntimeError("method already has paid execution state: " + str(root))
        prior = method_config(arm)
        method_cfg = copy.deepcopy(cfg)
        method_cfg.update(schema="within-chain-experiment-v1", session_root=str(SESSION),
            experiment_purpose="five-arm within-chain adaptation screening on selected repository",
            research_result_status="prepared; no paid task started",
            comparison_source={"run": str(CONTROL), "configuration_sha256": sha(CONTROL / "configuration.json")},
            initial_state_policy="empty learning state per method-chain",
            official_method=prior["official_method"], adaptations=prior["adaptations"])
        method_cfg["arms"] = {arm: copy.deepcopy(prior["arms"][arm])}
        method_cfg["arms"][arm]["paired_with"] = str(CONTROL) + "#kira-static"
        method_cfg["runtime"].update(gateway_port=PORT + 1 + n, arm_concurrency=1,
            scheduling="independent chronological selected repository chain; empty learning state; no cross-chain transfer")
        method_cfg["reporting"].update(cohort="within-chain-" + CHAIN + "-five", scope="one complete independent chain")
        atomic(root / "configuration.json", method_cfg)
        atomic(root / "task-stream.json", stream)
        for i, entry in enumerate(stream):
            atomic(root / "entries" / f"{i}.json", entry)
        (root / "initial/skills").mkdir(parents=True, exist_ok=True)
        (root / "initial/note.md").touch(exist_ok=True)
        (root / "preparation").mkdir(exist_ok=True)
        if not (root / "tokenizer.json").exists():
            (root / "tokenizer.json").symlink_to(CONTROL / "tokenizer.json")
        (root / "PAUSE").write_text("selected repository preparation held.\n")
        atomic(root / "initial-state.json", {"chain_id": stream[0]["chain_id"],
            "cross_chain_transfer": False, "skills": {}, "note_tokens": 0,
            "frontier": [], "harness": "official KIRA"})
        atomic(root / "source-provenance.json", {"release": str(RELEASE), "control": str(CONTROL),
            "control_configuration_sha256": sha(CONTROL / "configuration.json"),
            "benchmark_task_stream_sha256": sha(CONTROL / "task-stream.json"),
            "learned_artifacts_imported": False, "paid_formal_results_copied": False})
        validate(root, require_ready=False)
        runs.append({"method": arm, "chain": CHAIN, "root": str(root),
                     "control": str(CONTROL), "configuration_sha256": sha(root / "configuration.json"), "tasks": 15})
    manifest = {"schema": "within-chain-session-v1", "release": str(RELEASE),
        "session_root": str(SESSION), "state": "prepared_held", "runs": runs,
        "new_formal_tasks": 75, "reuse_controls": [{"chain": CHAIN, "root": str(CONTROL),
                                                   "arms": ["kira-static", "meta-harness"]}],
        "paid_model_calls_started": 0, "start_requires_explicit_user_instruction": False,
        "user_instruction": None}
    atomic(SESSION / "manifest.json", manifest)
    return manifest


def verify():
    from within_chain.offline import inventory, probe
    from within_chain.contracts import source_pins, validate, budget_snapshot
    manifest = read(SESSION / "manifest.json")
    # One live zero-model Harbor path in this method stack; the environment probe
    # separately exercised the control scorer and every task's oracle/noop pair.
    first = Path(manifest["runs"][0]["root"])
    plumbing = probe(first)
    for row in manifest["runs"]:
        root = Path(row["root"])
        evidence = inventory(root)
        atomic(root / "preparation/task-inventory.json", {"passed": True, "tasks": evidence})
        atomic(root / "native-source-pins.json", source_pins(root))
        atomic(root / "preparation/readiness.json", {
            "passed": True, "paid_model_calls": 0,
            "configuration_sha256": sha(root / "configuration.json"),
            "task_inventory_sha256": sha(root / "preparation/task-inventory.json"),
            "probe": plumbing, "initial_learning_state_empty": True, "held": True, "time": time.time()})
        validate(root)
    result = {"passed": True, "state": "ready_held", "new_runs": 3,
              "control_arms": 2, "formal_tasks": 75, "paid_model_calls": 0,
              "budget": budget_snapshot(read(CONTROL / "configuration.json")), "time": time.time()}
    atomic(SESSION / "readiness.json", result)
    return result
