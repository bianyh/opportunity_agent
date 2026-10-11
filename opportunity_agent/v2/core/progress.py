"""Best-effort, request-scoped Research progress; never a source of agent results."""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress

from .config import settings


STAGES = {"parse", "sql", "search", "read", "read_fallback", "extract", "school_done", "extraction_unavailable",
          "repair_analyze", "repair_adjust", "repair_validate", "repair_budget_exhausted"}


def progress_key(channel):
    if not re.fullmatch(r"[a-f0-9]{32}", channel or ""):
        return None
    return "research:progress:" + channel


def safe_progress(payload):
    if not isinstance(payload, dict) or payload.get("stage") not in STAGES:
        return None
    result = {"stage": payload["stage"]}
    for key in ("school", "program", "error_code", "tool"):
        if isinstance(payload.get(key), str):
            result[key] = payload[key][:180]
    for key in ("current", "total", "tools_used", "tool_limit"):
        if isinstance(payload.get(key), int) and not isinstance(payload[key], bool):
            result[key] = max(0, min(1000, payload[key]))
    return result


class ResearchProgress:
    def __init__(self, channel):
        self.key = progress_key(channel) if os.getenv("RESEARCH_PROGRESS_ENABLED", "0") == "1" else None
        self.client = None

    async def emit(self, stage, **payload):
        data = safe_progress(dict(stage=stage, **payload))
        if not self.key or not data:
            return
        try:
            if self.client is None:
                from redis.asyncio import Redis
                self.client = Redis.from_url(settings.redis_url, socket_connect_timeout=.3, socket_timeout=.3)
            async with asyncio.timeout(.4):
                batch = self.client.pipeline(transaction=True)
                batch.xadd(self.key, {"data": json.dumps(data, ensure_ascii=False)}, maxlen=500, approximate=False)
                batch.expire(self.key, 3600)
                await batch.execute()  # TTL and append commit together.
        except Exception:
            self.key = None  # Unavailable UI transport must not stop research.

    async def close(self):
        if self.client:
            with suppress(Exception):
                await self.client.aclose()


@asynccontextmanager
async def relay_progress(request, state):
    """Read a unique invocation stream, including its tail before agent_completed."""
    key = progress_key(getattr(request, "progress_channel", ""))
    if not key:
        yield
        return
    from redis.asyncio import Redis
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=.3, socket_timeout=.5)
    cursor = "0-0"
    async def read(block=None):
        nonlocal cursor
        rows = await client.xread({key: cursor}, count=100, block=block)
        for _, entries in rows:
            for sequence, fields in entries:
                cursor = sequence.decode() if isinstance(sequence, bytes) else sequence
                try:
                    raw = fields.get(b"data", fields.get("data", "{}"))
                    if len(raw) > 2048:
                        continue
                    data = safe_progress(json.loads(raw))
                    if data:
                        state.add_event("research_progress", round_id=request.round_id, **data)
                except (ValueError, TypeError):
                    continue
        return bool(rows)
    async def watch():
        try:
            while True:
                if not await read(200):
                    await asyncio.sleep(.02)
        except Exception:
            return
    watcher = asyncio.create_task(watch())
    try:
        yield
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        with suppress(Exception):
            async with asyncio.timeout(.5):
                while await read():
                    pass
        with suppress(Exception):
            await client.aclose()
