"""Transient DB error retry + safe standalone DB helpers for Jaeger worker."""
import logging
import time

_logger = logging.getLogger(__name__)

_TRANSIENT_PG_CODES = frozenset({"40001", "40P01", "08006", "08001"})
_DB_WRITE_MAX_RETRIES = 3
_DB_WRITE_RETRY_BASE_DELAY = 0.5


def is_transient_db_error(exc):
    pgcode = getattr(exc, "pgcode", None)
    if pgcode and pgcode in _TRANSIENT_PG_CODES:
        return True
    if type(exc).__name__ == "OperationalError" and pgcode is None:
        return True
    return False


def write_with_retry(registry, callback, description="DB write"):
    """Execute callback(cr) with retry on transient PG errors.

    Opens a fresh cursor, calls callback(cr), commits, closes.
    On transient errors (serialization failure, deadlock, connection drop),
    retries up to _DB_WRITE_MAX_RETRIES times with exponential backoff.
    """
    last_error = None
    for attempt in range(1, _DB_WRITE_MAX_RETRIES + 1):
        cr = None
        try:
            cr = registry.cursor()
            callback(cr)
            cr.commit()
            return
        except Exception as exc:
            if cr:
                try:
                    cr.rollback()
                except Exception:
                    pass
            if is_transient_db_error(exc) and attempt < _DB_WRITE_MAX_RETRIES:
                delay = _DB_WRITE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                _logger.warning(
                    "%s: transient error (attempt %d/%d): %s. Retrying in %.1fs.",
                    description, attempt, _DB_WRITE_MAX_RETRIES, exc, delay,
                )
                last_error = exc
                time.sleep(delay)
            else:
                raise
        finally:
            if cr:
                cr.close()
    raise last_error


_ALLOWED_COLUMNS = frozenset({
    "crawl_status", "pr_collection_status", "docker_build_status",
    "test_execution_status", "dataset_status", "trajectory_status",
    "delivery_status", "current_stage", "log", "last_heartbeat",
    "base_image_status", "eks_job_id",
})


def update_repo(registry, repo_id, vals):
    """Safe column-whitelist UPDATE on jaeger_repository."""
    safe_vals = {k: v for k, v in vals.items() if k in _ALLOWED_COLUMNS}
    if not safe_vals:
        return

    def _do(cr):
        sets = ", ".join(f"{k} = %s" for k in sorted(safe_vals))
        params = [safe_vals[k] for k in sorted(safe_vals)]
        params.append(repo_id)
        cr.execute(
            f"UPDATE jaeger_repository SET {sets} WHERE id = %s",  # noqa: S608
            params,
        )

    write_with_retry(registry, _do, f"update_repo(id={repo_id})")


def append_log(registry, repo_id, msg):
    """Bounded log append — keeps last 500K characters."""
    def _do(cr):
        cr.execute(
            "UPDATE jaeger_repository SET log = RIGHT(COALESCE(log, '') || %s, 500000) WHERE id = %s",
            (msg + "\n", repo_id),
        )

    write_with_retry(registry, _do, f"append_log(id={repo_id})")


def heartbeat(registry, repo_id, progress_text=None):
    """Write heartbeat timestamp."""
    def _do(cr):
        cr.execute(
            "UPDATE jaeger_repository SET last_heartbeat = NOW() AT TIME ZONE 'UTC' WHERE id = %s",
            (repo_id,),
        )

    write_with_retry(registry, _do, f"heartbeat(id={repo_id})")
