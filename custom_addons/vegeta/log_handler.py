"""Python logging handler that captures `[job=N]` tagged log records
from `odoo.addons.vegeta.*` and persists them to `vegeta.job.log` for
the unified log viewer on the job form.

The handler runs synchronously in the logging thread but writes via a
short-lived Registry cursor so it does not poison the request's own
transaction. Failures are swallowed — logging must never raise."""

import logging
import os
import re
import threading

_logger = logging.getLogger(__name__)

_JOB_TAG_RE = re.compile(r"\[job=(VEG-)?(\d+)\]")
_HOSTNAME = os.environ.get("HOSTNAME", "")


def _detect_source():
    if _HOSTNAME.startswith("vegeta-worker-"):
        return "worker"
    return "odoo"


_SOURCE = _detect_source()


def _resolve_db_name():
    # Odoo pins dbname on each thread for HTTP requests; the worker's
    # heartbeat thread also sets it. Fall back to env var (set in K8s
    # manifests) for cron / startup paths.
    th = threading.current_thread()
    return getattr(th, "dbname", None) or os.environ.get("ODOO_DB") or None


class VegetaJobLogHandler(logging.Handler):
    """Filters records named ``odoo.addons.vegeta*`` whose message carries
    ``[job=<id>]`` and writes them to ``vegeta_job_log`` via direct SQL
    on a short-lived cursor."""

    _name_to_id_cache = {}

    def emit(self, record):
        try:
            if not record.name.startswith("odoo.addons.vegeta"):
                return
            msg = record.getMessage()
            m = _JOB_TAG_RE.search(msg)
            if not m:
                return
            job_id = self._resolve_job_id(m)
            if not job_id:
                return
            db_name = _resolve_db_name()
            if not db_name:
                return
            self._write_log(db_name, job_id, record.levelname, msg, record.created)
        except Exception:
            pass

    def _resolve_job_id(self, match):
        prefix, num = match.group(1), match.group(2)
        if not prefix:
            # `[job=12]` - integer is the record id directly.
            try:
                return int(num)
            except ValueError:
                return None
        # `[job=VEG-00012]` - look up by name with a small cache.
        name = f"VEG-{num.zfill(5)}"
        if name in self._name_to_id_cache:
            return self._name_to_id_cache[name]
        db_name = _resolve_db_name()
        if not db_name:
            return None
        try:
            from odoo.modules.registry import Registry
            with Registry(db_name).cursor() as cr:
                cr.execute("SELECT id FROM vegeta_job WHERE name = %s LIMIT 1", (name,))
                row = cr.fetchone()
                if row:
                    self._name_to_id_cache[name] = row[0]
                    return row[0]
        except Exception:
            return None
        return None

    def _write_log(self, db_name, job_id, level, message, created):
        from datetime import datetime, timezone
        from odoo.modules.registry import Registry
        ts = datetime.fromtimestamp(created, timezone.utc).replace(tzinfo=None)
        truncated = message[:16384]
        try:
            with Registry(db_name).cursor() as cr:
                # FK on job_id needs KEY SHARE on vegeta_job; the parent
                # request may hold FOR UPDATE on the same row inside its
                # own cursor, so cap the wait and drop the log line on
                # timeout rather than deadlocking action_run.
                cr.execute("SET LOCAL lock_timeout = '500ms'")
                cr.execute(
                    """
                    INSERT INTO vegeta_job_log
                        (job_id, timestamp, source, level, message, pod, create_uid, write_uid, create_date, write_date)
                    VALUES (%s, %s, %s, %s, %s, %s, 1, 1, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
                    """,
                    (job_id, ts, _SOURCE, level, truncated, _HOSTNAME or None),
                )
                cr.commit()
        except Exception:
            pass


_INSTALLED = False


def install_handler():
    """Attach the handler to the root `odoo.addons.vegeta` logger exactly once.
    Safe to call multiple times (idempotent via module-level guard)."""
    global _INSTALLED
    if _INSTALLED:
        return
    handler = VegetaJobLogHandler()
    handler.setLevel(logging.INFO)
    target = logging.getLogger("odoo.addons.vegeta")
    target.addHandler(handler)
    _INSTALLED = True
    _logger.info("[vegeta] log handler installed (source=%s, hostname=%s)", _SOURCE, _HOSTNAME)
