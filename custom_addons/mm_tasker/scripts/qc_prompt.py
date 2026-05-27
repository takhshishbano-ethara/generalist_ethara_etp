#!/usr/bin/env python3
"""Prompt-only QC.

Invoked by mm_tasker when the tasker clicks Submit on the Prompts tab.
Pass = the prompt is allowed to be locked and the workflow can move on
to Media; Fail = the section stays editable and the message is shown
back to the tasker.

Input (stdin, JSON)
-------------------
    {"task_id": int, "final_prompt": str}

Output (stdout, JSON)
---------------------
    {"verdict": "pass" | "fail", "message": str}
"""

from __future__ import annotations

import json
import re
import sys


_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]{1,}>")
_MIN_PROMPT_CHARS = 50


def validate(task: dict) -> dict:
    issues: list[str] = []
    prompt = (task.get("final_prompt") or "").strip()

    if not prompt:
        return {"verdict": "fail", "message": "final_prompt is empty"}

    if len(prompt) < _MIN_PROMPT_CHARS:
        issues.append(
            f"final_prompt is too short ({len(prompt)} chars; "
            f"want >= {_MIN_PROMPT_CHARS})"
        )
    placeholders = _PLACEHOLDER_RE.findall(prompt)
    if placeholders:
        issues.append(
            f"final_prompt has unresolved placeholders: "
            f"{sorted(set(placeholders))}"
        )
    if "results/" in prompt or "/workspace/" in prompt:
        issues.append(
            "final_prompt mentions a path prefix (results/ or /workspace/); "
            "use bare filenames per the Goku guidelines"
        )

    if issues:
        return {"verdict": "fail", "message": "; ".join(issues)}
    return {
        "verdict": "pass",
        "message": f"Prompt OK ({len(prompt)} chars).",
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
