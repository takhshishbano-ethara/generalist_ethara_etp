import json
import logging
from pathlib import Path

from .util import extract_resolved_issues

_logger = logging.getLogger(__name__)

TOKEN_ROTATE_INTERVAL = 50


def main(pool, out_dir, prs_file, mode="swe", skip_commit_message=False):
    out_dir = Path(out_dir)
    prs_file = Path(prs_file)

    stem = prs_file.stem
    parts = stem.rsplit("_prs", 1)
    org_repo = parts[0]
    org, repo = org_repo.split("__", 1)

    _logger.info("Filtering PRs for %s/%s (mode=%s)", org, repo, mode)

    if mode == "lht":
        out_filename = f"{org}__{repo}_lht_filtered_prs.jsonl"
    else:
        out_filename = f"{org}__{repo}_filtered_prs.jsonl"

    g, token, r = None, None, None
    if not skip_commit_message:
        g, token = pool.get_github_client(per_page=100)
        r = g.get_repo(f"{org}/{repo}")

    with open(prs_file, encoding="utf-8") as in_f:
        prs = [json.loads(line) for line in in_f]

    total_prs = len(prs)
    count = 0
    skipped_not_closed = 0
    skipped_not_merged = 0
    skipped_no_issues = 0
    skipped_commit_error = 0
    api_calls = 0
    with open(out_dir / out_filename, "w", encoding="utf-8") as out_f:
        for i, pull in enumerate(prs, 1):
            pr_num = pull.get("number", "?")
            pr_title = (pull.get("title") or "")[:80]

            if pull["state"] != "closed":
                _logger.info(
                    "PR #%s [%d/%d] SKIP: state=%s (not closed) title=%.80s",
                    pr_num, i, total_prs, pull["state"], pr_title,
                )
                skipped_not_closed += 1
                continue

            if mode == "lht" and not pull.get("merged_at"):
                _logger.info(
                    "PR #%s [%d/%d] SKIP: not merged (LHT requires merge) title=%.80s",
                    pr_num, i, total_prs, pr_title,
                )
                skipped_not_merged += 1
                continue

            pull["commits"] = []
            if not skip_commit_message and r:
                if api_calls > 0 and api_calls % TOKEN_ROTATE_INTERVAL == 0:
                    _logger.info("Rotating GitHub token after %d API calls", api_calls)
                    pool.report_from_client(g, token)
                    g, token = pool.get_github_client(per_page=100)
                    r = g.get_repo(f"{org}/{repo}")

                try:
                    pr_obj = r.get_pull(pull["number"])
                    commits = list(pr_obj.get_commits())
                    pull["commits"] = [
                        {
                            "sha": commit.sha,
                            "parents": [p.sha for p in commit.parents],
                            "message": commit.commit.message,
                        }
                        for commit in commits
                    ]
                    api_calls += 1
                    _logger.info(
                        "PR #%s [%d/%d] fetched %d commits",
                        pr_num, i, total_prs, len(commits),
                    )
                except Exception as e:
                    _logger.warning(
                        "PR #%s [%d/%d] WARN: commit fetch failed: %s",
                        pr_num, i, total_prs, e,
                    )
                    skipped_commit_error += 1
                    api_calls += 1

            resolved_issues = extract_resolved_issues(pull)

            if mode == "swe" and len(resolved_issues) == 0:
                _logger.info(
                    "PR #%s [%d/%d] SKIP: no resolved issues (SWE requires fix/close/resolve keyword) title=%.80s",
                    pr_num, i, total_prs, pr_title,
                )
                skipped_no_issues += 1
                continue

            pull["resolved_issues"] = resolved_issues
            out_f.write(json.dumps(pull, ensure_ascii=False) + "\n")
            count += 1
            _logger.info(
                "PR #%s [%d/%d] PASS: %d resolved issues=%s title=%.80s",
                pr_num, i, total_prs, len(resolved_issues), resolved_issues, pr_title,
            )

    _logger.info(
        "Filter summary for %s/%s: %d/%d passed | skipped: %d not-closed, %d not-merged, "
        "%d no-resolved-issues, %d commit-fetch-errors",
        org, repo, count, total_prs, skipped_not_closed, skipped_not_merged,
        skipped_no_issues, skipped_commit_error,
    )

    if g and token:
        pool.report_from_client(g, token)

    out_path = out_dir / out_filename
    _logger.info("Filtered to %d PRs -> %s", count, out_path)
    return out_path
