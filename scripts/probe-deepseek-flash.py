"""One-off read-only latency comparison; never persists credentials or responses."""

import getpass
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: probe-deepseek-flash.py REQUEST_JSON ...")
    key = (
        getpass.getpass("API key (input hidden): ").strip()
        if sys.stdin.isatty()
        else sys.stdin.readline().strip()
    )
    if not key:
        raise SystemExit("API key required on stdin")
    for name in sys.argv[1:]:
        path = Path(name)
        agent_turn = path.name == "trajectory.json"
        if agent_turn:
            trajectory = json.loads(path.read_text())
            original = {
                "messages": [
                    {"role": "user", "content": trajectory["steps"][0]["message"]}
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 4096,
            }
            original_result = {}
        else:
            original = json.loads(path.read_text())
            original_result = json.loads((path.parent / "result.json").read_text())
        payload = {
            k: v for k, v in original.items() if k != "chat_template_kwargs"
        }
        payload["model"] = "deepseek-flash"
        payload["thinking"] = {"type": "enabled" if agent_turn else "disabled"}
        if agent_turn:
            payload["reasoning_effort"] = "high"
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                output = json.load(response)
        except urllib.error.HTTPError as exc:
            print(json.dumps({"request": str(path), "http_error": exc.code}))
            return 1
        except (OSError, ValueError) as exc:
            print(json.dumps({"request": str(path), "error_type": type(exc).__name__}))
            return 1
        wall = time.monotonic() - started
        usage = output.get("usage") or {}
        message = (output.get("choices") or [{}])[0]
        content = (message.get("message") or {}).get("content") or ""
        try:
            parsed = json.loads(content)
            valid_json = True
        except ValueError:
            parsed = None
            valid_json = False
        local_usage = original_result.get("usage") or {}
        print(
            json.dumps(
                {
                    "request": str(path),
                    "probe_type": "agent_first_turn" if agent_turn else "improvement",
                    "local_qwen": {
                        "wall_seconds": round(original_result.get("model_wall_seconds") or 0, 2),
                        "prompt_tokens": local_usage.get("prompt_tokens"),
                        "completion_tokens": local_usage.get("completion_tokens"),
                    },
                    "deepseek_flash": {
                        "wall_seconds": round(wall, 2),
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "finish_reason": message.get("finish_reason"),
                        "valid_json": valid_json,
                        "json_keys": sorted(parsed) if isinstance(parsed, dict) else None,
                        "command_count": (
                            len(parsed.get("commands") or [])
                            if isinstance(parsed, dict)
                            else None
                        ),
                        "reasoning_tokens": (
                            (usage.get("completion_tokens_details") or {}).get(
                                "reasoning_tokens"
                            )
                        ),
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
