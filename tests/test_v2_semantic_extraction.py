"""Contract/evidence tests use recorded responses; NOT a claim of LLM accuracy."""
import asyncio
import json
import time
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult
from opportunity_agent.v2.research.extraction import (
    AdmissionFact, AdmissionExtraction, ExtractionCache, blocks, candidate_blocks,
    normalized_date, verify_fact, same_program)
from opportunity_agent.v2.research.service import ResearchService
from opportunity_agent.v2.research.task import ResearchTaskSpec

CASES = json.loads((Path(__file__).parent/'fixtures/research_extraction_stage1.json').read_text(encoding='utf-8'))


@pytest.fixture(autouse=True)
def no_writes(monkeypatch):
    monkeypatch.setenv('RESEARCH_PERSIST_FACTS', '0')
    monkeypatch.setenv('RESEARCH_QUEUE_INGEST', '0')


def objects(index=0):
    c = CASES[index]
    page = dict(url=c['url'], title=c['program'], text=c['text'])
    target = ProgramResult(university=c['university'], program=c['program'], intake='2027 Fall')
    task = ResearchTaskSpec(query='deadline and GRE', requested_fields=['deadline', 'gre_policy'], as_of=date(2026, 10, 11))
    return page, target, task


def fact(page, target, **updates):
    block = blocks(page['text'], page['title'])[-1]
    data = dict(field='gre_policy', value='not_required', block_id=block['id'],
                quote='GRE is not required.', university=target.university, program=target.program,
                program_quote=target.program, degree='masters', scope='all', intake='2027 Fall', intake_quote='Fall 2027')
    data.update(updates)
    if data['field'] == 'gre_policy':
        required,considered = {'required':(True,None),'optional':(False,True),
            'not_required':(False,None),'not_accepted':(None,False)}[data['value']]
        data.update(gre_required=required,gre_considered=considered)
    return AdmissionFact(**data)


class RecordedModel:
    enabled = True
    def __init__(self, facts):
        self.facts, self.requests = facts, []
    def generate_structured(self, model, **kwargs):
        self.requests.append(kwargs)
        return model(facts=self.facts)


def test_schema_forbids_unstructured_or_missing_identity():
    with pytest.raises(ValidationError):
        AdmissionFact(field='deadline', value='2027-01-15', quote='January 15')


@pytest.mark.parametrize('raw,expected', [
    ('January 15, 2027', '2027-01-15'), ('the fifteenth of January, 2027', '2027-01-15'),
    ('2027年2月1日', '2027-02-01'), ('22 January 2027', '2027-01-22'),
    ('2027-01-20', '2027-01-20'), ('January 15', None), ('02/03/2027', None),
    ('February 30, 2027', None)])
def test_generic_date_normalization(raw, expected):
    assert normalized_date(raw) == expected


@pytest.mark.parametrize('index,quote,value,raw', [
    (0,'Application deadline: January 15, 2027.','2027-01-15','January 15, 2027'),
    (1,'Applications must reach us by the fifteenth of January, 2027.','2027-01-15','the fifteenth of January, 2027'),
    (2,'| Application closes | 2027-01-20 |','2027-01-20','2027-01-20'),
    (3,'申请截止日期为2027年2月1日。','2027-02-01','2027年2月1日'),
    (5,'Application deadline: January 15, 2027.','2027-01-15','January 15, 2027'),
    (6,'Application deadline: January 20, 2027.','2027-01-20','January 20, 2027'),
    (9,'The final application deadline is 22 January 2027 at noon.','2027-01-22','22 January 2027')])
def test_cross_school_recorded_date_evidence(index, quote, value, raw):
    page, target, task = objects(index)
    f = fact(page, target, field='deadline', quote=quote, value=value, date_text=raw, deadline_kind='final')
    proof, reason = verify_fact(f, target, page, task.requested_fields, task.as_of)
    assert reason == ''
    assert page['text'][proof['start']:proof['end']] == quote


@pytest.mark.parametrize('change,reason', [
    ({'university':'Georgia Institute of Technology'}, 'school_mismatch'),
    ({'program':'Master of Data Science'}, 'program_identity_not_grounded'),
    ({'degree':'doctoral'}, 'degree_mismatch'),
    ({'home_unit':'School of Medicine'}, 'home_unit_not_grounded'),
    ({'quote':'GRE is required.'}, 'quote_not_in_block'),
    ({'intake':'2028 Fall'}, 'intake_mismatch'),
    ({'scope':'international'}, 'restricted_or_unknown_applicant_scope')])
def test_reject_misattributed_evidence(change, reason):
    page, target, task = objects()
    assert verify_fact(fact(page,target,**change),target,page,task.requested_fields,task.as_of)[1] == reason


def test_missing_year_never_uses_requested_cycle_or_copyright():
    page, target, task = objects(4)
    f = fact(page,target,field='deadline',value='2027-02-01',quote='The application deadline is February 1.',
             date_text='February 1',deadline_kind='final',intake='',intake_quote='')
    assert verify_fact(f,target,page,task.requested_fields,task.as_of)[1] == 'date_not_explicit_or_invalid'


@pytest.mark.parametrize('index', [5,6])
def test_quote_from_sibling_degree_or_program_cannot_borrow_page_title(index):
    page,target,task = objects(index)
    wrong = blocks(page['text'],page['title'])[0]
    f = fact(page,target,block_id=wrong['id'],quote='GRE is required.',value='required')
    assert verify_fact(f,target,page,task.requested_fields,task.as_of)[0] is None


def test_home_unit_required_when_target_specifies_it():
    page,target,task = objects()
    target.home_unit = 'School of Engineering'
    assert verify_fact(fact(page,target),target,page,task.requested_fields,task.as_of)[1] == 'home_unit_mismatch'


def test_gre_not_considered_cannot_be_labeled_merely_not_required():
    page,target,task = objects()
    response = fact(page,target).model_copy(update={'gre_considered':False})
    assert verify_fact(response,target,page,task.requested_fields,task.as_of)[1] == 'inconsistent_gre_semantics'


def test_gre_identifier_boundary_works_next_to_non_latin_text():
    page,target,task = objects(3)
    response = fact(page,target,quote='GRE成绩不作为录取评估材料。',value='not_accepted')
    assert verify_fact(response,target,page,task.requested_fields,task.as_of)[1] == ''


def test_preparation_excludes_explicit_sibling_programmes_not_shared_context():
    page,target,_ = objects(6)
    selected = candidate_blocks(page,target,['deadline','gre_policy'])
    assert len(selected) == 1 and 'Master of Science in Computer Science' in ' '.join(selected[0]['headings'])


def test_abbreviation_needs_local_alias_evidence():
    assert not same_program('CSE','Computational Science and Engineering','Computer Science and Engineering')
    assert same_program('CSE','Computational Science and Engineering','Computational Science and Engineering (CSE)')
    assert not same_program('MDS','Master of Science in Computer Science','MDS')


def test_multiple_fields_one_call_and_conflicts_are_preserved():
    async def run():
        page,target,task = objects()
        page['text'] += '\nGRE is optional.'
        a = fact(page,target)
        b = fact(page,target,quote='GRE is optional.',value='optional')
        c = fact(page,target,field='deadline',value='2027-01-15',quote='Application deadline: January 15, 2027.',
                 date_text='January 15, 2027',deadline_kind='final')
        model, result = RecordedModel([a,b,c]), ResearchResult()
        await ResearchService(None,llm=model)._accept_page(task,result,target,page)
        assert len(model.requests) == 1
        assert set(model.requests[0]['context']['fields']) == {'deadline','gre_policy'}
        assert result.programs[0].deadline == date(2027,1,15)
        assert result.programs[0].gre_policy == 'unknown'
        assert all(f.verification_status == 'conflicting' for f in result.programs[0].facts if f.field == 'gre_policy')
        assert all(e.extraction_provenance for e in result.programs[0].evidence)
    asyncio.run(run())


def test_disabled_model_and_open_circuit_never_fall_back_to_regex():
    async def run():
        page,target,task = objects()
        model = RecordedModel([])
        service,result = ResearchService(None,llm=model),ResearchResult()
        await service._accept_page(task,result,target,page,llm_blocked='EXTRACTION_CIRCUIT_OPEN')
        assert not result.programs and not model.requests
        assert result.errors[0]['code'] == 'EXTRACTION_CIRCUIT_OPEN'
    asyncio.run(run())


def test_candidate_selection_bounded_and_keeps_late_relevant_section():
    page,target,_ = objects()
    page['text'] = '# News\n' + ('unrelated sports news\n'*10000) + '\n' + page['text']
    selected = candidate_blocks(page,target,['deadline','gre_policy'],4000)
    assert sum(len(b['text'])+sum(map(len,b['headings']))+len(b['title']) for b in selected) <= 4000
    assert any('January 15' in b['text'] for b in selected)
    assert all(page['text'][b['start']:b['end']] == b['text'] for b in selected)


def test_program_heading_does_not_select_unrelated_marketing():
    page,target,_ = objects()
    page['text'] = '# '+target.program+'\n' + ('Our alumni enjoy sports and music.\n'*2000) + '\nGRE is not required.'
    selected = candidate_blocks(page,target,['gre_policy'],2500)
    assert any('GRE' in b['text'] for b in selected)
    assert sum(len(b['text']) for b in selected) <= 1200


def test_year_unknown_deadline_retained_but_not_completion_supported():
    from opportunity_agent.v2.research.quality import field_supported
    from opportunity_agent.v2.agents.contracts import SuccessCriteria
    async def run():
        page,target,task = objects(4)
        response = fact(page,target,field='deadline',value='February 1',
            quote='The application deadline is February 1.',date_text='February 1',
            date_year_status='unknown',deadline_kind='final',intake='',intake_quote='')
        gre = fact(page,target,intake='',intake_quote='')
        result = ResearchResult()
        await ResearchService(None,llm=RecordedModel([response,gre]))._accept_page(task,result,target,page)
        p = result.programs[0]
        assert p.deadline is None and p.gre_policy == 'not_required'
        assert next(f for f in p.facts if f.field == 'deadline').verification_status == 'unknown'
        assert not field_supported(p,'deadline',SuccessCriteria())
        assert p.evidence[0].extraction_provenance['year_status'] == 'unknown'
        from opportunity_agent.v2.agents.orchestrator import _research_for_synthesis
        assert _research_for_synthesis(result)['programs'][0]['facts'][0]['value'] == 'February 1'
    asyncio.run(run())


def test_gre_enum_normalization_changes_format_not_meaning():
    page,target,_ = objects()
    original = fact(page,target).model_dump()
    assert AdmissionFact(**{**original,'value':'Not Required'}).value == 'not_required'
    assert AdmissionFact(**{**original,'value':'You may submit scores'}).value == 'You may submit scores'


def test_persistence_independently_refuses_year_unknown_deadlines():
    from opportunity_agent.v2.research.fact_store import persist_verified_page
    page,target,task = objects(4)
    response = fact(page,target,field='deadline',value='February 1',
        quote='The application deadline is February 1.',date_text='February 1',
        date_year_status='unknown',deadline_kind='final',intake='',intake_quote='')
    proof,_ = verify_fact(response,target,page,task.requested_fields,task.as_of)
    with pytest.raises(ValueError,match='independent persistence verification'):
        asyncio.run(persist_verified_page(None,target,page,[{**response.model_dump(),'provenance':proof}]))


@pytest.mark.parametrize('value', ['February 30', 'tomorrow', '2027-02-01'])
def test_unknown_year_not_a_validation_bypass(value):
    page,target,task = objects(4)
    page['text'] = '# '+target.program+'\nApplication deadline: '+value+'.'
    response = fact(page,target,field='deadline',value=value,quote='Application deadline: '+value+'.',
        date_text=value,date_year_status='unknown',deadline_kind='final',intake='',intake_quote='')
    assert verify_fact(response,target,page,task.requested_fields,task.as_of)[0] is None


def test_role_models_are_independent_and_override_shared_fast_model(monkeypatch):
    monkeypatch.setenv('RESEARCH_FAST_MODEL','fixture-fast')
    monkeypatch.setenv('RESEARCH_EXTRACT_MODEL','fixture-extract')
    monkeypatch.setenv('RESEARCH_PARSE_MODEL','fixture-parse')
    monkeypatch.delenv('RESEARCH_REPAIR_MODEL',raising=False)
    from opportunity_agent.v2.research.models import role_client
    assert role_client('extract',30).model == 'fixture-extract'
    assert role_client('parse',6).model == 'fixture-parse'
    assert role_client('discovery',10).model == 'fixture-fast'


def test_cache_key_scope_expiry_and_deep_copy():
    page,target,_ = objects()
    cache = ExtractionCache(capacity=1,ttl=30)
    key = cache.key(page,target,['gre_policy'],object(),{})
    cached = AdmissionExtraction(facts=[fact(page,target)])
    cache.put(key,cached)
    cache.get(key).facts.clear()
    assert len(cache.get(key).facts) == 1
    assert key != cache.key(page,target.model_copy(update={'home_unit':'Engineering'}),['gre_policy'],object(),{})
    cache.put('other',cached)
    assert cache.get(key) is None
    cache.ttl = 0
    assert cache.get('other') is None


def test_timeout_has_diagnostics_and_does_not_create_evidence():
    class Slow(RecordedModel):
        def generate_structured(self,*args,**kwargs):
            time.sleep(.05)
            return AdmissionExtraction()
    async def run():
        page,target,task = objects()
        service,result = ResearchService(None,llm=Slow([])),ResearchResult()
        service._deadline = asyncio.get_running_loop().time()+.01
        with pytest.raises(TimeoutError):
            await service._accept_page(task,result,target,page)
        assert result.diagnostics['extraction_attempts'] and not result.programs
    asyncio.run(run())


def test_html_table_rows_preserve_field_value_association():
    from opportunity_agent.v2.research.web import ResearchTextParser
    parser = ResearchTextParser()
    parser.feed('<main><h1>MSc in Computer Science</h1><table><tr><th>Field</th><th>Policy</th></tr>'
                '<tr><td>GRE</td><td>Not required</td></tr></table></main>')
    assert '| GRE | Not required' in parser.get_text()
    assert '# MSc in Computer Science' in parser.get_text()


def test_service_cache_skips_model_and_rechecks_expired_evidence(monkeypatch):
    from opportunity_agent.llm_client import LLMClient
    from opportunity_agent.v2.research import extraction
    monkeypatch.setattr(extraction,'CACHE',ExtractionCache())
    async def run():
        page,target,task = objects()
        answer = AdmissionExtraction(facts=[fact(page,target,field='deadline',value='2027-01-15',
            quote='Application deadline: January 15, 2027.',date_text='January 15, 2027',deadline_kind='final')])
        calls = []
        model = LLMClient(api_key='offline-fixture')
        def generate(*args,**kwargs):
            calls.append(kwargs)
            return answer
        monkeypatch.setattr(model,'generate_structured',generate)
        service = ResearchService(None,llm=model)
        for as_of in (date(2026,10,11),date(2026,10,11),date(2027,2,1)):
            task.as_of = as_of
            result = ResearchResult()
            await service._accept_page(task,result,target,page)
        assert len(calls) == 1
        assert result.diagnostics['extraction_attempts'][0]['cache_hit']
        assert not result.programs
        assert result.diagnostics['web_field_rejections'][0]['reason'] == 'expired_deadline'
    asyncio.run(run())


def test_semantic_provenance_database_roundtrip_and_tamper_rejection():
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from opportunity_agent.v2.db.base import Base
    from opportunity_agent.v2.research.fact_store import persist_verified_page
    from opportunity_agent.v2.research.catalog import ResearchCatalog
    async def run():
        page,target,task = objects(1)
        answer = fact(page,target,field='deadline',value='2027-01-15',
            quote='Applications must reach us by the fifteenth of January, 2027.',
            date_text='the fifteenth of January, 2027',deadline_kind='final')
        proof,reason = verify_fact(answer,target,page,task.requested_fields,task.as_of)
        assert not reason
        record = {**answer.model_dump(),'provenance':proof}
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine,expire_on_commit=False)() as session:
                stored = await persist_verified_page(session,target,page,[record])
                await session.commit()
                from opportunity_agent.v2.db.models import ResearchProgram
                row = await session.get(ResearchProgram,stored['program_id'])
                result = await ResearchCatalog(session).result(row,task.as_of)
                assert result.deadline == date(2027,1,15)
                assert result.evidence[0].extraction_provenance['fact']['date_text'] == answer.date_text
                assert result.evidence[0].temporal_scope == 'explicit_intake'
                with pytest.raises(ValueError,match='independent persistence verification'):
                    await persist_verified_page(session,target,page,[{**record,'value':'2027-01-16'}])
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_cross_page_different_home_units_are_not_merged():
    async def run():
        page,target,task = objects()
        result = ResearchResult()
        for unit in ('School of Engineering','School of Science'):
            source = {**page,'text':page['text']+'\nHome Unit: '+unit}
            response = fact(source,target,home_unit=unit)
            await ResearchService(None,llm=RecordedModel([response]))._accept_page(task,result,target,source)
        assert result.programs[0].gre_policy == 'unknown'
        assert all(f.verification_status == 'conflicting' for f in result.programs[0].facts)
        assert result.missing_items[-1]['reason'] == 'ambiguous_program_or_home_unit'
    asyncio.run(run())
