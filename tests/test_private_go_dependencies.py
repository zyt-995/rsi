from pathlib import Path
import subprocess
import pytest
from swe_chain_evo.offline_go_dependencies import missing_modules, CACHE


def test_missing_public_package_is_identified_from_real_offline_network_error():
    log='Get "https://proxy.golang.org/github.com/hashicorp/golang-lru/v2/@v/v2.0.7.zip": dial tcp: network is unreachable'
    assert missing_modules(log)==['github.com/hashicorp/golang-lru/v2@v2.0.7']


def test_normal_wrong_solution_does_not_trigger_dependency_recovery():
    assert missing_modules('udp_session_test.go:25:9: undefined: sessionKey')==[]
    assert missing_modules('--- FAIL: TestLookupSessionMiss (0.00s)')==[]


def test_proxy_path_traversal_is_rejected():
    with pytest.raises(ValueError,match='unsafe'):
        missing_modules('Get "https://proxy.golang.org/github.com/../secret/@v/v1.0.0.zip": network is unreachable')


def test_meta_real_sandbox_cannot_read_private_reference_dependency_cache(tmp_path):
    import shutil
    if not shutil.which('bwrap') or not shutil.which('claude'):
        pytest.skip('real sandbox requires bubblewrap and native Claude binary')
    probe = subprocess.run(['bwrap', '--ro-bind', '/', '/', '--unshare-user', '/bin/true'], capture_output=True)
    if probe.returncode:
        pytest.skip('host does not permit required user namespace')
    from methods.evo_fifty.native_meta import isolated_command
    workspace=tmp_path/'workspace';workspace.mkdir()
    cmd=isolated_command(['claude'],workspace)
    assert cmd[-1]=='/opt/claude'
    cmd=cmd[:-1]+['/usr/bin/python3','-c',f'from pathlib import Path; assert not Path({str(CACHE)!r}).exists(); print("private grader cache hidden")']
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    assert p.returncode==0,p.stderr
    assert 'private grader cache hidden' in p.stdout
