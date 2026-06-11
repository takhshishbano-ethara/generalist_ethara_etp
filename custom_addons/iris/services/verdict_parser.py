"""Deterministic verdict/recommendation parsing for Iris LLM outputs.

Pure-Python module (no Odoo imports). The LLM is instructed (see
``prompts/SCREENING.md`` and ``prompts/SCORECARD.md``) to emit:

- a Metadata table row ``| Verdict | ✅ SHIP |`` (verdict with emoji), and
  verdict mentions in the body in the bold form ``✅ **SHIP**`` /
  ``⏸ **HOLD**`` / ``🚫 **BLOCK**``;
- a scorecard line ``**Recommendation:** Strong Hire — rationale``.

Parsing is intentionally conservative: any ambiguity returns ``None`` so the
caller can route the record to ``needs_review`` instead of guessing. The body
prose legitimately names all three verdict words (the prompt explains the
rules), so free-text word matching is never used — only the anchored
metadata row and the bold(+optional emoji) form.

Injection defense (layer 2, after the prompt-sanitizer fences): strategy 1
only searches the METADATA REGION — the text before the first ``\n---``
separator that the output contract places right after the Metadata section
— so a verdict-looking table row quoted later in the body (e.g. inside the
Evidence Table) can never satisfy the authoritative anchor.

v1.1 additions: :func:`parse_batch_consistency` (the batch report's
``### Machine Summary`` fenced-JSON block) and the assessment-draft header
parsers :func:`parse_assessment_rating` /
:func:`parse_assessment_recommendation`.
"""

from __future__ import annotations

import json
import re

# --- screening verdict -----------------------------------------------------

#: Strategy 1 anchor: the Metadata two-column table row for the verdict.
_METADATA_VERDICT_ROW_RE = re.compile(
    r"^\|\s*Verdict\s*\|(.+)\|\s*$",
    re.MULTILINE | re.IGNORECASE,
)

#: Strategy 2 anchor: bold verdict form, optionally preceded by its emoji,
#: e.g. ``✅ **SHIP**`` or ``**hold**``. Body prose mentions of the bare
#: words SHIP/HOLD/BLOCK deliberately do NOT match.
_BOLD_VERDICT_RE = re.compile(
    r"(?:✅|⏸️?|🚫)?\s*\*\*\s*(SHIP|HOLD|BLOCK)\s*\*\*",
    re.IGNORECASE,
)

_VERDICT_WORD_RE = re.compile(r"\b(SHIP|HOLD|BLOCK)\b", re.IGNORECASE)

_EMOJI_VERDICTS = (
    ("✅", "ship"),
    ("⏸", "hold"),
    ("🚫", "block"),
)


def _verdicts_in_cell(cell: str) -> list[str]:
    """Distinct verdicts (ordered) found in a metadata-table cell."""
    found: list[str] = []
    for match in _VERDICT_WORD_RE.finditer(cell):
        verdict = match.group(1).lower()
        if verdict not in found:
            found.append(verdict)
    for emoji, verdict in _EMOJI_VERDICTS:
        if emoji in cell and verdict not in found:
            found.append(verdict)
    return found


def _verdict_from_metadata_row(md: str) -> str | None:
    """Strategy 1: parse the ``| Verdict | ... |`` Metadata table row.

    The search is RESTRICTED to the metadata region — everything before
    the first ``\\n---`` separator (the output contract places exactly one
    right after the Metadata section). Verdict rows appearing later in the
    document (quoted resume content, evidence tables) never match.

    Returns the verdict only when the first matching row's cell contains
    exactly one distinct verdict (word or emoji); otherwise ``None``.
    """
    metadata_region = md.split("\n---", 1)[0]
    match = _METADATA_VERDICT_ROW_RE.search(metadata_region)
    if not match:
        return None
    found = _verdicts_in_cell(match.group(1))
    if len(found) == 1:
        return found[0]
    return None


def parse_screening_verdict(md: str) -> str | None:
    """Extract the screening verdict from an LLM screening record.

    Two anchored strategies:

    1. The Metadata table row ``| Verdict | ✅ SHIP |`` — authoritative when
       its cell resolves to exactly one verdict (word or emoji). Searched
       ONLY in the metadata region (before the first ``\\n---`` separator).
    2. Fallback: the first bold(+optional emoji) verdict form in the whole
       document, e.g. ``✅ **SHIP**``.

    Returns ``None`` (caller routes to ``needs_review``) when:

    - neither strategy finds a verdict,
    - the strategies disagree,
    - strategy 2 finds 2+ distinct bold verdicts and no usable metadata row.

    Args:
        md: Full markdown screening record produced by the LLM.

    Returns:
        ``"ship"`` / ``"hold"`` / ``"block"``, or ``None`` when ambiguous.
    """
    if not md:
        return None

    s1 = _verdict_from_metadata_row(md)

    s2_all: list[str] = []
    for match in _BOLD_VERDICT_RE.finditer(md):
        verdict = match.group(1).lower()
        if verdict not in s2_all:
            s2_all.append(verdict)
    s2 = s2_all[0] if s2_all else None

    if s1 and s2:
        return s1 if s1 == s2 else None
    if s1:
        return s1
    if s2:
        if len(s2_all) >= 2:
            # No usable metadata row and multiple distinct bold verdicts
            # (e.g. the prompt's own verdict legend echoed back) — ambiguous.
            return None
        return s2
    return None


# --- scorecard recommendation ----------------------------------------------

#: Anchor for the scorecard's ``**Recommendation:** ...`` line.
_RECOMMENDATION_LINE_RE = re.compile(
    r"\*\*\s*Recommendation\s*:\s*\*\*(.*)$",
    re.MULTILINE | re.IGNORECASE,
)

#: Longest-first alternation: "Strong No Hire" must win over "No Hire",
#: "Strong Hire" over "Hire", etc. Python's ``re`` tries alternatives
#: left-to-right at each position, so order encodes precedence.
_RECOMMENDATION_RE = re.compile(
    r"\b(strong\s+no\s+hire|strong\s+hire|no\s+hire|hire)\b",
    re.IGNORECASE,
)

_RECOMMENDATION_MAP = {
    "strong no hire": "strong_no_hire",
    "strong hire": "strong_hire",
    "no hire": "no_hire",
    "hire": "hire",
}


def parse_recommendation(md: str) -> str | None:
    """Extract the hire recommendation from an LLM scorecard.

    Anchors the first ``**Recommendation:**`` line, then matches the
    recommendation longest-first ("Strong No Hire" before "Strong Hire"
    before "No Hire" before "Hire"), case-insensitively.

    Args:
        md: Full markdown scorecard produced by the LLM.

    Returns:
        One of ``"strong_hire"`` / ``"hire"`` / ``"no_hire"`` /
        ``"strong_no_hire"``, or ``None`` when no anchored recommendation
        is found (caller routes to ``needs_review``).
    """
    if not md:
        return None

    line_match = _RECOMMENDATION_LINE_RE.search(md)
    if not line_match:
        return None

    rec_match = _RECOMMENDATION_RE.search(line_match.group(1))
    if not rec_match:
        return None

    key = re.sub(r"\s+", " ", rec_match.group(1).lower())
    return _RECOMMENDATION_MAP.get(key)


# --- batch consistency machine summary ---------------------------------------

#: Schema identifier the Machine Summary JSON must declare.
BATCH_CONSISTENCY_SCHEMA = "iris.batch_consistency.v1"

#: Anchor for the report's ``### Machine Summary`` heading.
_MACHINE_SUMMARY_HEADING_RE = re.compile(
    r"^###\s+Machine\s+Summary\s*$",
    re.MULTILINE | re.IGNORECASE,
)

#: A fenced ```json block (first one after the anchored heading).
_JSON_FENCE_RE = re.compile(
    r"```json\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

_BATCH_VERDICTS = ("ship", "hold", "block")


def _normalized_batch_verdict(value) -> str | None:
    """Lowercased ``ship``/``hold``/``block``, or ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    verdict = value.strip().lower()
    return verdict if verdict in _BATCH_VERDICTS else None


def _parse_batch_candidates(entries) -> list[dict] | None:
    """Validate + normalize the ``candidates`` array; ``None`` on any drift."""
    candidates: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        reference = entry.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            return None
        current_verdict = _normalized_batch_verdict(entry.get("current_verdict"))
        if current_verdict is None:
            return None
        revision_raw = entry.get("revision_recommended")
        if revision_raw is None:
            revision = None
        else:
            revision = _normalized_batch_verdict(revision_raw)
            if revision is None:
                return None
        flags = entry.get("inconsistent_flags", [])
        signals = entry.get("fraud_signals", [])
        if not isinstance(flags, list) or not isinstance(signals, list):
            return None
        candidates.append({
            "reference": reference.strip(),
            "current_verdict": current_verdict,
            "revision_recommended": revision,
            "inconsistent_flags": [str(flag) for flag in flags],
            "fraud_signals": signals,
        })
    return candidates


def _parse_batch_inconsistencies(entries) -> list[dict] | None:
    """Validate + normalize the ``inconsistencies`` array; ``None`` on drift."""
    inconsistencies: list[dict] = []
    for finding in entries:
        if not isinstance(finding, dict):
            return None
        fired_on = finding.get("fired_on", [])
        should_fire_on = finding.get("should_fire_on", [])
        if not isinstance(fired_on, list) or not isinstance(should_fire_on, list):
            return None
        if not all(isinstance(ref, str) for ref in fired_on + should_fire_on):
            return None
        inconsistencies.append({
            "flag": str(finding.get("flag") or ""),
            "fired_on": fired_on,
            "should_fire_on": should_fire_on,
            "evidence": str(finding.get("evidence") or ""),
        })
    return inconsistencies


def parse_batch_consistency(md: str) -> dict | None:
    """Extract the Machine Summary from a batch consistency report.

    Anchors the LAST ``### Machine Summary`` heading (the report's own
    summary sits at the end per the output contract — earlier occurrences
    can only be quoted/spoofed copies), takes the next fenced ```json
    block after it, ``json.loads`` it, then validates:

    - ``schema == "iris.batch_consistency.v1"``;
    - ``candidates``: list of dicts, each with a non-empty string
      ``reference``, a ``ship``/``hold``/``block`` ``current_verdict``,
      a ``revision_recommended`` that is ``None`` or a valid verdict, and
      list-typed ``inconsistent_flags`` / ``fraud_signals``;
    - ``inconsistencies``: list of dicts with list-of-string ``fired_on``
      / ``should_fire_on`` (``flag`` / ``evidence`` coerced to strings).

    Candidate REFERENCES are deliberately NOT validated against the batch
    members here — ``iris.screening.batch`` does that itself (unknown
    references are skipped when raising advisory activities).

    Anything off returns ``None`` — the caller FAILS OPEN (the batch still
    completes; only the machine-readable findings are dropped).

    Args:
        md: Full markdown consistency report produced by the LLM.

    Returns:
        Normalized ``{"schema", "candidates", "inconsistencies"}`` dict,
        or ``None`` when the summary is absent, unparseable, or malformed.
    """
    if not md:
        return None

    heading_match = None
    for heading_match in _MACHINE_SUMMARY_HEADING_RE.finditer(md):
        pass
    if heading_match is None:
        return None

    fence_match = _JSON_FENCE_RE.search(md, heading_match.end())
    if not fence_match:
        return None

    try:
        data = json.loads(fence_match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != BATCH_CONSISTENCY_SCHEMA:
        return None

    candidates_raw = data.get("candidates", [])
    inconsistencies_raw = data.get("inconsistencies", [])
    if not isinstance(candidates_raw, list) or not isinstance(inconsistencies_raw, list):
        return None

    candidates = _parse_batch_candidates(candidates_raw)
    if candidates is None:
        return None
    inconsistencies = _parse_batch_inconsistencies(inconsistencies_raw)
    if inconsistencies is None:
        return None

    return {
        "schema": BATCH_CONSISTENCY_SCHEMA,
        "candidates": candidates,
        "inconsistencies": inconsistencies,
    }


# --- assessment review draft -------------------------------------------------

#: Anchor for the assessment draft's ``**Rating:** ...`` header bullet.
_RATING_LINE_RE = re.compile(
    r"\*\*\s*Rating\s*:\s*\*\*(.*)$",
    re.MULTILINE | re.IGNORECASE,
)

#: Longest-first alternation: the two-word bands must win over "Average".
_RATING_RE = re.compile(
    r"\b(above\s+average|below\s+average|exceptional|average|poor)\b",
    re.IGNORECASE,
)

_RATING_MAP = {
    "above average": "above_average",
    "below average": "below_average",
    "exceptional": "exceptional",
    "average": "average",
    "poor": "poor",
}

#: Longest-first alternation: "Lean No Hire" before "Lean Hire" before
#: "No Hire" before "Hire".
_ASSESSMENT_RECOMMENDATION_RE = re.compile(
    r"\b(lean\s+no\s+hire|lean\s+hire|no\s+hire|hire)\b",
    re.IGNORECASE,
)

_ASSESSMENT_RECOMMENDATION_MAP = {
    "lean no hire": "lean_no_hire",
    "lean hire": "lean_hire",
    "no hire": "no_hire",
    "hire": "hire",
}


def parse_assessment_rating(md: str) -> str | None:
    """Extract the rating from an LLM assessment-review draft.

    Anchors the first ``**Rating:**`` header bullet, then matches the band
    longest-first ("Above Average" / "Below Average" before "Average"),
    case-insensitively. None-safe: missing input, missing anchor, or an
    unrecognized band all return ``None`` (the caller fills nothing).

    Returns:
        One of ``"exceptional"`` / ``"above_average"`` / ``"average"`` /
        ``"below_average"`` / ``"poor"``, or ``None``.
    """
    if not md:
        return None

    line_match = _RATING_LINE_RE.search(md)
    if not line_match:
        return None

    rating_match = _RATING_RE.search(line_match.group(1))
    if not rating_match:
        return None

    key = re.sub(r"\s+", " ", rating_match.group(1).lower())
    return _RATING_MAP.get(key)


def parse_assessment_recommendation(md: str) -> str | None:
    """Extract the recommendation from an LLM assessment-review draft.

    Anchors the first ``**Recommendation:**`` header bullet, then matches
    the band longest-first ("Lean No Hire" → "Lean Hire" → "No Hire" →
    "Hire"), case-insensitively. None-safe like
    :func:`parse_assessment_rating`.

    Returns:
        One of ``"hire"`` / ``"lean_hire"`` / ``"lean_no_hire"`` /
        ``"no_hire"``, or ``None``.
    """
    if not md:
        return None

    line_match = _RECOMMENDATION_LINE_RE.search(md)
    if not line_match:
        return None

    rec_match = _ASSESSMENT_RECOMMENDATION_RE.search(line_match.group(1))
    if not rec_match:
        return None

    key = re.sub(r"\s+", " ", rec_match.group(1).lower())
    return _ASSESSMENT_RECOMMENDATION_MAP.get(key)
