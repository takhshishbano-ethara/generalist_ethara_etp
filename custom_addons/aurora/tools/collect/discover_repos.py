"""
GitHub Repository Discovery for Aurora Pipeline.

Discovers candidate repositories via the GitHub Search API with star-range
splitting to work around the 1000-result limit.  Outputs JSONL records
suitable for creating ``aurora.discovery`` model entries.

Adapted from commit0_pipeline/tools/discover.py for multi-language support
and TokenRotator integration.
"""

import json
import logging
import time
from pathlib import Path

import requests

from .util import TokenRotator

_logger = logging.getLogger(__name__)

# Map our internal lowercase language codes to the GitHub Linguist names
# accepted by the Search API's `language:X` qualifier. GitHub stores the
# canonical names with their original punctuation/capitalisation, so
# `language:cpp` literally returns zero results.
_GITHUB_LINGUIST_NAME = {
    "cpp": "C++",
    "csharp": "C#",
    "golang": "Go",
    "python": "Python",
    "java": "Java",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "rust": "Rust",
    "ruby": "Ruby",
    "php": "PHP",
    "kotlin": "Kotlin",
    "scala": "Scala",
    "swift": "Swift",
    "html": "HTML",
    "c": "C",
}



# ── helpers ────────────────────────────────────────────────────────────────

def _compute_star_ranges(
    min_stars: int, max_results: int,
) -> list[tuple[int, int | None]]:
    """Split star range into chunks to work around GitHub's 1000-result limit."""
    if max_results <= 900:
        return [(min_stars, None)]
    boundaries = [b for b in [min_stars, 5000, 10000, 20000, 50000, 100000] if b >= min_stars]
    if not boundaries or boundaries[0] != min_stars:
        boundaries.insert(0, min_stars)
    ranges: list[tuple[int, int | None]] = []
    for i in range(len(boundaries) - 1):
        ranges.append((boundaries[i], boundaries[i + 1] - 1))
    ranges.append((boundaries[-1], None))
    return list(reversed(ranges))  # highest-star ranges first


def _search_repos(
    rotator: TokenRotator,
    language: str,
    min_stars: int,
    max_results: int,
    excluded: set[str],
    progress_callback=None,
) -> list[dict]:
    """Search GitHub for repos matching *language* + *star* criteria."""
    repos: list[dict] = []
    star_ranges = _compute_star_ranges(min_stars, max_results)

    for star_min, star_max in star_ranges:
        if len(repos) >= max_results:
            break
        if star_max:
            gh_lang = _GITHUB_LINGUIST_NAME.get(language.lower(), language)
            query = f'language:"{gh_lang}" stars:{star_min}..{star_max} archived:false fork:false'
        else:
            query = f'language:"{gh_lang}" stars:>={star_min} archived:false fork:false'

        for page in range(1, 11):
            if len(repos) >= max_results:
                break
            _logger.info("Searching: %s (page %d, found %d)", query, page, len(repos))
            if progress_callback:
                progress_callback(f"Searching: {query} (page {page}, {len(repos)} found)")

            client = rotator.get_client()
            try:
                result = client.search_repositories(query=query, sort="stars", order="desc")
                items = list(result.get_page(page - 1))
            except Exception as exc:
                _logger.warning("Search failed for %s page %d: %s", query, page, exc)
                time.sleep(2)
                break

            if not items:
                break

            for item in items:
                full_name = item.full_name
                if full_name in excluded or item.fork or item.archived:
                    continue
                repos.append({
                    "github_org": item.owner.login,
                    "github_repo": item.name,
                    "full_name": full_name,
                    "stars": item.stargazers_count,
                    "forks": item.forks_count,
                    "size_kb": item.size,
                    "description": item.description or "",
                    "topics": json.dumps(item.topics or []),
                    "license_spdx": (item.license.spdx_id if item.license else "") or "",
                    "default_branch": item.default_branch or "main",
                    "open_issues": item.open_issues_count,
                    "last_pushed": item.pushed_at.isoformat() if item.pushed_at else "",
                    "primary_language": item.language or "",
                })

            time.sleep(1)

    # deduplicate
    seen: set[str] = set()
    unique: list[dict] = []
    for r in repos:
        if r["full_name"] not in seen:
            seen.add(r["full_name"])
            unique.append(r)
    return sorted(unique, key=lambda x: x["stars"], reverse=True)[:max_results]


def _enrich_repo(rotator: TokenRotator, repo: dict) -> dict:
    """Enrich a single repo with language %, test and CI detection."""
    full_name = repo["full_name"]
    client = rotator.get_client()

    # ── language breakdown ──
    try:
        gh_repo = client.get_repo(full_name)
        languages_url = gh_repo.languages_url
        token = rotator.get_token()
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        resp = requests.get(languages_url, headers=headers, timeout=15)
        resp.raise_for_status()
        lang_dict = resp.json()
        total_bytes = sum(lang_dict.values())
        primary = repo.get("primary_language", "")
        lang_bytes = 0
        if total_bytes and primary:
            for name, nbytes in lang_dict.items():
                if name.lower() == primary.lower():
                    lang_bytes = nbytes
                    break
        repo["language_pct"] = round(lang_bytes / total_bytes * 100, 1) if total_bytes else 0.0
    except Exception as exc:
        _logger.warning("Language lookup failed for %s: %s", full_name, exc)
        repo["language_pct"] = 0.0

    # ── test detection ──
    test_indicators = ["pytest", "unittest", "jest", "mocha", "cargo test", "go test",
                       "junit", "rspec", "phpunit"]
    config_files = ["pyproject.toml", "setup.cfg", "package.json", "Cargo.toml",
                    "go.mod", "build.gradle", "pom.xml", "Gemfile", "composer.json"]
    has_tests = False
    try:
        gh_repo = client.get_repo(full_name)
        branch = repo.get("default_branch", "main")
        for cf in config_files:
            try:
                content_file = gh_repo.get_contents(cf, ref=branch)
                if content_file and hasattr(content_file, "decoded_content"):
                    text = content_file.decoded_content.decode(errors="replace").lower()
                    if any(ind in text for ind in test_indicators):
                        has_tests = True
                        break
            except Exception:
                continue
    except Exception:
        pass
    repo["has_tests"] = has_tests

    # ── CI detection ──
    has_ci = False
    ci_paths = [".github/workflows", ".circleci", ".travis.yml", "Jenkinsfile", ".gitlab-ci.yml"]
    try:
        gh_repo = client.get_repo(full_name)
        branch = repo.get("default_branch", "main")
        for ci_path in ci_paths:
            try:
                gh_repo.get_contents(ci_path, ref=branch)
                has_ci = True
                break
            except Exception:
                continue
    except Exception:
        pass
    repo["has_ci"] = has_ci

    time.sleep(0.5)
    return repo


# ── public entry point (matches tools/collect/* pattern) ──────────────────

def main(
    tokens: list[str],
    out_dir: Path,
    language: str = "python",
    min_stars: int = 1000,
    max_results: int = 50,
    min_language_pct: float = 60.0,
    excluded_repos: set[str] | None = None,
    enrich: bool = True,
    progress_callback=None,
) -> list[dict]:
    """Discover GitHub repositories and write results as JSONL.

    Parameters
    ----------
    tokens : list[str]
        GitHub API tokens for :class:`TokenRotator`.
    out_dir : Path
        Directory to write the output JSONL file.
    language : str
        Programming language to search for (must match GitHub Linguist names).
    min_stars : int
        Minimum star-count filter.
    max_results : int
        Maximum number of results to return.
    min_language_pct : float
        Minimum percentage of target language in repo.
    excluded_repos : set[str] | None
        Set of ``"org/repo"`` strings to skip.
    enrich : bool
        Whether to run enrichment (language %, tests, CI).
    progress_callback : callable | None
        Optional callback ``(msg: str) -> None`` for progress reporting.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_file = out_dir / f"{language}_discovery_candidates.jsonl"

    rotator = TokenRotator(tokens)
    excluded = excluded_repos or set()

    _logger.info("Starting discovery: language=%s min_stars=%d max_results=%d",
                 language, min_stars, max_results)

    # Phase 1 — Search
    repos = _search_repos(rotator, language, min_stars, max_results, excluded, progress_callback)
    _logger.info("Search found %d candidate repos", len(repos))

    if not repos:
        if not dry_run:
            output_file.write_text("")
        return repos

    if enrich:
        enriched: list[dict] = []
        for i, repo in enumerate(repos):
            if progress_callback:
                progress_callback(f"Enriching {i + 1}/{len(repos)}: {repo['full_name']}")
            _logger.info("[%d/%d] Enriching %s", i + 1, len(repos), repo["full_name"])
            repo = _enrich_repo(rotator, repo)
            pct = repo.get("language_pct", 0)
            if pct >= min_language_pct:
                enriched.append(repo)
            else:
                _logger.info("  Skipped %s: %.1f%% < %.1f%% threshold",
                             repo["full_name"], pct, min_language_pct)
        repos = enriched

    with output_file.open("w", encoding="utf-8") as fh:
        for repo in repos:
            fh.write(json.dumps(repo, default=str) + "\n")

    _logger.info("Discovery complete: %d repos written to %s", len(repos), output_file)
    return repos
