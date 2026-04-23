import json
import logging
import time
from pathlib import Path

from .util import extract_resolved_issues

_logger = logging.getLogger(__name__)

TOKEN_ROTATE_INTERVAL = 50
_MAX_RETRIES = 3
_RETRY_BACKOFF = 10


def _fetch_commits_with_retry(pool, g, token, r, org, repo, pr_number, retries=_MAX_RETRIES):
    for attempt in range(1, retries + 1):
        try:
            pr_obj = r.get_pull(pr_number)
            commits = list(pr_obj.get_commits())
            return g, token, r, [
                {
                    "sha": c.sha,
                    "parents": [p.sha for p in c.parents],
                    "message": c.commit.message,
                }
                for c in commits
            ]
        except Exception as e:
            is_last = attempt == retries
            is_network = "NameResolution" in str(e) or "ConnectionError" in str(type(e).__name__) or "MaxRetry" in str(e)
            if is_network and not is_last:
                wait = _RETRY_BACKOFF * attempt
                _logger.warning(
                    "PR #%s attempt %d/%d network error: %s — retrying in %ds",
                    pr_number, attempt, retries, e, wait,
                )
                time.sleep(wait)
                pool.report_from_client(g, token)
                g, token = pool.get_github_client(per_page=100)
                r = g.get_repo(f"{org}/{repo}")
                continue
            raise


def main(pool, out_dir, prs_file, mode="swe", skip_commit_message=False,
         progress_callback=None):
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
                if progress_callback and i % 10 == 0:
                    progress_callback(i, total_prs, count)
                continue

            if mode == "lht" and not pull.get("merged_at"):
                _logger.info(
                    "PR #%s [%d/%d] SKIP: not merged (LHT requires merge) title=%.80s",
                    pr_num, i, total_prs, pr_title,
                )
                skipped_not_merged += 1
                if progress_callback and i % 10 == 0:
                    progress_callback(i, total_prs, count)
                continue

            pull["commits"] = []
            if not skip_commit_message and r:
                if api_calls > 0 and api_calls % TOKEN_ROTATE_INTERVAL == 0:
                    _logger.info("Rotating GitHub token after %d API calls", api_calls)
                    try:
                        pool.report_from_client(g, token)
                        g, token = pool.get_github_client(per_page=100)
                        r = g.get_repo(f"{org}/{repo}")
                    except Exception as e:
                        _logger.warning(
                            "Token rotation failed at PR #%s [%d/%d]: %s — retrying after backoff",
                            pr_num, i, total_prs, e,
                        )
                        time.sleep(_RETRY_BACKOFF)
                        try:
                            g, token = pool.get_github_client(per_page=100)
                            r = g.get_repo(f"{org}/{repo}")
                        except Exception as e2:
                            _logger.error(
                                "Token rotation retry failed at PR #%s: %s — skipping PR",
                                pr_num, e2,
                            )
                            skipped_commit_error += 1
                            api_calls += 1
                            continue

                try:
                    g, token, r, commit_data = _fetch_commits_with_retry(
                        pool, g, token, r, org, repo, pull["number"],
                    )
                    pull["commits"] = commit_data
                    api_calls += 1
                    _logger.info(
                        "PR #%s [%d/%d] fetched %d commits",
                        pr_num, i, total_prs, len(commit_data),
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
                if progress_callback and i % 10 == 0:
                    progress_callback(i, total_prs, count)
                continue

            pull["resolved_issues"] = resolved_issues
            out_f.write(json.dumps(pull, ensure_ascii=False) + "\n")
            count += 1
            _logger.info(
                "PR #%s [%d/%d] PASS: %d resolved issues=%s title=%.80s",
                pr_num, i, total_prs, len(resolved_issues), resolved_issues, pr_title,
            )

            if progress_callback and i % 10 == 0:
                progress_callback(i, total_prs, count)

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
