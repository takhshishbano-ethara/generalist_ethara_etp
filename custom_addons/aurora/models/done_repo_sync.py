"""Sync done/no-PR lists with the ElhaanEth/auroraScraping GitHub repo.

Discovery uses these lists to skip repos that have already been processed
(by the standalone EC2 scraper or by a prior Aurora pipeline). After Phase 2
of an evaluation completes successfully, the {org}/{repo} pair is appended
back to done_repo.txt on GitHub.

Auth: encrypted PAT (or comma-separated PAT pool) stored at
`aurora.auroraScraping_token` in ir.config_parameter. Repo coordinates from
`aurora.auroraScraping_repo` (default ElhaanEth/auroraScraping) and
`aurora.auroraScraping_branch` (default main).

Behaviour:
- Reads cached for 10 minutes per-process.
- Appends always fetch fresh content first to avoid stale-SHA conflicts.
- Retries once on 409 (concurrent write).
"""
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

_logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 10 * 60
_DONE_FILE = "done_repo.txt"
_NO_PR_FILE = "no_pr.txt"

_DEFAULT_REPO = "ElhaanEth/auroraScraping"
_DEFAULT_BRANCH = "main"


@dataclass(frozen=True)
class MatchIndex:
    exact: frozenset
    bare: frozenset
    fuzzy: dict


_cache_lock = threading.Lock()
_cache: dict = {}


def _strip_for_fuzzy(s: str) -> str:
    return s.replace("-", "").replace("_", "").replace(".", "").replace(" ", "").lower()


def _normalize_repo_name(raw: str) -> Optional[str]:
    """Normalise any repo reference to canonical 'org/repo' lowercase.

    Handles slash, double/triple underscore separator, and ':lang:status'
    tails. The '_lht_final' suffix stripping from the original EC2 scraper
    is intentionally NOT performed - Aurora never writes that suffix.
    """
    entry = raw.strip().lower()
    if not entry or entry.startswith("#"):
        return None

    entry = re.sub(r":.*$", "", entry)

    if "/" in entry and "__" not in entry:
        parts = entry.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0].strip()}/{parts[1].strip()}"

    if "__" in entry:
        parts = re.split(r"_{2,}", entry, maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1]}"

    if "/" not in entry:
        return entry

    return None


def _build_match_index(entries):
    exact, bare = set(), set()
    fuzzy = {}
    for e in entries:
        if "/" in e:
            exact.add(e)
            fuzzy[_strip_for_fuzzy(e)] = e
        else:
            bare.add(e)
            fuzzy[_strip_for_fuzzy(e)] = e
    return MatchIndex(exact=frozenset(exact), bare=frozenset(bare), fuzzy=fuzzy)


def _match_repo(org: str, repo: str, idx: MatchIndex):
    org_l, repo_l = org.lower(), repo.lower()
    canonical = f"{org_l}/{repo_l}"

    if canonical in idx.exact:
        return True, "exact"

    if repo_l in idx.bare or org_l in idx.bare:
        return True, "bare"

    if _strip_for_fuzzy(canonical) in idx.fuzzy or _strip_for_fuzzy(repo_l) in idx.fuzzy:
        return True, "fuzzy"

    return False, "none"


def is_repo_done(org: str, repo: str, done_idx: MatchIndex):
    return _match_repo(org, repo, done_idx)


def is_repo_no_pr(org: str, repo: str, no_pr_idx: MatchIndex):
    return _match_repo(org, repo, no_pr_idx)


def _pick_token(token_value: str) -> str:
    if "," in token_value:
        tokens = [t.strip() for t in token_value.split(",") if t.strip()]
        if not tokens:
            raise ValueError("auroraScraping token pool is empty after split")
        return random.choice(tokens)
    return token_value.strip()


def _get_repo_handle(token_value: str, repo_full_name: str):
    from github import Auth, Github
    token = _pick_token(token_value)
    return Github(auth=Auth.Token(token)).get_repo(repo_full_name)


def _fetch_lines(gh_repo, path: str, branch: str):
    content_file = gh_repo.get_contents(path, ref=branch)
    text = content_file.decoded_content.decode("utf-8")
    entries = set()
    for line in text.splitlines():
        normalized = _normalize_repo_name(line)
        if normalized:
            entries.add(normalized)
    return entries, content_file.sha, text


def get_config(env):
    from .credential_manager import decrypt_value
    ICP = env["ir.config_parameter"].sudo()
    repo_full_name = ICP.get_param("aurora.auroraScraping_repo", _DEFAULT_REPO)
    branch = ICP.get_param("aurora.auroraScraping_branch", _DEFAULT_BRANCH)
    raw_token = ICP.get_param("aurora.auroraScraping_token", "")
    if not raw_token:
        raise ValueError(
            "ir.config_parameter 'aurora.auroraScraping_token' is empty; "
            "set an encrypted PAT (or comma-separated pool) with read+write "
            "scope on the auroraScraping repo."
        )
    token_value = decrypt_value(ICP, raw_token)
    return repo_full_name, branch, token_value


def sync_done_repos(token_value: str, repo_full_name: str, branch: str, *,
                    force_refresh: bool = False):
    """Fetch done_repo.txt + no_pr.txt; return (done_idx, no_pr_idx).

    10-minute in-process TTL cache. Pass force_refresh=True to bypass.
    """
    cache_key = f"{repo_full_name}@{branch}"
    now = time.time()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and not force_refresh and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

    gh_repo = _get_repo_handle(token_value, repo_full_name)
    done_entries, _, _ = _fetch_lines(gh_repo, _DONE_FILE, branch)
    try:
        no_pr_entries, _, _ = _fetch_lines(gh_repo, _NO_PR_FILE, branch)
    except Exception as exc:
        _logger.warning("no_pr.txt fetch failed (%s); treating as empty", exc)
        no_pr_entries = set()

    done_idx = _build_match_index(done_entries)
    no_pr_idx = _build_match_index(no_pr_entries)

    with _cache_lock:
        _cache[cache_key] = (now, (done_idx, no_pr_idx))

    _logger.info(
        "Synced auroraScraping: %d done, %d no_pr from %s@%s",
        len(done_entries), len(no_pr_entries), repo_full_name, branch,
    )
    return done_idx, no_pr_idx


def invalidate_cache(repo_full_name: Optional[str] = None, branch: Optional[str] = None):
    with _cache_lock:
        if repo_full_name and branch:
            _cache.pop(f"{repo_full_name}@{branch}", None)
        else:
            _cache.clear()


def append_done_repo(token_value: str, repo_full_name: str, branch: str,
                     org: str, repo: str, *, commit_suffix: str = ""):
    """Append `org/repo` to done_repo.txt on GitHub. Idempotent + race-safe.

    Returns True on append, False if entry was already present.
    Retries once on 409 (concurrent write).
    """
    canonical = f"{org.lower()}/{repo.lower()}"
    commit_msg = f"Aurora: mark {canonical} as done"
    if commit_suffix:
        commit_msg = f"{commit_msg} ({commit_suffix})"

    last_exc = None
    for attempt in range(2):
        try:
            gh_repo = _get_repo_handle(token_value, repo_full_name)
            existing_entries, current_sha, current_text = _fetch_lines(
                gh_repo, _DONE_FILE, branch,
            )

            if canonical in existing_entries:
                _logger.info(
                    "auroraScraping: %s already in done_repo.txt, skip append",
                    canonical,
                )
                return False

            if current_text and not current_text.endswith("\n"):
                current_text += "\n"
            new_text = current_text + canonical + "\n"

            gh_repo.update_file(
                path=_DONE_FILE,
                message=commit_msg,
                content=new_text,
                sha=current_sha,
                branch=branch,
            )
            invalidate_cache(repo_full_name, branch)
            _logger.info("auroraScraping: appended %s to done_repo.txt", canonical)
            return True
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and "409" in str(exc):
                _logger.warning(
                    "auroraScraping append SHA conflict, retrying: %s", exc,
                )
                time.sleep(0.5)
                continue
            raise

    if last_exc:
        raise last_exc
    return False
