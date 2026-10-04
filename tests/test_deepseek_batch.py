import json
from types import SimpleNamespace

import pytest

from rsi_harness import clean_campaign as core
from rsi_harness import deepseek_batch as batch


def test_batch_matches_frozen_qwen_order_and_pilot():
    data = batch.manifest()
    assert data["chain_order"] == batch.CHAINS
    assert data["task_order"]["build-pmars"] == core.tasks("build-pmars")
    assert data["frozen_source_hashes"] == core.source_hashes()
    assert data["baseline_policy"] == "fresh-solve-no-qwen-reuse"
    assert sum(map(len, data["task_order"].values())) == 48


def test_next_chain_preserves_frozen_order():
    pending = [
        ("circuit-fibsqrt", "baseline", "circuit-fibsqrt"),
        ("bn-fit-modify", "skill-rsi", "bn-fit-modify-EVO-1"),
    ]
    assert batch.next_chain(pending) == "bn-fit-modify"
    assert batch.next_chain([]) is None


def test_peak_cost_reads_only_usage(tmp_path):
    rows = [
        {"status": 200, "usage": {"prompt_cache_hit_tokens": 1_000_000,
                                   "prompt_cache_miss_tokens": 1_000_000,
                                   "completion_tokens": 1_000_000}},
        {"status": 429, "usage": {}},
    ]
    (tmp_path / "api_usage.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )
    usage = batch.peak_estimate(tmp_path)
    assert usage["requests"] == 2
    assert usage["errors"] == 1
    assert usage["peak_estimate_rmb"] == 10.04


def test_stage_rejects_pilot_and_unknown_chain(tmp_path):
    for chain in ("build-pmars", "not-a-chain"):
        with pytest.raises(ValueError, match="stage chain not allowed"):
            batch.patched_stage(
                SimpleNamespace(root=tmp_path, chain=chain,
                                task=chain, arm="baseline")
            )
