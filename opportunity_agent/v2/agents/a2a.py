"""V2.2 openJiuwen A2A boundary for the three domain agents.

The CustomOrchestrator owns this client.  The Router never imports it, so a
route decision cannot accidentally turn into tool execution or a final answer.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import json
import logging
import signal
import time
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.config import settings
from ..core.telemetry import span, trace_carrier
from ..core.research_budget import research_limit, research_seconds, estimate_school_count, synthesis_reserve
from .contracts import (
    AgentName,
    MissingTask,
    PlanResult,
    ProfileResult,
    ResearchResult,
    SuccessCriteria,
)
from .planning_agent import PlanningAgent
from .profile_agent import ProfileAgent
from .research_agent import ResearchAgent


logger = logging.getLogger(__name__)
PROTOCOL_VERSION = "2.2"


class DomainA2ARequest(BaseModel):
    """Minimal, serialisable input for one stateless V2 domain invocation."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["2.2"] = PROTOCOL_VERSION
    agent: AgentName
    user_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=10_000)
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    success_criteria: SuccessCriteria | None = None
    missing_task: MissingTask | None = None
    remaining_budget_seconds: float = Field(default=600, gt=0, le=3600)
    research_progress: dict[str, Any] = Field(default_factory=dict)
    research_tool_state: dict[str, Any] = Field(default_factory=dict)
    progress_channel: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    round_id: int = Field(default=0, ge=0)
    trace_context: dict[str, str] = Field(default_factory=dict)
    # Profile and Planning may need an explicit user-owned profile summary.
    # Research deliberately receives no profile snapshot.
    profile_payload: dict[str, Any] = Field(default_factory=dict)
    profile_version: int = 1
    profile_facts: list[dict[str, Any]] = Field(default_factory=list)
    applications: list[dict[str, Any]] = Field(default_factory=list)
    current_plan: dict[str, Any] = Field(default_factory=dict)
    current_plan_version: int = 0
    current_tasks: list[dict[str, Any]] = Field(default_factory=list)
    research_result: dict[str, Any] = Field(default_factory=dict)
    relevant_memory: dict[str, Any] = Field(default_factory=dict)
    preference_memory: dict[str, Any] = Field(default_factory=dict)
    turn_preferences: list[dict[str, Any]] = Field(default_factory=list)

    def wire_json(self) -> str:
        # Only Research consumes new repair metadata. Keep Profile/Planning's
        # existing strict wire schema compatible during a rolling deployment.
        return self.model_dump_json(exclude={"research_progress", "research_tool_state", "progress_channel"} if self.agent != "research" else None)

    @model_validator(mode="after")
    def validate_agent_scope(self) -> "DomainA2ARequest":
        if self.missing_task and self.missing_task.agent != self.agent:
            raise ValueError("missing_task must target the receiving agent")
        if self.agent == "research" and any((self.profile_payload, self.profile_facts, self.applications,
                                               self.current_plan, self.current_tasks, self.research_result,
                                               self.recent_messages, self.conversation_context)):
            raise ValueError("research requests must not include a user state snapshot")
        return self


class DomainA2AResponse(BaseModel):
    """Correlated envelope around a structured domain result."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["2.2"] = PROTOCOL_VERSION
    agent: AgentName
    run_id: str
    request_id: str
    result: dict[str, Any]


class DomainAgentUnavailable(RuntimeError):
    """A2A binding, endpoint, protocol, or response failure."""


def request_from_state(
    agent: AgentName,
    state: Any,
    missing_task: MissingTask | None = None,
) -> DomainA2ARequest:
    """Make the least-privilege request allowed for the destination agent."""

    has_snapshot = agent in {"profile", "planning"}
    query = (state.route_decision.resolved_query if agent != "profile" and state.route_decision
             and state.route_decision.resolved_query else state.message)
    allowance = research_seconds(estimate_school_count(query,
        state.success_criteria.required_program_count if state.success_criteria else None)) if agent == "research" else 55.
    if getattr(state, "_execution_deadline", None):
        allowance = min(allowance, state._execution_deadline - time.monotonic()
                        - (synthesis_reserve() if agent == "research" else 0))
    return DomainA2ARequest(
        progress_channel=uuid.uuid4().hex if agent == "research" and os.getenv("RESEARCH_PROGRESS_ENABLED", "0") == "1" else "",
        agent=agent,
        user_id=state.user_id,
        conversation_id=state.conversation_id,
        run_id=state.run_id,
        request_id=state.request_id,
        message=(state.route_decision.resolved_query if agent != "profile" and state.route_decision
                 and state.route_decision.resolved_query else state.message),
        recent_messages=list(state.recent_messages) if agent == "profile" else [],
        conversation_context=dict(state.conversation_context) if has_snapshot else {},
        success_criteria=state.success_criteria,
        missing_task=missing_task,
        round_id=state.round_id,
        remaining_budget_seconds=max(.01, min(3600., allowance)),
        research_progress=dict(state.research_result.diagnostics.get("web_progress", {}))
            if agent == "research" and state.research_result else {},
        research_tool_state=dict(state.research_result.diagnostics.get("tool_execution", {}))
            if agent == "research" and state.research_result else {},
        trace_context=trace_carrier(),
        profile_payload=dict(state.profile_payload) if has_snapshot else {},
        profile_version=state.profile_version if has_snapshot else 1,
        profile_facts=list(state.profile_facts) if agent == "profile" else [],
        applications=list(state.applications) if has_snapshot else [],
        current_plan=dict(state.current_plan) if has_snapshot else {},
        current_plan_version=state.current_plan_version if has_snapshot else 0,
        current_tasks=list(state.current_tasks) if has_snapshot else [],
        research_result=state.research_result.model_dump(mode="json") if agent == "planning" and state.research_result else {},
        relevant_memory=dict(state.memory) if has_snapshot or os.getenv("RESEARCH_ALLOW_SEED_FIXTURES", "0") == "1" else {},
        preference_memory=state.preference_memory.model_dump(mode="json") if has_snapshot else {
            "preferences": [{"key": p.key, "value": p.value, "version": p.version}
                            for p in state.preference_memory.preferences]},
        turn_preferences=list(state.turn_preferences) if has_snapshot else [
            {"key": p["key"], "value": p["value"]} for p in state.turn_preferences],
    )


class RustOpenJiuwenDomainAgents:
    """Pooled synchronous project-built Rust A2A clients (local compatibility)."""

    def __init__(
        self,
        endpoints: Mapping[AgentName, str] | None = None,
        timeouts: Mapping[AgentName, float] | None = None,
    ) -> None:
        self.endpoints: dict[AgentName, str] = dict(endpoints or {
            "profile": settings.profile_a2a_url,
            "research": settings.research_a2a_url,
            "planning": settings.planning_a2a_url,
        })
        self.timeouts: dict[AgentName, float] = dict(timeouts or {
            "profile": float(settings.profile_a2a_timeout_seconds),
            "research": float(settings.research_a2a_timeout_seconds),
            "planning": float(settings.planning_a2a_timeout_seconds),
        })
        self._clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _binding() -> Any:
        try:
            from openjiuwenrust._rust import A2aClient
        except ImportError as exc:  # pragma: no cover - exercised only without optional binding
            raise DomainAgentUnavailable(
                "openjiuwenrust is required for JIUWEN_BACKEND=rust; "
                "install the project-built binding before starting V2 A2A agents."
            ) from exc
        return A2aClient

    async def _client_for(self, endpoint: str) -> Any:
        async with self._lock:
            client = self._clients.get(endpoint)
            if client is None:
                client = self._binding()(json.dumps({"endpoint": endpoint, "streaming": True}))
                self._clients[endpoint] = client
            return client

    async def execute(self, agent: AgentName, state: Any,
                      missing_task: MissingTask | None = None) -> ProfileResult | ResearchResult | PlanResult:
        endpoint = self.endpoints.get(agent)
        if not endpoint:
            raise DomainAgentUnavailable(f"no A2A endpoint configured for {agent}")
        request = request_from_state(agent, state, missing_task)
        wire = json.dumps({"query": request.wire_json(), "conversation_id": request.conversation_id}, ensure_ascii=False)
        client = await self._client_for(endpoint)
        timeout = max(self.timeouts.get(agent, 60.0), request.remaining_budget_seconds + 10) if agent == "research" else self.timeouts.get(agent, 60.0)
        try:
            from ..core.progress import relay_progress
            async with relay_progress(request, state):
                with span("a2a.domain_call", agent=agent, run_id=request.run_id, endpoint=endpoint):
                    raw = await asyncio.wait_for(
                        asyncio.to_thread(client.send_message, wire, timeout), timeout=timeout + 5,
                    )
            envelope = DomainA2AResponse.model_validate_json(raw)
        except DomainAgentUnavailable:
            raise
        except Exception as exc:
            raise DomainAgentUnavailable(f"{agent} A2A call failed: {type(exc).__name__}: {exc}") from exc
        if envelope.agent != agent or envelope.run_id != request.run_id or envelope.request_id != request.request_id:
            raise DomainAgentUnavailable(f"{agent} A2A response correlation mismatch")
        return _result_model(agent, envelope.result)

    async def aclose(self) -> None:
        """Release pooled Rust clients during graceful API shutdown."""
        async with self._lock:
            clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await asyncio.to_thread(client.destroy)


class PythonOpenJiuwenDomainAgents:
    """Pooled official Python SDK A2A clients for Linux and Windows deployments."""

    def __init__(
        self,
        endpoints: Mapping[AgentName, str] | None = None,
        timeouts: Mapping[AgentName, float] | None = None,
    ) -> None:
        self.endpoints: dict[AgentName, str] = dict(endpoints or {
            "profile": settings.profile_a2a_url,
            "research": settings.research_a2a_url,
            "planning": settings.planning_a2a_url,
        })
        self.timeouts: dict[AgentName, float] = dict(timeouts or {
            "profile": float(settings.profile_a2a_timeout_seconds),
            "research": float(settings.research_a2a_timeout_seconds),
            "planning": float(settings.planning_a2a_timeout_seconds),
        })
        self._clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _binding() -> tuple[Any, Any, Any]:
        try:
            from openjiuwen.core.single_agent.schema.agent_card import AgentCard
            from openjiuwen.extensions.a2a.a2a_client import A2AClient
            from openjiuwen.extensions.a2a.a2a_agentcard_adapter import A2AAgentCardAdapter
        except ImportError as exc:  # pragma: no cover - depends on optional deployment extra
            raise DomainAgentUnavailable(
                "openjiuwen[all-a2a] is required for JIUWEN_BACKEND=python; "
                "install the V2 dependencies before starting V2 A2A agents."
            ) from exc
        return AgentCard, A2AClient, A2AAgentCardAdapter

    @staticmethod
    def _agent_card(agent: AgentName, endpoint: str) -> Any:
        """Convert openjiuwen's card into the a2a-sdk card required by A2AClient."""

        AgentCard, _, A2AAgentCardAdapter = PythonOpenJiuwenDomainAgents._binding()
        card = AgentCard(
            id=f"opportunity_v2_{agent}_agent",
            name=f"V2 {agent.title()} Agent",
            description="Structured V2 domain-agent A2A client contract.",
            interface_url=endpoint,
        )
        converted = A2AAgentCardAdapter.to_a2a_agent_card(
            card,
            interface_url=endpoint,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )
        if converted is None:  # defensive: adapter rejects incompatible card types
            raise DomainAgentUnavailable(f"could not create Python A2A card for {agent}")
        return converted

    async def _client_for(self, agent: AgentName, endpoint: str) -> Any:
        async with self._lock:
            client = self._clients.get(endpoint)
            if client is None:
                _, A2AClient, _ = self._binding()
                client = A2AClient(card=self._agent_card(agent, endpoint))
                # openJiuwen currently constructs ClientConfig() internally,
                # whose HTTPX default read timeout is only five seconds. Keep
                # its transformer/invoke contract but supply a budgeted SDK
                # transport via the public client attribute.
                from a2a.client import ClientConfig, ClientFactory
                import httpx
                await client.stop()
                read_timeout = max(self.timeouts.get(agent, 60.0), research_limit() + 10) if agent == "research" else self.timeouts.get(agent, 60.0)
                transport = httpx.AsyncClient(timeout=httpx.Timeout(read_timeout, connect=10.0))
                try:
                    client.client = ClientFactory(ClientConfig(httpx_client=transport)).create(client.card)
                except BaseException:
                    await transport.aclose()
                    raise
                self._clients[endpoint] = client
            return client

    async def execute(
        self,
        agent: AgentName,
        state: Any,
        missing_task: MissingTask | None = None,
    ) -> ProfileResult | ResearchResult | PlanResult:
        endpoint = self.endpoints.get(agent)
        if not endpoint:
            raise DomainAgentUnavailable(f"no A2A endpoint configured for {agent}")
        request = request_from_state(agent, state, missing_task)
        client = await self._client_for(agent, endpoint)
        timeout = max(self.timeouts.get(agent, 60.0), request.remaining_budget_seconds + 10) if agent == "research" else self.timeouts.get(agent, 60.0)
        try:
            from ..core.progress import relay_progress
            async with relay_progress(request, state):
                with span("a2a.domain_call", agent=agent, run_id=request.run_id, endpoint=endpoint, backend="python"):
                    result = await asyncio.wait_for(
                        client.invoke({"query": request.wire_json(), "conversation_id": request.conversation_id}),
                        timeout=timeout,
                    )
            envelope = _python_envelope(result)
        except DomainAgentUnavailable:
            raise
        except Exception as exc:
            raise DomainAgentUnavailable(f"{agent} Python A2A call failed: {type(exc).__name__}: {exc}") from exc
        if envelope.agent != agent or envelope.run_id != request.run_id or envelope.request_id != request.request_id:
            raise DomainAgentUnavailable(f"{agent} A2A response correlation mismatch")
        return _result_model(agent, envelope.result)

    async def aclose(self) -> None:
        async with self._lock:
            clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.stop()


def _python_envelope(agent_result: Any) -> DomainA2AResponse:
    """Read our final JSON artifact from the Python SDK's aggregate result."""

    for artifact in reversed(getattr(agent_result, "artifacts", []) or []):
        for part in reversed(getattr(artifact, "parts", []) or []):
            text = getattr(part, "text", None)
            if not text:
                continue
            try:
                return DomainA2AResponse.model_validate_json(text)
            except Exception:
                continue
    raise DomainAgentUnavailable("Python A2A response did not contain a V2 structured result artifact")


class OpenJiuwenDomainAgents:
    """Select the configured A2A implementation without leaking it into the Orchestrator."""

    def __init__(
        self,
        endpoints: Mapping[AgentName, str] | None = None,
        timeouts: Mapping[AgentName, float] | None = None,
        backend: str | None = None,
    ) -> None:
        selected = (backend or settings.jiuwen_backend).casefold()
        if selected == "python":
            self._delegate: Any = PythonOpenJiuwenDomainAgents(endpoints, timeouts)
        elif selected == "rust":
            self._delegate = RustOpenJiuwenDomainAgents(endpoints, timeouts)
        else:
            raise ValueError("JIUWEN_BACKEND must be 'python' or 'rust'")

    async def execute(
        self,
        agent: AgentName,
        state: Any,
        missing_task: MissingTask | None = None,
    ) -> ProfileResult | ResearchResult | PlanResult:
        return await self._delegate.execute(agent, state, missing_task)

    async def aclose(self) -> None:
        await self._delegate.aclose()


def _result_model(agent: AgentName, payload: dict[str, Any]) -> ProfileResult | ResearchResult | PlanResult:
    if agent == "profile":
        return ProfileResult.model_validate(payload)
    if agent == "research":
        return ResearchResult.model_validate(payload)
    return PlanResult.model_validate(payload)


_DOMAIN_AGENTS: dict[AgentName, Any] = {
    "profile": ProfileAgent(),
    "research": ResearchAgent(),
    "planning": PlanningAgent(),
}


def execute_domain_request(request: DomainA2ARequest) -> ProfileResult | ResearchResult | PlanResult:
    """Dispatch only; individual Agents own their business implementation."""

    from ...llm_context import conversation_scope
    with conversation_scope(request.conversation_context), span("domain.execute", carrier=request.trace_context,
            agent=request.agent, run_id=request.run_id, task_id=request.request_id):
        return _DOMAIN_AGENTS[request.agent].execute(request)


def _port_for(agent: AgentName) -> int:
    return {"profile": 8771, "research": 8772, "planning": 8773}[agent]


def _interface_url(host: str, port: int, advertised_host: str | None = None) -> str:
    """Separate bind host (often 0.0.0.0) from the URL published in Agent Card."""

    public_host = advertised_host or ("127.0.0.1" if host in {"0.0.0.0", "::"} else host)
    return f"http://{public_host}:{port}/a2a/jsonrpc/"


def create_rust_domain_a2a_server(
    agent: AgentName,
    host: str = "127.0.0.1",
    port: int | None = None,
    advertised_host: str | None = None,
) -> Any:
    """Build a local domain service using the project-built Rust binding."""

    try:
        from openjiuwenrust.core.controller.schema.task import TaskStatus
        from openjiuwenrust.core.single_agent.schema.agent_card import AgentCard
        from openjiuwenrust.core.single_agent.schema.agent_result import AgentResult, Artifact, Part
        from openjiuwenrust.extensions.a2a.a2a_server import A2AServer
    except ImportError as exc:  # pragma: no cover - requires optional binding
        raise DomainAgentUnavailable("openjiuwenrust is required to host Rust V2 A2A agents") from exc

    port = port or _port_for(agent)

    async def invoke_handler(inputs: dict[str, Any]) -> Any:
        try:
            request = DomainA2ARequest.model_validate_json(inputs.get("query", ""))
            if request.agent != agent:
                raise ValueError(f"request targets {request.agent}, not {agent}")
            result = await asyncio.to_thread(execute_domain_request, request)
            response = DomainA2AResponse(
                agent=agent,
                run_id=request.run_id,
                request_id=request.request_id,
                result=result.model_dump(mode="json"),
            )
            return AgentResult(
                status=TaskStatus.COMPLETED,
                artifacts=[Artifact(artifactId=f"v2_{agent}_result", parts=[Part(text=response.model_dump_json())])],
            )
        except Exception as exc:
            logger.warning("V2 %s A2A request rejected: %s", agent, exc)
            return AgentResult(
                status=TaskStatus.FAILED,
                artifacts=[Artifact(
                    artifactId=f"v2_{agent}_error",
                    parts=[Part(text=json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))],
                )],
            )

    display_name = {"profile": "V2 Profile Agent", "research": "V2 Research Agent", "planning": "V2 Planning Agent"}[agent]
    return A2AServer(
        agent_card=AgentCard(id=f"opportunity_v2_{agent}_agent", name=display_name,
                             description=f"Stateless V2 {agent} domain agent with structured results only."),
        adapter_id=f"opportunity-v2-{agent}-a2a",
        invoke_handler=invoke_handler,
        interface_url=_interface_url(host, port, advertised_host),
        protocol_binding="JSONRPC",
        rpc_url="/a2a/jsonrpc/",
    )


class PythonDomainServer:
    """Adapt the SDK's blocking start() to a ready/stop lifecycle."""

    def __init__(self, server):
        self.server, self.task = server, None

    def __getattr__(self, name):
        return getattr(self.server, name)

    async def start(self, **kwargs):
        self.task = asyncio.create_task(self.server.start(**kwargs))
        try:
            async with asyncio.timeout(15):
                while not getattr(getattr(self.server, "_uvicorn_server", None), "started", False):
                    if self.task.done():
                        await self.task
                        raise DomainAgentUnavailable("A2A server exited before becoming ready")
                    await asyncio.sleep(.05)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        await self.server.stop()
        if self.task:
            try:
                await asyncio.wait_for(self.task, 10)
            except TimeoutError:
                self.task.cancel()
            finally:
                self.task = None


def create_python_domain_a2a_server(
    agent: AgentName,
    host: str = "127.0.0.1",
    port: int | None = None,
    advertised_host: str | None = None,
) -> Any:
    """Build a portable domain service with the official openjiuwen Python SDK."""

    try:
        from openjiuwen.core.controller.schema.task import TaskStatus
        from openjiuwen.core.single_agent.schema.agent_card import AgentCard
        from openjiuwen.core.single_agent.schema.agent_result import AgentResult, Artifact, Part
        from openjiuwen.extensions.a2a.a2a_server import A2AServer
    except ImportError as exc:  # pragma: no cover - depends on optional deployment extra
        raise DomainAgentUnavailable(
            "openjiuwen[all-a2a] is required to host Python V2 A2A agents"
        ) from exc

    port = port or _port_for(agent)

    async def invoke_handler(inputs: dict[str, Any]) -> Any:
        try:
            request = DomainA2ARequest.model_validate_json(inputs.get("query", ""))
            if request.agent != agent:
                raise ValueError(f"request targets {request.agent}, not {agent}")
            result = await asyncio.to_thread(execute_domain_request, request)
            response = DomainA2AResponse(
                agent=agent,
                run_id=request.run_id,
                request_id=request.request_id,
                result=result.model_dump(mode="json"),
            )
            return AgentResult(
                status=TaskStatus.COMPLETED,
                artifacts=[Artifact(artifactId=f"v2_{agent}_result", parts=[Part(text=response.model_dump_json())])],
            )
        except Exception as exc:
            logger.warning("V2 %s Python A2A request rejected: %s", agent, exc)
            return AgentResult(
                status=TaskStatus.FAILED,
                artifacts=[Artifact(
                    artifactId=f"v2_{agent}_error",
                    parts=[Part(text=json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))],
                )],
            )

    display_name = {"profile": "V2 Profile Agent", "research": "V2 Research Agent", "planning": "V2 Planning Agent"}[agent]
    return PythonDomainServer(A2AServer(
        agent_card=AgentCard(id=f"opportunity_v2_{agent}_agent", name=display_name,
                             description=f"Stateless V2 {agent} domain agent with structured results only."),
        adapter_id=f"opportunity-v2-{agent}-a2a",
        invoke_handler=invoke_handler,
        interface_url=_interface_url(host, port, advertised_host),
        protocol_binding="JSONRPC",
        rpc_url="/a2a/jsonrpc/",
    ))


def create_domain_a2a_server(
    agent: AgentName,
    host: str = "127.0.0.1",
    port: int | None = None,
    backend: str | None = None,
    advertised_host: str | None = None,
) -> Any:
    """Build one deployable V2 domain service for the selected SDK implementation."""

    selected = (backend or settings.jiuwen_backend).casefold()
    if selected == "python":
        return create_python_domain_a2a_server(agent, host, port, advertised_host)
    if selected == "rust":
        return create_rust_domain_a2a_server(agent, host, port, advertised_host)
    raise ValueError("JIUWEN_BACKEND must be 'python' or 'rust'")


async def run_domain_server(
    agent: AgentName,
    host: str = "127.0.0.1",
    port: int | None = None,
    backend: str | None = None,
    advertised_host: str | None = None,
) -> None:
    if agent == "research" and os.getenv("RESEARCH_WARMUP", "0") == "1":
        from ..rag.models import shared_reranker, shared_embedder
        report = await asyncio.to_thread(shared_reranker().warmup)
        vector = await asyncio.to_thread(shared_embedder().embed, "machine learning curriculum")
        if vector is None or len(vector) != 384:
            raise RuntimeError("Research embedding warmup failed or dimension is not 384")
        print("Research model warmup: " + json.dumps(report), flush=True)
    server = create_domain_a2a_server(agent, host, port, backend, advertised_host)
    actual_port = port or _port_for(agent)
    await server.start(host=host, port=actual_port)
    print(f"V2 {agent} A2A Agent ({backend or settings.jiuwen_backend}): {_interface_url(host, actual_port, advertised_host)}")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop_event.wait()
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one V2 openJiuwen A2A domain agent")
    parser.add_argument("agent", choices=["profile", "research", "planning"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--backend", choices=["python", "rust"], default=settings.jiuwen_backend)
    parser.add_argument("--advertised-host", default=os.getenv("A2A_ADVERTISED_HOST"))
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_domain_server(
        arguments.agent, arguments.host, arguments.port, arguments.backend, arguments.advertised_host,
    ))


if __name__ == "__main__":
    main()
