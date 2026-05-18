"""
Score a PRD and write actionable feedback for LLM iteration.

Usage: python scripts/score_and_validate.py <prd_path> <output_dir>

Writes feedback.md and score_report.json to output_dir.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.prd_scorer import score_prd
from modules.qc_validator import validate_prd_quality, validate_data_fidelity
from config import RUBRIC_SECTIONS, TIER1_BANNED_PHRASES


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/score_and_validate.py <prd_path> <output_dir>")
        sys.exit(1)

    prd_path = sys.argv[1]
    output_dir = sys.argv[2]

    if not os.path.exists(prd_path):
        print(f"ERROR: PRD file not found: {prd_path}")
        sys.exit(1)

    with open(prd_path, "r", encoding="utf-8") as f:
        prd_text = f.read()

    raw_dir = os.path.join(output_dir, "raw_data")
    category = "Normal Website"
    site_discovery_path = os.path.join(raw_dir, "site_discovery.json")
    if os.path.exists(site_discovery_path):
        with open(site_discovery_path, "r", encoding="utf-8") as f:
            site_data = json.load(f)
            category = site_data.get("category", "Normal Website")

    report = score_prd(prd_text, category)

    score_path = os.path.join(output_dir, "score_report.json")
    with open(score_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    prd_quality_issues = validate_prd_quality(prd_text, category)
    fidelity_result = {}
    if os.path.isdir(raw_dir):
        fidelity_result = validate_data_fidelity(prd_text, raw_dir)

    feedback = _build_feedback(report, prd_quality_issues, fidelity_result, category, prd_text)

    feedback_path = os.path.join(output_dir, "feedback.md")
    with open(feedback_path, "w", encoding="utf-8") as f:
        f.write(feedback)

    score = report["total_score"]
    grade = report["grade"]
    passed = score >= 95 and grade != "REJECT" and not _has_critical_qc(prd_quality_issues)
    verdict = "PASS: YES" if passed else "PASS: NO"

    print(f"Score: {score}/100 ({grade}) | {verdict}")
    print(f"Feedback written to: {feedback_path}")


def _has_critical_qc(issues):
    if isinstance(issues, list):
        return any(i.get("severity") == "CRITICAL" for i in issues)
    return False


def _build_feedback(report, qc_issues, fidelity, category, prd_text):
    lines = []
    score = report["total_score"]
    grade = report["grade"]
    word_count = report["details"].get("word_count", 0)
    rejects = report.get("reject_triggers", [])

    has_critical = _has_critical_qc(qc_issues) if isinstance(qc_issues, list) else False
    passed = score >= 95 and grade != "REJECT" and not has_critical

    lines.append(f"# Feedback — PRD Scoring & QC")
    lines.append("")
    lines.append(f"## Overall")
    lines.append(f"- Score: {score}/100 (need 95+)")
    lines.append(f"- Grade: {grade}")
    lines.append(f"- Word count: {word_count} (range: 800-3500)")
    lines.append(f"- Category: {category}")
    lines.append("")

    if rejects:
        lines.append("## Auto-Reject Triggers")
        for r in rejects:
            lines.append(f"- {r}")
            lines.append(f"  FIX: Address this reject trigger before anything else")
        lines.append("")

    lines.append("## Section Scores")
    for key in ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11"]:
        if key not in report["section_scores"]:
            continue
        sec = report["section_scores"][key]
        sec_score = sec["score"]
        sec_max = sec["max"]
        if sec_max == 0:
            continue
        pct = round((sec_score / sec_max) * 100) if sec_max > 0 else 0
        config = RUBRIC_SECTIONS.get(key, {})
        name = config.get("name", key)
        status = "OK" if pct >= 80 else "NEEDS WORK" if pct >= 50 else "CRITICAL"
        lines.append(f"- **{key} {name}:** {sec_score}/{sec_max} ({pct}%) — {status}")

        if pct < 80:
            fixes = _suggest_fixes(key, sec, report["details"], prd_text)
            for fix in fixes:
                lines.append(f"  - FIX: {fix}")

    lines.append("")

    tier1 = report["details"].get("tier1_violations", [])
    if tier1:
        lines.append("## Banned Phrases Found")
        for phrase in tier1:
            lines.append(f"- \"{phrase}\"")
            lines.append(f"  FIX: Replace with a specific, quantified description")
        lines.append("")

    if isinstance(qc_issues, list) and qc_issues:
        critical_high = [i for i in qc_issues if i.get("severity") in ("CRITICAL", "HIGH")]
        if critical_high:
            lines.append("## QC Issues")
            for issue in critical_high:
                sev = issue.get("severity", "?")
                code = issue.get("code", "?")
                msg = issue.get("message", "?")
                lines.append(f"- [{sev}] [{code}] {msg}")
                if issue.get("evidence"):
                    lines.append(f"  Evidence: {issue['evidence']}")
                lines.append(f"  FIX: {msg}")
            lines.append("")

    if fidelity and fidelity.get("synthesized_count", 0) > 0:
        total = fidelity.get("total_claims", 0)
        synth = fidelity.get("synthesized_count", 0)
        verified = fidelity.get("verified_count", 0)
        if total > 0 and synth / total > 0.5:
            lines.append("## Data Fidelity Warning")
            lines.append(f"- {synth}/{total} claims are synthesized (not in raw data)")
            lines.append(f"- {verified}/{total} claims verified against extracted data")
            lines.append(f"  FIX: Use only data from prd_prompt_v2.md. Remove fabricated tech/color/font claims.")
            lines.append("")

    lines.append("## VERDICT")
    if passed:
        lines.append(f"PASS: YES — Score {score}/100, Grade {grade}")
    else:
        reasons = []
        if score < 95:
            reasons.append(f"Score {score}/100 (need 95+)")
        if grade == "REJECT":
            reasons.append("Auto-rejected (see triggers above)")
        if has_critical:
            reasons.append("Critical QC issues")

        low_sections = []
        for key in ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11"]:
            if key in report["section_scores"]:
                sec = report["section_scores"][key]
                if sec["max"] > 0 and (sec["score"] / sec["max"]) < 0.8:
                    low_sections.append(key)

        lines.append(f"PASS: NO — {'; '.join(reasons)}")
        if low_sections:
            lines.append(f"ACTION: Improve {', '.join(low_sections)} per FIX instructions above.")

    return "\n".join(lines)


def _suggest_fixes(section_key, sec_data, details, prd_text):
    import re
    fixes = []

    if section_key == "S1":
        wc = details.get("word_count", 0)
        if wc < 800:
            fixes.append(f"Add more content — currently {wc} words, need 800+")
        elif wc > 3500:
            fixes.append(f"Trim content — currently {wc} words, max 3500")

    elif section_key == "S2":
        hex_count = len(re.findall(r'#[0-9A-Fa-f]{6}\b', prd_text))
        if hex_count < 5:
            fixes.append(f"Add more hex color codes — currently {hex_count}, need 5+")
        custom_props = len(re.findall(r'--[a-zA-Z][\w-]+\s*`?\s*[:|,.]', prd_text))
        if custom_props < 3:
            fixes.append(f"Add CSS custom properties (--var-name: value) — currently {custom_props}, need 3+")
        if not re.search(r'(?:philosophy|aesthetic|design language|visual direction)', prd_text, re.IGNORECASE):
            fixes.append("Add 'philosophy' or 'aesthetic' keyword to design direction section")
        type_scale = len(re.findall(r'(?:H[1-6]|Body|Caption|Display)\s*\**[:–|]\**\s*\d+', prd_text, re.IGNORECASE))
        if type_scale < 3:
            fixes.append(f"Add type scale entries (H1: 64px format) — currently {type_scale}, need 3+")

    elif section_key == "S4":
        ms_vals = [v for v in re.findall(r'\b(\d+)\s*ms\b', prd_text) if int(v) > 0]
        if len(ms_vals) < 10:
            fixes.append(f"Add more ms timing values (>0ms) — currently {len(ms_vals)}, need 10+")
        easing = re.findall(r'(?:cubic-bezier|ease-(?:in|out|in-out)|power\d\.(?:in|out|inOut)|expo\.out|back\.out)', prd_text, re.IGNORECASE)
        if len(easing) < 5:
            fixes.append(f"Add more easing functions — currently {len(easing)}, need 5+")
        if not re.search(r'(?:Lenis|smooth\s*scroll|scrollerProxy|lerp)', prd_text, re.IGNORECASE):
            fixes.append("Mention scroll library (Lenis, scrollerProxy, or lerp)")
        if not re.search(r'(?:skeleton|shimmer|loading\s*(?:state|animation)|placeholder)', prd_text, re.IGNORECASE):
            fixes.append("Add loading state description (skeleton, shimmer, or placeholder)")

    elif section_key == "S5":
        if not re.search(r'(?:instead of|rather than|replaces|alternative to)', prd_text, re.IGNORECASE):
            fixes.append("Add architecture decision language ('instead of', 'rather than')")
        versioned = re.findall(r'(?:GSAP|Framer Motion|Lenis|ScrollTrigger)\s+\d[\d.]*', prd_text, re.IGNORECASE)
        if len(versioned) < 2:
            fixes.append(f"Add versioned animation libraries — currently {len(versioned)}, need 2+")
        if not re.search(r'(?:Sanity|Contentful|Strapi|Prismic|DatoCMS|Hygraph|Payload)', prd_text, re.IGNORECASE):
            fixes.append("Mention a CMS (Sanity, Contentful, etc.) or explicitly state 'No CMS'")

    elif section_key == "S6":
        roles = set(r.lower() for r in re.findall(r'(?:Visitor|Admin|Editor|User|Member|Guest|Manager)', prd_text, re.IGNORECASE))
        if len(roles) < 3:
            fixes.append(f"Add more user roles — currently {len(roles)}, need 3+")
        edge = re.findall(r'(?:session expir|redirect|protected route|rate limit)', prd_text, re.IGNORECASE)
        if len(edge) < 2:
            fixes.append(f"Add edge cases (session expiry, protected route, rate limit) — currently {len(edge)}")

    elif section_key == "S7":
        fields = re.findall(r'(?:title|slug|name|email|description|content|image|date|status|type|url|body)\s*[:–|]', prd_text, re.IGNORECASE)
        if len(fields) < 8:
            fixes.append(f"Add more typed fields to schemas — currently {len(fields)}, need 8+")
        caching = re.findall(r'(?:ISR|revalidat|webhook|CDN|SWR)', prd_text, re.IGNORECASE)
        if len(caching) < 2:
            fixes.append("Add caching rules (ISR, webhook, CDN, SWR)")

    elif section_key == "S8":
        bps = set(re.findall(r'\b(?:1440|1024|768|375)\s*px\b', prd_text))
        if len(bps) < 4:
            missing = {"1440px", "1024px", "768px", "375px"} - {f"{b}px" if not b.endswith("px") else b for b in bps}
            fixes.append(f"Add missing breakpoints: {', '.join(missing) if missing else 'check format'}")
        if not re.findall(r'\d+\s*col\w*\s*(?:to)\s*\d+\s*col', prd_text, re.IGNORECASE):
            fixes.append("Add column change descriptions ('12 col to 2 col to 1 col')")
        behavior = re.findall(r'(?:hidden|stacked|collapsed|hamburger|touch)', prd_text, re.IGNORECASE)
        if len(behavior) < 3:
            fixes.append(f"Add behavior keywords (hidden, stacked, collapsed, hamburger, touch) — currently {len(behavior)}")

    elif section_key == "S9":
        if not re.search(r'prefers-reduced-motion', prd_text, re.IGNORECASE):
            fixes.append("Add 'prefers-reduced-motion' mention")
        vitals = re.findall(r'\b(?:LCP|CLS|TBT|INP|TTFB)\s*(?:<|>|:)\s*[\d.]', prd_text, re.IGNORECASE)
        if len(vitals) < 3:
            fixes.append(f"Add Core Web Vitals with values (LCP < 2.5s, CLS < 0.1, etc.) — currently {len(vitals)}")

    elif section_key == "S10":
        page_trans = re.findall(r'(?:to|->)\s*\w+.*?\d+ms', prd_text)
        if len(page_trans) < 3:
            fixes.append(f"Add page-to-page transitions ('to /about...600ms' format) — currently {len(page_trans)}, need 3+")
        scroll_map = re.findall(r'\d+%\s*(?::|–|-)\s*\w+', prd_text)
        if len(scroll_map) < 5:
            fixes.append(f"Add scroll-triggered animation map entries ('25% - heading reveals' format) — currently {len(scroll_map)}, need 5+")
        staggers = re.findall(r'(?:stagger|inter-element|delay)\s*[:=]?\s*\d+ms', prd_text, re.IGNORECASE)
        if len(staggers) < 3:
            fixes.append(f"Add stagger specs ('stagger: 80ms' format) — currently {len(staggers)}, need 3+")
        micro = re.findall(r'(?:hover|click|focus)\s*\w*\s*\**[:–|]\**\s*\w+', prd_text, re.IGNORECASE)
        if len(micro) < 5:
            fixes.append(f"Add micro-interaction specs ('hover: scale 1.03 300ms' format) — currently {len(micro)}, need 5+")
        if not re.search(r'\b(?:Barba\.js|Barba\s+\d|Swup|View\s*Transitions?\s*API)\b', prd_text, re.IGNORECASE):
            fixes.append("MUST mention a page transition library (Barba.js, Swup, or View Transitions API)")

    return fixes


if __name__ == "__main__":
    main()
