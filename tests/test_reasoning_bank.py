"""Official-source behavior, causal retrieval and no-double-charge regressions."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from swe_chain_evo.common import atomic, read, sha
from reasoning_bank import native
from reasoning_bank.transport import Clients, EmbeddingBroker, checkpoint_call
from conftest import configuration


def cfg(root):
    value=configuration('experience-notes')
    value['arms']={'reasoning-bank':{}}
    value['spend']={'global_limit':'50.00','ledger_path':str(root/'spend.sqlite')}
    value['runtime']['model_transport_retries']=0
    return value


def test_official_retrieval_uses_instruction_query_and_only_previous_cache(tmp_path):
    cache=tmp_path/'embeddings.jsonl'
    cache.write_text('\n'.join(json.dumps(v) for v in [
        {'id':'a','text':'older a','embedding':[1.,0.,0.]},
        {'id':'b','text':'older b','embedding':[0.,1.,0.]}])+'\n')
    bank=[{'task_id':'a','index':0,'memory_items':['memory A']},
          {'task_id':'b','index':1,'memory_items':['memory B']}]
    seen=[]
    def embed(text,kind,dim):
        seen.append((text,kind,dim))
        return [1.,0.,0.] if text.startswith('Instruct:') else [0.,1.,0.]
    selected,text=native.retrieval(bank,'new task','c',cache,embed)
    assert [x['task_id'] for x in selected]==['a'] and text=='memory A'
    assert len(seen)==2 and all(x[1:] == ('RETRIEVAL_DOCUMENT',3072) for x in seen)
    assert [json.loads(x)['id'] for x in cache.read_text().splitlines()]==['a','b','c']


def test_native_judge_and_extractor_keep_original_signals_and_role_temperatures():
    calls=[]
    def generate(role,model,contents,options):
        calls.append((role,contents,options))
        return 'fail' if role=='judge' else '# Memory Item 1\n## Title Check contracts\n## Description Inspect signatures\n## Content Read callers.'
    status,items=native.judge_and_extract('public issue','public action and observation','same-model',generate)
    assert status is False and len(items)==1
    assert [c[0] for c in calls]==['judge','extraction']
    assert [c[2].temperature for c in calls]==[0.,1.]
    assert calls[1][2].system_instruction.strip()==native.prompt_values()[1].strip()
    assert all(c[2].max_output_tokens==65536 for c in calls)


def test_meta_checkpoint_reuses_paid_response_without_second_generation(tmp_path):
    count=[]
    def send(payload):
        count.append(payload)
        return {'id':'offline','choices':[{'message':{'content':'success'}}],
                'usage':{'prompt_tokens':100,'completion_tokens':1,'cost':0.00001}}
    clients=Clients(tmp_path,'offline-key',cfg(tmp_path),sender=send)
    options=SimpleNamespace(max_output_tokens=65536,system_instruction='judge',temperature=0.)
    for i in range(2):
        assert clients.generate(tmp_path/'arms/reasoning-bank/steps/000000/update','judge','openai/gpt-6.1-sol','public',options)=='success'
    assert len(count)==1
    assert float(clients.chat['judge'].ledger.snapshot()['charged_usd'])==0.00001
    assert count[0]['temperature']==0.0  # Preserve the native judge role temperature.


def test_full_trace_preserved_when_output_ceiling_is_slightly_too_large(tmp_path):
    from reasoning_bank.transport import fingerprint
    seen=[]
    def send(payload):
        seen.append(payload)
        return {'id':'fitted','choices':[{'message':{'content':'success'}}],
                'usage':{'prompt_tokens':195741,'completion_tokens':10,'cost':0.03}}
    configuration=cfg(tmp_path)
    configuration['model']['admission_token_margin']=1.0  # Live DeepSeek profile.
    clients=Clients(tmp_path,'offline',configuration,sender=send)
    broker=clients.chat['judge']
    broker.tokenizer=SimpleNamespace(encode=lambda text:SimpleNamespace(ids=range(195741)))
    output=tmp_path/'arms/reasoning-bank/steps/000000/update'
    options=SimpleNamespace(max_output_tokens=65536,system_instruction='judge',temperature=0.)
    payload={'model':'openai/gpt-6.1-sol','max_tokens':65536,
             'messages':[{'role':'system','content':'judge'},{'role':'user','content':'complete trace'}]}
    folder=output/'judge';folder.mkdir(parents=True)
    token=broker.scope('reasoning-bank','judge',str(folder.relative_to(tmp_path)))
    atomic(folder/'call.json',{'token':token,'payload_sha256':fingerprint(payload)})
    assert clients.generate(output,'judge','openai/gpt-6.1-sol','complete trace',options)=='success'
    assert seen[0]['max_tokens']==65251
    assert seen[0]['messages']==payload['messages']
    assert read(folder/'predispatch-context-recovery.json')['prior_provider_calls']==0
    assert read(folder/'context-admission.json')['input_truncated'] is False


def test_empty_provider_message_retries_once_with_all_costs_and_reuses_checkpoint(tmp_path):
    seen=[]
    def send(payload):
        seen.append(payload)
        return {'id':'empty' if len(seen)==1 else 'valid','choices':[{
            'finish_reason':'stop','message':{'content':None if len(seen)==1 else 'memory'}}],
                'usage':{'prompt_tokens':100,'completion_tokens':10,'cost':0.00001}}
    clients=Clients(tmp_path,'offline',cfg(tmp_path),sender=send)
    options=SimpleNamespace(max_output_tokens=65536,system_instruction='extract',temperature=1.)
    output=tmp_path/'arms/reasoning-bank/steps/000000/update'
    for _ in range(2):
        assert clients.generate(output,'extraction','openai/gpt-6.1-sol','complete trace',options)=='memory'
    assert len(seen)==2 and seen[0]==seen[1]
    assert float(clients.chat['extraction'].ledger.snapshot()['charged_usd'])==0.00002
    assert read(output/'extraction/empty-response-recovery.json')['state']=='recovered'


def test_embedding_unknown_usage_retains_reservation_and_cannot_become_free(tmp_path):
    def send(payload):return {'data':[{'embedding':[0.]*3072}],'usage':{'prompt_tokens':4}}
    b=EmbeddingBroker(tmp_path/'channels/embedding','offline',cfg(tmp_path),sender=send)
    p={'model':'google/gemini-embedding-001','input':'query'}
    with pytest.raises(ValueError,match='usage/cost'):
        checkpoint_call(b,tmp_path/'steps/000000/retrieval/embed-01','reasoning-bank','retrieval',p)
    snapshot=b.ledger.snapshot()
    assert float(snapshot['pending_reserved_usd'])>0 and snapshot['blocking'][0][1]=='uncertain'
    with pytest.raises(RuntimeError,match='Partial paid'):
        checkpoint_call(b,tmp_path/'steps/000000/retrieval/embed-01','reasoning-bank','retrieval',p)


def test_embedding_disconnect_before_dispatch_retries_without_unknown_charge(tmp_path):
    import urllib.error
    from methods.evo_fifty.gateway import TransientProviderError
    attempts=[]
    def send(payload):
        attempts.append(payload)
        if len(attempts)==1:
            b.observation['request_bytes_may_have_been_sent']=False
            raise urllib.error.URLError('TLS handshake interrupted')
        return {'data':[{'embedding':[0.]*3072}],
                'usage':{'prompt_tokens':3,'cost':0.00000045}}
    b=EmbeddingBroker(tmp_path/'channels/embedding','offline',cfg(tmp_path),sender=send)
    out=tmp_path/'steps/000000/retrieval/embed-01'
    payload={'model':'google/gemini-embedding-001','input':'query'}
    with pytest.raises(TransientProviderError):
        checkpoint_call(b,out,'reasoning-bank','retrieval',payload)
    assert not b.ledger.snapshot()['blocking']
    assert checkpoint_call(b,out,'reasoning-bank','retrieval',payload)['data']
    assert len(attempts)==2
    assert b.ledger.snapshot()['pending_reserved_usd']=='0'


def test_openrouter_embedding_uses_supported_document_input_type(tmp_path):
    seen=[]
    def send(payload):
        seen.append(payload)
        return {'data':[{'embedding':[0.]*3072}],
                'usage':{'prompt_tokens':3,'cost':0.00000045}}
    clients=Clients(tmp_path,'offline',cfg(tmp_path),embedding_sender=send)
    vector=clients.embed(0,tmp_path/'steps/000000/retrieval','query','RETRIEVAL_DOCUMENT',3072)
    assert len(vector)==3072
    assert seen[0]['input_type']=='RETRIEVAL_DOCUMENT'
    assert 'task_type' not in seen[0]


def test_system_memory_reaches_kira_once_without_changing_task(tmp_path,monkeypatch):
    import litellm
    from reasoning_bank.agent import KiraReasoningBank
    from swe_chain_evo.agents import KiraAudited
    note=tmp_path/'note.md';note.write_text('Reusable memory.')
    monkeypatch.setenv('WITHIN_CHAIN_NOTE',str(note));monkeypatch.setenv('WITHIN_CHAIN_NOTE_SHA256',sha(note))
    captured=[]
    async def provider(*args,**kwargs):captured.append(kwargs);return 'ok'
    async def run(self,instruction,env,context):
        assert instruction=='unchanged task'
        original=[{'role':'system','content':'Original KIRA system'},{'role':'user','content':instruction}]
        for i in range(2):await litellm.acompletion(messages=original)
        assert original[0]['content']=='Original KIRA system'
    monkeypatch.setattr(litellm,'acompletion',provider);monkeypatch.setattr(KiraAudited,'run',run)
    agent=KiraReasoningBank.__new__(KiraReasoningBank);agent.logs_dir=tmp_path
    asyncio.run(agent.run('unchanged task',None,None))
    assert len(captured)==2
    assert all(c['messages'][0]['content'].count('Reusable memory.')==1 for c in captured)
    assert all(c['messages'][0]['content'].startswith('Original KIRA system') for c in captured)
