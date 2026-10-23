"""Fetch pinned benchmark metadata only, without images or model calls."""
import json
import os
from _bootstrap import REPO
from rsi_portable.paths import WORKSPACE

if __name__ == '__main__':
    from huggingface_hub import snapshot_download
    cfg = json.loads((REPO / 'upstream/manifest.json').read_text())['dataset']
    destination = WORKSPACE / 'data/SWE-Chain-Evo'
    snapshot_download(cfg['repository'], repo_type='dataset', revision=cfg['revision'],
        local_dir=destination, endpoint=os.environ.get('HF_ENDPOINT', 'https://huggingface.co'),
        allow_patterns=['README.md', 'chains/**', 'chains50.txt'])
    import hashlib
    entries = json.loads((REPO / 'configs/benchmark/seven-chains.json').read_text())['entries']
    for item in entries:
        path = destination / item['instance_path']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['instance_sha256']:
            raise RuntimeError('Pinned instance mismatch: ' + item['instance_id'])
    print(json.dumps({'metadata_ready': len(entries), 'revision': cfg['revision'], 'image_downloads': 0, 'paid_model_calls': 0}))
