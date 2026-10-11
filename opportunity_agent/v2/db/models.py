from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Timestamped, UUIDPrimaryKey

Json = dict[str, Any]


class User(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(32), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Profile(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "profiles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[Json] = mapped_column(JSON, default=dict)


class ProfileFact(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "profile_facts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(120), index=True)
    raw_value: Mapped[Json] = mapped_column(JSON, default=dict)
    normalized_value: Mapped[Json] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column()
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation: Mapped[str] = mapped_column(String(16), default="set")


class ProfileChange(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "profile_changes"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    field: Mapped[str] = mapped_column(String(120))
    before: Mapped[Json | None] = mapped_column(JSON, nullable=True)
    after: Mapped[Json | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text)


class Conversation(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "conversations"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="新对话")
    version: Mapped[int] = mapped_column(Integer, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # V1 import provenance. Titles are not unique (many V1 sessions are all
    # "新对话"), so the legacy id is the only safe idempotency key for
    # re-running the migration.
    legacy_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    __table_args__ = (UniqueConstraint("user_id", "legacy_session_id", name="uq_conversation_legacy_session"),)


class Message(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="processed")
    metadata_json: Mapped[Json] = mapped_column("metadata", JSON, default=dict)


class ConversationSummary(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "conversation_summaries"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    through_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    through_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), default="extractive")


class ProfileConflict(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "profile_conflicts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(120))
    old_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    old_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_source: Mapped[str] = mapped_column(String(64))
    new_evidence: Mapped[str] = mapped_column(Text)
    candidate: Mapped[Json] = mapped_column(JSON)
    expected_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    __table_args__ = (UniqueConstraint("run_id", "field", name="uq_profile_conflict_run_field"),)


class AgentRun(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "agent_runs"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    graph_state: Mapped[Json] = mapped_column(JSON, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (UniqueConstraint("conversation_id", "request_id", name="uq_run_request"),)


class AgentEvent(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "agent_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[Json] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_agent_event_sequence"),)


class ApplicationPlan(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "application_plans"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    roadmap: Mapped[Json] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")
    revision_reason: Mapped[str] = mapped_column(String(160), default="initial_profile")
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_application_plan_user_version"),)


class Application(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "applications"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    university: Mapped[str] = mapped_column(String(200), index=True)
    program: Mapped[str] = mapped_column(String(200), index=True)
    intake: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(32), default="considering")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requirements: Mapped[Json] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ApplicationTask(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "application_tasks"
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=True)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("application_plans.id", ondelete="CASCADE"), index=True, nullable=True)
    stable_key: Mapped[str] = mapped_column(String(160), index=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(32), default="application")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="planned")
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("application_id", "stable_key", name="uq_application_task_stable_key"),
        UniqueConstraint("plan_id", "stable_key", name="uq_plan_task_stable_key"),
    )


class TaskProgress(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "task_progress"
    task_id: Mapped[str] = mapped_column(ForeignKey("application_tasks.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="user_explicit")


class OfficialSource(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "official_sources"
    source_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    university: Mapped[str] = mapped_column(String(200), index=True)
    program: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    excerpt: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="verified")


class OfficialRequirement(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "official_requirements"
    source_id: Mapped[str] = mapped_column(ForeignKey("official_sources.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[str] = mapped_column(Text)
    qualifier: Mapped[str] = mapped_column(String(32), default="unknown")
    confidence: Mapped[float] = mapped_column()


class ResearchProgram(UUIDPrimaryKey, Timestamped, Base):
    """Public programme catalogue, independent of a user's applications."""
    __tablename__ = "research_programs"
    university: Mapped[str] = mapped_column(String(200), index=True)
    program: Mapped[str] = mapped_column(String(200), index=True)
    intake: Mapped[str] = mapped_column(String(40))
    country: Mapped[str] = mapped_column(String(80), default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("university", "program", "intake", name="uq_research_program_identity"),)


class ResearchRequirement(UUIDPrimaryKey, Timestamped, Base):
    """Versioned field observations. Conflicting observations are retained."""
    __tablename__ = "research_requirements"
    program_id: Mapped[str] = mapped_column(ForeignKey("research_programs.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("official_sources.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[Any] = mapped_column(JSON)
    date_value: Mapped[Any | None] = mapped_column(Date, nullable=True, index=True)
    qualifier: Mapped[str] = mapped_column(String(64), default="")
    excerpt: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(128))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="verified")
    program_match: Mapped[str] = mapped_column(String(32), default="unknown")
    extraction_provenance: Mapped[dict] = mapped_column(JSON, default=dict)


class KnowledgeDocument(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "knowledge_documents"
    source_id: Mapped[str | None] = mapped_column(ForeignKey("official_sources.id", ondelete="SET NULL"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="official")
    authority: Mapped[str] = mapped_column(String(32), default="official")
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(2048), default="")
    content_hash: Mapped[str] = mapped_column(String(128), unique=True)
    metadata_json: Mapped[Json] = mapped_column("metadata", JSON, default=dict)


class KnowledgeChunk(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "knowledge_chunks"
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[Json] = mapped_column("metadata", JSON, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384).with_variant(JSON, "sqlite"), nullable=True)
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_index"),)


class MemoryItem(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "memory_items"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(160), index=True)
    value: Mapped[Json] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    evidence: Mapped[str] = mapped_column(Text, default="", server_default="")
    source_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384).with_variant(JSON, "sqlite"), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "memory_type", "key", name="uq_memory_preference"),)


class MemoryAudit(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "memory_audits"
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    memory_id: Mapped[str] = mapped_column(String(64))
    event_key: Mapped[str] = mapped_column(String(256))
    snapshot: Mapped[Json] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("user_id", "event_key", name="uq_memory_audit_event"),)


class MemoryOutbox(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "memory_outbox"
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    job_type: Mapped[str] = mapped_column(String(64), default="consolidate")
    payload: Mapped[Json] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[Json] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("run_id", "job_type", name="uq_memory_outbox_run"),)


class ChangeProposal(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "change_proposals"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    proposal_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[Json] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)


class ApprovalRequest(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "approval_requests"
    proposal_id: Mapped[str] = mapped_column(ForeignKey("change_proposals.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")


class EvaluationCase(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "evaluation_cases"
    category: Mapped[str] = mapped_column(String(64), index=True)
    input: Mapped[Json] = mapped_column(JSON, default=dict)
    expected: Mapped[Json] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class EvaluationRun(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "evaluation_runs"
    name: Mapped[str] = mapped_column(String(160))
    configuration: Mapped[Json] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running")


class EvaluationMetric(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "evaluation_metrics"
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float] = mapped_column()
    details: Mapped[Json] = mapped_column(JSON, default=dict)
