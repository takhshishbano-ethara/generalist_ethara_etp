"""Build the final LHT dataset from tag groups.

Vendored from multi-swe-bench/collect/build_lht_dataset.py.

For each tag group (version range):
  1. Fetch the unified diff between base_sha and head_sha
     - Primary: GitHub compare API with diff accept header
     - Fallback: clone repo locally (bare blobless) and run git diff
  2. Split into fix_patch and test_patch using unidiff
  3. Aggregate all resolved issues from the PRs in the group
  4. Write the final record to JSONL

Outputs: {org}__{repo}_raw_dataset.jsonl
"""
import json
import logging
import re
import subprocess
import time
from pathlib import Path
import requests
from unidiff import PatchSet

_logger = logging.getLogger(__name__)

# Test file path keywords for patch splitting
_TEST_PATH_KEYWORDS = ["test", "tests", "e2e", "testing", "spec", "__tests__"]

# Issue reference patterns
_ISSUE_REF_PATTERN = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)
_ISSUE_URL_PATTERN = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")


# ---------------------------------------------------------------------------
# Repo clone cache for local diff fallback
# ---------------------------------------------------------------------------


class _RepoCloneCache:
    """Caches bare blobless git clones for local diff generation."""

    def __init__(self, cache_dir=".repo_cache"):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _repo_path(self, org, repo):
        return self._cache_dir / f"{org}__{repo}.git"

    def ensure_cloned(self, org, repo):
        """Clone the repo if not already cached. Returns path to bare clone."""
        repo_path = self._repo_path(org, repo)
        if repo_path.exists():
            _logger.info("Fetching latest for cached %s/%s", org, repo)
            try:
                subprocess.run(
                    ["git", "-C", str(repo_path), "fetch", "--quiet"],
                    capture_output=True, text=True, timeout=300,
                )
            except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError) as e:
                _logger.warning("Fetch failed for %s/%s: %s", org, repo, e)
            return repo_path

        _logger.info("Cloning %s/%s (bare, blobless)...", org, repo)
        url = f"https://github.com/{org}/{repo}.git"
        try:
            result = subprocess.run(
                ["git", "clone", "--bare", "--filter=blob:none", url, str(repo_path)],
                capture_output=True, text=True, timeout=600,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "git is not installed or not on PATH.",
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Clone timed out (600s) for {org}/{repo}.",
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to clone {org}/{repo}: {result.stderr.strip()}",
            )
        _logger.info("Clone complete for %s/%s", org, repo)
        return repo_path

    def get_diff(self, org, repo, base_sha, head_sha):
        """Generate diff locally using git diff on the cached bare clone."""
        if not base_sha or not head_sha:
            raise ValueError("base_sha and head_sha must be non-empty")
        repo_path = self.ensure_cloned(org, repo)
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "diff", f"{base_sha}...{head_sha}"],
                capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            raise RuntimeError("git is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"git diff timed out for {org}/{repo} "
                f"({base_sha[:8]}...{head_sha[:8]}).",
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"git diff failed for {org}/{repo}: {result.stderr.strip()}",
            )
        return result.stdout


# ---------------------------------------------------------------------------
# Diff fetching with fallback
# ---------------------------------------------------------------------------


def _fetch_unified_diff(org, repo, base_sha, head_sha, token, clone_cache):
    """Fetch the unified diff between two commits.

    Tries GitHub compare API first, falls back to local clone.
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
        elif response.status_code in (403, 429):
            retry_after = response.headers.get("Retry-After", "60")
            raise Exception(
                f"Rate limited ({response.status_code}), retry after {retry_after}s",
            )
        else:
            _logger.info(
                "GitHub API returned %d for %s...%s, using clone fallback",
                response.status_code, base_sha[:8], head_sha[:8],
            )
    except requests.RequestException as e:
        _logger.info("GitHub API request failed: %s, using clone fallback", e)

    return clone_cache.get_diff(org, repo, base_sha, head_sha)


# ---------------------------------------------------------------------------
# Patch splitting
# ---------------------------------------------------------------------------


def _split_patches(diff_text):
    """Split a unified diff into fix_patch and test_patch."""
    test_patch = ""
    fix_patch = ""

    try:
        for hunk in PatchSet(diff_text):
            path_lower = hunk.path.lower()
            if any(kw in path_lower for kw in _TEST_PATH_KEYWORDS):
                test_patch += str(hunk)
            else:
                fix_patch += str(hunk)
    except Exception as e:
        _logger.warning("unidiff parse failed, using raw diff as fix_patch: %s", e)
        return diff_text, ""

    return fix_patch, test_patch


# ---------------------------------------------------------------------------
# Issue aggregation
# ---------------------------------------------------------------------------


def _extract_issue_numbers_from_body(body):
    """Extract issue numbers referenced in a PR body."""
    if not body:
        return []
    numbers = set()
    for m in _ISSUE_REF_PATTERN.finditer(body):
        num = int(m.group(1))
        if num > 0:
            numbers.add(num)
    for m in _ISSUE_URL_PATTERN.finditer(body):
        num = int(m.group(1))
        if num > 0:
            numbers.add(num)
    return sorted(numbers)


def _aggregate_issues(group_prs, all_issues):
    """Aggregate resolved issues for all PRs in a bundle.

    - If a PR links to issues, include those issues.
    - If the issue body is empty, substitute with the PR description.
    - If a PR has no linked issues, add the PR itself as a pseudo-issue.
    """
    result = []
    seen_numbers = set()

    for pr in group_prs:
        pr_num = pr.get("number", 0)
        pr_body = pr.get("body") or ""
        pr_title = pr.get("title") or ""

        issue_num_set = set(_extract_issue_numbers_from_body(pr_body))

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
                if not issue_body.strip():
                    issue_body = pr_body
                result.append({
                    "number": issue_num,
                    "title": issue.get("title", ""),
                    "body": issue_body,
                })
                has_real_issues = True

        if not has_real_issues and pr_num not in seen_numbers:
            seen_numbers.add(pr_num)
            result.append({
                "number": pr_num,
                "title": pr_title,
                "body": pr_body,
            })

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(
    token, out_dir, org, repo, *,
    lang="python",
    delay_on_error=300,
    retry_attempts=3,
    cache_dir=".repo_cache",
):
    """Build LHT dataset from tag groups with unified diffs.

    Args:
        token: GitHub PAT string (or list of tokens).
        out_dir: Path to output directory.
        org: GitHub organization name.
        repo: GitHub repository name.
        lang: Programming language of the repository.
        delay_on_error: Seconds to wait before retrying on error.
        retry_attempts: Number of retry attempts per group.
        cache_dir: Directory for cached bare git clones.

    Returns:
        Path: Path to the output JSONL file.
    """
    out_dir = Path(out_dir)
    tokens = token if isinstance(token, list) else [token]
    if not tokens:
        raise ValueError("No tokens provided")

    _logger.info("Building LHT dataset for %s/%s", org, repo)

    clone_cache = _RepoCloneCache(cache_dir)

    # Load tag groups
    groups_file = out_dir / f"{org}__{repo}_tag_groups.jsonl"
    if not groups_file.exists():
        raise FileNotFoundError(f"Tag groups file not found: {groups_file}")
    with open(groups_file, encoding="utf-8") as f:
        tag_groups = [json.loads(line) for line in f if line.strip()]
    _logger.info("Loaded %d tag groups", len(tag_groups))

    # Load PRs for issue aggregation
    prs_file = out_dir / f"{org}__{repo}_lht_filtered_prs.jsonl"
    if not prs_file.exists():
        prs_file = out_dir / f"{org}__{repo}_filtered_prs.jsonl"
    pr_lookup = {}
    if prs_file.exists():
        with open(prs_file, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        pr = json.loads(line)
                        num = pr.get("number")
                        if num is not None:
                            pr_lookup[num] = pr
                    except json.JSONDecodeError:
                        continue
    _logger.info("Loaded %d PRs", len(pr_lookup))

    # Load related issues (if available)
    issues_file = out_dir / f"{org}__{repo}_related_issues.jsonl"
    all_issues = {}
    if issues_file.exists():
        with open(issues_file, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        issue = json.loads(line)
                        num = issue.get("number")
                        if num is not None:
                            all_issues[num] = issue
                    except json.JSONDecodeError:
                        continue
    _logger.info("Loaded %d related issues", len(all_issues))

    # Resume support: check existing records
    out_file = out_dir / f"{org}__{repo}_raw_dataset.jsonl"
    existing_ids = set()
    if out_file.exists():
        with open(out_file, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        record = json.loads(line)
                        existing_ids.add(record.get("instance_id", ""))
                    except json.JSONDecodeError:
                        pass
    if existing_ids:
        _logger.info("Resuming: %d records already processed", len(existing_ids))

    # Process each tag group
    import random

    records_written = 0
    with open(out_file, "a", encoding="utf-8") as f:
        for group in tag_groups:
            base_sha = group.get("base_sha", "")
            head_sha = group.get("head_sha", "")
            pr_numbers = group.get("pr_numbers", [])
            base_tag = group.get("base_tag", "")
            head_tag = group.get("head_tag", "")

            sorted_pr_numbers = sorted(pr_numbers)
            pr_numbers_str = "-".join(str(n) for n in sorted_pr_numbers)
            instance_id = f"{org.lower()}__{repo.lower()}-{pr_numbers_str}"

            if instance_id in existing_ids:
                continue

            if not base_sha or not head_sha:
                _logger.debug("Skipping group (missing SHA): %s..%s", base_tag, head_tag)
                continue

            group_prs = [pr_lookup[n] for n in pr_numbers if n in pr_lookup]
            if not group_prs:
                _logger.debug("Skipping group (no PRs found): %s..%s", base_tag, head_tag)
                continue

            # Fetch unified diff with retry + clone fallback
            diff_text = ""
            for attempt in range(retry_attempts):
                try:
                    diff_text = _fetch_unified_diff(
                        org, repo, base_sha, head_sha,
                        random.choice(tokens), clone_cache,
                    )
                    break
                except Exception as e:
                    error_msg = str(e)
                    is_permanent = any(
                        marker in error_msg
                        for marker in [
                            "404", "No common ancestor", "Not Found",
                            "not installed", "timed out", "not our ref",
                            "Invalid symmetric difference",
                        ]
                    )
                    if is_permanent:
                        _logger.warning(
                            "Skipping group %s..%s: permanent error — %s",
                            base_tag, head_tag, error_msg,
                        )
                        break
                    if attempt == retry_attempts - 1:
                        _logger.error(
                            "Failed to get diff for %s..%s: %s",
                            base_tag, head_tag, error_msg,
                        )
                        break
                    _logger.info(
                        "Attempt %d failed for %s..%s. Retrying in %ds...",
                        attempt + 1, base_tag, head_tag, delay_on_error,
                    )
                    time.sleep(delay_on_error)

            if not diff_text or not diff_text.strip():
                continue

            fix_patch, test_patch = _split_patches(diff_text)
            if not fix_patch.strip():
                continue

            resolved_issues = _aggregate_issues(group_prs, all_issues)

            tag_label = f"{base_tag}..{head_tag}" if base_tag else ""
            primary_pr = group_prs[0]
            base_ref = primary_pr.get("base", {}).get("ref", "main")

            _empty_run = {
                "passed_count": 0, "failed_count": 0, "skipped_count": 0,
                "passed_tests": [], "failed_tests": [], "skipped_tests": [],
            }

            record = {
                "instance_id": instance_id,
                "org": org,
                "repo": repo,
                "number": sorted_pr_numbers[0],
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
                "run_result": _empty_run,
                "test_patch_result": dict(_empty_run),
                "fix_patch_result": dict(_empty_run),
                "prs_in_bundle": sorted_pr_numbers,
                "release_line": group.get("release_line", ""),
                "attribution_methods": group.get("attribution_methods", {}),
                "hints": "",
                "lang": lang,
                "pr_url": f"https://github.com/{org}/{repo}/pull/{sorted_pr_numbers[0]}",
            }

            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            records_written += 1

    _logger.info("Wrote %d LHT records to %s", records_written, out_file)
    return out_file
