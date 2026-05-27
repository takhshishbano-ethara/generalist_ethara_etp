#!/usr/bin/env python3
"""Media QC.

Invoked when the tasker clicks Submit on the Media tab. Checks the media
itself AND that it is consistent with the (already-locked) prompt. Pass
unlocks the Rubrics step.

Input (stdin, JSON)
-------------------
    {
      "task_id": int,
      "final_prompt": str,
      "media": [{"filename": str, "kind": str, "size_bytes": int}]
    }

Output (stdout, JSON)
---------------------
    {"verdict": "pass" | "fail", "message": str}
"""

from __future__ import annotations

import json
import os
import re
import sys


_PROMPT_FILENAME_RE = re.compile(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9]{2,5}")
_MAX_BYTES_PER_FILE = 50 * 1024 * 1024  # 50 MB; tune once we have real data
_ALLOWED_KINDS = frozenset({"image", "pdf", "video"})


def validate(task: dict) -> dict:
    issues: list[str] = []
    prompt = (task.get("final_prompt") or "").strip()
    media = task.get("media") or []

    if not prompt:
        return {
            "verdict": "fail",
            "message": "final_prompt is missing — submit the Prompts tab first.",
        }

    if not media:
        return {"verdict": "fail", "message": "no media attached"}

    seen_names: set[str] = set()
    for idx, m in enumerate(media, start=1):
        fname = (m.get("filename") or "").strip()
        kind = (m.get("kind") or "").strip().lower()
        size = int(m.get("size_bytes") or 0)

        if not fname:
            issues.append(f"media #{idx}: filename is empty")
            continue

        lower = fname.lower()
        if lower in seen_names:
            issues.append(f"media #{idx}: duplicate filename {fname!r}")
        seen_names.add(lower)

        if kind == "other" or kind not in _ALLOWED_KINDS:
            issues.append(
                f"media #{idx} ({fname}): kind {kind!r} not in "
                f"image/pdf/video — fix the extension or set kind manually"
            )
        if size <= 0:
            issues.append(f"media #{idx} ({fname}): empty file")
        elif size > _MAX_BYTES_PER_FILE:
            issues.append(
                f"media #{idx} ({fname}): {size} bytes exceeds the "
                f"{_MAX_BYTES_PER_FILE} byte limit"
            )

        if "/" in fname or "\\" in fname:
            issues.append(
                f"media #{idx}: filename {fname!r} contains a path separator; "
                "use a bare filename"
            )

    # Cross-check: every bare filename referenced in the prompt should map
    # to an attached media item. Catches the typical "edited the prompt
    # but forgot to upload the new screenshot" mistake.
    referenced = {
        m.group(0).lower()
        for m in _PROMPT_FILENAME_RE.finditer(prompt)
    }
    attached = {(m.get("filename") or "").lower() for m in media}
    # Filter out tokens that don't look like media (e.g. "config.yaml"
    # mentioned in passing) — only flag image/pdf/video extensions.
    media_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                  ".tif", ".tiff", ".pdf", ".mp4", ".mov", ".avi",
                  ".mkv", ".webm", ".m4v")
    missing = sorted(
        name for name in referenced
        if name.endswith(media_exts) and name not in attached
    )
    if missing:
        issues.append(
            f"prompt references media not attached: {missing}"
        )

    # Heads-up (not a hard fail): media attached but never referenced.
    extras = sorted(
        name for name in attached
        if name and name not in referenced
        and os.path.splitext(name)[1] in media_exts
    )
    notes = ""
    if extras and not issues:
        notes = f" Note: {len(extras)} attached file(s) not mentioned in the prompt — fine, just flagging."

    if issues:
        return {"verdict": "fail", "message": "; ".join(issues)}
    return {
        "verdict": "pass",
        "message": f"Media OK ({len(media)} file(s)).{notes}",
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
