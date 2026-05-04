import json
import logging
import re
import time
from pathlib import Path

import requests
from unidiff import PatchSet

_logger = logging.getLogger(__name__)


_TEST_PATH_KEYWORDS = ["test", "tests", "e2e", "testing", "spec", "__tests__"]


def _split_patch_text(patch_text):
    test_patch = ""
    fix_patch = ""
    for hunk in PatchSet(patch_text):
        if any(kw in hunk.path.lower() for kw in _TEST_PATH_KEYWORDS):
            test_patch += str(hunk)
        else:
            fix_patch += str(hunk)
    return fix_patch, test_patch


def _fetch_via_compare_api(pull, token):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff",
    }

    org = pull.get("org")
    repo = pull.get("repo")
    base_sha = pull.get("base", {}).get("sha")
    commits = pull.get("commits", [])

    if not all([org, repo, base_sha]) or not commits:
        return "", "", None, None

    head_sha = commits[-1].get("sha")
    if not head_sha:
        return "", "", None, None

    compare_url = (
        f"https://api.github.com/repos/{org}/{repo}/compare/{base_sha}...{head_sha}"
    )
    response = requests.get(compare_url, headers=headers, timeout=60)
    if response.status_code != 200:
        raise Exception(
            f"Compare API failed: {response.status_code} - {response.text[:300]}",
        )

    patch_text = response.text
    if (
        "exceeded a secondary rate limit" in patch_text
        or "Access to this site has been restricted" in patch_text
    ):
        raise Exception("GitHub API rate limit exceeded.")

    remaining = response.headers.get("X-RateLimit-Remaining")
    reset_at = response.headers.get("X-RateLimit-Reset")

    fix_patch, test_patch = _split_patch_text(patch_text)
    return fix_patch, test_patch, remaining, reset_at


def _fetch_via_diff_url(pull, token):
    diff_url = pull.get("diff_url")
    if not diff_url:
        return "", "", None, None

    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(diff_url, headers=headers, timeout=60)
    if response.status_code != 200:
        raise Exception(
            f"diff_url fetch failed: {response.status_code} - {response.text[:300]}",
        )

    patch_text = response.text
    if (
        "exceeded a secondary rate limit" in patch_text
        or "Access to this site has been restricted" in patch_text
    ):
        raise Exception("GitHub API rate limit exceeded.")

    fix_patch, test_patch = _split_patch_text(patch_text)
    return fix_patch, test_patch, None, None


def extract_patches(pull, token):
    try:
        return _fetch_via_compare_api(pull, token)
    except Exception as compare_err:
        error_msg = str(compare_err)
        if "rate limit" in error_msg.lower():
            raise

        _logger.info(
            "PR #%s compare API failed (%s), trying diff_url fallback...",
            pull.get("number", "?"), error_msg[:120],
        )
        try:
            return _fetch_via_diff_url(pull, token)
        except Exception as fallback_err:
            _logger.debug(
                "PR #%s diff_url fallback also failed: %s",
                pull.get("number", "?"), fallback_err,
            )
            raise compare_err


def main(pool, out_dir, filtered_prs_with_issues_file,
         delay_on_error=300, retry_attempts=3, mode="swe",
         progress_callback=None):
    out_dir = Path(out_dir)
    filtered_prs_with_issues_file = Path(filtered_prs_with_issues_file)

    org_repo_re = re.compile(r"(.+)__(.+?)_(?:filtered_prs_with_issues|rct_dataset_candidates)\.jsonl")
    m = org_repo_re.match(filtered_prs_with_issues_file.name)
    if not m:
        raise ValueError(
            f"Invalid filename: {filtered_prs_with_issues_file.name}",
        )

    org = m.group(1)
    repo = m.group(2)
    out_path = out_dir / f"{org}__{repo}_raw_dataset.jsonl"

    _logger.info("Building raw dataset for %s/%s", org, repo)

    with open(filtered_prs_with_issues_file, encoding="utf-8") as f:
        prs = [json.loads(line) for line in f]

    existing = set()
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    existing.add(data["number"])
        _logger.info("Resuming: %d PRs already in dataset (will skip)", len(existing))

    total_prs = len(prs)
    count = len(existing)
    processed = 0
    skipped_existing = 0
    skipped_empty_patch = 0
    skipped_no_commits = 0
    skipped_permanent_error = 0
    skipped_all_retries_failed = 0
    built_ok = 0
    with open(out_path, "a", encoding="utf-8") as f:
        for pr in prs:
            processed += 1
            pr_num = pr.get("number", "?")
            pr_title = (pr.get("title") or "")[:80]
            base_sha = pr.get("base", {}).get("sha", "?")[:8]
            commits = pr.get("commits", [])
            head_sha = commits[-1].get("sha", "?")[:8] if commits else "none"

            if pr["number"] in existing:
                _logger.info(
                    "PR #%s [%d/%d] SKIP: already in dataset (resume mode) title=%.80s",
                    pr_num, processed, total_prs, pr_title,
                )
                skipped_existing += 1
                if progress_callback and processed % 10 == 0:
                    progress_callback(processed, total_prs, built_ok)
                continue

            if not commits:
                _logger.info(
                    "PR #%s [%d/%d] SKIP: no commits available (base=%s) title=%.80s",
                    pr_num, processed, total_prs, base_sha, pr_title,
                )
                skipped_no_commits += 1
                if progress_callback and processed % 10 == 0:
                    progress_callback(processed, total_prs, built_ok)
                continue

            _logger.info(
                "PR #%s [%d/%d] fetching diff: base=%s head=%s (%d commits) title=%.80s",
                pr_num, processed, total_prs, base_sha, head_sha, len(commits), pr_title,
            )

            token = pool.get_token()

            for attempt in range(retry_attempts):
                try:
                    fix_patch, test_patch, remaining, reset_at = extract_patches(pr, token)

                    if remaining is not None and reset_at is not None:
                        pool.report_usage(
                            token,
                            int(remaining),
                            float(reset_at),
                        )
                        _logger.info(
                            "PR #%s rate-limit: %s remaining, resets at %s",
                            pr_num, remaining, reset_at,
                        )

                    pr["fix_patch"] = fix_patch
                    pr["test_patch"] = test_patch

                    fix_lines = len(fix_patch.splitlines()) if fix_patch else 0
                    test_lines = len(test_patch.splitlines()) if test_patch else 0

                    if not fix_patch or not test_patch:
                        reason = []
                        if not fix_patch:
                            reason.append("no fix patch")
                        if not test_patch:
                            reason.append("no test patch")
                        _logger.info(
                            "PR #%s [%d/%d] SKIP: %s (fix=%d lines, test=%d lines) title=%.80s",
                            pr_num, processed, total_prs,
                            " + ".join(reason), fix_lines, test_lines, pr_title,
                        )
                        skipped_empty_patch += 1
                        break

                    f.write(json.dumps(pr, ensure_ascii=False) + "\n")
                    count += 1
                    built_ok += 1
                    _logger.info(
                        "PR #%s [%d/%d] BUILT: fix=%d lines, test=%d lines (total dataset: %d) title=%.80s",
                        pr_num, processed, total_prs, fix_lines, test_lines, count, pr_title,
                    )
                    break
                except Exception as e:
                    error_msg = str(e)
                    is_permanent = any(
                        marker in error_msg
                        for marker in ["404", "No common ancestor", "422", "Not Found"]
                    )
                    if is_permanent:
                        _logger.warning(
                            "PR #%s [%d/%d] SKIP: permanent error — %s title=%.80s",
                            pr_num, processed, total_prs, error_msg[:200], pr_title,
                        )
                        skipped_permanent_error += 1
                        break
                    if attempt == retry_attempts - 1:
                        _logger.error(
                            "PR #%s [%d/%d] FAILED: exhausted %d retries — %s title=%.80s",
                            pr_num, processed, total_prs, retry_attempts, e, pr_title,
                        )
                        skipped_all_retries_failed += 1
                    else:
                        _logger.info(
                            "PR #%s attempt %d/%d failed: %s — sleeping %ds before retry...",
                            pr_num, attempt + 1, retry_attempts, error_msg[:150], delay_on_error,
                        )
                        time.sleep(delay_on_error)
                        token = pool.get_token()

            if progress_callback and processed % 10 == 0:
                progress_callback(processed, total_prs, built_ok)

    _logger.info(
        "Dataset summary for %s/%s: %d entries built | %d total processed | "
        "skipped: %d existing, %d empty-patch, %d no-commits, %d permanent-error, %d retries-exhausted -> %s",
        org, repo, built_ok, processed,
        skipped_existing, skipped_empty_patch, skipped_no_commits,
        skipped_permanent_error, skipped_all_retries_failed, out_path,
    )
    return out_path
