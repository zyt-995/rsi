"""Real no-network acceptance. Separate infrastructure artifacts, never scores."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import uuid

from .profiles import sha
from .runner import docker, grade_container, load_manifest


def check(task, manifest, root):
    output = root / task
    if output.exists():
        raise RuntimeError('acceptance_attempt_exists:' + str(output))
    image = manifest['tasks'][task]['image_id']
    name = 'rsi-offline-check-' + uuid.uuid4().hex[:12]
    container = docker(['create', '--network', 'none', '--name', name,
                        '--entrypoint', 'sleep', image, 'infinity']).stdout.strip()
    start = time.time()
    try:
        docker(['start', container])
        record = grade_container(container, task, manifest, output)
        result = {'task': task, 'status': 'executed', 'seconds': time.time() - start,
                  'summary': record['summary'], 'is_formal_score': False,
                  'unsolved_image': True, 'network_mode': 'none',
                  'note': 'Expected answer failures are allowed; this alone does not prove grading equivalence.'}
    except Exception as exc:
        result = {'task': task, 'status': 'infrastructure_failed', 'reason': str(exc), 'is_formal_score': False}
    finally:
        # Only this function's disposable container is removed; no images/results.
        docker(['rm', '-f', container])
    output.mkdir(parents=True, exist_ok=True)
    (output / 'acceptance.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--task', action='append')
    p.add_argument('--workers', type=int, default=2)
    a = p.parse_args()
    m = load_manifest(a.manifest, sha(a.manifest))
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(lambda task: check(task, m, a.output), a.task or list(m['tasks'])))
    (a.output / 'summary.json').write_text(json.dumps(results, indent=2))
    if any(r['status'] != 'executed' for r in results):
        raise SystemExit(1)
