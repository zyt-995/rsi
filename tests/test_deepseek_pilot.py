import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from rsi_harness import deepseek_pilot as pilot


def test_fixed_single_chain_manifest():
    data = pilot.manifest()
    assert data["chain"] == "build-pmars"
    assert data["tasks"] == [
        "build-pmars",
        "build-pmars-EVO-1",
        "build-pmars-EVO-2",
        "build-pmars-EVO-3",
        "build-pmars-EVO-4",
    ]
    assert data["arms"] == ("baseline", "skill-rsi", "harness-rsi")
    assert data["baseline_policy"] == "fresh-solve-no-qwen-reuse"


def test_request_translation_preserves_payload():
    request = {
        "model": "openai/deepseek-flash",
        "messages": [{"role": "user", "content": "test"}],
        "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
    converted = pilot.transform_request(request)
    assert converted["model"] == "deepseek-flash"
    assert converted["thinking"] == {"type": "disabled"}
    assert converted["messages"] == request["messages"]
    assert converted["response_format"] == request["response_format"]
    assert "chat_template_kwargs" not in converted
    assert pilot.transform_request({"messages": []})["reasoning_effort"] == "high"


def test_stage_rejects_other_chain(tmp_path):
    with pytest.raises(ValueError, match="exactly one chain"):
        pilot.patched_stage(
            SimpleNamespace(root=tmp_path, chain="chess-best-move", task="chess-best-move", arm="baseline")
        )


def test_gateway_models_and_auth(tmp_path):
    gateway = pilot.Gateway(("127.0.0.1", 0), "dummy-upstream", "local-token", tmp_path)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{gateway.server_port}/v1"
        with urllib.request.urlopen(url + "/models") as response:
            assert json.load(response)["data"][0]["id"] == "deepseek-flash"
        request = urllib.request.Request(
            url + "/chat/completions",
            data=b"{}",
            headers={"Authorization": "Bearer wrong"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 401
    finally:
        gateway.shutdown()
        gateway.server_close()
