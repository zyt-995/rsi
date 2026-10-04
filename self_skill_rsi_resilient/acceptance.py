"""Offline infrastructure-only controls. Never consumed by the model/skill bank."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import tarfile
import uuid

from offline_verifier.runner import docker, validate_result
from offline_verifier.profiles import sha
from rsi_harness.terminal_infra import atomic_json
from . import grading


def check(task, manifest, output, control=None):
    output = Path(output)
    if output.exists():
        raise RuntimeError('acceptance_output_already_exists')
    cid = docker(['create', '--network', 'none', '--name', 'rsi-resilience-test-' + uuid.uuid4().hex[:12],
                  '--entrypoint', 'sleep', manifest['tasks'][task]['image_id'], 'infinity']).stdout.strip()
    try:
        docker(['start', cid])
        if control and control.startswith('chess'):
            code = "from pathlib import Path; Path('/app/inspect.py').write_text('raise RuntimeError(\"POISON_INSPECT\")'); Path('/app/json.py').write_text('raise RuntimeError(\"POISON_JSON\")')"
            if control == 'chess-positive':
                code += "; Path('/app/move.txt').write_text('e2e4')"
            docker(['exec', cid, 'python3', '-I', '-c', code])
        if control and control.startswith('html'):
            source = Path(__file__).resolve().parents[1] / 'tests/offline_fixtures'
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w') as tar:
                for name, fixture in [('out.html', 'out.html' if control == 'html-positive' else 'invalid.html'),
                                      ('payload.html', 'payload.html')]:
                    info = tar.gettarinfo(str(source / fixture), arcname=name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    with (source / fixture).open('rb') as stream:
                        tar.addfile(info, stream)
            subprocess.run(['docker', 'exec', '-i', cid, 'tar', '--no-same-owner', '-xf', '-', '-C', '/app'],
                           input=data.getvalue(), check=True, capture_output=True)
        result = grading.grade_container(cid, task, manifest, output)
        if control:
            assert result['reward'] == int(control.endswith('positive'))
        record = {'task': task, 'control': control, 'status': 'executed',
                  'network_mode': 'none', 'reward': result['reward'], 'is_formal_score': False}
    except Exception as exc:
        record = {'task': task, 'control': control, 'status': 'infrastructure_failed', 'reason': str(exc)}
    finally:
        docker(['rm', '-f', cid])
    atomic_json(output / 'acceptance.json', record)
    print(json.dumps(record), flush=True)
    return record


def run(manifest_path, output):
    m = grading.load_manifest(manifest_path, sha(manifest_path))
    jobs = [(task, output / 'tasks' / task, None) for task in m['tasks']]
    jobs += [('chess-best-move-EVO-3', output / 'controls' / name, name)
             for name in ('chess-positive', 'chess-negative')]
    jobs += [('break-filter-js-from-html-EVO-2', output / 'controls' / name, name)
             for name in ('html-positive', 'html-negative')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda job: check(job[0], m, job[1], job[2]), jobs))
    passed = all(r['status'] == 'executed' for r in rows)
    hashes = {str(p): sha(p) for p in output.rglob('*') if p.is_file()}
    atomic_json(output / 'summary.json', {'passed': passed, 'grader_sha256': sha(manifest_path),
        'rows': rows, 'evidence_hashes': hashes, 'formal_scores': 0, 'model_calls': 0})
    if not passed:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    run(a.manifest, a.output)
