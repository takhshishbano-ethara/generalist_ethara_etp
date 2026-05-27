#!/usr/bin/env python3
"""Rubric QC.

Invoked when the tasker clicks Submit on the Rubrics tab. Checks the
rubrics themselves AND their coherence with the prompt + media. Pass
flips the third readiness flag, which (together with the other two)
enables Run Models on the Verdict tab.

Input (stdin, JSON)
-------------------
    {
      "task_id": int,
      "final_prompt": str,
      "media":   [{"filename": str, "kind": str, "size_bytes": int}],
      "rubrics": [{
        "number": int, "type": str, "category": str,
        "points": int, "importance": str, "criterion": str,
        "raw_json": str
      }]
    }

Output (stdout, JSON)
---------------------
    {"verdict": "pass" | "fail", "message": str}
"""

from __future__ import annotations

import json
import re
import sys


_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]{1,}>")

# Keep aligned with benchmarks/goku/models.py::RubricType.
_CONTROLLED_TYPES = frozenset({
    "probe_file_exists",
    "probe_file_contains",
    "probe_dir_exists",
    "shell_succeeds_real",
    "response_contains",
    "response_regex_present",
    "response_criteria",
    "response_not_criteria",
})

_MIN_RUBRICS = 3


def validate(task: dict) -> dict:
    issues: list[str] = []
    prompt = (task.get("final_prompt") or "").strip()
    media = task.get("media") or []
    rubrics = task.get("rubrics") or []

    if not prompt:
        return {
            "verdict": "fail",
            "message": "final_prompt is missing — submit the Prompts tab first.",
        }
    if not media:
        return {
            "verdict": "fail",
            "message": "no media attached — submit the Media tab first.",
        }
    if not rubrics:
        return {"verdict": "fail", "message": "no rubrics parsed"}

    if len(rubrics) < _MIN_RUBRICS:
        issues.append(
            f"only {len(rubrics)} rubric(s); want >= {_MIN_RUBRICS}"
        )

    positives = [r for r in rubrics if (r.get("points") or 0) > 0]
    negatives = [r for r in rubrics if (r.get("points") or 0) < 0]
    if not positives:
        issues.append("no positive rubric (points > 0)")
    if not negatives:
        issues.append(
            "no negative rubric (points < 0); add at least one "
            "response_not_criteria to penalize hallucinations"
        )

    mandatory = [r for r in rubrics if (r.get("importance") or "").lower() == "mandatory"]
    if not mandatory:
        issues.append("no mandatory rubric items")

    prompt_lower = prompt.lower()
    media_names = {(m.get("filename") or "").lower() for m in media}

    # Only flag mismatches for tokens that look like attachable media
    # (image / pdf / video). Output paths in probe / shell rubrics
    # commonly end in .json / .csv / .txt / .md — those are files the
    # agent CREATES at run time, not files we'd attach upfront, so
    # cross-checking them against media_names is always wrong.
    _MEDIA_EXTS = (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
        ".pdf", ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
    )

    for r in rubrics:
        num = r.get("number", "?")
        rtype = (r.get("type") or "").strip()
        crit = (r.get("criterion") or "").strip()

        if rtype not in _CONTROLLED_TYPES:
            issues.append(f"rubric #{num}: unknown type {rtype!r}")
        if not crit:
            issues.append(f"rubric #{num}: criterion is empty")
            continue
        ph = _PLACEHOLDER_RE.findall(crit)
        if ph:
            issues.append(
                f"rubric #{num}: criterion has placeholders "
                f"{sorted(set(ph))}"
            )

        # Coherence: if a criterion mentions an input-media filename
        # (image / pdf / video), it must actually be attached. Skip
        # probe_* and shell_* rubrics entirely — their filename tokens
        # describe agent OUTPUTS, not inputs.
        if rtype.startswith("probe_") or rtype.startswith("shell_"):
            continue
        for token in re.findall(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9]{2,5}", crit):
            tok = token.lower()
            if not tok.endswith(_MEDIA_EXTS):
                continue
            if tok not in media_names:
                issues.append(
                    f"rubric #{num}: references media file {token!r} "
                    f"not in attached media"
                )

    # Coherence: each rubric should have some textual anchor to either
    # the prompt or the media set. Pure boilerplate criteria slip through
    # otherwise. Cheap check: rubric criterion shares at least one
    # 4+ char word with the prompt OR mentions an attached filename.
    prompt_words = {
        w for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", prompt_lower)
    }
    if prompt_words:
        orphan = []
        for r in rubrics:
            crit_lower = (r.get("criterion") or "").lower()
            if not crit_lower:
                continue
            crit_words = set(re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", crit_lower))
            if crit_words & prompt_words:
                continue
            if any(name in crit_lower for name in media_names if name):
                continue
            orphan.append(r.get("number", "?"))
        if orphan:
            issues.append(
                f"rubric(s) {orphan} share no vocabulary with the prompt "
                f"or attached media — likely off-topic"
            )

    if issues:
        return {"verdict": "fail", "message": "; ".join(issues)}
    return {
        "verdict": "pass",
        "message": (
            f"Rubrics OK ({len(rubrics)} item(s): {len(positives)} positive, "
            f"{len(negatives)} negative, {len(mandatory)} mandatory)."
        ),
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        task = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        sys.stdout.write(json.dumps({
            "verdict": "fail",
            "message": f"bad input JSON: {e}",
        }))
        return 1
    sys.stdout.write(json.dumps(validate(task)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
