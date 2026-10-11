"""Minimal interface shared by stateless V2 domain-agent implementations."""
from __future__ import annotations

from typing import Any, Protocol

from .contracts import AgentName, MissingTask, SuccessCriteria


class DomainRequest(Protocol):
    """Transport-independent view of a validated A2A domain request."""

    agent: AgentName
    user_id: str
    request_id: str
    message: str
    recent_messages: list[dict[str, str]]
    conversation_context: dict[str, Any]
    success_criteria: SuccessCriteria | None
    missing_task: MissingTask | None
    remaining_budget_seconds: float
    research_progress: dict[str, Any]
    research_tool_state: dict[str, Any]
    progress_channel: str
    profile_payload: dict[str, Any]
    profile_version: int
    profile_facts: list[dict[str, Any]]
    applications: list[dict[str, Any]]
    current_plan: dict[str, Any]
    current_plan_version: int
    current_tasks: list[dict[str, Any]]
    research_result: dict[str, Any]
    relevant_memory: dict[str, Any]
    preference_memory: dict[str, Any]
    turn_preferences: list[dict[str, Any]]
