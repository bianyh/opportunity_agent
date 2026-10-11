"""Offline legacy baseline. Fixtures are synthetic, NOT real admissions advice.

Legacy primary extraction is measured without model fallback. Live semantic
accuracy must be reported separately; these numbers are not end-to-end accuracy.
"""
import json
import time
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

from opportunity_agent.v2.research.normalization import keyword_facts

FIXTURES = Path(__file__).resolve().parents[1] / 'tests/fixtures/research_extraction_stage1.json'


def metrics(rows):
    expected = sum(len(r['expected']) for r in rows)
    returned = sum(len(r['actual']) for r in rows)
    correct = sum(sum(v == r['expected'].get(k) for k, v in r['actual'].items()) for r in rows)
    return dict(correct=correct, returned=returned, available=expected,
                precision=correct / returned if returned else 0, coverage=correct / expected if expected else 0)


def baseline():
    rows = []
    for case in json.loads(FIXTURES.read_text(encoding='utf-8')):
        started = time.perf_counter()
        actual = {f['field']: f['value'] for f in keyword_facts(case['text'], ['deadline', 'gre_policy'], '2027 Fall')}
        rows.append(dict(id=case['id'], expected=case['expected'], actual=actual,
                         milliseconds=round((time.perf_counter()-started)*1000, 3)))
    return dict(mode='legacy_primary_regex_before_verification', metrics=metrics(rows), rows=rows)


async def live():
    from opportunity_agent.v2.research.service import ResearchService
    from opportunity_agent.v2.research.task import ResearchTaskSpec
    from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult
    os.environ['RESEARCH_PERSIST_FACTS'] = '0'
    os.environ['RESEARCH_QUEUE_INGEST'] = '0'
    if '--legacy-live' in sys.argv:
        from scripts.research_legacy_baseline import LegacyResearchService
        service = LegacyResearchService(None)
    else:
        service = ResearchService(None)
    if not service.llm.enabled:
        raise RuntimeError('Configure the extraction LLM before running --live')
    rows = []
    for case in json.loads(FIXTURES.read_text(encoding='utf-8')):
        if '--case' in sys.argv and case['id'] != sys.argv[sys.argv.index('--case')+1]:
            continue
        result = ResearchResult()
        print('CASE ' + case['id'], flush=True)
        target = ProgramResult(university=case['university'], program=case['program'], intake='2027 Fall')
        task = ResearchTaskSpec(query='deadline and GRE', requested_fields=['deadline', 'gre_policy'], as_of=date(2026, 10, 11))
        page = dict(url=case['url'], title=case['program'], text=case['text'])
        started = time.perf_counter()
        error = None
        try:
            async with asyncio.timeout(40):
                await service._accept_page(task, result, target, page)
        except Exception as exc:
            error = type(exc).__name__
        actual = {f.field: f.value for p in result.programs for f in p.facts if f.verification_status == 'verified'}
        row = dict(id=case['id'], expected=case['expected'], actual=actual,
                   partial_facts=[f.model_dump(mode='json') for p in result.programs for f in p.facts
                                  if f.verification_status == 'unknown'],
                   seconds=round(time.perf_counter()-started, 3), error=error,
                   rejections=result.diagnostics.get('web_field_rejections', []),
                   attempts=result.diagnostics.get('extraction_attempts', []))
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    import statistics
    print(json.dumps(dict(mode='legacy_live' if '--legacy-live' in sys.argv else 'semantic_live',
                          model=service.llm.model, effort=service.llm.reasoning_effort,
                          metrics=metrics(rows), timeouts=sum(r['error']=='TimeoutError' for r in rows),
                          total_seconds=sum(r['seconds'] for r in rows),
                          median_seconds=statistics.median(r['seconds'] for r in rows)), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    if '--live' in sys.argv or '--legacy-live' in sys.argv:
        asyncio.run(live())
    else:
        print(json.dumps(baseline(), ensure_ascii=False, indent=2))
