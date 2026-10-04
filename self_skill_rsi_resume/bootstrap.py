"""Translate only audited dependency bootstrap, preserving official test commands."""
import re
import shlex

BASE = ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5']
OCAML_URL = 'https://github.com/sadiqj/ocaml/'
OCAML_TAG = 'tag_purposefully_broken_sweeping_changes'


def translate(script):
    script = script.replace('\\\n', ' ')
    requirements, packages, lines, replacements = [], [], [], []
    runtime, executable, calls = 'native', None, 0
    for line in script.splitlines():
        stripped = line.strip()
        if stripped == 'apt-get update':
            replacements.append(line)
            continue
        if stripped.startswith('apt-get install -y '):
            names = shlex.split(stripped)[3:]
            if not set(names) <= {'curl', 'gcc', 'binutils', 'primer3'}:
                raise ValueError('unreviewed_system_dependencies')
            # curl was used only to fetch uv's installer, which is replaced by
            # the cached uv binary. The builder checks that tests do not use it.
            packages.extend(name for name in names if name != 'curl')
            replacements.append(line)
            continue
        if stripped in ('curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh',
                         'source $HOME/.local/bin/env'):
            replacements.append(line)
            continue
        if stripped.startswith('pip install '):
            if shlex.split(stripped)[2:] != BASE:
                raise ValueError('unreviewed_pip_bootstrap')
            requirements = list(BASE)
            replacements.append(line)
            continue
        if stripped.startswith('uvx '):
            args = shlex.split(stripped)[1:]
            if args[:2] != ['-p', '3.13']:
                raise ValueError('unexpected_python_requirement')
            args = args[2:]
            while args[:1] == ['-w']:
                requirements.append(args[1])
                args = args[2:]
            if args[:1] != ['pytest'] or set(requirements) not in (
                    set(BASE), set(BASE + ['numpy==2.3.0']), set(BASE + ['setuptools==80.9.0'])):
                raise ValueError('unreviewed_uvx_bootstrap')
            runtime, executable = 'standalone', 'python3'
            line = '"$RSI_PYTEST_PYTHON" -P "$RSI_PYTEST_WRAPPER" ' + shlex.join(args[1:])
            calls += 1
        else:
            match = re.fullmatch(r'(python3?) -m pytest (.+)', stripped)
            if match:
                if executable and executable != match[1]:
                    raise ValueError('mixed_native_python_commands')
                executable = match[1]
                line = '"$RSI_PYTEST_PYTHON" -P "$RSI_PYTEST_WRAPPER" ' + match[2]
                calls += 1
        lines.append(line)
    body = '\n'.join(lines) + '\n'
    if calls not in (1, 2) or not executable or '/tests/test_outputs.py' not in body:
        raise ValueError('unsupported_official_test_layout')
    if calls == 2 and ('original-repo-ctrf.json' not in body or 'ORIGINAL_EXIT_CODE' not in body):
        raise ValueError('unreviewed_multiple_test_suites')
    return {'script': body, 'runtime': runtime, 'executable': executable,
            'requirements': requirements, 'apt_packages': list(dict.fromkeys(packages)),
            'pytest_calls': calls, 'ocaml_fixture': OCAML_URL in body,
            'bootstrap_replacements': replacements}
