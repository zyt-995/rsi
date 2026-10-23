"""Independent chronological execution; requires a separately recorded start grant."""
import argparse
import asyncio
import fcntl
import os
from pathlib import Path
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from swe_chain_evo.common import atomic, read, sha
from evoskill_swe.trial import TrialRunner
from evoskill_swe.native import update as evolve
from evoskill_swe.campaign import acceptance
from formal_swe_chain.budget import Broker
from methods.evo_fifty.gateway import Server
from methods.evo_fifty.trial import NetworkUnavailable
from methods.evo_fifty.spend import SpendBlocked
from . import notes
from .contracts import validate, require_authorization
from .report import run_report


def run(root):
    root = Path(root).resolve()
    require_authorization(root)
    cfg, arm, stream = validate(root)
    with (root / ".campaign.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Authorization is checked before either credential access or broker creation.
        broker = Broker(root, Path(cfg["credential_file"]).read_text().strip(), cfg)
        runner = TrialRunner(root, broker, cfg["runtime"]["gateway_port"])
        runner.pins = read(root / "native-source-pins.json")
        statefile = root / "native-campaign.json"
        state = read(statefile) if statefile.exists() else {
            "schema": "within-chain-campaign-v1", "created": time.time(),
            "configuration_sha256": sha(root / "configuration.json"), "active": {},
            "arms": {arm: {"rows": [], "updates": [], "workspace": None,
                     "skills": str(root / "initial/skills"), "note": str(root / "initial/note.md"),
                     "pending_update": None, "status": "ready"}}}
        if state["configuration_sha256"] != sha(root / "configuration.json"):
            raise RuntimeError("saved campaign configuration changed")
        server = Server(("127.0.0.1", runner.port), broker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        item = state["arms"][arm]

        def save(status="running"):
            state.update(state=status, updated=time.time(), pid=os.getpid())
            atomic(statefile, state)
            atomic(root / "heartbeat.json", {"pid": os.getpid(), "time": time.time(), "active": state["active"]})
            run_report(root, state)

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                while len(item["rows"]) < len(stream) or item["pending_update"] is not None:
                    pilot_limit = cfg['runtime'].get('pilot_formal_task_limit')
                    if pilot_limit and len(item['rows']) >= pilot_limit:
                        (root / 'PAUSE').write_text('Configured first-task pilot complete; review before continuation.\n')
                    if (root / "PAUSE").exists() or (Path(cfg["session_root"]) / "HOLD").exists():
                        save("paused")
                        return
                    updating = item["pending_update"] is not None
                    index = item["pending_update"] if updating else len(item["rows"])
                    out = root / "arms" / arm / "steps" / f"{index:06d}"
                    state["active"] = {arm: {"index": index, "phase": "rsi_update" if updating else "formal"}}
                    item["status"] = "running"
                    save()
                    if updating and arm == "evoskill":
                        future = pool.submit(lambda: asyncio.run(evolve(runner, index, item["workspace"], out / "update")))
                    elif updating:
                        future = pool.submit(notes.update, runner, arm, index, item["rows"], item["note"], out / "update")
                    else:
                        kwargs = {"skills": Path(item["skills"])} if arm == "evoskill" else {"note": Path(item["note"])}
                        future = pool.submit(runner.run, arm, index, out / "formal", **kwargs)
                    while not future.done():
                        save()
                        time.sleep(10)
                    try:
                        value = future.result()
                        if updating:
                            if not value.get("execution", {}).get("passed"):
                                raise RuntimeError("update lacks actual execution evidence")
                            item["updates"].append(value)
                            if arm == "evoskill":
                                from evoskill_swe.skills import hashes
                                value["deployed_changed"] = value["skills_hashes"] != hashes(item["skills"])
                                item.update(workspace=value["workspace"], skills=value["skills"])
                            else:
                                item["note"] = value["note"]
                            item["pending_update"] = None
                            if len(item["updates"]) == 1:
                                atomic(root / "first-update-acceptance.json", {
                                    "passed": True, "execution": value["execution"],
                                    "algorithmic_no_candidate_allowed": True,
                                    "deployed_changed": value.get("deployed_changed", False),
                                    "index": index, "time": time.time()})
                        else:
                            if value.get("exception"):
                                raise RuntimeError("formal infrastructure/agent exception; preserve for review")
                            if not item["rows"]:
                                acceptance(root, runner, value)
                            item["rows"].append(value)
                            if len(item["rows"]) % 4 == 0 and len(item["rows"]) < len(stream):
                                item["pending_update"] = index
                        item.update(status="ready")
                        item.pop("error", None)
                        state["active"] = {}
                        save()
                    except NetworkUnavailable as exc:
                        item.update(status="waiting_for_network", error=str(exc))
                        save("waiting_for_network")
                        time.sleep(cfg["runtime"]["network_recovery_cooldown_seconds"])
                    except BaseException as exc:
                        status = "budget_exhausted" if isinstance(exc, SpendBlocked) else "needs_review"
                        item.update(status=status, error={"type": type(exc).__name__, "message": str(exc),
                                                        "traceback": traceback.format_exc()})
                        save(status)
                        return
            save("completed")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    run(parser.parse_args().root)
