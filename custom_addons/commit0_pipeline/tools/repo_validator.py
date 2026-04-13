"""
GitHub API-based repository validation for Commit0 benchmark.

Ports the Filter 1 (initial quality) + Filter 2 (benchmark suitability)
checks from commit0_automation.py into a reusable module.

All checks use the GitHub REST API only — no cloning required.

Usage (standalone):
    python -m tools.repo_validator pallets/flask
    python -m tools.repo_validator arrow-py/arrow --token ghp_xxx

Usage (from Odoo):
    from tools.repo_validator import validate_repo
    result = validate_repo("arrow-py/arrow", github_token="ghp_xxx")
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import threading
import time
from typing import Any, Optional

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (exact values from commit0_automation.py)
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"

PRIMARY_STAR_THRESHOLD = 2000
FALLBACK_STAR_THRESHOLD = 2000

MIN_PYTHON_RATIO = 0.80
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

# ---------------------------------------------------------------------------
# HTTP helpers (ported from commit0_automation.py with token-as-parameter)
# ---------------------------------------------------------------------------

_http_session = None
_http_lock = threading.Lock()

# Module-level token — set by validate_repo() before running checks
_current_token: str = ""


def _get_session():
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
    """Build headers for GitHub API requests using the current token."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if _current_token:
        headers["Authorization"] = "token %s" % _current_token
    return headers


def _rate_limit_wait(response) -> None:
    """Sleep until rate limit resets if we hit 403."""
    reset_ts = response.headers.get("X-RateLimit-Reset")
    if reset_ts:
        wait = max(int(reset_ts) - int(time.time()), 1) + 5
        _logger.warning("Rate limited. Sleeping %d seconds until reset ...", wait)
        time.sleep(wait)
    else:
        _logger.warning("Rate limited (no reset header). Sleeping 60s ...")
        time.sleep(60)


def github_get(url: str, params: Optional[dict] = None, max_retries: int = 3):
    """GET with retry, rate-limit handling, and auth fallback."""
    session = _get_session()
    headers = _github_headers()
    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=headers, params=params, timeout=30)
        except Exception as exc:
            _logger.warning(
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
            _logger.warning("401 with token — retrying without auth ...")
            headers.pop("Authorization", None)
            continue
        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            if remaining == "0" or "rate limit" in resp.text.lower():
                _rate_limit_wait(resp)
                continue
        if resp.status_code == 422:
            _logger.warning("422 Unprocessable: %s", resp.text[:200])
            return resp
        if resp.status_code >= 500:
            _logger.warning(
                "Server error %d (attempt %d/%d)",
                resp.status_code,
                attempt + 1,
                max_retries,
            )
            time.sleep(2**attempt)
            continue

        _logger.warning("Unexpected status %d for %s", resp.status_code, url)
        return resp

    raise RuntimeError("Failed after %d retries: %s" % (max_retries, url))


def github_get_json(url: str, params: Optional[dict] = None) -> Any:
    """GET + parse JSON."""
    resp = github_get(url, params)
    resp.raise_for_status()
    return resp.json()


def _get_languages(full_name: str) -> dict[str, int]:
    """Get language breakdown (bytes) for a repo."""
    return github_get_json("%s/repos/%s/languages" % (GITHUB_API, full_name))


def _get_repo_contents(full_name: str, path: str = "") -> list[dict]:
    """List directory contents via GitHub API."""
    try:
        return github_get_json(
            "%s/repos/%s/contents/%s" % (GITHUB_API, full_name, path)
        )
    except Exception:
        return []


def _get_file_content(full_name: str, path: str, ref: str = "HEAD") -> Optional[str]:
    """Download raw file content from raw.githubusercontent.com."""
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (full_name, ref, path)
    try:
        resp = _get_session().get(url, timeout=15)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception:
        return None


def _file_exists(full_name: str, path: str) -> bool:
    """Check if a file/dir exists in a repo (HEAD request)."""
    url = "%s/repos/%s/contents/%s" % (GITHUB_API, full_name, path)
    try:
        resp = _get_session().head(url, headers=_github_headers(), timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Filter 1 checks — initial quality (ALL must pass)
# ---------------------------------------------------------------------------


def _check_not_fork(repo: dict) -> tuple[bool, str]:
    """Reject forked repositories."""
    if repo.get("fork"):
        return False, "Is a fork"
    return True, ""


def _check_not_archived(repo: dict) -> tuple[bool, str]:
    """Reject archived repositories."""
    if repo.get("archived"):
        return False, "Is archived"
    return True, ""


def _check_stars(
    repo: dict, threshold: int = PRIMARY_STAR_THRESHOLD
) -> tuple[bool, str]:
    """Check minimum star count."""
    stars = repo.get("stargazers_count", 0)
    if stars < threshold:
        return False, "Stars %d < %d" % (stars, threshold)
    return True, ""


def _check_repo_size(repo: dict) -> tuple[bool, str]:
    """Reject oversized repositories (>500MB)."""
    size_kb = repo.get("size", 0)
    if size_kb > MAX_REPO_SIZE_KB:
        return False, "Size %dKB > %dKB" % (size_kb, MAX_REPO_SIZE_KB)
    return True, ""


def _check_not_ml_framework(repo: dict) -> tuple[bool, str]:
    """Reject ML/DL frameworks."""
    topics = [t.lower() for t in repo.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    name = repo.get("full_name", "").lower()
    combined = " ".join(topics) + " " + desc + " " + name

    for kw in ML_FRAMEWORK_KEYWORDS:
        if kw in combined:
            return False, "ML framework keyword: %s" % kw
    return True, ""


def _check_not_cli_tool(repo: dict) -> tuple[bool, str]:
    """Reject CLI tools."""
    topics = [t.lower() for t in repo.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    combined = " ".join(topics) + " " + desc

    for kw in CLI_TOOL_KEYWORDS:
        if kw in combined:
            return False, "CLI tool keyword: %s" % kw
    return True, ""


def _check_not_native_wrapper(repo: dict) -> tuple[bool, str]:
    """Reject native C/C++/Rust wrappers."""
    topics = [t.lower() for t in repo.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    combined = " ".join(topics) + " " + desc

    for kw in NATIVE_WRAPPER_KEYWORDS:
        if kw in combined:
            return False, "Native wrapper keyword: %s" % kw
    return True, ""


def _check_python_ratio(full_name: str) -> tuple[bool, str]:
    """Check >=80% Python by bytes."""
    try:
        langs = _get_languages(full_name)
    except Exception as exc:
        return False, "Failed to get languages: %s" % exc

    if not langs:
        return False, "No languages detected"

    total = sum(langs.values())
    if total == 0:
        return False, "Zero total bytes"

    python_bytes = langs.get("Python", 0)
    ratio = python_bytes / total
    if ratio < MIN_PYTHON_RATIO:
        return False, "Python ratio %.2f%% < %.0f%%" % (
            ratio * 100,
            MIN_PYTHON_RATIO * 100,
        )
    return True, "Python %.1f%%" % (ratio * 100)


def _check_no_native_extensions(
    full_name: str, root_contents: Optional[list[dict]] = None
) -> tuple[bool, str]:
    """Check for C/C++/Cython extensions in setup.py, setup.cfg, and top-level files."""
    setup_py = _get_file_content(full_name, "setup.py")
    if setup_py:
        for pat in NATIVE_EXTENSION_PATTERNS:
            if pat.search(setup_py):
                return False, "Native extension in setup.py: %s" % pat.pattern
        for pat in BINDING_IMPORT_PATTERNS:
            if pat.search(setup_py):
                return False, "Binding import in setup.py: %s" % pat.pattern

    setup_cfg = _get_file_content(full_name, "setup.cfg")
    if setup_cfg:
        for pat in NATIVE_EXTENSION_PATTERNS:
            if pat.search(setup_cfg):
                return False, "Native extension in setup.cfg: %s" % pat.pattern

    if root_contents is None:
        root_contents = _get_repo_contents(full_name)
    for item in root_contents:
        name = item.get("name", "")
        if name.endswith((".c", ".cpp", ".pyx", ".so", ".pyd")):
            return False, "Native file in root: %s" % name

    return True, ""


def _check_docs_website(repo: dict, full_name: str) -> tuple[bool, str]:
    """Check if repo has a documentation website."""
    session = _get_session()
    homepage = repo.get("homepage", "") or ""

    if homepage:
        lhp = homepage.lower()
        is_non_docs = any(d in lhp for d in NON_DOCS_DOMAINS)
        if not is_non_docs and lhp.startswith("http"):
            return True, "Homepage: %s" % homepage

    repo_name = full_name.split("/")[-1]
    rtd_url = "https://%s.readthedocs.io" % repo_name
    try:
        resp = session.head(rtd_url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return True, "ReadTheDocs: %s" % rtd_url
    except Exception:
        pass

    org = full_name.split("/")[0]
    ghio_url = "https://%s.github.io/%s" % (org, repo_name)
    try:
        resp = session.head(ghio_url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return True, "GitHub Pages: %s" % ghio_url
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

    # Must have build config
    has_build_config = bool(names & {"pyproject.toml", "setup.py", "setup.cfg"})
    if not has_build_config:
        return False, "No pyproject.toml/setup.py/setup.cfg"

    # Must have tests
    has_tests = bool(names & {"tests", "test"})
    if not has_tests:
        for n in names:
            if types.get(n) == "dir" and n.startswith("test"):
                has_tests = True
                break

    if not has_tests:
        return False, "No tests/ or test/ directory"

    # Must have source package
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
                if _file_exists(full_name, "%s/__init__.py" % n):
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
                return False, "Syntax error in %s: %s" % (filepath, exc)
    return True, ""


# ---------------------------------------------------------------------------
# Filter 2 checks — Commit0 Benchmark validation
# ---------------------------------------------------------------------------


def _check_uses_pytest(full_name: str) -> tuple[bool, str]:
    """Check if the repo uses pytest as its test framework."""
    for cfg_file in ("conftest.py", "pytest.ini", ".pytest.ini"):
        if _file_exists(full_name, cfg_file):
            return True, "Found %s" % cfg_file

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
            return True, "pytest in %s" % req_file

    return False, "No pytest configuration found"


def _check_no_gpu_usage(full_name: str) -> tuple[bool, str]:
    """Check if repo requires GPU (CUDA, GPU keywords in config/requirements)."""
    for filepath in ("requirements.txt", "setup.py", "pyproject.toml", "setup.cfg"):
        content = _get_file_content(full_name, filepath)
        if content:
            lower = content.lower()
            if any(kw in lower for kw in ["cuda", "cupy", "nvidia", "gpu-required"]):
                return False, "GPU keyword in %s" % filepath
    return True, ""


def _check_installable(full_name: str) -> tuple[bool, str]:
    """Check if pip install -e . would likely work."""
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
        return False, "Too many dependencies: %d" % len(deps)
    return True, "%d dependencies" % len(deps)


def _check_python_version_compat(full_name: str) -> tuple[bool, str]:
    """Check if repo supports Python 3.10+."""
    content = _get_file_content(full_name, "pyproject.toml")
    if content:
        match = re.search(r'requires-python\s*=\s*"([^"]*)"', content)
        if match:
            spec = match.group(1)
            if re.search(r"<=?\s*3\.[0-9]\b", spec):
                return False, "Python version spec too old: %s" % spec

    content = _get_file_content(full_name, "setup.py")
    if content:
        match = re.search(r'python_requires\s*=\s*["\']([^"\']*)', content)
        if match:
            spec = match.group(1)
            if re.search(r"<=?\s*3\.[0-9]\b", spec):
                return False, "Python version spec too old: %s" % spec

    return True, ""


def _check_test_isolation(full_name: str) -> tuple[bool, str]:
    """Heuristic check for test isolation — external service deps in conftest."""
    content = _get_file_content(full_name, "conftest.py")
    if content:
        lower = content.lower()
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


# ---------------------------------------------------------------------------
# Main validator
# ---------------------------------------------------------------------------


def run_filter1(repo: dict, full_name: str) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Run all Filter 1 checks. Returns (all_passed, checks_list)."""
    checks: list[tuple[str, bool, str]] = []

    # Quick checks from repo dict (no extra API calls)
    for label, check_fn in [
        ("Not a fork", _check_not_fork),
        ("Not ML framework", _check_not_ml_framework),
        ("Not CLI tool", _check_not_cli_tool),
        ("Not native wrapper", _check_not_native_wrapper),
        ("Repository size", _check_repo_size),
    ]:
        ok, reason = check_fn(repo)
        checks.append((label, ok, reason))

    # Stars check (fallback threshold for filter1)
    ok, reason = _check_stars(repo, threshold=FALLBACK_STAR_THRESHOLD)
    checks.append(("Stars >= %d" % FALLBACK_STAR_THRESHOLD, ok, reason))

    # API-based checks
    ok, reason = _check_python_ratio(full_name)
    checks.append(("Python >= 80%%", ok, reason))

    root_contents = _get_repo_contents(full_name)

    ok, reason = _check_no_native_extensions(full_name, root_contents)
    checks.append(("No native extensions", ok, reason))

    ok, reason = _check_docs_website(repo, full_name)
    checks.append(("Has documentation", ok, reason))

    ok, reason = _check_project_structure(full_name, root_contents)
    checks.append(("Project structure", ok, reason))

    ok, reason = _check_code_quality_basic(full_name)
    checks.append(("Code quality (basic)", ok, reason))

    # "Not archived" is informational only — does not block pass/fail
    ok, reason = _check_not_archived(repo)
    checks.append(("[INFO] Not archived", ok, reason))

    all_passed = all(
        passed for label, passed, _ in checks if not label.startswith("[INFO]")
    )
    return all_passed, checks


def run_filter2(
    repo: dict, full_name: str, root_contents: Optional[list[dict]] = None
) -> tuple[bool, int, list[tuple[str, bool, str]]]:
    """Run all Filter 2 checks. Returns (passed, should_score, checks_list)."""
    checks: list[tuple[str, bool, str]] = []

    # MUST checks
    ok, reason = _check_uses_pytest(full_name)
    checks.append(("[MUST] Uses pytest", ok, reason))
    must_pass = ok

    ok, reason = _check_no_gpu_usage(full_name)
    checks.append(("[MUST] No GPU usage", ok, reason))
    must_pass = must_pass and ok

    # SHOULD checks (scoring — need >= 2 of 4)
    should_score = 0

    ok, reason = _check_installable(full_name)
    checks.append(("[SHOULD] Installable", ok, reason))
    if ok:
        should_score += 1

    ok, reason = _check_stars(repo, threshold=PRIMARY_STAR_THRESHOLD)
    checks.append(("[SHOULD] Stars >= %d" % PRIMARY_STAR_THRESHOLD, ok, reason))
    if ok:
        should_score += 1

    if root_contents is None:
        root_contents = _get_repo_contents(full_name)
    ok, reason = _check_project_structure(full_name, root_contents)
    checks.append(("[SHOULD] Project structure", ok, reason))
    if ok:
        should_score += 1

    ok, reason = _check_python_version_compat(full_name)
    checks.append(("[SHOULD] Python 3.10+ compatible", ok, reason))
    if ok:
        should_score += 1

    # Informational checks (don't affect pass/fail)
    ok, reason = _check_dependency_count(full_name)
    checks.append(("[INFO] Dependency count", ok, reason))

    ok, reason = _check_test_isolation(full_name)
    checks.append(("[INFO] Test isolation", ok, reason))

    passed = must_pass and should_score >= 2
    return passed, should_score, checks


def validate_repo(full_name: str, github_token: str = "") -> dict:
    """Run the full validation pipeline on a GitHub repository.

    Args:
        full_name: GitHub repo in 'owner/name' format (e.g., 'arrow-py/arrow')
        github_token: GitHub personal access token (optional but recommended)

    Returns:
        dict with:
            passed: bool — overall pass/fail
            filter1_passed: bool
            filter2_passed: bool
            filter2_score: int — SHOULD score (0-4)
            checks: list of (check_name, passed, reason) tuples
            repo_info: dict — basic repo metadata
            summary: str — human-readable summary
            error: str | None — error message if validation couldn't complete
    """
    global _current_token
    _current_token = github_token

    result = {
        "passed": False,
        "filter1_passed": False,
        "filter2_passed": False,
        "filter2_score": 0,
        "checks": [],
        "repo_info": {},
        "summary": "",
        "error": None,
    }

    # Step 1: Fetch repo metadata from GitHub API
    try:
        repo = github_get_json("%s/repos/%s" % (GITHUB_API, full_name))
    except Exception as exc:
        result["error"] = "Cannot access repo %s: %s" % (full_name, exc)
        result["summary"] = result["error"]
        return result

    result["repo_info"] = {
        "full_name": repo.get("full_name", full_name),
        "description": (repo.get("description") or "")[:200],
        "stars": repo.get("stargazers_count", 0),
        "size_kb": repo.get("size", 0),
        "language": repo.get("language", ""),
        "homepage": repo.get("homepage", ""),
        "fork": repo.get("fork", False),
        "archived": repo.get("archived", False),
        "topics": repo.get("topics", []),
    }

    # Step 2: Run Filter 1
    _logger.info("Running Filter 1 checks on %s ...", full_name)
    f1_passed, f1_checks = run_filter1(repo, full_name)
    result["filter1_passed"] = f1_passed
    result["checks"].extend(f1_checks)

    if not f1_passed:
        # Still run Filter 2 for informational purposes
        _logger.info("Filter 1 FAILED — running Filter 2 for info ...")
        f2_passed, f2_score, f2_checks = run_filter2(repo, full_name)
        result["filter2_passed"] = f2_passed
        result["filter2_score"] = f2_score
        result["checks"].extend(f2_checks)

        failed = [
            name
            for name, ok, _ in f1_checks
            if not ok and not name.startswith("[INFO]")
        ]
        result["summary"] = "FAILED Filter 1: %s" % ", ".join(failed)
        return result

    # Step 3: Run Filter 2
    _logger.info("Filter 1 PASSED — running Filter 2 checks ...")
    f2_passed, f2_score, f2_checks = run_filter2(repo, full_name)
    result["filter2_passed"] = f2_passed
    result["filter2_score"] = f2_score
    result["checks"].extend(f2_checks)

    if not f2_passed:
        failed_must = [name for name, ok, _ in f2_checks if not ok and "[MUST]" in name]
        if failed_must:
            result["summary"] = "FAILED Filter 2 (MUST): %s" % ", ".join(failed_must)
        else:
            result["summary"] = (
                "FAILED Filter 2: SHOULD score %d/4 (need >= 2)" % f2_score
            )
        return result

    # All passed
    result["passed"] = True
    result["summary"] = (
        "PASSED all checks (Filter 1 OK, Filter 2 OK, SHOULD score %d/4)" % f2_score
    )
    return result


def format_validation_report(result: dict) -> str:
    """Format validation result as a human-readable report."""
    lines = []

    # Header
    info = result.get("repo_info", {})
    if info:
        lines.append("Repository: %s" % info.get("full_name", "?"))
        lines.append(
            "Stars: %s | Language: %s | Size: %s KB"
            % (
                "{:,}".format(info.get("stars", 0)),
                info.get("language", "?"),
                "{:,}".format(info.get("size_kb", 0)),
            )
        )
        if info.get("description"):
            lines.append("Description: %s" % info["description"])
        lines.append("")

    if result.get("error"):
        lines.append("ERROR: %s" % result["error"])
        return "\n".join(lines)

    # Filter 1 section
    lines.append("=== Filter 1: Initial Quality ===")
    f1_checks = [
        c
        for c in result["checks"]
        if not c[0].startswith("[MUST]") and not c[0].startswith("[SHOULD]")
    ]
    for name, passed, reason in f1_checks:
        icon = "PASS" if passed else ("INFO" if name.startswith("[INFO]") else "FAIL")
        line = "[%s] %s" % (icon, name)
        if reason:
            line += " — %s" % reason
        lines.append(line)
    lines.append("Filter 1: %s" % ("PASSED" if result["filter1_passed"] else "FAILED"))
    lines.append("")

    # Filter 2 section
    lines.append("=== Filter 2: Benchmark Suitability ===")
    f2_checks = [
        c
        for c in result["checks"]
        if c[0].startswith("[MUST]") or c[0].startswith("[SHOULD]")
    ]
    f2_info = [
        c for c in result["checks"] if c[0].startswith("[INFO]") and c not in f1_checks
    ]
    for name, passed, reason in f2_checks + f2_info:
        icon = "PASS" if passed else ("INFO" if name.startswith("[INFO]") else "FAIL")
        line = "[%s] %s" % (icon, name)
        if reason:
            line += " — %s" % reason
        lines.append(line)
    lines.append("SHOULD Score: %d/4 (need >= 2)" % result["filter2_score"])
    lines.append("Filter 2: %s" % ("PASSED" if result["filter2_passed"] else "FAILED"))
    lines.append("")

    # Summary
    lines.append("=== RESULT: %s ===" % result["summary"])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a GitHub repo for Commit0 benchmark suitability"
    )
    parser.add_argument(
        "repo",
        help="GitHub repo in owner/name format (e.g., arrow-py/arrow)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default="",
        help="GitHub personal access token (or set GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted report",
    )

    args = parser.parse_args()

    import os

    token = args.token or os.environ.get("GITHUB_TOKEN", "")

    result = validate_repo(args.repo, github_token=token)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_validation_report(result))

    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
