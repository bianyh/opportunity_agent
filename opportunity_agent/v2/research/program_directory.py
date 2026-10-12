"""School-local programme identities and official navigation, never policy facts.

Entries only help find the source. Dates and requirements must still be read
from the live page and pass the normal source-bound verifier.
"""
import re

from .identity import canonical_school, program_aliases


PROGRAM_DIRECTORY = (
    {
        "university": "Carnegie Mellon University",
        "program": "Master of Science in Artificial Intelligence and Innovation",
        "abbreviation": "MSAII",
        "urls": ("https://msaii.cs.cmu.edu/apply-msaii-program",),
    },
)


def registered_program(university, name):
    for entry in PROGRAM_DIRECTORY:
        if (canonical_school(university) == canonical_school(entry["university"])
                and name.casefold() in {a.casefold() for a in program_aliases(entry["program"])}):
            return entry
    return None


def visual_program_mentions(query, university):
    """Resolve I/l/1 confusables only against one school's known acronyms.

    Do not apply global spelling replacements, edit-distance guesses or infer
    that a similarly named programme at another school is the same degree.
    """
    tokens = re.findall(r"(?<![a-zA-Z0-9])[a-zA-Z][a-zA-Z0-9]{3,}(?![a-zA-Z0-9])", query)
    visual_key = lambda s: s.casefold().translate(str.maketrans({"i": "l", "1": "l"}))
    found = []
    for token in tokens:
        candidates = [entry for entry in PROGRAM_DIRECTORY
                      if canonical_school(university) == canonical_school(entry["university"])
                      and visual_key(token) == visual_key(entry["abbreviation"])]
        if len(candidates) == 1 and token.casefold() != candidates[0]["abbreviation"].casefold():
            found.append({"input": token, "resolved": candidates[0]["abbreviation"],
                          "university": candidates[0]["university"], "method": "school_local_visual_alias"})
    return found


def navigation_entries(query):
    """Return identities whose exact alias appears in a retrieval query."""
    for entry in PROGRAM_DIRECTORY:
        if any(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", query, re.I)
               for alias in program_aliases(entry["program"])):
            yield entry
