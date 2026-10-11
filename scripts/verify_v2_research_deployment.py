"""Read-only catalogue/OTLP smoke; optionally create an isolated API test account."""
import argparse
import asyncio
import json
import secrets
import time
import uuid
from pathlib import Path

import httpx
from opportunity_agent.v2.core.research_budget import execution_limit


async def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--jaeger-url", default="http://localhost:16686")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-timeout", type=float, default=execution_limit() + 180,
                        help="Maximum seconds to wait for a dynamically budgeted Run")
    args = parser.parse_args()
    if args.run_timeout <= 0:
        parser.error("--run-timeout must be positive")
    report = {}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30, trust_env=False) as client:
        identity = uuid.uuid4().hex
        response = await client.post("/api/v1/auth/register", json={
            "email": "routing-" + identity + "@example.test", "password": secrets.token_urlsafe(24)})
        response.raise_for_status()
        conversation = await client.post("/api/v1/conversations", json={"title": "GRE hypothetical routing verification"})
        conversation.raise_for_status()
        response = await client.post("/api/v1/conversations/" + conversation.json()["id"] + "/runs",
            json={"message": "如果我不考虑GRE，会有什么影响？", "request_id": identity})
        response.raise_for_status()
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + args.run_timeout
        while time.monotonic() < deadline:
            response = await client.get("/api/v1/runs/" + run_id)
            response.raise_for_status()
            run = response.json()
            if run["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(1)
        if run["status"] not in {"completed", "failed"}:
            raise TimeoutError(f"Run {run_id} did not finish within --run-timeout")
        events = await client.get("/api/v1/runs/" + run_id + "/events", timeout=args.run_timeout)
        events.raise_for_status()
        route = None
        for block in events.text.split("\n\n"):
            if "event: route_selected" in block:
                data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), "{}")
                route = json.loads(data).get("payload", {})
        preferences = await client.get("/api/v1/memory/preferences")
        preferences.raise_for_status()
        research = run.get("research_result") or {}
        report = {"run_id": run_id, "status": run["status"], "trace_id": run.get("trace_id"),
            "route": route, "completion": run.get("completion"), "research_route": research.get("route"),
            "research_history": research.get("route_history"), "diagnostics": research.get("diagnostics"),
            "errors": research.get("errors"), "answer": run.get("answer"),
            "saved_preferences": preferences.json()}
        if run.get("trace_id"):
            async with httpx.AsyncClient(timeout=10, trust_env=False) as jaeger:
                for _ in range(15):
                    response = await jaeger.get(args.jaeger_url + "/api/v3/traces/" + run["trace_id"])
                    response.raise_for_status()
                    envelopes = [json.loads(line) for line in response.text.splitlines() if line.strip()]
                    resources = [rs for envelope in envelopes for rs in envelope.get("result", {}).get("resourceSpans", [])]
                    spans = [s for rs in resources for scope in rs.get("scopeSpans", []) for s in scope.get("spans", [])]
                    if {"agent.run", "domain.execute", "research.execute", "sql.retrieve"} <= {s["name"] for s in spans}:
                        break
                    await asyncio.sleep(1)
                report["otel"] = {"found": bool(spans), "span_count": len(spans),
                    "operations": sorted({s["name"] for s in spans})}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report.get(k) for k in ("run_id", "status", "trace_id", "route", "research_route", "otel")}, ensure_ascii=False))
    assert report["status"] == "completed", report.get("errors")
    assert report["route"] and "research" in report["route"]["agents"]
    assert "profile" not in report["route"]["agents"]
    assert report["research_route"] == "sql"
    assert report["saved_preferences"] == []
    assert report.get("otel", {}).get("found")
    assert {"agent.run", "domain.execute", "research.execute", "sql.retrieve"} <= set(report["otel"]["operations"])


if __name__ == "__main__":
    asyncio.run(main())
