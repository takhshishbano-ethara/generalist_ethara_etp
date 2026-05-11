import atexit
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import psycopg2

_logger = logging.getLogger(__name__)

_MAX_ENRICHMENT_THREADS = 1
_executor = ThreadPoolExecutor(max_workers=_MAX_ENRICHMENT_THREADS, thread_name_prefix="aurora-disc")
_semaphore = threading.Semaphore(1)
atexit.register(_executor.shutdown, wait=True, cancel_futures=True)

_ALLOWED_COLUMNS = frozenset({
    "state", "enrichment_status", "enrichment_log", "last_enrichment",
    "stars", "forks", "open_issues", "primary_language", "language_pct",
    "has_tests", "has_ci", "license_spdx", "size_kb", "last_pushed",
    "topics", "description", "default_branch", "quality_score",
    "last_seen", "discovery_count", "source_tags",
})

_MAX_LOG_SIZE = 500_000


def _open_cursor(db_name):
    from odoo.modules.registry import Registry
    return Registry(db_name).cursor()


def _update_discovery(cr: Any, rec_id: int, vals: dict[str, Any]) -> None:
    if not vals:
        return
    invalid = set(vals) - _ALLOWED_COLUMNS
    if invalid:
        raise ValueError(f"Attempted to update disallowed columns: {invalid}")
    sets = ", ".join(f"{k} = %s" for k in vals)
    params = list(vals.values()) + [rec_id]
    cr.execute(f"UPDATE aurora_discovery SET {sets} WHERE id = %s", params)


def _append_log(cr: Any, rec_id: int, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    cr.execute(
        "UPDATE aurora_discovery SET enrichment_log = "
        "RIGHT(COALESCE(enrichment_log, '') || %s, %s) WHERE id = %s",
        [line, _MAX_LOG_SIZE, rec_id],
    )


def _lease_tokens(cr) -> list[str]:
    from .credential_manager import decrypt_value_raw
    cr.execute(
        "SELECT id, token FROM aurora_github_token "
        "WHERE state = 'active' AND leased_by_run_id IS NULL "
        "ORDER BY rate_limit_remaining DESC NULLS LAST LIMIT 2"
    )
    rows = cr.fetchall()
    tokens: list[str] = []
    for _, encrypted_token in rows:
        try:
            tokens.append(decrypt_value_raw(cr, encrypted_token))
        except Exception:
            continue
    return tokens


def _enrich_single_repo(tokens: list[str], full_name: str, branch: str) -> dict:
    from github import Auth, Github

    client = Github(auth=Auth.Token(tokens[0]), per_page=100)
    gh_repo = client.get_repo(full_name)

    result: dict[str, Any] = {}
    result["stars"] = gh_repo.stargazers_count
    result["forks"] = gh_repo.forks_count
    result["open_issues"] = gh_repo.open_issues_count
    result["size_kb"] = gh_repo.size
    result["description"] = (gh_repo.description or "")[:500]
    result["primary_language"] = gh_repo.language or ""
    result["license_spdx"] = (gh_repo.license.spdx_id if gh_repo.license else "") or ""
    if gh_repo.pushed_at:
        result["last_pushed"] = gh_repo.pushed_at.strftime("%Y-%m-%d %H:%M:%S")

    languages = gh_repo.get_languages()
    total_bytes = sum(languages.values())
    primary = gh_repo.language or ""
    lang_bytes = 0
    if total_bytes and primary:
        for name, nbytes in languages.items():
            if name.lower() == primary.lower():
                lang_bytes = nbytes
                break
    result["language_pct"] = round(lang_bytes / total_bytes * 100, 1) if total_bytes else 0.0

    test_indicators = ["pytest", "unittest", "jest", "mocha", "cargo test",
                       "go test", "junit", "rspec", "phpunit"]
    config_files = ["pyproject.toml", "setup.cfg", "package.json", "Cargo.toml",
                    "go.mod", "build.gradle", "pom.xml", "Gemfile", "composer.json"]
    has_tests = False
    for cf in config_files:
        try:
            content = gh_repo.get_contents(cf, ref=branch)
            if content and hasattr(content, "decoded_content"):
                text = content.decoded_content.decode(errors="replace").lower()
                if any(ind in text for ind in test_indicators):
                    has_tests = True
                    break
        except Exception:
            continue
    result["has_tests"] = has_tests

    ci_paths = [".github/workflows", ".circleci", ".travis.yml",
                "Jenkinsfile", ".gitlab-ci.yml"]
    has_ci = False
    for ci_path in ci_paths:
        try:
            gh_repo.get_contents(ci_path, ref=branch)
            has_ci = True
            break
        except Exception:
            continue
    result["has_ci"] = has_ci

    return result


def _run_enrichment(db_name: str, uid: int, rec_id: int) -> None:
    cr = _open_cursor(db_name)
    try:
        _update_discovery(cr, rec_id, {"enrichment_status": "running", "state": "enriching"})
        cr.commit()

        _append_log(cr, rec_id, "Starting enrichment...")
        cr.commit()

        tokens = _lease_tokens(cr)
        if not tokens:
            _update_discovery(cr, rec_id, {
                "enrichment_status": "failed", "state": "new",
            })
            _append_log(cr, rec_id, "ERROR: No tokens available for enrichment")
            cr.commit()
            return

        cr.execute(
            "SELECT github_org, github_repo, default_branch FROM aurora_discovery WHERE id = %s",
            [rec_id],
        )
        row = cr.fetchone()
        if not row:
            return
        org, repo, branch = row
        full_name = f"{org}/{repo}"
        branch = branch or "main"

        result = _enrich_single_repo(tokens, full_name, branch)

        vals = {
            "enrichment_status": "done",
            "state": "validated",
            "last_enrichment": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        vals.update(result)
        _update_discovery(cr, rec_id, vals)
        _append_log(cr, rec_id, f"Enrichment complete: score will be recomputed")
        cr.commit()

    except Exception as exc:
        _logger.exception("Enrichment failed for rec %d", rec_id)
        try:
            _update_discovery(cr, rec_id, {"enrichment_status": "failed", "state": "new"})
            _append_log(cr, rec_id, f"ERROR: {exc}")
            cr.commit()
        except Exception:
            pass
    finally:
        try:
            cr.close()
        except Exception:
            pass


def _run_enrichment_batch(db_name: str, uid: int, rec_ids: list[int]) -> None:
    for rec_id in rec_ids:
        try:
            _run_enrichment(db_name, uid, rec_id)
        except Exception as exc:
            _logger.warning("Batch enrichment failed for rec %d: %s", rec_id, exc)
        time.sleep(0.5)


def submit_enrichment_async(db_name: str, uid: int, rec_id: int) -> bool:
    if not _semaphore.acquire(blocking=False):
        _logger.info("Enrichment slot busy, skipping rec %d", rec_id)
        return False
    try:
        _executor.submit(_safe_run, _run_enrichment, db_name, uid, rec_id)
    except Exception:
        _semaphore.release()
        raise
    return True


def submit_enrichment_batch_async(db_name: str, uid: int, rec_ids: list[int]) -> bool:
    if not _semaphore.acquire(blocking=False):
        _logger.info("Enrichment slot busy, skipping batch of %d", len(rec_ids))
        return False
    try:
        _executor.submit(_safe_run, _run_enrichment_batch, db_name, uid, rec_ids)
    except Exception:
        _semaphore.release()
        raise
    return True


def is_enrichment_slot_available() -> bool:
    acquired = _semaphore.acquire(blocking=False)
    if acquired:
        _semaphore.release()
    return acquired


def _safe_run(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception:
        _logger.exception("Discovery executor error in %s", fn.__name__)
    finally:
        _semaphore.release()
