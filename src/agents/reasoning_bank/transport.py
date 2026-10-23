"""Accounted OpenRouter SDK seams sharing the common process-safe $50 ledger."""
import copy
from decimal import Decimal
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid

from swe_chain_evo.common import atomic, read
from methods.evo_fifty.gateway import Broker, BoundedHTTPSHandler, TRANSIENT_HTTP, transport_failure_record, TransientProviderError
from methods.evo_fifty.spend import SpendLedger


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class EmbeddingBroker:
    def __init__(self, root, key, config, sender=None):
        self.root, self.key, self.config = Path(root), key, config
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = SpendLedger(Path(config['spend']['ledger_path']), config['spend']['global_limit'], allow_uncertain=True)
        self.scopes = read(self.root / 'gateway-scopes.json') if (self.root / 'gateway-scopes.json').exists() else {}
        self.sender = sender or self._send
        self.observation = {}
        proxy=config['runtime'].get('embedding_http_proxy','')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy} if proxy else {}), BoundedHTTPSHandler())

    def persist(self):
        atomic(self.root / 'gateway-scopes.json', self.scopes)

    def scope(self, arm, phase, name):
        if any(s['name'] == name for s in self.scopes.values()):
            raise ValueError('Embedding scope already exists')
        token = secrets.token_urlsafe(32)
        self.scopes[token] = dict(arm=arm, phase=phase, name=name, calls=[], pending_tokens=0, blocked=False)
        self.persist()
        return token

    def _send(self, payload):
        request = urllib.request.Request(self.config['model']['base_url'] + '/embeddings',
             data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        self.observation['request_bytes_may_have_been_sent'] = False
        request._transport_observation = self.observation
        with self.opener.open(request, timeout=90) as response:
            return json.load(response)

    def execute(self, token, payload):
        scope = self.scopes[token]
        if scope['blocked']:
            raise RuntimeError('Embedding accounting requires review')
        bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 4096
        price = Decimal('0.00000015')
        call_id = str(uuid.uuid4())
        upper = bound * price
        self.ledger.reserve(call_id, scope['arm'], scope['phase'], upper)
        scope['calls'].append(call_id); scope['pending_tokens'] = bound; self.persist()
        folder = self.root / 'provider-calls' / call_id; folder.mkdir(parents=True)
        record = dict(id=call_id, arm=scope['arm'], phase=scope['phase'], scope=scope['name'],
             state='started', payload=payload, started=time.time(), reserved_usd=str(upper), token_reservation=bound,
             transport_route=self.config['runtime'].get('embedding_http_proxy') or 'direct')
        atomic(folder / 'request.json', record)
        self.observation = {}
        try:
            raw = self.sender(payload)
            atomic(folder / 'response.json', raw)
            if raw.get('error'):
                raise ValueError('Embedding provider returned error')
            usage = raw.get('usage') or {}
            count = usage.get('prompt_tokens', usage.get('input_tokens'))
            cost = usage.get('cost')
            if type(count) is not int or count < 0 or cost is None:
                raise ValueError('Authoritative embedding usage/cost missing')
            self.ledger.settle(call_id, cost, count, 0)
            scope['pending_tokens'] = 0
            if count > bound:
                raise ValueError('Embedding usage exceeded admission bound')
            record.update(state='settled', generation_id=raw.get('id'),
                          usage={'prompt_tokens': count, 'completion_tokens': 0, 'cost': cost})
            return raw
        except BaseException as exc:
            transient = isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError,
                ssl.SSLError, http.client.RemoteDisconnected, http.client.IncompleteRead, json.JSONDecodeError))
            if isinstance(exc, urllib.error.HTTPError):
                transient = exc.code in TRANSIENT_HTTP
                record['http_status'] = exc.code
                (folder / 'rejection.txt').write_bytes(exc.read())
            no_dispatch = self.observation.get('request_bytes_may_have_been_sent') is False
            if (isinstance(exc, urllib.error.HTTPError) and exc.code in (400,401,402,403,404,413,422,429)) or (transient and no_dispatch):
                self.ledger.settle(call_id, '0', 0, 0)
                scope['pending_tokens'] = 0
                record.update(state='rejected', usage={'prompt_tokens':0,'completion_tokens':0,'cost':0}, not_dispatched=no_dispatch)
            elif record['state'] == 'started':
                self.ledger.uncertain(call_id)
                record.update(state='requires_review')
            record.update(exception=type(exc).__name__, retryable_transport=transient)
            scope['blocked'] = True
            if transient:
                failure = TransientProviderError(str(exc), getattr(exc, 'code', None))
                failure.call_id = call_id
                raise failure from exc
            raise
        finally:
            record['finished'] = time.time()
            record['transport_timing'] = self.observation
            self.persist(); atomic(folder / 'request.json', record)


def checkpoint_call(broker, output, arm, phase, payload):
    """Reuse a saved paid response exactly; never replay a successful generation."""
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    signature = fingerprint(payload)
    path = output / 'call.json'
    name = str(output.relative_to(broker.root.parents[1])) if broker.root.parent.name == 'channels' else str(output.relative_to(broker.root))
    if path.exists():
        checkpoint = read(path)
        if checkpoint['payload_sha256'] != signature:
            raise RuntimeError('Saved model call inputs changed')
        token = checkpoint['token']
    else:
        token = broker.scope(arm, phase, name)
        checkpoint = {'token': token, 'payload_sha256': signature}
        atomic(path, checkpoint)
    scope = broker.scopes[token]
    calls = [read(broker.root / 'provider-calls' / cid / 'request.json') for cid in scope['calls']]
    success = [r for r in calls if r['state'] == 'settled' and
               (broker.root / 'provider-calls' / r['id'] / 'response.json').exists() and
               not read(broker.root / 'provider-calls' / r['id'] / 'response.json').get('error')]
    if success:
        raw = read(broker.root / 'provider-calls' / success[-1]['id'] / 'response.json')
        return raw
    if scope.get('blocked') or scope.get('pending_tokens'):
        if calls and all(transport_failure_record(r) for r in calls):
            # All old charged/uncertain attempts remain reserved in the shared ledger.
            if any(r['state'] == 'started' for r in calls):
                raise RuntimeError('Unfinished dispatched call needs review')
            name += '-resume-' + str(len(scope['calls'])) + '-' + str(len(broker.scopes))
            token = broker.scope(arm, phase, name)
            atomic(output / ('previous-call-' + str(len(broker.scopes)) + '.json'), checkpoint)
            atomic(path, {'token': token, 'payload_sha256': signature})
        else:
            raise RuntimeError('Partial paid call retained; inspect accounting before resume')
    return broker.execute(token, payload)


class Clients:
    def __init__(self, root, key, config, sender=None, embedding_sender=None):
        self.root, self.config = Path(root), config
        self.chat = {}
        for role, temperature in [('judge', 0.0), ('extraction', 1.0)]:
            cfg = copy.deepcopy(config)
            cfg['model']['temperature'] = temperature
            channel = self.root / 'channels' / role
            channel.mkdir(parents=True, exist_ok=True)
            # All roles must use the same calibrated context tokenizer. Falling
            # back to byte counts here would reject otherwise valid long traces.
            tokenizer = self.root / 'tokenizer.json'
            if tokenizer.exists() and not (channel / 'tokenizer.json').exists():
                (channel / 'tokenizer.json').symlink_to(tokenizer.resolve())
            self.chat[role] = Broker(channel, key, cfg, sender=sender)
        self.embedding = EmbeddingBroker(self.root / 'channels/embedding', key, config, sender=embedding_sender)
        self.embed_sequence = 0

    def embed(self, index, output, text, task_type, dimensions):
        if task_type != 'RETRIEVAL_DOCUMENT' or dimensions != 3072:
            raise ValueError('Official embedding parameters changed')
        self.embed_sequence += 1
        payload = dict(model='google/gemini-embedding-001', input=text, encoding_format='float',
            dimensions=dimensions, input_type=task_type,
            provider={'only':['google-vertex/us-central1'], 'allow_fallbacks':False,
                      'max_price':{'prompt':0.15,'completion':0}})
        result = checkpoint_call(self.embedding, Path(output) / f'embed-{self.embed_sequence:02d}',
                                 'reasoning-bank', 'retrieval', payload)
        vector = result['data'][0]['embedding']
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise ValueError('Unexpected embedding dimensionality')
        return vector

    def generate(self, output, role, model, contents, options):
        if model != self.config['model']['id']:
            raise ValueError('Official function requested a different backbone')
        payload = {'model': model, 'max_tokens': options.max_output_tokens,
                   'messages': [{'role':'system','content':options.system_instruction},
                                {'role':'user','content':contents}]}
        raw = checkpoint_call(self.chat[role], Path(output) / role, 'reasoning-bank', role, payload)
        message = raw['choices'][0]['message']
        text = message.get('content')
        if not isinstance(text, str) or not text.strip() or message.get('tool_calls'):
            raise RuntimeError('Official memory phase produced no usable text; paid response retained')
        return text
