#!/usr/bin/python3
"""Run the official SDK's Claude CLI with only the current EvoSkill workspace visible."""
import os
from pathlib import Path
import shutil
import sys

workspace = Path(os.environ["EVOSKILL_META_WORKSPACE"]).resolve()
vendor = Path(__file__).resolve().parents[1] / "upstream/EvoSkill"
binary = Path(shutil.which("claude")).resolve()
args = ["bwrap", "--die-with-parent", "--unshare-user", "--unshare-pid", "--clearenv"]
for name, value in os.environ.items():
    if (name.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE", "EVOSKILL_"))
            or name in ("PATH", "HOME", "LANG", "LC_ALL", "NO_PROXY", "no_proxy", "USER", "SHELL", "TMPDIR", "RSI_FORBIDDEN_PATHS")):
        args += ["--setenv", name, value]
for path in ("/usr", "/bin", "/lib", "/lib64", "/etc/ssl", "/etc/resolv.conf", "/etc/hosts"):
    args += ["--ro-bind", path, path]
args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/opt",
         "--ro-bind", str(binary), "/opt/claude", "--bind", str(workspace), str(workspace)]
# The framework owns git metadata/configuration; meta agents edit skill files only.
for path in (workspace / ".git", workspace / ".claude/program.yaml", workspace / "public-history"):
    if path.exists():
        args += ["--ro-bind", str(path), str(path)]
if os.environ.get("EVOSKILL_META_ROLE") == "skill_proposer":
    args += ["--ro-bind", str(workspace / ".claude/skills"), str(workspace / ".claude/skills")]
for name in ("brainstorming", "skill-creator"):
    args += ["--ro-bind", str(vendor / ".claude/skills" / name), str(workspace / ".claude/skills" / name)]
if os.environ.get("EVOSKILL_SANDBOX_CHECK") == "1":
    check = """from pathlib import Path
import os
p=Path.cwd()
assert (p/'.claude/skills/brainstorming/SKILL.md').is_file()
assert (p/'.claude/skills/skill-creator/SKILL.md').is_file()
for secret_path in os.environ.get('RSI_FORBIDDEN_PATHS', '').split(os.pathsep):
 if secret_path:
  assert not Path(secret_path).exists()

try:
 (p/'.git/config').write_text('forbidden')
except OSError:
 pass
else:
 raise AssertionError('git metadata writable')
print('sandbox checks passed')
"""
    args += ["--chdir", str(workspace), "/usr/bin/python3", "-c", check]
else:
    args += ["--chdir", str(workspace), "/opt/claude", *sys.argv[1:]]
os.execvp("bwrap", args)
