"""Verdict computation from findings.

v2.1 thresholds:
- 1+ CRITICAL = BLOCK
- 2+ HIGH = BLOCK
- 4+ MEDIUM = BLOCK (was: 4+ STANDARD)
- 6+ LOW = BLOCK (was: 6+ ADVISORY)
- CONDITIONAL SHIP: zero CRITICAL, zero HIGH, exactly 3 MEDIUM OR exactly 5 LOW
- SHIP: zero CRITICAL, zero HIGH, <=2 MEDIUM, <=4 LOW
"""

from __future__ import annotations

from .types import Finding, Severity, Verdict


def compute_verdict(findings: list[Finding]) -> tuple[Verdict, int, int, int, int]:
    """Compute final verdict from findings list.

    Returns:
        (verdict, critical_count, high_count, medium_count, low_count)
    """
    critical = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    high = sum(1 for f in findings if f.severity == Severity.HIGH)
    medium = sum(1 for f in findings if f.severity == Severity.MEDIUM)
    low = sum(1 for f in findings if f.severity == Severity.LOW)

    if critical >= 1:
        verdict = Verdict.BLOCK
    elif high >= 2:
        verdict = Verdict.BLOCK
    elif medium >= 4:
        verdict = Verdict.BLOCK
    elif low >= 6:
        verdict = Verdict.BLOCK
    elif medium == 3 or low == 5:
        verdict = Verdict.CONDITIONAL_SHIP
    else:
        verdict = Verdict.SHIP

    return verdict, critical, high, medium, low
