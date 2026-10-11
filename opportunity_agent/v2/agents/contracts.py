"""Shared, serialisable contracts for the V2.2 orchestration pipeline.

These models deliberately describe *results* and control decisions.  They do
not give the router a way to execute an agent or to write user state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PrivateAttr, model_validator
from ..services.memory_contracts import PreferenceSnapshot, UserMemoryMessage


AgentName = Literal["profile", "research", "planning"]
RouteMode = Literal["delegate", "direct_reply"]
CompletionStatus = Literal["PASS", "RETRY", "NEED_USER", "FAIL", "PARTIAL"]


class SuccessCriteria(BaseModel):
    """Only explicit, objectively checkable requirements belong here."""

    required_program_count: int | None = Field(default=None, ge=1)
    deadline_after: date | None = None
    deadline_before: date | None = None
    gre_policy: Literal["required", "not_required", "any"] = "any"
    citation_required: bool = False
    evidence_required: bool = False
    minimum_relevance_score: float = Field(default=0.7, ge=0, le=1)
    accepted_authorities: list[Literal["official", "trusted"]] = Field(
        default_factory=lambda: ["official", "trusted"]
    )
    needs_user_input: list[str] = Field(default_factory=list)
    memory_constraints: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bounds_and_evidence(self) -> "SuccessCriteria":
        if self.deadline_after and self.deadline_before and self.deadline_after > self.deadline_before:
            raise ValueError("deadline_after must not be later than deadline_before")
        if not self.accepted_authorities:
            raise ValueError("accepted_authorities must not be empty")
        if self.citation_required:
            self.evidence_required = True
        return self


class RouteDecision(BaseModel):
    """The Router's complete public output: a decision, never an answer."""

    model_config = ConfigDict(extra="forbid")

    mode: RouteMode
    agents: list[AgentName] = Field(default_factory=list)
    parallel: bool = False
    reason: str
    resolved_query: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def validate_mode(self) -> "RouteDecision":
        if self.mode == "direct_reply" and (self.agents or self.parallel):
            raise ValueError("direct_reply must not select agents or parallel execution")
        if self.mode == "delegate" and not self.agents:
            raise ValueError("delegate must select at least one agent")
        return self


class Evidence(BaseModel):
    source_id: str
    evidence_id: str = ""
    document_id: str | None = None
    chunk_id: str | None = None
    title: str = ""
    program_match: Literal["exact", "generic", "rejected", "unknown"] = "unknown"
    intake: str = ""
    content_hash: str = ""
    temporal_scope: Literal["legacy", "explicit_intake", "current_policy"] = "legacy"
    supports_fields: list[str] = Field(default_factory=list)
    relevance_method: str = "legacy"
    relevance_passed: bool | None = None
    raw_scores: dict[str, float] = Field(default_factory=dict)
    model_version: str = ""
    extraction_provenance: dict[str, Any] = Field(default_factory=dict)
    expires_at: date | None = None
    url: HttpUrl | None = None
    excerpt: str = ""
    authority: Literal["official", "trusted", "unknown", "rejected"] = "unknown"
    relevance_score: float | None = Field(default=None, ge=0, le=1)
    retrieved_at: date | None = None

    @model_validator(mode="after")
    def stable_id(self) -> "Evidence":
        if not self.evidence_id:
            import hashlib
            self.evidence_id = hashlib.sha256(
                f"{self.source_id}|{self.content_hash}|{self.chunk_id}|{self.excerpt}".encode()
            ).hexdigest()[:32]
        return self


def merge_evidence(items: list[Evidence]) -> list[Evidence]:
    """Union field bindings for a shared passage without changing input objects."""
    merged = {}
    for item in items:
        prior = merged.get(item.evidence_id)
        if prior is None:
            merged[item.evidence_id] = item
        else:
            preferred = prior if prior.relevance_passed is True and item.relevance_passed is not True else item
            merged[item.evidence_id] = preferred.model_copy(update={
                "supports_fields": list(dict.fromkeys([*prior.supports_fields, *item.supports_fields]))})
    return list(merged.values())


class ResearchFact(BaseModel):
    field: str
    value: Any = None
    qualifier: str = ""
    verification_status: Literal["verified", "unknown", "conflicting", "stale"] = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)


class ResearchFinding(BaseModel):
    finding_id: str
    program_id: str | None = None
    topic: str
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)


class ProgramResult(BaseModel):
    program_id: str | None = None
    required_fields: list[str] = Field(default_factory=list)
    country: str = ""
    required_country: str = ""
    university: str
    program: str
    degree: str = ""
    home_unit: str = ""
    intake: str = ""
    deadline: date | None = None
    gre_policy: Literal["required", "not_required", "optional", "not_accepted", "unknown"] = "unknown"
    facts: list[ResearchFact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def identity(self) -> tuple[str, str, str]:
        from ..research.identity import canonical_school, canonical_program, normalize_intake
        return (canonical_school(self.university), canonical_program(self.program), normalize_intake(self.intake).casefold())


class ProfileResult(BaseModel):
    facts: list[dict[str, Any]] = Field(default_factory=list)
    extracted_facts: list[dict[str, Any]] = Field(default_factory=list)
    accepted_facts: list[dict[str, Any]] = Field(default_factory=list)
    proposed_changes: list[dict[str, Any]] = Field(default_factory=list)
    preference_candidates: list[dict[str, Any]] = Field(default_factory=list)
    requires_confirmation: bool = False
    errors: list[str] = Field(default_factory=list)
    extraction_mode: str = "rule_only"
    extraction_route: str = "rule_only"
    extraction_route_reason: str = ""
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    projected_profile: dict[str, Any] = Field(default_factory=dict)
    derived_state: dict[str, Any] = Field(default_factory=dict)
    progress_updates: list[dict[str, Any]] = Field(default_factory=list)
    clarifications: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["complete", "no_change", "partial", "needs_confirmation", "failed"] = "no_change"


class ResearchResult(BaseModel):
    schema_version: str = "2.3"
    task_id: str = ""
    route_history: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[ResearchFinding] = Field(default_factory=list)
    missing_items: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    programs: list[ProgramResult] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    route: Literal["sql", "rag", "hybrid", "mcp_web", "stub"] = "stub"
    status: Literal["complete", "partial", "no_results", "failed"] = "no_results"


def research_revision(result: ResearchResult | None) -> str | None:
    """Stable content revision used to detect plans built from stale evidence."""
    if result is None:
        return None
    payload = {
        "programs": [item.model_dump(mode="json") for item in result.programs],
        "findings": [item.model_dump(mode="json") for item in result.findings],
        "evidence": [item.model_dump(mode="json") for item in result.evidence],
        "missing_items": result.missing_items,
        "status": result.status,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PlanResult(BaseModel):
    schema_version: str = "2.4"
    plan_kind: Literal["roadmap", "advice"] = "roadmap"
    article_markdown: str = ""
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    roadmap: dict[str, Any] = Field(default_factory=dict)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    input_versions: dict[str, Any] = Field(default_factory=dict)
    generation_mode: Literal["qwen", "rule_fallback"] = "rule_fallback"
    error: str | None = None
    status: Literal["complete", "no_plan"] = "no_plan"


class MissingTask(BaseModel):
    agent: AgentName
    reason: str
    required_count: int | None = Field(default=None, ge=1)
    excluded_programs: list[tuple[str, str, str]] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class CompletionResult(BaseModel):
    status: CompletionStatus
    missing_tasks: list[MissingTask] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class AgentEvent(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentResultRecord(BaseModel):
    """Immutable trace entry; aggregated result fields remain the current view."""

    round_id: int
    agent: AgentName
    result: dict[str, Any]


class AgentFailure(BaseModel):
    round_id: int
    agent: AgentName
    error: str


class ExecutionState(BaseModel):
    _event_queue: Any = PrivateAttr(default=None)
    _execution_deadline: float | None = PrivateAttr(default=None)
    _execution_scope: Any = PrivateAttr(default=None)
    _execution_started: float | None = PrivateAttr(default=None)
    user_id: str
    conversation_id: str
    run_id: str
    request_id: str
    message: str
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    profile_payload: dict[str, Any] = Field(default_factory=dict)
    profile_version: int = 1
    profile_facts: list[dict[str, Any]] = Field(default_factory=list)
    applications: list[dict[str, Any]] = Field(default_factory=list)
    current_plan: dict[str, Any] = Field(default_factory=dict)
    current_plan_version: int = 0
    current_tasks: list[dict[str, Any]] = Field(default_factory=list)
    memory: dict[str, Any] = Field(default_factory=dict)
    preference_memory: PreferenceSnapshot = Field(default_factory=PreferenceSnapshot)
    turn_preferences: list[dict[str, Any]] = Field(default_factory=list)
    preference_versions: dict[str, int] = Field(default_factory=dict)
    user_messages: list[UserMemoryMessage] = Field(default_factory=list)
    consolidation_input: dict[str, Any] | None = None
    success_criteria: SuccessCriteria | None = None
    route_decision: RouteDecision | None = None
    routing_diagnostics: dict[str, Any] = Field(default_factory=dict)
    profile_result: ProfileResult | None = None
    research_result: ResearchResult | None = None
    plan_result: PlanResult | None = None
    completion: CompletionResult | None = None
    round_id: int = 0
    result_history: list[AgentResultRecord] = Field(default_factory=list)
    agent_failures: list[AgentFailure] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    answer: str = ""
    proposals: list[dict[str, Any]] = Field(default_factory=list)

    def add_event(self, event_type: str, **payload: Any) -> None:
        self.events.append(AgentEvent(type=event_type, payload=payload))
        if self._event_queue is not None:
            self._event_queue.put_nowait((event_type, payload))

    def add_result(self, agent: AgentName, result: BaseModel) -> None:
        self.result_history.append(AgentResultRecord(
            round_id=self.round_id,
            agent=agent,
            result=result.model_dump(mode="json"),
        ))

    def add_failure(self, agent: AgentName, error: str) -> None:
        self.agent_failures.append(AgentFailure(round_id=self.round_id, agent=agent, error=error))

    def serialise(self) -> dict[str, Any]:
        """JSON-compatible state used by AgentRun.graph_state and the API."""
        return self.model_dump(mode="json")
