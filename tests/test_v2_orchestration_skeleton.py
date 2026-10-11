from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.contracts import (
    Evidence,
    ExecutionState,
    PlanResult,
    ProfileResult,
    ProgramResult,
    ResearchResult,
    RouteDecision,
    SuccessCriteria,
    research_revision,
)
from opportunity_agent.v2.agents.orchestrator import (
    CustomOrchestrator, DeterministicSynthesizer, HeuristicGoalParser, HeuristicRouter, LLMGoalParser, LLMRouter,
    LLMSynthesizer, RouterUnavailable,
)


def run(coro):
    return asyncio.run(coro)


def state(message: str) -> ExecutionState:
    return ExecutionState(
        user_id="user", conversation_id="conversation", run_id="run", request_id="request", message=message,
    )


class RecordingAgents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int | None]] = []
        self.research_round = 0

    async def execute(self, agent, execution_state, missing_task=None):
        self.calls.append((agent, execution_state.round_id, missing_task.required_count if missing_task else None))
        if agent != "research":
            raise AssertionError(f"unexpected domain agent: {agent}")
        self.research_round += 1
        evidence = Evidence(source_id=f"source-{self.research_round}", url="https://example.edu/admissions",
                            authority="official", relevance_score=0.95, retrieved_at=date(2026, 1, 1))
        if self.research_round == 1:
            programs = [ProgramResult(university=f"U{index}", program="MSCS", gre_policy="optional", evidence=[evidence]) for index in range(1, 5)]
        else:
            programs = [ProgramResult(university="U5", program="MSCS", gre_policy="optional", evidence=[evidence])]
        return ResearchResult(programs=programs, route="stub", status="complete")


def test_direct_reply_skips_domain_agents_and_completion_checker():
    agents = RecordingAgents()
    result = run(CustomOrchestrator(agent_client=agents, router=HeuristicRouter(), synthesizer=DeterministicSynthesizer()).run(state("你好，谢谢你")))

    assert result.route_decision is not None
    assert result.route_decision.mode == "direct_reply"
    assert agents.calls == []
    assert result.completion is None
    assert result.answer
    assert [event.type for event in result.events] == ["run_started", "goal_parse_started", "goal_parsed",
        "routing_started", "route_selected", "synthesis_started", "final_answer"]


def test_llm_router_returns_validated_decision_instead_of_keyword_route():
    recorded = {}

    def completion(payload):
        recorded.update(payload)
        return '{"mode":"delegate","agents":["research"],"parallel":false,"reason":"needs official evidence"}'

    router = LLMRouter(LLMClient(completion_fn=completion))
    decision = run(router.route(state("今年 CMU MSCS 的 GRE 要求是什么？")))

    assert decision.mode == "delegate"
    assert decision.agents == ["research"]
    prompt = recorded["messages"][0]["content"]
    assert "sole job is to select the next path" in prompt
    assert "There are exactly two modes" in prompt
    assert "mode=delegate" in prompt


def test_default_llm_router_never_silently_falls_back_when_unconfigured(monkeypatch):
    monkeypatch.setenv("OPPORTUNITY_AGENT_DISABLE_DOTENV", "1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("MODELSCOPE_API_KEY", raising=False)
    assert LLMRouter().client.timeout_seconds == 60
    with pytest.raises(RouterUnavailable, match="not configured"):
        run(LLMRouter().route(state("帮我查 CMU 截止日期")))


def test_llm_synthesizer_receives_user_message_and_structured_profile_result():
    recorded = {}

    def completion(payload):
        recorded.update(payload)
        return "我识别到你的托福成绩为 105 分，已生成待确认的画像更新。"

    execution = state("我的托福考了105分")
    execution.profile_result = ProfileResult(
        facts=[{"field": "toefl_score", "normalized_value": 105}],
        proposals=[{"type": "profile.change"}], status="complete",
    )
    answer = run(LLMSynthesizer(LLMClient(completion_fn=completion)).synthesize(execution))

    assert answer == "我识别到你的托福成绩为 105 分，已生成待确认的画像更新。"
    assert "candidate changes" in recorded["messages"][0]["content"]
    assert "toefl_score" in recorded["messages"][1]["content"]


def test_research_repair_preserves_first_four_results_and_only_retries_research():
    agents = RecordingAgents()
    result = run(CustomOrchestrator(agent_client=agents, router=HeuristicRouter(), synthesizer=DeterministicSynthesizer()).run(state("帮我找5个不要求 GRE 的项目")))

    assert result.completion is not None
    assert result.completion.status == "PASS"
    assert result.research_result is not None
    assert {item.university for item in result.research_result.programs} == {"U1", "U2", "U3", "U4", "U5"}
    assert agents.calls == [("research", 0, None), ("research", 1, 1)]
    assert any(event.type == "repair_round_started" for event in result.events)


def test_duplicate_research_results_do_not_count_toward_missing_program_requirement():
    class DuplicateAgents:
        async def execute(self, agent, execution_state, missing_task=None):
            evidence = Evidence(source_id="u1", url="https://example.edu/u1", authority="official", relevance_score=0.9)
            return ResearchResult(programs=[ProgramResult(university="U1", program="MSCS", gre_policy="optional", evidence=[evidence])],
                                  route="stub", status="complete")

    result = run(CustomOrchestrator(agent_client=DuplicateAgents(), router=HeuristicRouter(), synthesizer=DeterministicSynthesizer(), max_rounds=2).run(state("找2个不要求 GRE 的项目")))

    assert result.completion is not None
    assert result.completion.status == "PARTIAL"
    assert result.research_result is not None
    assert len(result.research_result.programs) == 1
    assert "还需要 1 个合格项目" in result.answer


def test_heuristic_goal_parser_and_router_cover_fixed_orchestration_corpus():
    cases = json.loads((Path(__file__).parent / "fixtures" / "v2_orchestration_cases.json").read_text(encoding="utf-8"))
    parser = HeuristicGoalParser()
    router = HeuristicRouter()

    for case in cases:
        execution = state(case["message"])
        execution.success_criteria = run(parser.parse(execution.message))
        decision = run(router.route(execution, CustomOrchestrator._guard(execution.message)))
        assert decision.mode == case["mode"], case["id"]
        assert set(decision.agents) == set(case["agents"]), case["id"]
        expected = case["criteria"]
        if expected is None:
            assert execution.success_criteria is None, case["id"]
            continue
        assert execution.success_criteria is not None, case["id"]
        for key, value in expected.items():
            actual = getattr(execution.success_criteria, key)
            if key.startswith("deadline_") and value is not None:
                actual = actual.isoformat()
            elif key == "needs_user_input":
                actual = bool(actual)
            assert actual == value, f"{case['id']}: {key}"


def test_llm_goal_parser_uses_schema_and_keeps_subjective_request_out_of_criteria():
    recorded = {}

    def completion(payload):
        recorded.update(payload)
        return '{"required_program_count":5,"gre_policy":"not_required","evidence_required":true,"citation_required":true}'

    criteria = run(LLMGoalParser(LLMClient(completion_fn=completion)).parse("找 5 个 AI 很强、不要求 GRE 的项目，并附官网"))

    assert criteria is not None
    assert criteria.required_program_count == 5
    assert criteria.gre_policy == "not_required"
    assert criteria.evidence_required is True
    assert "Do not turn phrases like" in recorded["messages"][0]["content"]


def test_missing_or_low_relevance_evidence_does_not_count_and_returns_partial_after_budget():
    class WeakEvidenceAgents:
        async def execute(self, agent, execution_state, missing_task=None):
            assert agent == "research"
            weak = Evidence(source_id="weak", url="https://blog.example/claim", authority="unknown", relevance_score=0.2)
            program = ProgramResult(university="U1", program="MSCS", gre_policy="optional", evidence=[weak])
            return ResearchResult(programs=[program], route="stub", status="complete")

    result = run(CustomOrchestrator(
        agent_client=WeakEvidenceAgents(), router=HeuristicRouter(), synthesizer=DeterministicSynthesizer(), max_rounds=2,
    ).run(state("找一个不要求 GRE 的项目，并附官网来源")))

    assert result.completion is not None
    assert result.completion.status == "PARTIAL"
    assert len(result.result_history) == 2
    assert [entry.agent for entry in result.result_history] == ["research", "research"]


def test_research_query_without_count_retries_until_evidence_is_available():
    class EvidenceOnRepairAgents:
        calls = 0

        async def execute(self, agent, execution_state, missing_task=None):
            self.calls += 1
            if self.calls == 1:
                return ResearchResult(route="stub", status="no_results")
            evidence = Evidence(source_id="cmu", url="https://www.cmu.edu/admissions", authority="official", relevance_score=0.91)
            return ResearchResult(evidence=[evidence], route="stub", status="complete")

    agents = EvidenceOnRepairAgents()
    result = run(CustomOrchestrator(
        agent_client=agents, router=HeuristicRouter(), synthesizer=DeterministicSynthesizer(),
    ).run(state("CMU MSCS 的截止日期是什么？请给官网来源")))

    assert agents.calls == 2
    assert result.completion is not None
    assert result.completion.status == "PASS"
    assert result.research_result is not None
    assert result.research_result.evidence[0].source_id == "cmu"


def test_duplicate_repair_preserves_verified_first_result_instead_of_overwriting_it():
    class ConflictingDuplicateAgents:
        calls = 0

        async def execute(self, agent, execution_state, missing_task=None):
            self.calls += 1
            if self.calls == 1:
                official = Evidence(source_id="official", url="https://official.example/mit", authority="official", relevance_score=0.95)
                return ResearchResult(programs=[ProgramResult(university="MIT", program="MSCS", gre_policy="optional", deadline=date(2026, 12, 10), evidence=[official])], route="stub", status="complete")
            weak = Evidence(source_id="weak", url="https://blog.example/mit", authority="unknown", relevance_score=0.2)
            return ResearchResult(programs=[ProgramResult(university="MIT", program="MSCS", gre_policy="unknown", evidence=[weak])], route="stub", status="complete")

    result = run(CustomOrchestrator(
        agent_client=ConflictingDuplicateAgents(), router=HeuristicRouter(), synthesizer=DeterministicSynthesizer(), max_rounds=2,
    ).run(state("找2个不要求 GRE 的项目")))

    assert result.research_result is not None
    mit = result.research_result.programs[0]
    assert mit.deadline == date(2026, 12, 10)
    assert mit.gre_policy == "optional"
    assert {source.source_id for source in mit.evidence} == {"official", "weak"}


def test_ambiguous_deadline_requests_clarification_before_any_agent_call():
    class NeverCalledAgents:
        async def execute(self, agent, execution_state, missing_task=None):
            raise AssertionError("clarification must be requested before domain work")

    result = run(CustomOrchestrator(
        agent_client=NeverCalledAgents(), router=HeuristicRouter(), synthesizer=DeterministicSynthesizer(),
    ).run(state("找三个截止日期在12月以后、不要求 GRE 的项目")))

    assert result.completion is not None
    assert result.completion.status == "NEED_USER"
    assert "年份" in result.answer
    assert result.result_history == []


def test_domain_agent_failure_is_a_structured_fail_not_an_uncaught_run_error():
    class FailingResearchAgent:
        async def execute(self, agent, execution_state, missing_task=None):
            raise TimeoutError("upstream research service timed out")

    result = run(CustomOrchestrator(
        agent_client=FailingResearchAgent(), router=HeuristicRouter(), synthesizer=DeterministicSynthesizer(),
    ).run(state("查询 CMU MSCS 截止日期，并给官网来源")))

    assert result.completion is not None
    assert result.completion.status == "FAIL"
    assert result.agent_failures[0].agent == "research"
    assert any(event.type == "agent_failed" for event in result.events)
    assert "research 执行失败" in result.answer


def test_invalid_deadline_range_is_rejected_by_the_shared_contract():
    with pytest.raises(ValueError, match="deadline_after"):
        SuccessCriteria(deadline_after=date(2027, 1, 1), deadline_before=date(2026, 12, 1))


def test_planning_missing_task_retries_only_planning_and_preserves_result_history():
    class PlanningOnRepair:
        calls: list[tuple[str, int]] = []

        async def execute(self, agent, execution_state, missing_task=None):
            self.calls.append((agent, execution_state.round_id))
            assert agent == "planning"
            if execution_state.round_id == 0:
                return PlanResult(status="no_plan")
            return PlanResult(timeline=[{"month": "2026-10", "task": "准备文书"}], status="complete")

    agents = PlanningOnRepair()
    result = run(CustomOrchestrator(
        agent_client=agents, router=HeuristicRouter(), synthesizer=DeterministicSynthesizer(),
    ).run(state("请帮我制定申请时间线")))

    assert result.completion is not None
    assert result.completion.status == "PASS"
    assert agents.calls == [("planning", 0), ("planning", 1)]
    assert [entry.agent for entry in result.result_history] == ["planning", "planning"]


def test_completion_replans_when_research_changed_after_the_plan_was_built():
    research = ResearchResult(
        findings=[{"finding_id": "f1", "topic": "curriculum", "statement": "课程包含机器学习",
                   "evidence_ids": ["e1"]}],
        evidence=[Evidence(source_id="s1", evidence_id="e1", url="https://example.edu/program",
                           authority="official", relevance_score=0.9, relevance_passed=True)],
        route="rag", status="complete",
    )
    execution = state("查询项目课程并制定申请规划")
    execution.success_criteria = SuccessCriteria(evidence_required=True)
    execution.route_decision = RouteDecision(
        mode="delegate", agents=["research", "planning"], parallel=False, reason="test",
    )
    execution.research_result = research
    execution.plan_result = PlanResult(
        article_markdown="旧证据生成的规划", roadmap={"article": "旧证据生成的规划"},
        input_versions={"research_revision": "outdated"}, status="complete",
    )

    completion = CustomOrchestrator._check_completion(execution)

    assert research_revision(research) != "outdated"
    assert completion.status == "RETRY"
    assert [item.agent for item in completion.missing_tasks] == ["planning"]
