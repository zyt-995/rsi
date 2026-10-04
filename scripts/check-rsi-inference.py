#!/usr/bin/env python3
"""API compatibility smoke: text, JSON, automatic tool call and tool response."""
import argparse
import json
from pathlib import Path
import time
import urllib.request


def query(port, **extra):
    payload = {'model': 'Qwen/Qwen3.8-27B', 'temperature': 0, 'seed': 0, 'max_tokens': 1024,
               'chat_template_kwargs': {'enable_thinking': False}, **extra}
    req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer EMPTY'})
    with urllib.request.urlopen(req, timeout=120) as r:
        value = json.load(r)
    if value['choices'][0]['finish_reason'] == 'length':
        raise RuntimeError('smoke_response_truncated')
    return value


def check(port):
    started = time.time()
    plain = query(port, messages=[{'role':'user', 'content':'Reply with the sum of 17 and 25. Just the number.'}])
    assert plain['choices'][0]['message']['content'].strip() == '42'
    structured = query(port, messages=[{'role':'user','content':'Return a JSON object with key ok and value true.'}],
                       response_format={'type':'json_object'})
    assert json.loads(structured['choices'][0]['message']['content']) == {'ok': True}
    messages = [{'role':'system','content':'When asked to run a command, call the terminal tool. Do not pretend to execute it.'},
                {'role':'user','content':'Run the exact command echo RSI_SMOKE_OK using the terminal tool.'}]
    tools = [{'type':'function','function':{'name':'terminal','description':'Execute a terminal command',
             'parameters':{'type':'object','properties':{'command':{'type':'string'}},'required':['command']}}}]
    tool = query(port, messages=messages, tools=tools, tool_choice='auto')
    msg = tool['choices'][0]['message']
    calls = msg.get('tool_calls', [])
    assert len(calls) == 1 and calls[0]['function']['name'] == 'terminal'
    assert json.loads(calls[0]['function']['arguments']) == {'command': 'echo RSI_SMOKE_OK'}
    messages += [{'role':'assistant', 'content':msg.get('content'), 'tool_calls': calls},
                 {'role':'tool','tool_call_id':calls[0]['id'],'content':'RSI_SMOKE_OK\n'},
                 {'role':'user','content':'Repeat the observed output exactly; do not call a tool.'}]
    followup = query(port, messages=messages, tools=tools, tool_choice='none')
    assert 'RSI_SMOKE_OK' in followup['choices'][0]['message']['content']
    reasoning_tool = query(port, messages=messages[:2], tools=tools, tool_choice='auto',
                           chat_template_kwargs={'enable_thinking': True})
    reasoning_message = reasoning_tool['choices'][0]['message']
    reasoning_calls = reasoning_message.get('tool_calls', [])
    assert len(reasoning_calls) == 1 and reasoning_calls[0]['function']['name'] == 'terminal'
    assert json.loads(reasoning_calls[0]['function']['arguments']) == {'command': 'echo RSI_SMOKE_OK'}
    return {'port': port, 'time': started, 'elapsed_seconds': time.time()-started, 'passed': True,
            'checks': ['plain_text', 'json_object', 'automatic_tool_call', 'tool_response_roundtrip', 'reasoning_tool_call'],
            'responses': [plain, structured, tool, followup, reasoning_tool], 'formal_scores': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise RuntimeError('smoke_output_already_exists')
    report = check(args.port)
    with args.output.open('x') as out:
        json.dump(report, out, indent=2)
    print(json.dumps({k:v for k,v in report.items() if k != 'responses'}))
