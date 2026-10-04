import json
import os
from pathlib import Path

import pytest

from offline_verifier import profiles, runner
from offline_verifier.grade import browser_infrastructure_failure

DATASET = Path(__file__).resolve().parents[1] / 'benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo'


@pytest.mark.parametrize('chain,count,kind', [
    ('break-filter-js-from-html', 5, 'html'), ('bn-fit-modify', 4, 'stats'),
    ('build-cython-ext', 5, 'cython'), ('chess-best-move', 5, 'plain')])
def test_registered_native_profiles(chain, count, kind):
    for i in range(count):
        name = chain + (f'-EVO-{i}' if i else '')
        expected = 'native' if chain == 'chess-best-move' and i else kind
        assert profiles.profile(DATASET / name) == expected


def test_unknown_task_is_not_silently_guessed(tmp_path):
    with pytest.raises(ValueError, match='unsupported'):
        profiles.profile(tmp_path)


def test_changed_dependency_version_rejected(tmp_path):
    task = tmp_path / 'break-filter-js-from-html'
    (task / 'tests').mkdir(parents=True)
    text = (DATASET / task.name / 'tests/test.sh').read_text().replace('8.4.1', '8.4.2')
    (task / 'tests/test.sh').write_text(text)
    with pytest.raises(ValueError, match='bootstrap'):
        profiles.profile(task)


def test_tests_digest_ignores_only_pycache(tmp_path):
    (tmp_path / 'tests/__pycache__').mkdir(parents=True)
    (tmp_path / 'tests/test.sh').write_text('test')
    (tmp_path / 'tests/__pycache__/foo.pyc').write_text('cache')
    assert list(profiles.test_hashes(tmp_path)) == ['test.sh']


def result_fixture(root, failed=0):
    summary = {'tests': 1, 'passed': 1-failed, 'failed': failed, 'pending': 0, 'other': 0, 'skipped': 0}
    values = {'offline-result.json': {'status': 'complete', 'task': 'example', 'reward': 1-failed},
              'network.json': {'network': 'isolated_loopback_only', 'loopback': 'passed', 'capabilities_dropped': True},
              'ctrf.json': {'results': {'summary': summary, 'tests': [{'status': 'failed' if failed else 'passed'}]}}}
    for name, value in values.items():
        (root / name).write_text(json.dumps(value))
    (root / 'reward.txt').write_text(str(1-failed))


@pytest.mark.parametrize('failed', [0, 1])
def test_real_pass_and_fail_are_valid(tmp_path, failed):
    result_fixture(tmp_path, failed)
    assert runner.validate_result(tmp_path, 'example')['reward'] == 1-failed


@pytest.mark.parametrize('name,field,value', [
    ('network.json', 'network', 'host'), ('network.json', 'capabilities_dropped', False),
    ('offline-result.json', 'task', 'different'), ('offline-result.json', 'reward', 0)])
def test_missing_isolation_or_inconsistent_score_rejected(tmp_path, name, field, value):
    result_fixture(tmp_path)
    payload = json.loads((tmp_path / name).read_text())
    payload[field] = value
    (tmp_path / name).write_text(json.dumps(payload))
    with pytest.raises(RuntimeError):
        runner.validate_result(tmp_path, 'example')


def test_dependency_failure_is_not_zero(tmp_path):
    result_fixture(tmp_path, failed=1)
    (tmp_path / 'offline-error.json').write_text('{"error":"missing_wheel"}')
    with pytest.raises(RuntimeError, match='infrastructure'):
        runner.validate_result(tmp_path, 'example')


def test_skipped_tests_are_not_ready(tmp_path):
    result_fixture(tmp_path)
    report = json.loads((tmp_path / 'ctrf.json').read_text())
    report['results']['summary']['skipped'] = 1
    (tmp_path / 'ctrf.json').write_text(json.dumps(report))
    with pytest.raises(RuntimeError):
        runner.validate_result(tmp_path, 'example')


def test_pid_resolution_is_exact_and_unambiguous(tmp_path):
    for pid, line in [('100', 'NSpid:\t100\t23\t1'), ('101', 'NSpid:\t101\t24\t1')]:
        (tmp_path / pid).mkdir()
        (tmp_path / pid / 'status').write_text(line)
    assert runner.host_pid(23, tmp_path) == 100
    with pytest.raises(RuntimeError):
        runner.host_pid(25, tmp_path)
    (tmp_path / '101/status').write_text('NSpid:\t101\t23\t1')
    with pytest.raises(RuntimeError):
        runner.host_pid(23, tmp_path)


def test_manifest_and_bundle_hashes_are_enforced(tmp_path):
    bundle = tmp_path / 'runtime.tar.gz'
    bundle.write_bytes(b'payload')
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps({'schema': profiles.SCHEMA, 'bundle': str(bundle),
                               'bundle_sha256': profiles.sha(bundle), 'code': {}}))
    digest = profiles.sha(path)
    assert runner.load_manifest(path, digest)['schema'] == profiles.SCHEMA
    bundle.write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='bundle'):
        runner.load_manifest(path, digest)
    with pytest.raises(RuntimeError, match='manifest'):
        runner.load_manifest(path, 'wrong')


def test_existing_result_is_never_overwritten(tmp_path):
    (tmp_path / 'offline-result.json').write_text('{}')
    with pytest.raises(RuntimeError, match='never_overwrite'):
        runner.grade_container('unused', 'unused', {}, tmp_path)


def test_error_text_in_unexecuted_source_is_not_infrastructure_failure():
    assert not browser_infrastructure_failure([{'trace':
        '    assert result.returncode == 0, "Filter failed with error:"\n'
        'E       AssertionError: No alert detected after filtering\nE       assert False'}])


@pytest.mark.parametrize('error', ['selenium.common.exceptions.SessionNotCreatedException: browser unavailable',
                                  'AssertionError: Filter failed with error: missing package'])
def test_actual_browser_or_trusted_filter_failure_is_infrastructure(error):
    assert browser_infrastructure_failure([{'trace': 'E       ' + error}])
