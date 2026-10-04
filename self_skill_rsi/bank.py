"""Persistent, immutable skill versions compatible with CleanTerminalAgent.

Library capacity is independent of prompt capacity: 64 archived-active skills /
128 KiB, but the unchanged solver still retrieves only four / 12,000 characters.
Retirement never destroys older versions or their literal evidence references.
"""
import json
import re
import tempfile
from pathlib import Path

from rsi_harness.clean_rsi import SCHEMA, digest, read_json
from rsi_harness.terminal_infra import atomic_json

LIMITS = {'max_skills': 64, 'state_bytes': 131072, 'max_document_chars': 6000,
          'retrieval_top_k': 4, 'retrieval_chars': 12000}


def validate(payload):
    if not isinstance(payload, dict) or len(payload) > LIMITS['max_skills']:
        raise ValueError('invalid_skill_library_size')
    for key, doc in payload.items():
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', key):
            raise ValueError('invalid_skill_id')
        if not isinstance(doc, str) or not doc.strip() or len(doc) > LIMITS['max_document_chars']:
            raise ValueError('invalid_skill_document')
        if any(s not in doc for s in ('# ', '## When', '## Steps', '## Cautions')):
            raise ValueError('missing_skill_sections')
    if len(json.dumps(payload, ensure_ascii=False).encode()) > LIMITS['state_bytes']:
        raise ValueError('skill_library_bytes_exceeded')
    return payload


def metadata(payload, provenance, revision, task):
    return {'schema': SCHEMA, 'arm': 'skill-rsi', 'revision': revision,
            'sha256': digest(payload), 'provenance_sha256': digest(provenance), 'after_task': task}


def publish(store, payload, provenance, revision, task):
    validate(payload)
    if set(provenance) != set(payload):
        raise ValueError('provenance_must_cover_every_active_skill')
    head = metadata(payload, provenance, revision, task)
    parent = store / 'versions'
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f'{revision:06d}'
    if target.exists():
        if (read_json(target / 'metadata.json') != head or read_json(target / 'payload.json') != payload
                or read_json(target / 'provenance.json') != provenance):
            raise ValueError('immutable_version_collision')
    else:
        with tempfile.TemporaryDirectory(prefix='.candidate-', dir=parent) as tmp:
            version = Path(tmp) / 'version'
            atomic_json(version / 'payload.json', payload)
            atomic_json(version / 'metadata.json', head)
            atomic_json(version / 'provenance.json', provenance)
            for key, doc in payload.items():
                p = version / 'skills' / (key + '.md')
                p.parent.mkdir(exist_ok=True)
                p.write_text(doc)
            version.rename(target)
    atomic_json(store / 'HEAD.json', head)
    return head


def initialize(store):
    if not (store / 'HEAD.json').exists():
        return publish(store, {}, {}, 0, 'initial')
    head = read_json(store / 'HEAD.json')
    load(store, head)
    return head


def load(store, head=None):
    head = head or read_json(store / 'HEAD.json')
    directory = store / 'versions' / f"{head['revision']:06d}"
    payload, provenance = read_json(directory / 'payload.json'), read_json(directory / 'provenance.json')
    if (read_json(directory / 'metadata.json') != head or digest(payload) != head['sha256']
            or digest(provenance) != head['provenance_sha256'] or set(payload) != set(provenance)):
        raise RuntimeError('skill_version_corrupted')
    validate(payload)
    # Detect edits to the human-readable view too, although the solver reads JSON.
    for key, doc in payload.items():
        if (directory / 'skills' / (key + '.md')).read_text() != doc:
            raise RuntimeError('skill_markdown_corrupted')
    return payload, provenance


def promote(store, before, payload, provenance, task):
    expected = metadata(payload, provenance, before['revision'] + 1, task)
    actual = read_json(store / 'HEAD.json')
    if actual == expected:
        load(store, expected)
        return expected  # recover a committed transaction, no new version
    if actual != before:
        raise RuntimeError('stale_update_parent')
    load(store, before)
    return publish(store, payload, provenance, expected['revision'], task)


def restore(store, before, revision, reason):
    """Explicit offline rollback creates a NEW revision, never rewrites history."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('rollback_reason_required')
    previous = read_json(store / 'versions' / f'{revision:06d}' / 'metadata.json')
    if not previous or revision >= before['revision']:
        raise ValueError('rollback_requires_an_earlier_version')
    payload, provenance = load(store, previous)
    return promote(store, before, payload, provenance, 'rollback:' + reason)
