"""Offline provider SSE, request progress, recovery and provenance boundaries."""
import asyncio
import importlib
import json
from io import BytesIO
from types import SimpleNamespace
from threading import Event

import httpx
import pytest

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.contracts import ExecutionState, ResearchResult, ProgramResult
from opportunity_agent.v2.agents.orchestrator import LLMSynthesizer, CustomOrchestrator, SynthesizerUnavailable
from opportunity_agent.v2.core.progress import ResearchProgress, relay_progress, progress_key, safe_progress
from opportunity_agent.v2.research.service import ResearchService, extraction_context
from opportunity_agent.v2.research.task import parse_task
from opportunity_agent.v2.research.web import TavilyMCP


def frame(content=None, finish=None, **delta):
    if content is not None:
        delta['content'] = content
    return ('data: '+json.dumps({'choices':[{'index':0, 'delta':delta, 'finish_reason':finish}]})+'\n\n').encode()


class Stream(BytesIO):
    headers = {'content-type':'text/event-stream'}


def test_provider_stream_emits_before_completion_and_never_reasoning(monkeypatch):
    stream=Stream(frame(reasoning_content='private reasoning')+frame('你好')+frame('，朋友')+frame(finish='stop')+b'data: [DONE]\n\n')
    received=[]
    def observe(delta):
        received.append(delta)
        assert not stream.closed and stream.tell()<len(stream.getvalue())
    monkeypatch.setattr('opportunity_agent.llm_client.open_modelscope_request', lambda *a,**k: stream)
    assert LLMClient(api_key='test').generate_stream(system='test',user='test',on_delta=observe)=='你好，朋友'
    assert received==['你好','，朋友']


def test_partial_stream_error_is_not_retried_or_committed(monkeypatch):
    calls=[]
    def open_stream(*a,**k):
        calls.append(1)
        return Stream(frame('draft'))  # Missing completion marker.
    monkeypatch.setattr('opportunity_agent.llm_client.open_modelscope_request',open_stream)
    received=[]
    with pytest.raises(RuntimeError):
        LLMClient(api_key='test',retries=3).generate_stream(system='test',user='test',on_delta=received.append)
    assert calls==[1] and received==['draft']


def test_stream_cancellation_stops_next_frame(monkeypatch):
    cancel=Event()
    monkeypatch.setattr('opportunity_agent.llm_client.open_modelscope_request',lambda *a,**k:Stream(frame('one')+frame('two')+frame(finish='stop')))
    received=[]
    def observe(delta):
        received.append(delta);cancel.set()
    with pytest.raises(TimeoutError):
        LLMClient(api_key='test').generate_stream(system='test',user='test',on_delta=observe,cancel_event=cancel)
    assert received==['one']


def test_draft_hides_links_and_final_uses_citation_validation(monkeypatch):
    monkeypatch.setattr('opportunity_agent.llm_client.open_modelscope_request',lambda *a,**k:Stream(
        frame('你好 [链接](htt')+frame('ps://invented.test/')+frame(')')+frame(finish='stop')))
    async def run():
        state=ExecutionState(user_id='u',conversation_id='c',run_id='r',request_id='q',message='你好')
        answer=await LLMSynthesizer(LLMClient(api_key='test')).synthesize(state)
        snapshots=[e.payload['text'] for e in state.events if e.type=='answer_snapshot']
        assert snapshots and all('invented.test' not in s for s in snapshots)
        assert 'invented.test' not in answer and '来源未核验' in answer
    asyncio.run(run())


def test_failed_synthesis_resets_draft(monkeypatch):
    class Fail:
        async def synthesize(self,state):
            state.add_event('answer_snapshot',text='uncommitted',provisional=True)
            raise SynthesizerUnavailable('test')
    async def run():
        state=ExecutionState(user_id='u',conversation_id='c',run_id='r',request_id='q',message='你好')
        await CustomOrchestrator(synthesizer=Fail())._synthesize_with_fallback(state)
        assert [e.type for e in state.events][-2:]==['answer_reset','synthesizer_fallback']
    asyncio.run(run())


def test_research_progress_relay_is_scoped_and_sanitized(monkeypatch):
    rows=[]
    class Redis:
        def pipeline(self,**kwargs):return self
        def xadd(self,key,data,**kwargs):rows.append((f'{len(rows)+1}-0',data));return self
        def expire(self,*args):return self
        async def execute(self):pass
        async def xread(self,streams,**kwargs):
            cursor=int(next(iter(streams.values())).split('-')[0])
            return [(next(iter(streams)),rows[cursor:])] if rows[cursor:] else []
        async def aclose(self):pass
    monkeypatch.setenv('RESEARCH_PROGRESS_ENABLED','1')
    monkeypatch.setattr('redis.asyncio.Redis.from_url',lambda *a,**k:Redis())
    async def run():
        channel='a'*32
        state=ExecutionState(user_id='u',conversation_id='c',run_id='r',request_id='q',message='query')
        request=SimpleNamespace(progress_channel=channel,round_id=1)
        publisher=ResearchProgress(channel)
        async with relay_progress(request,state):
            await publisher.emit('search',school='CMU',current=1,total=2,secret='must not leak')
        assert state.events[0].type=='research_progress'
        assert state.events[0].payload=={'round_id':1,'stage':'search','school':'CMU','current':1,'total':2}
        await publisher.close()
    asyncio.run(run())
    assert progress_key('not-a-channel') is None
    assert safe_progress({'stage':'arbitrary','text':'secret'}) is None


def test_progress_failure_never_aborts_research(monkeypatch):
    class Broken:
        def pipeline(self,**kwargs):return self
        def xadd(self,*a,**k):return self
        def expire(self,*a,**k):return self
        async def execute(self):raise OSError('offline')
        async def aclose(self):raise OSError('offline')
    monkeypatch.setenv('RESEARCH_PROGRESS_ENABLED','1')
    monkeypatch.setattr('redis.asyncio.Redis.from_url',lambda *a,**k:Broken())
    async def run():
        progress=ResearchProgress('a'*32)
        await progress.emit('search',school='CMU')
        assert progress.key is None
        await progress.close()
    asyncio.run(run())


@pytest.mark.parametrize('error', ['connect','403','404','validation','certificate'])
def test_page_fallback_only_for_retryable_transport_errors(monkeypatch,error):
    calls=[]
    async def direct(*a):
        if error=='validation':raise ValueError('Page left verified official domains')
        if error=='certificate':raise httpx.ConnectError('CERTIFICATE_VERIFY_FAILED')
        if error=='connect':raise httpx.ConnectError('offline')
        response=httpx.Response(int(error),request=httpx.Request('GET','https://www.cmu.edu/'))
        raise httpx.HTTPStatusError('offline',request=response.request,response=response)
    async def extract(*a,**kwargs):
        calls.append(kwargs);return {'url':'https://www.cmu.edu/','text':'body'}
    async def run():
        web=TavilyMCP();web.tools={'tavily_extract':None};web._read_direct=direct;web.extract=extract
        if error in {'connect','403'}:
            assert (await web.read('https://www.cmu.edu/', ['cmu.edu']))['text']=='body'
            assert calls==[{'consume_page':False}]
        else:
            with pytest.raises((ValueError,httpx.HTTPError)):
                await web.read('https://www.cmu.edu/', ['cmu.edu'])
            assert not calls
    asyncio.run(run())


def test_context_compression_keeps_exact_late_quotes():
    quote='Fall 2027 GRE is optional. Deadline December 10, 2026.'
    text='CMU MSCS\n'+'irrelevant '*9000+quote
    shortened=extraction_context(text)
    assert len(shortened)<=12000 and quote in shortened and shortened.startswith('CMU MSCS')


def test_irrelevant_or_wrong_intake_page_never_creates_evidence():
    class Model:
        enabled=True
        def generate_structured(self,*a,**k):
            from research_response_fixtures import structured
            return structured(k['context'],[dict(field='gre_policy',value='optional',quote='GRE is optional.')])
    async def run():
        task=parse_task(SimpleNamespace(message='查询 CMU MSCS 2027 Fall GRE',request_id='q',missing_task=None,success_criteria=None))
        service=ResearchService(None,llm=Model())
        target=ProgramResult(university='CMU',program='MSCS',intake='2027 Fall')
        for text in ['MSCS Fall 2026 GRE is optional.','Master of Biostatistics Fall 2027 GRE optional.']:
            result=ResearchResult()
            await service._accept_page(task,result,target,{'url':'https://www.cmu.edu/admissions','title':text,'text':text})
            assert not result.programs
            assert result.diagnostics['web_field_rejections']
    asyncio.run(run())


def test_prior_extraction_timeouts_open_circuit_across_repairs():
    from opportunity_agent.v2.agents.a2a import DomainA2ARequest
    request=DomainA2ARequest(agent='research',user_id='u',conversation_id='c',run_id='r',request_id='q',message='CMU MSCS 2027 Fall GRE')
    task=parse_task(request)
    task.research_progress={'one':{'pages':{str(i):{'stage':'extract','code':'TimeoutError'} for i in range(3)}}}
    class Web:
        search_limit,page_limit=2,5
        search_calls=page_calls=0
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def search(self,*a):raise AssertionError('no repeated search when extractor is unavailable')
    async def run():
        result=ResearchResult()
        await ResearchService(None,llm=SimpleNamespace(enabled=False),web_factory=Web)._web(task,result,
            [ProgramResult(university='CMU',program='MSCS',intake='2027 Fall')])
        assert result.diagnostics['extraction_circuit_open'] and not result.diagnostics['search_calls']
    asyncio.run(run())


def test_sse_resume_draft_reset_and_ownership(monkeypatch):
    from httpx import AsyncClient,ASGITransport
    from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine
    from opportunity_agent.v2.db.base import Base
    from opportunity_agent.v2.db.session import get_session
    from opportunity_agent.v2.db.models import AgentRun,AgentEvent
    api=importlib.import_module('opportunity_agent.v2.api.app')
    async def run():
        engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        async with engine.begin() as conn:await conn.run_sync(Base.metadata.create_all)
        factory=async_sessionmaker(engine,expire_on_commit=False)
        async def sessions():
            async with factory() as session:yield session
        api.app.dependency_overrides[get_session]=sessions
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app),base_url='http://test') as client:
                user=(await client.post('/api/v1/auth/register',json={'email':'progress@example.test','password':'long-password'})).json()['user']['id']
                cid=(await client.post('/api/v1/conversations',json={'title':'progress'})).json()['id']
                async with factory.begin() as session:
                    session.add(AgentRun(id='progress-run',user_id=user,conversation_id=cid,status='running',request_id='q',event_sequence=2))
                    await session.flush()
                    session.add_all([AgentEvent(run_id='progress-run',sequence=1,event_type='research_progress',payload={'stage':'search','school':'CMU'}),
                        AgentEvent(run_id='progress-run',sequence=2,event_type='answer_snapshot',payload={'text':'draft','provisional':True})])
                view=(await client.get('/api/v1/runs/progress-run')).json()
                assert view['draft_answer']=='draft' and view['progress']['school']=='CMU' and view['last_event_sequence']==2
                async with factory.begin() as session:
                    row=await session.get(AgentRun,'progress-run');row.status='completed';row.event_sequence=4;row.graph_state={'answer':'final'}
                    session.add_all([AgentEvent(run_id='progress-run',sequence=3,event_type='answer_reset',payload={}),
                        AgentEvent(run_id='progress-run',sequence=4,event_type='run_completed',payload={})])
                response=await client.get('/api/v1/runs/progress-run/events?after=1',headers={'Last-Event-ID':'2'})
                assert response.status_code==200 and 'id: 3' in response.text and 'id: 4' in response.text
                assert 'answer_snapshot' not in response.text and 'research_progress' not in response.text
                assert response.headers['x-accel-buffering']=='no'
                assert (await client.get('/api/v1/runs/progress-run/events',headers={'Last-Event-ID':'bad'})).status_code==400
                assert (await client.get('/api/v1/runs/progress-run/events?after=99')).status_code==400
                assert (await client.get('/api/v1/runs/progress-run')).json()['draft_answer']==''
                await client.post('/api/v1/auth/register',json={'email':'other-progress@example.test','password':'long-password'})
                assert (await client.get('/api/v1/runs/progress-run/events')).status_code==404
        finally:
            api.app.dependency_overrides.clear();await engine.dispose()
    asyncio.run(run())
