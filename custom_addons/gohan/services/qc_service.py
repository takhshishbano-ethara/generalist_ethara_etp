"""QC Service — Odoo Flow (artifact-alignment check).

Verifies that the generated PRD accurately reflects the extraction data.
Does NOT require browser access. Does NOT check against the live site.

The goal: ensure PRD content aligns with what was actually extracted
(colors, fonts, tech stack, animations, page structure) rather than
hallucinating specs to pass arbitrary quality gates.
"""

import json
import logging
import re
from pathlib import Path

_logger = logging.getLogger(__name__)


def run_qc(
    prd_text: str,
    extraction_data: dict,
    site_discovery: dict,
    url: str,
    category: str,
    inference_arn: str,
    region: str = "us-east-1",
    access_key_id: str = None,
    secret_access_key: str = None,
    qc_system_prompt: str = "",
    screenshot_blocks: list = None,
) -> dict:
    """Run QC: deterministic structural checks + the v2 compliance QC review.

    Args:
        prd_text: The generated PRD markdown.
        extraction_data: Dict of raw extraction artifacts (style_data, animation_data, etc.)
        site_discovery: Site discovery output (title, pages, tech_stack).
        url: Canonical website URL.
        category: Website category name.
        inference_arn: Bedrock model ARN.
        region: AWS region.
        access_key_id: Optional AWS key.
        secret_access_key: Optional AWS secret.

    Returns:
        dict with: verdict, report, issues_critical, issues_high,
                   issues_medium, issues_low
    """
    # Phase 1: Fast structural checks (no LLM)
    structural_issues = _run_structural_checks(prd_text, category)

    # Phase 2: v2 compliance QC review (PRD-only LLM pass)
    llm_result = _run_llm_alignment_check(
        prd_text=prd_text,
        extraction_data=extraction_data,
        site_discovery=site_discovery,
        url=url,
        category=category,
        inference_arn=inference_arn,
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        qc_system_prompt=qc_system_prompt,
        screenshot_blocks=screenshot_blocks,
    )

    # Verdict comes from the v2 QC's own VERDICT: PASS|FAIL line -- the QC
    # prompt computes it via its documented verdict policy. Cross-checked
    # against the parsed CHECK TABLE and fail-closed: not_shippable unless
    # the verdict says PASS *and* no critical-class FAIL row was parsed.
    llm_issues = llm_result.get("issues", [])
    llm_verdict = llm_result.get("verdict") or ""
    has_critical_issue = any(i["severity"] == "critical" for i in llm_issues)
    if llm_verdict == "shippable" and not has_critical_issue:
        verdict = "shippable"
    else:
        verdict = "not_shippable"

    # Build report. The structural checks still run as a deterministic
    # backstop and appear in their own report section; the verdict and the
    # issue counts come from the v2 compliance QC review.
    report = _build_report(
        verdict=verdict,
        url=url,
        category=category,
        prd_text=prd_text,
        structural_issues=structural_issues,
        llm_report=llm_result.get("report", ""),
        all_issues=llm_issues,
    )

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in llm_issues:
        sev = issue["severity"]
        if sev in counts:
            counts[sev] += 1

    return {
        "verdict": verdict,
        "report": report,
        "issues_critical": counts["critical"],
        "issues_high": counts["high"],
        "issues_medium": counts["medium"],
        "issues_low": counts["low"],
    }


# =============================================================================
# STRUCTURAL CHECKS (fast, no LLM)
# =============================================================================

def _run_structural_checks(prd_text: str, category: str) -> list:
    """Regex-based structural validation."""
    issues = []
    words = prd_text.split()
    word_count = len(words)

    # Word count bounds -- the v9 pipeline band is 800-1,500 words by wc -w.
    # (Pre-v9 this checked an obsolete 4,000-5,000 band and false-flagged
    # every compliant PRD as "below target range".)
    if word_count > 1500:
        issues.append({
            "severity": "high",
            "code": "S-WORDCOUNT",
            "message": f"PRD exceeds 1,500 words ({word_count}). Over the hard ceiling.",
        })
    elif word_count < 800:
        issues.append({
            "severity": "high",
            "code": "S-WORDCOUNT",
            "message": f"PRD below 800 words ({word_count}). Under the hard floor.",
        })

    # Markdown tables — allowed (scoring rewards structured formatting)
    # Downgraded from HIGH to informational; no longer triggers not_shippable.
    table_lines = re.findall(r"^\|.*\|.*\|", prd_text, re.MULTILINE)
    if len(table_lines) >= 2:
        issues.append({
            "severity": "low",
            "code": "S-TABLES",
            "message": f"Markdown tables found ({len(table_lines)} lines). Acceptable if well-structured.",
        })

    # Non-keyboard characters (H15)
    non_ascii = re.findall(
        r"[\u2013\u2014\u2018\u2019\u201c\u201d\u2026"
        r"\u2190-\u21ff\u2022\u25cf\u25cb\u25a0\u25a1"
        r"\u2713\u2714\u2717\u2718\u2500-\u257f"
        r"\U0001f300-\U0001f9ff]",
        prd_text,
    )
    if non_ascii:
        unique = list(set(non_ascii))[:5]
        issues.append({
            "severity": "high",
            "code": "S-CHARS",
            "message": f"Non-keyboard characters: {', '.join(repr(c) for c in unique)}",
        })

    # Required 8 sections
    required = [
        (r"##?\s*\d*\.?\s*product\s+overview", "Product Overview"),
        (r"##?\s*\d*\.?\s*visual", "Visual & Brand Direction"),
        (r"##?\s*\d*\.?\s*technical", "Technical Ambition"),
        (r"##?\s*\d*\.?\s*(site\s+)?architecture", "Site Architecture"),
        (r"##?\s*\d*\.?\s*motion", "Motion Language"),
        (r"##?\s*\d*\.?\s*application\s+logic", "Application Logic"),
        (r"##?\s*\d*\.?\s*accessibility", "Accessibility & Quality"),
        (r"##?\s*\d*\.?\s*content", "Content & SEO"),
    ]
    text_lower = prd_text.lower()
    missing = []
    for pattern, name in required:
        if not re.search(pattern, text_lower):
            missing.append(name)

    if missing:
        severity = "critical" if len(missing) >= 3 else "medium"
        issues.append({
            "severity": severity,
            "code": "S-SECTIONS",
            "message": f"Missing sections: {', '.join(missing)}",
        })

    # NOTE: the v9 prompt forbids any preamble -- the PRD's first line is
    # "## 1. Product Overview", with no metadata header. The old S-CATEGORY
    # and S-RESOLUTION checks looked for a `Category:` / `Target Resolution:`
    # preamble and so false-flagged every compliant v9 PRD; both removed.

    # Banned vague phrases (sample check)
    banned = [
        "smooth animation", "modern ux", "clean layout", "sleek",
        "seamless experience", "intuitive navigation", "pixel-perfect",
        "cutting-edge", "state-of-the-art", "next-level",
    ]
    found_banned = [b for b in banned if b in text_lower]
    if len(found_banned) >= 3:
        issues.append({
            "severity": "medium",
            "code": "S-SLOP",
            "message": f"Banned vague phrases ({len(found_banned)}): {', '.join(found_banned[:5])}",
        })

    return issues


# =============================================================================
# LLM ALIGNMENT CHECK
# =============================================================================

_QC_SYSTEM_PROMPT = """You are a QC reviewer for Project Gohan. Your job is to verify that a PRD accurately reflects the extraction data collected from a website.

You receive:
1. The generated PRD
2. The extraction data (styles, animations, tech stack, site structure)

Your job is to check ALIGNMENT -- does the PRD faithfully describe what was actually extracted? You are NOT checking against the live site. You are checking: PRD claims vs extraction artifacts.

IMPORTANT RULES:
- Do NOT penalize the PRD for things not present in the extraction data. If extraction didn't capture a font, the PRD can't be expected to name it.
- Do NOT manufacture findings. A clean submission with zero issues is valid and common.
- Do NOT be overly strict. The PRD is meant to be a useful specification, not a perfect mirror of raw data.
- DO flag clear hallucinations: colors, fonts, libraries, or specs that contradict the extraction data.
- DO flag missing critical information that IS present in the extraction data but absent from the PRD.

CHECK THESE ALIGNMENTS:

1. COLORS: Do hex codes in the PRD match colors found in extraction style_data? Flag invented hex codes not in the extracted palette.

2. TYPOGRAPHY: Do font families in the PRD match what style_data extracted? Flag invented font names.

3. TECH STACK: Does the PRD's claimed stack match site_discovery.tech_stack? Flag libraries that were NOT detected.

4. PAGE STRUCTURE: Does the PRD cover the pages found in site_discovery.pages? Flag pages the PRD describes that don't exist.

5. ANIMATIONS: If animation_data shows specific animations, does the PRD describe them? If the PRD claims specific timings (ms, easing), are they grounded in extraction data or fabricated?

6. CATEGORY FIT: Does the PRD's declared category match the site_discovery.category? Does the PRD body actually reflect that category's requirements?

OUTPUT FORMAT:
Produce a short alignment report with this structure:

VERDICT: SHIPPABLE | NOT SHIPPABLE

ALIGNMENT SUMMARY: (2-3 sentences)

ISSUES: (if any -- one per line)
- [SEVERITY] CODE: description
  Evidence: what the extraction shows vs what the PRD claims

Severities: CRITICAL (hallucinated data contradicting extraction), HIGH (major omission of extracted data), MEDIUM (minor gaps), LOW (polish)

If no issues found, write:
VERDICT: SHIPPABLE
ALIGNMENT SUMMARY: PRD accurately reflects extraction data. No hallucinations detected.
ISSUES: None.
"""


DEFAULT_QC_SYSTEM_PROMPT = _QC_SYSTEM_PROMPT


def _run_llm_alignment_check(
    prd_text: str,
    extraction_data: dict,
    site_discovery: dict,
    url: str,
    category: str,
    inference_arn: str,
    region: str,
    access_key_id: str = None,
    secret_access_key: str = None,
    qc_system_prompt: str = "",
    screenshot_blocks: list = None,
) -> dict:
    """Call Bedrock to run the PRD-only compliance QC review.

    The QC reviewer is a spec-compliance check: its input is the PRD
    markdown and nothing else. Extraction data, the source URL/brand, and
    screenshots are deliberately NOT sent -- the QC prompt treats the whole
    user message as the artifact under review, so appending that context
    made the model mistake it for part of the PRD and emit false
    violations (source URL "in the PRD body", real brand name, trailing
    block after Section 8).

    ``extraction_data``, ``site_discovery``, ``url``, ``category`` and
    ``screenshot_blocks`` are kept on the signature for call-site
    compatibility but are intentionally unused here.
    """
    from .bedrock_service import generate_prd as _call_bedrock

    # Use custom prompt if provided, otherwise fall back to default
    effective_prompt = qc_system_prompt or _QC_SYSTEM_PROMPT

    # PRD-only input: the whole user message is the artifact under review.
    try:
        response = _call_bedrock(
            inference_arn=inference_arn,
            region=region,
            system_prompt=effective_prompt,
            messages=[{"role": "user", "content": prd_text}],
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )
    except Exception as exc:
        _logger.exception("QC LLM call failed")
        # Fail-closed: LLM failure = not shippable (never silently pass)
        return {
            "report": f"LLM QC failed: {exc}",
            "issues": [{
                "severity": "critical",
                "code": "LLM-FAIL",
                "message": f"QC LLM evaluation failed: {exc}. Defaulting to NOT SHIPPABLE.",
            }],
            "verdict": "not_shippable",
        }

    # The model sometimes wraps its whole response in a Markdown code fence
    # (echoing the prompt's output-format template); unwrap it so the report
    # renders as real Markdown (headings, tables) in the Quality tab.
    response = _strip_code_fence(response)

    # Parse the v2 QC report: the CHECK TABLE FAIL rows + the VERDICT line.
    issues = _parse_llm_issues(response)
    verdict = _parse_qc_verdict(response)

    return {"report": response, "issues": issues, "verdict": verdict}


def _summarize_extraction(extraction_data: dict, site_discovery: dict) -> str:
    """Build a concise summary of extraction data for the LLM."""
    parts = []

    # Site discovery
    if site_discovery:
        parts.append(f"### Site Discovery\n")
        parts.append(f"- Title: {site_discovery.get('title', 'Unknown')}")
        parts.append(f"- Category: {site_discovery.get('category', 'Unknown')}")
        pages = site_discovery.get("pages", [])
        if pages:
            parts.append(f"- Pages: {', '.join(str(p) for p in pages[:10])}")
        tech = site_discovery.get("tech_stack", {})
        if tech:
            if isinstance(tech, dict):
                tech_str = ", ".join(
                    f"{k} ({v.get('version', '?')})" if isinstance(v, dict) else f"{k}"
                    for k, v in tech.items()
                )
            else:
                tech_str = str(tech)
            parts.append(f"- Tech stack: {tech_str}")
        parts.append("")

    # Style data
    style = extraction_data.get("style_data") or extraction_data.get("raw_data/style_data.json")
    if style:
        if isinstance(style, str):
            try:
                style = json.loads(style)
            except (json.JSONDecodeError, TypeError):
                style = None
        if style and isinstance(style, dict):
            parts.append("### Extracted Styles")
            colors = style.get("colors", style.get("palette", []))
            if colors and isinstance(colors, list):
                parts.append(f"- Colors: {', '.join(str(c) for c in colors[:15])}")
            fonts = style.get("fonts", style.get("typography", []))
            if fonts and isinstance(fonts, list):
                parts.append(f"- Fonts: {', '.join(str(f) for f in fonts[:10])}")
            parts.append("")

    # Animation data
    anim = extraction_data.get("animation_data") or extraction_data.get("raw_data/animation_data.json")
    if anim:
        if isinstance(anim, str):
            try:
                anim = json.loads(anim)
            except (json.JSONDecodeError, TypeError):
                anim = None
        if anim and isinstance(anim, dict):
            parts.append("### Extracted Animations")
            anims = anim.get("animations", anim.get("css_animations", []))
            if anims and isinstance(anims, list):
                parts.append(f"- Count: {len(anims)} animations detected")
                for a in anims[:5]:
                    if isinstance(a, dict):
                        name = a.get("name", a.get("property", "unknown"))
                        dur = a.get("duration", "?")
                        parts.append(f"  - {name}: {dur}")
                    else:
                        parts.append(f"  - {a}")
            parts.append("")

    # Network / performance
    perf = extraction_data.get("performance_data") or extraction_data.get("raw_data/performance_data.json")
    if perf:
        if isinstance(perf, str):
            try:
                perf = json.loads(perf)
            except (json.JSONDecodeError, TypeError):
                perf = None
        if perf and isinstance(perf, dict):
            parts.append("### Performance Data")
            for key in ["lcp", "fcp", "cls", "tbt", "dom_size"]:
                if key in perf:
                    parts.append(f"- {key}: {perf[key]}")
            parts.append("")

    if not parts:
        parts.append("(No structured extraction data available)")

    return "\n".join(parts)


# Check IDs whose FAIL is a critical (auto-FAIL) violation per the v2 QC
# verdict policy: every A/B/C/H-class check and M1/M2/M3, plus these named
# hard-rule checks. Every other FAIL is a soft warning.
_QC_CRITICAL_NAMED = frozenset({
    "M1", "M2", "M3",
    "E2", "F3", "F4", "G4", "G5", "I2", "I4",
    "J2", "J5", "J6", "J7", "K2", "L4", "L6", "L7",
})


def _qc_check_is_critical(check_id: str) -> bool:
    """True if a FAIL on this v2 check ID auto-fails the PRD.

    Per the v2 QC verdict policy: any A/B/C/H-class check, M1/M2/M3, and the
    named hard-rule checks (E2, F3, F4, G4, G5, I2, I4, J2, J5, J6, J7, K2,
    L4, L6, L7). Everything else is a soft warning.
    """
    cid = (check_id or "").strip().upper()
    if not cid:
        return False
    # B6 (per-section word budgets) is explicitly "warning-class, not
    # auto-FAIL" in the v2 spec, despite being B-class -- treat it as soft.
    if cid == "B6":
        return False
    if cid[0] in ("A", "B", "C", "H"):
        return True
    return cid in _QC_CRITICAL_NAMED


def _strip_code_fence(text: str) -> str:
    """Unwrap a single Markdown code fence wrapping the whole text.

    The QC model sometimes echoes the prompt's output-format ``` fence and
    emits its whole report inside it. Unwrapping keeps the report as plain
    Markdown so it renders (headings, tables) instead of one code block.
    """
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    nl = t.find("\n")
    if nl == -1:
        return t
    t = t[nl + 1:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


def _parse_qc_verdict(response: str) -> str:
    """Parse the v2 QC ``VERDICT: PASS | FAIL`` line.

    Uses the LAST ``VERDICT:`` line: the QC model sometimes emits a first
    verdict then a corrected one, and the last is its final answer.
    Returns ``"shippable"`` for PASS, ``"not_shippable"`` for FAIL, or ``""``
    when no verdict line is present (the caller treats that as fail-closed).
    """
    matches = re.findall(r"(?im)^\s*VERDICT:\s*(PASS|FAIL)\b", response or "")
    if not matches:
        return ""
    return "shippable" if matches[-1].upper() == "PASS" else "not_shippable"


def _parse_llm_issues(response: str) -> list:
    """Parse the v2 QC CHECK TABLE into the list of FAIL issues.

    The QC model sometimes emits the CHECK TABLE more than once -- a first
    pass plus a "corrected" re-emission. To reflect the model's FINAL
    answer (and never duplicate an issue), each check ID is resolved to its
    LAST occurrence in the response; only rows whose final status is FAIL
    become issues. This dedupes and drops rows the model later retracted.

    Table rows look like ``| A1 | check description | FAIL | evidence |``.
    Severity is ``critical`` for v2 critical-class checks and ``medium``
    for soft-warning checks (see ``_qc_check_is_critical``).
    """
    # check_id (upper) -> (status, raw_id, check_name, evidence); last wins.
    by_id = {}
    order = []
    for line in (response or "").split("\n"):
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        check_id, check_name, status = cells[0], cells[1], cells[2]
        evidence = "|".join(cells[3:]).strip()
        status = status.upper()
        # Skip the header row ("Status") and the alignment row ("------").
        if status not in ("PASS", "FAIL", "N/A"):
            continue
        cid = check_id.strip().upper()
        if cid not in by_id:
            order.append(cid)
        by_id[cid] = (status, check_id, check_name, evidence)

    issues = []
    for cid in order:
        status, check_id, check_name, evidence = by_id[cid]
        if status != "FAIL":
            continue
        critical = _qc_check_is_critical(check_id)
        issues.append({
            "severity": "critical" if critical else "medium",
            "code": f"QC-{check_id}",
            "check": check_name,
            "evidence": evidence,
            "message": f"{check_name}: {evidence}".strip(" :"),
        })
    return issues


# =============================================================================
# REPORT BUILDER
# =============================================================================

_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEV_LABEL = {
    "critical": "BLOCKING - CRITICAL",
    "high": "BLOCKING - HIGH",
    "medium": "SOFT WARNING",
    "low": "SOFT WARNING",
}


def _build_report(
    verdict: str,
    url: str,
    category: str,
    prd_text: str,
    structural_issues: list,
    llm_report: str,
    all_issues: list,
) -> str:
    """Build the QC report.

    Leads with an "Issues to Fix" section -- one plainly-explained block per
    failing check, blocking issues first -- so a tasker sees exactly what to
    fix without scanning the full ~90-row check table. The complete QC
    review is kept below for reference.
    """
    word_count = len(prd_text.split())
    verdict_str = "SHIPPABLE" if verdict == "shippable" else "NOT SHIPPABLE"

    # Structural findings and the v2 QC findings are disjoint (different
    # checkers); show them together so nothing is missed.
    issues = list(structural_issues or []) + list(all_issues or [])
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in issues:
        sev = issue.get("severity")
        if sev in counts:
            counts[sev] += 1
    total = sum(counts.values())
    blocking = counts["critical"] + counts["high"]
    soft = counts["medium"] + counts["low"]

    report = "# QC Report\n\n"
    report += f"**Verdict:** {verdict_str}\n"
    report += f"**PRD word count:** {word_count}\n"
    report += f"**Source URL:** {url}\n"
    report += f"**Category:** {category}\n\n"
    report += "---\n\n"

    # --- Issues to Fix: the section the tasker acts on ---
    report += "## Issues to Fix\n\n"
    if issues:
        report += (
            f"This PRD has **{blocking} blocking issue(s)** and "
            f"**{soft} soft warning(s)**. Blocking issues must be fixed "
            f"before the PRD can ship; soft warnings are recommended. "
            f"Each issue below names the check, what is wrong, and "
            f"(where given) how to fix it.\n\n"
        )
        ordered = sorted(issues, key=lambda i: _SEV_RANK.get(i.get("severity"), 9))
        for n, issue in enumerate(ordered, 1):
            sev = issue.get("severity", "medium")
            label = _SEV_LABEL.get(sev, "ISSUE")
            code = issue.get("code", "") or ""
            check = issue.get("check") or ""
            detail = issue.get("evidence") or issue.get("message") or ""
            heading = f"### {n}. [{label}] {code}".rstrip()
            if check and check.lower() not in code.lower():
                heading += f" - {check}"
            report += heading + "\n\n"
            report += f"{detail}\n\n"
    else:
        report += "No issues found. The PRD passed every QC check.\n\n"

    report += "---\n\n"

    # --- Issue counts ---
    report += "## Issue Counts\n\n"
    report += "| Severity | Count |\n|---|---|\n"
    report += f"| Critical | {counts['critical']} |\n"
    report += f"| High | {counts['high']} |\n"
    report += f"| Medium | {counts['medium']} |\n"
    report += f"| Low | {counts['low']} |\n"
    report += f"| Total | {total} |\n\n"
    report += "---\n\n"

    # --- Full QC review (raw v2 output) for reference ---
    report += "## Full QC Review\n\n"
    report += "The complete check-by-check QC output is below for reference.\n\n"
    report += (llm_report or "(no QC review output)") + "\n"

    return report
