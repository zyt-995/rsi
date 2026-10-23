"""Keep hidden grading artifacts outside the Meta proposer bind mount."""
from pathlib import Path


def private_root(root):
    return Path(root) / "arms/meta-harness/private-evaluations"


def isolate_legacy_trials(root):
    """Retain legacy host paths while making their targets absent in the sandbox."""
    root = Path(root)
    workspace = root / "native-meta-workspace"
    for source in (workspace / "jobs").glob("*/*/evo-trials"):
        target = private_root(root) / source.relative_to(workspace)
        if source.is_symlink():
            if source.resolve() != target.resolve():
                raise ValueError("unexpected private evaluation link")
            continue
        if source.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError("private trial archive already exists")
            source.rename(target)
            source.symlink_to(target, target_is_directory=True)


class NativeMetaRunner:
    def __init__(self, runner):
        self.runner = runner

    def __getattr__(self, key):
        return getattr(self.runner, key)

    def run(self, arm, index, output, **kwargs):
        if arm != "meta-harness" or kwargs.get("phase") != "validation":
            raise ValueError("Meta native adapter may only launch validation")
        workspace = self.root / "native-meta-workspace"
        relative = Path(output).relative_to(workspace)
        if relative.parts[0] != "jobs" or "evo-trials" not in relative.parts:
            raise ValueError("unexpected native validation path")
        return self.runner.run(arm, index, private_root(self.root) / relative, **kwargs)
