"""Run-scoped atomic accounting. Redis failure closes the external-call gate."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
import base64
import zlib

from ..core.config import settings
from ..core.research_budget import execution_limit, school_seconds
from .repair import ToolFailure


class RedisToolLedger:
    def __init__(self, user_id, run_id, schools, inherited=None, client=None):
        from redis.asyncio import Redis
        self.client = client or Redis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1)
        digest = hashlib.sha256(f"{user_id}:{run_id}".encode()).hexdigest()
        self.key = "research:tools:" + digest
        self.ttl = int(execution_limit() + 180 + 3600)
        self.initial = {"version": 1, "tool_limit": min(60, 6 * max(1, schools)),
            "decision_limit": min(20, 2 * max(1, schools)), "tools_used": 0, "decisions_used": 0,
            "schools": {}, "calls": [], "signatures": {}, "circuit": {}, "blocked": False}
        if inherited and inherited.get("version") == 1:
            # A saved snapshot can recover an expired key, never increase its allowance.
            self.initial.update(copy.deepcopy({k: v for k, v in inherited.items() if k != "_progress"}))
            self.initial["tool_limit"] = min(60, self.initial["tool_limit"])
            self.initial["decision_limit"] = min(20, self.initial["decision_limit"])
        self.state = copy.deepcopy(self.initial)

    async def _change(self, mutate):
        from redis.exceptions import WatchError
        try:
            async with asyncio.timeout(2):
                for _ in range(8):
                    async with self.client.pipeline(transaction=True) as pipe:
                        await pipe.watch(self.key)
                        raw = await pipe.get(self.key)
                        state = json.loads(raw) if raw else copy.deepcopy(self.initial)
                        value = mutate(state)
                        pipe.multi()
                        pipe.set(self.key, json.dumps(state), ex=self.ttl)
                        try:
                            await pipe.execute()
                        except WatchError:
                            continue
                        self.state = state
                        return value
            raise RuntimeError("ledger contention")
        except ToolFailure:
            raise
        except Exception:
            raise ToolFailure("LEDGER_UNAVAILABLE") from None

    async def initialize(self):
        await self._change(lambda s: None)

    def public_snapshot(self):
        # Resume text and full URLs stay exclusively in the run-owned Redis record.
        return copy.deepcopy({k: v for k, v in self.state.items() if k != "_progress"})

    def load_progress(self, target_id):
        value = self.state.get("_progress", {}).get(target_id, {})
        if isinstance(value, str):
            try:
                return json.loads(zlib.decompress(base64.b64decode(value)))
            except (ValueError, zlib.error):
                raise ToolFailure("LEDGER_UNAVAILABLE") from None
        return copy.deepcopy(value)

    async def save_progress(self, target_id, progress, *, finished=False, reason=""):
        # Preserve the exact source and its content hash while keeping WATCH records small.
        compressed = base64.b64encode(zlib.compress(json.dumps(progress).encode())).decode()
        def mutate(s):
            s.setdefault("_progress", {})[target_id] = compressed
            s.setdefault("targets", {})[target_id] = {"finished": finished, "reason": reason,
                "pending_count": len(progress.get("pending", []))}
        await self._change(mutate)

    def remaining(self, school):
        entry = self.state["schools"].get(school, {})
        if entry.get("active"):
            return 0.
        return max(0., school_seconds() - entry.get("seconds", 0))

    async def reserve(self, school, *, tool="", signature="", retry=False, extraction_key=""):
        call_id = uuid.uuid4().hex
        def mutate(s):
            entry = s["schools"].setdefault(school, {"tools": 0, "decisions": 0, "seconds": 0})
            if s.get("blocked") or entry.get("active"):
                raise ToolFailure("TOOL_BUDGET_EXHAUSTED")
            if entry["seconds"] >= school_seconds():
                raise ToolFailure("SCHOOL_BUDGET_EXHAUSTED")
            kind, limit = ("tools", 6) if tool else ("decisions", 2)
            total = "tools_used" if tool else "decisions_used"
            cap = "tool_limit" if tool else "decision_limit"
            if entry[kind] >= limit or s[total] >= s[cap]:
                raise ToolFailure("TOOL_BUDGET_EXHAUSTED" if tool else "REPAIR_BUDGET_EXHAUSTED")
            previous = s["signatures"].get(signature, {}) if tool else {}
            if previous.get("failed") and not (retry and previous.get("retryable") and previous.get("count", 0) < 2):
                raise ToolFailure("REPEATED_FAILED_CALL")
            if extraction_key:
                counts = s.setdefault("extractions", {})
                if counts.get(extraction_key, 0) >= 2:
                    raise ToolFailure("PAGE_EXTRACTION_LIMIT")
                counts[extraction_key] = counts.get(extraction_key, 0) + 1
            entry[kind] += 1
            s[total] += 1
            entry["active"] = {"id": call_id, "started": time.time()}
            if tool:
                s["signatures"][signature] = {**previous, "count": previous.get("count", 0) + 1}
            return call_id
        return await self._change(mutate)

    async def finish(self, school, call_id, elapsed, observation=None, signature=""):
        def mutate(s):
            entry = s["schools"][school]
            if entry.get("active", {}).get("id") != call_id:
                return  # repeated completion is not charged twice
            entry.pop("active", None)
            entry["seconds"] += max(0, elapsed)
            if observation:
                s["calls"].append(observation)
                s["calls"] = s["calls"][-60:]
                if observation["status"] != "ok":
                    s["signatures"][signature].update(failed=True, retryable=observation["retryable"])
        await self._change(mutate)

    async def circuit(self, channel, operation, *, minimum_remaining=0):
        def mutate(s):
            circuit = s["circuit"].setdefault(channel, {"failures": 0, "opened_at": 0, "probe_used": False})
            if operation == "check" and circuit["failures"] >= 3:
                if circuit["probe_used"] or time.time() - circuit["opened_at"] < 30 or minimum_remaining < 30:
                    raise ToolFailure("EXTRACTION_CIRCUIT_OPEN")
                circuit["probe_used"] = True
            elif operation == "ok":
                circuit["failures"] = 0
            elif operation == "failure":
                circuit["failures"] += 1
                if circuit["failures"] == 3:
                    circuit["opened_at"] = time.time()
        await self._change(mutate)

    async def block(self):
        await self._change(lambda s: s.update(blocked=True))

    async def close(self):
        await self.client.aclose()
