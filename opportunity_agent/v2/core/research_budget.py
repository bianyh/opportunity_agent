"""Shared bounded budgets: a school allowance is not a whole-query timeout."""
from __future__ import annotations

import os
import re


def seconds(name, default, maximum=3600):
    try:
        value = float(os.getenv(name, str(default)))
        if not 0 < value <= maximum:
            return float(default)
        return value
    except ValueError:
        return float(default)


def school_seconds():
    return seconds("RESEARCH_PER_SCHOOL_SECONDS", 90 if os.getenv("RESEARCH_TOOL_REPAIR_ENABLED", "0") == "1" else 60, 600)


def research_limit():
    # The old setting remains an explicit hard cap, not the default budget.
    return seconds("RESEARCH_BUDGET_SECONDS", seconds("RESEARCH_MAX_BUDGET_SECONDS", 600))


def research_seconds(school_count):
    return min(research_limit(), max(55., seconds("RESEARCH_OVERHEAD_SECONDS", 15)
                                   + school_seconds() * max(1, school_count)))


def estimate_school_count(message, required_count=None):
    from ...official_research import OfficialDomainRegistry
    lowered = message.casefold()
    schools = set()
    for item in OfficialDomainRegistry().items:
        for alias in [item["name"], *item.get("aliases", [])]:
            alias = alias.casefold()
            matched = bool(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", lowered)) if alias.isascii() else alias in lowered
            if matched:
                schools.add(item["name"])
                break
    if schools:
        return len(schools)
    # Broad discovery has no named-school count yet. Research refines this from
    # catalogue candidates; the caller must allow enough time to discover them.
    return max(int(seconds("RESEARCH_DISCOVERY_SCHOOL_COUNT", 10, 200)), required_count or 0)


def execution_limit():
    return seconds("EXECUTION_MAX_BUDGET_SECONDS", 1800)


def execution_seconds(message, rounds=3, required_count=None):
    return min(execution_limit(), seconds("EXECUTION_OVERHEAD_SECONDS", 180)
               + rounds * research_seconds(estimate_school_count(message, required_count)))


def synthesis_reserve():
    return seconds("SYNTHESIZER_BUDGET_SECONDS", 90) + 5
