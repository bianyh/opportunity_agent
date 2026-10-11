"""Deterministic deadline/GRE extraction: one date grammar, intake-cycle years."""
import pytest

from opportunity_agent.official_research import classify_program_page
from opportunity_agent.v2.research.normalization import (
    _extract_dates_from_text, date_mentions, keyword_facts, supported_value)

# Excerpts of the live pages behind the failing runs (2026-10).
GATECH = ("Admission Requirements Applicants to the MSCS program are evaluated holistically. "
          "Effective January 2026, the TOEFL scale will change to 1-6, and our new requirement will be 5.0. "
          "A GRE score is not required for admission to the MSCS program. "
          "The application deadline is Feb. 1 for admission to the following fall semester. "
          "© 2026 Georgia Institute of Technology")
UCSD_MDS = ("Program Start Dates and Application Deadlines Winter 2027 Application Opens: August 12, 2026 "
            "Final Deadline/Close: November 4, 2026 Program Starts: January, 4, 2027 Fall 2027 Application "
            "Opens: August 12, 2026 Final Deadline/Close: July 28, 2027 Program Starts: September 20, 2027. "
            "Is the GRE required to apply for this program? No, the GRE exam is not required to apply to the "
            "Master of Data Science program.")


@pytest.mark.parametrize("text,intake,expected", [
    ("Application deadline: 2027-01-15", "", "2027-01-15"),
    ("Deadline: February 1, 2027", "", "2027-02-01"),
    ("Deadline: Feb. 1st, 2027", "", "2027-02-01"),
    ("Deadline: 1 February 2027", "", "2027-02-01"),
    ("Deadline: 2/1/2027", "", "2027-02-01"),
    ("申请截止日期：2027年2月1日", "", "2027-02-01"),
    # Year-less dates resolve from the requested cycle, not today or a copyright year.
    ("The deadline is Feb. 1.", "2027 Fall", "2027-02-01"),
    ("The deadline is December 15.", "2027 Fall", "2026-12-15"),
    ("The deadline is 15 January.", "2027 Fall", "2027-01-15"),
    ("The deadline is October 1.", "2027 Spring", "2026-10-01"),
    ("截止日期为12月15日", "2027 Fall", "2026-12-15"),
])
def test_date_grammar(text, intake, expected):
    assert _extract_dates_from_text(text, intake) == [expected]


def test_yearless_date_needs_a_season():
    # A year-only intake cannot decide whether "Dec. 15" is in 2026 or 2027.
    assert date_mentions("Deadline: Dec. 15", "2027")[0][2] is None
    assert date_mentions("Deadline: Dec. 15", "")[0][2] is None


def test_words_are_not_dates():
    assert _extract_dates_from_text("Documents may be submitted by email in March 2027.", "2027 Fall") == []


def test_supported_value_binds_yearless_quote_to_intake():
    quote = "The application deadline is Feb. 1 for admission to the following fall semester."
    assert supported_value("deadline", "2027-02-01", quote, "2027 Fall")
    assert not supported_value("deadline", "2026-02-01", quote, "2027 Fall")
    assert not supported_value("deadline", "2027-02-01", quote)  # no cycle, no guess
    assert not supported_value("deadline", "2027-02-01", "Classes start Feb. 1.", "2027 Fall")


def test_gre_policy_classification():
    assert supported_value("gre_policy", "not_required", "The GRE is no longer required.")
    assert not supported_value("gre_policy", "required", "The GRE is no longer required.")
    assert not supported_value("gre_policy", "required", "Progress reports are required.")


def test_keyword_facts_gatech():
    facts = {f["field"]: f for f in keyword_facts(GATECH, ["deadline", "gre_policy"], "2027 Fall")}
    assert facts["deadline"]["value"] == "2027-02-01"
    assert facts["gre_policy"]["value"] == "not_required"
    for fact in facts.values():
        assert fact["quote"] in GATECH  # exact substring, required for evidence


def test_keyword_facts_skip_questions_and_ambiguity():
    facts = {f["field"]: f["value"] for f in keyword_facts(UCSD_MDS, ["deadline", "gre_policy"], "2027 Fall")}
    # The question "Is the GRE required…?" must not count as a 'required' statement.
    assert facts.get("gre_policy") == "not_required"
    # Winter and Fall deadlines share one run-on block: left for the model, not guessed.
    assert "deadline" not in facts


def test_keyword_facts_prefer_regular_over_priority():
    text = "Priority deadline: December 1, 2026. Final application deadline: January 15, 2027."
    assert keyword_facts(text, ["deadline"], "2027 Fall") == [
        {"field": "deadline", "value": "2027-01-15", "quote": "Final application deadline: January 15, 2027."}]


def test_keyword_facts_conflicting_regular_deadlines_defer_to_model():
    text = "Domestic applicants: deadline January 15, 2027. International applicants: deadline December 15, 2026."
    assert keyword_facts(text, ["deadline"], "2027 Fall") == []


def test_keyword_facts_ignore_other_degrees_and_cycles():
    text = ("PhD applications are due December 1, 2026. The MS application deadline is February 1, 2027. "
            "Spring 2027 applications close October 1, 2026.")
    assert [f["value"] for f in keyword_facts(text, ["deadline"], "2027 Fall")] == ["2027-02-01"]


class NoModel:
    """Fails the test if the model is called; keyword pass must cover the fields."""
    enabled = True
    def generate_structured(self, *args, **kwargs):
        raise AssertionError("model must not be called")


def _accept(page_text, fields=("deadline", "gre_policy"), llm=None, **kwargs):
    import asyncio
    from datetime import date
    from types import SimpleNamespace
    from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult, SuccessCriteria
    from opportunity_agent.v2.research.service import ResearchService
    from opportunity_agent.v2.research.task import parse_task
    task = parse_task(SimpleNamespace(message="Georgia Tech MSCS 2027 Fall GRE 和截止日期",
                                      success_criteria=SuccessCriteria(), missing_task=None))
    task.as_of, task.requested_fields = date(2026, 10, 10), list(fields)
    result = ResearchResult()
    target = ProgramResult(university="Georgia Institute of Technology",
                           program="Master of Science in Computer Science", intake="2027 Fall")
    page = {"url": "https://www.cc.gatech.edu/ms-computer-science-admission-requirements",
            "title": "MS Computer Science Admission Requirements", "text": page_text}
    asyncio.run(ResearchService(None, llm=llm or NoModel())._accept_page(task, result, target, page, **kwargs))
    return result


@pytest.fixture
def no_persist(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERSIST_FACTS", "0")
    monkeypatch.setenv("RESEARCH_QUEUE_INGEST", "0")
    monkeypatch.setenv("RESEARCH_KEYWORD_EXTRACTION", "1")


def test_accept_page_keyword_path_skips_model(no_persist):
    from datetime import date
    result = _accept(GATECH)
    program = result.programs[0]
    assert program.deadline == date(2027, 2, 1) and program.gre_policy == "not_required"
    assert not result.diagnostics.get("extraction_attempts")
    assert all(e.temporal_scope == "current_policy" for e in program.evidence)


def test_accept_page_keyword_path_runs_with_open_circuit(no_persist):
    from opportunity_agent.v2.research.repair import ToolFailure
    result = _accept(GATECH, llm_blocked="EXTRACTION_CIRCUIT_OPEN")
    assert result.programs[0].gre_policy == "not_required"
    with pytest.raises(ToolFailure, match="EXTRACTION_CIRCUIT_OPEN"):
        import opportunity_agent.v2.research.service as service_module
        service = service_module.ResearchService(None, llm=NoModel())
        service._repair_active = True
        import asyncio
        from types import SimpleNamespace
        from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult, SuccessCriteria
        from opportunity_agent.v2.research.task import parse_task
        task = parse_task(SimpleNamespace(message="Georgia Tech MSCS 2027 Fall GRE", success_criteria=SuccessCriteria(), missing_task=None))
        task.requested_fields = ["deadline", "gre_policy"]
        asyncio.run(service._accept_page(task, ResearchResult(),
            ProgramResult(university="Georgia Institute of Technology", program="Master of Science in Computer Science", intake="2027 Fall"),
            {"url": "https://www.cc.gatech.edu/x", "title": "MS Computer Science", "text": "MSCS program overview."},
            llm_blocked="EXTRACTION_CIRCUIT_OPEN"))


def test_accept_page_keyword_path_rejects_expired_and_wrong_cycle(no_persist):
    expired = _accept("MSCS. The application deadline is January 15, 2026. GRE is not required.", llm=None)
    assert expired.programs[0].deadline is None
    assert any(r["reason"] == "expired_deadline" for r in expired.diagnostics["web_field_rejections"])
    wrong = _accept("MSCS admissions for Fall 2026. The deadline is Feb. 1. GRE is not required.")
    assert not wrong.programs  # explicit other cycle is rejected before any extraction


def test_data_science_page_is_not_cse():
    match, _, _ = classify_program_page(
        "Master of Science in Computer Science and Engineering", "Admissions",
        "https://mds.ucsd.edu/admissions/index.html",
        "Degree Type: Master of Data Science. Computer Science and Engineering department news. " + UCSD_MDS)
    assert match == "rejected"


def test_mscs_page_still_exact():
    match, _, _ = classify_program_page("Master of Science in Computer Science", "MS Computer Science Admission Requirements",
        "https://www.cc.gatech.edu/ms-computer-science-admission-requirements", GATECH)
    assert match == "exact"
