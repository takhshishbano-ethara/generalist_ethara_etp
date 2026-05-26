#!/usr/bin/env python3
"""Vegeta PRD Worker — long-lived pool daemon (19.0.2.5.0).

Replaces the one-job-per-pod pattern (v19.0.2.4.0). The worker now boots
ONCE, opens a fixed-size thread pool, and continuously claims pending PRD
jobs from Postgres via ``SELECT ... FOR UPDATE SKIP LOCKED``. Each claimed
job runs the same ``vegeta.job._run_prd_generation_bg`` logic as before —
the per-job pipeline is unchanged; only the dispatch model is.

Why this exists:
    The previous K8s-Job-per-task design spawned one pod (with an Odoo
    cold-start of ~30-60 s) per PRD task. At 500 tasks/day this is wasted
    capacity. A small fixed number of long-lived pods, each handling N
    jobs concurrently, gives the same isolation with far less overhead
    and a predictable cluster footprint (Karpenter still provisions
    nodes for the pods; we just need fewer pods).

Lifecycle:
    1. Boot the Odoo registry (one-time cost per pod).
    2. Install a SIGTERM handler that sets a shutdown event.
    3. Loop:
        a. Claim up to CLAIM_BATCH unclaimed jobs from the queue
           (state='generating' AND job_name IS NULL) via SELECT FOR
           UPDATE SKIP LOCKED — race-safe across many concurrent workers
           and multiple pods.
        b. Submit each claimed job to the pool.
           ``_run_prd_generation_bg`` manages its own short-lived cursors
           and heartbeat thread.
        c. When no work is found, sleep POLL_INTERVAL_S.
    4. On SIGTERM: stop claiming new jobs, wait for in-flight to drain up
       to SHUTDOWN_TIMEOUT_S, then exit. Any job still running past the
       drain timeout is left alone — its heartbeat goes stale and the
       Odoo reconcile cron detects and requeues it.

Required env vars: ``ODOO_DB``.
Optional env vars:
    ``ODOO_CONF`` (default ``/etc/odoo/odoo.conf``)
    ``DB_HOST`` / ``DB_PORT`` / ``DB_USER`` / ``DB_PASSWORD``
    ``VEGETA_WORKER_CONCURRENCY``        (default 50)
    ``VEGETA_WORKER_CLAIM_BATCH``        (default 10)
    ``VEGETA_WORKER_POLL_S``             (default 5)
    ``VEGETA_WORKER_SHUTDOWN_TIMEOUT_S`` (default 1800)
"""

import logging
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [vegeta-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger("vegeta.worker")


WORKER_CONCURRENCY = int(os.environ.get("VEGETA_WORKER_CONCURRENCY", "100"))
CLAIM_BATCH_SIZE = int(os.environ.get("VEGETA_WORKER_CLAIM_BATCH", "10"))
POLL_INTERVAL_S = int(os.environ.get("VEGETA_WORKER_POLL_S", "5"))
SHUTDOWN_TIMEOUT_S = int(
    os.environ.get("VEGETA_WORKER_SHUTDOWN_TIMEOUT_S", "1800")
)
# H3 fix: claim batch is capped at the Bedrock semaphore size so workers
# don't pile up jobs that can't progress past the per-pod Bedrock cap.
# Otherwise at low scale (1 pod) the daemon claims 100, only 22 actively
# run, 78 hold cursors waiting on a semaphore — and the scaler sees
# load=100 and decides no-scale-up is needed because 100/100=1 pod.
BEDROCK_MAX_CONCURRENT = int(
    os.environ.get("VEGETA_BEDROCK_MAX_CONCURRENT", "22")
)
# H5 fix: after this many consecutive claim failures, exit non-zero so
# the Deployment respawns a pod with a fresh registry. The previous code
# logged + retried forever, masking RDS failovers as "no work to do".
CLAIM_FAILURE_LIMIT = int(os.environ.get("VEGETA_WORKER_CLAIM_FAIL_LIMIT", "5"))

# H6 fix: claim SQL exported so tests import the exact same string the
# worker runs. The previous test_worker_scaler duplicated this SQL
# inside TestWorkerClaimSQL; any drift between the two passed CI but
# broke prod. Two queries because PG can't combine SELECT FOR UPDATE
# with the dependent UPDATE in one statement portably.
CLAIM_SELECT_SQL = """
    SELECT id
      FROM vegeta_job
     WHERE state = 'generating'
       AND (job_name IS NULL OR job_name = '')
       AND cancel_requested = FALSE
     ORDER BY id
       FOR UPDATE SKIP LOCKED
     LIMIT %s
"""
CLAIM_UPDATE_SQL = """
    UPDATE vegeta_job
       SET job_name = %s,
           last_heartbeat = (now() AT TIME ZONE 'UTC'),
           started_processing_at = COALESCE(
               started_processing_at, (now() AT TIME ZONE 'UTC')
           )
     WHERE id = ANY(%s)
"""

_WORKER_LABEL = f"worker-{socket.gethostname()}-{os.getpid()}"

_shutdown = threading.Event()


def _boot_odoo(db_name, conf_path=None):
    conf_path = conf_path or os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf")
    # Explicit --addons-path overrides whatever is in odoo.conf so this
    # worker is portable across environments. src/odoo.conf has a stale
    # macOS dev path (/Users/apple/...) that gets baked into the image
    # via COPY src/; without this override, Odoo can't find vegeta in the
    # container and the registry boots without it — every job_claim then
    # crashes on env["vegeta.job"]. Override via ODOO_ADDONS_PATH for
    # environments with different layout.
    addons_path = os.environ.get(
        "ODOO_ADDONS_PATH", "/opt/odoo/addons,/opt/odoo/custom_addons",
    )
    sys.argv = [
        "odoo", "--no-http",
        f"--config={conf_path}",
        f"--database={db_name}",
        f"--addons-path={addons_path}",
    ]

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


def _claim_jobs(registry, batch_size, worker_label):
    """Atomically claim up to ``batch_size`` pending PRD jobs.

    SELECT FOR UPDATE SKIP LOCKED guarantees that two concurrent workers
    (in the same pod OR across pods) never claim the same row. The UPDATE
    stamps the worker label into ``job_name`` and refreshes
    ``last_heartbeat`` so the reconcile/watchdog crons see fresh ownership
    immediately and don't requeue a freshly-claimed job.
    """
    with registry.cursor() as cr:
        cr.execute(CLAIM_SELECT_SQL, (batch_size,))
        rows = cr.fetchall()
        if not rows:
            cr.commit()
            return []
        ids = [r[0] for r in rows]
        cr.execute(CLAIM_UPDATE_SQL, (worker_label, ids))
        cr.commit()
        return ids


def _run_one(registry, db_name, job_id):
    """Run a single PRD job. Called from a pool thread.

    Holds one cursor for the full ``_run_prd_generation_bg`` lifetime
    (~13 min typical) — same pattern the per-job worker used, just now
    multiplied by ``WORKER_CONCURRENCY``. Per-pod DB connection cost:
    ``WORKER_CONCURRENCY`` (one parking cursor per running job) plus
    ``_write_with_cursor`` internal cursors (short-lived). Set
    ``db_maxconn`` on the Odoo backend so 3 pods × 50 workers fits
    comfortably under Postgres ``max_connections`` (see DEVOPS-HANDOFF).

    Any exception escaping ``_run_prd_generation_bg`` is logged — the
    method is responsible for writing a terminal ``failed`` state itself.
    Jobs left with a stale heartbeat (worker crash / hard kill) are
    detected and requeued by the Odoo reconcile cron.
    """
    from odoo import api, SUPERUSER_ID

    t0 = time.monotonic()
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            job = env["vegeta.job"].browse(job_id)
            if not job.exists():
                _logger.warning(
                    "[job=%s] disappeared after claim — abandoning", job_id,
                )
                return
            _logger.info("[job=%s] worker starting PRD generation", job_id)
            job._run_prd_generation_bg(db_name, job_id)
    except Exception:
        _logger.exception(
            "[job=%s] unhandled exception in _run_prd_generation_bg "
            "after %.1fs", job_id, time.monotonic() - t0,
        )
        return
    _logger.info(
        "[job=%s] worker finished PRD generation in %.1fs",
        job_id, time.monotonic() - t0,
    )


def _on_signal(signum, _frame):
    if not _shutdown.is_set():
        _logger.warning(
            "Received signal %d — stopping claim loop, draining in-flight "
            "jobs (timeout %ds)", signum, SHUTDOWN_TIMEOUT_S,
        )
        _shutdown.set()


def main():
    db_name = os.environ.get("ODOO_DB")
    if not db_name:
        _logger.error("ODOO_DB environment variable is required")
        sys.exit(1)

    _logger.info(
        "Worker booting: db=%s concurrency=%d claim_batch=%d "
        "poll_interval=%ds label=%s",
        db_name, WORKER_CONCURRENCY, CLAIM_BATCH_SIZE, POLL_INTERVAL_S,
        _WORKER_LABEL,
    )

    try:
        registry = _boot_odoo(db_name)
    except Exception:
        _logger.exception("Failed to boot Odoo registry")
        sys.exit(2)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    pool = ThreadPoolExecutor(
        max_workers=WORKER_CONCURRENCY,
        thread_name_prefix="vegeta-prd",
    )
    in_flight = 0
    in_flight_lock = threading.Lock()
    active_futures = set()
    active_futures_lock = threading.Lock()

    def _track_done(fut):
        nonlocal in_flight
        with in_flight_lock:
            in_flight -= 1
        with active_futures_lock:
            active_futures.discard(fut)

    consecutive_claim_failures = 0
    try:
        while not _shutdown.is_set():
            with in_flight_lock:
                slots_free = WORKER_CONCURRENCY - in_flight
            if slots_free <= 0:
                _shutdown.wait(timeout=1)
                continue
            # H3 fix: cap claim batch at the Bedrock semaphore size so jobs
            # don't pile up waiting on a semaphore (see CLAIM_BATCH_SIZE
            # comment near module top). The min() chain enforces:
            #   batch ≤ claim batch tunable
            #   batch ≤ free worker-thread slots
            #   batch ≤ Bedrock-semaphore room (per-pod cap)
            batch = min(CLAIM_BATCH_SIZE, slots_free, BEDROCK_MAX_CONCURRENT)
            try:
                ids = _claim_jobs(registry, batch, _WORKER_LABEL)
                consecutive_claim_failures = 0
            except Exception:
                consecutive_claim_failures += 1
                _logger.exception(
                    "claim loop failed (%d/%d consecutive); backing off %ds",
                    consecutive_claim_failures, CLAIM_FAILURE_LIMIT,
                    POLL_INTERVAL_S,
                )
                # H5 fix: after CLAIM_FAILURE_LIMIT consecutive failures, the
                # Odoo registry's cached Postgres connections are almost
                # certainly stale (RDS failover, network blip, db_maxconn
                # exhaustion). Exit non-zero so K8s respawns a pod with a
                # fresh registry rather than logging "claim failed" forever
                # while the queue grows.
                if consecutive_claim_failures >= CLAIM_FAILURE_LIMIT:
                    _logger.error(
                        "claim loop failed %d times in a row — exiting "
                        "non-zero so Deployment replaces this pod with a "
                        "fresh Odoo registry. Reconcile cron will recover "
                        "any in-flight jobs via stale-heartbeat detection.",
                        consecutive_claim_failures,
                    )
                    sys.exit(4)
                _shutdown.wait(timeout=POLL_INTERVAL_S)
                continue

            if not ids:
                _shutdown.wait(timeout=POLL_INTERVAL_S)
                continue

            _logger.info(
                "claimed %d job(s): %s (in_flight=%d/%d)",
                len(ids), ids, in_flight, WORKER_CONCURRENCY,
            )
            for job_id in ids:
                with in_flight_lock:
                    in_flight += 1
                fut = pool.submit(_run_one, registry, db_name, job_id)
                with active_futures_lock:
                    active_futures.add(fut)
                fut.add_done_callback(_track_done)
    finally:
        with active_futures_lock:
            pending_snapshot = {f for f in active_futures if not f.done()}
        _logger.info(
            "shutdown requested — %d in-flight job(s), bounded drain "
            "(timeout %ds)", len(pending_snapshot), SHUTDOWN_TIMEOUT_S,
        )
        # Bounded drain: wait up to SHUTDOWN_TIMEOUT_S for futures to finish,
        # then ABANDON the rest. The previous pool.shutdown(wait=True) had
        # no per-future timeout — a single hung Bedrock call (network glitch,
        # regional brownout) would block the whole pod past
        # terminationGracePeriodSeconds, getting SIGKILLed without telling
        # us which job hung. Surfacing the abandoned list lets the reconcile
        # cron's stale-heartbeat path recover those jobs cleanly.
        if pending_snapshot:
            _, not_done = futures_wait(
                pending_snapshot, timeout=SHUTDOWN_TIMEOUT_S,
            )
            if not_done:
                _logger.warning(
                    "shutdown: %d job(s) STILL RUNNING after %ds drain "
                    "timeout — abandoning to SIGKILL; reconcile cron will "
                    "re-queue them via stale-heartbeat detection",
                    len(not_done), SHUTDOWN_TIMEOUT_S,
                )
        # wait=False because we've already drained to the budget above —
        # this just releases the pool's internal resources without blocking
        # again on any future that's still running.
        pool.shutdown(wait=False, cancel_futures=False)
        _logger.info("pool drained — exiting")

    sys.exit(0)


if __name__ == "__main__":
    main()
