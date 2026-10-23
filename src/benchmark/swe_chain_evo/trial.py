"""SWE-Chain-Evo transport for the existing native method adapters."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

from .common import ROOT, PROJECT, existing_imports, read, atomic, sha

existing_imports()
from methods.evo_fifty.trial import TrialRunner as ExistingRunner
from methods.evo_fifty.gateway import transport_failure_record
from methods.modular_update import tree_hashes
from rsi_portable.paths import runner_environment


class TrialRunner(ExistingRunner):
    def __init__(self, root, broker, port=8058):
        self.root, self.broker, self.port = Path(root), broker, port
        self.config = broker.config
        self.stream = read(self.root / "task-stream.json")
        self.by_task = {entry["task"]: i for i, entry in enumerate(self.stream)}
        self.env = runner_environment(port)
        from .common import docker_env
        self.env.update(docker_env())
        self.pins = None

    def _execution_contract(self):
        return {"model": self.config["model"], "solve": self.config["solve"],
                "adapter_sha256": sha(Path(__file__)),
                "agent_sha256": sha(ROOT / "swe_chain_evo/agents.py"),
                "grader_sha256": sha(ROOT / "swe_chain_evo/grading.py"),
                "go_test_isolation_sha256": sha(ROOT / "swe_chain_evo/go_test_isolation.py"),
                "environment_sha256": sha(ROOT / "swe_chain_evo/environment.py")}

    def _identity(self, arm, index, modules=None, candidate=None,
                  composer_name="llm_dynamic", composer_scope="all", locked_module_type=None):
        source = {"candidate_sha256": sha(candidate) if candidate else None,
                  "modules_hashes": tree_hashes(Path(modules)) if modules else None,
                  "composer_name": composer_name, "composer_scope": composer_scope,
                  "locked_module_type": locked_module_type}
        if candidate:
            source["prompt_templates"] = tree_hashes(Path(candidate).parent.parent / "prompt-templates")
            source["agents_source"] = tree_hashes(Path(candidate).parent)
        entry = self.stream[index]
        return {"task_checksum": entry["task_checksum"], "image_id": entry["image_id"],
                "history": self._history(arm, index), "source": source}

    def run(self, arm, index, output, phase="formal", **kwargs):
        output = Path(output)
        # Only the first candidate validation attempt may reuse its own smoke.
        # Distinct stochastic attempts, formal results and other updates cannot.
        job = output.parent.parent
        if (arm == "meta-harness" and phase == "validation" and kwargs.get("candidate")
                and job.name.startswith("evolve-") and output.name.endswith("-0")
                and not (output / "launch.json").exists() and not (output / "summary.json").exists()):
            self.check_pins()
            identity = self._identity(arm, index, **{k: v for k, v in kwargs.items() if k != "scope_limit"})
            for path in sorted(job.parent.glob("smoke-*/evo-trials/*/summary.json")):
                saved = read(path)
                if (saved.get("index") != index or saved.get("trial_identity") != identity
                        or saved.get("execution_contract") != self._execution_contract()
                        or not saved.get("usage_complete") or saved.get("exception")):
                    continue
                if sha(saved["result"]) != saved["result_sha256"] or sha(saved["grade"]) != saved["grade_sha256"]:
                    raise ValueError("immutable smoke result changed")
                atomic(output / "reused-smoke.json", {"source": str(path), "source_sha256": sha(path),
                        "trial_identity": identity, "new_provider_calls": 0,
                        "accounting": "original call IDs retained and counted once"})
                atomic(output / "summary.json", saved)
                return saved
        return super().run(arm, index, output, phase=phase, **kwargs)

    def _history(self, arm, index):
        state_path = self.root / "native-campaign.json"
        rows = read(state_path)["arms"][arm]["rows"] if state_path.exists() else []
        return [{"milestone": self.stream[r["index"]]["instruction"], "success": bool(r["reward"])}
                for r in rows if r["index"] < index
                and self.stream[r["index"]]["chain_id"] == self.stream[index]["chain_id"]]

    def _run_once(self, arm, index, output, phase="formal", modules=None, candidate=None,
                  composer_name="llm_dynamic", locked_module_type=None,
                  composer_scope="all", scope_limit=None):
        output = Path(output)
        self.check_pins()
        entry = self.stream[index]
        identity = self._identity(arm, index, modules, candidate, composer_name, composer_scope, locked_module_type)
        history, source = identity["history"], identity["source"]
        cached = output / "summary.json"
        if cached.exists():
            saved = read(cached)
            if (saved["arm"], saved["index"], saved["phase"], saved["trial_identity"]) != (arm, index, phase, identity):
                raise ValueError("cached SWE trial inputs changed")
            if sha(saved["result"]) != saved["result_sha256"] or sha(saved["grade"]) != saved["grade_sha256"]:
                raise ValueError("immutable SWE result changed")
            return saved
        if (output / "launch.json").exists():
            launch = read(output / "launch.json")
            if (launch["arm"], launch["index"], launch["phase"], launch["trial_identity"]) != (arm, index, phase, identity):
                raise ValueError("interrupted SWE trial inputs changed")
            scopes = [(t, s) for t, s in self.broker.scopes.items()
                      if s["name"] == str(output.relative_to(self.root))]
            results = list((output / "trials").glob("*/result.json"))
            if (len(scopes) != 1 or len(results) != 1 or scopes[0][1]["pending_tokens"]
                    or not read(results[0]).get("finished_at")):
                raise RuntimeError("partial SWE trial retained; no complete result to recover")
            token = scopes[0][0]
            value = self._collect(arm, index, output, phase, source, identity, token, None)
            atomic(output / "completed-trial-recovery.json", {"time": time.time(),
                   "paid_trial_replayed": False, "result_sha256": value["result_sha256"],
                   "call_ids": value["call_ids"]})
            return value
        from harbor.models.task.task import Task
        if Task(Path(entry["path"])).checksum != entry["task_checksum"]:
            raise ValueError("prepared SWE task changed")
        inspected = subprocess.check_output(["docker", "image", "inspect", entry["image_id"],
                       "--format", "{{.Id}}"], env=self.env, text=True, timeout=30).strip()
        if inspected != entry["image_id"]:
            raise ValueError("solver image changed")
        family = "meta-harness" if arm in ("meta-harness", "kira-static") else "modularrsi"
        agent = "CandidateAudited" if candidate else "KiraAudited" if family == "meta-harness" else "ModularAudited"
        model = self.config["model"]
        profile = {"max_input_tokens": model["context_tokens"] - model["max_output_tokens"],
                   "max_output_tokens": model["max_output_tokens"],
                   "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
        cmd = [str(PROJECT / ".venv-terminal/bin/harbor"), "trials", "start", "--path", entry["path"],
               "--trials-dir", str(output / "trials"), "--env", "swe_chain_evo.environment:OfflineDocker",
               "--agent-import-path", "swe_chain_evo.agents:" + agent,
               "--model", "openai/" + model["id"],
               "--agent-timeout", str(self.config["solve"]["agent_timeout_seconds"]),
               "--verifier-timeout", str(self.config["solve"]["verifier_timeout_seconds"] + 60),
               "--verifier", "swe_chain_evo.verifier:SWEVerifier"]
        kw = {"record_terminal_session": False, "max_turns": self.config["solve"]["max_agent_turns"],
              "suppress_max_turns_warning": True, "temperature": model["temperature"],
              "llm_call_kwargs": {"max_tokens": model["max_output_tokens"], "timeout": 2700},
              "api_base": f"http://127.0.0.1:{self.port}/v1", "model_info": profile}
        if family == "meta-harness":
            kw["max_output_retries"] = 1
        else:
            if modules is None:
                raise ValueError("Modular arm requires a frozen module library")
            kw.update(modules_root=str(Path(modules).resolve()), composer_name=composer_name,
                      composer_scope=composer_scope)
            if locked_module_type:
                kw["locked_module_type"] = locked_module_type
        for name, val in kw.items():
            cmd.extend(["--agent-kwarg", name + "=" + (val if isinstance(val, str) else json.dumps(val))])
        frozen = self.root / "entries" / (str(index) + ".json")
        for name, val in {"entry_file": str(frozen), "entry_sha256": sha(frozen)}.items():
            cmd.extend(["--verifier-kwarg", name + "=" + val])
        for name, val in {"GIT_PAGER": "cat", "PAGER": "cat", "TAR_OPTIONS": "--no-same-owner"}.items():
            cmd.extend(["--ae", name + "=" + val])
        output.mkdir(parents=True, exist_ok=True)
        atomic(output / "public-history.json", history)
        token = self.broker.scope(arm, phase, str(output.relative_to(self.root)), limit=scope_limit)
        env = dict(self.env)
        for key in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env.update(OPENAI_API_KEY=token, OPENAI_BASE_URL=f"http://127.0.0.1:{self.port}/v1",
                   EVO_ONLINE_API_BASE=f"http://127.0.0.1:{self.port}/v1",
                   EVO_MAX_OUTPUT_TOKENS=str(model["max_output_tokens"]),
                   PYTHONPATH=os.pathsep.join([str(ROOT), str(PROJECT),
                        str(PROJECT / "methods/vendor/meta-harness/reference_examples/terminal_bench_2")]),
                   SWE_CHAIN_HISTORY=str(output / "public-history.json"),
                   TMPDIR=str(self.root / "tmp"), PYTHONDONTWRITEBYTECODE="1")
        Path(env["TMPDIR"]).mkdir(exist_ok=True)
        if candidate:
            env["EVO_ONLINE_CANDIDATE"] = str(Path(candidate).resolve())
        atomic(output / "launch.json", {"arm": arm, "index": index, "phase": phase,
                   "entry": entry, "command": cmd, "trial_identity": identity,
                   "candidate_path": str(Path(candidate).resolve()) if candidate else None,
                   "execution_contract": self._execution_contract(), "time": time.time()})
        with (output / "worker.log").open("ab") as log:
            proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=8400)
        return self._collect(arm, index, output, phase, source, identity, token, proc.returncode)

    def _collect(self, arm, index, output, phase, source, identity, token, returncode):
        self.check_pins()
        entry = self.stream[index]
        candidate = source["candidate_sha256"]
        # Recompute from the exact launch paths, including completed recovery.
        # Candidate hashes and complete source trees were frozen before launch.
        launch = read(output / "launch.json")
        cmd = launch["command"]
        module_arg = next((x.split("=", 1)[1] for x in cmd if x.startswith("modules_root=")), None)
        modules = Path(module_arg) if module_arg else None
        if modules and tree_hashes(Path(modules)) != source["modules_hashes"]:
            raise RuntimeError("module library changed during task")
        if candidate:
            # The input candidate path is retained in launch for new trials;
            # legacy completed trials still have the immutable source snapshot.
            candidate_path = launch.get("candidate_path")
            if candidate_path and (sha(candidate_path) != candidate or
                    tree_hashes(Path(candidate_path).parent) != source["agents_source"] or
                    tree_hashes(Path(candidate_path).parent.parent / "prompt-templates") != source["prompt_templates"]):
                raise RuntimeError("candidate changed during task")
        paths = list((output / "trials").glob("*/result.json"))
        result = read(paths[0]) if len(paths) == 1 else None
        scope = self.broker.scopes[token]
        billed = [read(self.root / "provider-calls" / cid / "request.json") for cid in scope["calls"]]
        usage = [r["usage"] for r in billed if r["state"] == "settled"]
        reward = (((result or {}).get("verifier_result") or {}).get("rewards") or {}).get("reward")
        incomplete = [r for r in billed if r["state"] in ("requires_review", "rejected") or r.get("retryable_transport")]
        def network(row):
            response = self.root / "provider-calls" / row["id"] / "response.json"
            return transport_failure_record(row, read(response) if response.exists() else None)
        recovered = bool(incomplete) and all(network(r) for r in incomplete) and (not scope["blocked"] or reward == 1)
        if any(s == "over_bound" for _, s in self.broker.ledger.snapshot()["blocking"]) or (
                (scope["blocked"] or scope["pending_tokens"]) and not recovered):
            raise RuntimeError("SWE trial provider accounting requires review")
        reason = str((((result or {}).get("agent_result") or {}).get("metadata") or {}).get("budget_stop_reason") or "")
        if "global dollar limit" in reason:
            from methods.evo_fifty.spend import SpendBlocked
            raise SpendBlocked("development/lifetime dollar limit reached")
        if reward not in (0, 1):
            raise RuntimeError("unscored SWE trial; inspect " + str(output / "worker.log"))
        grade_path = paths[0].parent / "verifier/grading/grade.json"
        if not grade_path.exists():
            raise RuntimeError("SWE grading receipt missing")
        metadata = ((result or {}).get("agent_result") or {}).get("metadata") or {}
        exception = ((result or {}).get("exception_info") or {}).get("exception_type")
        exception = exception or (metadata.get("agent_exception") or {}).get("type")
        value = {"arm": arm, "index": index, "phase": phase, "task": entry["task"],
                 "source_fingerprint": source, "trial_identity": identity,
                 "reward": reward, "result": str(paths[0]), "result_sha256": sha(paths[0]),
                 "grade": str(grade_path), "grade_sha256": sha(grade_path),
                 "input_tokens": sum(u["prompt_tokens"] for u in usage),
                 "output_tokens": sum(u["completion_tokens"] for u in usage),
                 "cost_usd": sum(u["cost"] for u in usage), "call_ids": scope["calls"],
                 "exception": exception,
                 "usage_complete": not any(r["state"] == "requires_review" for r in billed),
                 "uncertain_call_ids": [r["id"] for r in billed if r["state"] == "requires_review"],
                 "returncode": returncode, "time": time.time()}
        if "execution_contract" in launch:
            value["execution_contract"] = launch["execution_contract"]
        cap = self.config["solve"]["max_input_plus_output_tokens"]
        if cap is not None and value["input_tokens"] + value["output_tokens"] > cap:
            raise RuntimeError("SWE task token cap exceeded")
        atomic(output / "summary.json", value)
        return value
