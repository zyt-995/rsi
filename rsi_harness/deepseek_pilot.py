"""Isolated, one-chain DeepSeek pilot using the frozen clean RSI agent and grader.

The upstream API key lives only in this coordinator's memory. Harbor workers use
an authenticated localhost gateway with a disposable token instead of the key.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from . import clean_campaign as core
from .clean_rsi import ARMS, IMPROVE, SOLVE, atomic_json, digest, read_json
from .clean_scheduler import paired_metrics

CHAIN = "build-pmars"
MODEL = "deepseek-flash"
PORT = 8055
ROOT = core.PROJECT / "runs/deepseek-flash-build-pmars-20260924"
ENV_FILE = ROOT / "llm-proxy.env"
API_URL = "https://api.deepseek.com/chat/completions"


def transform_request(body: dict) -> dict:
    """Preserve task payloads; translate only provider-specific routing knobs."""
    payload = {k: v for k, v in body.items() if k not in ("chat_template_kwargs", "session_id")}
    payload["model"] = MODEL
    if (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False:
        payload["thinking"] = {"type": "disabled"}
        payload.pop("reasoning_effort", None)
    else:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "high"
    return payload


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, key: str, token: str, root: Path):
        super().__init__(address, GatewayHandler)
        self.key = key
        self.token = token
        self.root = root
        self.usage_lock = threading.Lock()

    def audit(self, record: dict) -> None:
        with self.usage_lock:
            with (self.root / "api_usage.jsonl").open("a") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")


class GatewayHandler(BaseHTTPRequestHandler):
    server: Gateway

    def log_message(self, format: str, *args) -> None:
        # Do not log requests, headers, prompts, or API responses.
        pass

    def send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self.send_json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self.send_json(404, {"error": {"message": "not found"}})
            return
        if self.headers.get("Authorization") != "Bearer " + self.server.token:
            self.send_json(401, {"error": {"message": "unauthorized"}})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 16_000_000:
                raise ValueError("invalid request size")
            payload = transform_request(json.loads(self.rfile.read(size)))
            if payload.get("stream"):
                raise ValueError("streaming not supported by pilot gateway")
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": {"message": "invalid request"}})
            return
        started = time.monotonic()
        status, data = 502, None
        for attempt in range(3):
            request = urllib.request.Request(
                API_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.server.key,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=240) as response:
                    data = json.load(response)
                status = 200
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status not in (408, 429, 500, 502, 503, 504):
                    break
            except (OSError, ValueError):
                status = 502
            if attempt < 2:
                time.sleep(2 ** attempt)
        record = {
            "time": time.time(),
            "status": status,
            "attempts": attempt + 1,
            "wall_seconds": round(time.monotonic() - started, 3),
            "thinking": payload["thinking"]["type"],
            "usage": (data or {}).get("usage", {}),
        }
        self.server.audit(record)
        if status == 200 and isinstance(data, dict):
            self.send_json(200, data)
        else:
            self.send_json(status, {"error": {"message": f"upstream HTTP {status}"}})


def patched_stage(args) -> int:
    if args.chain != CHAIN or args.task not in core.tasks(CHAIN) or args.arm not in ARMS:
        raise ValueError("pilot permits exactly one chain and three fixed arms")
    manifest = read_json(args.root / "manifest.json", {})
    if manifest.get("model") != MODEL or manifest.get("chain") != CHAIN:
        raise ValueError("pilot manifest mismatch")
    token = read_json(args.root / "gateway.json", {}).get("token")
    if not token:
        raise ValueError("gateway token unavailable")
    core.MODEL = MODEL
    original_environment = core.runner_environment
    original_improve = core.improve
    original_request = urllib.request.Request

    def runner_environment(port: int) -> dict:
        env = original_environment(port)
        env["HARBOR_EVOMEM_LLM_ENV"] = str(ENV_FILE)
        return env

    def authenticated_request(url, *a, **kw):
        if str(url).startswith(f"http://127.0.0.1:{PORT}/v1/chat/completions"):
            headers = dict(kw.get("headers") or {})
            headers["Authorization"] = "Bearer " + token
            kw["headers"] = headers
        return original_request(url, *a, **kw)

    def improve(*a, **kw):
        with patch.object(urllib.request, "Request", authenticated_request):
            return original_improve(*a, **kw)

    with patch.object(core, "runner_environment", runner_environment), patch.object(
        core, "legacy_baseline", lambda *_: None
    ), patch.object(core, "improve", improve):
        return core.run_stage(args)


def manifest() -> dict:
    return {
        "schema": "deepseek-one-chain-pilot-v1",
        "model": MODEL,
        "chain": CHAIN,
        "tasks": core.tasks(CHAIN),
        "arms": ARMS,
        "solve": SOLVE,
        "improve": IMPROVE,
        "baseline_policy": "fresh-solve-no-qwen-reuse",
        "chain_reset": True,
        "frozen_source_hashes": core.source_hashes(),
        "pilot_source_sha256": digest(Path(__file__).read_text()),
    }


def status(root: Path, active: dict, terminal: str | None = None) -> dict:
    rows, pending, blocked, summary = core.status(root, [CHAIN])
    return {
        "time": time.time(),
        "chain": CHAIN,
        "model": MODEL,
        "summary": summary,
        "paired": paired_metrics(rows, [CHAIN]),
        "active": active,
        "pending": pending,
        "blocked": blocked,
        "terminal": terminal,
    }


def run_campaign(root: Path, key: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    candidate = manifest()
    prior = read_json(root / "manifest.json")
    if prior and digest(prior) != digest(candidate):
        raise RuntimeError("pilot manifest changed")
    atomic_json(root / "manifest.json", candidate)
    token = secrets.token_urlsafe(32)
    atomic_json(root / "gateway.json", {"token": token})
    ENV_FILE.write_text(
        f"LLM_API_KEY={token}\nLLM_MODEL=openai/{MODEL}\n"
        f"LLM_BASE_URL=http://127.0.0.1:{PORT}/v1\n"
    )
    ENV_FILE.chmod(0o600)
    server = Gateway(("127.0.0.1", PORT), key, token, root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(json.dumps({"event": "gateway_ready", "port": PORT, "time": time.time()}), flush=True)
    active: dict[str, dict] = {}
    children: dict[str, subprocess.Popen] = {}
    done = False
    quarantined = False

    def stop(_signum, _frame):
        nonlocal done
        done = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not done:
            rows, pending, blocked, _summary = core.status(root, [CHAIN])
            for arm, worker in list(active.items()):
                process = children[arm]
                if process.poll() is not None:
                    record = core.stage_path(root, CHAIN, arm, worker["task"]) / "record.json"
                    if not record.exists():
                        atomic_json(
                            record,
                            {"status": "blocked", "reason": "worker_exited_without_record"},
                        )
                    active.pop(arm)
                    children.pop(arm)
            rows, pending, blocked, _summary = core.status(root, [CHAIN])
            if blocked and not quarantined:
                quarantined = True
                print(json.dumps({"event": "quarantined", "count": len(blocked)}), flush=True)
            for chain, arm, task in ([] if quarantined else pending):
                if arm in active:
                    continue
                log = core.stage_path(root, chain, arm, task) / "stage.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                command = [
                    sys.executable, "-u", "-m", "rsi_harness.deepseek_pilot", "stage",
                    "--root", str(root), "--chain", chain, "--arm", arm,
                    "--task", task, "--port", str(PORT),
                ]
                with log.open("ab", buffering=0) as stream:
                    process = subprocess.Popen(
                        command, cwd=core.PROJECT, env=os.environ.copy(),
                        stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
                    )
                children[arm] = process
                active[arm] = {"pid": process.pid, "task": task, "started": time.time()}
                print(json.dumps({"event": "stage_started", "arm": arm, "task": task, "pid": process.pid}), flush=True)
            terminal = (
                "needs_review" if quarantined and not active else
                "complete" if not pending and not active else None
            )
            atomic_json(root / "status.json", status(root, active, terminal))
            if terminal:
                print(json.dumps({"event": terminal, "time": time.time()}), flush=True)
                break
            time.sleep(5)
    finally:
        server.shutdown()
        server.server_close()
        if read_json(root / "status.json", {}).get("terminal") == "complete":
            with contextlib.suppress(FileNotFoundError):
                ENV_FILE.unlink()
            with contextlib.suppress(FileNotFoundError):
                (root / "gateway.json").unlink()
    return 0 if read_json(root / "status.json", {}).get("terminal") == "complete" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("start", "stage"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--chain", default=CHAIN)
    parser.add_argument("--arm")
    parser.add_argument("--task")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.old_root = core.OLD_ROOT
    if args.operation == "stage":
        return patched_stage(args)
    if args.chain != CHAIN or args.port != PORT or args.root != ROOT:
        raise ValueError("fixed one-chain pilot configuration")
    for task in core.tasks(CHAIN):
        ok, reason = core.ready(task, core.OLD_ROOT)
        if not ok:
            raise RuntimeError(f"preflight failed for {task}: {reason}")
    key = getpass.getpass("DeepSeek API key (hidden): ").strip()
    if not key:
        raise ValueError("API key required")
    pid = os.fork()
    if pid:
        print(json.dumps({"event": "pilot_daemon_started", "pid": pid, "root": str(ROOT)}))
        return 0
    os.setsid()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    log = (ROOT / "pilot.log").open("a", buffering=1)
    null = open(os.devnull, "r")
    os.dup2(null.fileno(), sys.stdin.fileno())
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())
    try:
        code = run_campaign(ROOT, key)
    except Exception as exc:
        print(json.dumps({"event": "coordinator_error", "type": type(exc).__name__, "time": time.time()}), flush=True)
        code = 2
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
