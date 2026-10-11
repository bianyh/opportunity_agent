import asyncio
import importlib

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opportunity_agent.v2.research.failures import run_failure_report
from opportunity_agent.v2.research.failures import failure_summary
from opportunity_agent.v2.agents.contracts import ExecutionState, CompletionResult, ResearchResult
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import AgentRun, AgentEvent
from opportunity_agent.v2.db.session import get_session


def partial_state():
    return {'completion':{'status':'PARTIAL','reasons':['尚未达到证据要求'],
            'missing_tasks':[{'agent':'research','missing_fields':['deadline','gre_policy']}]},
        'research_result':{'errors':[
            {'stage':'extract','code':'TimeoutError','target_id':'brown','page_id':'one','message':'secret provider response'},
            {'stage':'extract','code':'ValueError','reason':'empty_content','target_id':'brown','page_id':'two'},
            {'stage':'read_page','code':'ValueError','reason':'official_domain_boundary','target_id':'cmu','page_id':'three'},
            *[{'stage':'extract','code':'extraction_service_unavailable'} for _ in range(3)]],
            'diagnostics':{'web_progress':{'brown':{'university':'Brown University'},'cmu':{'university':'Carnegie Mellon University'}},
                'rounds':[{'extraction_attempts':[{'seconds':30.558,'attempts':[{'error_code':'TimeoutError'}]}],
                    'web_rejections':[{'precheck':True,'university':'Duke University','reason':'program_or_intake_mismatch'}]}, {}, {}]}}}


def test_report_uses_observed_reasons_and_joins_schools_without_secrets():
    report=run_failure_report(partial_state())
    issues={item['code']:item for item in report['issues']}
    assert report['has_issues'] and report['status']=='PARTIAL'
    assert issues['timeout']['schools']==['Brown University']
    assert issues['empty_content']['count']==1
    assert issues['official_domain_boundary']['schools']==['Carnegie Mellon University']
    assert issues['extraction_service_unavailable']['count']==1  # Not three extra model requests.
    assert issues['source_precheck']['schools']==['Duke University']
    assert report['extraction_timeout_seconds']==30.558
    assert report['missing_fields']==['deadline','gre_policy']
    assert 'secret provider response' not in str(report)
    assert '尚未公布' not in str(report)  # Absence of evidence is not evidence of publication status.


def test_pass_hides_recovered_failures_and_no_criteria_is_not_failure():
    state=partial_state();state['completion']['status']='PASS'
    report=run_failure_report(state)
    assert not report['has_issues'] and not report['issues']
    assert not run_failure_report({})['has_issues']
    assert run_failure_report({'error':'private traceback'})['summary']=='执行失败'
    assert 'private traceback' not in str(run_failure_report({'error':'private traceback'}))


def test_new_error_summary_matches_frontend_and_counts_calls_not_duplicate_snapshots():
    errors = [{'stage': 'read_page', 'code': 'HTTPS_REQUIRED', 'call_id': 'http'},
              {'stage': 'extract', 'code': 'PROGRAM_MISMATCH', 'call_id': 'wrong'},
              {'stage': 'repair', 'code': 'REPAIR_MODEL_TIMEOUT', 'call_id': 'planner'},
              {'stage': 'extract', 'code': 'MODEL_TIMEOUT', 'call_id': 'model1'},
              {'stage': 'extract', 'code': 'MODEL_TIMEOUT', 'call_id': 'model2'}]
    result = ResearchResult(errors=[*errors, errors[-1]])
    report = run_failure_report({'completion': {'status': 'PARTIAL'}, 'research_result': result.model_dump()})
    issues = {i['code']: i for i in report['issues']}
    assert issues['timeout']['count'] == 2
    assert 'repair_planner_failure' in issues and 'source_mismatch' in issues
    assert 'read_failure' not in issues
    assert failure_summary(result) == [i['message'] for i in report['issues']]


def test_diagnostics_event_exists_before_synthesizer_is_called():
    class Synthesizer:
        async def synthesize(self,state):
            report=next(e.payload for e in state.events if e.type=='run_diagnostics')
            assert report['has_issues'] and report['issues'][0]['code']=='timeout'
            return 'partial answer'
    async def run():
        state=ExecutionState(user_id='u',conversation_id='c',run_id='r',request_id='q',message='research',
            completion=CompletionResult(status='PARTIAL'),research_result=ResearchResult(errors=[{'stage':'extract','code':'TimeoutError'}]))
        assert await CustomOrchestrator(synthesizer=Synthesizer())._synthesize_with_fallback(state)=='partial answer'
    asyncio.run(run())


def test_historical_and_inflight_reports_are_returned_with_ownership(monkeypatch):
    api=importlib.import_module('opportunity_agent.v2.api.app')
    async def run():
        engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        async with engine.begin() as connection:await connection.run_sync(Base.metadata.create_all)
        factory=async_sessionmaker(engine,expire_on_commit=False)
        async def sessions():
            async with factory() as session:yield session
        api.app.dependency_overrides[get_session]=sessions
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app),base_url='http://test') as client:
                user=(await client.post('/api/v1/auth/register',json={'email':'failure-report@example.test','password':'long-password'})).json()['user']['id']
                cid=(await client.post('/api/v1/conversations',json={'title':'reports'})).json()['id']
                report=run_failure_report(partial_state())
                async with factory.begin() as session:
                    session.add_all([AgentRun(id='old-report',user_id=user,conversation_id=cid,status='completed',graph_state=partial_state()),
                        AgentRun(id='inflight-report',user_id=user,conversation_id=cid,status='running',graph_state={},event_sequence=1)])
                    await session.flush()
                    session.add(AgentEvent(run_id='inflight-report',sequence=1,event_type='run_diagnostics',payload=report))
                old=(await client.get('/api/v1/runs/old-report')).json()
                assert old['status']=='completed' and old['failure_report']==report
                current=(await client.get('/api/v1/runs/inflight-report')).json()
                assert current['status']=='running' and current['failure_report']==report
                await client.post('/api/v1/auth/register',json={'email':'other-report@example.test','password':'long-password'})
                assert (await client.get('/api/v1/runs/old-report')).status_code==404
                assert (await client.get('/api/v1/runs/inflight-report')).status_code==404
        finally:
            api.app.dependency_overrides.clear();await engine.dispose()
    asyncio.run(run())
