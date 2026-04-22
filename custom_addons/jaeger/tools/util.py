"""Shared utilities for Jaeger vendored tools."""
import logging
import re
from datetime import datetime

_logger = logging.getLogger(__name__)


def datetime_serializer(obj):
    """JSON serializer for datetime objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def extract_resolved_issues(pull):
    """Extract issue numbers referenced by fix/close/resolve keywords in PR.

    Args:
        pull: dict with 'title', 'body', 'commits' keys.

    Returns:
        list[int]: Deduplicated issue numbers.
    """
    issues_pat = re.compile(r"(\w+)\s+\#(\d+)")
    comments_pat = re.compile(r"(?s)<!--.*?-->")
    keywords = {
        "close", "closes", "closed",
        "fix", "fixes", "fixed",
        "resolve", "resolves", "resolved",
    }

    text = pull.get("title") or ""
    text += "\n" + (pull.get("body") or "")
    text += "\n" + "\n".join(c.get("message", "") for c in pull.get("commits", []))

    text = comments_pat.sub("", text)
    resolved = set()
    for word, issue_num in issues_pat.findall(text):
        if word.lower() in keywords:
            num = int(issue_num)
            if num > 0:
                resolved.add(num)
    return sorted(resolved)
