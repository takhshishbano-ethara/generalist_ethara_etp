"""Standalone pipeline helpers for Jaeger background threads.

These functions run outside the ORM (no self, no env) — they open their own
cursors and are safe to call from background threads.

NOTE: The K8s worker entrypoint is now worker/entrypoint.py (no Odoo imports).
This file is retained for _write_with_retry, _append_log_standalone,
_check_cancelled, and PipelineCancelled which are used by Stages 3-7.
"""
import logging
import threading
import time as _time
from datetime import datetime

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _write_with_retry(db_name, repo_id, vals):
    import time
    from odoo.orm.registry import Registry
    for attempt in range(3):
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                repo = env["jaeger.repository"].browse(repo_id)
                if not repo.exists():
                    _logger.error("Repo %s does not exist", repo_id)
                    return
                repo.write(vals)
            return
        except Exception as e:
            if "serialize" in str(e).lower() and attempt < 2:
                _logger.warning("Serialization conflict (attempt %d/3)", attempt + 1)
                time.sleep(1 + attempt)
                continue
            raise


def _append_log_standalone(db_name, repo_id, msg):
    from odoo.orm.registry import Registry
    line = "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg)
    for attempt in range(3):
        try:
            with Registry(db_name).cursor() as cr:
                cr.execute(
                    "UPDATE jaeger_repository SET log_output = "
                    "CASE WHEN LENGTH(COALESCE(log_output, '')) > 200000 "
                    "THEN RIGHT(log_output, 150000) || %s "
                    "ELSE COALESCE(log_output, '') || %s END "
                    "WHERE id = %s",
                    [line, line, repo_id],
                )
            return
        except Exception as e:
            if "serialize" in str(e).lower() and attempt < 2:
                _time.sleep(1 + attempt)
                continue
            _logger.warning("Failed to append log: %s", e)
            return


class PipelineCancelled(Exception):
    pass


def _check_cancelled(db_name, repo_id):
    from odoo.orm.registry import Registry
    with Registry(db_name).cursor() as cr:
        cr.execute(
            "SELECT cancel_requested FROM jaeger_repository WHERE id = %s",
            [repo_id],
        )
        row = cr.fetchone()
        if row and row[0]:
            raise PipelineCancelled("Pipeline %s cancelled by user" % repo_id)


def _heartbeat_standalone(db_name, repo_id, text=None):
    vals = {"last_heartbeat": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if text:
        vals["pr_collection_step"] = text
    _write_with_retry(db_name, repo_id, vals)
