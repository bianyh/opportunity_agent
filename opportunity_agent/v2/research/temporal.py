"""Keep retrieval freshness distinct from the intake a source actually states."""
import re
from datetime import date

from .identity import normalize_intake
from .task import intake_matches

CURRENT_POLICY_PREFIX = "@current_policy:"


def source_intake(expected: str, text: str) -> str:
    """Return an explicitly stated season/year, or empty for an evergreen page.

    Deadline years and copyright years alone do not identify an admission cycle.
    Multiple cycles are allowed if one matches the requested intake.
    """
    season = r"(?:fall|autumn|spring|summer|秋季|春季|夏季)"
    pattern = rf"(?<!\w)(?:{season}\s*20\d{{2}}|20\d{{2}}\s*{season})(?!\w)"
    stated = list(dict.fromkeys(normalize_intake(m[0]) for m in re.finditer(pattern, text, re.I)))
    matching = [value for value in stated if intake_matches(expected, value)]
    if matching:
        return matching[0]
    if stated and expected:
        raise ValueError("explicit_intake_mismatch")
    return stated[0] if stated else ""


def expired_current_deadline(value: str, expected: str, as_of: date) -> bool:
    year = re.search(r"20\d{2}", expected)
    # Historical queries may legitimately ask about a past deadline.
    return bool(year and int(year[0]) >= as_of.year and date.fromisoformat(value) < as_of)
