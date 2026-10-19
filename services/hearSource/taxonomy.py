"""Allowed Hear Source values, and the guardrails around them.

Hear Source is a closed list - it is enforced on the OpenAI request itself as a
JSON-schema ``enum``, so the model physically cannot return anything else.

Hear Source Detail is deliberately NOT a closed list. Per the system prompt it
is a normalized name (an event, a publication, a person, a community), so the
set is open-ended. What we do enforce is the small number of hard rules the
prompt states: the details that must always be blank, and the two sources whose
detail IS a closed list.

The classification rules themselves live in ``system_prompt.md`` - edit that
file, not this one, to change how text is categorised.
"""
import os
import re

# ---------------------------------------------------------------------------
# Hear Source - the enum sent to OpenAI. Order matches system_prompt.md §1.
# ---------------------------------------------------------------------------
HEAR_SOURCES: list[str] = [
    "Organic Search",
    "Paid Search",
    "Paid Social",
    "Display",
    "Organic Social",
    "AI Assistant",
    "Referral",
    "Event",
    "Sales",
    "Unknown",
]

# Sources whose detail must always be blank (system_prompt.md §2).
DETAIL_ALWAYS_BLANK: set[str] = {"Unknown"}

# Sources whose detail IS a closed list (system_prompt.md §2).
DETAIL_CLOSED_LIST: dict[str, list[str]] = {
    "Sales": ["Call", "Email", "LinkedIn"],
}

# ---------------------------------------------------------------------------
# Detail aliases - make known names deterministic
# ---------------------------------------------------------------------------
# The model normalizes spelling well but not consistently: the same input can
# come back as "Marketo Nation" on one run and "Marketo Community" on the next,
# which splits your reporting across two picklist values. Anything listed here
# is forced to the canonical spelling after the model answers.
#
# Matching ignores case, spaces and punctuation, so "Marketo Nation",
# "marketo nation" and "MARKETO-NATION" all resolve from one variant. Add a line
# whenever the Sheets log shows a name arriving in more than one spelling.
#
#   "Canonical value": ["variant", "variant", ...]
DETAIL_ALIASES: dict[str, list[str]] = {
    # Publications / communities
    "Marketo Community": ["marketo nation", "nation", "marketo community forum"],
    "G2": ["g 2", "g2 crowd", "g2crowd"],
    # Social platforms
    "LinkedIn": ["linked in", "linkden", "linkdin", "li", "linkedin post"],
    "X": ["x twitter", "twitter", "tweet", "x formerly twitter"],
    "YouTube": ["you tube", "yt"],
    # Search engines
    "Google": ["google search", "google com", "googled", "goggle"],
    "DuckDuckGo": ["duck duck go", "ddg"],
    # Paid
    "Google Ad": ["google ads", "google adwords", "adwords", "google paid search"],
    "Bing Ad": ["bing ads", "microsoft ads", "microsoft advertising"],
    # AI assistants
    "ChatGPT": ["chat gpt", "gpt", "open ai", "openai", "chatgpt com"],
    "Microsoft Copilot": ["copilot", "ms copilot", "microsoft co pilot"],
    # Referral shorthands
    "WOM": ["word of mouth", "colleague", "coworker", "co worker", "friend",
            "peer", "referral", "word of mouth referral"],
    # Events
    "Adobe Summit": ["summit", "adobe summit 2026", "adobe summit booth"],
    "GURU Conference": ["guru", "guru conf", "guru conference 2026"],
    "INBOUND Conference": ["inbound", "inbound conf", "hubspot inbound"],
    # Sales channels (also enforced by DETAIL_CLOSED_LIST below)
    "Email": ["e mail", "emailed", "outbound email", "cold email"],
    "Call": ["phone", "phone call", "cold call", "called me"],
}

# Placeholder text the prompt forbids in the detail - coerced to blank.
FORBIDDEN_DETAILS: set[str] = {
    "unknown", "unspecified", "none", "n/a", "na", "blank", "not specified",
    "not given", "no detail", "-", "--",
}

# Where anything unusable lands.
FALLBACK_SOURCE = "Unknown"
FALLBACK_DETAIL = ""

SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "system_prompt.md")


def _alias_key(value: str) -> str:
    """Collapse a detail to a comparison key: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


# Flattened lookup, built once. Canonical values map to themselves so the
# normalisation is idempotent.
_ALIAS_LOOKUP: dict[str, str] = {}


def _build_alias_lookup() -> None:
    _ALIAS_LOOKUP.clear()
    for canonical, variants in DETAIL_ALIASES.items():
        _ALIAS_LOOKUP[_alias_key(canonical)] = canonical
        for variant in variants:
            _ALIAS_LOOKUP.setdefault(_alias_key(variant), canonical)


def normalize_detail(detail: str) -> str:
    """Force a known detail to its canonical spelling; pass anything else through."""
    if not detail:
        return ""
    if not _ALIAS_LOOKUP:
        _build_alias_lookup()
    return _ALIAS_LOOKUP.get(_alias_key(detail), detail.strip())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def json_schema() -> dict:
    """Strict JSON schema for the OpenAI structured-output request.

    ``hear_source`` is an enum, so an off-taxonomy value is impossible.
    """
    return {
        "name": "hear_source_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "hear_source": {
                    "type": "string",
                    "description": "The discovery channel. Must be one of the allowed values.",
                    "enum": HEAR_SOURCES,
                },
                "hear_source_detail": {
                    "type": "string",
                    "description": "Normalized specific name of the source, or an empty string when there is no detail beyond the hear_source.",
                },
            },
            "required": ["hear_source", "hear_source_detail"],
            "additionalProperties": False,
        },
    }


def validate(source: str, detail: str) -> tuple[str, str, bool]:
    """Apply the prompt's hard detail rules. Returns (source, detail, was_clean)."""
    source = (source or "").strip()
    detail = (detail or "").strip()

    if source not in HEAR_SOURCES:
        # Should be unreachable while the enum is enforced, but never write an
        # unrecognised value onto a lead record.
        return FALLBACK_SOURCE, FALLBACK_DETAIL, False

    if detail.lower() in FORBIDDEN_DETAILS:
        return source, "", False

    # Force known names to one spelling so reporting does not split.
    detail = normalize_detail(detail)

    if source in DETAIL_ALWAYS_BLANK:
        return source, "", detail == ""

    allowed = DETAIL_CLOSED_LIST.get(source)
    if allowed is not None and detail and detail not in allowed:
        # Right source, detail outside its closed list - keep the source, drop
        # the detail rather than writing something the picklist has no room for.
        return source, "", False

    return source, detail, True


def load_system_prompt() -> str:
    with open(SYSTEM_PROMPT_FILE, encoding="utf-8") as f:
        return f.read().strip()


def sources_in_prompt() -> list[str]:
    """Pull the Hear Source values out of §1 of system_prompt.md.

    Used by self_check to catch the prompt and the enum drifting apart.
    """
    text = load_system_prompt()
    match = re.search(r"^## 1\..*?$(.*?)^## ", text, re.S | re.M)
    section = match.group(1) if match else ""
    return re.findall(r"^-\s+\*\*(.+?)\*\*", section, re.M)


def self_check() -> list[str]:
    """Validate the configuration at request time. Returns a list of problems."""
    problems = []

    if FALLBACK_SOURCE not in HEAR_SOURCES:
        problems.append(f'FALLBACK_SOURCE "{FALLBACK_SOURCE}" is not in HEAR_SOURCES')
    if len(set(HEAR_SOURCES)) != len(HEAR_SOURCES):
        problems.append("HEAR_SOURCES contains duplicates")
    for source in list(DETAIL_ALWAYS_BLANK) + list(DETAIL_CLOSED_LIST):
        if source not in HEAR_SOURCES:
            problems.append(f'"{source}" has detail rules but is not in HEAR_SOURCES')

    _build_alias_lookup()
    for canonical, variants in DETAIL_ALIASES.items():
        for variant in variants:
            if _ALIAS_LOOKUP.get(_alias_key(variant)) != canonical:
                problems.append(
                    f'alias "{variant}" is claimed by both "{canonical}" and '
                    f'"{_ALIAS_LOOKUP[_alias_key(variant)]}"')

    if not os.path.exists(SYSTEM_PROMPT_FILE):
        problems.append("system_prompt.md is missing")
        return problems

    in_prompt = sources_in_prompt()
    if not in_prompt:
        problems.append("Could not parse the Hear Source list out of system_prompt.md §1")
    else:
        missing = [s for s in HEAR_SOURCES if s not in in_prompt]
        extra = [s for s in in_prompt if s not in HEAR_SOURCES]
        if missing:
            problems.append(f"In HEAR_SOURCES but not in system_prompt.md §1: {missing}")
        if extra:
            problems.append(f"In system_prompt.md §1 but not in HEAR_SOURCES: {extra}")

    return problems
