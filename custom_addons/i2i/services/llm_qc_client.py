from __future__ import annotations

import json
import logging
import os

from . import openrouter_client

_logger = logging.getLogger(__name__)


_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_QC_SYSTEM_PROMPT_PATH = os.path.join(_MODULE_ROOT, "qc_system_prompt.md")
_cached_qc_system_prompt: str | None = None


class LLMQCConfigError(Exception):
    pass


def load_qc_system_prompt() -> str:
    global _cached_qc_system_prompt
    if _cached_qc_system_prompt is not None:
        return _cached_qc_system_prompt
    try:
        with open(_QC_SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise LLMQCConfigError(
            f"Cannot read qc_system_prompt.md at {_QC_SYSTEM_PROMPT_PATH}: {exc}"
        ) from exc
    if not text.strip():
        raise LLMQCConfigError(
            f"qc_system_prompt.md at {_QC_SYSTEM_PROMPT_PATH} is empty."
        )
    _cached_qc_system_prompt = text
    return text


_FIELD_TO_YES_VALUE = {
    "edit_only_instructed": "instruction_aligned",
    "images_aligned": "images_aligned",
    "free_of_ai_slop": "slop_free",
}


def _map_verdict(field: str, verdict) -> str | bool:
    if not verdict:
        return False
    s = str(verdict).strip().upper()
    if s == "YES":
        return _FIELD_TO_YES_VALUE[field]
    if s == "NO":
        return "no"
    return False


def _extract_content(response_body):
    try:
        return response_body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise openrouter_client.OpenRouterAPIError(
            f"Malformed response (no choices[0].message.content): {response_body}"
        ) from exc


def _parse_json_payload(content):
    if isinstance(content, dict):
        return content
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise openrouter_client.OpenRouterAPIError(
            f"LLM response was not valid JSON: {text[:300]}"
        ) from exc


def _trim(s, limit=400):
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit - 1].rstrip() + "\u2026"


def _summarise_artifacts(items, label):
    if not items:
        return ""
    parts = []
    for item in items[:6]:
        if not isinstance(item, dict):
            continue
        cat = (item.get("category") or "").strip()
        desc = (item.get("description") or "").strip()
        if cat and desc:
            parts.append(f"{cat}: {desc}")
        elif desc:
            parts.append(desc)
        elif cat:
            parts.append(cat)
    if not parts:
        return ""
    return f"{label}: " + "; ".join(parts)


def _build_reasoning(parsed: dict) -> str:
    gt = parsed.get("ground_truth") or {}
    q1 = gt.get("q1") or {}
    q2 = gt.get("q2") or {}
    q3 = gt.get("q3") or {}

    lines = []
    decision = (parsed.get("decision_one_line") or "").strip()
    if decision:
        lines.append(f"Decision: {decision}")

    rubric = (parsed.get("rubric_verdict") or "").strip().upper()
    final = (parsed.get("final_verdict") or "").strip().upper()
    if rubric or final:
        verdict_line = []
        if rubric:
            verdict_line.append(f"Rubric: {rubric}")
        if final and final != rubric:
            verdict_line.append(f"Final: {final}")
        if verdict_line:
            lines.append(" | ".join(verdict_line))

    def _axis_line(label, axis, *evidence_keys):
        verdict = (axis.get("verdict") or "").strip().upper() or "?"
        code = (axis.get("auto_fail_code") or "").strip() or "NONE"
        evidences = []
        for key in evidence_keys:
            val = (axis.get(key) or "").strip()
            if val:
                evidences.append(_trim(val, 240))
        ev_txt = (" " + " ".join(evidences)) if evidences else ""
        return f"Q1/Q2/Q3 [{label}] {verdict} (code={code}).{ev_txt}".strip()

    if q1:
        lines.append(_axis_line("Q1 Instruction", q1, "completeness_evidence", "precision_evidence"))
    if q2:
        lines.append(_axis_line("Q2 Alignment", q2, "alignment_evidence"))
    if q3:
        which = (q3.get("which_image_failed") or "").strip().upper() or "NONE"
        verdict = (q3.get("verdict") or "").strip().upper() or "?"
        code = (q3.get("auto_fail_code") or "").strip() or "NONE"
        lines.append(
            f"Q1/Q2/Q3 [Q3 Slop] {verdict} (code={code}, failed_image={which})."
        )
        orig_summary = _summarise_artifacts(q3.get("original_image_artifacts"), "Original")
        edited_summary = _summarise_artifacts(q3.get("edited_image_artifacts"), "Edited")
        if orig_summary:
            lines.append(orig_summary)
        if edited_summary:
            lines.append(edited_summary)

    findings = parsed.get("findings") or []
    if isinstance(findings, list) and findings:
        lines.append("Findings:")
        for f in findings[:8]:
            if not isinstance(f, dict):
                continue
            code = (f.get("code") or "").strip() or "?"
            axis = (f.get("axis") or "").strip() or "?"
            evidence = _trim(f.get("evidence"), 200)
            fix = _trim(f.get("required_fix"), 200)
            location = _trim(f.get("location"), 120)
            seg = f"- [{axis}/{code}]"
            if location:
                seg += f" {location}"
            if evidence:
                seg += f" \u2014 {evidence}"
            if fix:
                seg += f" (fix: {fix})"
            lines.append(seg)

    return "\n".join(lines).strip()


def review_image_pair(
    *,
    api_key,
    model,
    instruction,
    original_url,
    edited_url,
    http_referer=None,
    app_title=None,
    max_retries=3,
):
    system_prompt = load_qc_system_prompt()
    user_text = (
        f"INSTRUCTION: {instruction}\n\n"
        f"IMAGE 1 (Original): {original_url}\n"
        f"IMAGE 2 (Edited): {edited_url}\n\n"
        "No TASKER_SUBMISSION is provided. Operate in LABEL mode. "
        "Emit exactly one JSON object that conforms to the OUTPUT CONTRACT."
    )

    response = openrouter_client.chat_completion_vision(
        api_key,
        model=model,
        system_prompt=system_prompt,
        user_text=user_text,
        image_urls=[original_url, edited_url],
        response_format={"type": "json_object"},
        http_referer=http_referer,
        app_title=app_title,
        max_retries=max_retries,
    )

    content = _extract_content(response)
    parsed = _parse_json_payload(content)
    if not isinstance(parsed, dict):
        raise openrouter_client.OpenRouterAPIError(
            f"LLM JSON payload was not an object: {parsed!r}"
        )

    gt = parsed.get("ground_truth") or {}
    q1_verdict = (gt.get("q1") or {}).get("verdict")
    q2_verdict = (gt.get("q2") or {}).get("verdict")
    q3_verdict = (gt.get("q3") or {}).get("verdict")

    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    tokens = int(usage.get("total_tokens", 0) or 0)

    return {
        "edit_only_instructed": _map_verdict("edit_only_instructed", q1_verdict),
        "images_aligned": _map_verdict("images_aligned", q2_verdict),
        "free_of_ai_slop": _map_verdict("free_of_ai_slop", q3_verdict),
        "reasoning": _build_reasoning(parsed),
        "tokens": tokens,
        "raw": parsed,
    }
