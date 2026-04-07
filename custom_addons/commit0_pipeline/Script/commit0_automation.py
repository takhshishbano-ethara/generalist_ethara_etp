#!/usr/bin/env python3
"""
commit0_automation.py — Production-grade 2-stage GitHub repository discovery,
filtering, and processing pipeline for the Commit0 benchmark.

Pipeline:
  1. Discover TARGET_REPO_COUNT Python repos via star-range partitioning
  2. Filter 1: Initial quality checks (language, docs, structure, stars)
  3. Filter 2: Commit0 Benchmark validation (pytest, build, test reliability)
  4. Clone final repos, generate PDF docs + YAML metadata

Fully automated. No user interaction required.
"""

from __future__ import annotations

import ast
import bz2
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Phase 0 — Bootstrap: auto-create venv and install dependencies
# ---------------------------------------------------------------------------

VENV_DIR = ".venv_commit0"
REQUIRED_PACKAGES = [
    "requests",
    "PyYAML",
    "PyMuPDF",
    "PyPDF2",
    "beautifulsoup4",
    "playwright",
    "google-api-python-client",
    "google-auth-oauthlib",
]


def _bootstrap() -> None:
    """Ensure we are running inside a venv with all dependencies installed."""
    venv_python = os.path.join(VENV_DIR, "bin", "python")

    if sys.executable != os.path.abspath(venv_python):
        if not os.path.isdir(VENV_DIR):
            log.info("Creating virtual environment at %s ...", VENV_DIR)
            subprocess.check_call([sys.executable, "-m", "venv", VENV_DIR])

        log.info("Installing dependencies into venv ...")
        subprocess.check_call(
            [venv_python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"]
        )
        subprocess.check_call(
            [venv_python, "-m", "pip", "install", "--quiet"] + REQUIRED_PACKAGES
        )
        subprocess.check_call(
            [venv_python, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        log.info("Re-executing inside venv ...")
        os.execv(venv_python, [venv_python] + sys.argv)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
_log_filename = LOG_DIR / f"pipeline_{time.strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_filename, encoding="utf-8"),
    ],
)
log = logging.getLogger("commit0")


def _display_width(s: str) -> int:
    import unicodedata

    w = 0
    prev_was_emoji_base = False
    for ch in s:
        cp = ord(ch)
        if cp == 0xFE0F:
            if prev_was_emoji_base:
                w += 1
            prev_was_emoji_base = False
            continue
        if cp == 0xFE0E:
            prev_was_emoji_base = False
            continue
        eaw = unicodedata.east_asian_width(ch)
        cat = unicodedata.category(ch)
        if eaw in ("W", "F"):
            w += 2
            prev_was_emoji_base = False
        elif cat in ("Mn", "Me", "Cf"):
            prev_was_emoji_base = False
        elif cat == "So":
            w += 1
            prev_was_emoji_base = True
        else:
            w += 1
            prev_was_emoji_base = False
    return w


def _pad_cell(text: str, target_width: int) -> str:
    extra = target_width - _display_width(text)
    return text + " " * max(extra, 0)


def _log_table(headers: list[str], rows: list[list[str]]) -> None:
    col_widths = [_display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], _display_width(cell))

    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header_line = (
        "|"
        + "|".join(f" {_pad_cell(h, col_widths[i])} " for i, h in enumerate(headers))
        + "|"
    )

    log.info(sep)
    log.info(header_line)
    log.info(sep)
    for row in rows:
        line = (
            "|"
            + "|".join(
                f" {_pad_cell(row[i], col_widths[i])} " for i in range(len(headers))
            )
            + "|"
        )
        log.info(line)
    log.info(sep)


def _log_step(step: str, status: str = "✅") -> None:
    log.info("%s  %s", status, step)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
SEARCH_ENDPOINT = f"{GITHUB_API}/search/repositories"

TARGET_REPO_COUNT = 50000
MIN_FILTER2_PASS = 3
MAX_RETRY_ROUNDS = 20

# Concurrency settings — tune based on machine / API limits
FILTER_WORKERS = 8  # Parallel threads for filter1 & filter2 API checks
CLONE_WORKERS = 8  # Parallel git clones (network-bound)
YAML_WORKERS = 6  # Parallel YAML generation (GitHub API calls)
PDF_WORKERS = 3  # Parallel Playwright crawls (memory-heavy)
UPLOAD_WORKERS = 3  # Parallel Drive uploads (kept low to avoid macOS SSL crashes)

PRIMARY_STAR_THRESHOLD = 5000
FALLBACK_STAR_THRESHOLD = 3000

STAR_RANGES: list[tuple[int, Optional[int]]] = [
    (100000, None),
    (50000, 99999),
    (30000, 49999),
    (20000, 29999),
    (15000, 19999),
    (10000, 14999),
    (8000, 9999),
    (6000, 7999),
    (5000, 5999),
    (4000, 4999),
    (3500, 3999),
    (3000, 3499),
    (2500, 2999),
    (2000, 2499),
    (1500, 1999),
    (1200, 1499),
    (1000, 1199),
    (800, 999),
    (600, 799),
    (500, 599),
]

MIN_PYTHON_RATIO = 0.95
MAX_REPO_SIZE_KB = 500_000

ML_FRAMEWORK_KEYWORDS = [
    "machine-learning",
    "deep-learning",
    "neural-network",
    "tensorflow",
    "pytorch",
    "keras",
    "torch",
    "jax",
    "huggingface",
    "transformers",
    "llm",
    "diffusion",
    "computer-vision",
]

CLI_TOOL_KEYWORDS = [
    "cli",
    "command-line",
    "terminal",
    "console",
    "shell-tool",
]

NATIVE_WRAPPER_KEYWORDS = [
    "binding",
    "wrapper",
    "ffi",
    "ctypes",
    "cffi",
    "pybind11",
    "cython",
    "pyo3",
    "swig",
    "native",
]

NATIVE_EXTENSION_PATTERNS = [
    re.compile(r"ext_modules\s*=", re.IGNORECASE),
    re.compile(r"Extension\s*\(", re.IGNORECASE),
    re.compile(r"cythonize\s*\(", re.IGNORECASE),
]

BINDING_IMPORT_PATTERNS = [
    re.compile(r"(?:import|from)\s+(?:ctypes|cffi|pybind11|cython|pyo3)\b"),
]

DOCS_FRAMEWORKS = ["sphinx", "mkdocs", "readthedocs", "github.io"]

NON_DOCS_DOMAINS = [
    "pypi.org",
    "github.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "reddit.com",
    "stackoverflow.com",
    "medium.com",
    "dev.to",
    "youtube.com",
    "discord.com",
    "discord.gg",
    "t.me",
]

YAML_FIELD_ORDER = [
    "name",
    "commit",
    "tag",
    "python",
    "install",
    "packages",
    "pip_packages",
    "pre_install",
    "specification",
    "test_cmd",
    "test_dir",
    "src_dir",
]

BASE_DIR = Path(".")
FILTERED_STAGE1_FILE = BASE_DIR / "filtered_stage1.json"
FINAL_SELECTED_FILE = BASE_DIR / "final_selected.json"
PROCESSED_REPOS_FILE = BASE_DIR / "processed_repos.json"
CLONED_REPOS_DIR = BASE_DIR / "cloned_repos"
BUILD_DATASET_DIR = BASE_DIR / "build_dataset"
BUILD_DATASET_REPO = "https://github.com/commit-0/build_dataset.git"
OUTPUT_DIR = BUILD_DATASET_DIR / "data"

# Google Drive upload settings
DRIVE_PARENT_FOLDER_NAME = "Automation Scripts Data"
DRIVE_RAW_SUBFOLDER = "Raw Data"
DRIVE_PDF_SUBFOLDER = "PDF"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_CREDS_FILE = BASE_DIR / "credentials.json"  # OAuth2 client secret from GCP
DRIVE_TOKEN_FILE = BASE_DIR / "drive_token.json"  # Cached OAuth2 token
RAW_REPOS_JSONL_FILE = BASE_DIR / "raw_repos.jsonl"

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

_http_session: Optional["requests.Session"] = None
_http_lock = threading.Lock()


def _get_session() -> "requests.Session":
    """Return a shared thread-safe requests.Session with connection pooling."""
    global _http_session
    if _http_session is None:
        with _http_lock:
            if _http_session is None:
                import requests
                from requests.adapters import HTTPAdapter

                s = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=20, pool_maxsize=20, max_retries=0
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _http_session = s
    return _http_session


def _github_headers() -> dict[str, str]:
    """Build headers for GitHub API requests."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _rate_limit_wait(response: "requests.Response") -> None:
    """Sleep until rate limit resets if we hit 403."""
    reset_ts = response.headers.get("X-RateLimit-Reset")
    if reset_ts:
        wait = max(int(reset_ts) - int(time.time()), 1) + 5
        log.warning("Rate limited. Sleeping %d seconds until reset ...", wait)
        time.sleep(wait)
    else:
        log.warning("Rate limited (no reset header). Sleeping 60s ...")
        time.sleep(60)


def github_get(
    url: str, params: Optional[dict] = None, max_retries: int = 3
) -> "requests.Response":
    """GET with retry, rate-limit handling, and auth fallback."""
    session = _get_session()
    headers = _github_headers()
    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=headers, params=params, timeout=30)
        except Exception as exc:
            log.warning(
                "Request failed (attempt %d/%d): %s", attempt + 1, max_retries, exc
            )
            time.sleep(2**attempt)
            continue

        remaining_str = resp.headers.get("X-RateLimit-Remaining", "")
        if remaining_str.isdigit():
            remaining_val = int(remaining_str)
            if 0 < remaining_val < 100:
                time.sleep(1.0)
            elif remaining_val < 50:
                time.sleep(2.0)

        if resp.status_code == 200:
            return resp
        if resp.status_code == 401 and "Authorization" in headers:
            log.warning("401 with token — retrying without auth ...")
            headers.pop("Authorization", None)
            continue
        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            if remaining == "0" or "rate limit" in resp.text.lower():
                _rate_limit_wait(resp)
                continue
        if resp.status_code == 422:
            log.warning("422 Unprocessable: %s", resp.text[:200])
            return resp
        if resp.status_code >= 500:
            log.warning(
                "Server error %d (attempt %d/%d)",
                resp.status_code,
                attempt + 1,
                max_retries,
            )
            time.sleep(2**attempt)
            continue

        log.warning("Unexpected status %d for %s", resp.status_code, url)
        return resp

    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def github_get_json(url: str, params: Optional[dict] = None) -> Any:
    """GET + parse JSON."""
    resp = github_get(url, params)
    resp.raise_for_status()
    return resp.json()


def _get_languages(full_name: str) -> dict[str, int]:
    """Get language breakdown (bytes) for a repo."""
    return github_get_json(f"{GITHUB_API}/repos/{full_name}/languages")


def _get_repo_contents(full_name: str, path: str = "") -> list[dict]:
    """List directory contents via GitHub API."""
    try:
        return github_get_json(f"{GITHUB_API}/repos/{full_name}/contents/{path}")
    except Exception:
        return []


def _get_file_content(full_name: str, path: str, ref: str = "HEAD") -> Optional[str]:
    """Download raw file content from raw.githubusercontent.com."""
    url = f"https://raw.githubusercontent.com/{full_name}/{ref}/{path}"
    try:
        resp = _get_session().get(url, timeout=15)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception:
        return None


def _file_exists(full_name: str, path: str) -> bool:
    """Check if a file/dir exists in a repo (HEAD request)."""
    url = f"{GITHUB_API}/repos/{full_name}/contents/{path}"
    try:
        resp = _get_session().head(url, headers=_github_headers(), timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# STEP 1: Large-scale repository discovery with star-range partitioning
# ---------------------------------------------------------------------------


def _fetch_range(
    min_stars: int, max_stars: Optional[int], seen: set[str]
) -> list[dict]:
    """Fetch all repos in a single star range (up to 1000 via pagination)."""
    if max_stars is not None:
        q = f"language:python stars:{min_stars}..{max_stars} archived:false"
    else:
        q = f"language:python stars:>={min_stars} archived:false"

    repos: list[dict] = []
    page = 1
    per_page = 100

    while page <= 10:
        params = {
            "q": q,
            "sort": "stars",
            "order": "desc",
            "per_page": per_page,
            "page": page,
        }

        try:
            resp = github_get(SEARCH_ENDPOINT, params)
            if resp.status_code == 422:
                log.warning(
                    "422 for range %d..%s page %d — stopping range",
                    min_stars,
                    max_stars or "∞",
                    page,
                )
                break
            data = resp.json()
        except Exception as exc:
            log.warning(
                "Error fetching range %d..%s page %d: %s",
                min_stars,
                max_stars or "∞",
                page,
                exc,
            )
            break

        items = data.get("items", [])
        if not items:
            break

        for repo in items:
            fn = repo.get("full_name", "")
            if fn and fn not in seen:
                seen.add(fn)
                repos.append(repo)

        if data.get("incomplete_results"):
            log.warning(
                "Incomplete results for range %d..%s page %d",
                min_stars,
                max_stars or "∞",
                page,
            )

        if len(items) < per_page:
            break

        page += 1
        time.sleep(0.5)

    return repos


def fetch_repos(exclude_names: Optional[set[str]] = None) -> list[dict]:
    """
    STEP 1: Discover TARGET_REPO_COUNT Python repos using star-range partitioning.
    Skips any repo in exclude_names (from previous retry rounds).
    """
    log.info("=" * 60)
    log.info("STEP 1: Fetching ~%d Python repositories ...", TARGET_REPO_COUNT)
    log.info("=" * 60)

    all_repos: list[dict] = []
    seen: set[str] = set(exclude_names) if exclude_names else set()
    if seen:
        log.info("  Excluding %d repos from previous rounds.", len(seen))

    for i, (min_s, max_s) in enumerate(STAR_RANGES):
        if len(all_repos) >= TARGET_REPO_COUNT:
            log.info(
                "  Reached target of %d repos — stopping discovery.", TARGET_REPO_COUNT
            )
            break

        label = f"{min_s}..{max_s}" if max_s else f">={min_s}"
        log.info("  Range %d/%d: stars %s ...", i + 1, len(STAR_RANGES), label)

        batch = _fetch_range(min_s, max_s, seen)
        all_repos.extend(batch)
        log.info("    Found %d new repos (total: %d)", len(batch), len(all_repos))

        time.sleep(1)

    all_repos = all_repos[:TARGET_REPO_COUNT]

    with open(RAW_REPOS_JSONL_FILE, "w", encoding="utf-8") as f:
        for repo in all_repos:
            f.write(json.dumps(repo, ensure_ascii=False) + "\n")

    log.info(
        "Discovery complete: %d total repos saved to %s",
        len(all_repos),
        RAW_REPOS_JSONL_FILE,
    )
    return all_repos


# ---------------------------------------------------------------------------
# STEP 2: Filter 1 — Initial quality checks
# ---------------------------------------------------------------------------


def _check_not_fork(repo: dict) -> tuple[bool, str]:
    if repo.get("fork"):
        return False, "Is a fork"
    return True, ""


def _check_not_archived(repo: dict) -> tuple[bool, str]:
    if repo.get("archived"):
        return False, "Is archived"
    return True, ""


def _check_stars(
    repo: dict, threshold: int = PRIMARY_STAR_THRESHOLD
) -> tuple[bool, str]:
    stars = repo.get("stargazers_count", 0)
    if stars < threshold:
        return False, f"Stars {stars} < {threshold}"
    return True, ""


def _check_repo_size(repo: dict) -> tuple[bool, str]:
    size_kb = repo.get("size", 0)
    if size_kb > MAX_REPO_SIZE_KB:
        return False, f"Size {size_kb}KB > {MAX_REPO_SIZE_KB}KB"
    return True, ""


def _check_not_ml_framework(repo: dict) -> tuple[bool, str]:
    topics = [t.lower() for t in repo.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    name = repo.get("full_name", "").lower()
    combined = " ".join(topics) + " " + desc + " " + name

    for kw in ML_FRAMEWORK_KEYWORDS:
        if kw in combined:
            return False, f"ML framework keyword: {kw}"
    return True, ""


def _check_not_cli_tool(repo: dict) -> tuple[bool, str]:
    topics = [t.lower() for t in repo.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    combined = " ".join(topics) + " " + desc

    for kw in CLI_TOOL_KEYWORDS:
        if kw in combined:
            return False, f"CLI tool keyword: {kw}"
    return True, ""


def _check_not_native_wrapper(repo: dict) -> tuple[bool, str]:
    topics = [t.lower() for t in repo.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    combined = " ".join(topics) + " " + desc

    for kw in NATIVE_WRAPPER_KEYWORDS:
        if kw in combined:
            return False, f"Native wrapper keyword: {kw}"
    return True, ""


def _check_python_ratio(full_name: str) -> tuple[bool, str]:
    """Check ≥95% Python by bytes."""
    try:
        langs = _get_languages(full_name)
    except Exception as exc:
        return False, f"Failed to get languages: {exc}"

    if not langs:
        return False, "No languages detected"

    total = sum(langs.values())
    if total == 0:
        return False, "Zero total bytes"

    python_bytes = langs.get("Python", 0)
    ratio = python_bytes / total
    if ratio < MIN_PYTHON_RATIO:
        return False, f"Python ratio {ratio:.2%} < {MIN_PYTHON_RATIO:.0%}"
    return True, ""


def _check_no_native_extensions(
    full_name: str, root_contents: Optional[list[dict]] = None
) -> tuple[bool, str]:
    """Check for C/C++/Cython extensions in setup.py, setup.cfg, and top-level files."""
    setup_py = _get_file_content(full_name, "setup.py")
    if setup_py:
        for pat in NATIVE_EXTENSION_PATTERNS:
            if pat.search(setup_py):
                return False, f"Native extension in setup.py: {pat.pattern}"
        for pat in BINDING_IMPORT_PATTERNS:
            if pat.search(setup_py):
                return False, f"Binding import in setup.py: {pat.pattern}"

    setup_cfg = _get_file_content(full_name, "setup.cfg")
    if setup_cfg:
        for pat in NATIVE_EXTENSION_PATTERNS:
            if pat.search(setup_cfg):
                return False, f"Native extension in setup.cfg: {pat.pattern}"

    if root_contents is None:
        root_contents = _get_repo_contents(full_name)
    for item in root_contents:
        name = item.get("name", "")
        if name.endswith((".c", ".cpp", ".pyx", ".so", ".pyd")):
            return False, f"Native file in root: {name}"

    return True, ""


def _check_docs_website(repo: dict, full_name: str) -> tuple[bool, str]:
    """Check if repo has a documentation website."""
    session = _get_session()
    homepage = repo.get("homepage", "") or ""

    if homepage:
        lhp = homepage.lower()
        is_non_docs = any(d in lhp for d in NON_DOCS_DOMAINS)
        if not is_non_docs and lhp.startswith("http"):
            return True, f"Homepage: {homepage}"

    repo_name = full_name.split("/")[-1]
    rtd_url = f"https://{repo_name}.readthedocs.io"
    try:
        resp = session.head(rtd_url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return True, f"ReadTheDocs: {rtd_url}"
    except Exception:
        pass

    org = full_name.split("/")[0]
    ghio_url = f"https://{org}.github.io/{repo_name}"
    try:
        resp = session.head(ghio_url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return True, f"GitHub Pages: {ghio_url}"
    except Exception:
        pass

    if _file_exists(full_name, "docs"):
        return True, "Has docs/ directory"

    return False, "No documentation website found"


def _check_project_structure(
    full_name: str, root_contents: Optional[list[dict]] = None
) -> tuple[bool, str]:
    """Check for proper project structure: src/pkg layout, tests, build config."""
    if root_contents is None:
        root_contents = _get_repo_contents(full_name)

    if not root_contents:
        return False, "Could not read root contents"

    names = {item["name"] for item in root_contents if "name" in item}
    types = {item["name"]: item.get("type", "") for item in root_contents}

    has_build_config = bool(names & {"pyproject.toml", "setup.py", "setup.cfg"})
    if not has_build_config:
        return False, "No pyproject.toml/setup.py/setup.cfg"

    has_tests = bool(names & {"tests", "test"})
    if not has_tests:
        for n in names:
            if types.get(n) == "dir" and n.startswith("test"):
                has_tests = True
                break

    if not has_tests:
        return False, "No tests/ or test/ directory"

    has_src = "src" in names and types.get("src") == "dir"
    if not has_src:
        repo_name = full_name.split("/")[-1].replace("-", "_").lower()
        for item in root_contents:
            if item.get("type") == "dir":
                n = item["name"]
                if n in (
                    "tests",
                    "test",
                    "docs",
                    "doc",
                    ".github",
                    ".git",
                    "examples",
                    "scripts",
                    "benchmarks",
                    "tools",
                    "bin",
                ):
                    continue
                if n.startswith("."):
                    continue
                if _file_exists(full_name, f"{n}/__init__.py"):
                    has_src = True
                    break

    if not has_src:
        return False, "No src/ or package directory found"

    return True, ""


def _check_code_quality_basic(full_name: str) -> tuple[bool, str]:
    """Basic code quality check: valid Python syntax in key files."""
    for filepath in ["setup.py", "pyproject.toml"]:
        content = _get_file_content(full_name, filepath)
        if content and filepath.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                return False, f"Syntax error in {filepath}: {exc}"
    return True, ""


def _filter1_single(repo: dict) -> Optional[dict]:
    """Run all Filter 1 checks on a single repo. Returns repo if passed, else None."""
    fn = repo.get("full_name", "unknown")

    for check_fn in (
        _check_not_fork,
        _check_not_archived,
        _check_not_ml_framework,
        _check_not_cli_tool,
        _check_not_native_wrapper,
        _check_repo_size,
    ):
        ok, reason = check_fn(repo)
        if not ok:
            return None

    ok, reason = _check_stars(repo, threshold=FALLBACK_STAR_THRESHOLD)
    if not ok:
        return None

    ok, reason = _check_python_ratio(fn)
    if not ok:
        return None

    root_contents = _get_repo_contents(fn)

    ok, reason = _check_no_native_extensions(fn, root_contents)
    if not ok:
        return None

    ok, reason = _check_docs_website(repo, fn)
    if not ok:
        return None

    ok, reason = _check_project_structure(fn, root_contents)
    if not ok:
        return None

    _check_code_quality_basic(fn)

    repo["_filter1_passed"] = True
    repo["_root_contents"] = root_contents
    return repo


def filter1(repos: list[dict]) -> list[dict]:
    """
    STEP 2: Apply Filter 1 (initial quality checks) in parallel.
    Returns repos that pass all MUST checks and most SHOULD checks.
    """
    log.info("=" * 60)
    log.info("STEP 2: Applying Filter 1 (Initial Filter) to %d repos ...", len(repos))
    log.info("=" * 60)

    passed: list[dict] = []
    done_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=FILTER_WORKERS) as pool:
        future_to_repo = {pool.submit(_filter1_single, repo): repo for repo in repos}
        for future in concurrent.futures.as_completed(future_to_repo):
            done_count += 1
            if done_count % 100 == 0:
                log.info(
                    "  Filter 1 progress: %d/%d (passed so far: %d)",
                    done_count,
                    len(repos),
                    len(passed),
                )
            result = future.result()
            if result is not None:
                passed.append(result)
                fn = result.get("full_name", "")
                stars = result.get("stargazers_count", 0)
                log.info("  ✓ PASS: %s (★ %d)", fn, stars)

    log.info("Filter 1 complete: %d / %d passed", len(passed), len(repos))

    saveable = []
    for r in passed:
        r_copy = {k: v for k, v in r.items() if not k.startswith("_")}
        saveable.append(r_copy)

    with open(FILTERED_STAGE1_FILE, "w") as f:
        json.dump(saveable, f, indent=1)
    log.info("Saved Filter 1 results to %s", FILTERED_STAGE1_FILE)

    return passed


# ---------------------------------------------------------------------------
# STEP 3: Filter 2 — Commit0 Benchmark validation
# ---------------------------------------------------------------------------


def _check_uses_pytest(full_name: str) -> tuple[bool, str]:
    """Check if the repo uses pytest as its test framework."""
    for cfg_file in ("conftest.py", "pytest.ini", ".pytest.ini"):
        if _file_exists(full_name, cfg_file):
            return True, f"Found {cfg_file}"

    content = _get_file_content(full_name, "pyproject.toml")
    if content:
        if "[tool.pytest" in content or "pytest" in content.lower():
            return True, "pytest in pyproject.toml"

    content = _get_file_content(full_name, "setup.cfg")
    if content:
        if "[tool:pytest]" in content or "pytest" in content.lower():
            return True, "pytest in setup.cfg"

    content = _get_file_content(full_name, "tox.ini")
    if content and "pytest" in content.lower():
        return True, "pytest in tox.ini"

    for req_file in (
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "dev-requirements.txt",
        "test-requirements.txt",
    ):
        content = _get_file_content(full_name, req_file)
        if content and "pytest" in content.lower():
            return True, f"pytest in {req_file}"

    return False, "No pytest configuration found"


def _check_no_gpu_usage(full_name: str) -> tuple[bool, str]:
    """Check if repo requires GPU (CUDA, GPU keywords in config/requirements)."""
    for filepath in ("requirements.txt", "setup.py", "pyproject.toml", "setup.cfg"):
        content = _get_file_content(full_name, filepath)
        if content:
            lower = content.lower()
            if any(kw in lower for kw in ["cuda", "cupy", "nvidia", "gpu-required"]):
                return False, f"GPU keyword in {filepath}"
    return True, ""


def _check_installable(full_name: str) -> tuple[bool, str]:
    """Check if pip install -e . would likely work (has setup.py/pyproject.toml)."""
    if _file_exists(full_name, "pyproject.toml"):
        content = _get_file_content(full_name, "pyproject.toml")
        if content and ("[build-system]" in content or "[project]" in content):
            return True, "Has pyproject.toml with build-system/project"
    if _file_exists(full_name, "setup.py"):
        return True, "Has setup.py"
    if _file_exists(full_name, "setup.cfg"):
        content = _get_file_content(full_name, "setup.cfg")
        if content and "[metadata]" in content:
            return True, "Has setup.cfg with metadata"
    return False, "Not installable (no build config)"


def _check_dependency_count(full_name: str) -> tuple[bool, str]:
    """Check if repo has fewer than 100 dependencies."""
    deps: set[str] = set()

    content = _get_file_content(full_name, "requirements.txt")
    if content:
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                pkg = re.split(r"[><=!~\[]", line)[0].strip()
                if pkg:
                    deps.add(pkg.lower())

    content = _get_file_content(full_name, "pyproject.toml")
    if content:
        dep_match = re.findall(r'"([a-zA-Z0-9_-]+)', content)
        for d in dep_match:
            if len(d) > 2:
                deps.add(d.lower())

    if len(deps) > 100:
        return False, f"Too many dependencies: {len(deps)}"
    return True, ""


def _check_python_version_compat(full_name: str) -> tuple[bool, str]:
    """Check if repo supports Python 3.10+."""
    content = _get_file_content(full_name, "pyproject.toml")
    if content:
        match = re.search(r'requires-python\s*=\s*"([^"]*)"', content)
        if match:
            spec = match.group(1)
            if re.search(r"<=?\s*3\.[0-9]\b", spec):
                return False, f"Python version spec too old: {spec}"

    content = _get_file_content(full_name, "setup.py")
    if content:
        match = re.search(r'python_requires\s*=\s*["\']([^"\']*)', content)
        if match:
            spec = match.group(1)
            if re.search(r"<=?\s*3\.[0-9]\b", spec):
                return False, f"Python version spec too old: {spec}"

    return True, ""


def _check_test_isolation(full_name: str) -> tuple[bool, str]:
    """
    Heuristic check for test isolation — look for network calls,
    external service deps, filesystem side effects in test configs.
    """
    content = _get_file_content(full_name, "conftest.py")
    if content:
        lower = content.lower()
        if "monkeypatch" in lower or "mock" in lower or "responses" in lower:
            pass
        if any(
            kw in lower
            for kw in [
                "docker",
                "redis.from_url",
                "psycopg2",
                "mysql",
                "mongodb",
                "elasticsearch",
            ]
        ):
            return False, "Test config references external services"

    return True, ""


def _filter2_single(repo: dict) -> Optional[dict]:
    """Run all Filter 2 checks on a single repo. Returns repo if passed, else None."""
    fn = repo.get("full_name", "unknown")

    ok, reason = _check_uses_pytest(fn)
    if not ok:
        return None

    ok, reason = _check_no_gpu_usage(fn)
    if not ok:
        return None

    should_score = 0

    ok, reason = _check_installable(fn)
    if ok:
        should_score += 1

    ok, _ = _check_stars(repo, threshold=PRIMARY_STAR_THRESHOLD)
    if ok:
        should_score += 1

    root_contents = repo.get("_root_contents") or _get_repo_contents(fn)
    ok, reason = _check_project_structure(fn, root_contents)
    if ok:
        should_score += 1

    ok, reason = _check_python_version_compat(fn)
    if ok:
        should_score += 1

    _check_dependency_count(fn)
    _check_test_isolation(fn)

    if should_score < 2:
        return None

    return repo


def filter2(repos: list[dict]) -> list[dict]:
    """
    STEP 3: Apply Filter 2 (Commit0 Benchmark validation) in parallel.
    Full validation on Filter 1 results with early exit on MUST failures.
    """
    log.info("=" * 60)
    log.info(
        "STEP 3: Applying Filter 2 (Commit0 Benchmark) to %d repos ...", len(repos)
    )
    log.info("=" * 60)

    passed: list[dict] = []
    done_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=FILTER_WORKERS) as pool:
        future_to_repo = {pool.submit(_filter2_single, repo): repo for repo in repos}
        for future in concurrent.futures.as_completed(future_to_repo):
            done_count += 1
            repo = future_to_repo[future]
            fn = repo.get("full_name", "unknown")
            stars = repo.get("stargazers_count", 0)
            result = future.result()
            if result is not None:
                passed.append(result)
                log.info(
                    "  [%d/%d] ✓ ACCEPTED: %s (★ %d)",
                    done_count,
                    len(repos),
                    fn,
                    stars,
                )
            else:
                log.info(
                    "  [%d/%d] ✗ REJECTED: %s (★ %d)",
                    done_count,
                    len(repos),
                    fn,
                    stars,
                )

    log.info("Filter 2 complete: %d / %d passed", len(passed), len(repos))

    saveable = []
    for r in passed:
        r_copy = {k: v for k, v in r.items() if not k.startswith("_")}
        saveable.append(r_copy)

    with open(FINAL_SELECTED_FILE, "w") as f:
        json.dump(saveable, f, indent=1)
    log.info("Saved final selection to %s", FINAL_SELECTED_FILE)

    return passed


# ---------------------------------------------------------------------------
# STEP 5: Clone repositories
# ---------------------------------------------------------------------------


def clone_repo(full_name: str) -> Path:
    """Clone a repo with --depth 1. Returns the clone path."""
    safe_name = full_name.replace("/", "_")
    clone_path = CLONED_REPOS_DIR / safe_name

    if clone_path.exists():
        log.info("  Repo already cloned: %s", clone_path)
        return clone_path

    CLONED_REPOS_DIR.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{full_name}.git"
    log.info("  Cloning %s → %s ...", url, clone_path)

    try:
        subprocess.check_call(
            ["git", "clone", "--depth", "1", url, str(clone_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        log.error("  Failed to clone %s: %s", full_name, exc)
        raise

    return clone_path


# ---------------------------------------------------------------------------
# STEP 6: Setup build_dataset (one-time)
# ---------------------------------------------------------------------------


def setup_build_dataset() -> Path:
    if BUILD_DATASET_DIR.exists():
        log.info("build_dataset/ already exists — reusing.")
        try:
            subprocess.check_call(
                ["git", "pull", "--ff-only"],
                cwd=str(BUILD_DATASET_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("  Updated build_dataset via git pull.")
        except subprocess.CalledProcessError:
            log.info("  git pull failed (non-critical) — using existing version.")
    else:
        log.info("Cloning build_dataset from %s ...", BUILD_DATASET_REPO)
        subprocess.check_call(
            ["git", "clone", BUILD_DATASET_REPO, str(BUILD_DATASET_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        log.info("Installing build_dataset dependencies ...")
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "-e",
                str(BUILD_DATASET_DIR),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    _patch_scrape_pdf()
    return BUILD_DATASET_DIR


def _patch_scrape_pdf() -> None:
    """Fix upstream bugs in scrape_pdf.py and add page-limit guard.

    Patches applied (all idempotent):
    1. ``output_dir`` → ``"pdfs"`` (undefined variable bug).
    2. ``asyncio.get_event_loop().run_until_complete(`` → ``asyncio.run(``
       (deprecated in Python ≥ 3.12, removed in 3.14).
    3. Inject ``MAX_PAGES = 50`` cap into ``crawl_website()`` so the BFS
       crawler doesn't visit thousands of pages on large doc sites.
    """
    scrape_file = BUILD_DATASET_DIR / "scrape_pdf.py"
    if not scrape_file.exists():
        return
    text = scrape_file.read_text()
    changed = False

    if "os.makedirs(output_dir" in text:
        text = text.replace("os.makedirs(output_dir", 'os.makedirs("pdfs"')
        changed = True

    if "asyncio.get_event_loop().run_until_complete(" in text:
        text = text.replace(
            "asyncio.get_event_loop().run_until_complete(",
            "asyncio.run(",
        )
        changed = True

    if "MAX_PAGES" not in text:
        text = text.replace(
            "async def crawl_website(browser, base_url, output_dir):",
            "MAX_PAGES = 30\n\n\nasync def crawl_website(browser, base_url, output_dir):",
        )
        text = text.replace(
            "    while to_visit:\n        current_url = to_visit.pop(0)",
            '    while to_visit:\n        if len(visited) >= MAX_PAGES:\n            print(f"Reached MAX_PAGES ({MAX_PAGES}), stopping crawl.")\n            break\n        current_url = to_visit.pop(0)',
        )
        changed = True

    if changed:
        scrape_file.write_text(text)
        log.info("  Patched scrape_pdf.py: fixed output_dir / asyncio / page-limit.")


# ---------------------------------------------------------------------------
# STEP 7: PDF generation (Playwright-based, replaces broken pyppeteer scrape_pdf.py)
# ---------------------------------------------------------------------------

MAX_CRAWL_PAGES = 30


def _try_direct_pdf_download(spec_url: str, repo_name: str) -> bool:
    """Try ReadTheDocs/direct PDF URL before crawling. Saves to build_dataset/pdfs/."""
    session = _get_session()

    repo_pdf_dir = BUILD_DATASET_DIR / "pdfs" / repo_name
    repo_pdf_dir.mkdir(parents=True, exist_ok=True)
    out_path = repo_pdf_dir / f"{repo_name}.pdf"

    candidates: list[str] = []

    if "readthedocs.io" in spec_url or "readthedocs.org" in spec_url:
        from urllib.parse import urlparse

        host = urlparse(spec_url).hostname or ""
        project = host.split(".")[0]
        candidates.append(
            f"https://{project}.readthedocs.io/_/downloads/en/latest/pdf/"
        )
        candidates.append(
            f"https://{project}.readthedocs.io/_/downloads/en/stable/pdf/"
        )

    if spec_url.rstrip("/").endswith(".pdf"):
        candidates.append(spec_url)

    for url in candidates:
        try:
            log.info("  Trying direct PDF download: %s", url)
            resp = session.get(url, timeout=60, stream=True)
            if resp.status_code == 200 and "pdf" in resp.headers.get(
                "content-type", ""
            ):
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                bz2_path = out_path.with_suffix(".pdf.bz2")
                with open(out_path, "rb") as f_in, bz2.open(bz2_path, "wb") as f_out:
                    f_out.write(f_in.read())
                pdf_size = out_path.stat().st_size
                bz2_size = bz2_path.stat().st_size
                log.info(
                    "  ✓ Direct PDF downloaded: %s (pdf: %d bytes, bz2: %d bytes)",
                    bz2_path,
                    pdf_size,
                    bz2_size,
                )
                return True
        except Exception as exc:
            log.debug("  Direct download failed for %s: %s", url, exc)

    return False


def _crawl_and_pdf_playwright(spec_url: str, repo_name: str) -> bool:
    """BFS-crawl spec_url (max MAX_CRAWL_PAGES pages), extract content from
    each page, combine into a single HTML document, and render one clean PDF."""
    from urllib.parse import urlparse, urljoin
    from playwright.sync_api import sync_playwright

    repo_pdf_dir = BUILD_DATASET_DIR / "pdfs" / repo_name
    repo_pdf_dir.mkdir(parents=True, exist_ok=True)
    out_path = repo_pdf_dir / f"{repo_name}.pdf"

    base_netloc = urlparse(spec_url).netloc
    visited: set[str] = set()
    to_visit: list[str] = [spec_url]
    collected_html: list[str] = []
    seen_content_hashes: set[int] = set()

    _CONTENT_SELECTORS = [
        "[role='main']",
        "main",
        "article",
        ".document",
        ".body",
        ".rst-content",
        ".wy-nav-content",
        ".content",
        ".md-content",
        "#content",
        ".main-content",
    ]

    _REMOVE_SELECTORS = (
        "nav, .sidebar, .nav-side, .wy-nav-side, .wy-side-scroll, "
        ".rst-versions, header, footer, .headerlink, .mobile-header, "
        ".toctree-wrapper, .breadcrumb, .page-nav, .prev-next-area, "
        "script, style, .admonition.note .last a.reference.internal"
    )

    _SKIP_PATH_PATTERNS = (
        "/_sources/",
        "/_static/",
        "/_modules/",
        "/_images/",
        "/genindex",
        "/search",
        "/py-modindex",
        "/_downloads/",
        "/changelog",
        "/CHANGELOG",
    )
    _SKIP_EXTENSIONS = (".txt", ".json", ".xml", ".zip", ".gz", ".tar", ".whl", ".egg")

    # Regex patterns that indicate junk links we should never follow.
    # Covers: locale variants, old doc versions, individual game/env pages, etc.
    import re as _re

    _SKIP_LINK_RE = _re.compile(
        r"(?:"
        # Locale-prefixed paths like /ja/, /zh-cn/, /pt-br/ (but not /en/)
        r"/(?:ja|zh|ko|de|fr|es|it|pt|ru|ar|he|tr|pl|nl|sv|cs|hu|fi|da|nb|uk|vi|th|id|ms|el|ro|bg|sr|hr|sk|sl|et|lv|lt|sq|mk|ka|hy|az|be|bs|ca|eu|fa|ga|gl|is|kk|km|kn|lo|mg|ml|mn|my|ne|ps|si|ta|te|tl|ur|uz|cy|bn|gu|mr|pa|sw|am|as|or|sd|ha|ig|yo|so|zu|rw|mt|lb|fo|se|ku|fy|mi|sm|to|ht|jv|su|oc|br|gd|af|tg|eo|ia|ie|io|vo|wa|an|ast|scn|sc|nap|vec|pms|lij|eml|fur|lmo|rup|szl|hsb|dsb|wuu|nan|hak|gan|cdo|kok|doi|mni|bho|mai|bhb|shn|new|dz|dv)"
        r"(?:-[a-z]{2,4})?/"  # optional region like -cn, -br, -hant
        r"|"
        # Old version paths like /en/v0.4.3/, /en/0.3.14/, /v2.1/, /stable/, /latest/
        r"/(?:en/)?v?\d+\.\d+(?:\.\d+)?(?:[a-z0-9._-]*)/"
        r"|"
        # Individual game/environment pages (Gym, Gymnasium Atari, MuJoCo, etc.)
        r"/environments?/(?:atari|mujoco|box2d|classic_control|toy_text|third_party)/"
        r"|"
        # Common junk: print pages, single-source, edit-on-github
        r"/_print/"
        r"|"
        r"/edit/"
        r")",
        _re.IGNORECASE,
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()

            while to_visit and len(visited) < MAX_CRAWL_PAGES:
                current = to_visit.pop(0)
                if current in visited:
                    continue

                parsed = urlparse(current)
                if parsed.fragment:
                    continue
                if parsed.netloc != base_netloc:
                    continue

                path_lower = parsed.path.lower()
                if any(p in path_lower for p in _SKIP_PATH_PATTERNS):
                    visited.add(current)
                    continue
                if any(path_lower.endswith(ext) for ext in _SKIP_EXTENSIONS):
                    visited.add(current)
                    continue

                visited.add(current)
                log.info("    [%d/%d] %s", len(visited), MAX_CRAWL_PAGES, current)

                try:
                    # Try progressively less strict wait strategies
                    resp = None
                    for _wait_strat in ("networkidle", "domcontentloaded", "load"):
                        try:
                            resp = page.goto(
                                current, wait_until=_wait_strat, timeout=30000
                            )
                            break  # success
                        except Exception as nav_exc:
                            if _wait_strat == "load":
                                raise nav_exc  # all strategies exhausted
                            log.debug(
                                "      %s failed with %s — retrying with weaker wait",
                                current,
                                _wait_strat,
                            )

                    actual_url = page.url
                    if actual_url.startswith(
                        "chrome-error://"
                    ) or actual_url.startswith("chrome://"):
                        log.warning(
                            "    Skipping %s: navigation redirected to %s",
                            current,
                            actual_url,
                        )
                        continue

                    if resp and resp.status == 404:
                        continue

                    page.evaluate(
                        """() => {
                        window.scrollTo(0, document.body.scrollHeight);
                        return new Promise(r => setTimeout(r, 500));
                    }"""
                    )
                    page.wait_for_timeout(800)

                    page.evaluate(
                        """() => Promise.all(
                        Array.from(document.images)
                             .filter(i => !i.complete)
                             .map(i => new Promise(r => {
                                 i.onload = i.onerror = r;
                                 setTimeout(r, 5000);
                             }))
                    )"""
                    )

                    links = page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)"
                    )
                    for link in links:
                        full = urljoin(current, link)
                        canon = full.split("#")[0].rstrip("/")
                        canon_path = urlparse(canon).path
                        if (
                            canon not in visited
                            and urlparse(canon).netloc == base_netloc
                            and canon.startswith(
                                spec_url.split("#")[0].rstrip("/").rsplit("/", 1)[0]
                            )
                            and not _SKIP_LINK_RE.search(canon_path)
                        ):
                            to_visit.append(canon)

                    page.evaluate(
                        """(selectors) => {
                        document.querySelectorAll(selectors).forEach(el => el.remove());
                    }""",
                        _REMOVE_SELECTORS,
                    )

                    page.evaluate(
                        """() => {
                        document.querySelectorAll('img').forEach(img => {
                            if (img.src && !img.src.startsWith('data:')) {
                                try { img.src = new URL(img.src, document.baseURI).href; }
                                catch(e) {}
                            }
                        });
                    }"""
                    )

                    content_html = None
                    for sel in _CONTENT_SELECTORS:
                        content_html = page.evaluate(
                            """(sel) => {
                            const el = document.querySelector(sel);
                            return el ? el.innerHTML : null;
                        }""",
                            sel,
                        )
                        if content_html and len(content_html.strip()) > 100:
                            break

                    if not content_html or len(content_html.strip()) < 100:
                        content_html = page.evaluate(
                            "() => document.body ? document.body.innerHTML : ''"
                        )

                    if content_html and len(content_html.strip()) > 50:
                        content_hash = hash(content_html.strip()[:2000])
                        if content_hash in seen_content_hashes:
                            log.info("      Duplicate content — skipping")
                            continue
                        seen_content_hashes.add(content_hash)
                        title = page.title() or current
                        collected_html.append(
                            f'<div class="page-section">'
                            f'<h1 class="page-title">{title}</h1>'
                            f"{content_html}"
                            f"</div>"
                        )
                except Exception as exc:
                    log.warning("    Skipping %s: %s", current, exc)

            if not collected_html:
                browser.close()
                log.warning("  No content extracted for %s", repo_name)
                return False

            combined_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{repo_name} Documentation</title>
<style>
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                     "Helvetica Neue", Arial, sans-serif;
        font-size: 11pt;
        line-height: 1.6;
        color: #333;
        margin: 0;
        padding: 20px 40px;
    }}
    .page-section {{
        page-break-before: always;
        padding-top: 10px;
    }}
    .page-section:first-child {{
        page-break-before: avoid;
    }}
    .page-title {{
        font-size: 20pt;
        color: #1a1a1a;
        border-bottom: 2px solid #ddd;
        padding-bottom: 8px;
        margin-bottom: 16px;
    }}
    h1, h2, h3, h4, h5, h6 {{
        color: #1a1a1a;
        margin-top: 1.2em;
        margin-bottom: 0.6em;
        page-break-after: avoid;
    }}
    h2 {{ font-size: 16pt; border-bottom: 1px solid #eee; padding-bottom: 4px; }}
    h3 {{ font-size: 13pt; }}
    pre, code {{
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        font-size: 9pt;
    }}
    pre {{
        background: #f6f8fa;
        border: 1px solid #e1e4e8;
        border-radius: 4px;
        padding: 12px;
        overflow-x: visible;
        white-space: pre-wrap;
        word-wrap: break-word;
        page-break-inside: avoid;
    }}
    code {{
        background: #f0f0f0;
        padding: 2px 4px;
        border-radius: 3px;
    }}
    pre code {{
        background: none;
        padding: 0;
    }}
    img {{
        max-width: 100%;
        height: auto;
        display: block;
        margin: 10px 0;
    }}
    table {{
        border-collapse: collapse;
        width: 100%;
        margin: 12px 0;
        font-size: 10pt;
        page-break-inside: avoid;
    }}
    th, td {{
        border: 1px solid #ddd;
        padding: 6px 10px;
        text-align: left;
    }}
    th {{ background: #f6f8fa; font-weight: 600; }}
    a {{ color: #0366d6; text-decoration: none; }}
    blockquote {{
        border-left: 4px solid #ddd;
        margin: 10px 0;
        padding: 8px 16px;
        color: #555;
    }}
    .admonition, .note, .warning, .tip {{
        border-left: 4px solid #0366d6;
        background: #f8f9fa;
        padding: 10px 14px;
        margin: 12px 0;
        border-radius: 0 4px 4px 0;
    }}
    .warning {{ border-left-color: #e36209; }}
    dl dt {{ font-weight: 600; margin-top: 10px; }}
    dl dd {{ margin-left: 20px; }}
</style>
</head>
<body>
{"".join(collected_html)}
</body>
</html>"""

            page.set_content(
                combined_html, wait_until="domcontentloaded", timeout=60000
            )
            page.wait_for_timeout(2000)

            page.evaluate(
                """() => Promise.all(
                Array.from(document.images)
                     .filter(i => !i.complete)
                     .map(i => new Promise(r => {
                         i.onload = i.onerror = r;
                         setTimeout(r, 8000);
                     }))
            )"""
            )

            page.pdf(
                path=str(out_path),
                format="A4",
                print_background=True,
                margin={
                    "top": "20mm",
                    "bottom": "20mm",
                    "left": "15mm",
                    "right": "15mm",
                },
            )

            browser.close()
    except Exception as exc:
        log.error("  Playwright browser error: %s", exc)
        return False

    if not out_path.exists() or out_path.stat().st_size < 1000:
        log.warning("  PDF output missing or too small for %s", repo_name)
        return False

    bz2_path = out_path.with_suffix(".pdf.bz2")
    with open(out_path, "rb") as f_in, bz2.open(bz2_path, "wb") as f_out:
        f_out.write(f_in.read())

    pdf_size = out_path.stat().st_size
    bz2_size = bz2_path.stat().st_size
    log.info(
        "  ✓ PDF generated: %s (%d sections, pdf: %d bytes, bz2: %d bytes)",
        bz2_path,
        len(collected_html),
        pdf_size,
        bz2_size,
    )
    return True


def _readme_fallback_pdf(full_name: str, repo_name: str) -> bool:
    """Last-resort PDF: fetch README from GitHub and render it."""
    from playwright.sync_api import sync_playwright

    session = _get_session()
    headers = _github_headers()
    headers["Accept"] = "application/vnd.github.v3.html"
    url = f"{GITHUB_API}/repos/{full_name}/readme"
    try:
        resp = session.get(url, headers=headers, timeout=30)
        if resp.status_code != 200 or len(resp.text.strip()) < 100:
            log.warning("  README fallback: no usable README for %s", full_name)
            return False
    except Exception as exc:
        log.warning("  README fallback failed for %s: %s", full_name, exc)
        return False

    readme_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{full_name} — README</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
         "Helvetica Neue", Arial, sans-serif; font-size: 11pt;
         line-height: 1.6; color: #333; padding: 20px 40px; }}
  pre {{ background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px;
         padding: 12px; white-space: pre-wrap; word-wrap: break-word; }}
  code {{ font-size: 9pt; }}
  h1,h2,h3 {{ color: #1a1a1a; }}
</style></head><body>{resp.text}</body></html>"""

    repo_pdf_dir = BUILD_DATASET_DIR / "pdfs" / repo_name
    repo_pdf_dir.mkdir(parents=True, exist_ok=True)
    out_path = repo_pdf_dir / f"{repo_name}.pdf"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.set_content(readme_html, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.pdf(
                path=str(out_path),
                format="A4",
                print_background=True,
                margin={
                    "top": "20mm",
                    "bottom": "20mm",
                    "left": "15mm",
                    "right": "15mm",
                },
            )
            browser.close()
    except Exception as exc:
        log.error("  README fallback PDF render failed: %s", exc)
        return False

    if not out_path.exists() or out_path.stat().st_size < 500:
        return False

    bz2_path = out_path.with_suffix(".pdf.bz2")
    with open(out_path, "rb") as f_in, bz2.open(bz2_path, "wb") as f_out:
        f_out.write(f_in.read())

    pdf_size = out_path.stat().st_size
    bz2_size = bz2_path.stat().st_size
    log.info(
        "  ✓ README fallback PDF: %s (pdf: %d bytes, bz2: %d bytes)",
        bz2_path,
        pdf_size,
        bz2_size,
    )
    return True


def generate_pdf(repo_yaml_path: Path) -> bool:
    import yaml as _yaml

    with open(repo_yaml_path) as fh:
        data = _yaml.safe_load(fh)

    entry = next(iter(data.values())) if data else {}
    spec_url = entry.get("specification", "")
    full_name = entry.get("name", "")
    repo_name = full_name.replace("/", "_")

    if not repo_name:
        log.warning("  Missing repo name — skipping PDF")
        return False

    if spec_url:
        if _try_direct_pdf_download(spec_url, repo_name):
            return True

        log.info("  Direct download unavailable — crawling with Playwright")
        if _crawl_and_pdf_playwright(spec_url, repo_name):
            return True

        log.info("  Playwright crawl failed — trying README fallback")

    log.info("  Generating PDF from README for %s", full_name)
    return _readme_fallback_pdf(full_name, repo_name)


# ---------------------------------------------------------------------------
# STEP 8: YAML generation
# ---------------------------------------------------------------------------


def _detect_src_dir(full_name: str, root_contents: list[dict]) -> str:
    """Detect the main source directory."""
    names = {item["name"] for item in root_contents if "name" in item}
    types = {item["name"]: item.get("type", "") for item in root_contents}

    if "src" in names and types.get("src") == "dir":
        return "src"

    repo_name = full_name.split("/")[-1].replace("-", "_").lower()
    for item in root_contents:
        if item.get("type") == "dir":
            n = item["name"]
            if n.replace("-", "_").lower() == repo_name:
                return n

    for item in root_contents:
        if item.get("type") == "dir":
            n = item["name"]
            if n.startswith(".") or n in (
                "tests",
                "test",
                "docs",
                "doc",
                ".github",
                "examples",
                "scripts",
                "benchmarks",
                "tools",
                "bin",
            ):
                continue
            if _file_exists(full_name, f"{n}/__init__.py"):
                return n

    return "."


def _detect_test_dir(root_contents: list[dict]) -> str:
    """Detect the test directory."""
    names = {item["name"] for item in root_contents if "name" in item}
    if "tests" in names:
        return "tests"
    if "test" in names:
        return "test"
    return "tests"


def _detect_test_cmd(full_name: str) -> str:
    """Detect the appropriate pytest command."""
    content = _get_file_content(full_name, "pyproject.toml")
    if content:
        if "benchmark" in content.lower():
            return "pytest --benchmark-disable"
        if "addopts" in content:
            return "pytest"

    content = _get_file_content(full_name, "tox.ini")
    if content and "pytest" in content:
        return "pytest"

    return "pytest"


def _detect_python_version(full_name: str) -> str:
    """Detect the Python version from config files."""
    content = _get_file_content(full_name, "pyproject.toml")
    if content:
        match = re.search(r'requires-python\s*=\s*">=?\s*(3\.\d+)', content)
        if match:
            return match.group(1)

    content = _get_file_content(full_name, "setup.py")
    if content:
        match = re.search(r'python_requires\s*=\s*["\']>=?\s*(3\.\d+)', content)
        if match:
            return match.group(1)

    content = _get_file_content(full_name, "setup.cfg")
    if content:
        match = re.search(r"python_requires\s*=\s*>=?\s*(3\.\d+)", content)
        if match:
            return match.group(1)

    return "3.10"


def _detect_install_cmd(full_name: str) -> str:
    """Detect the install command."""
    content = _get_file_content(full_name, "pyproject.toml")
    if content:
        extras = re.findall(
            r"\[project\.optional-dependencies\]\s*\n((?:.*=.*\n)*)", content
        )
        if extras:
            for extra_name in ("dev", "test", "testing", "all"):
                if (
                    f"[project.optional-dependencies]\n{extra_name}" in content
                    or re.search(rf"\b{extra_name}\s*=\s*\[", content)
                ):
                    return f"pip install -e .[{extra_name}]"

    return "pip install -e ."


def _detect_docs_url(repo: dict, full_name: str) -> Optional[str]:
    """Detect the documentation URL."""
    session = _get_session()
    homepage = repo.get("homepage", "") or ""
    if homepage:
        lhp = homepage.lower()
        is_non_docs = any(d in lhp for d in NON_DOCS_DOMAINS)
        if not is_non_docs and lhp.startswith("http"):
            return homepage

    repo_name = full_name.split("/")[-1]

    rtd_url = f"https://{repo_name}.readthedocs.io"
    try:
        resp = session.head(rtd_url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return rtd_url
    except Exception:
        pass

    org = full_name.split("/")[0]
    ghio_url = f"https://{org}.github.io/{repo_name}"
    try:
        resp = session.head(ghio_url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return ghio_url
    except Exception:
        pass

    return None


def _detect_packages(full_name: str, root_contents: list[dict]) -> Optional[list[str]]:
    """Detect requirements files."""
    names = {item["name"] for item in root_contents if "name" in item}
    req_files = []
    for candidate in (
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "dev-requirements.txt",
        "test-requirements.txt",
    ):
        if candidate in names:
            req_files.append(candidate)
    return req_files if req_files else None


def _get_latest_commit(full_name: str) -> Optional[str]:
    """Get the latest commit SHA from the default branch."""
    try:
        data = github_get_json(
            f"{GITHUB_API}/repos/{full_name}/commits", params={"per_page": 1}
        )
        if data and isinstance(data, list) and len(data) > 0:
            return data[0].get("sha")
    except Exception:
        pass
    return None


def _get_latest_tag(full_name: str) -> Optional[str]:
    """Get the latest release tag."""
    try:
        data = github_get_json(f"{GITHUB_API}/repos/{full_name}/releases/latest")
        if isinstance(data, dict) and "tag_name" in data:
            return data["tag_name"]
    except Exception:
        pass

    try:
        data = github_get_json(
            f"{GITHUB_API}/repos/{full_name}/tags", params={"per_page": 1}
        )
        if data and isinstance(data, list) and len(data) > 0:
            return data[0].get("name")
    except Exception:
        pass

    return None


def _format_yaml_value(value: Any) -> str:
    """Format a value for YAML output, matching build_dataset/data/repos.yml style."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        if not value:
            return "null"
        lines = []
        for item in value:
            lines.append(f'    - "{item}"')
        return "\n" + "\n".join(lines)
    return str(value)


def generate_yaml_entry(repo: dict, full_name: str, index: int) -> dict:
    """Build the YAML entry dict for a repo."""
    root_contents = repo.get("_root_contents") or _get_repo_contents(full_name)

    src_dir = _detect_src_dir(full_name, root_contents)
    test_dir = _detect_test_dir(root_contents)
    test_cmd = _detect_test_cmd(full_name)
    python_version = _detect_python_version(full_name)
    install_cmd = _detect_install_cmd(full_name)
    docs_url = _detect_docs_url(repo, full_name)
    packages = _detect_packages(full_name, root_contents)

    commit_sha = _get_latest_commit(full_name)
    latest_tag = _get_latest_tag(full_name)

    if latest_tag:
        commit_val = None
        tag_val = latest_tag
    else:
        commit_val = commit_sha
        tag_val = None

    entry = {
        "name": full_name,
        "commit": commit_val,
        "tag": tag_val,
        "python": python_version,
        "install": install_cmd,
        "packages": packages,
        "pip_packages": None,
        "pre_install": None,
        "specification": docs_url,
        "test_cmd": test_cmd,
        "test_dir": test_dir,
        "src_dir": src_dir,
    }

    return entry


def write_yaml_entry(entry: dict, index: int) -> Path:
    """Write a single YAML entry to build_dataset/data/<repo_name>.yml."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    repo_name = entry["name"].replace("/", "_")
    filepath = OUTPUT_DIR / f"{repo_name}.yml"

    lines: list[str] = []
    lines.append(f"{index}:")
    for field in YAML_FIELD_ORDER:
        value = entry.get(field)
        formatted = _format_yaml_value(value)
        if "\n" in formatted:
            lines.append(f"  {field}:{formatted}")
        else:
            lines.append(f"  {field}: {formatted}")

    content = "\n".join(lines) + "\n"

    with open(filepath, "w") as f:
        f.write(content)

    log.info("  Wrote YAML: %s", filepath)
    return filepath


def _format_entry_block(entry: dict, index: int) -> str:
    """Format a single repo entry as a YAML block string for repos.yml."""
    lines: list[str] = []
    lines.append(f"{index}:")
    for field in YAML_FIELD_ORDER:
        value = entry.get(field)
        formatted = _format_yaml_value(value)
        if "\n" in formatted:
            lines.append(f"  {field}:{formatted}")
        else:
            lines.append(f"  {field}: {formatted}")
    return "\n".join(lines) + "\n"


def _append_to_repos_yml(entries: list[tuple[dict, int]]) -> None:
    """Append processed repo entries to build_dataset/data/repos.yml."""
    repos_yml = OUTPUT_DIR / "repos.yml"

    existing_content = ""
    if repos_yml.exists():
        with open(repos_yml) as f:
            existing_content = f.read()

    with open(repos_yml, "a") as f:
        for entry, index in entries:
            block = _format_entry_block(entry, index)
            if existing_content and not existing_content.endswith("\n\n"):
                f.write("\n")
            f.write(block)
            f.write("\n")

    log.info("Appended %d entries to %s", len(entries), repos_yml)


# ---------------------------------------------------------------------------
# STEP 9: State management
# ---------------------------------------------------------------------------


def _get_max_index_from_repos_yml() -> int:
    """Scan repos.yml for the highest entry index. Returns -1 if none found."""
    repos_yml = OUTPUT_DIR / "repos.yml"
    if not repos_yml.exists():
        return -1
    import re

    max_idx = -1
    with open(repos_yml) as f:
        for line in f:
            m = re.match(r"^(\d+):\s*$", line)
            if m:
                max_idx = max(max_idx, int(m.group(1)))
    return max_idx


def _load_processed() -> dict[str, Any]:
    """Load the processed repos state file.

    If processed_repos.json is missing or next_index is 0, fall back to
    scanning repos.yml so we never overwrite existing entries.
    """
    state: dict[str, Any] = {"repos": {}, "next_index": 0}
    if PROCESSED_REPOS_FILE.exists():
        with open(PROCESSED_REPOS_FILE) as f:
            state = json.load(f)

    # Reconcile with repos.yml — always use the higher value
    yml_max = _get_max_index_from_repos_yml()
    if yml_max >= 0:
        correct_next = yml_max + 1
        if state.get("next_index", 0) < correct_next:
            log.info(
                "Adjusting next_index %d → %d (repos.yml has entries up to %d)",
                state.get("next_index", 0),
                correct_next,
                yml_max,
            )
            state["next_index"] = correct_next

    return state


def _save_processed(state: dict[str, Any]) -> None:
    """Save the processed repos state file."""
    with open(PROCESSED_REPOS_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _is_processed(state: dict[str, Any], full_name: str) -> bool:
    """Check if a repo has already been processed."""
    return full_name in state.get("repos", {})


def _mark_processed(
    state: dict[str, Any], full_name: str, yaml_path: str, pdf_ok: bool
) -> None:
    """Mark a repo as processed."""
    state["repos"][full_name] = {
        "yaml_path": yaml_path,
        "pdf_generated": pdf_ok,
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    state["next_index"] = state.get("next_index", 0) + 1
    _save_processed(state)


# ---------------------------------------------------------------------------
# STEP 10: Main orchestrator
# ---------------------------------------------------------------------------


def _clone_single(full_name: str) -> tuple[str, bool, str]:
    """Clone a single repo. Returns (full_name, success, error_msg)."""
    try:
        clone_repo(full_name)
        return (full_name, True, "")
    except Exception as exc:
        return (full_name, False, str(exc))


def _yaml_single(
    repo: dict, full_name: str, index: int
) -> tuple[str, bool, Optional[dict], Optional[Path], str]:
    """Generate YAML for a single repo. Returns (full_name, success, entry, yaml_path, error)."""
    try:
        entry = generate_yaml_entry(repo, full_name, index)
        yaml_path = write_yaml_entry(entry, index)
        return (full_name, True, entry, yaml_path, "")
    except Exception as exc:
        return (full_name, False, None, None, str(exc))


def _pdf_single(full_name: str, yaml_path: Path, has_spec: bool) -> tuple[str, bool]:
    try:
        ok = generate_pdf(yaml_path)
        if ok:
            repo_name = full_name.replace("/", "_")
            pdf_path = BUILD_DATASET_DIR / "pdfs" / repo_name / f"{repo_name}.pdf"
            if not _validate_pdf(pdf_path):
                log.warning(
                    "  PDF validation failed for %s — marking as failed", full_name
                )
                return (full_name, False)
        return (full_name, ok)
    except Exception as exc:
        log.error("  PDF generation error for %s: %s", full_name, exc)
        return (full_name, False)


def _validate_pdf(pdf_path: Path) -> bool:
    if not pdf_path.exists():
        return False
    size = pdf_path.stat().st_size
    if size < 1000:
        log.warning("  PDF too small (%d bytes): %s", size, pdf_path)
        return False
    try:
        with open(pdf_path, "rb") as f:
            header = f.read(5)
            if header != b"%PDF-":
                log.warning("  Invalid PDF header: %s", pdf_path)
                return False
    except Exception:
        return False
    return True


def process_repos_parallel(
    repos: list[dict], state: dict[str, Any]
) -> tuple[list[tuple[dict, int]], list[dict[str, str]]]:
    """Process repos in 3 parallel phases: clone → YAML → PDF.

    Returns (yaml_entries, repo_results) matching the old sequential interface.
    """
    yaml_entries: list[tuple[dict, int]] = []
    repo_results: list[dict[str, str]] = []

    # --- Assign indices upfront (sequential — must be deterministic) -----------
    index_map: dict[str, int] = {}
    next_idx = state.get("next_index", 0)
    for repo in repos:
        fn = repo.get("full_name", "")
        index_map[fn] = next_idx
        next_idx += 1

    names = [r.get("full_name", "") for r in repos]

    # --- Phase 1: Parallel clone ------------------------------------------------
    log.info("=" * 60)
    log.info("PHASE 1 — Cloning %d repos (%d workers)", len(repos), CLONE_WORKERS)
    log.info("=" * 60)

    clone_ok: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CLONE_WORKERS) as pool:
        futures = {pool.submit(_clone_single, fn): fn for fn in names}
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            fn, ok, err = fut.result()
            done_count += 1
            if ok:
                clone_ok.add(fn)
                log.info("  [%d/%d] ✓ Cloned %s", done_count, len(repos), fn)
            else:
                log.error(
                    "  [%d/%d] ✗ Clone failed %s: %s", done_count, len(repos), fn, err
                )

    log.info("Clone phase: %d/%d successful", len(clone_ok), len(repos))

    # --- Phase 2: Parallel YAML generation --------------------------------------
    cloneable_repos = [r for r in repos if r.get("full_name", "") in clone_ok]

    log.info("=" * 60)
    log.info(
        "PHASE 2 — YAML generation for %d repos (%d workers)",
        len(cloneable_repos),
        YAML_WORKERS,
    )
    log.info("=" * 60)

    yaml_map: dict[str, tuple[dict, Path]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=YAML_WORKERS) as pool:
        futures = {
            pool.submit(
                _yaml_single,
                r,
                r.get("full_name", ""),
                index_map[r.get("full_name", "")],
            ): r.get("full_name", "")
            for r in cloneable_repos
        }
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            fn, ok, entry, ypath, err = fut.result()
            done_count += 1
            if ok and entry and ypath:
                yaml_map[fn] = (entry, ypath)
                log.info("  [%d/%d] ✓ YAML %s", done_count, len(cloneable_repos), fn)
            else:
                log.error(
                    "  [%d/%d] ✗ YAML failed %s: %s",
                    done_count,
                    len(cloneable_repos),
                    fn,
                    err,
                )

    log.info("YAML phase: %d/%d successful", len(yaml_map), len(cloneable_repos))

    # --- Phase 3: Parallel PDF generation ---------------------------------------
    pdf_tasks = [
        (fn, ypath, bool(entry.get("specification")))
        for fn, (entry, ypath) in yaml_map.items()
    ]

    log.info("=" * 60)
    log.info(
        "PHASE 3 — PDF generation for %d repos (%d workers)",
        len(pdf_tasks),
        PDF_WORKERS,
    )
    log.info("=" * 60)

    pdf_ok_set: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=PDF_WORKERS) as pool:
        futures = {
            pool.submit(_pdf_single, fn, ypath, has_spec): fn
            for fn, ypath, has_spec in pdf_tasks
        }
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            fn, ok = fut.result()
            done_count += 1
            if ok:
                pdf_ok_set.add(fn)
                log.info("  [%d/%d] ✓ PDF %s", done_count, len(pdf_tasks), fn)
            else:
                log.info("  [%d/%d] ⏭️  PDF skipped %s", done_count, len(pdf_tasks), fn)

    log.info("PDF phase: %d/%d generated", len(pdf_ok_set), len(pdf_tasks))

    _sanitize_pdf_folders({fn.replace("/", "_") for fn in pdf_ok_set})

    # --- Phase 4: Cleanup cloned repos to free disk space -----------------------
    log.info("=" * 60)
    log.info("PHASE 4 — Cleaning up cloned repos to free disk space")
    log.info("=" * 60)
    cleaned = 0
    for fn in names:
        repo_dir = CLONED_REPOS_DIR / fn.replace("/", "_")
        if repo_dir.exists():
            try:
                shutil.rmtree(repo_dir)
                cleaned += 1
            except Exception as exc:
                log.warning("  Could not delete %s: %s", repo_dir, exc)
    log.info("Cleanup: removed %d cloned repos", cleaned)

    # --- Collect results & update state -----------------------------------------
    for repo in repos:
        fn = repo.get("full_name", "")
        if fn in yaml_map:
            entry, ypath = yaml_map[fn]
            pdf_ok = fn in pdf_ok_set
            _mark_processed(state, fn, str(ypath), pdf_ok)
            yaml_entries.append((entry, index_map[fn]))
            repo_results.append(
                {
                    "name": fn,
                    "clone": "✅",
                    "yaml": "✅",
                    "pdf": "✅" if pdf_ok else "❌",
                    "repos_yml": "✅",
                }
            )
        elif fn in clone_ok:
            repo_results.append(
                {
                    "name": fn,
                    "clone": "✅",
                    "yaml": "❌",
                    "pdf": "❌",
                    "repos_yml": "❌",
                }
            )
        else:
            repo_results.append(
                {
                    "name": fn,
                    "clone": "❌",
                    "yaml": "❌",
                    "pdf": "❌",
                    "repos_yml": "❌",
                }
            )

    return yaml_entries, repo_results


# ---------------------------------------------------------------------------
# STEP 8 — Google Drive upload
# ---------------------------------------------------------------------------


def _get_drive_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build as build_service

    creds = None

    if DRIVE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(DRIVE_TOKEN_FILE), DRIVE_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired Drive token ...")
            creds.refresh(GoogleAuthRequest())
        else:
            if not DRIVE_CREDS_FILE.exists():
                raise FileNotFoundError(
                    f"OAuth2 credentials file not found at {DRIVE_CREDS_FILE.resolve()}. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials → "
                    "OAuth 2.0 Client IDs → Download JSON → save as credentials.json"
                )
            log.info("Opening browser for Google Drive authorization ...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(DRIVE_CREDS_FILE), DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(DRIVE_TOKEN_FILE, "w", encoding="utf-8") as tok:
            tok.write(creds.to_json())
        log.info("Drive token saved to %s", DRIVE_TOKEN_FILE)

    return build_service("drive", "v3", credentials=creds)


def _find_drive_folder(
    service, name: str, parent_id: Optional[str] = None
) -> Optional[str]:
    q = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and name='{name}' and trashed=false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"
    resp = (
        service.files()
        .list(q=q, spaces="drive", fields="files(id)", pageSize=1)
        .execute()
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_drive_folder(service, name: str, parent_id: Optional[str] = None) -> str:
    meta: dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        meta["parents"] = [parent_id]
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def _get_or_create_drive_folder(
    service, name: str, parent_id: Optional[str] = None
) -> str:
    existing = _find_drive_folder(service, name, parent_id)
    if existing:
        log.info("  Drive folder '%s' already exists (id=%s)", name, existing)
        return existing
    fid = _create_drive_folder(service, name, parent_id)
    log.info("  Created Drive folder '%s' (id=%s)", name, fid)
    return fid


def _find_drive_file(service, name: str, parent_id: str) -> Optional[str]:
    """Check if a file with the given name already exists in a Drive folder.
    Returns the file ID if found, None otherwise."""
    q = (
        f"name='{name}' and '{parent_id}' in parents "
        f"and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    )
    resp = (
        service.files()
        .list(q=q, spaces="drive", fields="files(id)", pageSize=1)
        .execute()
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _upload_file_to_drive(
    service,
    local_path: Path,
    parent_id: str,
    max_retries: int = 3,
    overwrite: bool = False,
) -> dict:
    from googleapiclient.http import MediaFileUpload

    import mimetypes as _mt

    mime, _ = _mt.guess_type(str(local_path))
    mime = mime or "application/octet-stream"

    existing_id = _find_drive_file(service, local_path.name, parent_id)
    if existing_id and not overwrite:
        log.info(
            "    Skipping %s — already exists on Drive (id=%s)",
            local_path.name,
            existing_id,
        )
        return {"id": existing_id, "name": local_path.name, "skipped": True}

    if existing_id and overwrite:
        try:
            service.files().delete(fileId=existing_id).execute()
            log.info(
                "    Deleted old Drive file %s (id=%s) for overwrite",
                local_path.name,
                existing_id,
            )
        except Exception as exc:
            log.warning(
                "    Could not delete old Drive file %s: %s", local_path.name, exc
            )

    for attempt in range(1, max_retries + 1):
        try:
            media = MediaFileUpload(
                str(local_path),
                mimetype=mime,
                resumable=True,
                chunksize=5 * 1024 * 1024,
            )
            meta = {"name": local_path.name, "parents": [parent_id]}
            request = service.files().create(
                body=meta, media_body=media, fields="id, name, webViewLink"
            )
            uploaded = None
            while uploaded is None:
                status, uploaded = request.next_chunk(num_retries=3)
                if status:
                    log.debug(
                        "    %s upload progress: %.0f%%",
                        local_path.name,
                        status.progress() * 100,
                    )
            log.info(
                "    Uploaded %s (%.1f KB) → %s",
                local_path.name,
                local_path.stat().st_size / 1024,
                uploaded.get("webViewLink", uploaded["id"]),
            )
            return uploaded
        except Exception as exc:
            if attempt < max_retries:
                wait = 5 * attempt
                log.warning(
                    "    Upload attempt %d/%d failed for %s: %s — retrying in %ds",
                    attempt,
                    max_retries,
                    local_path.name,
                    exc,
                    wait,
                )
                time.sleep(wait)
            else:
                log.error(
                    "    Upload FAILED after %d attempts for %s: %s",
                    max_retries,
                    local_path.name,
                    exc,
                )
                raise


def upload_to_drive() -> dict[str, Any]:
    log.info("=" * 60)
    log.info("STEP 8 — Uploading to Google Drive")
    log.info("=" * 60)

    service = _get_drive_service()

    parent_id = _get_or_create_drive_folder(service, DRIVE_PARENT_FOLDER_NAME)

    from datetime import datetime

    date_folder_name = datetime.now().strftime("%d %B %Y")
    date_folder_id = _get_or_create_drive_folder(service, date_folder_name, parent_id)

    raw_folder_id = _get_or_create_drive_folder(
        service, DRIVE_RAW_SUBFOLDER, date_folder_id
    )
    pdf_folder_id = _get_or_create_drive_folder(
        service, DRIVE_PDF_SUBFOLDER, date_folder_id
    )

    jsonl_ok = False
    if RAW_REPOS_JSONL_FILE.exists():
        _upload_file_to_drive(
            service, RAW_REPOS_JSONL_FILE, raw_folder_id, overwrite=True
        )
        jsonl_ok = True
    else:
        log.warning("No raw_repos.jsonl found — skipping Raw Data upload.")

    repos_yml_path = OUTPUT_DIR / "repos.yml"
    repos_yml_ok = False
    if repos_yml_path.exists():
        _upload_file_to_drive(service, repos_yml_path, raw_folder_id, overwrite=True)
        repos_yml_ok = True
    else:
        log.warning("No repos.yml found — skipping repos.yml upload.")

    pdf_count = 0
    bz2_count = 0
    pdfs_root = BUILD_DATASET_DIR / "pdfs"
    if pdfs_root.is_dir():
        upload_tasks: list[tuple[Path, str]] = []
        for repo_dir in sorted(pdfs_root.iterdir()):
            if not repo_dir.is_dir():
                continue
            repo_drive_folder = _get_or_create_drive_folder(
                service, repo_dir.name, pdf_folder_id
            )
            for f in sorted(repo_dir.iterdir()):
                if f.is_file():
                    upload_tasks.append((f, repo_drive_folder))

        if upload_tasks:
            log.info(
                "Uploading %d files with %d workers ...",
                len(upload_tasks),
                UPLOAD_WORKERS,
            )

            import threading

            _thread_local = threading.local()

            def _thread_drive_service():
                if not hasattr(_thread_local, "service"):
                    _thread_local.service = _get_drive_service()
                return _thread_local.service

            def _do_upload(task: tuple[Path, str]) -> str | None:
                fpath, parent = task
                try:
                    svc = _thread_drive_service()
                    _upload_file_to_drive(svc, fpath, parent)
                    return fpath.suffix
                except Exception as exc:
                    log.error(
                        "    Skipping %s due to upload failure: %s", fpath.name, exc
                    )
                    return None

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=UPLOAD_WORKERS
            ) as pool:
                for suffix in pool.map(_do_upload, upload_tasks):
                    if suffix == ".bz2":
                        bz2_count += 1
                    elif suffix == ".pdf":
                        pdf_count += 1
    else:
        log.warning("No PDFs directory found at %s — skipping PDF upload.", pdfs_root)

    _log_step(
        f"Drive upload complete — {pdf_count} PDFs, {bz2_count} BZ2s, JSONL: {'yes' if jsonl_ok else 'no'}"
    )

    return {"pdfs": pdf_count, "bz2s": bz2_count, "jsonl": jsonl_ok}


def _clear_intermediate_caches() -> None:
    for cache_file in (FILTERED_STAGE1_FILE, FINAL_SELECTED_FILE):
        if cache_file.exists():
            cache_file.unlink()
            log.info("  Deleted cache: %s", cache_file)


def _clean_previous_run_artifacts() -> None:
    """Remove stale artifacts from previous runs so they don't leak into the
    current run.  Called once at the very start of main()."""
    pdfs_dir = BUILD_DATASET_DIR / "pdfs"
    if pdfs_dir.exists():
        shutil.rmtree(pdfs_dir)
        log.info("Cleaned previous run's pdfs directory: %s", pdfs_dir)

    if CLONED_REPOS_DIR.exists():
        shutil.rmtree(CLONED_REPOS_DIR)
        log.info("Cleaned previous run's cloned_repos directory: %s", CLONED_REPOS_DIR)

    _clear_intermediate_caches()


def _sanitize_pdf_folders(pdf_ok_set: set[str]) -> None:
    """Enforce exactly 1 .pdf + 1 .bz2 per repo folder inside build_dataset/pdfs/.
    Removes any extra files. Removes folders for repos not in pdf_ok_set."""
    pdfs_root = BUILD_DATASET_DIR / "pdfs"
    if not pdfs_root.is_dir():
        return

    for repo_dir in sorted(pdfs_root.iterdir()):
        if not repo_dir.is_dir():
            continue

        if repo_dir.name not in pdf_ok_set:
            shutil.rmtree(repo_dir)
            log.info(
                "  Removed PDF folder for repo not in current run: %s", repo_dir.name
            )
            continue

        pdf_files = sorted(repo_dir.glob("*.pdf"))
        bz2_files = sorted(repo_dir.glob("*.bz2"))
        other_files = [
            f
            for f in repo_dir.iterdir()
            if f.is_file()
            and not f.name.endswith(".pdf")
            and not f.name.endswith(".bz2")
        ]

        for f in other_files:
            f.unlink()
            log.info("  Removed unexpected file: %s", f)

        if len(pdf_files) > 1:
            for f in pdf_files[1:]:
                f.unlink()
                log.info("  Removed extra PDF: %s", f)

        if len(bz2_files) > 1:
            for f in bz2_files[1:]:
                f.unlink()
                log.info("  Removed extra BZ2: %s", f)

        remaining = list(repo_dir.iterdir())
        if len(remaining) != 2:
            log.warning(
                "  Repo %s has %d files (expected 2): %s",
                repo_dir.name,
                len(remaining),
                [f.name for f in remaining],
            )


def _print_banner() -> None:
    """Print a cool ASCII art banner on startup."""
    banner = r"""
   ██████╗ ██████╗ ███╗   ███╗███╗   ███╗██╗████████╗ ██████╗
  ██╔════╝██╔═══██╗████╗ ████║████╗ ████║██║╚══██╔══╝██╔═████╗
  ██║     ██║   ██║██╔████╔██║██╔████╔██║██║   ██║   ██║██╔██║
  ██║     ██║   ██║██║╚██╔╝██║██║╚██╔╝██║██║   ██║   ████╔╝██║
  ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║ ╚═╝ ██║██║   ██║   ╚██████╔╝
   ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═╝╚═╝   ╚═╝    ╚═════╝
    """
    print("\033[1;36m" + banner + "\033[0m")  # ANSI bold cyan
    print("\033[1;33m  ⚡ Automated Repository Pipeline\033[0m")
    print(
        "\033[0;90m  ──────────────────────────────────────────────────────────────────\033[0m\n"
    )


def main() -> None:
    _bootstrap()
    _print_banner()
    log.info("Working directory: %s", os.getcwd())
    log.info(
        "GitHub token: %s",
        "SET" if os.environ.get("GITHUB_TOKEN") else "NOT SET (limited to 60 req/hr!)",
    )

    _clean_previous_run_artifacts()

    import requests  # noqa: F811
    import yaml  # noqa: F811

    all_accepted: list[dict] = []
    pre_state = _load_processed()
    excluded_names: set[str] = set(pre_state.get("repos", {}).keys())

    if RAW_REPOS_JSONL_FILE.exists():
        with open(RAW_REPOS_JSONL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        repo = json.loads(line)
                        name = repo.get("full_name", "")
                        if name:
                            excluded_names.add(name)
                    except json.JSONDecodeError:
                        pass

    if excluded_names:
        log.info(
            "Excluding %d previously seen repos from discovery.",
            len(excluded_names),
        )
    round_num = 0

    for round_num in range(1, MAX_RETRY_ROUNDS + 1):
        log.info("=" * 60)
        log.info(
            "DISCOVERY ROUND %d / %d  (accepted so far: %d, need: %d)",
            round_num,
            MAX_RETRY_ROUNDS,
            len(all_accepted),
            MIN_FILTER2_PASS,
        )
        log.info("=" * 60)

        raw_repos = fetch_repos(exclude_names=excluded_names)
        _log_table(
            ["Step", "Result", "Status"],
            [
                [
                    "Discovery",
                    f"{len(raw_repos)} repos found",
                    "✅" if raw_repos else "❌",
                ]
            ],
        )

        if not raw_repos:
            log.warning(
                "No new repos found in round %d — all star ranges exhausted.", round_num
            )
            break

        stage1_repos = filter1(raw_repos)
        _log_table(
            ["Step", "Input", "Passed", "Status"],
            [["Filter 1", str(len(raw_repos)), str(len(stage1_repos)), "✅"]],
        )

        final_repos = filter2(stage1_repos)
        _log_table(
            ["Step", "Input", "Passed", "Status"],
            [["Filter 2", str(len(stage1_repos)), str(len(final_repos)), "✅"]],
        )

        all_accepted.extend(final_repos)

        for repo in raw_repos:
            excluded_names.add(repo.get("full_name", ""))

        log.info(
            "Cumulative accepted: %d / %d required", len(all_accepted), MIN_FILTER2_PASS
        )

        if len(all_accepted) >= MIN_FILTER2_PASS:
            log.info(
                "✓ Reached minimum of %d accepted repos. Proceeding to processing.",
                MIN_FILTER2_PASS,
            )
            break

        log.warning(
            "Only %d/%d repos accepted so far. Starting new discovery round ...",
            len(all_accepted),
            MIN_FILTER2_PASS,
        )
        _clear_intermediate_caches()
        time.sleep(2)
    else:
        log.error(
            "Exhausted %d retry rounds. Only %d/%d repos accepted.",
            MAX_RETRY_ROUNDS,
            len(all_accepted),
            MIN_FILTER2_PASS,
        )

    with open(FINAL_SELECTED_FILE, "w") as f:
        saveable = [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in all_accepted
        ]
        json.dump(saveable, f, indent=1)
    log.info(
        "Saved %d final accepted repos to %s", len(all_accepted), FINAL_SELECTED_FILE
    )

    if not all_accepted:
        log.warning("No repos passed both filters after all rounds. Exiting.")
        return

    setup_build_dataset()
    _log_step("Build dataset setup complete")

    state = _load_processed()
    log.info("Previously processed: %d repos", len(state.get("repos", {})))

    skipped_repos: list[dict[str, str]] = []
    new_repos: list[dict] = []
    for repo in all_accepted:
        fn = repo.get("full_name", "")
        if _is_processed(state, fn):
            log.info("  Skipping (already processed): %s", fn)
            repo_info = state.get("repos", {}).get(fn, {})
            skipped_repos.append(
                {
                    "name": fn,
                    "clone": "✅",
                    "yaml": "✅",
                    "pdf": "✅" if repo_info.get("pdf_generated") else "⏭️",
                    "repos_yml": "✅",
                }
            )
        else:
            new_repos.append(repo)

    yaml_entries: list[tuple[dict, int]] = []
    new_results: list[dict[str, str]] = []

    if new_repos:
        yaml_entries, new_results = process_repos_parallel(new_repos, state)
    else:
        log.info("All repos already processed — nothing new to do.")

    repo_results = skipped_repos + new_results
    processed_count = sum(1 for r in new_results if r["yaml"] == "✅")
    failed_count = sum(1 for r in new_results if r["yaml"] == "❌")
    skipped_count = len(skipped_repos)

    if yaml_entries:
        _append_to_repos_yml(yaml_entries)
        _log_step(f"Appended {len(yaml_entries)} entries to repos.yml")

    log.info("")
    log.info("=" * 60)
    log.info("PIPELINE STEPS")
    log.info("=" * 60)
    _log_table(
        ["Step", "Result", "Status"],
        [
            ["Discovery", f"{len(all_accepted)} repos accepted", "✅"],
            [
                "Clone + YAML + PDF",
                f"{processed_count} processed, {skipped_count} skipped, {failed_count} failed",
                "✅" if failed_count == 0 else "⚠️",
            ],
            [
                "repos.yml append",
                f"{len(yaml_entries)} entries added",
                "✅" if yaml_entries else "⏭️",
            ],
        ],
    )

    drive_uploaded_pdfs = 0
    drive_uploaded_bz2 = 0
    drive_jsonl_ok = False
    try:
        upload_result = upload_to_drive()
        if upload_result:
            drive_uploaded_pdfs = upload_result.get("pdfs", 0)
            drive_uploaded_bz2 = upload_result.get("bz2s", 0)
            drive_jsonl_ok = upload_result.get("jsonl", False)
    except FileNotFoundError as exc:
        log.warning("Drive upload skipped: %s", exc)
    except Exception as exc:
        log.error("Drive upload failed: %s", exc, exc_info=True)

    for r in repo_results:
        repo_pdf_dir = BUILD_DATASET_DIR / "pdfs" / r["name"].replace("/", "_")
        bz2_path = repo_pdf_dir / (r["name"].replace("/", "_") + ".pdf.bz2")
        r["bz2"] = "✅" if bz2_path.exists() else "❌"
        r["drive"] = "✅" if (drive_uploaded_pdfs + drive_uploaded_bz2) > 0 else "❌"

    log.info("")
    log.info("=" * 60)
    log.info("FINAL SUMMARY")
    log.info("=" * 60)

    _log_table(
        ["Metric", "Count"],
        [
            ["Total repos accepted", str(len(all_accepted))],
            ["Cloned", str(sum(1 for r in repo_results if r["clone"] == "✅"))],
            ["YAMLs generated", str(sum(1 for r in repo_results if r["yaml"] == "✅"))],
            ["PDFs generated", str(sum(1 for r in repo_results if r["pdf"] == "✅"))],
            ["BZ2 compressed", str(sum(1 for r in repo_results if r["bz2"] == "✅"))],
            [
                "Added to repos.yml",
                str(sum(1 for r in repo_results if r["repos_yml"] == "✅")),
            ],
            ["Drive PDFs uploaded", str(drive_uploaded_pdfs)],
            ["Drive BZ2s uploaded", str(drive_uploaded_bz2)],
            ["Drive JSONL uploaded", "✅" if drive_jsonl_ok else "❌"],
        ],
    )

    log.info("")
    log.info("=" * 60)
    log.info("PER-REPO STATUS")
    log.info("=" * 60)
    _log_table(
        ["Repo", "Clone", "YAML", "PDF", "BZ2", "repos.yml", "Drive"],
        [
            [
                r["name"],
                r["clone"],
                r["yaml"],
                r["pdf"],
                r["bz2"],
                r["repos_yml"],
                r["drive"],
            ]
            for r in repo_results
        ],
    )

    log.info("")
    log.info("🥂🥂🥂  Script completed successfully!  🥂🥂🥂")
    log.info("")


if __name__ == "__main__":
    try:
        main()
    finally:
        if CLONED_REPOS_DIR.exists():
            try:
                shutil.rmtree(CLONED_REPOS_DIR)
                log.info("Cleanup: removed cloned_repos directory")
            except Exception as exc:
                log.warning("Could not remove cloned_repos: %s", exc)
