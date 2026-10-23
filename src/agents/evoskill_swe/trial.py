"""Existing scored trial transport with the EvoSkill Harbor inner-agent seam."""
import json
import os
from pathlib import Path
import subprocess
import time

from . import RELEASE, BACKBONE
from .skills import hashes, require_inventory, verify_discovery
from .recovery import compatible_contract, predispatch_skill_failure
from formal_swe_chain.trial import TrialRunner as FormalTrialRunner
from swe_chain_evo.common import PROJECT, atomic, read, sha


class TrialRunner(FormalTrialRunner):
    def _execution_contract(self):
        return {**super()._execution_contract(), "evoskill_adapter_sha256": sha(Path(__file__)),
                "evoskill_agent_sha256": sha(RELEASE / "evoskill_swe/agents.py"),
                "skill_loader_sha256": sha(RELEASE / "evoskill_swe/skills.py")}

    def _identity(self, arm, index, skills=None, note=None, **kwargs):
        value = {"task_checksum": self.stream[index]["task_checksum"],
                "image_id": self.stream[index]["image_id"], "history": self._history(arm, index),
                "source": {"candidate_sha256": None, "modules_hashes": None,
                           "kira_source_sha256": sha(PROJECT / "methods/vendor/meta-harness/reference_examples/terminal_bench_2/agents/baseline_kira.py"),
                           "skills_hashes": hashes(skills) if skills else {}}}
        if note is not None:
            value["source"]["note_sha256"] = sha(note)
        return value

    def _run_once(self, arm, index, output, phase="formal", skills=None, scope_limit=None, note=None):
        self.check_pins()
        if skills is not None:
            require_inventory(skills)
        output = Path(output)
        identity = self._identity(arm, index, skills=skills, note=note)
        source, entry = identity["source"], self.stream[index]
        cached = output / "summary.json"
        if cached.exists():
            saved = read(cached)
            if (saved["arm"], saved["index"], saved["phase"], saved["trial_identity"]) != (arm, index, phase, identity):
                raise ValueError("cached EvoSkill trial inputs changed")
            if predispatch_skill_failure(output, saved):
                if saved["trial_identity"] != identity:
                    raise ValueError("failed infrastructure trial inputs changed")
                repaired = output / "infrastructure-retries/skills-host-path-v1"
                atomic(output / "infrastructure-recovery.json", {
                    "original_summary_sha256": sha(cached), "provider_calls_before_failure": 0,
                    "original_result_sha256": saved["result_sha256"],
                    "reason": "host Path collided with inherited remote skills_dir before dispatch",
                    "retry": str(repaired), "original_paid_artifacts_retained": True})
                return self.run(arm, index, repaired, phase=phase, skills=skills, scope_limit=scope_limit)
            if not compatible_contract(saved.get("execution_contract"), self._execution_contract(), identity):
                raise ValueError("cached EvoSkill execution contract changed")
            if sha(saved["result"]) != saved["result_sha256"] or sha(saved["grade"]) != saved["grade_sha256"]:
                raise ValueError("immutable EvoSkill trial changed")
            if note is None:
                verify_discovery(saved["result"], skills)
            return saved
        if (output / "launch.json").exists():
            launch = read(output / "launch.json")
            if launch["trial_identity"] != identity or not compatible_contract(launch["execution_contract"], self._execution_contract(), identity):
                raise ValueError("interrupted EvoSkill trial inputs changed")
            scopes = [(t, s) for t, s in self.broker.scopes.items()
                      if s["name"] == str(output.relative_to(self.root))]
            results = list((output / "trials").glob("*/result.json"))
            if len(scopes) != 1 or len(results) != 1 or scopes[0][1]["pending_tokens"] or not read(results[0]).get("finished_at"):
                raise RuntimeError("partial paid EvoSkill trial retained; no safe complete-result recovery")
            row = self._collect(arm, index, output, phase, source, identity, scopes[0][0], None)
            if note is None:
                verify_discovery(row["result"], skills)
            return row
        from harbor.models.task.task import Task
        if Task(Path(entry["path"])).checksum != entry["task_checksum"]:
            raise ValueError("prepared task changed")
        image = subprocess.check_output(["docker", "image", "inspect", entry["image_id"], "--format", "{{.Id}}"],
                                        env=self.env, text=True, timeout=30).strip()
        if image != entry["image_id"]:
            raise ValueError("solver image changed")
        model, solve = self.config["model"], self.config["solve"]
        profile = {"max_input_tokens": model["context_tokens"] - model["max_output_tokens"],
                   "max_output_tokens": model["max_output_tokens"], "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
        cmd = [str(PROJECT / ".venv-terminal/bin/harbor"), "trials", "start", "--path", entry["path"],
               "--trials-dir", str(output / "trials"), "--env", "swe_chain_evo.environment:OfflineDocker",
               "--agent-import-path", ("within_chain.agents:KiraNotes" if note is not None else "evoskill_swe.agents:KiraSkills"), "--model", "openai/" + model["id"],
               "--agent-timeout", str(solve["agent_timeout_seconds"]),
               "--verifier-timeout", str(solve["verifier_timeout_seconds"] + 60),
               "--verifier", "swe_chain_evo.verifier:SWEVerifier"]
        kw = {"record_terminal_session": False, "max_turns": solve["max_agent_turns"],
              "suppress_max_turns_warning": True, "temperature": model["temperature"],
              "llm_call_kwargs": {"max_tokens": model["max_output_tokens"], "timeout": 2700},
              "api_base": f"http://127.0.0.1:{self.port}/v1", "model_info": profile,
              "max_output_retries": 1}
        if skills and hashes(skills):
            kw["skills_dir"] = str(Path(skills).resolve())
            cmd += ["--mounts-json", json.dumps([{"type": "bind", "source": str(Path(skills).resolve()),
                                                 "target": "/skills", "read_only": True}])]
        for name, value in kw.items():
            cmd += ["--agent-kwarg", name + "=" + (value if isinstance(value, str) else json.dumps(value))]
        frozen = self.root / "entries" / f"{index}.json"
        for name, value in {"entry_file": str(frozen), "entry_sha256": sha(frozen)}.items():
            cmd += ["--verifier-kwarg", name + "=" + value]
        for name, value in {"GIT_PAGER": "cat", "PAGER": "cat", "TAR_OPTIONS": "--no-same-owner"}.items():
            cmd += ["--ae", name + "=" + value]
        output.mkdir(parents=True, exist_ok=True)
        atomic(output / "public-history.json", identity["history"])
        token = self.broker.scope(arm, phase, str(output.relative_to(self.root)), limit=scope_limit)
        env = dict(self.env)
        for key in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
        env.update(OPENAI_API_KEY=token, OPENAI_BASE_URL=f"http://127.0.0.1:{self.port}/v1",
                   EVO_ONLINE_API_BASE=f"http://127.0.0.1:{self.port}/v1",
                   EVO_MAX_OUTPUT_TOKENS=str(model["max_output_tokens"]),
                   PYTHONPATH=os.pathsep.join([str(RELEASE), str(BACKBONE), str(PROJECT),
                        str(PROJECT / "methods/vendor/meta-harness/reference_examples/terminal_bench_2")]),
                   SWE_CHAIN_HISTORY=str(output / "public-history.json"),
                   TMPDIR=str(self.root / "tmp"), PYTHONDONTWRITEBYTECODE="1")
        if note is not None:
            env.update(WITHIN_CHAIN_NOTE=str(Path(note).resolve()),
                       WITHIN_CHAIN_NOTE_SHA256=source["note_sha256"])
        Path(env["TMPDIR"]).mkdir(exist_ok=True)
        atomic(output / "launch.json", {"arm": arm, "index": index, "phase": phase, "entry": entry,
            "command": cmd, "trial_identity": identity, "execution_contract": self._execution_contract(),
            "skills_path": str(skills) if skills else None, "time": time.time()})
        with (output / "worker.log").open("ab") as log:
            proc = subprocess.run(cmd, cwd=RELEASE, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=8400)
        if skills and hashes(skills) != source["skills_hashes"]:
            raise RuntimeError("task skill snapshot changed during execution")
        if note is not None and sha(note) != source["note_sha256"]:
            raise RuntimeError("task experience note changed during execution")
        row = self._collect(arm, index, output, phase, source, identity, token, proc.returncode)
        if note is None:
            verify_discovery(row["result"], skills)
        return row
