"""Normalize registered university aliases without conflating distinct programmes."""
from functools import lru_cache
import re

from ...official_research import OfficialDomainRegistry


# Degree-level synonyms only. Department overlap does not make MSCS, MCS,
# CSE or ECE interchangeable, unlike broad webpage relevance matching.
_PROGRAM_ALIASES = {
    "MSCS": ("MSCS", "Master of Science in Computer Science", "MS in Computer Science", "M.S. in Computer Science",
             "MSc CS", "MSc in Computer Science", "MS CS"),
    "MCS": ("MCS", "Master of Computer Science"),
    "CSE": ("CSE", "MS CSE", "Master of Science in Computer Science and Engineering",
            "Master of Science in Computer Science & Engineering"),
    "MSML": ("MSML", "Master of Science in Machine Learning"),
    "MSAII": ("MSAII", "Master of Science in Artificial Intelligence and Innovation"),
}


def program_aliases(name):
    if name.strip().casefold() in {"cse", "ms cse"}:
        # CSE can mean Computational Science and Engineering, among others.
        # Preserve the query; official pages establish its school-local meaning.
        return (name.strip(),)
    for aliases in _PROGRAM_ALIASES.values():
        if name.strip().casefold() in {a.casefold() for a in aliases}:
            return aliases
    return (name,)


def canonical_program(name):
    return program_aliases(name)[0].strip().casefold()


@lru_cache(maxsize=1024)
def school_aliases(name):
    registry = OfficialDomainRegistry()
    record = registry.resolve(name)
    if not record:
        return (name,)
    for item in registry.items:
        if item.get("name") == record["university"]:
            return tuple(dict.fromkeys([item["name"], *item.get("aliases", [])]))
    return (name,)


def canonical_school(name):
    return school_aliases(name)[0].strip().casefold()


def normalize_intake(value):
    """Canonicalize only explicit year/season information; never infer Fall."""
    year = re.search(r"20\d{2}", value)
    if not year:
        return value.strip()
    for season, pattern in (("Fall", r"(?<![a-z])(?:fall|autumn)(?![a-z])|秋季"),
                            ("Spring", r"(?<![a-z])spring(?![a-z])|春季"),
                            ("Summer", r"(?<![a-z])summer(?![a-z])|夏季")):
        if re.search(pattern, value, re.I):
            return year[0] + " " + season
    return year[0]


def intake_aliases(value):
    value = normalize_intake(value)
    parts = value.split()
    if len(parts) == 2:
        aliases = [value, parts[1] + " " + parts[0]]
        if parts[1] == "Fall":
            aliases += [parts[0] + " Autumn", "Autumn " + parts[0]]
        return aliases
    return [value]
