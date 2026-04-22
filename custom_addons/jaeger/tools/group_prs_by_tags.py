"""Group merged PRs into version-tag ranges for Long Horizon Tasks.

Vendored from multi-swe-bench/collect/group_prs_by_tags.py.

Multi-layered strategy:

    Layer 1 — Smart tag parsing (consumed from get_version_tags output):
        Tags arrive pre-classified as semver/calver/unknown with release_line,
        pre_release flags, and semver sort_key.

    Layer 2 — Git ancestry verification:
        Before pairing two tags, verify base is an actual git ancestor of head
        via ``git merge-base --is-ancestor``.

    Layer 3 — Branch-aware release-line grouping:
        Filter pre-release tags, group by release line (major.minor),
        sort by semver within each group, pair consecutive tags.

    Layer 4 — Tiered PR attribution:
        1st  git log --merges --first-parent base..head
        2nd  GitHub compare API SHA matching
        3rd  git cherry detection (cherry-picked PRs)
        4th  Date-range fallback (last resort)

    Bundles with fewer than _MIN_PRS_PER_BUNDLE PRs are excluded.

Outputs: {org}__{repo}_tag_groups.jsonl
"""
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from github import Auth, Github, GithubException

_logger = logging.getLogger(__name__)

# GitHub compare API hard limit
_COMPARE_COMMITS_CAP = 250

# Default timeout for git commands (seconds)
_GIT_TIMEOUT = 120

# Minimum PRs required per version-tag pair to form a valid LHT bundle
_MIN_PRS_PER_BUNDLE = 2

# PR number extraction patterns for merge commit messages
_PR_NUMBER_PATTERNS = [
    re.compile(r"Merge pull request #(\d+)"),
    re.compile(r"\(#(\d+)\)\s*$"),
    re.compile(r"PR #(\d+)"),
    re.compile(r"pull request #(\d+)", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Git operations (Layer 2)
# ---------------------------------------------------------------------------


def _run_git(args, repo_path, *, timeout=_GIT_TIMEOUT):
    """Run a git command in the given repo path."""
    cmd = ["git", "-C", str(repo_path)] + args
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _is_ancestor(repo_path, ancestor_sha, descendant_sha):
    """Check if ancestor_sha is a git ancestor of descendant_sha."""
    try:
        result = _run_git(
            ["merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
            repo_path,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _get_merge_commits(repo_path, base_sha, head_sha):
    """Get merge commits between two refs using git log --merges --first-parent."""
    try:
        result = _run_git(
            [
                "log", "--merges", "--first-parent",
                "--format=%H%n%aI%n%s%n---END---",
                f"{base_sha}..{head_sha}",
            ],
            repo_path,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, Exception):
        return []

    commits = []
    raw = result.stdout.strip()
    if not raw:
        return commits

    for entry in raw.split("---END---"):
        entry = entry.strip()
        if not entry:
            continue
        lines = entry.split("\n", 2)
        if len(lines) < 3:
            continue
        sha = lines[0].strip()
        message = lines[2].strip()
        pr_numbers = _extract_pr_numbers(message)
        commits.append({"sha": sha, "message": message, "pr_numbers": pr_numbers})

    return commits


def _get_all_commit_shas(repo_path, base_sha, head_sha):
    """Get all commit SHAs between two refs (first-parent walk)."""
    try:
        result = _run_git(
            ["log", "--first-parent", "--format=%H", f"{base_sha}..{head_sha}"],
            repo_path,
        )
        if result.returncode != 0:
            return set()
    except (subprocess.TimeoutExpired, Exception):
        return set()

    return {sha.strip() for sha in result.stdout.strip().split("\n") if sha.strip()}


def _detect_cherry_picks(repo_path, upstream_ref, head_ref):
    """Detect cherry-picked commit SHAs via git cherry."""
    try:
        result = _run_git(
            ["cherry", "-v", upstream_ref, head_ref], repo_path, timeout=60,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, Exception):
        return []

    cherry_shas = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("- "):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                cherry_shas.append(parts[1])
    return cherry_shas


def _extract_pr_numbers(message):
    """Extract PR numbers from a merge commit message."""
    numbers = []
    seen = set()
    for pattern in _PR_NUMBER_PATTERNS:
        for match in pattern.finditer(message):
            num = int(match.group(1))
            if num not in seen:
                numbers.append(num)
                seen.add(num)
    return numbers


def _ensure_repo_cloned(org, repo, cache_dir):
    """Ensure the repo is cloned as a bare blobless clone. Returns path or None."""
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    repo_path = cache_path / f"{org}__{repo}.git"

    if repo_path.exists():
        _logger.info("Fetching latest for cached %s/%s", org, repo)
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "fetch", "--tags", "--force", "--quiet"],
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
        _logger.error("git not installed. Ancestry checks disabled.")
        return None
    except subprocess.TimeoutExpired:
        _logger.error("Clone timed out for %s/%s", org, repo)
        return None

    if result.returncode != 0:
        _logger.error("Clone failed for %s/%s: %s", org, repo, result.stderr.strip())
        return None

    _logger.info("Clone complete for %s/%s", org, repo)
    return repo_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(date_str):
    """Parse an ISO-8601 date string into a timezone-aware datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Layer 3: Tag grouping
# ---------------------------------------------------------------------------


def _filter_pre_releases(tags):
    """Filter out pre-release tags. Keep all if ONLY pre-releases exist."""
    stable = [t for t in tags if not t.get("is_pre_release", False)]
    if not stable:
        _logger.info("All tags are pre-releases — keeping them all")
        return tags
    filtered_count = len(tags) - len(stable)
    if filtered_count > 0:
        _logger.info("Filtered %d pre-release tags, %d remaining", filtered_count, len(stable))
    return stable


def _group_tags_by_release_line(tags):
    """Group tags by release_line, sorted by sort_key within each group."""
    groups = {}
    for t in tags:
        line = t.get("release_line", "unknown")
        groups.setdefault(line, []).append(t)
    for line in groups:
        groups[line].sort(key=lambda t: t.get("sort_key", []))
    return groups


def _find_cross_line_pairs(all_sorted, existing_pairs, repo_path):
    """Find tag pairs that bridge release lines (e.g., v1.9.5 → v2.0.0)."""
    cross = []
    for i in range(len(all_sorted) - 1):
        base = all_sorted[i]
        head = all_sorted[i + 1]
        if base.get("release_line") == head.get("release_line"):
            continue
        key = (base["sha"], head["sha"])
        if key in existing_pairs:
            continue
        if not base["sha"] or not head["sha"] or base["sha"] == head["sha"]:
            continue
        if repo_path and not _is_ancestor(repo_path, base["sha"], head["sha"]):
            continue
        line = f"{base.get('release_line', '?')}->{head.get('release_line', '?')}"
        cross.append((base, head, line))
    return cross


# ---------------------------------------------------------------------------
# Layer 4: Tiered PR attribution
# ---------------------------------------------------------------------------


def _collect_prs_for_pair(
    base_tag, head_tag, pr_by_sha, pr_by_number, all_prs,
    assigned_pr_numbers, repo_path, tokens, org, repo,
):
    """Collect PRs for a tag pair using all 4 tiers."""
    base_sha = base_tag["sha"]
    head_sha = head_tag["sha"]
    found = []
    methods = {}
    seen = set()

    def _add(pr_num, method):
        if pr_num and pr_num not in assigned_pr_numbers and pr_num not in seen:
            found.append(pr_num)
            seen.add(pr_num)
            methods[method] = methods.get(method, 0) + 1

    # Tier 1: git log --merges
    if repo_path:
        merge_commits = _get_merge_commits(repo_path, base_sha, head_sha)
        for mc in merge_commits:
            for pr_num in mc["pr_numbers"]:
                _add(pr_num, "git_log_merge")

        all_shas = _get_all_commit_shas(repo_path, base_sha, head_sha)
        for sha in all_shas:
            if sha in pr_by_sha:
                for pr_item in pr_by_sha[sha]:
                    _add(pr_item.get("number", 0), "git_log_sha")

    # Tier 2: GitHub compare API SHA matching
    use_date_fallback = False
    comparison_shas = set()

    import random
    auth = Auth.Token(random.choice(tokens) if tokens else "")
    g = Github(auth=auth, per_page=100)
    r = g.get_repo(f"{org}/{repo}")

    try:
        comparison = r.compare(base_sha, head_sha)
        if comparison.total_commits > _COMPARE_COMMITS_CAP:
            _logger.info(
                "%s..%s: %d commits (>%d), compare API capped",
                base_tag["name"], head_tag["name"],
                comparison.total_commits, _COMPARE_COMMITS_CAP,
            )
            use_date_fallback = True
        else:
            comparison_shas = {c.sha for c in comparison.commits}
    except GithubException:
        use_date_fallback = True

    if comparison_shas:
        for sha in comparison_shas:
            if sha in pr_by_sha:
                for pr_item in pr_by_sha[sha]:
                    _add(pr_item.get("number", 0), "compare_api")

    # Tier 3: Cherry-pick detection
    if repo_path:
        cherry_shas = _detect_cherry_picks(repo_path, base_sha, head_sha)
        for sha in cherry_shas:
            if sha in pr_by_sha:
                for pr_item in pr_by_sha[sha]:
                    _add(pr_item.get("number", 0), "cherry_pick")

    # Tier 4: Date-range fallback
    if not found and use_date_fallback:
        base_date = _parse_date(base_tag.get("date", ""))
        head_date = _parse_date(head_tag.get("date", ""))
        if base_date and head_date:
            for pr in all_prs:
                pr_num = pr.get("number", 0)
                if pr_num in assigned_pr_numbers or pr_num in seen:
                    continue
                merged_at = _parse_date(pr.get("merged_at", ""))
                if merged_at and base_date < merged_at <= head_date:
                    _add(pr_num, "date_range")

    return found, methods


def _maybe_emit_group(
    base_tag, head_tag, release_line, pr_by_sha, pr_by_number,
    all_prs, assigned_pr_numbers, repo_path, tokens, org, repo,
    groups, existing_pairs,
):
    """Collect PRs for a tag pair and append to groups if threshold met."""
    pr_numbers, methods = _collect_prs_for_pair(
        base_tag, head_tag, pr_by_sha, pr_by_number,
        all_prs, assigned_pr_numbers, repo_path, tokens, org, repo,
    )
    if len(pr_numbers) >= _MIN_PRS_PER_BUNDLE:
        assigned_pr_numbers.update(pr_numbers)
        existing_pairs.add((base_tag["sha"], head_tag["sha"]))
        groups.append({
            "base_tag": base_tag["name"],
            "head_tag": head_tag["name"],
            "base_sha": base_tag["sha"],
            "head_sha": head_tag["sha"],
            "pr_numbers": sorted(pr_numbers),
            "release_line": release_line,
            "attribution_methods": methods,
        })


# ---------------------------------------------------------------------------
# Fallback: time-window grouping
# ---------------------------------------------------------------------------


def _group_by_time_window(prs, window_days=30):
    """Fallback: group PRs by time windows when insufficient tags exist."""
    if not prs:
        return []

    sorted_prs = sorted(
        [p for p in prs if p.get("merged_at")],
        key=lambda p: (
            _parse_date(p.get("merged_at", ""))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
    )
    if not sorted_prs:
        return []

    by_base = {}
    for pr in sorted_prs:
        base_ref = pr.get("base", {}).get("ref", "main")
        by_base.setdefault(base_ref, []).append(pr)

    groups = []
    for _base_ref, branch_prs in by_base.items():
        current_group = [branch_prs[0]]
        group_start = _parse_date(branch_prs[0].get("merged_at", ""))

        for i in range(1, len(branch_prs)):
            curr_date = _parse_date(branch_prs[i].get("merged_at", ""))
            if (
                group_start
                and curr_date
                and (curr_date - group_start).days <= window_days
            ):
                current_group.append(branch_prs[i])
            else:
                if len(current_group) >= _MIN_PRS_PER_BUNDLE:
                    _emit_time_window_group(groups, current_group)
                current_group = [branch_prs[i]]
                group_start = curr_date

        if len(current_group) >= _MIN_PRS_PER_BUNDLE:
            _emit_time_window_group(groups, current_group)

    return groups


def _emit_time_window_group(groups, prs):
    first_pr = prs[0]
    last_pr = prs[-1]
    first_merged = first_pr.get("merged_at", "")
    last_merged = last_pr.get("merged_at", "")
    groups.append({
        "base_tag": f"window:{first_merged[:10]}" if first_merged else "",
        "head_tag": f"window:{last_merged[:10]}" if last_merged else "",
        "base_sha": first_pr.get("base", {}).get("sha", ""),
        "head_sha": last_pr.get("merge_commit_sha", ""),
        "pr_numbers": sorted(p.get("number", 0) for p in prs),
        "release_line": "time_window",
        "attribution_methods": {"time_window": len(prs)},
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(out_dir, org, repo, window_days=30, cache_dir=".repo_cache", tokens=None):
    """Group PRs into version-tag ranges for LHT.

    Args:
        out_dir: Path to output directory.
        org: GitHub organization name.
        repo: GitHub repository name.
        window_days: Fallback time-window size in days.
        cache_dir: Directory for cached bare git clones.
        tokens: Optional list of GitHub PAT strings (needed for compare API).

    Returns:
        Path: Path to the tag groups JSONL file.
    """
    out_dir = Path(out_dir)
    tokens = tokens or []

    _logger.info("Grouping PRs by version tags for %s/%s", org, repo)

    # Load tags (pre-classified with scheme, release_line, sort_key)
    tags_file = out_dir / f"{org}__{repo}_tags.jsonl"
    tags = []
    if tags_file.exists():
        with open(tags_file, encoding="utf-8") as f:
            tags = [json.loads(line) for line in f if line.strip()]
    _logger.info("Loaded %d tags", len(tags))

    # Load all merged PRs
    prs_file = out_dir / f"{org}__{repo}_lht_filtered_prs.jsonl"
    if not prs_file.exists():
        prs_file = out_dir / f"{org}__{repo}_filtered_prs.jsonl"
    if not prs_file.exists():
        raise FileNotFoundError(f"No PR file found at {prs_file}")

    prs = []
    with open(prs_file, encoding="utf-8") as f:
        prs = [json.loads(line) for line in f if line.strip()]
    _logger.info("Loaded %d PRs from %s", len(prs), prs_file.name)

    if not prs:
        _logger.warning("No PRs to group")
        out_file = out_dir / f"{org}__{repo}_tag_groups.jsonl"
        out_file.write_text("")
        return out_file

    # Build lookups
    pr_by_sha = {}
    pr_by_number = {}
    for pr in prs:
        sha = pr.get("merge_commit_sha", "")
        if sha:
            pr_by_sha.setdefault(sha, []).append(pr)
        num = pr.get("number", 0)
        if num:
            pr_by_number[num] = pr

    # Ensure repo is cloned for git operations
    repo_path = _ensure_repo_cloned(org, repo, cache_dir)
    if repo_path:
        _logger.info("Git repo ready at %s", repo_path)
    else:
        _logger.warning(
            "No git clone available. Ancestry checks disabled, "
            "falling back to compare API only.",
        )

    groups = []

    if len(tags) >= 2:
        # Layer 3: Filter pre-releases, group by release line
        filtered_tags = _filter_pre_releases(tags)

        if len(filtered_tags) >= 2:
            release_groups = _group_tags_by_release_line(filtered_tags)
            _logger.info("Release lines: %s", list(release_groups.keys()))

            assigned_pr_numbers = set()
            existing_pairs = set()

            # Pair consecutive tags within each release line
            for line, line_tags in release_groups.items():
                if len(line_tags) < 2:
                    continue
                for i in range(len(line_tags) - 1):
                    base_tag = line_tags[i]
                    head_tag = line_tags[i + 1]
                    base_sha = base_tag.get("sha", "")
                    head_sha = head_tag.get("sha", "")

                    if not base_sha or not head_sha or base_sha == head_sha:
                        continue

                    # Layer 2: Ancestry verification
                    if repo_path and not _is_ancestor(repo_path, base_sha, head_sha):
                        _logger.debug(
                            "Skipping %s..%s: NOT ancestor",
                            base_tag["name"], head_tag["name"],
                        )
                        continue

                    # Layer 4: Tiered PR attribution
                    _maybe_emit_group(
                        base_tag, head_tag, line,
                        pr_by_sha, pr_by_number, prs,
                        assigned_pr_numbers, repo_path, tokens,
                        org, repo, groups, existing_pairs,
                    )

            # Cross-release-line pairs (e.g., v1.9.5 → v2.0.0)
            all_sorted = sorted(filtered_tags, key=lambda t: t.get("sort_key", ()))
            cross_pairs = _find_cross_line_pairs(all_sorted, existing_pairs, repo_path)
            for base_tag, head_tag, cross_line in cross_pairs:
                _maybe_emit_group(
                    base_tag, head_tag, cross_line,
                    pr_by_sha, pr_by_number, prs,
                    assigned_pr_numbers, repo_path, tokens,
                    org, repo, groups, existing_pairs,
                )

            _logger.info("Version-range grouping produced %d groups", len(groups))

    # Fallback to time-window grouping
    if not groups:
        _logger.info("No version-range groups; falling back to time-window grouping")
        groups = _group_by_time_window(prs, window_days)
        _logger.info("Time-window grouping produced %d groups", len(groups))

    # Write output
    out_file = out_dir / f"{org}__{repo}_tag_groups.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for group in groups:
            f.write(json.dumps(group, ensure_ascii=False) + "\n")

    total_prs = sum(len(g["pr_numbers"]) for g in groups)
    _logger.info("Wrote %d groups (%d total PRs) to %s", len(groups), total_prs, out_file)
    return out_file
