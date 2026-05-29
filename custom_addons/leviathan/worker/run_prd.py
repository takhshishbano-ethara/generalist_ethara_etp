#!/usr/bin/env python3
"""Leviathan PRD Worker — standalone claim-loop daemon.

Replaces the in-Odoo ``ir.cron`` drainer with a long-lived Python process
that owns the PRD claim loop. Activated when the System Parameter
``leviathan.prd_execution_mode = worker`` is set; the ``ir.cron`` drainer
short-circuits so this process is the only drainer cluster-wide.

Why a standalone process (vs. another Odoo pod with --no-http)
==============================================================
* No Odoo HTTP/cron framework boot per replica. Cold start is the
  Postgres ``Registry`` boot only (seconds, not 30-60 s).
* The claim loop runs in *this* process, not behind a 1-minute cron tick
  - reaction time to a fresh batch is the poll interval, not 60 s.
* Easier to reason about (single binary), easier to debug, leaner image.

Lifecycle
=========
1. Boot the Odoo registry for ``ODOO_DB`` (one-time, ~seconds).
2. Install a SIGTERM/SIGINT handler that sets a shutdown event.
3. Loop:
     a. Call ``leviathan.job._prd_queue_fail_poison`` /
        ``_prd_queue_recover_stale`` / ``_prd_queue_claim_and_dispatch``
        - exactly what the Odoo cron used to call - via a fresh cursor.
        The claim method already submits work to the in-process
        ``ThreadPoolExecutor`` (``_PRD_POOL_SIZE`` slots) defined in
        ``leviathan_job.py``.
     b. Sleep ``LEVIATHAN_WORKER_POLL_S`` seconds.
4. On SIGTERM: stop claiming new jobs, wait up to
   ``LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S`` for in-flight futures to
   finish, then exit. Anything still running past the drain budget is
   abandoned to SIGKILL; the row's heartbeat goes stale and the
   ``_prd_queue_recover_stale`` step on the *next* worker pod's tick
   recovers it.

Required env vars
=================
``ODOO_DB``                                Database name.

Optional env vars
=================
``ODOO_CONF``                              odoo.conf path (default
                                            ``/etc/odoo/odoo.conf``)
``DB_HOST`` / ``DB_PORT`` / ``DB_USER`` /  Override booted odoo.conf
``DB_PASSWORD``                            credentials.
``LEVIATHAN_WORKER_POLL_S``                Loop sleep (default 5).
``LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S``    Drain budget on SIGTERM
                                            (default 1800).
``LEVIATHAN_WORKER_CLAIM_FAIL_LIMIT``      Consecutive claim-loop
                                            failures before the process
                                            exits non-zero so K8s
                                            replaces it with a fresh
                                            registry (default 5).
``LEVIATHAN_ROLE``                         Auto-set to ``worker`` by
                                            this process. Set to ``ui``
                                            in the UI pod's env so the
                                            ``ir.cron`` drainer
                                            short-circuits there too.

Local check mode
================
``python run_prd.py --check`` boots the registry, runs a single drainer
iteration, and exits 0. Used by ``docker compose run`` smoke tests and
as a Kubernetes readiness probe.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import wait as futures_wait

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [leviathan-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger("leviathan.worker")

_WORKER_LABEL = f"leviathan-worker-{socket.gethostname()}-{os.getpid()}"
_shutdown = threading.Event()


# ---------------------------------------------------------------------------
# Settings resolution: ICP > env > default
# ---------------------------------------------------------------------------
# Mirrors the pattern in ``controllers/main.py::_get_webhook_max_bytes``:
# operator-facing knobs live in ``ir.config_parameter`` so they can be
# edited live from Odoo Settings without a worker restart. The env var is
# kept as a fallback so devops can still pin a value via the Deployment
# spec when needed. Defaults match the pre-ICP env defaults so behaviour
# is unchanged when neither is set.
#
# Read fresh per-tick (poll/claim_fail_limit) or per-shutdown
# (shutdown_timeout_s). Cheap — a single SELECT on a small indexed table
# — and the worker already clears the ormcache once per tick (see
# ``_drainer_tick`` below), so live edits propagate within one poll
# interval.
def _resolve_int_setting(
    registry, icp_key: str, env_key: str, default: int,
) -> int:
    """ICP > env > default. Returns ``default`` on any read failure."""
    try:
        with registry.cursor() as cr:
            cr.execute(
                "SELECT value FROM ir_config_parameter WHERE key = %s",
                (icp_key,),
            )
            row = cr.fetchone()
        if row and row[0] not in (None, "", "False", "false"):
            return int(row[0])
    except Exception:
        # Registry not ready yet, DB blip, or non-int value — fall
        # through to env / default. Worker keeps running.
        pass
    try:
        return int(os.environ.get(env_key, str(default)))
    except (TypeError, ValueError):
        return default


# Boot-time defaults. Live values are resolved per-tick / per-shutdown via
# ``_resolve_int_setting`` so an operator change to the System Parameter
# takes effect on the next poll without restart.
POLL_INTERVAL_S = int(os.environ.get("LEVIATHAN_WORKER_POLL_S", "5"))
SHUTDOWN_TIMEOUT_S = int(
    os.environ.get("LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S", "1800")
)
CLAIM_FAILURE_LIMIT = int(
    os.environ.get("LEVIATHAN_WORKER_CLAIM_FAIL_LIMIT", "5")
)


def _boot_odoo(db_name: str, conf_path: str | None = None):
    """Boot a headless Odoo registry. No HTTP, no cron, no web layer."""
    conf_path = conf_path or os.environ.get(
        "ODOO_CONF", "/etc/odoo/odoo.conf"
    )
    # Mirror the canonical odoo-bin invocation but skip the server loop.
    sys.argv = [
        "odoo",
        "--no-http",
        f"--config={conf_path}",
        f"--database={db_name}",
    ]

    # Odoo 19's top-level `odoo` package does NOT eagerly import its
    # submodules - you have to touch something inside the package first
    # to trigger the lazy bootstrap. `from odoo import init` is the
    # canonical kick (it registers the addons path + populates
    # `odoo.tools`). Without it `odoo.tools.config` raises AttributeError
    # because the submodule was never imported.
    from odoo import init as _odoo_init  # noqa: F401
    import odoo
    odoo.tools.config.parse_config(sys.argv[1:])

    # Per-env-var DB credential overrides. The worker pod's Secret holds
    # the canonical DB creds; odoo.conf is a fallback skeleton.
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


_QUEUE_DISABLED_LOGGED = False


def _drainer_tick(registry, db_name: str) -> int:
    """Run one full drainer pass (poison-cap, stale-recover, claim+dispatch).

    Calls ``leviathan.job._prd_drain_once`` directly — NOT the public cron
    entry-point, which short-circuits when ``prd_execution_mode=worker``
    (the whole point of the worker is to BE the drainer in that mode, so
    the cron entry-point's mode-gate would self-defeat).

    One claim implementation, shared with the in-Odoo cron path; one
    correctness analysis (the fence in ``_prd_queue_claim_and_dispatch``);
    one PG advisory lock acquired inside ``_prd_drain_once``.

    Returns the post-tick queue depth for log/metric purposes.
    """
    global _QUEUE_DISABLED_LOGGED
    from odoo import api, SUPERUSER_ID

    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        Job = env["leviathan.job"]

        # CRITICAL: invalidate this process's ORM cache before reading any
        # System Parameter. Odoo caches ``ir.config_parameter._get_param``
        # via ``@ormcache`` per-registry; cross-process invalidation
        # normally rides on ``bus.bus`` Postgres NOTIFY, which a headless
        # worker (--no-http, no bus listener) does NOT receive. Without
        # this clear, an operator updating a Bedrock token / Lambda URL /
        # any other parameter through the Odoo Settings UI on the host
        # pod has zero effect on the worker pod until the worker is
        # restarted — the worker keeps serving boot-time values from its
        # local cache forever. The clear is cheap (in-memory LRU drop)
        # and runs once per poll interval, so the only real cost is
        # re-querying ``ir_config_parameter`` rows (~ms) on the next
        # access. See https://github.com/odoo/odoo/blob/19.0/odoo/orm/registry.py
        # for the underlying cache mechanism.
        env.registry.clear_cache()

        if not Job._prd_queue_enabled():
            # One-shot log to avoid spamming the loop when the operator
            # has temporarily disabled the queue while leaving the worker
            # pod running. Reset the flag once the queue is back on.
            if not _QUEUE_DISABLED_LOGGED:
                _logger.warning(
                    "queue disabled (leviathan.prd_queue_enabled is not "
                    "True) — worker is idle. Set the System Parameter to "
                    "True to start draining."
                )
                _QUEUE_DISABLED_LOGGED = True
            return 0
        if _QUEUE_DISABLED_LOGGED:
            _logger.info("queue re-enabled — resuming drain")
            _QUEUE_DISABLED_LOGGED = False

        Job._prd_drain_once()

        cr.execute(
            "SELECT count(*) FROM leviathan_job "
            "WHERE state = 'generating' AND auto_continue = true "
            "  AND started_processing_at IS NULL"
        )
        return cr.fetchone()[0]


def _on_signal(signum, _frame):
    if not _shutdown.is_set():
        _logger.warning(
            "Received signal %d - stopping claim loop, draining in-flight "
            "futures (budget %ds)",
            signum, SHUTDOWN_TIMEOUT_S,
        )
        _shutdown.set()


def _drain_pool(timeout_s: int):
    """Wait up to ``timeout_s`` for in-flight PRD futures to finish.

    The PRD pool lives in ``leviathan_job`` module-globals. We snapshot
    the live futures (anything not done) at shutdown time and bounded-
    wait. Anything still running past the budget is abandoned - the row
    stops heartbeating, and the next worker's ``_prd_queue_recover_stale``
    re-claims it. No silent data loss.
    """
    try:
        from odoo.addons.leviathan.models import leviathan_job as lj
    except Exception:
        _logger.exception("cannot import leviathan_job for drain; skipping")
        return

    pool = lj._POOL_REGISTRY.get(os.getpid())
    if pool is None:
        _logger.info("no PRD pool registered for this pid - nothing to drain")
        return

    # The pool tracks futures internally; we cannot enumerate them
    # directly, so we drain by closing `submit` and then bounded-waiting
    # on the live worker threads (which are named).
    pool.shutdown(wait=False, cancel_futures=False)

    # Best-effort: use the active-thread count as a proxy for in-flight.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # The pool's threads are daemons named "leviathan-prd-*".
        live = sum(
            1 for t in threading.enumerate()
            if t.name.startswith("leviathan-prd[") and t.is_alive()
        )
        if live == 0:
            _logger.info("pool drained cleanly")
            return
        _logger.info(
            "drain: %d PRD thread(s) still running, %.0fs budget left",
            live, max(0.0, deadline - time.monotonic()),
        )
        time.sleep(5)

    live = sum(
        1 for t in threading.enumerate()
        if t.name.startswith("leviathan-prd[") and t.is_alive()
    )
    if live:
        _logger.warning(
            "drain budget exhausted - %d PRD thread(s) STILL RUNNING; "
            "abandoning to SIGKILL. stale-heartbeat recovery on the next "
            "worker's tick will re-claim these jobs.",
            live,
        )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    check_mode = "--check" in argv

    db_name = os.environ.get("ODOO_DB")
    if not db_name:
        _logger.error("ODOO_DB environment variable is required")
        return 1

    # Belt-and-suspenders pod-role hint. The drainer in this very process
    # uses the leviathan.prd_execution_mode switch (not LEVIATHAN_ROLE)
    # so this is purely for any *other* code that reads the role env var.
    os.environ.setdefault("LEVIATHAN_ROLE", "worker")

    _logger.info(
        "Worker booting: db=%s poll=%ds drain_budget=%ds label=%s "
        "check_mode=%s",
        db_name, POLL_INTERVAL_S, SHUTDOWN_TIMEOUT_S, _WORKER_LABEL,
        check_mode,
    )

    try:
        registry = _boot_odoo(db_name)
    except Exception:
        _logger.exception("failed to boot Odoo registry")
        return 2

    if check_mode:
        try:
            depth = _drainer_tick(registry, db_name)
            _logger.info("--check OK (post-tick queue depth=%d)", depth)
            return 0
        except Exception:
            _logger.exception("--check FAILED in drainer tick")
            return 3

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    consecutive_failures = 0
    _last_heartbeat_min = [-1]  # list-of-one so the inner loop can mutate it
    try:
        while not _shutdown.is_set():
            # Resolve live every tick (ICP > env > default). Cheap; lets
            # an operator change poll cadence / failure tolerance from
            # Odoo Settings without a pod restart.
            poll_s = _resolve_int_setting(
                registry, "leviathan.worker_poll_s",
                "LEVIATHAN_WORKER_POLL_S", POLL_INTERVAL_S,
            )
            fail_limit = _resolve_int_setting(
                registry, "leviathan.worker_claim_fail_limit",
                "LEVIATHAN_WORKER_CLAIM_FAIL_LIMIT", CLAIM_FAILURE_LIMIT,
            )
            try:
                depth = _drainer_tick(registry, db_name)
                consecutive_failures = 0
                # depth-only periodic log; the drain body itself logs the
                # per-tick claim count (see leviathan_job.
                # _prd_queue_claim_and_dispatch). Suppress when idle to
                # keep the log readable, but heartbeat once a minute so
                # the operator can confirm the loop is alive.
                now_min = int(time.monotonic() // 60)
                if depth:
                    _logger.info("queue depth=%d (post-tick)", depth)
                elif now_min != _last_heartbeat_min[0]:
                    _logger.info("idle (queue empty)")
                    _last_heartbeat_min[0] = now_min
            except Exception:
                consecutive_failures += 1
                _logger.exception(
                    "drainer tick failed (%d/%d consecutive); backing off %ds",
                    consecutive_failures, fail_limit, poll_s,
                )
                # After repeated failures the registry's cached Postgres
                # connections are almost certainly stale (RDS failover,
                # network blip, db_maxconn exhaustion). Exit non-zero so
                # K8s replaces the pod with a fresh registry rather than
                # log-spamming forever while the queue grows.
                if consecutive_failures >= fail_limit:
                    _logger.error(
                        "drainer failed %d times in a row - exiting "
                        "non-zero so the Deployment replaces this pod. "
                        "Stale-heartbeat recovery on the next worker tick "
                        "will re-claim any in-flight jobs.",
                        consecutive_failures,
                    )
                    return 4
            _shutdown.wait(timeout=poll_s)
    finally:
        # Resolve shutdown budget at SIGTERM time so an operator can
        # extend the drain window during a planned rollout without
        # rebuilding the image.
        shutdown_timeout = _resolve_int_setting(
            registry, "leviathan.worker_shutdown_timeout_s",
            "LEVIATHAN_WORKER_SHUTDOWN_TIMEOUT_S", SHUTDOWN_TIMEOUT_S,
        )
        _logger.info(
            "shutdown requested - bounded drain (budget %ds)",
            shutdown_timeout,
        )
        _drain_pool(shutdown_timeout)
        _logger.info("worker exiting")

    return 0


if __name__ == "__main__":
    sys.exit(main())
