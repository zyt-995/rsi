"""Normalize scoring IDs after hidden tests run; preserve every raw artifact."""
from pathlib import Path
import shutil

from swe_chain_evo.common import atomic, read, sha
from swe_chain_evo.trial import TrialRunner as CoreRunner
from .grading import normalize


class TrialRunner(CoreRunner):
    def _execution_contract(self):
        from . import grading
        return {**super()._execution_contract(), "formal_trial_sha256": sha(Path(__file__)),
                "test_id_normalizer_sha256": sha(Path(grading.__file__))}

    def _collect(self, arm, index, output, *args):
        results = list((output / "trials").glob("*/result.json"))
        if len(results) == 1:
            result_path = results[0]
            grade = result_path.parent / "verifier/grading/grade.json"
            if grade.exists():
                normalized = normalize(self.stream[index], grade.parent)
                result = read(result_path)
                raw = result_path.with_name("result.raw.json")
                if not raw.exists():
                    shutil.copy2(result_path, raw)
                verifier = result.get("verifier_result")
                if verifier and "reward" in (verifier.get("rewards") or {}):
                    verifier["rewards"]["reward"] = normalized["reward"]
                    atomic(result_path, result)
        return super()._collect(arm, index, output, *args)
