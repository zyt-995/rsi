"""Isolate each official RSI loop's Python hooks; share one billing gateway."""
import argparse
import json
import os
from pathlib import Path
import traceback
import urllib.request

from .gateway import load_config
from .spend import SpendLedger
from .trial import TrialRunner, atomic


def error_record(exc):
    kind = getattr(exc, 'original_type', type(exc).__name__)
    return {'type': kind, 'wrapper_type': type(exc).__name__, 'message': str(exc),
            'traceback': traceback.format_exc()}


class RemoteBroker:
    def __init__(self, root, arm, port, token):
        self.root, self.arm, self.port, self.token = Path(root), arm, port, token
        self.config = load_config(self.root)
        self.ledger = SpendLedger(self.root/'spend.sqlite', self.config['spend']['global_limit'])

    def request(self, route, body):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/internal/{route}',
            data=json.dumps(body).encode(), headers={'Authorization':'Bearer '+self.token,
            'Content-Type':'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=60) as response:
            return json.load(response)

    def scope(self, arm, phase, name, limit=None):
        if arm != self.arm: raise ValueError('worker cannot create another arm scope')
        return self.request('scope', {'phase':phase, 'name':name, 'limit':limit})['token']

    @property
    def scopes(self):
        return self.request('scopes', {})['scopes']


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--arm',choices=('meta-harness','modularrsi'),required=True)
    parser.add_argument('--index',type=int,required=True)
    args=parser.parse_args()
    # Do not expose the coordinator control credential to the native CLI or
    # editors and task subprocesses spawned by the method.
    token=os.environ.pop('EVO_ONLINE_CONTROL_TOKEN')
    config=load_config(args.root)
    broker=RemoteBroker(args.root,args.arm,config['runtime']['gateway_port'],token)
    runner=TrialRunner(args.root,broker,port=broker.port)
    runner.pins=json.loads((args.root/'native-source-pins.json').read_text())
    state=json.loads((args.root/'native-campaign.json').read_text())
    item=state['arms'][args.arm]
    output=args.root/'arms'/args.arm/'steps'/f'{args.index:06d}'/'update'
    try:
        if args.arm=='meta-harness':
            from .native_meta import update
        else:
            from .modular import update
        decision=update(runner,args.index,Path(item['parent']),item['rows'],output)
        outcome={'ok':True,'decision':decision}
    except BaseException as exc:
        outcome={'ok':False,'error':error_record(exc)}
    atomic(output/'worker-outcome.json',outcome)
    if not outcome['ok']: raise SystemExit(1)


if __name__=='__main__':main()
