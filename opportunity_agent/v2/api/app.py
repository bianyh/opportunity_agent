from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status, Query
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from ..agents.orchestrator import orchestrator
from ...conflict_resolver import ProfileConflictResolver
from ...models import StudentProfile, TaskProgress as DomainTaskProgress
from ...state import derive_state
from ..core.config import settings
from ..core.security import make_access_token
from ..core.telemetry import configure_telemetry, span
from ..db.models import (AgentRun, AgentEvent, Application, ApplicationPlan, ApplicationTask, ApprovalRequest,
                         ChangeProposal, Conversation, Message, ProfileFact, User)
from ..db.session import SessionLocal, create_schema
from ..mcp.tools import V2ToolService
from ..repositories import Repository, VersionConflict
from ..services.applications import ApplicationCommandService
from ..services.auth import AuthService
from ..services.conversation_context import ConversationContextService
from ..services.profile_conflicts import ProfileConflictService
from ..services.memory import MemoryService
from ..services.memory_contracts import RevokePreference
from ..db.models import ProfileConflict
from ..schemas import ConflictDecision
from ..schemas import (ApplicationCreate, ApprovalDecision, ConversationCreate, LoginRequest, ProfilePatch,
                       RegisterRequest, RunCreate, RunCreated, RunSummary, TaskCommand, UserOut)
from .dependencies import CurrentUser, DBSession


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.jwt_secret in {"change-this-before-any-deployment", "local-development-secret-change-me"}:
        raise RuntimeError("JWT_SECRET must be replaced with a private random secret")
    configure_telemetry()
    if settings.auto_create_schema:
        await create_schema()
    dispatcher = asyncio.create_task(dispatch_runs())
    try:
        yield
    finally:
        dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await dispatcher
        for task in list(_run_tasks):
            task.cancel()
        if _run_tasks:
            await asyncio.gather(*list(_run_tasks), return_exceptions=True)
        await orchestrator.aclose()


app = FastAPI(title="Opportunity Agent V2", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8766", "http://localhost:8766"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
app.mount("/v2/assets", StaticFiles(directory=WEB_DIR), name="v2-assets")


@app.get("/v2", include_in_schema=False)
async def v2_page() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


def user_out(user: User) -> dict:
    return UserOut(id=user.id, email=user.email, role=user.role).model_dump()


def set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    # HTTPS deployments should set COOKIE_SECURE=1; localhost must remain usable.
    secure = settings.database_url.startswith("postgres") and __import__("os").getenv("COOKIE_SECURE", "0") == "1"
    response.set_cookie("access_token", access, httponly=True, samesite="lax", secure=secure,
                        max_age=settings.access_token_minutes * 60)
    response.set_cookie("refresh_token", refresh, httponly=True, samesite="lax", secure=secure,
                        max_age=settings.refresh_token_days * 24 * 3600, path="/api/v1/auth")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "opportunity-agent-v2"}


@app.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, response: Response, session: DBSession) -> dict:
    try:
        service = AuthService(session)
        user = await service.register(body.email, body.password)
        access, refresh = await service.issue_tokens(user)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    set_auth_cookies(response, access, refresh)
    return {"user": user_out(user), "access_token": access}


@app.post("/api/v1/auth/login")
async def login(body: LoginRequest, response: Response, session: DBSession) -> dict:
    service = AuthService(session)
    user = await service.authenticate(body.email, body.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect email or password")
    access, refresh = await service.issue_tokens(user)
    await session.commit()
    set_auth_cookies(response, access, refresh)
    return {"user": user_out(user), "access_token": access}


@app.post("/api/v1/auth/refresh")
async def refresh(request: Request, response: Response, session: DBSession) -> dict:
    raw = request.cookies.get("refresh_token")
    result = await AuthService(session).rotate_refresh(raw or "")
    if not result:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")
    user, access, rotated = result
    await session.commit()
    set_auth_cookies(response, access, rotated)
    return {"user": user_out(user), "access_token": access}


@app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: DBSession) -> Response:
    await AuthService(session).revoke_refresh(request.cookies.get("refresh_token") or "")
    await session.commit()
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token", path="/api/v1/auth")
    return response


@app.get("/api/v1/auth/me")
async def me(user: CurrentUser) -> dict:
    return user_out(user)


@app.get("/api/v1/profile")
async def get_profile(user: CurrentUser, session: DBSession) -> dict:
    profile = await Repository(session).ensure_profile(user.id)
    await session.commit()
    return {"payload": profile.payload, "version": profile.version,
            "state": await _profile_state(user.id, profile.payload, session)}


async def _profile_state(user_id: str, payload: dict, session: DBSession) -> dict:
    try:
        profile = StudentProfile.model_validate({**payload, "user_id": user_id})
        applications = (await session.scalars(select(Application).where(Application.user_id == user_id))).all()
        active = await session.scalar(select(ApplicationPlan).where(
            ApplicationPlan.user_id == user_id, ApplicationPlan.status == "active"))
        ids = [item.id for item in applications]
        query = select(ApplicationTask).where(ApplicationTask.plan_id == active.id) if active else None
        plan_tasks = (await session.scalars(query)).all() if query is not None else []
        application_tasks = (await session.scalars(select(ApplicationTask).where(
            ApplicationTask.application_id.in_(ids)))).all() if ids else []
        progress = [DomainTaskProgress(target_id=item.stable_key, title=item.title,
                                       target_kind="event" if item.stable_key.startswith("event:") else "task",
                                       category=item.category, status=item.status,
                                       evidence=item.evidence or "", source_event_id="persisted")
                    for item in [*plan_tasks, *application_tasks]
                    if item.status in {"planned", "in_progress", "completed", "cancelled"}]
        derived = derive_state(profile, progress)
        if any(item.status in {"submitted", "offered"} for item in applications):
            derived.application = "applying"
        return derived.model_dump(mode="json")
    except Exception:
        return {}


@app.patch("/api/v1/profile", status_code=status.HTTP_202_ACCEPTED)
async def patch_profile(body: ProfilePatch, user: CurrentUser, session: DBSession) -> dict:
    try:
        current = await Repository(session).ensure_profile(user.id)
        before = dict(current.payload or {})
        if current.version != body.expected_version:
            raise VersionConflict("profile version conflict")
        payload = dict(body.payload)
        payload.pop("facts", None)
        payload.pop("change_history", None)
        if (before.get("target_schools") != payload.get("target_schools")
                or before.get("target_programs") != payload.get("target_programs")):
            payload.pop("target_program_choices", None)
        validated = StudentProfile.model_validate({**payload, "user_id": user.id})
        updated = validated.model_dump(mode="json", exclude={"facts", "change_history", "user_id"})
        changes = {field: value for field, value in updated.items()
                   if field in ProfileConflictResolver.PROFILE_FIELDS and before.get(field) != value}
        if not changes:
            await session.commit()
            return {"status": "no_change", "version": current.version}
        facts = [{"field": field, "raw_value": value, "normalized_value": value,
                  "source": "user_explicit", "confidence": 1.0,
                  "evidence": "用户在画像表单中提交", "operation": "set"}
                 for field, value in changes.items()]
        proposal = {"facts": facts, "before": {field: before.get(field) for field in changes},
                    "after": changes, "expected_version": current.version}
        approval = await ApplicationCommandService(session).propose(
            user.id, "profile.change", proposal, body.request_id, reason="请确认画像表单中的变更。")
        await session.commit()
    except (VersionConflict, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT if isinstance(exc, VersionConflict) else status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return {"status": "pending", "approval_id": approval.id, "version": current.version}


@app.get("/api/v1/conversations")
async def list_conversations(user: CurrentUser, session: DBSession) -> list[dict]:
    rows = await session.scalars(select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.updated_at.desc()))
    return [{"id": item.id, "title": item.title, "version": item.version, "updated_at": item.updated_at} for item in rows]


@app.post("/api/v1/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(body: ConversationCreate, user: CurrentUser, session: DBSession) -> dict:
    item = Conversation(user_id=user.id, title=body.title)
    session.add(item)
    await session.commit()
    return {"id": item.id, "title": item.title, "version": item.version}


@app.get("/api/v1/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: CurrentUser, session: DBSession) -> dict:
    conversation = await Repository(session).owned_conversation(conversation_id, user.id)
    if not conversation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    messages = await session.scalars(select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at))
    runs = list((await session.scalars(select(AgentRun).where(AgentRun.conversation_id == conversation.id,
        AgentRun.user_id == user.id))).all())
    by_request = {r.request_id: r for r in runs if r.request_id}
    by_id = {r.id: r for r in runs}
    def message_out(item):
        run = by_request.get(item.request_id) if item.request_id else by_id.get((item.metadata_json or {}).get("run_id"))
        return {"id": item.id, "role": item.role, "content": item.content, "status": item.status,
                "created_at": item.created_at, "run_id": run.id if run else None,
                "run_status": run.status if run else None, "trace_id": run.trace_id if run else None,
                "run_error": (run.graph_state or {}).get("error") if run else None}
    return {"id": conversation.id, "title": conversation.title, "version": conversation.version,
            "messages": [message_out(item) for item in messages]}


_run_tasks: set[asyncio.Task] = set()
_scheduled_runs: set[str] = set()


def schedule_run(run_id: str, user_id: str, conversation_id: str, message: str, request_id: str) -> None:
    if run_id in _scheduled_runs:
        return
    _scheduled_runs.add(run_id)
    task = asyncio.create_task(execute_run(run_id, user_id, conversation_id, message, request_id))
    _run_tasks.add(task)
    def finished(task):
        _run_tasks.discard(task)
        _scheduled_runs.discard(run_id)
        if not task.cancelled():
            task.exception()  # Retrieve any failure before the durable dispatcher retries.
    task.add_done_callback(finished)


async def dispatch_runs() -> None:
    """Database-backed queue shared by API workers; claims arbitrate execution."""
    while True:
        try:
            async with SessionLocal() as session:
                now = datetime.now(timezone.utc)
                await session.execute(update(AgentRun).where(
                    AgentRun.status == "running",
                    or_(AgentRun.lease_expires_at < now, AgentRun.lease_expires_at.is_(None)),
                ).values(status="queued", execution_token=None, lease_expires_at=None))
                queued = (await session.scalars(select(AgentRun).where(
                    AgentRun.status == "queued").order_by(AgentRun.created_at).limit(4))).all()
                jobs = []
                for run in queued:
                    if run.id in _scheduled_runs:
                        continue
                    message = await session.scalar(select(Message.content).where(
                        Message.conversation_id == run.conversation_id,
                        Message.request_id == (run.request_id or run.graph_state.get("request_id")),
                        Message.role == "user").order_by(Message.created_at).limit(1))
                    if message is not None:
                        jobs.append((run.id, run.user_id, run.conversation_id, message,
                                     run.request_id or run.graph_state["request_id"]))
                await session.commit()
            for job in jobs:
                if len(_run_tasks) < 4:
                    schedule_run(*job)
        except Exception:
            # A database outage must not permanently stop recovery polling.
            import logging
            logging.getLogger(__name__).exception("Run dispatcher polling failed")
        await asyncio.sleep(2)


async def execute_run(run_id: str, user_id: str, conversation_id: str, message: str, request_id: str) -> None:
    # Background work must own its transaction; a request-scoped AsyncSession is
    # closed as soon as the 202 response is returned.
    async with SessionLocal() as session:
        repo = Repository(session)
        token = uuid.uuid4().hex
        from ..core.research_budget import execution_limit
        if not await repo.claim_run(run_id, token, lease_seconds=int(execution_limit()) + 180):
            await session.rollback()
            return
        await session.commit()
        run = await session.get(AgentRun, run_id)
        if not run:
            return
        try:
            await repo.append_event(run.id, "run_preparing", {})
            await session.commit()
            initial = await _execution_initial(session, repo, run, user_id, conversation_id, message, request_id)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            run = await session.get(AgentRun, run_id)
            await repo.append_event(run.id, "run_failed", {"error": f"{type(exc).__name__}: {exc}"})
            await repo.finalize_run(run, {"error": str(exc)}, "failed", token)
            await session.commit()
            return
        event_queue: asyncio.Queue = asyncio.Queue()
        event_errors: list[Exception] = []

        async def persist_events() -> None:
            while True:
                item = await event_queue.get()
                try:
                    if item is None:
                        return
                    async with SessionLocal() as event_session:
                        await Repository(event_session).append_event(run.id, item[0], item[1])
                        await event_session.commit()
                except Exception as exc:
                    event_errors.append(exc)
                finally:
                    event_queue.task_done()

        event_writer = asyncio.create_task(persist_events())
        try:
            with span("agent.run", run_id=run.id, user_id=user_id) as run_span:
                trace_id = format(run_span.get_span_context().trace_id, "032x")
                run.trace_id = trace_id
                await repo.append_event(run.id, "trace_started", {"trace_id": trace_id})
                # Release the event counter row before the asynchronous event
                # writer starts; otherwise PostgreSQL/SQLite can deadlock.
                await session.commit()
                state = await orchestrator.ainvoke(initial, event_queue=event_queue)
            await event_queue.join()
            await session.refresh(run)
            if run.execution_token != token:
                return
            run.trace_id = trace_id
            if event_errors:
                raise RuntimeError(f"could not persist agent events: {event_errors[0]}")
            # Agent proposals are persisted separately and are the only route to future writes.
            if (state.get("completion") or {}).get("status") in {"PASS", "PARTIAL", "NEED_USER"}:
                current_message = await session.scalar(select(Message).where(Message.conversation_id == conversation_id,
                    Message.role == "user", Message.request_id == request_id))
                changed = await MemoryService(session).save_explicit(user_id, message, request_id,
                    conversation_id, current_message.id if current_message else None)
                if changed:
                    await repo.append_event(run.id, "memory_updated", {"preferences": changed})
            state["conflict_ids"] = []
            for conflict in (state.get("profile_result") or {}).get("conflicts", []):
                row = await ProfileConflictService(session).record(user_id, run.id, conflict)
                state["conflict_ids"].append(row.id)
                conflict["conflict_id"] = row.id
            if state["conflict_ids"]:
                await repo.append_event(run.id, "profile_conflict", {"conflict_ids": state["conflict_ids"]})
            if state.get("proposals"):
                service = ApplicationCommandService(session)
                state["approval_ids"] = []
                for index, proposal in enumerate(state["proposals"]):
                    approval = await service.propose(user_id, proposal["type"], proposal["payload"],
                                                     f"{request_id}:{index}", run.id, proposal.get("reason", ""))
                    state["approval_ids"].append(approval.id)
                    await repo.append_event(run.id, "approval_required", {"approval_id": approval.id, "proposal_type": proposal["type"]})
            session.add(Message(conversation_id=conversation_id, role="assistant", content=state["answer"], status="processed", metadata_json={"run_id": run.id}))
            await repo.finalize_run(run, state, "completed", token)
            if state.get("consolidation_input"):
                await MemoryService(session).enqueue(state["consolidation_input"])
            await repo.append_event(run.id, "run_completed", {"run_id": run.id})
        except Exception as exc:
            await event_queue.join()
            await session.rollback()
            run = await session.get(AgentRun, run_id)
            if run.execution_token != token:
                return
            await repo.append_event(run.id, "run_failed", {"error": f"{type(exc).__name__}: {exc}"})
            await repo.finalize_run(run, {"error": str(exc)}, "failed", token)
        finally:
            event_queue.put_nowait(None)
            await event_writer
        await session.commit()


async def _execution_initial(session: DBSession, repo: Repository, run: AgentRun,
                             user_id: str, conversation_id: str, message: str, request_id: str) -> dict:
        profile = await repo.ensure_profile(user_id)
        context = await ConversationContextService(session).build(user_id, conversation_id, request_id, message, profile.payload)
        try:
            async with session.begin_nested():
                preference_versions = await MemoryService(session).versions(user_id)
        except Exception:
            preference_versions = {}
        current_message = await session.scalar(select(Message).where(Message.conversation_id == conversation_id,
            Message.role == "user", Message.request_id == request_id))
        history_statement = select(Message).where(Message.conversation_id == conversation_id, Message.role == "user")
        if current_message:
            history_statement = history_statement.where(Message.created_at <= current_message.created_at)
        user_history = list((await session.scalars(history_statement.order_by(Message.created_at.desc(), Message.id.desc()).limit(6))).all())
        user_history = [m for m in reversed(user_history) if not current_message or m.id != current_message.id]
        if current_message:
            user_history.append(current_message)
        facts = (await session.scalars(select(ProfileFact).where(ProfileFact.user_id == user_id)
                                       .order_by(ProfileFact.created_at.desc()).limit(100))).all()
        applications = (await session.scalars(select(Application).where(Application.user_id == user_id))).all()
        active_plan = await session.scalar(select(ApplicationPlan).where(
            ApplicationPlan.user_id == user_id, ApplicationPlan.status == "active"
        ).order_by(ApplicationPlan.version.desc()))
        task_query = select(ApplicationTask).where(ApplicationTask.plan_id == active_plan.id) if active_plan else None
        plan_tasks = (await session.scalars(task_query)).all() if task_query is not None else []
        application_tasks = (await session.scalars(select(ApplicationTask).where(
            ApplicationTask.application_id.in_([item.id for item in applications])))).all() if applications else []
        tasks = [*plan_tasks, *application_tasks]
        return {"user_id": user_id, "conversation_id": conversation_id, "run_id": run.id, "request_id": request_id,
                   "message": message, "recent_messages": context.recent_messages,
                   "preference_memory": context.preference_memory.model_dump(mode="json"),
                   "preference_versions": preference_versions,
                   "user_messages": [{"message_id": m.id, "content": m.content[:10000]} for m in user_history[-6:]],
                   "conversation_context": context.model_dump(mode="json"),
                   "profile_payload": profile.payload, "profile_version": profile.version,
                   "profile_facts": [{"field": item.field, "raw_value": item.raw_value,
                                      "normalized_value": item.normalized_value, "source": item.source,
                                      "confidence": item.confidence, "evidence": item.evidence,
                                      "operation": item.operation} for item in reversed(facts)],
                   "applications": [{"id": item.id, "university": item.university, "program": item.program,
                                     "intake": item.intake, "status": item.status, "version": item.version}
                                    for item in applications],
                   "current_plan": {"roadmap": active_plan.roadmap} if active_plan else {},
                   "current_plan_version": active_plan.version if active_plan else 0,
                   "current_tasks": [{"id": item.id, "stable_key": item.stable_key, "title": item.title,
                                      "category": item.category, "status": item.status,
                                      "evidence": item.evidence, "due_at": item.due_at}
                                     for item in tasks], "events": []}


@app.post("/api/v1/conversations/{conversation_id}/runs", status_code=status.HTTP_202_ACCEPTED, response_model=RunCreated)
async def create_run(conversation_id: str, body: RunCreate, user: CurrentUser, session: DBSession) -> dict:
    conversation = await Repository(session).owned_conversation(conversation_id, user.id)
    if not conversation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    repo = Repository(session)
    try:
        run = await repo.create_run(user.id, conversation_id, body.message, body.request_id)
    except VersionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    if run.status == "queued" and len(_run_tasks) < 4:
        schedule_run(run.id, user.id, conversation_id, body.message, body.request_id)
    return {"run_id": run.id, "status": run.status}


@app.get("/api/v1/conversations/{conversation_id}/runs", summary="列出当前会话的 Run ID、状态和 Trace ID", response_model=list[RunSummary])
async def list_conversation_runs(conversation_id: str, user: CurrentUser, session: DBSession,
                                 limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    if not await Repository(session).owned_conversation(conversation_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    rows = await session.scalars(select(AgentRun).where(AgentRun.conversation_id == conversation_id,
        AgentRun.user_id == user.id).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit))
    return [{"run_id": r.id, "status": r.status, "trace_id": r.trace_id, "created_at": r.created_at,
             "error": (r.graph_state or {}).get("error")} for r in rows]


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, user: CurrentUser, session: DBSession,
                     after: int = Query(default=0, ge=0)) -> StreamingResponse:
    run = await session.get(AgentRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    try:
        resume = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid Last-Event-ID")
    cursor_start = max(after, resume)
    if resume < 0 or cursor_start > run.event_sequence:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid event cursor")
    await session.rollback()

    async def event_stream() -> AsyncIterator[str]:
        cursor = cursor_start
        heartbeat = asyncio.get_running_loop().time()
        yield "retry: 1500\n\n"
        while True:
            if await request.is_disconnected():
                break
            events = await Repository(session).list_events(run_id, cursor)
            rows = [(item.sequence, item.event_type, item.payload) for item in events]
            current = await session.execute(select(AgentRun.status, AgentRun.event_sequence).where(AgentRun.id == run_id))
            current_status, sequence = current.one()
            # Release the transaction/connection during network sends and idle waits.
            await session.rollback()
            for cursor, name, payload in rows:
                yield f"id: {cursor}\nevent: {name}\ndata: {json.dumps({'sequence': cursor, 'payload': payload}, ensure_ascii=False, default=str)}\n\n"
            if current_status in {"completed", "failed"} and cursor >= sequence:
                break
            now = asyncio.get_running_loop().time()
            if now-heartbeat >= 10:
                yield ": heartbeat\n\n"
                heartbeat = now
            await asyncio.sleep(0.25)
    return StreamingResponse(event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str, user: CurrentUser, session: DBSession) -> dict:
    run = await session.get(AgentRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    state = run.graph_state or {}
    from ..research.failures import run_failure_report
    failure_report = run_failure_report(state)
    if not state.get("completion") and run.status == "running":
        persisted_report = await session.scalar(select(AgentEvent.payload).where(AgentEvent.run_id == run_id,
            AgentEvent.event_type == "run_diagnostics").order_by(AgentEvent.sequence.desc()).limit(1))
        if persisted_report:
            failure_report = persisted_report
    routing = state.get("routing_diagnostics")
    if not routing:
        routing = await session.scalar(select(AgentEvent.payload).where(AgentEvent.run_id == run_id,
            AgentEvent.event_type == "router_diagnostics").order_by(AgentEvent.sequence.desc()).limit(1))
    progress = await session.scalar(select(AgentEvent).where(AgentEvent.run_id == run_id,
        AgentEvent.event_type.in_(["run_preparing", "run_started", "goal_parse_started", "goal_parsed", "routing_started",
            "route_selected", "agent_started", "agent_completed", "research_progress", "completion_checked",
            "repair_round_started", "synthesis_started"])).order_by(AgentEvent.sequence.desc()).limit(1))
    latest_draft = await session.scalar(select(AgentEvent).where(AgentEvent.run_id == run_id,
        AgentEvent.event_type.in_(["answer_snapshot", "answer_reset"])).order_by(AgentEvent.sequence.desc()).limit(1))
    return {"id": run.id, "status": run.status, "trace_id": run.trace_id, "answer": state.get("answer", ""),
            "routing_diagnostics": routing or {}, "route_decision": state.get("route_decision"),
            "approval_ids": state.get("approval_ids", []),
            "conflict_ids": state.get("conflict_ids", []),
            "completion": state.get("completion"), "profile_result": state.get("profile_result"),
            "research_result": state.get("research_result"), "plan_result": state.get("plan_result"),
            "error": state.get("error"), "failure_report": failure_report,
            "created_at": run.created_at, "last_event_sequence": run.event_sequence,
            "progress": {"event_type": progress.event_type, **progress.payload} if progress else {},
            "draft_answer": latest_draft.payload.get("text", "") if latest_draft and latest_draft.event_type == "answer_snapshot" and run.status == "running" else ""}


@app.get("/api/v1/memory/preferences")
async def list_memory_preferences(user: CurrentUser, session: DBSession) -> list[dict]:
    service = MemoryService(session)
    return [service.view(row).model_dump(mode="json") for row in await service.list_preferences(user.id)]


@app.post("/api/v1/memory/preferences/{key}/revoke")
async def revoke_memory_preference(key: str, body: RevokePreference, user: CurrentUser, session: DBSession) -> dict:
    try:
        row = await MemoryService(session).revoke(user.id, key, body.request_id, body.expected_version)
        await session.commit()
        return {"memory_id": row.id, "version": row.version, "active": row.active}
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(404, str(exc))
    except VersionConflict as exc:
        await session.rollback()
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(422, str(exc))


@app.get("/api/v1/applications")
async def list_applications(user: CurrentUser, session: DBSession) -> list[dict]:
    rows = await session.scalars(select(Application).where(Application.user_id == user.id))
    return [{"id": item.id, "university": item.university, "program": item.program, "intake": item.intake,
             "status": item.status, "deadline": item.deadline, "version": item.version} for item in rows]


def _plan_out(plan: ApplicationPlan, tasks: list[ApplicationTask]) -> dict:
    return jsonable_encoder({"id": plan.id, "version": plan.version, "status": plan.status,
                             "revision_reason": plan.revision_reason, "roadmap": plan.roadmap,
                             "created_at": plan.created_at,
                             "tasks": [{"id": item.id, "stable_key": item.stable_key, "title": item.title,
                                        "category": item.category, "due_at": item.due_at,
                                        "status": item.status, "evidence": item.evidence}
                                       for item in tasks]})


@app.get("/api/v1/plans")
async def list_plans(user: CurrentUser, session: DBSession) -> list[dict]:
    rows = (await session.scalars(select(ApplicationPlan).where(
        ApplicationPlan.user_id == user.id).order_by(ApplicationPlan.version.desc()))).all()
    return jsonable_encoder([{"id": item.id, "version": item.version, "status": item.status,
                              "revision_reason": item.revision_reason, "created_at": item.created_at}
                             for item in rows])


@app.get("/api/v1/plans/current")
async def get_current_plan(user: CurrentUser, session: DBSession) -> dict:
    plan = await session.scalar(select(ApplicationPlan).where(
        ApplicationPlan.user_id == user.id, ApplicationPlan.status == "active"
    ).order_by(ApplicationPlan.version.desc()))
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no current plan")
    tasks = (await session.scalars(select(ApplicationTask).where(ApplicationTask.plan_id == plan.id))).all()
    return _plan_out(plan, tasks)


@app.get("/api/v1/plans/{plan_id}")
async def get_plan(plan_id: str, user: CurrentUser, session: DBSession) -> dict:
    plan = await session.get(ApplicationPlan, plan_id)
    if not plan or plan.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    tasks = (await session.scalars(select(ApplicationTask).where(ApplicationTask.plan_id == plan.id))).all()
    return _plan_out(plan, tasks)


@app.get("/api/v1/approvals")
async def list_approvals(user: CurrentUser, session: DBSession, status_filter: str = "pending") -> list[dict]:
    query = select(ApprovalRequest).where(ApprovalRequest.user_id == user.id)
    if status_filter != "all":
        query = query.where(ApprovalRequest.status == status_filter)
    rows = (await session.scalars(query.order_by(ApprovalRequest.created_at.desc()))).all()
    proposals = {item.id: item for item in (await session.scalars(select(ChangeProposal).where(
        ChangeProposal.id.in_([row.proposal_id for row in rows])))).all()} if rows else {}
    return jsonable_encoder([{"id": item.id, "status": item.status, "created_at": item.created_at,
                              "proposal_type": proposals[item.proposal_id].proposal_type,
                              "reason": proposals[item.proposal_id].reason,
                              "payload": proposals[item.proposal_id].payload}
                             for item in rows])


@app.get("/api/v1/profile/conflicts")
async def list_profile_conflicts(user: CurrentUser, session: DBSession) -> list[dict]:
    rows = (await session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == user.id, ProfileConflict.status == "pending"
    ).order_by(ProfileConflict.created_at, ProfileConflict.id))).all()
    return jsonable_encoder([{"conflict_id": row.id, "field": row.field,
                              "old_value": row.old_value, "new_value": row.new_value,
                              "old_source": row.old_source, "new_source": row.new_source,
                              "new_evidence": row.new_evidence, "status": row.status} for row in rows])


@app.post("/api/v1/profile/conflicts/{conflict_id}/resolve")
async def resolve_profile_conflict(conflict_id: str, body: ConflictDecision,
                                   user: CurrentUser, session: DBSession) -> dict:
    try:
        row = await ProfileConflictService(session).resolve(conflict_id, user.id, body.choice)
        await session.commit()
        return {"conflict_id": row.id, "status": row.status}
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc))


@app.get("/api/v1/approvals/{approval_id}")
async def get_approval(approval_id: str, user: CurrentUser, session: DBSession) -> dict:
    approval = await session.get(ApprovalRequest, approval_id)
    if not approval or approval.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    proposal = await session.get(ChangeProposal, approval.proposal_id)
    return jsonable_encoder({"id": approval.id, "status": approval.status, "created_at": approval.created_at,
                             "proposal_type": proposal.proposal_type, "reason": proposal.reason,
                             "payload": proposal.payload})


@app.post("/api/v1/applications", status_code=status.HTTP_202_ACCEPTED)
async def propose_application(body: ApplicationCreate, user: CurrentUser, session: DBSession) -> dict:
    approval = await ApplicationCommandService(session).propose(user.id, "application.create", body.model_dump(mode="json"),
                                                                  request_id=f"application:{body.university}:{body.program}:{body.intake}")
    await session.commit()
    return {"approval_id": approval.id, "status": approval.status, "message": "Application creation requires confirmation."}


@app.post("/api/v1/tasks/{task_id}/commands", status_code=status.HTTP_202_ACCEPTED)
async def propose_task_command(task_id: str, body: TaskCommand, user: CurrentUser, session: DBSession) -> dict:
    task = await session.get(ApplicationTask, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    owner = await session.get(Application, task.application_id) if task.application_id else (
        await session.get(ApplicationPlan, task.plan_id) if task.plan_id else None)
    if not owner or owner.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    data = body.model_dump(mode="json") | {"task_id": task_id, "expected_status": task.status}
    approval = await ApplicationCommandService(session).propose(user.id, "task.command", data, body.request_id)
    await session.commit()
    return {"approval_id": approval.id, "status": approval.status}


@app.post("/api/v1/approvals/{approval_id}/accept")
async def approve(approval_id: str, _: ApprovalDecision, user: CurrentUser, session: DBSession) -> dict:
    try:
        proposal = await ApplicationCommandService(session).decide(approval_id, user.id, True)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        raise _approval_error(exc)
    return {"proposal_id": proposal.id, "status": proposal.status}


@app.post("/api/v1/approvals/{approval_id}/reject")
async def reject(approval_id: str, _: ApprovalDecision, user: CurrentUser, session: DBSession) -> dict:
    try:
        proposal = await ApplicationCommandService(session).decide(approval_id, user.id, False)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        raise _approval_error(exc)
    return {"proposal_id": proposal.id, "status": proposal.status}


def _approval_error(exc: ValueError | IntegrityError) -> HTTPException:
    detail = str(exc)
    if isinstance(exc, IntegrityError) or "conflict" in detail:
        return HTTPException(status.HTTP_409_CONFLICT, detail)
    if detail in {"approval request not found", "proposal not found", "task not found", "application not found"}:
        return HTTPException(status.HTTP_404_NOT_FOUND, detail)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


@app.get("/api/v1/research/sources")
async def research_sources(query: str, user: CurrentUser, session: DBSession, school: str = "", program: str = "") -> dict:
    return await V2ToolService(session).search_official_requirements(query, school, program)
