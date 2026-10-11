"""Opt-in real Research smoke: no account, conversation or knowledge writes."""
import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if (Path.cwd() / "opportunity_agent" / "v2").is_dir():
    sys.path.insert(0, str(Path.cwd()))


async def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--query", default="查询2027 Fall美国计算机硕士项目，筛选GRE不要求的项目，列出申请截止日期。")
    args = parser.parse_args()
    if not 0 < args.seconds <= 600:
        parser.error("--seconds must be between 0 and 600")
    # These overrides apply only to this isolated process.
    os.environ.update(RESEARCH_TOOL_REPAIR_ENABLED="1", RESEARCH_PER_SCHOOL_SECONDS="90",
        RESEARCH_PERSIST_FACTS="0", RESEARCH_QUEUE_INGEST="0", RESEARCH_PROGRESS_ENABLED="0")
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from opportunity_agent.v2.core.config import settings
    from opportunity_agent.v2.core.telemetry import span
    from opportunity_agent.v2.research.service import ResearchService
    from opportunity_agent.v2.agents.a2a import DomainA2ARequest
    from opportunity_agent.v2.agents.orchestrator import HeuristicGoalParser, CustomOrchestrator
    from opportunity_agent.v2.agents.contracts import ExecutionState, RouteDecision
    from opportunity_agent.v2.research.failures import run_failure_report
    run_id, owner = uuid.uuid4().hex, "tool-repair-smoke"
    engine = create_async_engine(settings.database_url)
    started = time.monotonic()
    try:
        criteria = await HeuristicGoalParser().parse(args.query)
        request = DomainA2ARequest(agent="research", user_id=owner, conversation_id=run_id,
            run_id=run_id, request_id=run_id, message=args.query, success_criteria=criteria,
            remaining_budget_seconds=args.seconds)
        state = ExecutionState(user_id=owner, conversation_id=run_id, run_id=run_id,
            request_id=run_id, message=args.query, success_criteria=criteria,
            route_decision=RouteDecision(mode="delegate", agents=["research"], reason="Isolated Research smoke", parallel=False))
        with span("research.tool_repair.smoke", run_id=run_id) as root:
            trace_id = format(root.get_span_context().trace_id, "032x")
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                result = await ResearchService(session).execute(request)
        state.research_result = result
        state.completion = CustomOrchestrator()._check_completion(state)
        if state.completion.status == "RETRY":
            state.completion.status = "PARTIAL"  # Smoke never runs task-level repair rounds.
        ledger = result.diagnostics.get("tool_execution", {})
        print(json.dumps({"smoke_run_id": run_id, "trace_id": trace_id, "seconds": round(time.monotonic()-started, 2),
            "completion": state.completion.status, "programs": len(result.programs),
            "tools_used": ledger.get("tools_used"), "tool_limit": ledger.get("tool_limit"),
            "decisions_used": ledger.get("decisions_used"),
            "errors": sorted({e.get("code", "") for e in result.errors}),
            "repairs": result.diagnostics.get("repair_history", []),
            "diagnostics": run_failure_report(state)}, ensure_ascii=False))
    finally:
        await engine.dispose()
        from redis.asyncio import Redis
        client = Redis.from_url(settings.redis_url)
        try:
            key = "research:tools:" + hashlib.sha256(f"{owner}:{run_id}".encode()).hexdigest()
            await client.delete(key)  # Exact unique smoke key, never shared user records.
        finally:
            await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
