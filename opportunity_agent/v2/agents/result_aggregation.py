"""Pure result aggregation and approval-context construction."""
from __future__ import annotations

import json
from typing import Any

from .contracts import ExecutionState, PlanResult, ProfileResult, ProgramResult, ResearchResult, research_revision, merge_evidence


class ResultAggregator:
    def merge_profile(self, state: ExecutionState, incoming: ProfileResult) -> None:
        previous = state.profile_result
        if previous is None:
            state.profile_result = incoming
            return
        facts = {self._key(item): item for item in [*previous.facts, *incoming.facts]}
        progress = {self._key(item): item for item in [*previous.progress_updates, *incoming.progress_updates]}
        proposals = {self._key(item): item for item in [*previous.proposals, *incoming.proposals]}
        clarifications = {self._key(item): item for item in [*previous.clarifications, *incoming.clarifications]}
        state.profile_result = ProfileResult(
            facts=list(facts.values()), progress_updates=list(progress.values()),
            proposals=list(proposals.values()), clarifications=list(clarifications.values()),
            conflicts=[*previous.conflicts, *incoming.conflicts],
            extracted_facts=[*previous.extracted_facts, *incoming.extracted_facts],
            accepted_facts=[*previous.accepted_facts, *incoming.accepted_facts],
            proposed_changes=list(proposals.values()),
            preference_candidates=[*previous.preference_candidates, *incoming.preference_candidates],
            requires_confirmation=bool(proposals or previous.conflicts or incoming.conflicts),
            errors=list(dict.fromkeys([*previous.errors, *incoming.errors])),
            decisions=[*previous.decisions, *incoming.decisions], extraction_mode=incoming.extraction_mode,
            extraction_route=incoming.extraction_route, extraction_route_reason=incoming.extraction_route_reason,
            projected_profile=incoming.projected_profile or previous.projected_profile,
            derived_state=incoming.derived_state or previous.derived_state,
            status=incoming.status if incoming.status in {"partial", "failed"} else "complete" if proposals else incoming.status,
        )

    def merge_plan(self, state: ExecutionState, incoming: PlanResult) -> None:
        previous = state.plan_result
        if incoming.status != "complete":
            state.plan_result = previous if previous and previous.status == "complete" else incoming
            return
        # PlanningAgent already inherits persisted progress by stable_key.  A
        # fresh roadmap is therefore an authoritative replacement: unioning
        # old and new task sets would resurrect tasks intentionally removed by
        # replanning.  Advice is also a complete per-run result, but is never
        # persisted as a replacement plan.
        state.plan_result = incoming

    def merge_research(self, state: ExecutionState, incoming: ResearchResult) -> None:
        existing = state.research_result or ResearchResult()
        repair_history = {v["call_id"]: v for v in [*existing.diagnostics.get("repair_history", []),
            *incoming.diagnostics.get("repair_history", [])] if "call_id" in v}
        old_ledger, new_ledger = existing.diagnostics.get("tool_execution", {}), incoming.diagnostics.get("tool_execution", {})
        ledger = new_ledger if (new_ledger.get("tools_used", 0), new_ledger.get("decisions_used", 0)) >= (
            old_ledger.get("tools_used", 0), old_ledger.get("decisions_used", 0)) else old_ledger
        if ledger:
            calls = {v.get("call_id") or self._key(v): v for v in [*old_ledger.get("calls", []), *new_ledger.get("calls", [])]}
            ledger = {**ledger, "calls": list(calls.values())[-60:],
                "tools_used": max(old_ledger.get("tools_used", 0), new_ledger.get("tools_used", 0)),
                "decisions_used": max(old_ledger.get("decisions_used", 0), new_ledger.get("decisions_used", 0))}
        by_identity = {item.identity: item for item in existing.programs}
        for item in incoming.programs:
            prior = by_identity.get(item.identity)
            by_identity[item.identity] = item if prior is None else self._merge_program(prior, item)
        evidence = {item.evidence_id: item for item in merge_evidence([*existing.evidence, *incoming.evidence])}
        findings = {item.finding_id: item for item in [*existing.findings, *incoming.findings]}
        has_results = bool(by_identity or findings or evidence)
        status = incoming.status
        if has_results and status in {"no_results", "failed"}:
            status = existing.status if existing.status == "complete" else "partial"
        state.research_result = ResearchResult(
            programs=list(by_identity.values()), evidence=list(evidence.values()),
            route=incoming.route, status=status, task_id=incoming.task_id,
            findings=list(findings.values()), route_history=[*existing.route_history, *incoming.route_history],
            missing_items=incoming.missing_items, errors=[*existing.errors, *incoming.errors],
            diagnostics={**existing.diagnostics, **incoming.diagnostics,
                "repair_history": list(repair_history.values()),
                **({"tool_execution": ledger} if ledger else {}),
                "web_progress": {**existing.diagnostics.get("web_progress", {}),
                                 **incoming.diagnostics.get("web_progress", {})},
                "rounds": [*existing.diagnostics.get("rounds", [{k: v for k, v in existing.diagnostics.items() if k != "rounds"}]),
                           {k: v for k, v in incoming.diagnostics.items() if k != "rounds"}]},
        )

    def approval_proposals(self, state: ExecutionState) -> list[dict[str, Any]]:
        profile = []
        for item in state.profile_result.proposals if state.profile_result else []:
            if item["type"] == "preference.change":
                body = item["payload"]
                if any(p["key"] == body["key"] and p["value"] == body["value"] for p in state.turn_preferences):
                    continue
                item = {**item, "payload": {**body, "expected_version": state.preference_versions.get(body["key"], 0),
                    "source_conversation_id": state.conversation_id,
                    "source_message_id": state.user_messages[-1].message_id if state.user_messages else None}}
            profile.append(item)
        plan = state.plan_result
        if (state.completion and state.completion.status in {"FAIL", "PARTIAL", "RETRY"}) or (
            plan is not None and "research_revision" in plan.input_versions
            and plan.input_versions["research_revision"] != research_revision(state.research_result)
        ):
            return profile
        if (plan is None or plan.status != "complete" or plan.plan_kind != "roadmap"
                or not plan.roadmap):
            return profile
        plan_proposal = {"type": "plan.replace", "reason": "请确认新的申请计划。",
                         "payload": {"roadmap": plan.roadmap, "tasks": plan.tasks,
                                     "research_revision": plan.input_versions.get("research_revision"),
                                     "expected_version": state.current_plan_version,
                                     "expected_profile_version": state.profile_version +
                                     int(any(item["type"] == "profile.change" for item in profile))}}
        if profile:
            return [{"type": "change_set", "reason": "请一并确认画像、进度和基于新画像生成的计划。",
                     "payload": {"changes": [*profile, plan_proposal]}}]
        return [plan_proposal]

    @staticmethod
    def _key(item: dict[str, Any]) -> str:
        return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)

    @staticmethod
    def _merge_program(existing: ProgramResult, incoming: ProgramResult) -> ProgramResult:
        evidence = {item.evidence_id: item for item in merge_evidence([*existing.evidence, *incoming.evidence])}
        quality = lambda result: sum(2 if item.authority == "official" else 1
                                     for item in result.evidence if item.url and item.relevance_score is not None)
        primary, secondary = (incoming, existing) if quality(incoming) > quality(existing) else (existing, incoming)
        prior_facts = []
        for fact in existing.facts:
            old_sources = [evidence[eid] for eid in fact.evidence_ids if eid in evidence]
            replacements = [evidence[eid] for f in incoming.facts
                if f.field == fact.field and f.verification_status == "verified"
                for eid in f.evidence_ids if eid in evidence]
            superseded = bool(old_sources) and all(any(
                new.source_id == old.source_id and new.content_hash and old.content_hash
                and new.content_hash != old.content_hash and new.retrieved_at and old.retrieved_at
                and new.retrieved_at >= old.retrieved_at for new in replacements) for old in old_sources)
            prior_facts.append(fact.model_copy(update={"verification_status": "stale"}) if superseded else fact)
        facts = {self_key: fact for fact in [*prior_facts, *incoming.facts]
                 for self_key in [json.dumps(fact.model_dump(), sort_keys=True, default=str)]}
        values = {}
        for fact in facts.values():
            if fact.verification_status == "verified":
                values.setdefault(fact.field, set()).add(json.dumps(fact.value, sort_keys=True))
        conflicting = {field for field, items in values.items() if len(items) > 1}
        merged_facts = []
        for fact in facts.values():
            merged_facts.append(fact.model_copy(update={"verification_status": "conflicting"})
                                if fact.field in conflicting else fact)
        deadline = primary.deadline or secondary.deadline
        gre = primary.gre_policy if primary.gre_policy != "unknown" else secondary.gre_policy
        for fact in merged_facts:
            if fact.verification_status == "verified" and fact.field == "deadline":
                from datetime import date
                try:
                    deadline = date.fromisoformat(str(fact.value))
                except ValueError:
                    fact.verification_status = "unknown"
            if fact.verification_status == "verified" and fact.field == "gre_policy":
                if isinstance(fact.value, str) and fact.value in {"required", "optional", "not_required", "not_accepted"}:
                    gre = fact.value
                else:
                    fact.verification_status = "unknown"
        return ProgramResult(
            program_id=primary.program_id or secondary.program_id, facts=merged_facts,
            required_fields=list(dict.fromkeys([*existing.required_fields, *incoming.required_fields])),
            country="" if existing.country and incoming.country and existing.country != incoming.country else primary.country or secondary.country,
            required_country=incoming.required_country or existing.required_country,
            university=primary.university, program=primary.program,
            intake=primary.intake or secondary.intake,
            deadline=None if "deadline" in conflicting else deadline,
            gre_policy="unknown" if "gre_policy" in conflicting else gre,
            evidence=list(evidence.values()),
        )
