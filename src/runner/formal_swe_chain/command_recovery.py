"""Complete one released command that omits declared target/regression tests."""
import copy
from pathlib import Path
import threading
from unittest.mock import patch

from swe_chain_evo import grading as core
from swe_chain_evo.common import atomic, instance, read, sha

OVERRIDES = {"pallets__click_lc_0030_8c95c73b": [
    "python -m pytest --continue-on-collection-errors -rA tests/test_options.py tests/test_termui.py"]}
_lock = threading.Lock()


def complete_test_logs(entry, folder):
    folder = Path(folder)
    extra = OVERRIDES.get(entry["instance_id"])
    if not extra:
        return folder
    item = instance(entry)
    commands = item["raw"]["test_cmds"] + extra
    output = folder / "complete-command-checks"
    contract = {"instance_id": entry["instance_id"], "official_commands": item["raw"]["test_cmds"],
        "effective_commands": commands, "adapter_sha256": sha(Path(__file__)),
        "core_grader_sha256": sha(Path(core.__file__)),
        "reason": "released command collects 8 tests but declared F2P/P2P lists require 698; preserve all declared tests"}
    receipt = output / "command-contract.json"
    if receipt.exists() and read(receipt) != contract:
        raise RuntimeError("frozen evaluation command contract changed")
    identity = read(folder / "grade.json")["identity"]
    prediction = next((p for p in (folder / "prediction.patch", folder.parent / "prediction.patch")
                       if p.is_file() and sha(p) == identity["prediction_sha256"]), None)
    if prediction is None:
        raise RuntimeError("original prediction unavailable for complete hidden verification")
    projected = copy.deepcopy(item)
    projected["raw"]["test_cmds"] = commands

    def load(requested):
        # Other threads can still run their unchanged grader while the one
        # affected task is evaluated in its own offline container.
        return projected if requested["instance_id"] == entry["instance_id"] else instance(requested)

    with _lock:
        atomic(receipt, contract)
        with patch.object(core, "instance", load):
            core.grade(entry, prediction, output)
    return output
