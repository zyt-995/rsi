"""Opt-in local integration tests; no model calls and no formal experiment scores."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import uuid

import pytest

from offline_verifier.profiles import sha
from offline_verifier.runner import docker, grade_container, load_manifest

MANIFEST = os.environ.get('RSI_OFFLINE_TEST_MANIFEST')
pytestmark = pytest.mark.skipif(not MANIFEST, reason='explicit offline Docker acceptance only')


@pytest.mark.parametrize('task,valid', [
    ('break-filter-js-from-html-EVO-2', True),
    ('break-filter-js-from-html-EVO-2', False),
    ('break-filter-js-from-html-EVO-4', False),
])
def test_saved_positive_and_real_browser_negative(tmp_path, task, valid):
    m = load_manifest(MANIFEST, sha(MANIFEST))
    cid = docker(['create', '--network', 'none', '--name', 'rsi-offline-control-' + uuid.uuid4().hex[:12],
                  '--entrypoint', 'sleep', m['tasks'][task]['image_id'], 'infinity']).stdout.strip()
    try:
        docker(['start', cid])
        source = Path(__file__).parent / 'offline_fixtures'
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name, relative in [('out.html', 'out.html' if valid else 'invalid.html'), ('payload.html', 'payload.html')]:
                info = tar.gettarinfo(str(source / relative), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                with (source / relative).open('rb') as stream:
                    tar.addfile(info, stream)
        subprocess.run(['docker', 'exec', '-i', cid, 'tar', '--no-same-owner', '-xf', '-', '-C', '/app'],
                       input=archive.getvalue(), check=True, capture_output=True)
        output = tmp_path / 'grading'
        record = grade_container(cid, task, m, output)
        assert record['reward'] == int(valid)
        # Both negative cases have an output: the browser/HTTP path must execute,
        # unlike an unsolved-image smoke that fails early on a missing file.
        text = (output / 'test-stdout.txt').read_text()
        if not valid:
            assert 'No alert detected after filtering' in text
        network = json.loads((output / 'network.json').read_text())
        assert network['interfaces'] == [[1, 'lo']]
        assert network['loopback'] == 'passed'
    finally:
        docker(['rm', '-f', cid])
