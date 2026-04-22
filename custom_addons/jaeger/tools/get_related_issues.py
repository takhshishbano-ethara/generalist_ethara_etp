import json
import logging
import re
from pathlib import Path

_logger = logging.getLogger(__name__)

TOKEN_ROTATE_INTERVAL = 50


def main(pool, out_dir, filtered_prs_file):
    out_dir = Path(out_dir)
    filtered_prs_file = Path(filtered_prs_file)

    org_repo_re = re.compile(r"(.+)__(.+?)_(?:lht_)?filtered_prs\.jsonl")
    m = org_repo_re.match(filtered_prs_file.name)
    if not m:
        raise ValueError(f"Invalid filtered PRs filename: {filtered_prs_file.name}")

    org = m.group(1)
    repo = m.group(2)

    _logger.info("Fetching related issues for %s/%s", org, repo)

    with open(filtered_prs_file, encoding="utf-8") as f:
        filtered_prs = [json.loads(line) for line in f]

    target_issues = set()
    pr_to_issues = {}
    for pr in filtered_prs:
        pr_num = pr.get("number", "?")
        pr_issues = []
        for issue in pr.get("resolved_issues", []):
            if isinstance(issue, int):
                target_issues.add(issue)
                pr_issues.append(issue)
            elif isinstance(issue, dict):
                num = issue.get("number")
                if num is not None:
                    target_issues.add(num)
                    pr_issues.append(num)
        if pr_issues:
            pr_to_issues[pr_num] = pr_issues
            _logger.info("PR #%s references issues: %s", pr_num, pr_issues)
        else:
            _logger.info("PR #%s has no resolved issue numbers", pr_num)

    out_path = out_dir / f"{org}__{repo}_related_issues.jsonl"

    if not target_issues:
        _logger.info("No resolved issues to fetch. Writing empty file.")
        out_path.write_text("")
        return out_path

    g, token = pool.get_github_client(per_page=100)
    r = g.get_repo(f"{org}/{repo}")

    _logger.info(
        "Need to fetch %d unique issues referenced by %d PRs",
        len(target_issues), len(pr_to_issues),
    )

    count = 0
    fetched_ok = 0
    fetch_failed = 0
    with open(out_path, "w", encoding="utf-8") as out_f:
        for issue_num in sorted(target_issues):
            if count > 0 and count % TOKEN_ROTATE_INTERVAL == 0:
                _logger.info("Rotating GitHub token after %d API calls", count)
                pool.report_from_client(g, token)
                g, token = pool.get_github_client(per_page=100)
                r = g.get_repo(f"{org}/{repo}")

            try:
                issue = r.get_issue(issue_num)
                body_preview = (issue.body or "")[:120].replace("\n", " ")
                _logger.info(
                    "Issue #%d [%d/%d] OK: state=%s title=%.80s body=%.120s",
                    issue_num, count + 1, len(target_issues),
                    issue.state, issue.title, body_preview,
                )
                out_f.write(
                    json.dumps(
                        {
                            "org": org,
                            "repo": repo,
                            "number": issue.number,
                            "state": issue.state,
                            "title": issue.title,
                            "body": issue.body,
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                )
                fetched_ok += 1
                count += 1
            except Exception as e:
                _logger.warning(
                    "Issue #%d [%d/%d] FAILED: %s",
                    issue_num, count + 1, len(target_issues), e,
                )
                fetch_failed += 1
                count += 1

    pool.report_from_client(g, token)
    _logger.info(
        "Issues summary: %d fetched OK, %d failed, %d total -> %s",
        fetched_ok, fetch_failed, count, out_path,
    )
    return out_path
