# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

# Adapted for the Aurora Pipeline Odoo addon (LGPL-3).
# Apache 2.0 is forward-compatible with LGPL-3 per ASF/FSF guidance.

"""Build the final dataset from tag groups.

For each tag group (version range):
  1. Fetch the unified diff between base_sha and head_sha
     - Primary: GitHub compare API with diff accept header
     - Fallback: clone repo locally (bare blobless) and run git diff
  2. Split into fix_patch and test_patch using unidiff
  3. Aggregate all resolved issues from the PRs in the group
     - If a PR has linked issues, include those issues
     - If a PR has no linked issues, use the PR title/body as a pseudo-issue
     - If an issue body is empty, substitute with the PR description
  4. Write the final record to JSONL

Outputs: {org}__{repo}_lht_dataset.jsonl

NOTE: This module is NOT safe for concurrent execution on the same output
file.  Resume support (existing_ids check) assumes single-process access.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from tqdm import tqdm
from unidiff import PatchSet

try:
    from .util import get_tokens, optional_int, AuroraPipelineError, TokenRotator, validate_name, clone_repo_bare
except ImportError:
    from util import get_tokens, optional_int, AuroraPipelineError, TokenRotator, validate_name, clone_repo_bare

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo Clone Cache — bare blobless clone for local diff generation
# ---------------------------------------------------------------------------


class RepoCloneCache:
    def __init__(self, cache_dir: str = ".repo_cache", auth_token: str = ""):
        self._cache_dir = cache_dir
        self._auth_token = auth_token

    def ensure_cloned(self, org: str, repo: str) -> Path:
        result = clone_repo_bare(
            org, repo, self._cache_dir, auth_token=self._auth_token,
        )
        if result is None:
            raise RuntimeError(f"Failed to clone {org}/{repo}")
        return result

    def get_diff(self, org: str, repo: str, base_sha: str, head_sha: str) -> str:
        if not base_sha or not head_sha:
            raise ValueError("base_sha and head_sha must be non-empty")
        repo_path = self.ensure_cloned(org, repo)
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "diff", f"{base_sha}...{head_sha}"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "git is not installed or not on PATH."
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"git diff timed out (120s) for {org}/{repo} "
                f"({base_sha[:8]}...{head_sha[:8]})."
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"git diff failed for {org}/{repo} "
                f"({base_sha[:8]}...{head_sha[:8]}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout


# ---------------------------------------------------------------------------
# Diff fetching with fallback
# ---------------------------------------------------------------------------


def fetch_unified_diff(
    org: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    token: str,
    clone_cache: RepoCloneCache,
) -> str:
    """Fetch the unified diff between two commits.

    Tries the GitHub compare API first, falls back to local clone if
    the API returns an error (406 for large diffs, etc.).
    """
    if not base_sha or not head_sha:
        raise ValueError("base_sha and head_sha must be non-empty")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
    }
    compare_url = (
        f"https://api.github.com/repos/{org}/{repo}/compare/{base_sha}...{head_sha}"
    )

    try:
        response = requests.get(compare_url, headers=headers, timeout=60)
        if response.status_code == 200:
            diff_text = response.text
            if diff_text and diff_text.strip():
                return diff_text
            # Empty diff from API — try clone fallback
        elif response.status_code in (403, 429):
            # Rate limited — raise so retry loop can sleep and retry
            retry_after = response.headers.get("Retry-After", "60")
            raise Exception(
                f"Rate limited ({response.status_code}), retry after {retry_after}s"
            )
        elif response.status_code in (406, 422, 500, 502, 503):
            # 406 = diff too large, others = server issues — fall back to clone
            _logger.info(
                f"  GitHub API returned {response.status_code} for "
                f"{base_sha[:8]}...{head_sha[:8]}, using clone fallback"
            )
        else:
            _logger.info(
                f"  GitHub API returned {response.status_code} for "
                f"{base_sha[:8]}...{head_sha[:8]}, trying clone fallback"
            )
    except requests.RequestException as e:
        _logger.info(f"  GitHub API request failed: {e}, using clone fallback")

    # Fallback: clone-based diff
    return clone_cache.get_diff(org, repo, base_sha, head_sha)


# ---------------------------------------------------------------------------
# Patch splitting
# ---------------------------------------------------------------------------

TEST_PATH_KEYWORDS = ["test", "tests", "e2e", "testing", "spec", "__tests__"]


def split_patches(diff_text: str) -> tuple[str, str]:
    """Split a unified diff into fix_patch and test_patch.

    Uses path-based heuristics to classify hunks as test or fix code.
    """
    test_patch = ""
    fix_patch = ""

    try:
        for hunk in PatchSet(diff_text):
            path_lower = hunk.path.lower()
            if any(kw in path_lower for kw in TEST_PATH_KEYWORDS):
                test_patch += str(hunk)
            else:
                fix_patch += str(hunk)
    except Exception as e:
        # If unidiff fails to parse (e.g. binary files, malformed diff),
        # return the raw diff as fix_patch so the record isn't silently lost
        _logger.warning(f"  Warning: unidiff parse failed, using raw diff as fix_patch: {e}")
        return diff_text, ""

    return fix_patch, test_patch


# ---------------------------------------------------------------------------
# Issue aggregation
# ---------------------------------------------------------------------------

ISSUE_REF_PATTERN = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)
ISSUE_URL_PATTERN = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")


def extract_issue_numbers_from_body(body: str) -> list[int]:
    """Extract issue numbers referenced in a PR body."""
    if not body:
        return []
    numbers: set[int] = set()
    for m in ISSUE_REF_PATTERN.finditer(body):
        num = int(m.group(1))
        if num > 0:
            numbers.add(num)
    for m in ISSUE_URL_PATTERN.finditer(body):
        num = int(m.group(1))
        if num > 0:
            numbers.add(num)
    return sorted(numbers)


def aggregate_issues(
    group_prs: list[dict],
    all_issues: dict[int, dict],
) -> list[dict]:
    """Aggregate resolved issues for all PRs in a bundle.

    - If a PR links to issues, include those issues.
    - If the issue body is empty, substitute with the PR description.
    - If a PR has no linked issues, add the PR itself as a pseudo-issue.

    Returns list of {number, title, body} dicts.
    """
    result: list[dict] = []
    seen_numbers: set[int] = set()

    for pr in group_prs:
        pr_num = pr.get("number", 0)
        pr_body = pr.get("body") or ""
        pr_title = pr.get("title") or ""

        # Collect all issue numbers (deduplicated via set)
        issue_num_set: set[int] = set(extract_issue_numbers_from_body(pr_body))

        # Also check resolved_issues field if present (from filter_prs step)
        resolved = pr.get("resolved_issues", [])
        if isinstance(resolved, list):
            for item in resolved:
                if isinstance(item, int) and item > 0:
                    issue_num_set.add(item)
                elif isinstance(item, dict):
                    num = item.get("number", 0)
                    if num:
                        issue_num_set.add(num)

        has_real_issues = False
        for issue_num in sorted(issue_num_set):
            if issue_num in seen_numbers:
                continue
            seen_numbers.add(issue_num)

            if issue_num in all_issues:
                issue = all_issues[issue_num]
                issue_body = issue.get("body") or ""
                # If issue body is empty, substitute with PR description
                if not issue_body.strip():
                    issue_body = pr_body
                result.append(
                    {
                        "number": issue_num,
                        "title": issue.get("title", ""),
                        "body": issue_body,
                    }
                )
                has_real_issues = True

        # If no linked issues found, add the PR itself as a pseudo-issue
        if not has_real_issues and pr_num not in seen_numbers:
            seen_numbers.add(pr_num)
            result.append(
                {
                    "number": pr_num,
                    "title": pr_title,
                    "body": pr_body,
                }
            )

    return result


# ---------------------------------------------------------------------------
# CLI & main
# ---------------------------------------------------------------------------


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build dataset from tag groups with unified diffs."
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument(
        "--tokens",
        type=str,
        nargs="*",
        default=None,
        help="API token(s) or path to token file.",
    )
    parser.add_argument("--org", type=str, required=True, help="Organization name.")
    parser.add_argument("--repo", type=str, required=True, help="Repository name.")
    parser.add_argument(
        "--delay-on-error",
        type=optional_int,
        default=300,
        help="Delay in seconds before retrying on error. If none, exit on error.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help="Number of attempts to retry on error.",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=".repo_cache",
        help="Directory for cached bare git clones (default: .repo_cache).",
    )
    parser.add_argument(
        "--lang",
        type=str,
        default="python",
        help="Programming language of the repository (default: python).",
    )
    return parser


def main(
    tokens: list[str],
    out_dir: Path,
    org: str,
    repo: str,
    delay_on_error: Optional[int] = 300,
    retry_attempts: int = 3,
    cache_dir: str = ".repo_cache",
    lang: str = "python",
):
    if not tokens:
        raise ValueError("No tokens provided")

    _logger.info("starting build dataset")
    validate_name(org, "org")
    validate_name(repo, "repo")
    _logger.info(f"Output directory: {out_dir}")
    _logger.info(f"Org: {org}")
    _logger.info(f"Repo: {repo}")

    clone_cache = RepoCloneCache(cache_dir, auth_token=tokens[0] if tokens else "")
    rotator = TokenRotator(tokens)

    # Load tag groups
    groups_file = out_dir / f"{org}__{repo}_tag_groups.jsonl"
    if not groups_file.exists():
        raise AuroraPipelineError(f"Tag groups file not found: {groups_file}")
    with open(groups_file, "r", encoding="utf-8") as f:
        tag_groups = [json.loads(line) for line in f if line.strip()]
    _logger.info(f"Loaded {len(tag_groups)} tag groups")

    # Load PRs (need full PR data for issue aggregation)
    prs_file = out_dir / f"{org}__{repo}_lht_filtered_prs.jsonl"
    pr_lookup: dict[int, dict] = {}
    if prs_file.exists():
        with open(prs_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        pr = json.loads(line)
                        num = pr.get("number")
                        if num is not None:
                            pr_lookup[num] = pr
                    except json.JSONDecodeError:
                        continue
    _logger.info(f"Loaded {len(pr_lookup)} PRs")

    # Load related issues (if available)
    issues_file = out_dir / f"{org}__{repo}_related_issues.jsonl"
    all_issues: dict[int, dict] = {}
    if issues_file.exists():
        with open(issues_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        issue = json.loads(line)
                        num = issue.get("number")
                        if num is not None:
                            all_issues[num] = issue
                    except json.JSONDecodeError:
                        continue
    _logger.info(f"Loaded {len(all_issues)} related issues")

    # Load existing dataset for resume support
    out_file = out_dir / f"{org}__{repo}_lht_dataset.jsonl"
    existing_ids: set[str] = set()
    if out_file.exists():
        with open(out_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        record = json.loads(line)
                        existing_ids.add(record.get("instance_id", ""))
                    except json.JSONDecodeError:
                        pass
    if existing_ids:
        _logger.info(f"Resuming: {len(existing_ids)} records already processed")

    # Process each tag group
    records_written = 0
    with open(out_file, "a", encoding="utf-8") as f:
        for group in tqdm(tag_groups, desc="Building dataset"):
            base_sha = group.get("base_sha", "")
            head_sha = group.get("head_sha", "")
            pr_numbers = group.get("pr_numbers", [])
            base_tag = group.get("base_tag", "")
            head_tag = group.get("head_tag", "")

            # Build instance_id — sort PR numbers for deterministic ordering
            sorted_pr_numbers = sorted(pr_numbers)
            pr_numbers_str = "-".join(str(n) for n in sorted_pr_numbers)
            instance_id = f"{org.lower()}__{repo.lower()}-{pr_numbers_str}"

            if instance_id in existing_ids:
                continue

            if not base_sha or not head_sha:
                _logger.info(f"  Skipping group (missing SHA): {base_tag}..{head_tag}")
                continue

            # Collect the PRs in this group
            group_prs = [pr_lookup[n] for n in pr_numbers if n in pr_lookup]
            if not group_prs:
                _logger.info(f"  Skipping group (no PRs found): {base_tag}..{head_tag}")
                continue

            # Fetch unified diff with retry + clone fallback
            diff_text = ""
            for attempt in range(retry_attempts):
                try:
                    diff_text = fetch_unified_diff(
                        org,
                        repo,
                        base_sha,
                        head_sha,
                        rotator.get_token(),
                        clone_cache,
                    )
                    break
                except Exception as e:
                    error_msg = str(e)
                    is_permanent = any(
                        marker in error_msg
                        for marker in [
                            "404",
                            "No common ancestor",
                            "Not Found",
                            "not installed",
                            "timed out",
                            "not our ref",
                            "Invalid symmetric difference",
                        ]
                    )
                    if is_permanent:
                        _logger.info(
                            f"\n  Skipping group {base_tag}..{head_tag}: "
                            f"permanent error — {error_msg}"
                        )
                        break
                    if delay_on_error is None or attempt == retry_attempts - 1:
                        _logger.info(
                            f"\n  Failed to get diff for "
                            f"{base_tag}..{head_tag}: {error_msg}"
                        )
                        break
                    _logger.info(
                        f"  Attempt {attempt + 1} failed for "
                        f"{base_tag}..{head_tag}. "
                        f"Retrying in {delay_on_error}s..."
                    )
                    time.sleep(delay_on_error)

            if not diff_text or not diff_text.strip():
                continue

            # Split into fix_patch and test_patch
            fix_patch, test_patch = split_patches(diff_text)
            if not fix_patch.strip():
                continue

            # Aggregate issues
            resolved_issues = aggregate_issues(group_prs, all_issues)

            # Build the tag label
            tag_label = f"{base_tag}..{head_tag}" if base_tag else ""

            # Use the first PR for base ref
            primary_pr = group_prs[0]
            base_ref = primary_pr.get("base", {}).get("ref", "main")

            pr_url = primary_pr.get("html_url", "") or primary_pr.get("url", "")

            record = {
                "instance_id": instance_id,
                "org": org,
                "repo": repo,
                "number": pr_numbers_str,
                "state": primary_pr.get("state", "closed"),
                "title": primary_pr.get("title", ""),
                "body": primary_pr.get("body", "") or "",
                "base": {
                    "label": tag_label,
                    "ref": base_ref,
                    "sha": base_sha,
                },
                "resolved_issues": resolved_issues,
                "fix_patch": fix_patch,
                "test_patch": test_patch,
                "fixed_tests": {},
                "p2p_tests": {},
                "f2p_tests": {},
                "s2p_tests": {},
                "n2p_tests": {},
                "run_result": {"passed_count": 0, "failed_count": 0, "skipped_count": 0, "passed_tests": [], "failed_tests": [], "skipped_tests": []},
                "test_patch_result": {"passed_count": 0, "failed_count": 0, "skipped_count": 0, "passed_tests": [], "failed_tests": [], "skipped_tests": []},
                "fix_patch_result": {"passed_count": 0, "failed_count": 0, "skipped_count": 0, "passed_tests": [], "failed_tests": [], "skipped_tests": []},
                "prs_in_bundle": sorted_pr_numbers,
                "release_line": group.get("release_line", ""),
                "attribution_methods": group.get("attribution_methods", {}),
                "hints": "",
                "lang": lang,
                "pr_url": pr_url,
            }

            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            records_written += 1

    _logger.info(f"Wrote {records_written} records to {out_file}")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    tokens = get_tokens(args.tokens)
    main(
        tokens,
        Path.cwd() / args.out_dir,
        args.org,
        args.repo,
        args.delay_on_error,
        args.retry_attempts,
        args.cache_dir,
        args.lang,
    )
