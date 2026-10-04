"""Online preparation ONCE; produces a new immutable grading bundle, never an image."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import zipfile

from .profiles import PREFIX, REQUIREMENTS, SCHEMA, GIT_URL, ZIP_URL, profile, sha, test_hashes

PROJECT = Path(__file__).resolve().parents[1]


def run(args, **kw):
    return subprocess.run(args, check=True, text=True, **kw)


def build(source, output, reuse=None):
    if (output / 'manifest.json').exists():
        raise RuntimeError('sealed_bundle_already_exists; use_a_new_version_directory')
    output.mkdir(parents=True, exist_ok=True)
    stage = output / 'staging'
    if reuse is not None and not stage.exists():
        shutil.copytree(reuse / 'staging', stage, symlinks=True)
    stage.mkdir(exist_ok=True)
    prefix = stage / PREFIX.lstrip('/')
    prefix.mkdir(parents=True, exist_ok=True)
    wheelhouse = prefix / 'wheels'
    wheelhouse.mkdir(exist_ok=True)
    # Bootstrap interpreter and uv already exist locally. No interpreter downloads.
    interpreter = PROJECT / '.cache/verifier/python/cpython-3.13.9-linux-x86_64-gnu'
    if not (prefix / 'python').exists():
        shutil.copytree(interpreter, prefix / 'python', symlinks=True)
    shutil.copy2(PROJECT / '.cache/verifier/uv-0.9.5/bin/uv', prefix / 'uv')
    for name in ('isolated.py', 'grade.py'):
        shutil.copy2(Path(__file__).parent / name, prefix / name)
    pins = sorted(set(p for rows in REQUIREMENTS.values() for p in rows))
    if reuse is None:
        run([str(interpreter / 'bin/python3'), '-m', 'pip', 'download', '--disable-pip-version-check',
             '--only-binary=:all:', '--dest', str(wheelhouse), *pins], timeout=600)
    wheel_versions = {}
    for p in wheelhouse.glob('*.whl'):
        with zipfile.ZipFile(p) as z:
            metadata = z.read(next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))).decode()
        fields = dict(line.split(': ', 1) for line in metadata.splitlines() if ': ' in line)
        wheel_versions[fields['Name']] = fields['Version']
    (prefix / 'constraints.txt').write_text(''.join(f'{k}=={v}\n' for k, v in sorted(wheel_versions.items())))
    resources = prefix / 'resources'
    resources.mkdir(exist_ok=True)
    repo = resources / 'pyknotid.git'
    if not repo.exists():
        run(['git', 'clone', '--bare', '--single-branch', '--branch', '0.5.3', GIT_URL, str(repo)], timeout=300)
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '0.5.3^{commit}'], text=True).strip()
    archive = resources / 'pyknotid-0.5.3.zip'
    if not archive.exists():
        run(['curl', '--fail', '--location', '--connect-timeout', '20', '--max-time', '180',
             '--output', str(archive), ZIP_URL], timeout=200)
    # The zip and git references must contain the SAME upstream test files.
    with zipfile.ZipFile(archive) as z:
        names = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-r', '--name-only', commit, 'tests'], text=True).splitlines()
        if not names:
            raise RuntimeError('upstream_test_fixture_empty')
        for name in names:
            raw = subprocess.check_output(['git', '-C', str(repo), 'show', f'{commit}:{name}'])
            if z.read('pyknotid-0.5.3/' + name) != raw:
                raise RuntimeError('zip_and_git_test_fixture_mismatch:' + name)
    manifest_source = json.loads(source.read_text())
    tasks = {}
    for e in manifest_source['stream']:
        path = Path(e['path'])
        tasks[e['task']] = {'profile': profile(path), 'path': str(path), 'image_id': e['image_id'],
                            'task_checksum': e['task_checksum'], 'tests': test_hashes(path)}
    # Only relative symlinks are portable. The standalone Python distribution uses these.
    for p in prefix.rglob('*'):
        if p.is_symlink():
            target = os.readlink(p)
            if target.startswith('/') or not p.resolve().is_relative_to(prefix.resolve()):
                raise RuntimeError('nonportable_runtime_symlink:' + str(p))
    payload = {'schema': SCHEMA, 'profiles': REQUIREMENTS, 'versions': wheel_versions,
               'tasks': tasks, 'fixture': {'git_url': GIT_URL, 'tag': '0.5.3', 'commit': commit,
                                          'zip_url': ZIP_URL, 'zip_sha256': sha(archive)},
               'files': {str(p.relative_to(prefix)): sha(p) for p in sorted(prefix.rglob('*'))
                         if p.is_file() and not p.is_symlink() and p != prefix / 'payload.json'}}
    (prefix / 'payload.json').write_text(json.dumps(payload, indent=2))
    bundle = output / 'runtime.tar.gz'
    with tarfile.open(bundle, 'w:gz') as tar:
        def owner(info):
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            return info
        tar.add(stage / 'opt', arcname='opt', filter=owner)
    result = {'schema': SCHEMA, 'created': time.time(), 'bundle': str(bundle.resolve()),
              'bundle_sha256': sha(bundle), 'payload_sha256': sha(prefix / 'payload.json'),
              'source_manifest': str(source.resolve()), 'source_manifest_sha256': sha(source),
              'tasks': tasks, 'fixture': payload['fixture'], 'versions': wheel_versions,
              'code': {str(p.resolve()): sha(p) for p in sorted(Path(__file__).parent.glob('*.py'))}}
    (output / 'manifest.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({'manifest': str(output / 'manifest.json'), 'tasks': len(tasks),
                      'bundle_bytes': bundle.stat().st_size, 'fixture_commit': commit}))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source-manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--reuse', type=Path, help='Copy previously prepared local resources; no package resolution/download.')
    a = p.parse_args()
    build(a.source_manifest, a.output, a.reuse)
