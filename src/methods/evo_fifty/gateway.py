"""One paid OpenRouter transport for all methods and all phases."""
from __future__ import annotations

import argparse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
import copy
import random
import ssl

from .spend import SpendBlocked, SpendLedger, SpendPending


TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}


class TransientProviderError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ContextAdmissionExceeded(ValueError):
    """Signal context pressure to the native summarizer before dispatch."""


def transport_failure_record(row, response=None):
    """Recognize infrastructure failures without treating task errors as retries."""
    if row.get('retryable_transport'):
        return True
    kind = row.get('exception')
    if kind in ('URLError', 'OSError', 'TimeoutError', 'RemoteDisconnected',
                'ConnectionResetError', 'IncompleteRead', 'JSONDecodeError',
                'TransientProviderError'):
        return True
    if kind == 'HTTPError':
        return any(f'HTTP Error {code}:' in row.get('error_message', '') for code in TRANSIENT_HTTP)
    error = (response or {}).get('error') or {}
    return isinstance(error, dict) and error.get('code') in TRANSIENT_HTTP


def receipt(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    os.replace(temp, path)


class BoundedHTTPSConnection(http.client.HTTPSConnection):
    """Bound connection setup while retaining the full model read deadline."""
    connect_timeout = 5.0
    observation = None

    def send(self, data):
        # A failure in connect() occurs before any model HTTP bytes are sent.
        if self.sock is None:
            self.connect()
        if self.observation is not None:
            self.observation['request_bytes_may_have_been_sent'] = True
        return super().send(data)

    def connect(self):
        read_timeout = self.timeout
        self.timeout = self.connect_timeout
        started = time.monotonic()
        try:
            # socket.create_connection tries the resolved addresses in order.
            # A dead address now gets five seconds, instead of the OS SYN limit.
            super().connect()
        finally:
            self.timeout = read_timeout
            if self.observation is not None:
                self.observation['connect_seconds'] = time.monotonic() - started
                self.observation['connect_timeout_per_address_seconds'] = self.connect_timeout
        self.sock.settimeout(read_timeout)
        if self.observation is not None:
            self.observation['connected_at'] = time.time()
            self.observation['peer_ip'] = self.sock.getpeername()[0]


class BoundedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        def connection(host, **kwargs):
            conn = BoundedHTTPSConnection(host, **kwargs)
            conn.observation = getattr(request, '_transport_observation', None)
            return conn
        return self.do_open(connection, request, context=self._context)


class Broker:
    def __init__(self, root: Path, key: str, config: dict, sender=None):
        self.root, self.key, self.config = Path(root), key, config
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = SpendLedger(Path(config['spend'].get('ledger_path', self.root / 'spend.sqlite')), config['spend']['global_limit'],
            allow_uncertain=config['accounting'].get('uncertain_request_policy') in (
                'retain_full_reservation_and_quarantine_scope',
                'retain_full_reservation_and_retry_transient_errors'))
        self.lock = threading.RLock()
        self.scopes = {}
        self.scope_path = self.root / 'gateway-scopes.json'
        if self.scope_path.exists():
            self.scopes = json.loads(self.scope_path.read_text())
            for scope in self.scopes.values():
                if scope.get('pending_tokens'):
                    scope['blocked'] = True
        self.sender = sender or self._send
        self.transport_state = threading.local()
        proxy = config['runtime'].get('model_http_proxy', config['runtime']['host_outbound_proxy'])
        self.transport_route = proxy or 'direct'
        # An explicit empty proxy map disables ambient proxy environment vars.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {'http':proxy,'https':proxy} if proxy else {}), BoundedHTTPSHandler())
        bounds = config['model'].get('reservation_bounds', {})
        self.input_price = Decimal(str(bounds.get('input_usd_per_token', '0.0000003')))
        self.output_price = Decimal(str(bounds.get('output_usd_per_token', '0.0000012')))
        self.native_context = int(bounds.get('native_context_tokens', 1048576))
        self.tokenizer = None
        self.reasoning_history = {}
        tokenizer_path = self.root / 'tokenizer.json'
        if tokenizer_path.exists():
            from .model_codec import load_codec
            self.tokenizer = load_codec(tokenizer_path)

    def _persist(self):
        temp = self.scope_path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.scopes, stream, ensure_ascii=False)
        os.replace(temp, self.scope_path)

    def scope(self, arm: str, phase: str, name: str, limit: int | None = None):
        if arm not in self.config['arms'] and arm != 'smoke':
            raise ValueError('unknown experiment arm')
        with self.lock:
            if any(s['name'] == name for s in self.scopes.values()):
                raise ValueError('scope already exists; do not replay')
            token = secrets.token_urlsafe(32)
            self.scopes[token] = {'arm': arm, 'phase': phase, 'name': name,
                                  'limit': limit if limit is not None else self.config['solve']['max_input_plus_output_tokens'],
                                  'used_tokens': 0, 'pending_tokens': 0, 'calls': [], 'blocked': False}
            self._persist()
            return token

    def _send(self, payload):
        request = urllib.request.Request(self.config['model']['base_url'] + '/chat/completions',
            data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + self.key,
            'Content-Type': 'application/json'})
        self.transport_state.observation['request_bytes_may_have_been_sent'] = False
        request._transport_observation = self.transport_state.observation
        with self.opener.open(request, timeout=2700) as response:
            return json.load(response)

    def execute(self, token: str, body: dict):
        retries = self.config['runtime'].get('model_transport_retries', 0)
        logical_id = str(uuid.uuid4())
        previous_calls = []
        for attempt in range(retries + 1):
            try:
                raw = self._execute_once(token, body, logical_id, attempt)
                if previous_calls:
                    receipt(self.root / 'network-recovery' / (logical_id + '.json'), {
                        'state': 'recovered', 'time': time.time(),
                        'failed_call_ids': previous_calls, 'retry_count': attempt,
                        'generation_id': raw.get('id')})
                return raw
            except TransientProviderError as exc:
                previous_calls.append(exc.call_id)
                directory = self.root / 'network-recovery'
                directory.mkdir(exist_ok=True)
                exhausted = attempt == retries
                delay = min(5 * 2 ** attempt, 60) * random.uniform(0.9, 1.1)
                receipt(directory / (logical_id + '.json'), {
                    'state': 'retry_exhausted' if exhausted else 'waiting_to_retry',
                    'time': time.time(), 'failed_call_ids': previous_calls,
                    'next_retry_at': None if exhausted else time.time() + delay,
                    'attempt': attempt + 1, 'error': str(exc)})
                if exhausted:
                    with self.lock:
                        self.scopes[token]['blocked'] = True
                        self._persist()
                    raise
                time.sleep(delay)

    def _execute_once(self, token: str, body: dict, logical_id=None, attempt=0):
        cfg = self.config['model']
        if body.get('stream'):
            raise ValueError('streaming disabled to retain complete usage')
        while True:
            with self.lock:
                scope = self.scopes.get(token)
                if scope is None:
                    raise PermissionError('unknown local scope')
                if scope['blocked']:
                    raise SpendBlocked('scope requires reconciliation')
                payload = copy.deepcopy(body)
                restored = self._restore_reasoning(token, scope, payload)
                for name in ('chat_template_kwargs', 'session_id', 'reasoning_effort', 'api_base', 'api_key'):
                    payload.pop(name, None)
                payload.update(model=cfg['id'], reasoning=cfg['reasoning'],
                               provider={**cfg['provider'], 'max_price': cfg.get('provider_max_price', {'prompt': 0.3, 'completion': 1.2})})
                for name in ('temperature', 'top_p'):
                    if cfg.get(name) is None:
                        payload.pop(name, None)
                    else:
                        payload[name] = cfg[name]
                out = min(int(payload.get('max_tokens') or cfg['max_output_tokens']), cfg['max_output_tokens'])
                # UTF-8 byte length is an intentionally conservative text-token
                # admission bound. Images reserve the whole configured input window.
                serialized = json.dumps({'messages': payload.get('messages', []),
                                         'tools': payload.get('tools', [])}, ensure_ascii=False)
                text_bound = len(self.tokenizer.encode(serialized).ids) if self.tokenizer else len(serialized.encode())
                text_bound = int(text_bound * cfg.get('admission_token_margin', 1.0) + .999)
                text_bound += 1024 + 64 * len(payload.get('messages', [])) + 512 * len(payload.get('tools', []))
                has_image = 'image_url' in json.dumps(payload.get('messages', []))
                input_bound = cfg['context_tokens'] - out if has_image else text_bound
                if input_bound + out > cfg['context_tokens']:
                    raise ContextAdmissionExceeded(
                        "This model's maximum context length is " + str(cfg['context_tokens'])
                        + " tokens; admission requires " + str(input_bound + out) + ". Summarize history.")
                if scope['limit'] is not None:
                    remaining = scope['limit'] - scope['used_tokens'] - scope['pending_tokens']
                    out = min(out, remaining - input_bound)
                if out < 1:
                    raise SpendBlocked('scope token budget reached')
                payload['max_tokens'] = out
                call_id = str(uuid.uuid4())
                # Dollar admission is separate from the approximate context
                # tokenizer. For text, UTF-8 bytes bound byte-token counts;
                # generously reserve framing/tool-schema overhead as well.
                # Nontext inputs retain the full provider context bound.
                monetary_input_bound = self.native_context
                reservation_policy = self.config.get('accounting', {}).get('dollar_reservation_policy')
                if reservation_policy == 'text_utf8_upper_bound_v1':
                    nontext = any(marker in serialized for marker in
                                  ('"image_url"', '"input_audio"', '"video_url"', '"file"'))
                    if not nontext:
                        byte_bound = len(serialized.encode('utf-8')) + 16384
                        byte_bound += 256 * len(payload.get('messages', []))
                        byte_bound += 2048 * len(payload.get('tools', []))
                        monetary_input_bound = min(self.native_context, max(input_bound, byte_bound))
                upper = monetary_input_bound * self.input_price + out * self.output_price
                try:
                    self.ledger.reserve(call_id, scope['arm'], scope['phase'], upper)
                except SpendPending:
                    wait_for_budget = True
                else:
                    wait_for_budget = False
                if not wait_for_budget:
                    bound = input_bound + out
                    scope['pending_tokens'] += bound
                    scope['calls'].append(call_id)
                    self._persist()
            if not wait_for_budget:
                break
            # Sleep outside the scope lock: other calls must be able to settle.
            time.sleep(1)
        folder = self.root / 'provider-calls' / call_id
        folder.mkdir(parents=True)
        request = {'id': call_id, 'arm': scope['arm'], 'phase': scope['phase'], 'scope': scope['name'],
                   'started': time.time(), 'state': 'started', 'payload': payload,
                   'logical_request_id': logical_id, 'transport_attempt': attempt,
                   'reserved_usd': str(upper), 'input_token_admission_bound': input_bound,
                   'monetary_input_token_bound': monetary_input_bound,
                   'dollar_reservation_policy': reservation_policy or 'full_native_context',
                   'token_reservation': bound, 'transport_route': self.transport_route}
        receipt(folder / 'request.json', request)
        self.transport_state.observation = {}
        error_usage = None
        try:
            raw = self.sender(payload)
            receipt(folder / 'response.json', raw)
            error = raw.get('error') or {}
            if isinstance(error, dict) and error.get('code') in TRANSIENT_HTTP:
                candidate_usage = raw.get('usage') or {}
                if (type(candidate_usage.get('prompt_tokens')) is int and
                    type(candidate_usage.get('completion_tokens')) is int and
                    min(candidate_usage['prompt_tokens'], candidate_usage['completion_tokens']) >= 0 and
                    candidate_usage.get('cost') is not None):
                    error_usage = candidate_usage
                raise TransientProviderError(error.get('message', 'upstream unavailable'), error['code'])
            usage = raw.get('usage') or {}
            tin, tout, cost = usage.get('prompt_tokens'), usage.get('completion_tokens'), usage.get('cost')
            if type(tin) is not int or type(tout) is not int or tin < 0 or tout < 0 or cost is None:
                raise ValueError('authoritative provider usage or cost missing')
            self.ledger.settle(call_id, cost, tin, tout)
            with self.lock:
                scope['pending_tokens'] -= bound
                scope['used_tokens'] += tin + tout
                if tin > input_bound or tin + tout > bound or (scope['limit'] is not None and scope['used_tokens'] > scope['limit']):
                    scope['blocked'] = True
                    raise ValueError('provider usage exceeded token admission bound')
                self._persist()
                self._remember_reasoning(token, raw)
            request.update(state='settled', usage=usage, generation_id=raw.get('id'),
                           provider=raw.get('provider'), actual_model=raw.get('model'),
                           reasoning_history_messages_restored=restored)
            return raw
        except BaseException as exc:
            transient = isinstance(exc, (TransientProviderError, urllib.error.URLError,
                TimeoutError, ConnectionError, ssl.SSLError, http.client.RemoteDisconnected,
                http.client.IncompleteRead, json.JSONDecodeError))
            if isinstance(exc, urllib.error.HTTPError):
                transient = exc.code in TRANSIENT_HTTP
                error_body = exc.read()
                (folder / 'rejection.txt').write_bytes(error_body)
                request['http_status'] = exc.code
            enabled = bool(self.config['runtime'].get('model_transport_retries', 0))
            no_dispatch = self.transport_state.observation.get('request_bytes_may_have_been_sent') is False
            if error_usage is not None:
                tin, tout = error_usage['prompt_tokens'], error_usage['completion_tokens']
                self.ledger.settle(call_id, error_usage['cost'], tin, tout)
                with self.lock:
                    scope['pending_tokens'] -= bound
                    scope['used_tokens'] += tin + tout
                    if tin > input_bound or tin + tout > bound or (scope['limit'] is not None and scope['used_tokens'] > scope['limit']):
                        scope['blocked'] = True
                        self._persist()
                        raise ValueError('provider error usage exceeded admission bound')
                    scope['blocked'] = scope['blocked'] or not (transient and enabled)
                    self._persist()
                request.update(state='settled', usage=error_usage, generation_id=raw.get('id'))
            elif (isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 402, 403, 404, 413, 422, 429)) or (transient and no_dispatch):
                # Only explicit pre-generation rejection or a locally proven
                # unsent request releases its reservation without usage data.
                self.ledger.settle(call_id, '0', 0, 0)
                with self.lock:
                    scope['pending_tokens'] -= bound
                    scope['blocked'] = scope['blocked'] or not (transient and enabled)
                    self._persist()
                request.update(state='rejected', not_dispatched=no_dispatch,
                    usage={'prompt_tokens': 0, 'completion_tokens': 0, 'cost': 0})
            else:
                self.ledger.uncertain(call_id)
                with self.lock:
                    scope['blocked'] = scope['blocked'] or not (transient and enabled)
                    self._persist()
                request.update(state='requires_review', exception=type(exc).__name__, error_message=str(exc))
            if transient:
                request.update(retryable_transport=True, exception=type(exc).__name__, error_message=str(exc))
                if enabled:
                    failure = TransientProviderError(str(exc), getattr(exc, 'code', None))
                    failure.call_id = call_id
                    raise failure from exc
            raise
        finally:
            request['finished'] = time.time()
            if self.transport_state.observation:
                request['transport_timing'] = self.transport_state.observation
            receipt(folder / 'request.json', request)


    @staticmethod
    def _assistant_identity(message):
        calls = []
        for call in message.get('tool_calls') or []:
            fn = call.get('function') or {}
            args = fn.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            calls.append({'id': call.get('id'), 'name': fn.get('name'), 'arguments': args})
        return json.dumps({'content': message.get('content') or '', 'tool_calls': calls},
                          ensure_ascii=False, sort_keys=True)

    def _remember_reasoning(self, token, raw):
        cache = self.reasoning_history.setdefault(token, {})
        for choice in raw.get('choices') or []:
            message = choice.get('message') or {}
            fields = {k: copy.deepcopy(message[k]) for k in
                      ('reasoning_details', 'reasoning', 'reasoning_content') if message.get(k)}
            if fields:
                cache[self._assistant_identity(message)] = fields

    def _restore_reasoning(self, token, scope, payload):
        # API compatibility only: restore exact provider-returned metadata for
        # matching assistant messages within this scope. Never add new reasoning.
        if not payload.get('tools'):
            return 0
        if token not in self.reasoning_history:
            self.reasoning_history[token] = {}
            for cid in scope['calls']:
                path = self.root / 'provider-calls' / cid / 'response.json'
                if path.exists():
                    self._remember_reasoning(token, json.loads(path.read_text()))
        restored = 0
        for message in payload.get('messages') or []:
            if message.get('role') != 'assistant':
                continue
            fields = self.reasoning_history[token].get(self._assistant_identity(message))
            if fields:
                missing = {k: v for k, v in fields.items() if not message.get(k)}
                if missing:
                    message.update(copy.deepcopy(missing))
                    restored += 1
        return restored


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, broker, control_tokens=None):
        self.broker = broker
        self.control_tokens = control_tokens or {}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Broker has already retained the upstream response and bill.

    def do_GET(self):
        if self.path == '/v1/models':
            self.reply(200, {'object': 'list', 'data': [{'id': self.server.broker.config['model']['id'], 'object': 'model'}]})
        else:
            self.reply(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        route = self.path.split('?', 1)[0]
        if route.startswith('/internal/'):
            return self.control(route)
        if route not in ('/v1/chat/completions', '/v1/messages', '/v1/messages/count_tokens'):
            return self.reply(404, {'error': {'message': 'not found'}})
        token = self.headers.get('Authorization', '').removeprefix('Bearer ') or self.headers.get('x-api-key', '')
        try:
            if token not in self.server.broker.scopes:
                raise PermissionError('unknown local scope')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 20_000_000:
                raise ValueError('invalid payload size')
            body = json.loads(self.rfile.read(length))
            if route == '/v1/chat/completions':
                self.reply(200, self.server.broker.execute(token, body))
            else:
                from .anthropic_transport import to_chat, from_chat, stream_message
                if route.endswith('/count_tokens'):
                    payload = to_chat({**body, 'max_tokens': body.get('max_tokens', 32768)})
                    serialized = json.dumps({'messages': payload['messages'], 'tools': payload.get('tools', [])}, ensure_ascii=False)
                    tok = self.server.broker.tokenizer
                    count = len(tok.encode(serialized).ids) if tok else len(serialized.encode())
                    return self.reply(200, {'input_tokens': count + 1024 + 64 * len(payload['messages']) + 512 * len(payload.get('tools', []))})
                raw = self.server.broker.execute(token, to_chat(body))
                message = from_chat(raw)
                if not body.get('stream'):
                    return self.reply(200, message)
                wire = stream_message(message)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                try:
                    self.wfile.write(wire)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        except PermissionError:
            self.reply(401, {'error': {'message': 'invalid local token'}})
        except ContextAdmissionExceeded as exc:
            self.reply(400, {'error': {'message': str(exc),
                'type': 'context_length_exceeded', 'code': 'context_length_exceeded'}})
        except SpendBlocked as exc:
            self.reply(402, {'error': {'message': str(exc), 'type': 'experiment_budget_stop'}})
        except urllib.error.HTTPError as exc:
            self.reply(exc.code, {'error': {'message': 'upstream request rejected; see saved receipt'}})
        except Exception as exc:
            self.reply(503, {'error': {'message': type(exc).__name__ + '; pause and inspect saved usage', 'type': 'experiment_requires_review'}})


    def control(self, route):
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        arm = self.server.control_tokens.get(token)
        if arm is None:
            return self.reply(401, {'error': {'message': 'invalid controller token'}})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 100000:
                raise ValueError('invalid controller payload')
            body = json.loads(self.rfile.read(length))
            broker = self.server.broker
            if route == '/internal/scope':
                name = body['name']
                allowed = (f'arms/{arm}/', 'native-meta-workspace/' if arm == 'meta-harness' else f'arms/{arm}/')
                if not name.startswith(allowed) or '..' in name.split('/'):
                    raise ValueError('scope outside this arm')
                local = broker.scope(arm, body['phase'], name, body.get('limit'))
                return self.reply(200, {'token': local})
            if route == '/internal/scopes':
                with broker.lock:
                    scopes = copy.deepcopy({t:s for t,s in broker.scopes.items() if s['arm'] == arm})
                return self.reply(200, {'scopes': scopes})
            self.reply(404, {'error': {'message': 'unknown controller operation'}})
        except Exception as exc:
            self.reply(409, {'error': {'message': str(exc)}})


def load_config(root):
    return json.loads((Path(root) / 'configuration.json').read_text())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--key-file', type=Path, required=True)
    p.add_argument('--port', type=int, default=8050)
    args = p.parse_args()
    broker = Broker(args.root, args.key_file.read_text().strip(), load_config(args.root))
    Server(('127.0.0.1', args.port), broker).serve_forever()


if __name__ == '__main__':
    main()
