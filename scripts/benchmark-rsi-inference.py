#!/usr/bin/env python3
"""Synthetic, fixed-token latency probe. Never creates benchmark task scores."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time
import urllib.request


def request(port, messages, tokens, seed):
    body = {'model': 'Qwen/Qwen3.8-27B', 'messages': messages, 'temperature': 0,
            'seed': seed, 'max_tokens': tokens, 'ignore_eos': True,
            'stream': True, 'stream_options': {'include_usage': True}}
    req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer EMPTY'})
    start = time.monotonic()
    first = None
    text = []
    usage = {}
    chunks = 0
    with urllib.request.urlopen(req, timeout=600) as response:
        for line in response:
            if not line.startswith(b'data: '):
                continue
            raw = line[6:].strip()
            if raw == b'[DONE]':
                break
            data = json.loads(raw)
            if data.get('usage'):
                usage = data['usage']
            for choice in data.get('choices', []):
                delta = choice.get('delta', {})
                piece = delta.get('content') or delta.get('reasoning_content') or delta.get('reasoning') or ''
                if piece:
                    first = first or time.monotonic()
                    chunks += 1
                    text.append(piece)
    end = time.monotonic()
    output = usage.get('completion_tokens', 0)
    if output != tokens or first is None:
        raise RuntimeError(f'not_a_fixed_token_generation:{usage}')
    return {'elapsed_seconds': end-start, 'ttft_seconds': first-start,
            'decode_tokens_per_second': (output-1)/max(end-first, 1e-6),
            'usage': usage, 'content_sha256': hashlib.sha256(''.join(text).encode()).hexdigest(),
            'stream_chunks': chunks}


def messages(size):
    code = ''.join(f'def guard_{i}(value):\n    return value if value > {i % 7} else {i % 7}\n' for i in range(size))
    return [{'role': 'system', 'content': 'You are a careful code reviewer. This is a synthetic inference benchmark.'},
            {'role': 'user', 'content': 'Review this generated Python module for edge cases and propose tests. Explain step by step.\n' + code}]


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--label', required=True)
    p.add_argument('--repeats', type=int, default=2)
    p.add_argument('--tokens', type=int, default=512)
    a = p.parse_args()
    if a.output.exists():
        raise RuntimeError('probe_output_already_exists')
    a.output.parent.mkdir(parents=True, exist_ok=True)
    warmup = request(a.port, messages(8), 32, 0)
    rows = []
    for size in (16, 200, 800):
        for repeat in range(a.repeats):
            row = {'size': size, 'repeat': repeat, **request(a.port, messages(size), a.tokens, 0)}
            rows.append(row)
            print(json.dumps({'label': a.label, **row}), flush=True)
    report = {'label': a.label, 'port': a.port, 'time': time.time(), 'warmup': warmup, 'rows': rows,
              'median_decode_tokens_per_second': statistics.median(r['decode_tokens_per_second'] for r in rows),
              'formal_scores': False, 'fixed_model_and_token_budget': True}
    a.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('rows','warmup')}), flush=True)
