"""Merge filtered PRs with their related issue details.

Vendored from multi-swe-bench/collect/merge_prs_with_issues.py.
"""
import json
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)


def main(out_dir, org, repo):
    """Merge filtered PRs with issue details.

    Args:
        out_dir: Path to output directory containing filtered PRs and issues.
        org: GitHub organization name.
        repo: GitHub repository name.

    Returns:
        Path: Path to the merged JSONL file.
    """
    out_dir = Path(out_dir)

    filtered_prs_path = out_dir / f"{org}__{repo}_filtered_prs.jsonl"
    issues_path = out_dir / f"{org}__{repo}_related_issues.jsonl"
    out_path = out_dir / f"{org}__{repo}_filtered_prs_with_issues.jsonl"

    _logger.info("Merging PRs with issues for %s/%s", org, repo)
    _logger.info("Reading filtered PRs from %s", filtered_prs_path)

    with open(filtered_prs_path, encoding="utf-8") as f:
        filtered_prs = [json.loads(line) for line in f]

    _logger.info("Loaded %d filtered PRs", len(filtered_prs))

    issues = {}
    if issues_path.exists():
        with open(issues_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    issue = json.loads(line)
                    issues[issue["number"]] = issue
                    _logger.info(
                        "Loaded issue #%d: state=%s title=%.80s",
                        issue["number"], issue.get("state", "?"), issue.get("title", ""),
                    )
    _logger.info("Loaded %d issues from %s", len(issues), issues_path)

    total = len(filtered_prs)
    count = 0
    issues_found = 0
    issues_missing = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for pull in filtered_prs:
            pr_num = pull.get("number", "?")
            pr_title = (pull.get("title") or "")[:80]
            resolved_nums = pull.get("resolved_issues", [])
            resolved = []
            found_for_pr = []
            missing_for_pr = []
            for issue_number in resolved_nums:
                if issue_number in issues:
                    resolved.append(issues[issue_number])
                    found_for_pr.append(issue_number)
                    issues_found += 1
                else:
                    missing_for_pr.append(issue_number)
                    issues_missing += 1
            pull["resolved_issues"] = resolved
            f.write(json.dumps(pull, ensure_ascii=False) + "\n")
            count += 1
            _logger.info(
                "PR #%s [%d/%d] merged: %d issues linked=%s, %d missing=%s title=%.80s",
                pr_num, count, total,
                len(found_for_pr), found_for_pr,
                len(missing_for_pr), missing_for_pr,
                pr_title,
            )

    _logger.info(
        "Merge summary: %d PRs merged, %d issue links resolved, %d issue links missing -> %s",
        count, issues_found, issues_missing, out_path,
    )
    return out_path
