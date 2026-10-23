"""Available-skill discovery for a Harbor inner agent without a native Skill tool."""
from pathlib import Path
import hashlib
import re
import yaml


def hashes(root):
    from swe_chain_evo.common import sha
    root = Path(root)
    if root.is_symlink():
        raise ValueError("skill snapshots must not contain symlinks")
    values = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("skill snapshots must not contain symlinks")
        if path.is_file():
            values[str(path.relative_to(root))] = sha(path)
    return values


def inventory(root):
    root = Path(root)
    hashes(root)  # reject links before any skill contents are opened
    items, invalid = [], []
    for path in sorted(root.glob("*/SKILL.md")):
        text = path.read_text()
        try:
            if not text.startswith("---\n"):
                raise ValueError("missing YAML frontmatter")
            metadata = yaml.safe_load(text.split("---", 2)[1])
            if not isinstance(metadata, dict):
                raise ValueError("invalid YAML metadata")
            name, description = metadata.get("name"), metadata.get("description")
            if name != path.parent.name or not isinstance(description, str) or not description.strip():
                raise ValueError("name and description are required")
            items.append({"name": name, "description": description,
                          "path": "/skills/" + name + "/SKILL.md"})
        except (ValueError, yaml.YAMLError, IndexError) as exc:
            invalid.append({"path": str(path.relative_to(root)), "error": str(exc)})
    return items, invalid


def require_inventory(root):
    """Reject unusable skill snapshots before any rollout/model call."""
    if not Path(root).is_dir():
        raise ValueError("skill snapshot directory is missing")
    items, invalid = inventory(root)
    if invalid:
        details = "; ".join(f"{s['path']}: {s['error']}" for s in invalid)
        raise ValueError("undiscoverable task skills: " + details)
    return items


def normalize_generated(root, fallback_description):
    """Apply the official format utility at the KIRA adapter boundary.

    Claude SDK mode skips this utility in the upstream loop. KIRA's YAML
    inventory needs it too. Valid files are never rewritten; task skill bodies
    are preserved byte for byte. No learning, selection, or rollout changes.
    """
    from src.harness.opencode.skill_utils import ensure_skill_frontmatter
    root = Path(root)
    _, invalid = inventory(root)
    repairs = []
    for issue in invalid:
        path = root / issue['path']
        before = path.read_bytes().decode('utf-8')
        match = re.match(r"\A---\n(.*?)\n---\n?", before, re.DOTALL)
        if not match:
            raise ValueError("cannot safely normalize missing/malformed skill frontmatter: " + issue['path'])
        # Retain the model's description when only YAML quoting is malformed.
        description = fallback_description
        field = re.search(r"(?m)^description:[ \t]*(.+)$", match[1])
        if field and field[1].strip() not in ('|', '>', '|-', '>-', '|+', '>+'):
            raw = field[1].strip()
            try:
                parsed = yaml.safe_load('description: ' + raw)
                description = parsed['description'] if isinstance(parsed.get('description'), str) else description
            except yaml.YAMLError:
                description = raw
        body = before[match.end():]
        ensure_skill_frontmatter(path, description=description, compatibility='claude')
        normalized = path.read_text()
        new_match = re.match(r"\A---\n(.*?)\n---\n?", normalized, re.DOTALL)
        if not new_match:
            raise ValueError("official skill normalizer did not produce frontmatter")
        path.write_text(normalized[:new_match.end()] + body)
        repairs.append({'path': issue['path'], 'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
                        'after_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'body_unchanged': True,
                        'normalizer': 'official ensure_skill_frontmatter'})
    items = require_inventory(root)
    return {'skills': items, 'normalizations': repairs, 'passed': True}


def verify_discovery(result_path, skills):
    """A score is usable only after the solver saw this exact inventory."""
    import json
    expected = require_inventory(skills) if skills is not None else []
    path = Path(result_path).parent / 'agent/skill-discovery.json'
    if not path.is_file():
        raise RuntimeError('skill-discovery evidence missing from scored rollout')
    observed = json.loads(path.read_text())
    if observed.get('invalid_skills') or observed.get('skills') != expected:
        raise RuntimeError('scored rollout did not discover the expected task skills')
    return {'path': str(path), 'skills': expected, 'passed': True}


def append_inventory(instruction, items):
    if not items:
        return instruction
    lines = ["\n\nAvailable reusable skills from previously completed milestones:",
             "When relevant, read the skill's SKILL.md with your terminal tools and follow it.",
             "The skill library is mounted read-only at /skills."]
    lines += [f"- {s['name']}: {s['description']} (file: {s['path']})" for s in items]
    return instruction + "\n".join(lines)
