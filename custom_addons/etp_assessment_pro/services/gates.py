# -*- coding: utf-8 -*-
"""Pre-LLM integrity gates for subjective/image scoring (Phase 1).

These run BEFORE the Vertex grader in services/scoring.py. A blank answer or a
prompt-injection attempt is resolved to a raw score of 0 locally and the LLM is
never called for that answer. The gate outcome is composed into the SAME
immutable llm_raw_100 the grader would write (never a second scoring path);
gate name and flags ride in the llm_result_json audit trail.

Pure Python, no Odoo imports, so it is unit-testable in isolation.
"""
import re

from ..constants import INTEGRITY_GATE_PATTERNS

_INJECTION_RES = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in INTEGRITY_GATE_PATTERNS)


def is_empty_answer(text):
    return not (text or "").strip()


def is_injection_attempt(text):
    candidate = text or ""
    if not candidate.strip():
        return False
    return any(rx.search(candidate) for rx in _INJECTION_RES)


def evaluate_gates(text):
    """Return the gate dict for this candidate text, or None when it is clean.

    empty_answer is screened first (blank text carries no injection); an
    injection attempt raises integrity_alert so the audit trail distinguishes a
    deliberate cheat from an honest blank.
    """
    if is_empty_answer(text):
        return {"gate": "empty_answer", "flags": []}
    if is_injection_attempt(text):
        return {"gate": "injection_attempt", "flags": ["integrity_alert"]}
    return None
