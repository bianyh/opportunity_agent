"""Conservative checks between an extracted value and its exact source quote.

Dates are recognised by one shared grammar (ISO, "Feb. 1, 2027", "1 February",
"2/1/2027", "2027年2月1日", "2月1日").  A date without a year is resolved only
from the requested intake cycle, never from today's date or a copyright line:
Fall/Summer Y deadlines from August onward belong to Y-1, earlier ones to Y;
Spring Y deadlines from June onward belong to Y-1.
"""
import re
from datetime import date

_MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
           "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
           "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
           "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}
_MONTH = r"(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>20\d{2})"
# Ordered: patterns carrying a year win over overlapping year-less ones.
_DATE_PATTERNS = (
    re.compile(r"\b(?P<year>20\d{2})-(?P<month>0[1-9]|1[0-2])-(?P<day>[0-3]\d)\b"),
    re.compile(r"(?P<year>20\d{2})\s*年\s*(?P<month>1[0-2]|0?[1-9])\s*月\s*(?P<day>[0-3]?\d)\s*日"),
    re.compile(r"\b(?P<month>1[0-2]|0?[1-9])/(?P<day>[0-3]?\d)/(?P<year>20\d{2})\b"),
    re.compile(rf"\b{_MONTH}\s+{_DAY}\s*,?\s+{_YEAR}\b", re.I),
    re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}\s*,?\s+{_YEAR}\b", re.I),
    re.compile(rf"\b{_MONTH}\s+{_DAY}\b(?!\s*[,/]?\s*20\d{{2}})(?!\s*[:]\d)", re.I),
    re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}(?![a-z])(?!\s*,?\s*20\d{{2}})", re.I),
    re.compile(r"(?<![年\d])(?P<month>1[0-2]|0?[1-9])\s*月\s*(?P<day>[0-3]?\d)\s*日"),
)
_DEADLINE_TERMS = re.compile(r"deadline|due\b|\bclose[sd]?\b|apply by|submitted by|received by|"
                             r"no later than|截止|截至", re.I)
_EARLY_TERMS = re.compile(r"\b(?:early|priority|preferred|first[- ]round|round 1|scholarship|"
                          r"fellowship|funding|financial aid|assistantship)\b", re.I)
_SEASONS = {"fall": re.compile(r"\b(?:fall|autumn)\b|秋季", re.I),
            "spring": re.compile(r"\bspring\b|春季", re.I),
            "summer": re.compile(r"\bsummer\b|夏季", re.I)}
_GRE_POLICIES = (
    ("not_accepted", re.compile(r"not (?:be )?accepted|do(?:es)? not accept|will not (?:be )?(?:consider|review)|不接受", re.I)),
    ("not_required", re.compile(r"not required|no longer required|no gre|waived for all|无需|不要求|不需要", re.I)),
    ("optional", re.compile(r"optional|可选|选交", re.I)),
    ("required", re.compile(r"required|must (?:submit|take|provide)|必须|要求", re.I)),
)
# Sentence ends at . ! ? ; or 。 but not after month/title abbreviations.
_ABBREVIATION = re.compile(r"(?:\b(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True))
                           + r"|e\.g|i\.e|u\.s|dr|mr|ms|no|vs|approx|incl)\.)$", re.I)


def _cycle_year(month, intake):
    """Year of a year-less deadline for the intake, or None when unknowable."""
    year = re.search(r"20\d{2}", intake or "")
    if not year:
        return None
    year = int(year[0])
    if _SEASONS["spring"].search(intake):
        return year - 1 if month >= 6 else year
    if _SEASONS["fall"].search(intake) or _SEASONS["summer"].search(intake):
        return year - 1 if month >= 8 else year
    return None  # A year-only intake does not fix which cycle a bare date belongs to.


def date_mentions(text, intake=""):
    """[(start, end, iso_or_None)] for every date-like span, non-overlapping."""
    found = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end, _ in found):
                continue
            month = match["month"]
            month = int(month) if month.isdigit() else _MONTHS[month.casefold().rstrip(".")]
            day = int(match["day"])
            year = int(match["year"]) if "year" in match.groupdict() and match["year"] else _cycle_year(month, intake)
            try:
                value = date(year, month, day).isoformat() if year else None
            except ValueError:
                continue
            found.append((match.start(), match.end(), value))
    return sorted(found)


def _extract_dates_from_text(text, intake=""):
    return list(dict.fromkeys(v for _, _, v in date_mentions(text, intake) if v))


def _gre_policy(text):
    for policy, pattern in _GRE_POLICIES:
        if pattern.search(text):
            return policy
    return None


def supported_value(field, value, quote, intake=""):
    lower = quote.casefold()
    if field == "gre_policy":
        return bool(re.search(r"\bgre\b", lower)) and _gre_policy(quote) == value
    if field == "deadline":
        return bool(_DEADLINE_TERMS.search(quote)) and value in _extract_dates_from_text(quote, intake)
    if field == "language":
        if not re.search(r"toefl|ielts|duolingo|english proficiency|english language|托福|雅思", lower):
            return False
        if value == "not_required":
            return bool(re.search(r"not required|no (?:english )?(?:test|proficiency)|不要求|无需", lower))
        if value == "optional":
            return bool(re.search(r"optional|可选", lower))
        if value == "conditional":
            return bool(re.search(r"waiv|exempt|豁免|not required.{0,100}(?:for|if)|unless", lower))
        if value == "required":
            return bool(re.search(r"required|must|need(?:ed)? to|proof of english|demonstrate english|要求|必须", lower))
        return False
    return bool(value and value in quote)


def _sentences(text, limit=400):
    """Exact (start, end) spans; long run-on blocks are cut so quotes stay short."""
    start = 0
    for match in re.finditer(r"[.!?;。！？；](?=\s|$)|\n", text):
        if match.group() == "." and _ABBREVIATION.search(text[max(0, match.start()-8):match.end()]):
            continue
        yield from _chunks(text, start, match.end(), limit)
        start = match.end()
    yield from _chunks(text, start, len(text), limit)


def _chunks(text, start, end, limit):
    while start < end:
        while start < end and text[start].isspace():
            start += 1
        stop = min(end, start + limit)
        if stop < end:  # break on whitespace, keep words whole
            space = text.rfind(" ", start, stop)
            stop = space if space > start else stop
        if stop > start:
            yield start, stop
        start = stop


def _other_cycle(sentence, intake):
    """True if the sentence names only a different season or year than requested."""
    wanted = [s for s, p in _SEASONS.items() if p.search(intake or "")]
    named = [s for s, p in _SEASONS.items() if p.search(sentence)]
    if wanted and named and wanted[0] not in named:
        return True
    years = set(re.findall(r"(?:fall|autumn|spring|summer)\s*(20\d{2})|(20\d{2})\s*(?:fall|autumn|spring|summer)",
                           sentence, re.I))
    years = {a or b for a, b in years}
    target = re.search(r"20\d{2}", intake or "")
    return bool(target and years and target[0] not in years)


_OTHER_DEGREES = re.compile(r"\b(?:ph\.?\s?d|doctoral|doctorate|undergraduate|bachelor'?s?|mba)\b", re.I)
_MASTERS = re.compile(r"\b(?:master'?s?\b|m\.?s\b|mcs\b|mscs\b|m\.?eng\b)", re.I)


def keyword_facts(text, fields, intake=""):
    """Deterministic deadline/GRE facts from keyword windows of the page text.

    Returns only unambiguous facts as {field, value, quote}, where quote is an
    exact substring of text that passes supported_value.  Anything ambiguous
    (several regular deadlines, conflicting GRE statements, a year-less date
    for a year-only intake) is left for the model extractor.
    """
    spans = [(a, b, text[a:b]) for a, b in _sentences(text)]
    facts = []
    if "deadline" in fields:
        regular, early = {}, {}
        for _, _, sentence in spans:
            if not _DEADLINE_TERMS.search(sentence) or _other_cycle(sentence, intake):
                continue
            if _OTHER_DEGREES.search(sentence) and not _MASTERS.search(sentence):
                continue
            values = {v for _, _, v in date_mentions(sentence, intake)}
            if len(values) != 1 or None in values:
                continue  # Several dates in one sentence need the model's reading.
            value = values.pop()
            bucket = early if _EARLY_TERMS.search(sentence) else regular
            bucket.setdefault(value, sentence.strip())
        if len(regular) == 1:
            value, quote = next(iter(regular.items()))
            if supported_value("deadline", value, quote, intake):
                facts.append({"field": "deadline", "value": value, "quote": quote})
    if "gre_policy" in fields:
        found = {}
        for _, _, sentence in spans:
            if sentence.rstrip().endswith(("?", "？")):
                continue  # "Is the GRE required?" asks, it does not state a policy.
            if re.search(r"\bGRE\b", sentence) and (policy := _gre_policy(sentence)):
                if _OTHER_DEGREES.search(sentence) and not _MASTERS.search(sentence):
                    continue
                found.setdefault(policy, sentence.strip())
        if len(found) == 1:
            value, quote = next(iter(found.items()))
            if supported_value("gre_policy", value, quote):
                facts.append({"field": "gre_policy", "value": value, "quote": quote})
    return facts
