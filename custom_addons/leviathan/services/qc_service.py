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
) -> dict:
    """Run QC: structural checks + LLM alignment review.

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

    # Phase 2: LLM alignment review (PRD vs extraction data)
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
    )

    # Combine results
    all_issues = structural_issues + llm_result.get("issues", [])

    # Determine verdict
    has_critical = any(i["severity"] == "critical" for i in all_issues)
    has_high = any(i["severity"] == "high" for i in all_issues)
    verdict = "not_shippable" if (has_critical or has_high) else "shippable"

    # Build report
    report = _build_report(
        verdict=verdict,
        url=url,
        category=category,
        prd_text=prd_text,
        structural_issues=structural_issues,
        llm_report=llm_result.get("report", ""),
        all_issues=all_issues,
    )

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in all_issues:
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

    # Word count bounds
    if word_count > 3500:
        issues.append({
            "severity": "critical",
            "code": "S-WORDCOUNT",
            "message": f"PRD exceeds 3,500 words ({word_count}). Platform hard cap.",
        })
    elif word_count < 800:
        issues.append({
            "severity": "high",
            "code": "S-WORDCOUNT",
            "message": f"PRD below 800 words ({word_count}). Insufficient depth.",
        })
    elif word_count < 2800:
        issues.append({
            "severity": "medium",
            "code": "S-WORDCOUNT",
            "message": f"PRD below target range ({word_count}/2800-3500).",
        })

    # Markdown tables (H16 in QC rubric)
    table_lines = re.findall(r"^\|.*\|.*\|", prd_text, re.MULTILINE)
    if len(table_lines) >= 2:
        issues.append({
            "severity": "high",
            "code": "S-TABLES",
            "message": f"Markdown tables found ({len(table_lines)} lines). Use bullet lists instead.",
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
        (r"##?\s*\d*\.?\s*backend", "Backend & Application Logic"),
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

    # Category declaration present
    if not re.search(r"category:\s*\S", text_lower):
        issues.append({
            "severity": "medium",
            "code": "S-CATEGORY",
            "message": "No category declaration in preamble.",
        })

    # Target resolution present
    if not re.search(r"target\s+resolution:", text_lower):
        issues.append({
            "severity": "medium",
            "code": "S-RESOLUTION",
            "message": "No target resolution declaration in preamble.",
        })

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

_QC_SYSTEM_PROMPT = """You are a QC reviewer for Project Leviathan. Your job is to verify that a PRD accurately reflects the extraction data collected from a website.

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
) -> dict:
    """Call Bedrock to check PRD vs extraction data alignment."""
    from .bedrock_service import generate_prd as _call_bedrock

    # Use custom prompt if provided, otherwise fall back to default
    effective_prompt = qc_system_prompt or _QC_SYSTEM_PROMPT

    # Build a concise extraction summary (don't dump everything)
    extraction_summary = _summarize_extraction(extraction_data, site_discovery)

    user_message = (
        f"## PRD to Review\n\n{prd_text}\n\n"
        f"---\n\n"
        f"## Extraction Data (ground truth)\n\n"
        f"**URL:** {url}\n"
        f"**Category:** {category}\n\n"
        f"{extraction_summary}"
    )

    try:
        response = _call_bedrock(
            inference_arn=inference_arn,
            region=region,
            system_prompt=effective_prompt,
            messages=[{"role": "user", "content": user_message}],
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
        }

    # Parse the LLM response
    issues = _parse_llm_issues(response)

    return {"report": response, "issues": issues}


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


def _parse_llm_issues(response: str) -> list:
    """Parse issues from LLM alignment check response."""
    issues = []
    severity_map = {
        "CRITICAL": "critical",
        "HIGH": "high",
        "MEDIUM": "medium",
        "LOW": "low",
    }

    for line in response.split("\n"):
        line = line.strip()
        if not line.startswith("- ["):
            continue
        for sev_label, sev_key in severity_map.items():
            if f"[{sev_label}]" in line:
                # Extract the message after the severity tag
                msg = re.sub(r"^-\s*\[" + sev_label + r"\]\s*", "", line)
                issues.append({
                    "severity": sev_key,
                    "code": f"LLM-{sev_label[:1]}",
                    "message": msg,
                })
                break

    return issues


# =============================================================================
# REPORT BUILDER
# =============================================================================

def _build_report(
    verdict: str,
    url: str,
    category: str,
    prd_text: str,
    structural_issues: list,
    llm_report: str,
    all_issues: list,
) -> str:
    """Build the QC_Report.md content."""
    word_count = len(prd_text.split())
    verdict_str = "SHIPPABLE" if verdict == "shippable" else "NOT SHIPPABLE"

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in all_issues:
        sev = issue["severity"]
        if sev in counts:
            counts[sev] += 1
    total = sum(counts.values())

    report = f"""# QC Report (Automated - Mode B)

**Verdict:** {verdict_str}
**Source URL:** {url}
**Category:** {category}
**PRD word count:** {word_count}
**Review mode:** Automated (structural + LLM alignment, no browser)

## Issue Counts

| Severity | Count |
|---|---|
| Critical | {counts['critical']} |
| High | {counts['high']} |
| Medium | {counts['medium']} |
| Low | {counts['low']} |
| **Total** | **{total}** |

---

## Structural Checks

"""
    if structural_issues:
        for issue in structural_issues:
            report += f"- **[{issue['code']}]** {issue['message']}\n"
    else:
        report += "_All structural checks passed._\n"

    report += f"""
---

## LLM Alignment Review

{llm_report}

---

## Issues Detail

"""
    if all_issues:
        for severity in ["critical", "high", "medium", "low"]:
            sev_issues = [i for i in all_issues if i["severity"] == severity]
            if sev_issues:
                report += f"### {severity.capitalize()} Issues\n\n"
                for issue in sev_issues:
                    report += f"- **[{issue['code']}]** {issue['message']}\n"
                report += "\n"
    else:
        report += "_No issues found. PRD aligns with extraction data._\n"

    return report
