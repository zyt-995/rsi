"""Recognize official bootstrap syntax without changing tests, pins or Python minors."""
import re
import shlex
OCAML_URL = 'https://github.com/sadiqj/ocaml/'


def translate(script):
    script = script.replace('\\\n', ' ')
    requirements, packages, lines, replacements = [], [], [], []
    runtime, executable, calls, minor = 'native', None, 0, None
    for line in script.splitlines():
        stripped = line.strip()
        if stripped == 'apt-get update':
            replacements.append(line); continue
        if stripped.startswith('apt-get install -y '):
            names = shlex.split(stripped)[3:]
            if any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name) for name in names):
                raise ValueError('nonliteral_apt_dependencies')
            packages.extend(names); replacements.append(line); continue
        if stripped in ('curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh', 'source $HOME/.local/bin/env'):
            replacements.append(line); continue
        if stripped.startswith('pip install '):
            requirements.extend(shlex.split(stripped)[2:]); replacements.append(line); continue
        if stripped.startswith('uvx '):
            args = shlex.split(stripped)[1:]
            if args[:1] != ['-p'] or args[1] not in ('3.12', '3.13'):
                raise ValueError('unsupported_python_minor')
            minor = args[1]; args = args[2:]
            while args[:1] == ['-w']:
                requirements.append(args[1]); args = args[2:]
            if args[:1] != ['pytest']: raise ValueError('nonpytest_uvx_command')
            runtime, executable = 'standalone', 'python3'
            line = '"$RSI_PYTEST_PYTHON" -P "$RSI_PYTEST_WRAPPER" ' + shlex.join(args[1:]); calls += 1
        else:
            match = re.fullmatch(r'((?:/[A-Za-z0-9_.-]+)*/?python[0-9.]*) -m pytest (.+)', stripped)
            if match:
                if executable and executable != match[1]: raise ValueError('mixed_python_commands')
                executable = match[1]
                line = '"$RSI_PYTEST_PYTHON" -P "$RSI_PYTEST_WRAPPER" ' + match[2]; calls += 1
        lines.append(line)
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9.+_-]+', r) for r in requirements):
        raise ValueError('dependencies_must_retain_literal_official_pins')
    body = '\n'.join(lines) + '\n'
    if calls not in (1, 2) or not executable or '/tests/test_outputs.py' not in body:
        raise ValueError('unsupported_official_test_layout')
    if calls == 2 and ('original-repo-ctrf.json' not in body or 'ORIGINAL_EXIT_CODE' not in body):
        raise ValueError('unreviewed_multiple_suites')
    return {'script': body, 'runtime': runtime, 'executable': executable, 'python_minor': minor,
        'requirements': list(dict.fromkeys(requirements)), 'apt_packages': list(dict.fromkeys(packages)),
        'pytest_calls': calls, 'ocaml_fixture': OCAML_URL in body, 'bootstrap_replacements': replacements}
