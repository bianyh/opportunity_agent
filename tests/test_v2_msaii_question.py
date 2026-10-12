"""Regressions for the user's original spelling, without live API calls."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult, SuccessCriteria
from opportunity_agent.v2.agents.orchestrator import HeuristicGoalParser, LLMGoalParser, _extract_gre_policy
from opportunity_agent.v2.research.extraction import AdmissionFact, AdmissionExtraction, bind_local_intake, blocks, candidate_blocks, verify_fact
from opportunity_agent.v2.research.identity import normalize_intake
from opportunity_agent.v2.research.program_directory import PROGRAM_DIRECTORY
from opportunity_agent.v2.research.service import ResearchService
from opportunity_agent.v2.research.task import intake_matches, parse_task
from opportunity_agent.v2.research.web import OfficialWeb
from opportunity_agent.llm_client import LLMClient

QUESTION = "cmu MSAll 2027Fall的申请截止日期和GRE要求是什么"
NAME = PROGRAM_DIRECTORY[0]["program"]
URL = PROGRAM_DIRECTORY[0]["urls"][0]


def request(query):
    return SimpleNamespace(message=query, success_criteria=None, missing_task=None)


def test_original_question_resolves_school_local_identity_and_intake():
    task = parse_task(request(QUESTION))
    assert task.entities.program == NAME
    assert task.entities.intake == "2027 Fall"
    assert task.entities.targets[0].program == NAME
    assert task.requested_fields == ["deadline", "gre_policy"]
    assert task.routing_diagnostics["entity_corrections"][0]["input"] == "MSAll"
    assert _extract_gre_policy(QUESTION) == "any"


@pytest.mark.parametrize("alias", ["MSAII", "MSAll", "MSAlI", "MSA11"])
def test_visual_alias_is_school_local(alias):
    assert parse_task(request(f"CMU {alias} GRE")).entities.program == NAME
    if alias != "MSAII":
        assert parse_task(request(f"UIUC {alias} GRE")).entities.program != NAME


@pytest.mark.parametrize("raw,normalized", [
    ("2027Fall", "2027 Fall"), ("Fall2027", "2027 Fall"),
    ("2027Spring", "2027 Spring"), ("2027年秋季", "2027 Fall"),
    ("春季2027", "2027 Spring"), ("2027", "2027"),
])
def test_compact_intake_keeps_season(raw, normalized):
    assert normalize_intake(raw) == normalized
    assert parse_task(request(f"CMU MSAII {raw} deadline")).entities.intake == normalized
    assert not intake_matches("2027Fall", "2027 Spring")


@pytest.mark.parametrize("query,policy", [
    (QUESTION, "any"), ("CMU MSAII 是否需要 GRE？", "any"),
    ("Is GRE required for CMU MSAII?", "any"),
    ("帮我找必须提交 GRE 的项目", "required"),
    ("找三个不要求 GRE 的项目", "not_required"),
])
def test_gre_question_not_a_selection_filter(query, policy):
    assert _extract_gre_policy(query) == policy


def test_semantic_goal_parser_cannot_turn_policy_question_into_filter():
    def completion(**kwargs):
        return '{"gre_policy":"required","evidence_required":true}'
    parser = LLMGoalParser(LLMClient(completion_fn=completion))
    assert asyncio.run(parser.parse(QUESTION)).gre_policy == "any"


def test_distinct_project_hosts_with_same_path_survive_ranking():
    wrong = "https://www.lti.cmu.edu/apply-msaii-program"
    assert set(OfficialWeb()._rank([wrong, URL], "MSAII admissions")) == {wrong, URL}


def test_real_navigation_before_guessed_paths():
    class RecordedNavigation(OfficialWeb):
        async def _sitemap_urls(self, domains, query=""):
            return ["https://msaii.cs.cmu.edu/about-msaii-program"]

        async def _search_engine(self, query, domains):
            return []

    result = asyncio.run(RecordedNavigation().search(f"CMU {NAME} 2027 Fall deadline GRE", ["cmu.edu"]))
    assert result["results"][0]["url"] == URL
    assert result["results"][1]["url"].endswith("/about-msaii-program")
    unrelated = asyncio.run(RecordedNavigation().search("UIUC MSAII", ["illinois.edu"]))
    assert all("cmu.edu" not in r["url"] for r in unrelated["results"])


def test_known_project_discovery_uses_robots_and_real_home_links_only():
    class RecordedSite(OfficialWeb):
        def __init__(self):
            super().__init__()
            self.fetched = []
        async def _fetch(self, url, domains, **kwargs):
            self.fetched.append(url)
            body = b'User-agent: *\nAllow: /' if url.endswith('robots.txt') else b'<a href="/apply-msaii-program">Apply</a>'
            return url, 'text/html', body
    async def run():
        web = RecordedSite()
        result = await web.search("CMU MSAII deadline", ['cmu.edu'])
        assert result['results'][0]['url'] == URL
        assert set(web.fetched) == {'https://msaii.cs.cmu.edu/robots.txt', 'https://msaii.cs.cmu.edu/'}
    asyncio.run(run())


def test_source_full_name_verifies_both_fields_and_stops_extra_reads(monkeypatch):
    """Recorded official excerpts; production still retrieves the live page."""
    monkeypatch.setenv("RESEARCH_PERSIST_FACTS", "0")
    page = {"url": URL, "title": f"Apply to the MSAII Program | Carnegie Mellon University - {NAME}",
            "text": "# Apply to the MSAII Program\n## How To Apply\n"
                    "The application system for entry in Fall 2027 is now open! "
                    "The deadline to apply is December 9, 2026.\n"
                    "GRE scores: GRE is required. Our Institution Code is 2074; Department Code is 0402."}
    b = blocks(page["text"], page["title"])[0]
    base = dict(block_id=b["id"], university="CMU", program=NAME, program_quote=NAME,
                degree="masters", intake="2027 Fall", intake_quote="Fall 2027", scope="all")
    facts = [AdmissionFact(**base, field="deadline", value="2026-12-09",
                 quote="The deadline to apply is December 9, 2026.", date_text="December 9, 2026", deadline_kind="final"),
             AdmissionFact(**base, field="gre_policy", value="required",
                 quote="GRE scores: GRE is required.", gre_required=True)]
    class RecordedModel:
        enabled = True
        def generate_structured(self, schema, **kwargs):
            return AdmissionExtraction(facts=facts)

    class RecordedWeb:
        search_limit, page_limit = 2, 5
        search_calls, page_calls = 0, 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def search(self, query, domains):
            self.search_calls += 1
            return {"results": [{"url": URL}, {"url": "https://www.cmu.edu/irrelevant"}]}
        async def read(self, url, domains):
            self.page_calls += 1
            assert url == URL, "Research should stop once the requested fields are supported"
            return page

    async def run():
        task = parse_task(request(QUESTION))
        task.as_of = date(2026, 10, 12)
        target = ProgramResult(university="CMU", program=NAME, intake="2027 Fall")
        assert all(verify_fact(f, target, page, task.requested_fields, task.as_of)[0] for f in facts)
        result = ResearchResult()
        await ResearchService(None, llm=RecordedModel(), web_factory=RecordedWeb)._web(task, result, [])
        assert result.programs[0].deadline == date(2026, 12, 9)
        assert result.programs[0].gre_policy == "required"
        assert result.diagnostics["page_calls"] == 1
        assert all(e.extraction_provenance for e in result.programs[0].evidence)
    asyncio.run(run())


def test_cached_acronym_is_resolved_to_school_local_source_identity():
    task = parse_task(request(QUESTION))
    candidate = ProgramResult(university="CMU", program="MSAII", intake="2027 Fall")
    targets = ResearchService(None)._web_targets(task, ResearchResult(), [candidate])
    assert targets[0].program == NAME


def test_marketing_page_not_accepted_as_answer_to_policy_lookup():
    task = parse_task(request(QUESTION))
    target = ProgramResult(university="CMU", program=NAME, intake="2027 Fall")
    result = ResearchResult()
    page = {"url": "https://msaii.cs.cmu.edu/industrial-sponsors", "title": NAME,
            "text": "Our industrial sponsors collaborate on exciting artificial intelligence innovations."}
    ResearchService(None)._accept_official_passages(
        task, result, target, page, AdmissionExtraction(), "exact", "")
    assert not result.programs and not result.findings


def test_extract_only_requested_policy_blocks_not_language_validity():
    page = {"url": URL, "title": NAME,
            "text": "# " + NAME + "\nThe deadline to apply is December 9, 2026. GRE is required.\n"
                    "## English proficiency\nIf you have received a degree in the US, you may submit an expired "
                    "test score up to five years old. Application requirements: submit test scores."}
    selected = candidate_blocks(page, ProgramResult(university="CMU", program=NAME), ["deadline", "gre_policy"])
    assert len(selected) == 1
    assert "five years" not in selected[0]["text"]
    passages = ResearchService._relevant_passages(parse_task(request(QUESTION)), page, AdmissionExtraction())
    assert passages and all("five years" not in text for text, _ in passages)


@pytest.mark.parametrize("source,expected", [
    ("Entry in Fall 2027. GRE is required.", "2027 Fall"),
    ("GRE is required.", ""),
    ("Entry in Fall 2028. GRE is required.", ""),
    ("Fall 2027 and Fall 2028. GRE is required.", ""),
])
def test_missing_cycle_is_bound_only_to_unique_local_source_cycle(source, expected):
    page = {"url": URL, "title": NAME, "text": "# " + NAME + "\n" + source}
    source_blocks = blocks(page["text"], page["title"])
    fact = AdmissionFact(field="gre_policy", value="required", block_id=source_blocks[0]["id"],
        quote="GRE is required.", university="CMU", program=NAME, program_quote=NAME,
        degree="masters", scope="all", gre_required=True)
    target = ProgramResult(university="CMU", program=NAME, intake="2027 Fall")
    parsed = bind_local_intake(AdmissionExtraction(facts=[fact]), target, source_blocks)
    assert parsed.facts[0].intake == expected
    assert fact.intake == "", "Do not mutate model/test input"
    if expected:
        assert verify_fact(parsed.facts[0], target, page, ["gre_policy"], date(2026, 10, 12))[0]
