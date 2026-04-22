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
    """Extract issue numbers referenced by fix/close/resolve keywords."""
    # Define 1. issue number regex pattern 2. comment regex pattern 3. keywords
    issues_pat = re.compile(r"(\w+)\s+\#(\d+)")
    comments_pat = re.compile(r"(?s)<!--.*?-->")
    keywords = {
        "close", "closes", "closed",
        "fix", "fixes", "fixed",
        "resolve", "resolves", "resolved",
    }

    # Construct text to search over for issue numbers from PR body and commit messages
    text = pull["title"] if pull["title"] else ""
    text += "\n" + (pull["body"] if pull["body"] else "")
    text += "\n" + "\n".join([commit["message"] for commit in pull["commits"]])

    # Remove comments from text
    text = comments_pat.sub("", text)
    # Look for issue numbers in text via scraping <keyword, number> patterns
    references = dict(issues_pat.findall(text))
    resolved_issues = set()
    if references:
        for word, issue_num in references.items():
            if word.lower() in keywords:
                resolved_issues.add(int(issue_num))

    if 0 in resolved_issues:
        resolved_issues.remove(0)

    return list(resolved_issues)
