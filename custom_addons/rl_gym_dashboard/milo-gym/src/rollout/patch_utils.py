"""Patch extraction and compact filtering utilities."""

from __future__ import annotations

import re

from src.core.schemas import Trajectory

DIFF_FENCE_RE = re.compile(
    r"```(?:diff|patch)?\s*\n(.*?)```",
    re.DOTALL,
)

RAW_DIFF_RE = re.compile(
    r"(^---\s+\S+.*?^@@\s+[^@]+@@.*?)(?=\n---\s+\S+|\n```|\Z)",
    re.DOTALL | re.MULTILINE,
)

SUBMIT_TAG_RE = re.compile(
    r"<submit>\s*(.*?)\s*</submit>",
    re.DOTALL,
)

_FENCE_STRIP_RE = re.compile(r"^```(?:diff|patch)?\s*\n?|```\s*$", re.MULTILINE)


def extract_patch(content: str) -> str:
    """Extract unified diff from response text. Priority: submit tag > diff fence > raw diff."""
    match = SUBMIT_TAG_RE.search(content)
    if match:
        inner = match.group(1).strip()
        inner = _FENCE_STRIP_RE.sub("", inner).strip()
        return inner

    match = DIFF_FENCE_RE.search(content)
    if match:
        return match.group(1).strip()

    match = RAW_DIFF_RE.search(content)
    if match:
        return match.group(1).strip()

    return ""


def is_empty_patch(patch: str) -> bool:
    return not patch.strip()


def is_compact_filtered(
    trajectory: Trajectory,
    max_turns: int,
    max_context_tokens: int = 32768,
) -> bool:
    """True if trajectory should be masked from advantage computation."""
    if trajectory.hit_max_turns:
        return True
    if trajectory.hit_max_context:
        return True
    if trajectory.timed_out:
        return True
    if is_empty_patch(trajectory.patch):
        return True
    if len(trajectory.turns) >= max_turns:
        return True
    if trajectory.total_tokens >= max_context_tokens:
        return True
    return False


def estimate_token_count(text: str) -> int:
    """Fast approximate: chars / 3.5 for code."""
    return max(1, int(len(text) / 3.5))


def patch_file_list(patch: str) -> list[str]:
    files: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            files.add(line[6:].strip())
        elif line.startswith("+++ b/"):
            files.add(line[6:].strip())
    files.discard("/dev/null")
    return sorted(files)


def patch_stats(patch: str) -> dict[str, int]:
    lines_added = 0
    lines_removed = 0
    hunks = 0
    files: set[str] = set()

    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            files.add(line[6:].strip())
        elif line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1

    return {
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "files_changed": len(files),
        "hunks": hunks,
    }
