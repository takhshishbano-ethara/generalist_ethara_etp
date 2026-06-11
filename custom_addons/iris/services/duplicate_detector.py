"""Cross-candidate duplicate-resume detection (P2-10).

Pure-Python helpers (no Odoo imports). The model layer hooks these at the
end of resume processing: it normalizes the extracted resume text, stores a
SHA-256 fingerprint for indexed exact matching, and runs a bounded difflib
near-duplicate scan over recent candidates.

Design notes:

* Detection is ADVISORY ONLY — callers warn (chatter + banner), they never
  block an upload, never touch candidate state, and never feed the signal
  into an LLM prompt (cross-candidate analysis is the batch consistency
  pass's job).
* ``pg_trgm`` was rejected: it requires superuser DDL and is tuned for
  short strings, not multi-page resume text.
* The near-duplicate scan is O(N) over at most ``iris.dup_scan_limit``
  candidates with two cheap prefilters (length delta, ``quick_ratio``)
  before the expensive ``SequenceMatcher.ratio()`` call.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterable

#: Default full-ratio threshold for a near-duplicate hit (ICP
#: ``iris.dup_similarity_threshold`` overrides at the call site).
DEFAULT_SIMILARITY_THRESHOLD = 0.90

#: Length prefilter: skip pairs whose lengths differ by more than 20%.
LENGTH_DIFF_RATIO = 0.20

#: ``[Page N]`` markers injected by the PDF extractor.
_PAGE_MARKER_RE = re.compile(r"\[page\s+\d+\]")

#: Everything that is not a lowercase letter or digit collapses to a space.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_resume_text(text: str | None) -> str:
    """Normalize resume text for hashing and similarity comparison.

    Lowercases, strips the extractor's ``[Page N]`` markers, replaces every
    non-alphanumeric run with a single space, and collapses whitespace.
    Cosmetic differences (punctuation, line breaks, casing, page layout)
    therefore never defeat duplicate detection.
    """
    if not text:
        return ""
    normalized = _PAGE_MARKER_RE.sub(" ", text.lower())
    normalized = _NON_ALNUM_RE.sub(" ", normalized)
    return " ".join(normalized.split())


def sha256_of(normalized_text: str | None) -> str:
    """Hex SHA-256 of normalized resume text (the exact-match fingerprint)."""
    return hashlib.sha256((normalized_text or "").encode("utf-8")).hexdigest()


def find_near_duplicates(
    normalized_text: str | None,
    others: Iterable[tuple[object, str | None]],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[tuple[object, float]]:
    """Find near-duplicate resumes among ``others``.

    Args:
        normalized_text: The :func:`normalize_resume_text` output for the
            candidate being checked.
        others: Iterable of ``(key, normalized_text)`` pairs — typically
            ``(candidate_id, normalized_resume)`` for the most recent N
            candidates. Empty/false texts are skipped.
        threshold: Minimum ``SequenceMatcher.ratio()`` for a hit.

    Returns:
        list[tuple[object, float]]: ``(key, ratio)`` hits sorted by ratio
        descending. Exact duplicates are also returned here (ratio 1.0) —
        callers usually catch those earlier via :func:`sha256_of`.

    Prefilters (cheap → expensive):

    1. Length delta: skip when lengths differ by more than 20% — such
       pairs cannot reach a 0.90 ratio.
    2. ``quick_ratio()``: a documented upper bound on ``ratio()`` — skip
       when even the upper bound is below the threshold.
    3. Full ``ratio()`` only on survivors.
    """
    base = normalized_text or ""
    hits: list[tuple[object, float]] = []
    if not base:
        return hits

    base_len = len(base)
    matcher = difflib.SequenceMatcher(autojunk=False)
    # seq2 is the cached side in SequenceMatcher: set it once.
    matcher.set_seq2(base)

    for key, other in others:
        other = other or ""
        if not other:
            continue
        other_len = len(other)
        if abs(base_len - other_len) > LENGTH_DIFF_RATIO * max(base_len, other_len):
            continue
        matcher.set_seq1(other)
        if matcher.quick_ratio() < threshold:
            continue
        ratio = matcher.ratio()
        if ratio >= threshold:
            hits.append((key, ratio))

    hits.sort(key=lambda hit: hit[1], reverse=True)
    return hits
