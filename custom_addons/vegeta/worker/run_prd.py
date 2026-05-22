#!/usr/bin/env python3
"""Vegeta PRD Worker — Kubernetes Job entrypoint.

Required env vars: ``JOB_ID`` (int), ``ODOO_DB`` (str).
Optional env vars: ``ODOO_CONF`` (path), ``DB_HOST``/``DB_PORT``/``DB_USER``/
``DB_PASSWORD`` (override the booted odoo.conf DB credentials).

This boots a headless Odoo 19 registry inside the pod and runs the EXISTING
``vegeta.job._run_prd_generation_bg`` logic verbatim — no PRD-generation logic
is duplicated here. SIGTERM (sent by Kubernetes on deletion / preemption /
``activeDeadlineSeconds``) is translated into a cooperative cancel that the
``_is_cancelled`` checkpoints inside ``_run_prd_generation_bg`` honour — before
generation, before scoring, before QC and before the final write.
"""

import logging
import os
import signal
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [vegeta-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger("vegeta.worker")


def _boot_odoo(db_name, conf_path=None):
    conf_path = conf_path or os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf")
    sys.argv = ["odoo", "--no-http", f"--config={conf_path}", f"--database={db_name}"]

    from odoo import init as _odoo_init  # noqa: F401 — triggers Odoo 19 lazy bootstrap
    import odoo
    odoo.tools.config.parse_config(sys.argv[1:])

    _db_env_overrides = {
        "DB_HOST": "db_host",
        "DB_PORT": "db_port",
        "DB_USER": "db_user",
        "DB_PASSWORD": "db_password",
    }
    for env_key, conf_key in _db_env_overrides.items():
        val = os.environ.get(env_key)
        if val:
            odoo.tools.config[conf_key] = val

    from odoo.modules.registry import Registry
    registry = Registry(db_name)
    _logger.info("Odoo registry booted for db=%s", db_name)
    return registry


def _request_cancel(db_name, job_id):
    """Set ``cancel_requested`` so ``_run_prd_generation_bg`` bails out at its
    next checkpoint. Uses a short-lived raw cursor because the SIGTERM handler
    can fire while the registry is mid-operation on another cursor."""
    try:
        from odoo.modules.registry import Registry
        with Registry(db_name).cursor() as cr:
            cr.execute(
                "UPDATE vegeta_job SET cancel_requested = TRUE WHERE id = %s",
                (job_id,),
            )
            cr.commit()
        _logger.warning(
            "SIGTERM received — requested cooperative cancel for job %s", job_id,
        )
    except Exception:
        _logger.exception("Failed to record SIGTERM cancel for job %s", job_id)


def main():
    job_id_raw = os.environ.get("JOB_ID")
    db_name = os.environ.get("ODOO_DB")
    conf_path = os.environ.get("ODOO_CONF")

    if not job_id_raw:
        _logger.error("JOB_ID environment variable is required")
        sys.exit(1)
    if not db_name:
        _logger.error("ODOO_DB environment variable is required")
        sys.exit(1)
    try:
        job_id = int(job_id_raw)
    except ValueError:
        _logger.error("JOB_ID must be an integer, got: %s", job_id_raw)
        sys.exit(1)

    _logger.info("Worker starting: job_id=%d, db=%s", job_id, db_name)

    # Wall-clock anchor for the whole pod lifetime — the "exiting" line at the
    # end reports elapsed from here. A pod killed by K8s activeDeadlineSeconds
    # shows its last log line near the deadline mark.
    _t0 = time.monotonic()

    try:
        registry = _boot_odoo(db_name, conf_path)
    except Exception:
        _logger.exception("Failed to boot Odoo registry")
        sys.exit(2)

    if threading.current_thread() is threading.main_thread():
        signal.signal(
            signal.SIGTERM,
            lambda _signum, _frame: _request_cancel(db_name, job_id),
        )

    from odoo import api, SUPERUSER_ID

    # _run_prd_generation_bg manages its own short-lived cursors per phase
    # (keyed by db_name); the call is made inside this cursor block so the
    # recordset is never used after its cursor has been closed.
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            vegeta_job = env["vegeta.job"].browse(job_id)
            if not vegeta_job.exists():
                _logger.error(
                    "vegeta.job %s not found — nothing to do", job_id,
                )
                sys.exit(1)
            # Stage boundary: pod has booted and found the job, now handing
            # off to the shared pipeline. If this line appears for a job but
            # the "exiting" line never does, the pod died INSIDE
            # _run_prd_generation_bg (OOM-kill / SIGTERM / deadline) — the
            # PRD-GEN PHASE lines tell you which phase.
            _logger.info(
                "[vegeta][job=%s] worker entering _run_prd_generation_bg "
                "(boot took %.1fs)", job_id, time.monotonic() - _t0,
            )
            vegeta_job._run_prd_generation_bg(db_name, job_id)
    except Exception:
        _logger.exception(
            "Unhandled exception running PRD generation for job %s", job_id,
        )
        sys.exit(3)

    _logger.info("Worker finished PRD generation for job %s", job_id)
    # Total pod lifetime — compare against PRD_DEADLINE_SECONDS (3600s). A
    # value close to the deadline means generation is too slow for the budget.
    _logger.info(
        "[vegeta][job=%s] worker exiting cleanly — total pod lifetime %.1fs",
        job_id, time.monotonic() - _t0,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
