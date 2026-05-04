"""Filter merged PRs to keep only bounty-related (RCT) instances.

Reads the merged file (filtered_prs_with_issues.jsonl) and keeps only PRs
whose connected issues or the PR itself have bounty signals.

Detection hierarchy:
  1. Issue labels (primary — bounty platforms label the ISSUE)
  2. PR labels (secondary — some repos label PRs too)
  3. Issue body text (tertiary — bounty bot comments)
  4. PR body text (quaternary — /claim commands)
"""
import json
import logging
import re
from pathlib import Path

_logger = logging.getLogger(__name__)

# ── Bounty Detection Patterns ──────────────────────────────────────────

# Exact label matches (case-sensitive for emoji labels)
_BOUNTY_LABELS_EXACT = {
    "💎 Bounty",
    "💰 Rewarded",
}

# Case-insensitive label matches
_BOUNTY_LABELS_CI = {
    "bounty", "funded", "gitcoin-funded", "issuehunt",
    "reward", "paid", "sponsored",
}

# Label prefixes (case-insensitive)
_BOUNTY_LABEL_PREFIXES = ("bounty/", "bounty:", "$", "💎", "💰")

# Body text regex patterns
_BOUNTY_BODY_PATTERNS = [
    re.compile(r"/bounty\s+\$\d+", re.IGNORECASE),
    re.compile(r"💎\s*\$\d+\s*bounty", re.IGNORECASE),
    re.compile(r"has been awarded\s+\*{0,2}\$\d+", re.IGNORECASE),
    re.compile(r"algora\.io", re.IGNORECASE),
    re.compile(r"gitcoin\.co", re.IGNORECASE),
    re.compile(r"issuehunt\.io", re.IGNORECASE),
    re.compile(r"bountysource\.com", re.IGNORECASE),
]

# PR body claim patterns
_CLAIM_PATTERNS = [
    re.compile(r"/claim\s+#\d+", re.IGNORECASE),
]


def _is_bounty_label(label_name):
    """Check if a label name indicates a bounty."""
    if label_name in _BOUNTY_LABELS_EXACT:
        return True
    if label_name.lower() in _BOUNTY_LABELS_CI:
        return True
    lower = label_name.lower()
    if any(lower.startswith(p) for p in _BOUNTY_LABEL_PREFIXES):
        return True
    return False


def _check_body_for_bounty(body):
    """Check if body text contains bounty signals."""
    if not body:
        return False
    for pat in _BOUNTY_BODY_PATTERNS:
        if pat.search(body):
            return True
    return False


def _check_pr_claim(pr_body):
    """Check if PR body contains a bounty claim command."""
    if not pr_body:
        return False
    for pat in _CLAIM_PATTERNS:
        if pat.search(pr_body):
            return True
    return False


def _detect_bounty_signals(pull):
    """Detect all bounty signals for a PR and its connected issues.

    Returns:
        tuple: (is_bounty: bool, signals: list[str])
    """
    signals = []

    # Signal 1: Issue labels (primary)
    for issue in pull.get("resolved_issues", []):
        issue_labels = issue.get("labels", [])
        for lbl in issue_labels:
            if _is_bounty_label(lbl):
                signals.append(f"issue#{issue.get('number')} label: {lbl}")

    # Signal 2: PR labels (secondary)
    pr_labels = pull.get("labels", [])
    for lbl in pr_labels:
        if _is_bounty_label(lbl):
            signals.append(f"PR label: {lbl}")

    # Signal 3: Issue body (tertiary)
    for issue in pull.get("resolved_issues", []):
        issue_body = issue.get("body", "")
        if _check_body_for_bounty(issue_body):
            signals.append(f"issue#{issue.get('number')} body matches bounty pattern")

    # Signal 4: PR body (quaternary)
    pr_body = pull.get("body", "")
    if _check_pr_claim(pr_body):
        signals.append("PR body contains /claim")
    if _check_body_for_bounty(pr_body):
        signals.append("PR body matches bounty pattern")

    return len(signals) > 0, signals


def main(out_dir, org, repo, progress_callback=None):
    """Filter merged PRs to keep only bounty-related (RCT) instances.

    Args:
        out_dir: Path to output directory.
        org: GitHub organization name.
        repo: GitHub repository name.
        progress_callback: Optional callback(processed, total, passed).

    Returns:
        Path: Path to the RCT-filtered output file.
    """
    out_dir = Path(out_dir)

    merged_file = out_dir / f"{org}__{repo}_filtered_prs_with_issues.jsonl"
    out_path = out_dir / f"{org}__{repo}_rct_dataset_candidates.jsonl"

    if not merged_file.exists():
        raise FileNotFoundError(f"Merged PRs file not found: {merged_file}")

    _logger.info("RCT filtering for %s/%s", org, repo)

    with open(merged_file, encoding="utf-8") as f:
        prs = [json.loads(line) for line in f if line.strip()]

    total = len(prs)
    passed = 0
    skipped_no_signal = 0

    with open(out_path, "w", encoding="utf-8") as out_f:
        for i, pull in enumerate(prs, 1):
            pr_num = pull.get("number", "?")
            pr_title = (pull.get("title") or "")[:80]

            is_bounty, signals = _detect_bounty_signals(pull)

            if is_bounty:
                out_f.write(json.dumps(pull, ensure_ascii=False) + "\n")
                passed += 1
                _logger.info(
                    "PR #%s [%d/%d] RCT-PASS: %d signals=%s title=%.80s",
                    pr_num, i, total, len(signals), signals, pr_title,
                )
            else:
                skipped_no_signal += 1
                _logger.info(
                    "PR #%s [%d/%d] RCT-SKIP: no bounty signal title=%.80s",
                    pr_num, i, total, pr_title,
                )

            if progress_callback and i % 10 == 0:
                progress_callback(i, total, passed)

    _logger.info(
        "RCT filter summary for %s/%s: %d/%d passed | %d skipped (no bounty signal) -> %s",
        org, repo, passed, total, skipped_no_signal, out_path,
    )

    return out_path
