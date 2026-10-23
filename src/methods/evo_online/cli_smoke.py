"""Exercise the exact upstream proposer wrapper through the common gateway."""
import argparse
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

from .gateway import Broker, Server, load_config
from .native_meta import VENDOR, isolated_command
from .trial import atomic


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--real',action='store_true')
    args=p.parse_args()
    output=args.root/('native-cli-real-smoke' if args.real else 'native-cli-mock-smoke')
    output.mkdir(parents=True,exist_ok=True)
    fixture=output/'fixture.txt';fixture.write_text('NATIVE_WRAPPER_READ_OK\n')
    config=load_config(args.root)
    import sys
    sys.path.insert(0,str(VENDOR))
    import claude_wrapper
    original_build=claude_wrapper.build_command
    received=[]
    def fake(body):
        received.append(body)
        used=any(m.get('role')=='tool' for m in body['messages'])
        message={'role':'assistant','content':'NATIVE_WRAPPER_READ_OK' if used else None}
        if not used:
            message['tool_calls']=[{'id':'native-read','type':'function','function':{
                'name':'Read','arguments':json.dumps({'file_path':str(fixture)})}}]
        return {'id':'mock-'+str(len(received)),'model':config['model']['id'],'provider':'DeepInfra',
                'choices':[{'index':0,'finish_reason':'stop' if used else 'tool_calls','message':message}],
                'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':0}}
    key=(args.root/'.openrouter-key').read_text().strip() if args.real else 'mock-only'
    broker=Broker(args.root,key,config,sender=None if args.real else fake)
    token=broker.scope('smoke','proposal',output.name,limit=200000)
    server=Server(('127.0.0.1',0),broker)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    env={k:v for k,v in os.environ.items() if k not in ('OPENROUTER_API_KEY','OPENAI_API_KEY','DEEPSEEK_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDECODE')}
    model=config['model']['id']
    env.update(ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',ANTHROPIC_AUTH_TOKEN=token,
        ANTHROPIC_API_KEY='',CLAUDE_CONFIG_DIR=str(output/'.claude-private'),CLAUDE_CODE_SUBAGENT_MODEL=model,
        ANTHROPIC_MODEL=model,CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CLAUDE_CODE_DISABLE_AUTO_UPDATE='1',
        CLAUDE_CODE_MAX_OUTPUT_TOKENS='1024',HTTP_PROXY=config['runtime']['host_outbound_proxy'],
        HTTPS_PROXY=config['runtime']['host_outbound_proxy'],NO_PROXY='127.0.0.1,localhost')
    for role in ('OPUS','SONNET','HAIKU','FABLE'):env['ANTHROPIC_DEFAULT_'+role+'_MODEL']=model
    try:
        with patch.dict(os.environ,env,clear=True),patch.object(claude_wrapper,'build_command',
              lambda *a,**k:isolated_command(original_build(*a,**k),output)):
            result=claude_wrapper.run(prompt=f'Use the Read tool to read {fixture}, then reply with its exact content. Do not use other tools.',
                model='opus',allowed_tools=['Read'],cwd=str(output),log_dir=str(output/'logs'),
                name='native-wrapper',timeout_seconds=180,progress=False)
        value={'exit_code':result.exit_code,'response':result.text,'calls':len(broker.scopes[token]['calls']),
               'tokens':broker.scopes[token]['used_tokens'],'ledger':broker.ledger.snapshot(),
               'stderr':result.stderr[-2000:],'original_wrapper_sha256':__import__('hashlib').sha256((VENDOR/'claude_wrapper.py').read_bytes()).hexdigest()}
        atomic(output/'result.json',value)
        print(json.dumps(value,ensure_ascii=False))
        if result.exit_code or 'NATIVE_WRAPPER_READ_OK' not in result.text:raise SystemExit(1)
    finally:
        server.shutdown();server.server_close()


if __name__=='__main__':main()
