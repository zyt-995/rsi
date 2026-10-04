"""Stateless bridge for a local OpenAI-compatible chat-completions endpoint.

The harness launches this file once per decision. Keep the path absolute when it
is passed through --agent-command. Authentication and endpoint details remain in
environment variables and are never copied into the experiment request.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SYSTEM = """You are the policy in a controlled continual-agent experiment.
Return exactly one JSON object with this shape:
{"action":{"type":"ONE_AVAILABLE_ACTION","payload":{},"reason":"short reason"}}
Choose only from request.actions and obey that action's payload contract.
Use request.phase_guidance. Persistent changes must be reusable rather than a
copy of a hidden answer. For PROPOSE_AGENT, files is the complete replacement
program and must include agent.py. Do not wrap the JSON in markdown.
"""


def parse_content(content: object) -> dict:
    if not isinstance(content, str):
        raise ValueError("model response content is not a string")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model did not return a JSON object")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict) or not isinstance(value.get("action"), dict):
        raise ValueError("model JSON has no action object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"))
    parser.add_argument("--model", default=os.environ.get("RSI_MODEL"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", "EMPTY"))
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--no-json-mode", action="store_true")
    args = parser.parse_args()
    if not args.model:
        parser.error("--model or RSI_MODEL is required")
    if args.timeout <= 0 or args.max_tokens < 1:
        parser.error("timeout and max-tokens must be positive")

    experiment_request = json.load(sys.stdin)
    body = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(experiment_request, ensure_ascii=False)},
        ],
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    if not args.no_json_mode:
        body["response_format"] = {"type": "json_object"}
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "User-Agent": "dynamic-rsi-harness/0.1"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    http_request = Request(
        endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urlopen(http_request, timeout=args.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"local model endpoint returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("local model endpoint is unreachable") from exc

    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("endpoint response has no choices")
    output = parse_content(choices[0].get("message", {}).get("content"))
    usage = result.get("usage", {})
    if (isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int)
            and isinstance(usage.get("completion_tokens"), int)):
        output["usage"] = {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
        }
    json.dump(output, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
