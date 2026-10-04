import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("meta_resources", Path(__file__).parents[1] / "scripts/resize-meta-skill-workers.py")
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


def test_only_resource_fields_change():
    old = {"ports": [8040, 8041], "concurrency": 2, "solve": {"calls": 15},
           "task_order": {"chain": ["a", "b"]}, "method": "same"}
    new = resources.resource_manifest(old, [8040, 8041, 8042, 8044])
    assert old["ports"] == [8040, 8041]
    assert new["concurrency"] == 4
    assert set(k for k in old if old[k] != new[k]) == {"ports", "concurrency"}
    new["solve"]["calls"] = 99
    assert old["solve"]["calls"] == 15


@pytest.mark.parametrize("ports", [[8040], [8040, 8041, 8041], [8040, 8041, 8888], []])
def test_invalid_resize_rejected(ports):
    with pytest.raises(ValueError):
        resources.resource_manifest({"ports": [8040, 8041], "concurrency": 2}, ports)


def test_running_supervisor_must_match_exact_process(tmp_path, monkeypatch):
    monkeypatch.setattr(resources.campaign, "alive", lambda _: True)
    monkeypatch.setattr(resources, "proc_args", lambda _: ["python", "another-job.py"])
    import os
    with pytest.raises(RuntimeError, match="unrecognized"):
        resources.owned_supervisor(tmp_path, {"pid": os.getpid(), "identity": "unused"})
