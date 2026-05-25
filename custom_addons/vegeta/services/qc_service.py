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
        screenshot_blocks=screenshot_blocks,
    )

    # Combine results
    all_issues = structural_issues + llm_result.get("issues", [])

    has_critical = any(i["severity"] == "critical" for i in all_issues)
    high_count = sum(1 for i in all_issues if i["severity"] == "high")
    medium_count = sum(1 for i in all_issues if i["severity"] == "medium")
    if has_critical or high_count >= 3:
        verdict = "not_shippable"
    elif high_count >= 1 or medium_count >= 4:
        verdict = "fixes"
    else:
        verdict = "shippable"

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
    if word_count > 5000:
        issues.append({
            "severity": "critical",
            "code": "S-WORDCOUNT",
            "message": f"PRD exceeds 5,000 words ({word_count}). Platform hard cap.",
        })
    elif word_count < 800:
        issues.append({
            "severity": "high",
            "code": "S-WORDCOUNT",
            "message": f"PRD below 800 words ({word_count}). Insufficient depth.",
        })
    elif word_count < 4000:
        issues.append({
            "severity": "medium",
            "code": "S-WORDCOUNT",
            "message": f"PRD below target range ({word_count}/4000-5000).",
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

    # Non-keyboard characters (H15). Catches: smart quotes, em/en-dash, ellipsis,
    # arrows, bullets/box-drawing, emoji, and invisible whitespace gremlins
    # (NBSP, zero-width space/joiner/non-joiner, BOM) that survive copy/paste.
    non_ascii = re.findall(
        r"[\u00a0\u2013\u2014\u2018\u2019\u201c\u201d\u2026"
        r"\u200b-\u200d\ufeff"
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

    required = [
        (r"##?#?\s*1\.?\s*overview", "Overview"),
        (r"##?#?\s*2\.?\s*goals\s*(&|and)\s*non-?goals", "Goals & Non-Goals"),
        (r"##?#?\s*3\.?\s*user\s+roles\s*(&|and)\s*permissions", "User Roles & Permissions"),
        (r"##?#?\s*4\.?\s*authentication\s*(&|and)\s*onboarding", "Authentication & Onboarding"),
        (r"##?#?\s*5\.?\s*core\s+features\s*(&|and)\s*user\s+flows", "Core Features & User Flows"),
        (r"##?#?\s*6\.?\s*data\s+model", "Data Model"),
        (r"##?#?\s*7\.?\s*api\s+design", "API Design"),
        (r"##?#?\s*8\.?\s*ui\s*/?\s*ux\s+requirements", "UI/UX Requirements"),
        (r"##?#?\s*9\.?\s*error\s+handling\s*(&|and)\s*edge\s+cases", "Error Handling & Edge Cases"),
        (r"##?#?\s*10\.?\s*non-?functional\s+requirements", "Non-Functional Requirements"),
        (r"##?#?\s*11\.?\s*category-?specific\s+guidelines", "Category-Specific Guidelines"),
    ]
    text_lower = prd_text.lower()
    missing = []
    for pattern, name in required:
        if not re.search(pattern, text_lower):
            missing.append(name)

    if missing:
        severity = "critical" if len(missing) >= 3 else "high"
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

    banned = [
        "modern ux", "seamless", "intuitive", "stunning", "leverage",
        "best-in-class", "robust", "world-class", "cutting-edge",
        "next-generation", "industry-leading", "state-of-the-art",
        "game-changing", "revolutionary", "powerful", "delightful",
        "elegant solution", "user-friendly",
    ]
    found_banned = [b for b in banned if re.search(rf"\b{re.escape(b)}\b", text_lower)]
    if len(found_banned) >= 3:
        issues.append({
            "severity": "critical",
            "code": "S-SLOP",
            "message": f"Banned vague phrases ({len(found_banned)}): {', '.join(found_banned[:5])}. 3+ triggers auto-reject.",
        })
    elif len(found_banned) >= 1:
        issues.append({
            "severity": "medium",
            "code": "S-SLOP",
            "message": f"Banned vague phrases ({len(found_banned)}): {', '.join(found_banned)}",
        })

    return issues


# =============================================================================
# LLM ALIGNMENT CHECK
# =============================================================================

_QC_SYSTEM_PROMPT = """You are a senior product-engineering QC reviewer for Project Vegeta. You audit a buildable Vegeta PRD for shippability against the Scraped Site Bundle (extraction artifacts). You DO NOT have browser access. You check PRD claims vs extraction artifacts plus structural/format compliance.

INPUT:
- The generated PRD (11 sections: Overview, Goals & Non-Goals, User Roles & Permissions, Authentication & Onboarding, Core Features & User Flows, Data Model, API Design, UI/UX Requirements, Error Handling & Edge Cases, Non-Functional Requirements, Category-Specific Guidelines).
- Extraction summary: site_discovery (title, category, pages, tech_stack), style_data, business_signals, auth_signals, api_signals, content_entities, integrations_observed, metadata.
- URL (reference only -- must never appear in PRD body).
- Assigned category (one of 16): Public Utility, News, Publishing, Retail, Services, ERP, Knowledge, Procurement, Vertical Markets, HCM, CRM, Gov. Portal, Community, TMS, Multimedia, AI Platform.

REVIEW PHILOSOPHY:
- Do NOT manufacture findings. Clean submissions with zero issues are valid.
- Do NOT penalize the PRD for things absent from the bundle. Tier-3 inference grounded in category norms + named reference brands is acceptable if it does not contradict Tier-1 evidence.
- DO flag clear hallucinations contradicting Tier-1: invented hex codes, font names, library versions, endpoints, schema names that disagree with the bundle.
- DO flag missing critical content that IS captured in the bundle and silently dropped from the PRD.
- DO flag structural failures: missing sections, banned phrases, non-ASCII output, markdown tables, missing `->` flow markers.

TIER-1 PRECEDENCE LADDER (use to resolve conflicts):
machine-readable API contracts > schema.org/JSON-LD > signup fields > pricing tiers > typed pages > XHR observations > rendered content > marketing copy.
A PRD claim contradicting a higher-ranked source while citing only a lower-ranked one is a CRITICAL hallucination.

3-TIER EVIDENCE MODEL:
- Tier 1 Observed: literal bundle contents. Must render faithfully.
- Tier 2 Evidenced inference: not captured but strongly constrained by Tier 1.
- Tier 3 Category-pattern inference: not observable (admin console, RBAC, infra). Reconstruct from category norms + reference brands.

STRUCTURAL CHECKS (CRITICAL if failed):
- Word count outside 800-5000 (target 3200-4800).
- 3+ banned phrases from: modern UX, seamless, intuitive, stunning, leverage, best-in-class, robust, world-class, cutting-edge, next-generation, industry-leading, state-of-the-art, game-changing, revolutionary, powerful, delightful, elegant solution, user-friendly.
- Header block missing Version / Category / Date / Target Resolution / Reference Style.
- Any markdown table (pipe-and-dash) in body.
- Non-ASCII chars where ASCII suffices: Unicode arrows (use `->`), em/en-dash (use `-` / `--`), smart quotes (use straight), ellipsis char (use `...`), NBSP/zero-width, multiplication sign in resolution (use lowercase `x`), emoji.
- Any S5 sub-feature missing `->` flow marker.
- Section count != 11.
- Category emphasis absent from S3/S5/S6/S7/S11 (category swappable without rewriting these sections).
- Source URL appears in PRD body.

ALIGNMENT CHECKS:
- A1 COLORS: hex codes in PRD vs style_data palette. Invented hex = CRITICAL.
- A2 TYPOGRAPHY: font families in PRD vs style_data. Invented fonts = HIGH.
- A3 TECH STACK: PRD's stack vs site_discovery.tech_stack. Undetected libraries acceptable only if marked Tier-3 inference and category-typical.
- A4 PAGES & ROUTES: every observed_route + observed_page must appear in PRD (S5 fully specified or 'Additional routes:' compressed list).
- A5 ENTITIES & FIELDS: every content_entity + visible field must appear in S6 or 'Supporting entities:' compressed list. Silent drops = HIGH.
- A6 API ENDPOINTS: every api_signals endpoint must appear in S7 or 'Additional endpoints:' list.
- A7 AUTH: S4 auth methods must match auth_signals (sign-in methods, SSO/SAML, 2FA). Invented auth = HIGH.
- A8 BUSINESS MODEL: S2 measurable targets must reflect business_signals (pricing tiers, billing model). Generic "improve engagement" goals = MEDIUM.
- A9 CATEGORY FIT: PRD's declared category must match assigned category. S3/S5/S6/S7/S11 must visibly reflect category's defining mechanic.
- A10 INTEGRATIONS: integrations_observed must appear in S7 or S10. Invented integrations = HIGH.

QUALITY CHECKS:
- Q1 ROLE COVERAGE: every S3 role appears in S6 (as ownership keys/relations) AND S7 (access-grouped endpoints).
- Q2 FLOW COVERAGE: every S5 sub-feature supported by S6 entities + S7 endpoints. Orphans = HIGH.
- Q3 SPECIFICITY: every flow has `->` marker; every color has hex; every key dimension has a number; every field has type; every enum lists values; every endpoint has method+path; every role has enumerated capabilities.
- Q4 S5 BREADTH: 5-10 bold-labeled sub-features (5.1-5.x) + 'Additional routes:' list. <5 = MEDIUM.
- Q5 CATEGORY MECHANIC VISIBILITY: S11 names the category's defining mechanic and gives 4-7 concrete rules unique to this category (not generic best practices).

OUTPUT FORMAT (ASCII only):

VERDICT: SHIPPABLE | NEEDS_FIXES | NOT_SHIPPABLE

ALIGNMENT SUMMARY: (2-3 sentences -- did the PRD render Tier-1 evidence faithfully? did inference stay coherent with category emphasis?)

ISSUES: (one per line, group by severity descending)
- [CRITICAL] CODE: short description
  Evidence: what the bundle shows vs what the PRD claims
- [HIGH] CODE: short description
  Evidence: ...
- [MEDIUM] CODE: short description
- [LOW] CODE: short description

If no issues:
VERDICT: SHIPPABLE
ALIGNMENT SUMMARY: PRD renders Tier-1 evidence faithfully. Inference stays coherent with category emphasis. No hallucinations or structural failures detected.
ISSUES: None.

VERDICT MAPPING:
- CRITICAL >= 1 OR HIGH >= 3 -> NOT_SHIPPABLE.
- HIGH 1-2 OR MEDIUM >= 4 -> NEEDS_FIXES.
- Otherwise -> SHIPPABLE.
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

    # Build content blocks: screenshots (if available) + text
    content_blocks = list(screenshot_blocks or [])
    content_blocks.append({"text": user_message})

    try:
        response = _call_bedrock(
            inference_arn=inference_arn,
            region=region,
            system_prompt=effective_prompt,
            messages=[{"role": "user", "content": content_blocks}],
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
    verdict_str = {"shippable": "SHIPPABLE", "fixes": "SHIPPABLE WITH FIXES"}.get(verdict, "NOT SHIPPABLE")

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
