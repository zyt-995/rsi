"""Isolate agent-added Go test symbols from private benchmark test symbols.

Only new top-level functions in agent-modified *_test.go files can be renamed.
Production code, private tests, assertions, test commands and required test IDs
are unchanged. Apply only inside the disposable private grader, with evidence.
"""
import hashlib
from pathlib import Path
import re

# Preserve literals and comments verbatim while replacing actual identifiers.
TOKEN = re.compile(r'//[^\n]*|/\*.*?\*/|`[^`]*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\S', re.DOTALL)


def tokens(source):
    return [m for m in TOKEN.finditer(source) if not m[0].startswith(('//', '/*'))]


def declarations(source):
    """Read package and top-level free-function names, excluding methods."""
    ts = tokens(source)
    package, names, depth = None, set(), 0
    for i, token in enumerate(ts):
        word = token[0]
        if depth == 0 and word == 'package' and i + 1 < len(ts):
            package = ts[i + 1][0]
        if depth == 0 and word == 'func' and i + 2 < len(ts):
            name, following = ts[i + 1][0], ts[i + 2][0]
            if re.fullmatch(r'[A-Za-z_]\w*', name) and following in ('(', '[') and name != 'init':
                names.add(name)
        if word == '{':
            depth += 1
        elif word == '}':
            depth -= 1
    return package, names


def replace_identifiers(source, replacements):
    changes, previous = [], None
    for token in tokens(source):
        word = token[0]
        if word in replacements and previous != '.':
            changes.append((token.start(), token.end(), replacements[word]))
        previous = word
    for start, end, replacement in reversed(changes):
        source = source[:start] + replacement + source[end:]
    return source


def isolate_sources(official, candidates, base):
    """Pure transformation used by both regression tests and private grading."""
    private = {}
    for path, source in official.items():
        package, names = declarations(source)
        private.setdefault((str(Path(path).parent), package), set()).update(names)
    mapping = {}
    for path, source in sorted(candidates.items()):
        package, names = declarations(source)
        _, old_names = declarations(base.get(path, ''))
        key = (str(Path(path).parent), package)
        collisions = (names - old_names) & private.get(key, set())
        for name in sorted(collisions):
            salt = hashlib.sha256(f'{key}:{path}:{name}'.encode()).hexdigest()[:16]
            is_test = name != 'TestMain' and re.match(r'^Test(?:[^a-z]|$)', name)
            replacement = ('TestSWEAgent' if is_test else 'SWEAgent') + salt
            mapping.setdefault(key, {}).setdefault(name, replacement)
    fixed = {}
    for path, source in candidates.items():
        package, _ = declarations(source)
        renames = mapping.get((str(Path(path).parent), package), {})
        if renames:
            value = replace_identifiers(source, renames)
            if value != source:
                fixed[path] = {'source': value, 'renamed_symbols': renames}
    return fixed


def apply(item, container, output, docker, copy_into, atomic, official_paths):
    """Inspect private grader files; never edit the submitted patch or solver."""
    paths = [p for p in official_paths if p.endswith('_test.go')]
    if not paths:
        return
    def get(path, baseline=False):
        if baseline:
            result = docker('exec', '--user', '0', container, 'git', '-C', '/testbed',
                            'show', item['raw']['base_commit'] + ':' + path, check=False)
        else:
            result = docker('exec', '--user', '0', container, 'cat', '/testbed/' + path, check=False)
        return result.stdout.decode() if result.returncode == 0 else ''
    changed = docker('exec', '--user', '0', container, 'git', '-C', '/testbed',
                     'diff', '--name-only', '-z', '--diff-filter=AM', item['raw']['base_commit']).stdout
    untracked = docker('exec', '--user', '0', container, 'git', '-C', '/testbed',
                       'ls-files', '--others', '--exclude-standard', '-z').stdout
    candidate_paths = sorted({p.decode() for p in (changed + untracked).split(b'\0') if p
                              and p.decode().endswith('_test.go') and p.decode() not in paths})
    if any(Path(p).is_absolute() or '..' in Path(p).parts for p in candidate_paths + paths):
        raise ValueError('unsafe Go test path')
    official = {p: get(p) for p in paths}
    candidates = {p: get(p) for p in candidate_paths}
    base = {p: get(p, baseline=True) for p in candidate_paths}
    changes = isolate_sources(official, candidates, base)
    evidence = []
    for path, change in changes.items():
        target = output / 'go-test-isolation' / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change['source'])
        copy_into(container, target, '/testbed/' + path)
        evidence.append({'path': path, 'renamed_symbols': change['renamed_symbols'],
                         'before_sha256': hashlib.sha256(candidates[path].encode()).hexdigest(),
                         'after_sha256': hashlib.sha256(change['source'].encode()).hexdigest()})
    if any(get(p) != original for p, original in official.items()):
        raise RuntimeError('Go test isolation changed benchmark test contents')
    atomic(output / 'go-test-isolation.json', {
        'policy': 'namespace only new agent test functions that collide with private test functions',
        'files': evidence, 'official_tests_unchanged': True, 'production_patch_unchanged': True,
        'official_commands_and_required_test_ids_unchanged': True,
        'private_grader_only': True})
