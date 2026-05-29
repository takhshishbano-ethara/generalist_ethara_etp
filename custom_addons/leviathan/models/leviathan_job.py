import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

_PRD_POOL_SIZE = int(os.environ.get("LEVIATHAN_PRD_POOL_SIZE", "100"))
_BATCH_FANOUT_POOL_SIZE = int(os.environ.get("LEVIATHAN_BATCH_FANOUT_SIZE", "250"))

# P0-3 admission ceiling: refuse to queue new heavy work once the pool's
# in-RAM backlog exceeds this. The job stays in its DB state and the watchdog
# re-dispatches it — far safer than the 443-deep queue that OOM-ed the pod on
# 21 May. See docs/BATCH_500_TEST_PLAN.md.
_PRD_ADMISSION_MAX = int(os.environ.get(
    "LEVIATHAN_PRD_ADMISSION_MAX", str(_PRD_POOL_SIZE * 3)))

# P0-2: task-label prefixes cheap enough (<1s) to safely run inline if the
# pool rejects. prd-gen / extract are NEVER inline — a multi-minute Bedrock
# call in an HTTP worker thread is what collapsed the instance on 21 May.
_INLINE_SAFE = ("artifacts-upload",)

# === Phase-2 PRD-queue in-flight counter (see docs/LEVIATHAN_POD_ARCHITECTURE.md §5.5) ==
# Per-process counter: PRD-gen tasks currently in the pool (running OR queued
# inside the executor — practically the same since the drainer only submits
# `free` jobs, so the queue stays ~0). The drainer claims at most
# `_PRD_POOL_SIZE - _prd_inflight_count()` rows per tick. Correctness comes
# from the DB row state (heartbeat + claim_count); this counter is just a
# rate-limiter. A pod restart resets it to 0 — that is fine, the DB-driven
# recovery (§5.6) re-syncs reality on the next tick.
_prd_inflight = 0
_prd_inflight_lock = threading.Lock()


def _prd_inflight_count() -> int:
    with _prd_inflight_lock:
        return _prd_inflight


def _prd_inflight_inc():
    global _prd_inflight
    with _prd_inflight_lock:
        _prd_inflight += 1


def _prd_inflight_dec():
    global _prd_inflight
    with _prd_inflight_lock:
        # Clamp to 0 defensively — should never go negative, but a bug would
        # only manifest as the drainer over-claiming, never under-claiming.
        _prd_inflight = max(0, _prd_inflight - 1)

# Per-pid pool registry. Odoo forks N worker processes from the master; a
# module-level ThreadPoolExecutor instantiated at import time would either
# be shared (broken — file descriptors don't survive fork cleanly) or, more
# commonly, lazily re-instantiated per fork at first .submit() call. With
# 3 pods × 4 Odoo workers × _PRD_POOL_SIZE=50, the latter silently gives
# you 600 PRD threads system-wide, each grabbing DB cursors for heartbeats
# — which is exactly the cursor-pool exhaustion you saw at 200-concurrent.
# Keying the pool on os.getpid() makes the multiplication explicit and lets
# operators size _PRD_POOL_SIZE × workers × pods against db_maxconn rather
# than guessing.
_POOL_REGISTRY: dict[int, ThreadPoolExecutor] = {}
_POOL_REGISTRY_LOCK = threading.Lock()


def _pool_is_dead(pool) -> bool:
    """A ThreadPoolExecutor is unusable once a worker thread failed to spawn
    (`_broken` — every subsequent submit() raises) or it was shut down
    (`_shutdown`). Either way it must be discarded and rebuilt."""
    return bool(
        getattr(pool, "_broken", None) or getattr(pool, "_shutdown", False)
    )


def _get_pool() -> ThreadPoolExecutor:
    """Return the ThreadPoolExecutor for the current process, creating it
    lazily on first call. Safe across Odoo's prefork-style worker model.

    P0-1: a `_broken`/`_shutdown` pool is detected and rebuilt rather than
    served forever — on 21 May the dead pool was handed out for 80 minutes,
    forcing every job onto the (catastrophic) inline fallback.
    """
    pid = os.getpid()
    pool = _POOL_REGISTRY.get(pid)
    if pool is not None and not _pool_is_dead(pool):
        return pool
    with _POOL_REGISTRY_LOCK:
        pool = _POOL_REGISTRY.get(pid)
        if pool is not None and not _pool_is_dead(pool):
            return pool
        if pool is not None:
            _logger.error(
                "[leviathan] PRD pool for pid=%d is DEAD (broken/shutdown) — "
                "rebuilding", pid,
            )
        pool = ThreadPoolExecutor(
            max_workers=_PRD_POOL_SIZE,
            thread_name_prefix=f"leviathan-prd[pid={pid}]",
        )
        _POOL_REGISTRY[pid] = pool
        _logger.info(
            "[leviathan] PRD pool initialised for pid=%d (max_workers=%d). "
            "If you run M Odoo workers × N pods, total concurrent PRD "
            "threads = %d × M × N — size db_maxconn accordingly.",
            pid, _PRD_POOL_SIZE, _PRD_POOL_SIZE,
        )
    return pool


def _submit_bg(label, fn, *args, **kwargs):
    """Submit a background job to the shared pool — with uptime guarantees.

    - Logs a warning when the pool queue is backing up (saturation) so a slow
      Bedrock/S3 never silently swallows work without a trace.
    - Wraps the callable so any escaped exception is logged instead of being
      lost in an un-awaited Future.
    - P0-2: if the pool rejects the task, heavy work (prd-gen/extract) is
      NEVER run inline — the job is left in its DB state for the watchdog to
      re-dispatch. Only sub-second `_INLINE_SAFE` tasks keep the inline
      fallback. Running a multi-minute Bedrock call inside an HTTP worker
      thread is what collapsed the instance on 21 May.
    - P0-3: heavy work is also refused when the pool backlog exceeds
      `_PRD_ADMISSION_MAX`, again deferring to the watchdog.
    """
    pool = _get_pool()
    qsize = -1
    try:
        qsize = pool._work_queue.qsize()
        if qsize > _PRD_POOL_SIZE:
            _logger.warning(
                "[leviathan] PRD pool saturated: %d queued / %d workers — jobs "
                "will run but are delayed; raise LEVIATHAN_PRD_POOL_SIZE.",
                qsize, _PRD_POOL_SIZE,
            )
    except Exception:
        pass

    # P0-3 admission control: bound the in-RAM backlog. Heavy work past the
    # ceiling is refused here — the job stays in its DB state and the watchdog
    # re-dispatches it. Sub-second `artifacts-upload` tasks are exempt.
    kind = label.split("[", 1)[0]
    if kind not in _INLINE_SAFE and qsize >= _PRD_ADMISSION_MAX:
        _logger.warning(
            "[leviathan] pool admission full (queue=%d >= %d) — deferring "
            "'%s' to watchdog recovery (job left in its DB state)",
            qsize, _PRD_ADMISSION_MAX, label,
        )
        return None

    # Submit-time marker: lets you measure pool queue-wait by diffing this
    # timestamp against the "started" line _guarded() emits below. A large
    # gap = the job sat waiting for a free worker (pool too small / backlog).
    submitted_at = time.monotonic()
    _logger.info(
        "[leviathan] _submit_bg: queued '%s' on pool[pid=%d] "
        "(queue_depth=%d, workers=%d)",
        label, os.getpid(), qsize, _PRD_POOL_SIZE,
    )

    def _guarded():
        wait_s = time.monotonic() - submitted_at
        _logger.info(
            "[leviathan] bg task '%s' STARTED (pool queue-wait=%.1fs)",
            label, wait_s,
        )
        # Phase-2: track in-flight count for the queue drainer's free-capacity
        # math (§5.5). Increment when the worker actually starts (avoids a
        # race with pool.submit returning slowly). Decrement in finally.
        is_prd_gen = (kind == "prd-gen")
        if is_prd_gen:
            _prd_inflight_inc()
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except Exception:
            _logger.exception("[leviathan] background task '%s' crashed", label)
        finally:
            if is_prd_gen:
                _prd_inflight_dec()
            _logger.info(
                "[leviathan] bg task '%s' FINISHED (ran %.1fs, queue-wait %.1fs)",
                label, time.monotonic() - t0, wait_s,
            )

    try:
        return pool.submit(_guarded)
    except RuntimeError as exc:
        # P0-2: the pool rejected the task (broken / shut down). Heavy tasks
        # (prd-gen, extract) must NEVER run inline — a multi-minute Bedrock
        # call inside an HTTP worker thread is exactly what drained
        # db_maxconn and collapsed the whole instance on 21 May. Leave the
        # job in its DB state; the watchdog re-dispatches it. Only the
        # sub-second `artifacts-upload` task keeps the inline fallback.
        if kind in _INLINE_SAFE:
            _logger.error(
                "[leviathan] pool rejected '%s' (%s) — running inline "
                "(inline-safe task)", label, exc,
            )
            _guarded()
        else:
            _logger.error(
                "[leviathan] pool rejected '%s' (%s) — NOT run inline; left "
                "for watchdog recovery", label, exc,
            )
        return None


_HEARTBEAT_INTERVAL_S = int(os.environ.get("LEVIATHAN_HEARTBEAT_INTERVAL_S", "60"))
_HEARTBEAT_MODE = os.environ.get("LEVIATHAN_HEARTBEAT_MODE", "aggregator").lower()


class _HeartbeatManager:
    """Process-wide shared heartbeat pulser.

    Replaces the per-job daemon-thread pattern that burned one thread AND
    one DB cursor every 60s for every active job. With 50 PRD workers
    running we were spawning 50 heartbeat threads competing for cursors
    against the workers themselves — the cursor pool (db_maxconn) was
    being drained by heartbeats alone, leaving real Odoo requests waiting.

    Design:
      * ONE daemon thread per Python process, lazily started on first
        register() call. Idle-exits when the registry empties; respawns
        on the next register(). No idle thread between batches.
      * Registry: ``set[(db_name, record_id)]``. register() / unregister()
        are O(1) under a single Lock. Multi-tenant safe (db_name keyed).
      * Every interval (LEVIATHAN_HEARTBEAT_INTERVAL_S, default 60s),
        the thread snapshots the registry, groups by db_name, and for
        each group issues ONE batched UPDATE filtered by state IN
        ('extracting','generating','scoring'). Terminal jobs never get
        pinged even if a worker forgot to unregister.
      * Self-heal: outer try/except in the loop. Any error (DB drop,
        registry corruption) is logged and the next tick continues.
        The thread MUST NOT die — the watchdog cron (5min) is the
        secondary safety net for jobs that go quiet.
      * Backward-compat: jobs running under the legacy per-job daemon
        at deploy time keep their own daemons and drain naturally
        (worst case ~9 min for PRD). New jobs route through the manager
        unless LEVIATHAN_HEARTBEAT_MODE=per_job.
    """

    __slots__ = ("_active", "_lock", "_interval", "_stop_event", "_thread")

    def __init__(self, interval=_HEARTBEAT_INTERVAL_S):
        self._active: set[tuple[str, int]] = set()
        self._lock = threading.Lock()
        self._interval = max(5, int(interval))
        self._stop_event = threading.Event()
        self._thread = None

    def register(self, db_name, record_id):
        if not record_id or not db_name:
            return
        with self._lock:
            self._active.add((db_name, record_id))
            self._ensure_thread_locked()

    def unregister(self, db_name, record_id):
        if not record_id or not db_name:
            return
        with self._lock:
            self._active.discard((db_name, record_id))

    def _ensure_thread_locked(self):
        # Called with self._lock held. Lazy-start the thread on the first
        # registration; respawn if a prior thread idle-exited.
        t = self._thread
        if t is not None and t.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="leviathan-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _run(self):
        while not self._stop_event.wait(self._interval):
            try:
                # Snapshot under lock; idle-exit if empty.
                with self._lock:
                    if not self._active:
                        self._thread = None
                        return
                    groups: dict[str, list[int]] = {}
                    for db_name, rid in self._active:
                        groups.setdefault(db_name, []).append(rid)

                for db_name, ids in groups.items():
                    try:
                        self._bulk_pulse(db_name, ids)
                    except Exception:
                        _logger.debug(
                            "[leviathan] heartbeat pulse failed for db=%s "
                            "ids=%s (will retry next tick)",
                            db_name, ids, exc_info=True,
                        )
                        # Two-gate-reconcile feeder: when the bulk UPDATE
                        # raises (DB blip, lock contention, RDS failover),
                        # every in-flight job in this batch just missed
                        # its heartbeat. Bump heartbeat_failure_count for
                        # all of them so the recovery query in
                        # ``_prd_queue_recover_stale`` can fire the
                        # short-stale gate (5 min + failures ≥ 3) instead
                        # of waiting the full 15-min unconditional gate.
                        # This separate UPDATE uses its own fresh cursor
                        # — if THIS one also fails, swallow silently
                        # (best-effort; the unconditional gate is the
                        # backstop).
                        try:
                            with Registry(db_name).cursor() as cr:
                                cr.execute(
                                    "UPDATE leviathan_job "
                                    "SET heartbeat_failure_count = "
                                    "    heartbeat_failure_count + 1 "
                                    "WHERE id = ANY(%s) "
                                    "  AND state IN ('extracting', "
                                    "    'generating', 'scoring', "
                                    "    'qc_running')",
                                    (list(ids),),
                                )
                                cr.commit()
                        except Exception:
                            pass
            except Exception:
                # Catch-all so a bad tick never kills the thread.
                # Watchdog cron is the real safety net.
                _logger.exception(
                    "[leviathan] heartbeat manager tick crashed; continuing",
                )

    def _bulk_pulse(self, db_name, record_ids):
        registry = Registry(db_name)
        with registry.cursor() as cr:
            cr.execute(
                "UPDATE leviathan_job "
                "SET last_heartbeat = (now() AT TIME ZONE 'UTC'), "
                # Reset the failure counter on every successful pulse —
                # paired with the +1 bump in the aggregator's catch
                # path above. Net effect: heartbeat_failure_count
                # accumulates only across CONSECUTIVE failures, never
                # across an isolated DB blip recovered on the next tick.
                "    heartbeat_failure_count = 0 "
                "WHERE id = ANY(%s) "
                # qc_running included so a multi-minute QC call doesn't go
                # silent and get false-recovered by the drainer (§5.12).
                "  AND state IN ('extracting', 'generating', 'scoring', 'qc_running')",
                (list(record_ids),),
            )
            pulsed = cr.rowcount
            cr.commit()
        # registered != pulsed means some registered jobs are already in a
        # terminal state — a worker finished but did not unregister, or the
        # job was cancelled out from under a live worker. Harmless (terminal
        # rows are filtered by the WHERE clause) but worth seeing in the log.
        _logger.info(
            "[leviathan] heartbeat pulse: db=%s registered=%d pulsed=%d",
            db_name, len(record_ids), pulsed,
        )


_HEARTBEAT_MGR = _HeartbeatManager()


class _HeartbeatTicker:
    """Backward-compat facade so call sites don't change.

    By default (LEVIATHAN_HEARTBEAT_MODE=aggregator) this is a thin wrapper
    that register/unregisters the job with the shared _HEARTBEAT_MGR. If
    operations needs to roll back to per-job daemons (e.g. for debugging
    the aggregator under load), set LEVIATHAN_HEARTBEAT_MODE=per_job and
    redeploy — the call sites stay identical.
    """

    __slots__ = ("_model", "_db_name", "_record_id", "_interval",
                 "_mode", "_stop_event", "_thread")

    def __init__(self, model, db_name, record_id, interval=_HEARTBEAT_INTERVAL_S):
        self._model = model
        self._db_name = db_name
        self._record_id = record_id
        self._interval = interval
        self._mode = _HEARTBEAT_MODE
        self._stop_event = threading.Event()
        self._thread = None

    def __enter__(self):
        if self._mode == "per_job":
            self._thread = threading.Thread(
                target=self._run_legacy,
                name=f"leviathan-hb[job={self._record_id}]",
                daemon=True,
            )
            self._thread.start()
        else:
            _HEARTBEAT_MGR.register(self._db_name, self._record_id)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._mode == "per_job":
            self._stop_event.set()
            if self._thread is not None:
                self._thread.join(timeout=2)
        else:
            _HEARTBEAT_MGR.unregister(self._db_name, self._record_id)
        return False

    def _run_legacy(self):
        # Old per-job daemon body kept for the LEVIATHAN_HEARTBEAT_MODE=per_job
        # rollback switch. Identical to the pre-hotfix _HeartbeatTicker._run.
        while not self._stop_event.wait(self._interval):
            try:
                self._model._write_with_cursor(
                    self._db_name,
                    self._record_id,
                    {"last_heartbeat": fields.Datetime.now()},
                )
            except Exception:
                _logger.debug(
                    "[leviathan][job=%s] heartbeat pulse failed (will retry)",
                    self._record_id, exc_info=True,
                )


# Bedrock Claude rejects images where either dimension exceeds 8000 px with:
#   "messages.x.content.y.image.source.bytes: At least one of the image
#    dimensions exceed max allowed size: 8000 pixels"
# Full-page screenshots from the Lambda routinely exceed this. We downsample
# to a safe maximum (well under 8000) preserving aspect ratio. PIL ships with
# Odoo, so the import is free.
_BEDROCK_MAX_IMAGE_DIM = 7800  # px, leaves margin under the 8000 hard limit


def _resize_image_for_bedrock(img_bytes: bytes, fmt: str) -> bytes:
    """Return ``img_bytes`` unchanged if both dimensions are <= the Bedrock cap,
    otherwise return a downscaled copy in the same format.

    Aspect ratio is preserved. On any decode/encode error returns the original
    bytes — better to let Bedrock reject one image than to drop the whole
    request from a PIL edge case.
    """
    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(img_bytes)) as im:
            w, h = im.size
            if w <= _BEDROCK_MAX_IMAGE_DIM and h <= _BEDROCK_MAX_IMAGE_DIM:
                return img_bytes
            im.thumbnail(
                (_BEDROCK_MAX_IMAGE_DIM, _BEDROCK_MAX_IMAGE_DIM),
                Image.LANCZOS,
            )
            buf = _io.BytesIO()
            pil_fmt = "JPEG" if fmt.lower() in ("jpg", "jpeg") else fmt.upper()
            save_kwargs = {"optimize": True}
            if pil_fmt == "JPEG":
                save_kwargs["quality"] = 85
                if im.mode in ("RGBA", "P"):
                    im = im.convert("RGB")
            im.save(buf, format=pil_fmt, **save_kwargs)
            new_bytes = buf.getvalue()
            _logger.info(
                "[leviathan] resized image %dx%d -> %dx%d (%d -> %d bytes) for Bedrock",
                w, h, im.size[0], im.size[1], len(img_bytes), len(new_bytes),
            )
            return new_bytes
    except Exception as exc:
        _logger.warning(
            "[leviathan] image resize failed (%s) — sending original; Bedrock "
            "may reject if >8000px", exc,
        )
        return img_bytes


def _load_screenshot_blocks_from_s3(
    screenshot_keys,
    max_n,
    s3_bucket,
    s3_region="us-east-1",
    s3_access_key_id="",
    s3_secret_access_key="",
    log_prefix="leviathan",
):
    """Return Bedrock Converse image content blocks ready for the
    ``messages[].content`` array, OR an empty list on any error.

    Best-effort visual enrichment — if S3 is misconfigured, the bucket
    is empty, or PIL chokes on one file, we return [] and the caller's
    PRD/QC request goes through text-only. **Never raises.** Image
    attachment is opt-in via Settings; the cost of a failure must never
    be losing the whole PRD generation.

    Output block shape (Bedrock Converse API):

        {"image": {"format": "png", "source": {"bytes": "<b64_string>"}}}

    The bytes field is **base64-encoded string**, not raw bytes. This is
    what the bearer-auth httpx path needs (JSON serialization can't
    carry raw bytes). If/when the SigV4 (boto3) path is wired for
    images, it will need a single ``base64.b64decode`` step before
    forwarding to ``client.converse``; for now bearer is the only
    image-capable path and that's all we need.

    Args:
        screenshot_keys: list of S3 keys (typically ``record.screenshot_keys``).
            Order is honoured — the first ``max_n`` are taken.
        max_n: hard cap on how many images we load + attach.
        s3_bucket / s3_region / s3_access_key_id / s3_secret_access_key:
            connection parameters; empty creds fall back to IRSA / pod role.
        log_prefix: prepended to log lines for grep'ability.
    """
    if not screenshot_keys or max_n <= 0 or not s3_bucket:
        return []

    import base64
    try:
        from ..services.s3_service import download_file_from_s3
    except Exception:
        _logger.warning(
            "[%s] cannot import s3_service — returning empty screenshot "
            "blocks; falling back to text-only request", log_prefix,
        )
        return []

    blocks = []
    for key in screenshot_keys[:max_n]:
        try:
            raw = download_file_from_s3(
                key=key,
                bucket=s3_bucket,
                access_key_id=s3_access_key_id or None,
                secret_key=s3_access_key_id and s3_secret_access_key or None,
                region=s3_region or "us-east-1",
            )
        except Exception as exc:
            _logger.warning(
                "[%s] screenshot S3 download failed for %s: %s — skipping",
                log_prefix, key, exc,
            )
            continue

        # Detect format from the file extension on the key; fall back to
        # PNG (the dominant format for our scroll-tile and hero captures).
        ext = (key.rsplit(".", 1)[-1] or "").lower()
        fmt = ext if ext in ("png", "jpeg", "jpg", "webp", "gif") else "png"
        # Bedrock spells JPEG as "jpeg", not "jpg".
        if fmt == "jpg":
            fmt = "jpeg"

        # Resize over the Bedrock 8000-px-per-side cap; safe on errors.
        resized = _resize_image_for_bedrock(raw, fmt)

        try:
            b64 = base64.b64encode(resized).decode("ascii")
        except Exception as exc:
            _logger.warning(
                "[%s] base64 encode failed for %s: %s — skipping",
                log_prefix, key, exc,
            )
            continue

        blocks.append({
            "image": {
                "format": fmt,
                "source": {"bytes": b64},
            },
        })

    if blocks:
        _logger.info(
            "[%s] attached %d screenshot block(s) to Bedrock content "
            "(requested up to %d, S3 keys available: %d)",
            log_prefix, len(blocks), max_n, len(screenshot_keys),
        )
    return blocks


class LeviathanJob(models.Model):
    _name = "leviathan.job"
    _description = "Leviathan Pipeline Task"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"

    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        copy=False,
        default="New",
        index=True,
    )
    url = fields.Char(string="Website URL", tracking=True)
    site_name = fields.Char(string="Site Name")
    state = fields.Selection(
        [
            ("not_assigned", "Not Assigned"),
            ("draft", "Draft"),
            ("extracting", "Extracting"),
            # Parked: extraction finished, waiting for a human to start PRD
            # generation (staged/manual mode only — auto_continue jobs never
            # park here, they cascade straight to `generating`).
            ("extracted", "Extracted"),
            ("generating", "Generating PRD"),
            # Parked: PRD generated, waiting for a human to run Score.
            ("generated", "Generated"),
            ("scoring", "Scoring"),
            # Parked: rubric score recorded, waiting for a human to run QC.
            ("scored", "Scored"),
            # Running: QC (Bedrock) in progress in the staged/manual flow.
            ("qc_running", "QC Running"),
            ("done", "Done"),
            ("submitted", "Submitted"),
            ("failed", "Failed"),
            ("discarded", "Discarded"),  # tasker: nothing usable / site unsuitable
            ("cancelled", "Cancelled"),  # legacy, hidden from UI
        ],
        string="Status",
        default="not_assigned",
        required=True,
        tracking=True,
    )
    category_id = fields.Many2one("leviathan.category", string="Website Category")
    category_key = fields.Char(related="category_id.technical_key", store=True)
    score = fields.Float(string="PRD Score", digits=(5, 2))
    score_display = fields.Char(
        string="Score", compute="_compute_score_display", store=False,
    )
    grade = fields.Char(string="Grade")
    qc_verdict = fields.Selection(
        [
            ("shippable", "SHIPPABLE"),
            ("fixes", "SHIPPABLE WITH FIXES"),
            ("not_shippable", "NOT SHIPPABLE"),
        ],
        string="QC Verdict",
    )
    prd_text = fields.Text(string="PRD Document")
    prd_text_html = fields.Html(string="PRD Editor", sanitize=False)
    prd_prompt = fields.Text(string="PRD Prompt (Extracted Data)")
    qc_report = fields.Text(string="QC Report")
    score_report_json = fields.Json(string="Score Report")
    tech_stack = fields.Text(string="Tech Stack")
    page_count = fields.Integer(string="Pages Discovered")
    site_discovery_json = fields.Json(string="Site Discovery Data")
    prd_url = fields.Char(string="PRD Download URL")
    artifacts_url = fields.Char(string="Artifacts Folder URL")
    screenshot_keys = fields.Json(string="S3 Screenshot Keys")
    asset_keys = fields.Json(string="S3 Asset Keys")
    deliverables_url = fields.Char(string="Deliverables URL")
    duration_seconds = fields.Float(string="Duration (s)")
    llm_attempts = fields.Integer(string="LLM Attempts")
    error_message = fields.Text(string="Error Message")
    extraction_warnings = fields.Text(
        string="Extraction Warnings",
        help="Non-fatal warnings from extraction (e.g. low screenshot count). "
             "A job with warnings is still a success — shown as a yellow banner, "
             "not a red failure.",
    )
    lambda_callback_json = fields.Json(
        string="Lambda Callback (raw)",
        help="Full payload the extraction Lambda posted back — for transparency/audit.",
    )
    llm_trace_json = fields.Json(
        string="LLM Trace (raw)",
        help="Every PRD-generation attempt + QC request/response — for transparency/audit.",
    )
    started_at = fields.Datetime(string="Started At")
    completed_at = fields.Datetime(string="Completed At")
    last_heartbeat = fields.Datetime(string="Last Heartbeat")
    # Set when a background worker actually picks the job up off the pool queue
    # (entry to `_run_prd_generation_bg`). Distinct from `started_at` (set at
    # batch dispatch). The watchdog uses this to tell "queued waiting for a
    # worker" from "actually running and stuck" — without it, jobs sitting in
    # _POOL._work_queue for >45 min get false-failed even though no work has
    # been attempted on them yet.
    started_processing_at = fields.Datetime(string="Worker Picked Up At")
    # Counts how many times the watchdog has auto-retried this job. The
    # watchdog gives a stuck job ONE free retry before marking it failed
    # for real — covers the 1% of legit Bedrock/Lambda hiccups without
    # requiring an admin click for every stuck job. Cap is System Parameter
    # ``leviathan.watchdog_auto_retry_max`` (default 1, set 0 to disable).
    watchdog_retry_count = fields.Integer(
        string="Watchdog Auto-Retries",
        default=0,
        copy=False,
        help="Number of times the watchdog has auto-retried this job. When "
             "this hits leviathan.watchdog_auto_retry_max, the next watchdog "
             "hit marks the job failed for real.",
    )

    # === Phase-2 PRD-queue fields (see docs/LEVIATHAN_POD_ARCHITECTURE.md §3) ==
    # All four are inert when the feature flag `leviathan.prd_queue_enabled` is
    # False (Phase-1 behaviour unchanged); they activate when the flag flips on.
    prd_queued_at = fields.Datetime(
        string="PRD Queued At",
        copy=False,
        help="Timestamp the job entered the PRD queue (auto pipeline). "
             "Used by the drainer to claim rows FIFO (ORDER BY prd_queued_at "
             "NULLS FIRST). NULL on legacy rows is treated as 'oldest'.",
    )
    # Monotonic fence token — drainer increments on every claim; workers carry
    # the captured value and write conditionally so a stale worker's writes
    # affect 0 rows after a recovery re-claim. See §5.4 / §5.10 / §5.11.
    prd_claim_count = fields.Integer(
        string="PRD Claim Count",
        default=0,
        copy=False,
        help="Fence token. Incremented by the drainer on each claim. Every "
             "worker write is conditional on prd_claim_count = captured value, "
             "so a worker that lost its claim to recovery cannot corrupt the row.",
    )
    # Poison counter — only the worker increments it, only on a genuine
    # exception. Recovery / re-claims do NOT touch it. A job hitting
    # leviathan.prd_max_attempts is marked failed by the drainer. See §5.7.
    prd_failure_count = fields.Integer(
        string="PRD Failure Count",
        default=0,
        copy=False,
        help="Counts genuine PRD-worker exceptions. Hitting "
             "leviathan.prd_max_attempts (default 3) → drainer marks failed. "
             "NOT incremented by pod restarts, recovery, or re-claims.",
    )
    # Tasker-visible 'what is the pipeline doing now'. Short Char, updated at
    # every phase boundary by the worker / dispatcher / webhook. NOT used for
    # any correctness check — informational only. See §5.15.
    pipeline_status = fields.Char(
        string="Pipeline Status",
        copy=False,
        help="Human-readable phase indicator shown under the statusbar. "
             "Examples: 'Generating PRD (Bedrock)', 'QC complete — finalizing'. "
             "Updated at every phase boundary; not load-bearing for correctness.",
    )

    # Sub-step WITHIN a state. Where `pipeline_status` says "Generating PRD",
    # `current_phase` carries the finer-grained "generating.calling_bedrock"
    # or "generating.scoring" — turned into a label via `_PHASE_LABELS` and
    # rendered above the stage timer in `stage_progress_html`. Tasker-facing
    # only; not load-bearing for correctness. Always-non-NULL (default '')
    # so writes never need NULL-guards.
    current_phase = fields.Char(
        string="Current Phase",
        default="",
        readonly=True,
        copy=False,
        help="Internal phase key like 'generating.calling_bedrock' — "
             "translated to a human-readable label via _PHASE_LABELS and "
             "shown in the Stage Progress widget. Tasker-facing only.",
    )

    # AWS Lambda RequestId captured at invoke time. Used by
    # `action_refresh_lambda_logs` to filter CloudWatch events down to
    # this exact invocation. Indexed (partial index via migration) for
    # the reconcile case where we look up jobs by RequestId.
    lambda_request_id = fields.Char(
        string="Lambda RequestId",
        readonly=True,
        copy=False,
        index=True,
        help="AWS Lambda RequestId from the most recent invoke. Used by "
             "the Refresh Lambda Logs button to filter CloudWatch events "
             "for this job.",
    )
    # Watermark for CloudWatch log polling. NULL on first fetch; updated
    # to the timestamp of the most recent event ingested. The fetch path
    # pulls only events newer than this watermark, so a re-click doesn't
    # re-ingest the entire log group.
    last_lambda_log_ts = fields.Datetime(
        string="Last Lambda Log Fetched",
        readonly=True,
        copy=False,
        help="Watermark for CloudWatch log polling; only events after "
             "this timestamp are pulled on the next refresh.",
    )

    # One2many to the per-job execution log table. Populated by:
    #   1. `log_handler.LeviathanJobLogHandler` (auto-scrapes [job=N] tags)
    #   2. `action_refresh_lambda_logs` (CloudWatch pull, admin button)
    # Read-only from the UI — never user-edited.
    log_ids = fields.One2many(
        "leviathan.job.log",
        "job_id",
        string="Execution Logs",
        readonly=True,
    )

    # Consecutive heartbeat-write failures for this row. Bumped by the
    # heartbeat aggregator's catch path when a pulse UPDATE raises;
    # reset to 0 on every successful pulse (the same UPDATE sets both
    # `last_heartbeat` and `heartbeat_failure_count = 0`).
    #
    # The two-gate reconcile in `_prd_queue_recover_stale` reads this:
    # a row with a stale heartbeat AND a failure count above the
    # threshold is recovered EARLY (default 5 min instead of 15) —
    # because we know the worker has been actively failing to pulse,
    # not just slow. Without this column, the single-gate "stale
    # heartbeat alone" check produced the documented double-Bedrock-
    # spend incident in vegeta v19.0.2.4.0.
    heartbeat_failure_count = fields.Integer(
        string="Heartbeat Failure Count",
        default=0,
        readonly=True,
        copy=False,
        help="Consecutive heartbeat-write failures since the last "
             "successful pulse. Bumped by the heartbeat aggregator on "
             "exception, reset to 0 on success. The two-gate reconcile "
             "uses this to short-circuit recovery when a worker is "
             "alive but its heartbeat writes are failing.",
    )


    # Computed HTML for asset previews
    screenshot_urls_html = fields.Html(
        string="Screenshot Previews", compute="_compute_asset_previews",
        sanitize=False,
    )
    asset_urls_html = fields.Html(
        string="Asset Previews", compute="_compute_asset_previews",
        sanitize=False,
    )
    asset_score_html = fields.Html(
        string="Extraction Summary", compute="_compute_asset_score_html",
        sanitize=False,
    )
    score_report_html = fields.Html(
        string="Score Report (HTML)", compute="_compute_score_report_html",
        sanitize=False,
    )
    stage_progress_html = fields.Html(
        string="Stage Progress", compute="_compute_stage_progress",
        sanitize=False,
    )
    cancel_requested = fields.Boolean(default=False)
    via_batch = fields.Boolean(
        string="Triggered via Batch Run",
        default=False,
        copy=False,
        help="True when this job was started by a batch concurrent run. "
             "On completion the job is auto-released to 'not_assigned' so taskers can claim it.",
    )
    auto_continue = fields.Boolean(
        string="Auto-Continue Pipeline",
        default=True,
        copy=False,
        help="When True (Run All / batch / retry / rerun), the pipeline "
             "cascades automatically: extraction -> generation -> scoring -> "
             "QC -> done with no human input. When False (staged manual run), "
             "the job parks after each stage and waits for the matching stage "
             "button. Checked only at the extraction->generation handoff in "
             "the webhook; manual jobs never enter the fused auto pipeline.",
    )

    user_id = fields.Many2one(
        "res.users",
        string="Tasker",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
    )
    is_admin = fields.Boolean(compute="_compute_is_admin")

    _url_required = models.Constraint(
        "CHECK(url IS NOT NULL AND url != '')",
        "Website URL is required!",
    )

    @property
    def _has_extraction_data(self):
        """True if any extraction artifacts exist on this record."""
        return bool(
            self.prd_prompt
            or self.site_discovery_json
            or self.screenshot_keys
            or self.asset_keys
        )

    def _smart_state_on_assign(self):
        """State a task should land in when (re)assigned to a user.

        Released tasks keep their data; on pick-up the state restores so the
        new owner sees the right buttons:
          - prd_text exists           -> done   (Submit / Rerun / Regenerate)
          - extraction data, no PRD   -> failed (Retry opens rerun wizard)
          - nothing                   -> draft  (Run Pipeline)
        """
        self.ensure_one()
        if self.prd_text:
            return "done"
        if self._has_extraction_data:
            return "failed"
        return "draft"

    # ------------------------------------------------------------------
    # Phase-2 PRD-queue feature flag (LEVIATHAN_POD_ARCHITECTURE.md §4)
    # ------------------------------------------------------------------

    @api.model
    def _prd_execution_mode(self):
        """Where the PRD drainer loop runs.

        Returns one of:
          * ``"inprocess"`` (default) — the Odoo ``ir.cron`` runs the drainer
            every minute and submits jobs to the in-process pool. The legacy
            Phase-2 target (single-pod, no separate worker process).
          * ``"worker"`` — a standalone Python process
            (``custom_addons/leviathan/worker/run_prd.py``) owns the claim
            loop. The Odoo ``ir.cron`` short-circuits so the worker pod is
            the *only* drainer cluster-wide. This is the production target
            (LEVIATHAN_POD_ARCHITECTURE.md §6).

        Independent of ``leviathan.prd_queue_enabled``: the mode is only
        consulted when the queue is enabled. With the queue OFF the
        Phase-1 in-process ``_submit_bg`` path runs and this setting is
        irrelevant.
        """
        return (
            self.env["ir.config_parameter"].sudo()
                .get_param("leviathan.prd_execution_mode", "inprocess")
            or "inprocess"
        ).strip().lower()

    @api.model
    def _prd_queue_enabled(self):
        """Master switch for Phase-2 PRD-queue behaviour.

        When False (default): every Phase-1 path runs exactly as today.
        Webhook + dispatchers go through `_submit_bg`, the worker's PHASE 1
        does the existing atomic claim, the watchdog handles PRD-side recovery.

        When True: the webhook + dispatchers write `state=generating` and
        return; the drainer cron claims & dispatches; the worker uses
        fence-verify in PHASE 1; the watchdog's PRD-side blocks no-op.

        Flipping the flag is reversible at any time (§12). Stored as a
        System Parameter so it can be toggled live without a restart.
        """
        return self.env["ir.config_parameter"].sudo().get_param(
            "leviathan.prd_queue_enabled", "False",
        ).strip().lower() in ("1", "true", "yes", "on")

    @api.model
    def _prd_queue_producer_vals(self):
        """Standard fields every producer merges into its state-write when
        transitioning a row to 'generating' on the auto pipeline (§11
        reference write). Harmless in flag-OFF mode — just extra data.

        - `auto_continue=True` keeps the drainer eligible for the row.
        - `prd_queued_at=now()` is the drainer's FIFO ordering key.
        - `prd_failure_count=0` is a user-initiated reset (Retry button etc.).
        - `started_processing_at=False` makes the row claim-ready.
        - `pipeline_status` gives the tasker a phase indicator.
        """
        return {
            "auto_continue": True,
            "prd_queued_at": fields.Datetime.now(),
            "prd_failure_count": 0,
            "started_processing_at": False,
            "pipeline_status": "Queued for PRD generation",
        }

    def _enter_prd_queue_dispatch(self, label_suffix=""):
        """Postcommit-dispatch helper for any producer that has just written
        state='generating' on this row.

        - Flag-OFF: schedules `_submit_bg("prd-gen[...]", ...)` (Phase-1 path).
        - Flag-ON : no-op; the drainer claims the row within ~1 min.
        """
        self.ensure_one()
        if self._prd_queue_enabled():
            _logger.info(
                "[leviathan][job=%s] producer: queue enabled — row left "
                "for drainer (no _submit_bg)", self.id,
            )
            return
        db_name = self.env.cr.dbname
        record_id = self.id
        label = f"prd-gen[job={record_id}]{label_suffix}"
        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                label, self._run_prd_generation_bg, db_name, record_id,
            )
        )

    # ------------------------------------------------------------------
    # Prompt helpers (read from Settings, fallback to file)
    # ------------------------------------------------------------------

    @api.model
    def _get_prd_system_prompt(self):
        """Read PRD system prompt from Settings; fall back to bundled file."""
        ICP = self.env["ir.config_parameter"].sudo()
        prompt = ICP.get_param("leviathan.prd_system_prompt", "")
        if prompt and prompt.strip():
            return prompt.strip()
        path = Path(__file__).parent.parent / "prompts" / "prd_agent_spec.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @api.model
    def _get_qc_system_prompt(self):
        """Read QC system prompt from Settings; fall back to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        prompt = ICP.get_param("leviathan.qc_system_prompt", "")
        if prompt and prompt.strip():
            return prompt.strip()
        from ..services.qc_service import DEFAULT_QC_SYSTEM_PROMPT
        return DEFAULT_QC_SYSTEM_PROMPT

    # ------------------------------------------------------------------
    # Computed
    # ------------------------------------------------------------------

    @api.depends_context("uid")
    def _compute_is_admin(self):
        for rec in self:
            rec.is_admin = self.env.user.has_group(
                "leviathan.group_leviathan_admin"
            )

    @api.depends("score", "grade")
    def _compute_score_display(self):
        for rec in self:
            if rec.score:
                rec.score_display = str(int(rec.score))
            else:
                rec.score_display = ""

    # Typical stage durations (seconds) from real pipeline data
    _STAGE_ESTIMATES = {
        "extracting": 600,   # ~10 min (Lambda + heavy sites)
        "generating": 180,   # ~3 min (multi-turn Bedrock)
        "scoring": 30,       # ~30s
    }

    # Human-readable label for every distinct value of `current_phase`.
    # Add new entries here when adding new `_write_with_cursor({"current_phase": ...})`
    # call sites in the worker; the label appears in stage_progress_html
    # the moment the write commits. Anything not in this table falls
    # back to an empty label (no header shown), which is the right thing
    # for transient/internal values.
    _PHASE_LABELS = {
        # Extraction sub-steps (UI / controller path)
        "extracting.invoking": "Invoking Lambda",
        "extracting.running": "Lambda running",
        # Worker pickup → PRD pipeline
        "generating.starting": "Worker starting",
        "generating.downloading_screenshots": "Downloading screenshots",
        "generating.calling_bedrock": "Calling Bedrock (PRD)",
        "generating.scoring": "Scoring PRD",
        "generating.uploading": "Uploading artifacts to S3",
        # QC phase
        "scoring.qc_review": "Running QC review (Bedrock)",
        "scoring.qc_complete": "QC complete — finalizing",
        # Staged-flow sub-steps
        "staged.generate": "Staged: generating PRD (manual)",
        "staged.qc": "Staged: running QC (manual)",
    }

    @api.depends("state", "started_at", "last_heartbeat", "completed_at",
                 "duration_seconds", "current_phase")
    def _compute_stage_progress(self):
        now = fields.Datetime.now()
        for rec in self:
            # Finished states: show total duration
            if rec.state in ("done", "submitted", "failed"):
                if rec.started_at and rec.completed_at:
                    total = rec.duration_seconds or max(0, (rec.completed_at - rec.started_at).total_seconds())
                    m, s = divmod(int(total), 60)
                    total_str = f"{m}m {s:02d}s" if m else f"{s}s"
                    color = "#dc3545" if rec.state == "failed" else "#28a745"
                    label = {"failed": "Failed after"}.get(rec.state, "Completed in")
                    rec.stage_progress_html = (
                        f'<div style="font-size:13px;color:{color};padding:4px 0;">'
                        f'{label}: <b>{total_str}</b>'
                        f'</div>'
                    )
                else:
                    rec.stage_progress_html = ""
                continue

            # In-progress states: show stage + total + estimate
            if rec.state not in self._STAGE_ESTIMATES:
                rec.stage_progress_html = ""
                continue

            stage_start = rec.last_heartbeat or rec.started_at or now
            overall_start = rec.started_at or stage_start
            stage_elapsed = max(0, (now - stage_start).total_seconds())
            overall_elapsed = max(0, (now - overall_start).total_seconds())

            # Remaining for this stage
            stage_est = self._STAGE_ESTIMATES[rec.state]
            stage_remaining = max(0, stage_est - stage_elapsed)

            # Remaining overall = this stage remaining + sum of future stages
            stages = ["extracting", "generating", "scoring"]
            idx = stages.index(rec.state)
            future = sum(self._STAGE_ESTIMATES[s] for s in stages[idx + 1:])
            total_remaining = stage_remaining + future

            def _fmt(secs):
                m, s = divmod(int(secs), 60)
                return f"{m}m {s:02d}s" if m else f"{s}s"

            # Sub-step header (only renders when current_phase is set and
            # has a human label). Sits ABOVE the main timer so the tasker
            # sees "Calling Bedrock" + 1m 23s, not just "1m 23s".
            phase_label = self._PHASE_LABELS.get(rec.current_phase or "", "")
            phase_html = (
                f'<div style="font-size:13px;color:#0d6efd;padding:2px 0;'
                f'font-weight:500;">&#9881; {phase_label}</div>'
            ) if phase_label else ""

            rec.stage_progress_html = (
                f'{phase_html}'
                f'<div style="font-size:13px;color:#495057;padding:4px 0;">'
                f'Stage: <b>{_fmt(stage_elapsed)}</b>'
                f' &middot; Total: <b>{_fmt(overall_elapsed)}</b>'
                f' &middot; Est. remaining: <b>~{_fmt(total_remaining)}</b>'
                f'</div>'
            )

    @api.depends("score_report_json")
    def _compute_score_report_html(self):
        """Render score_report_json as a formatted HTML table."""
        from ..services.scoring_service import RUBRIC_SECTIONS
        for rec in self:
            report = rec.score_report_json
            if not report:
                rec.score_report_html = ""
                continue

            total = report.get("total_score", 0)
            grade = report.get("grade", "?")
            details = report.get("details", {})
            sections = report.get("section_scores", {})

            gc = {"A": "#28a745", "B": "#17a2b8", "C": "#ffc107",
                  "D": "#fd7e14", "F": "#dc3545", "REJECT": "#dc3545"}
            color = gc.get(grade, "#6c757d")

            html = (
                f'<div style="margin-bottom:16px;">'
                f'<span style="font-size:32px;font-weight:700;color:{color};">{total}</span>'
                f'<span style="font-size:18px;color:{color};margin-left:4px;"></span>'
                f'<span style="display:inline-block;margin-left:12px;padding:4px 12px;'
                f'border-radius:4px;background:{color};color:#fff;font-weight:600;'
                f'font-size:16px;">{grade}</span>'
            )
            if details.get("grade_cap"):
                html += (
                    f'<span style="margin-left:12px;color:#6c757d;font-size:13px;">'
                    f'Cap: {details["grade_cap"]}</span>'
                )
            html += '</div>'

            html += (
                '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                '<tr style="background:#f8f9fa;font-weight:600;">'
                '<td style="padding:6px 8px;border-bottom:2px solid #dee2e6;">Section</td>'
                '<td style="padding:6px 8px;border-bottom:2px solid #dee2e6;text-align:center;">Score</td>'
                '<td style="padding:6px 8px;border-bottom:2px solid #dee2e6;text-align:center;">Max</td>'
                '<td style="padding:6px 8px;border-bottom:2px solid #dee2e6;">%</td>'
                '</tr>'
            )
            for key in sorted(sections.keys()):
                s = sections[key]
                score_val = s.get("score", 0) if isinstance(s, dict) else 0
                max_val = s.get("max", 0) if isinstance(s, dict) else 0
                name = RUBRIC_SECTIONS.get(key, {}).get("name", key)
                pct = round(score_val / max_val * 100) if max_val > 0 else 0
                bc = "#28a745" if pct >= 80 else "#ffc107" if pct >= 50 else "#dc3545"
                bar = (
                    f'<div style="background:#e9ecef;border-radius:3px;height:14px;width:100px;display:inline-block;">'
                    f'<div style="background:{bc};height:14px;border-radius:3px;width:{min(pct, 100)}px;"></div></div>'
                    f' <span style="color:#6c757d;">{pct}%</span>'
                )
                html += (
                    f'<tr style="border-bottom:1px solid #eee;">'
                    f'<td style="padding:5px 8px;">{key}: {name}</td>'
                    f'<td style="padding:5px 8px;text-align:center;font-weight:600;">{score_val}</td>'
                    f'<td style="padding:5px 8px;text-align:center;color:#6c757d;">{max_val}</td>'
                    f'<td style="padding:5px 8px;">{bar}</td>'
                    f'</tr>'
                )
            html += '</table>'

            rejects = report.get("reject_triggers", [])
            warnings = report.get("warnings", [])
            if rejects:
                html += '<div style="margin-top:10px;">'
                for r in rejects:
                    html += f'<span style="display:inline-block;margin:2px 4px;padding:2px 8px;background:#dc3545;color:#fff;border-radius:3px;font-size:12px;">{r}</span>'
                html += '</div>'

            if warnings:
                html += '<div style="margin-top:6px;">'
                for w in warnings:
                    html += f'<span style="display:inline-block;margin:2px 4px;padding:2px 8px;background:#ffc107;color:#000;border-radius:3px;font-size:12px;">{w}</span>'
                html += '</div>'

            wc = details.get("word_count", 0)
            t1 = details.get("tier1_violations", [])
            html += f'<div style="margin-top:10px;color:#6c757d;font-size:12px;">'
            html += f'Words: {wc}'
            if t1:
                html += f' &middot; Banned phrases: {", ".join(t1)}'
            html += '</div>'

            rec.score_report_html = html

    @api.depends("screenshot_keys", "asset_keys")
    def _compute_asset_previews(self):
        """Build HTML preview galleries for screenshots and assets."""
        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("leviathan.s3_bucket", "")
        region = ICP.get_param("leviathan.s3_region", "us-east-1")
        cdn_url = ICP.get_param("leviathan.s3_cdn_url", "")

        if cdn_url:
            base = cdn_url.rstrip("/")
        elif bucket:
            base = f"https://{bucket}.s3.{region}.amazonaws.com"
        else:
            base = ""

        for rec in self:
            keys = rec.screenshot_keys or []
            if keys and base:
                parts = []
                for key in keys:
                    url = f"{base}/{key}"
                    fname = key.rsplit("/", 1)[-1] if "/" in key else key
                    parts.append(
                        f'<div style="display:inline-block;margin:6px;text-align:center;">'
                        f'<a href="{url}" target="_blank">'
                        f'<img src="{url}" style="max-width:280px;max-height:180px;'
                        f'border:1px solid #ddd;border-radius:4px;" '
                        f'title="{fname}" loading="lazy"/>'
                        f'</a><br/><small>{fname}</small></div>'
                    )
                rec.screenshot_urls_html = "".join(parts)
            else:
                rec.screenshot_urls_html = (
                    "<p class='text-muted'>No screenshots available</p>"
                    if not keys else
                    "<p class='text-muted'>Configure S3 bucket in settings to preview</p>"
                )

            akeys = rec.asset_keys or []
            if akeys and base:
                # Group assets by subfolder
                groups = {}  # folder_label -> [(url, fname, ext)]
                for key in akeys:
                    url = f"{base}/{key}"
                    fname = key.rsplit("/", 1)[-1] if "/" in key else key
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                    # Determine folder group
                    if "/deliverables/Page Assets/" in key or "/Page Assets/" in key:
                        folder = "Page Assets"
                    elif "/deliverables/_unused/" in key or "/_unused/" in key:
                        folder = "Unused (Copyrighted)"
                    elif "/deliverables/References/" in key or "/References/" in key:
                        folder = "References"
                    else:
                        folder = "Other"
                    groups.setdefault(folder, []).append((url, fname, ext))

                html_parts = []
                # Display order
                for folder in ("Page Assets", "References", "Unused (Copyrighted)", "Other"):
                    items = groups.get(folder)
                    if not items:
                        continue
                    html_parts.append(
                        f'<div style="margin:12px 0 6px 0;font-weight:600;'
                        f'font-size:13px;color:#495057;border-bottom:1px solid #dee2e6;'
                        f'padding-bottom:4px;">{folder} ({len(items)})</div>'
                        f'<div style="display:flex;flex-wrap:wrap;">'
                    )
                    for url, fname, ext in items:
                        if ext in ("png", "jpg", "jpeg", "webp", "gif", "svg"):
                            html_parts.append(
                                f'<div style="display:inline-block;margin:6px;text-align:center;">'
                                f'<a href="{url}" target="_blank">'
                                f'<img src="{url}" style="max-width:200px;max-height:140px;'
                                f'border:1px solid #ddd;border-radius:4px;" '
                                f'title="{fname}" loading="lazy"/>'
                                f'</a><br/><small style="word-break:break-all;max-width:200px;'
                                f'display:inline-block;">{fname}</small></div>'
                            )
                        elif ext in ("woff2", "woff", "ttf", "otf"):
                            html_parts.append(
                                f'<div style="display:inline-block;margin:6px;padding:8px 12px;'
                                f'border:1px solid #ddd;border-radius:4px;background:#f8f9fa;">'
                                f'<a href="{url}" target="_blank">'
                                f'Font: {fname}</a></div>'
                            )
                        elif ext in ("mp4", "webm", "ogg"):
                            html_parts.append(
                                f'<div style="display:inline-block;margin:6px;">'
                                f'<video src="{url}" style="max-width:280px;max-height:180px;'
                                f'border:1px solid #ddd;border-radius:4px;" '
                                f'controls muted preload="metadata"/>'
                                f'<br/><small>{fname}</small></div>'
                            )
                        else:
                            html_parts.append(
                                f'<div style="display:inline-block;margin:6px;padding:8px 12px;'
                                f'border:1px solid #ddd;border-radius:4px;background:#f8f9fa;">'
                                f'<a href="{url}" target="_blank">'
                                f'{fname}</a></div>'
                            )
                    html_parts.append('</div>')

                rec.asset_urls_html = "".join(html_parts)
            else:
                rec.asset_urls_html = (
                    "<p class='text-muted'>No assets available</p>"
                    if not akeys else
                    "<p class='text-muted'>Configure S3 bucket in settings to preview</p>"
                )

    @api.depends("screenshot_keys", "asset_keys")
    def _compute_asset_score_html(self):
        """Build extraction summary matching Quality tab design."""
        for rec in self:
            ss = rec.screenshot_keys or []
            ak = rec.asset_keys or []
            if not ss and not ak:
                rec.asset_score_html = ""
                continue

            # Categorize assets by folder
            page_assets = [k for k in ak if "/Page Assets/" in k]
            references = [k for k in ak if "/References/" in k]
            unused = [k for k in ak if "/_unused/" in k]

            # Count by type
            images = [k for k in ak if any(
                k.lower().endswith(e)
                for e in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
            )]
            fonts = [k for k in ak if any(
                k.lower().endswith(e)
                for e in (".ttf", ".woff", ".woff2", ".otf")
            )]

            total = len(ss) + len(ak)
            usable = len(page_assets) + len(references)

            # Extraction quality rating
            if len(ss) >= 6 and usable >= 3:
                quality, qcolor = "Good", "#28a745"
            elif len(ss) >= 3 or usable >= 1:
                quality, qcolor = "Partial", "#ffc107"
            else:
                quality, qcolor = "Poor", "#dc3545"

            # Header: big quality label + total badge (mirrors score + grade)
            html = (
                f'<div style="margin-bottom:16px;">'
                f'<span style="font-size:32px;font-weight:700;color:{qcolor};">{total}</span>'
                f'<span style="font-size:14px;color:#6c757d;margin-left:4px;">files</span>'
                f'<span style="display:inline-block;margin-left:12px;padding:4px 12px;'
                f'border-radius:4px;background:{qcolor};color:#fff;font-weight:600;'
                f'font-size:16px;">{quality}</span>'
                '</div>'
            )

            # Table matching Quality tab style
            rows = [
                ("Screenshots", len(ss), 10),
                ("Page Assets (copyright-free)", len(page_assets), 5),
                ("References", len(references), 10),
                ("Unused (copyrighted)", len(unused), None),
                ("Images", len(images), None),
                ("Fonts", len(fonts), None),
            ]

            html += (
                '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                '<tr style="background:#f8f9fa;font-weight:600;">'
                '<td style="padding:6px 8px;border-bottom:2px solid #dee2e6;">Category</td>'
                '<td style="padding:6px 8px;border-bottom:2px solid #dee2e6;text-align:center;">Count</td>'
                '<td style="padding:6px 8px;border-bottom:2px solid #dee2e6;">Coverage</td>'
                '</tr>'
            )

            for name, count, target in rows:
                if target:
                    pct = min(round(count / target * 100), 100)
                    bc = "#28a745" if pct >= 80 else "#ffc107" if pct >= 40 else "#dc3545"
                    bar = (
                        f'<div style="background:#e9ecef;border-radius:3px;height:14px;'
                        f'width:100px;display:inline-block;">'
                        f'<div style="background:{bc};height:14px;border-radius:3px;'
                        f'width:{pct}px;"></div></div>'
                        f' <span style="color:#6c757d;">{pct}%</span>'
                    )
                else:
                    bar = ''

                html += (
                    f'<tr style="border-bottom:1px solid #eee;">'
                    f'<td style="padding:5px 8px;">{name}</td>'
                    f'<td style="padding:5px 8px;text-align:center;font-weight:600;">{count}</td>'
                    f'<td style="padding:5px 8px;">{bar}</td>'
                    f'</tr>'
                )

            html += '</table>'
            rec.asset_score_html = html

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("leviathan.job") or "New"
                )
            # If user_id is set at creation, auto-promote to draft
            if vals.get("user_id") and vals.get("state", "not_assigned") == "not_assigned":
                vals["state"] = "draft"
            # If no user, must be not_assigned
            if not vals.get("user_id") and vals.get("state") in ("draft", "done"):
                vals["state"] = "not_assigned"
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        # Auto-promote when admin assigns a user to a not_assigned task.
        # The target state preserves whatever progress the task already has
        # (see _smart_state_on_assign): released done tasks come back as done,
        # released failed-with-data tasks come back as failed (Retry visible),
        # everything else lands in draft. Plain rec.write() is used (not
        # super(LeviathanJob, ...).write) so mail.thread chatter records the
        # state restoration. Recursion is bounded: the recursive vals carries
        # only `state`, so this promote/demote block does not re-enter.
        if "user_id" in vals and vals["user_id"]:
            to_promote = self.filtered(lambda r: r.state == "not_assigned")
            for rec in to_promote:
                new_state = rec._smart_state_on_assign()
                if rec.state == new_state:
                    continue
                promote_vals = {"state": new_state}
                if new_state == "failed" and not rec.error_message:
                    promote_vals["error_message"] = (
                        "Reassigned with prior extraction data — "
                        "click Retry to resume."
                    )
                rec.write(promote_vals)
        # Auto-demote to not_assigned when user is removed from draft task
        if "user_id" in vals and not vals["user_id"]:
            to_demote = self.filtered(lambda r: r.state == "draft")
            if to_demote:
                to_demote.write({"state": "not_assigned"})
        return res

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    _ACTIVE_STATES = ("draft", "extracting", "generating", "scoring", "qc_running", "done")

    def action_start_task(self):
        """Tasker grabs the next available unassigned task — race-safe.

        Two taskers clicking simultaneously do NOT get the same task: the
        pick uses `SELECT ... FOR UPDATE SKIP LOCKED`, so concurrent
        transactions each lock different rows. The row stays locked until
        this request commits, so the subsequent ORM write that sets user_id
        cannot be lost to a competing writer. Without this, both clickers
        end up redirected to the same task and one of them sees the row
        vanish on refresh (record-rule denies access once user_id is the
        other tasker).

        Smart state restores prior progress (see _smart_state_on_assign).
        """
        user = self.env.user
        ICP = self.env["ir.config_parameter"].sudo()
        max_active = int(ICP.get_param("leviathan.max_jobs_per_user", "5"))

        if max_active > 0:
            active_count = self.sudo().search_count([
                ("user_id", "=", user.id),
                ("state", "in", self._ACTIVE_STATES),
            ])
            if active_count >= max_active:
                raise UserError(
                    f"You already have {active_count} active task(s). "
                    f"Submit or complete existing tasks first (max: {max_active})."
                )

        cat_id = self.env.context.get("start_task_category_id")
        cat_clause = " AND category_id = %s" if cat_id else ""
        params = [int(cat_id)] if cat_id else []

        # Only hand out tasks whose PRD is actually ready for review: a
        # `not_assigned` job with prd_text set (a batch-completed job, or a
        # released `done` task). Fresh `not_assigned` jobs (URL imported,
        # pipeline never run) and `failed` jobs are NOT tasker work — fresh
        # jobs go through a batch run, failed jobs through Retry. On claim,
        # _smart_state_on_assign restores a prd_text job to `done`, so the
        # tasker always lands on a finished PRD.
        self.env.cr.execute(
            f"""
            SELECT id FROM leviathan_job
             WHERE state = 'not_assigned'
               AND user_id IS NULL
               AND prd_text IS NOT NULL
               AND prd_text != ''
               {cat_clause}
             ORDER BY create_date ASC
             LIMIT 1
             FOR UPDATE SKIP LOCKED
            """,
            params,
        )
        row = self.env.cr.fetchone()
        if not row:
            raise UserError("No tasks available. Check back later.")

        task = self.sudo().browse(row[0])
        new_state = task._smart_state_on_assign()
        task.write({"user_id": user.id, "state": new_state})
        task._notify_state_change(new_state)
        _logger.info(
            "[leviathan] Start Task: user=%s claimed job=%s (state=%s)",
            user.login, task.name, new_state,
        )

        return {
            "type": "ir.actions.act_window",
            "res_model": "leviathan.job",
            "res_id": task.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }

    def action_release_task(self):
        """Admin releases a task back to the unassigned queue.

        Clears user assignment, preserves all progress data.
        Only works on draft, done, or failed tasks.
        """
        self.ensure_one()
        if self.state not in ("draft", "done", "failed"):
            raise UserError(
                "Can only release tasks in Draft, Done, or Failed state. "
                "Cancel in-progress tasks first."
            )
        self.write({
            "user_id": False,
            "state": "not_assigned",
            "error_message": False,
            "cancel_requested": False,
        })
        self._notify_state_change("not_assigned")

    def action_reset_selected(self):
        """Server action: reset selected tasks to not_assigned.

        Cancels in-progress pipeline, preserves extraction data if present.
        Clears user assignment. Works on any state except submitted.
        """
        eligible = self.filtered(lambda r: r.state != "submitted")
        if not eligible:
            raise UserError("No eligible tasks to reset.")
        skipped = self - eligible

        for task in eligible:
            vals = {
                "user_id": False,
                "state": "not_assigned",
                "cancel_requested": False,
                "via_batch": False,
                "error_message": False,
            }
            # Mark pipeline interruption for in-progress tasks
            if task.state in ("extracting", "generating", "scoring", "qc_running"):
                vals["cancel_requested"] = True
            task.write(vals)

        msg = f"{len(eligible)} task(s) reset to Not Assigned."
        if skipped:
            msg += f" {len(skipped)} submitted task(s) skipped."
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Tasks Reset",
                "message": msg,
                "type": "success",
                "sticky": False,
            },
        }

    def action_run(self):
        """Start the extraction pipeline.

        If extraction data already exists (e.g. task was retried/released),
        opens a wizard to let user choose re-extract vs regenerate.
        Works from draft or not_assigned (auto-assigns current user).
        """
        self.ensure_one()
        if self.state not in ("draft", "not_assigned"):
            raise UserError("Can only run tasks in Draft or Not Assigned state.")
        if not self.url:
            raise UserError("Please enter a website URL before running.")

        # Auto-assign if not_assigned or no user
        if self.state == "not_assigned" or not self.user_id:
            self.write({"user_id": self.env.uid, "state": "draft"})

        # If extraction data exists, ask user what to do
        if self._has_extraction_data and not self.env.context.get("force_extract"):
            wizard = self.env["leviathan.rerun.wizard"].create({"job_id": self.id})
            return {
                "type": "ir.actions.act_window",
                "name": "Extraction Data Exists",
                "res_model": "leviathan.rerun.wizard",
                "res_id": wizard.id,
                "view_mode": "form",
                "views": [(False, "form")],
                "target": "new",
            }

        # Per-user concurrent job limit (only count running tasks, not draft/done)
        max_jobs = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("leviathan.max_jobs_per_user", "5")
        )
        if max_jobs > 0:
            running_states = ("extracting", "generating", "scoring")
            running_count = self.sudo().search_count([
                ("user_id", "=", self.user_id.id),
                ("state", "in", running_states),
                ("id", "!=", self.id),
            ])
            if running_count >= max_jobs:
                raise UserError(
                    f"Too many tasks running ({running_count}). "
                    f"Wait for current tasks to complete."
                )

        # Lock row to prevent double-run (graceful on lock conflict). Using
        # ``cr.savepoint()`` auto-rolls-back on exception and avoids the
        # f-string-built SQL identifier — no functional change, just ORM-
        # native handling.
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "SELECT id FROM leviathan_job WHERE id = %s FOR UPDATE NOWAIT",
                    [self.id],
                )
        except Exception:
            raise UserError("Task is being modified by another session. Try again.")

        self.env.cr.execute(
            "SELECT state FROM leviathan_job WHERE id = %s", [self.id]
        )
        row = self.env.cr.fetchone()
        if not row or row[0] not in ("draft", "not_assigned"):
            raise UserError("Task is no longer available to run.")

        self.write({
            "state": "extracting",
            "error_message": False,
            "cancel_requested": False,
            "auto_continue": True,
            "started_at": fields.Datetime.now(),
            "completed_at": False,
            "duration_seconds": False,
            "last_heartbeat": fields.Datetime.now(),
        })
        # Chatter: pipeline kicked off — record WHO started it and on
        # which URL so the activity log on the job tells the full story
        # without anyone having to dig in Loki. See Phase-D notes in
        # LEVIATHAN_POD_ARCHITECTURE.md record-of-work.
        try:
            from markupsafe import escape
            user_name = escape(self.env.user.name or "?")
            url_safe = escape(self.url or "")
            self._chatter_post(
                f"<b>Pipeline started (Run All)</b> by {user_name} "
                f"&mdash; URL: {url_safe}"
            )
        except Exception:
            # _chatter_post already swallows; this outer try is for the
            # escape import in case markupsafe isn't installed (it is —
            # Odoo ships with it — but defensive). Never break action_run.
            pass
        self._trigger_extraction()

    def action_stage_extract(self):
        """Staged manual run: extract only, then park at 'extracted'.

        Same preconditions as action_run (the Run All path) but sets
        auto_continue=False, so the webhook parks the job after extraction
        instead of cascading into PRD generation. The tasker then advances
        one stage at a time via the Generate / Score / QC buttons.
        """
        self.ensure_one()
        if self.state not in ("draft", "not_assigned"):
            raise UserError("Can only run tasks in Draft or Not Assigned state.")
        if not self.url:
            raise UserError("Please enter a website URL before running.")

        if self.state == "not_assigned" or not self.user_id:
            self.write({"user_id": self.env.uid, "state": "draft"})

        # If extraction data already exists (e.g. a batch-completed task
        # auto-released to not_assigned, or a released task), do NOT silently
        # re-extract and destroy the existing PRD — open the rerun wizard so
        # the tasker chooses re-extract vs regenerate. Mirrors action_run.
        if self._has_extraction_data and not self.env.context.get("force_extract"):
            wizard = self.env["leviathan.rerun.wizard"].create({"job_id": self.id})
            return {
                "type": "ir.actions.act_window",
                "name": "Extraction Data Exists",
                "res_model": "leviathan.rerun.wizard",
                "res_id": wizard.id,
                "view_mode": "form",
                "views": [(False, "form")],
                "target": "new",
            }

        max_jobs = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("leviathan.max_jobs_per_user", "5")
        )
        if max_jobs > 0:
            running_states = ("extracting", "generating", "scoring", "qc_running")
            running_count = self.sudo().search_count([
                ("user_id", "=", self.user_id.id),
                ("state", "in", running_states),
                ("id", "!=", self.id),
            ])
            if running_count >= max_jobs:
                raise UserError(
                    f"Too many tasks running ({running_count}). "
                    f"Wait for current tasks to complete."
                )

        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "SELECT id FROM leviathan_job WHERE id = %s FOR UPDATE NOWAIT",
                    [self.id],
                )
        except Exception:
            raise UserError("Task is being modified by another session. Try again.")

        self.env.cr.execute(
            "SELECT state FROM leviathan_job WHERE id = %s", [self.id]
        )
        row = self.env.cr.fetchone()
        if not row or row[0] not in ("draft", "not_assigned"):
            raise UserError("Task is no longer available to run.")

        self.write({
            "state": "extracting",
            "error_message": False,
            "cancel_requested": False,
            "auto_continue": False,
            "started_at": fields.Datetime.now(),
            "completed_at": False,
            "duration_seconds": False,
            "last_heartbeat": fields.Datetime.now(),
        })
        self._trigger_extraction()

    def action_cancel(self):
        """Stop a running or parked task and return it to Draft.

        Running states (extracting / generating / scoring / qc_running) also
        set cancel_requested so background threads bail at their next check.
        Parked staged states (extracted / generated / scored) have no running
        thread — here Cancel is the Reset that walks a one-shot-forward staged
        job back to Draft.
        """
        self.ensure_one()
        running = ("extracting", "generating", "scoring", "qc_running")
        parked = ("extracted", "generated", "scored")
        if self.state not in running + parked:
            raise UserError("Cancel is only available while a task is running or staged.")
        self.write({
            "state": "draft",
            "cancel_requested": self.state in running,
            "error_message": False,
        })
        _logger.info("[leviathan][job=%s] cancelled by %s", self.name, self.env.user.name)
        self._notify_state_change("draft")
        # Chatter: clear record of cancellation so a tasker re-opening
        # the job later understands the state didn't transition by
        # accident.
        try:
            from markupsafe import escape
            self._chatter_post(
                f"<b>Pipeline cancelled</b> by "
                f"{escape(self.env.user.name or '?')}"
            )
        except Exception:
            pass

    def action_stage_generate(self):
        """Staged manual run: generate the PRD, then park at 'generated'."""
        self.ensure_one()
        if self.state != "extracted":
            raise UserError(
                "Generate is only available after extraction (Extracted state)."
            )
        if not self.prd_prompt:
            raise UserError("No extraction data available for PRD generation.")
        self.write({
            "state": "generating",
            "error_message": False,
            "cancel_requested": False,
            "completed_at": False,
            "last_heartbeat": fields.Datetime.now(),
            "started_processing_at": False,
        })
        db_name = self.env.cr.dbname
        record_id = self.id
        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                f"prd-gen-stage[job={record_id}]",
                self._run_generate_only_bg, db_name, record_id,
            )
        )

    def action_stage_score(self):
        """Staged manual run: score the PRD with the rubric, park at 'scored'.

        Synchronous — score_prd is pure-Python regex (no Bedrock), so it runs
        inline in the request rather than on the background pool.
        """
        self.ensure_one()
        if self.state != "generated":
            raise UserError(
                "Score is only available after PRD generation (Generated state)."
            )
        if not self.prd_text:
            raise UserError("No PRD text available to score.")
        from ..services.scoring_service import score_prd
        category = (
            self.category_id.name if self.category_id
            else (self.site_discovery_json or {}).get("category") or "Normal Website"
        )
        report = score_prd(prd_text=self.prd_text, category=category)
        self.write({
            "state": "scored",
            "score": report["total_score"],
            "grade": report["grade"],
            "score_report_json": report,
        })
        self._notify_state_change("scored")

    def action_stage_qc(self):
        """Staged manual run: run QC, then complete the job (Done)."""
        self.ensure_one()
        if self.state != "scored":
            raise UserError("QC is only available after scoring (Scored state).")
        if not self.prd_text:
            raise UserError("No PRD text available for QC.")
        self.write({
            "state": "qc_running",
            "error_message": False,
            "cancel_requested": False,
            "last_heartbeat": fields.Datetime.now(),
        })
        db_name = self.env.cr.dbname
        record_id = self.id
        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                f"qc-stage[job={record_id}]",
                self._run_qc_stage_bg, db_name, record_id,
            )
        )

    def action_run_batch_concurrent(self):
        """Server action: fire all selected jobs in parallel via async Lambda invoke.

        Replaces the legacy RabbitMQ + consumer.py fan-out. Uses a single
        ThreadPoolExecutor sized to ``leviathan.batch_concurrency`` (default 250)
        to issue ``boto3 lambda:Invoke(InvocationType='Event')`` calls in parallel.
        Each invoke returns in <1s; the Lambdas themselves run asynchronously
        and post back to the existing webhook.

        Caveats:
        - Selected count > Lambda's ReservedConcurrentExecutions => excess
          invocations are throttled by AWS (TooManyRequestsException).
        - All eligible jobs are marked ``extracting`` first; jobs whose invoke
          fails are reverted to ``not_assigned`` with an error message.
        """
        eligible = self.filtered(lambda r: r.state == "not_assigned" and r.url)
        if not eligible:
            raise UserError(
                "No eligible tasks. Tasks must be 'Not Assigned' with a URL."
            )
        skipped = self - eligible

        # Phase-2 batch-size guard (LEVIATHAN_POD_ARCHITECTURE.md §6.7).
        # One worker pod runs `_PRD_POOL_SIZE` (default 40) concurrently;
        # the queue absorbs the rest. The cap stops a runaway selection
        # from filling the queue beyond what's operationally healthy.
        ICP = self.env["ir.config_parameter"].sudo()
        batch_max = int(ICP.get_param("leviathan.batch_max_size", "500"))
        if batch_max > 0 and len(eligible) > batch_max:
            raise UserError(
                f"Batch too large: {len(eligible)} selected, max is {batch_max}.\n"
                f"One worker pod runs {_PRD_POOL_SIZE} jobs concurrently — the "
                f"queue absorbs the rest. Split into smaller batches if you "
                f"need a hard cap, or raise leviathan.batch_max_size in Settings."
            )

        config = {
            "function_name": ICP.get_param("leviathan.lambda_function_name"),
            "region": ICP.get_param("leviathan.lambda_region") or "ap-south-1",
            "access_key_id": ICP.get_param("leviathan.extraction_access_key_id") or "",
            "secret_access_key": ICP.get_param("leviathan.extraction_secret_access_key") or "",
            "local_url": ICP.get_param("leviathan.lambda_local_url") or "",
            "batch_concurrency": int(
                ICP.get_param("leviathan.batch_concurrency") or _BATCH_FANOUT_POOL_SIZE
            ),
        }

        # Skip re-extraction: a job that already has a prd_prompt is already
        # "extracted" — send it straight to PRD generation. Only jobs WITHOUT
        # extraction data go through the Lambda fan-out.
        to_generate = eligible.filtered(lambda r: r.prd_prompt)
        to_extract = eligible - to_generate

        if to_extract and not config["function_name"] and not config["local_url"]:
            raise UserError(
                "Lambda function name not configured "
                "(Settings -> Leviathan -> Lambda Function), and no "
                "leviathan.lambda_local_url is set for local dev either."
            )

        now = fields.Datetime.now()
        db_name = self.env.cr.dbname
        _common = {
            "via_batch": True,
            "started_at": now,
            "completed_at": False,
            "duration_seconds": False,
            "last_heartbeat": now,
            "error_message": False,
            "extraction_warnings": False,
            "cancel_requested": False,
        }

        # --- Path A: already extracted -> straight to PRD generation ---
        if to_generate:
            to_generate.write(dict(
                _common, state="generating",
                **self._prd_queue_producer_vals(),    # Phase-2 producer fields (§11)
            ))
            gen_ids = to_generate.ids
            _logger.info(
                "[leviathan] batch: %d job(s) already extracted -> PRD generation: %s",
                len(gen_ids), to_generate.mapped("name"),
            )

            # Phase-2: skip _submit_bg when the queue is enabled — the drainer
            # picks each row up within ~1 min. The state-write above includes
            # auto_continue=True + prd_queued_at, so the rows are claim-ready.
            if not self._prd_queue_enabled():
                def _deferred_generate():
                    for rid in gen_ids:
                        _submit_bg(
                            f"prd-gen[job={rid}]",
                            self._run_prd_generation_bg, db_name, rid,
                        )

                self.env.cr.postcommit.add(_deferred_generate)
            else:
                _logger.info(
                    "[leviathan] batch Path A: queue enabled — %d row(s) "
                    "left for drainer (no _submit_bg)", len(gen_ids),
                )

        # --- Path B: no extraction data -> Lambda fan-out ---
        if to_extract:
            to_extract.write(dict(_common, state="extracting"))
            record_ids = to_extract.ids
            record_urls = {rec.id: rec.url for rec in to_extract}
            webhook_url = to_extract[0]._get_webhook_url()
            _logger.info(
                "[leviathan] batch: %d job(s) dispatching to extraction Lambda",
                len(record_ids),
            )

            def _deferred_extract():
                _submit_bg(
                    "batch-fanout",
                    self._fanout_batch_extraction,
                    db_name, record_ids, record_urls, webhook_url, config,
                )

            self.env.cr.postcommit.add(_deferred_extract)

        parts = []
        if to_extract:
            parts.append(
                f"{len(to_extract)} extracting (max parallel: {config['batch_concurrency']})"
            )
        if to_generate:
            parts.append(f"{len(to_generate)} already extracted → PRD generation")
        msg = "; ".join(parts) + "."
        if skipped:
            msg += f" {len(skipped)} skipped (wrong state or missing URL)."
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Batch Dispatch Started",
                "message": msg,
                "type": "success",
                "sticky": False,
            },
        }

    def _fanout_batch_extraction(
        self, db_name, record_ids, record_urls, webhook_url, config,
    ):
        """Run the parallel async-invoke fan-out in a background thread.

        Each successful invoke leaves the record in ``extracting`` (the
        webhook completes the lifecycle). Each failed invoke reverts the
        record to ``not_assigned`` with the AWS error captured.
        """
        from ..services.extraction_service import trigger_extraction

        ok_ids: list[int] = []
        ok_request_ids: dict[int, str] = {}  # record_id -> Lambda RequestId
        failed: dict[int, str] = {}
        # P1-3: cap the fan-out at 30 threads. Async lambda:Invoke takes
        # <200 ms, so 30 threads issue 500 invokes in ~4 s — a 250-thread
        # pool was a needless thread-exhaustion contributor.
        max_workers = min(config["batch_concurrency"], len(record_ids), 30) or 1

        def _invoke_one(record_id: int) -> tuple[int, dict]:
            url = record_urls.get(record_id, "")
            result = trigger_extraction(
                url=url,
                job_id=record_id,
                callback_url=webhook_url,
                function_name=config["function_name"],
                region=config["region"],
                access_key_id=config["access_key_id"],
                secret_access_key=config["secret_access_key"],
                local_url=config.get("local_url", ""),
            )
            return record_id, result

        _logger.info(
            "Batch fan-out: %d records, max_workers=%d, function=%s, region=%s",
            len(record_ids), max_workers, config["function_name"], config["region"],
        )

        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="leviathan-fanout",
        ) as pool:
            futures = [pool.submit(_invoke_one, rid) for rid in record_ids]
            for future in as_completed(futures):
                try:
                    record_id, result = future.result()
                except Exception as exc:
                    _logger.exception("Fan-out worker crashed: %s", exc)
                    continue
                if result.get("success"):
                    ok_ids.append(record_id)
                    # Capture per-job request_id so the Logs tab can later
                    # filter CloudWatch events down to this exact invocation.
                    # Collected here, persisted in bulk below to avoid 500
                    # serial cursor opens for a 500-job batch.
                    rid_str = result.get("request_id") or ""
                    if rid_str:
                        ok_request_ids[record_id] = rid_str
                else:
                    failed[record_id] = result.get("error", "Unknown error")[:500]

        _logger.info(
            "Batch fan-out done: %d invoked OK, %d failed", len(ok_ids), len(failed),
        )

        # Bulk-persist lambda_request_id + current_phase for every job whose
        # invoke succeeded. One transaction for the whole batch — at 500 jobs
        # this is ~5 ms vs ~5 s with per-row writes. Best-effort: if this
        # block fails the jobs still progress (they just lose the CloudWatch
        # button until the watchdog or a retry re-stamps the id).
        if ok_request_ids:
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    for rid, request_id in ok_request_ids.items():
                        rec = env[self._name].browse(rid)
                        if rec.exists():
                            rec.write({
                                "lambda_request_id": request_id,
                                "current_phase": "extracting.running",
                            })
                    cr.commit()
            except Exception:
                _logger.exception(
                    "[leviathan] failed to persist %d lambda request_ids "
                    "after batch fan-out (jobs still progress; Logs tab "
                    "loses CloudWatch button for these rows)",
                    len(ok_request_ids),
                )

        if failed:
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    for rid, err in failed.items():
                        rec = env[self._name].browse(rid)
                        if rec.exists():
                            rec.write({
                                "state": "not_assigned",
                                "via_batch": False,
                                "error_message": f"Lambda invoke failed: {err}",
                                "completed_at": fields.Datetime.now(),
                            })
                    cr.commit()
            except Exception:
                _logger.exception(
                    "Failed to revert %d records after invoke failures", len(failed),
                )

    def action_mark_submitted(self):
        """Tasker marks the task as submitted."""
        self.ensure_one()
        if self.state != "done":
            raise UserError("Can only submit tasks that are Done.")
        if not self.qc_verdict:
            raise UserError(
                "Cannot submit: QC has not run. "
                "Review the QC report or rerun QC before submitting."
            )
        self.write({"state": "submitted"})

    def action_discard(self):
        """Discard a task as unusable — site unsuitable, or nothing extracted.

        Available on every state except ``discarded`` (already terminal), to
        both admin and tasker. Distinct from 'failed' (a pipeline error) and
        'submitted' (a good deliverable). A discarded task can be brought
        back into the workflow via the Assign button.

        For tasks discarded mid-pipeline, sets ``cancel_requested`` so any
        running background work bails out at its next checkpoint.
        """
        self.ensure_one()
        if self.state == "discarded":
            raise UserError("This task is already discarded.")
        # Keep the tasker assigned — the discarded task stays "theirs".
        vals = {
            "state": "discarded",
            "completed_at": fields.Datetime.now(),
        }
        # Signal any running background thread to stop at its next check.
        if self.state in ("extracting", "generating", "scoring", "qc_running"):
            vals["cancel_requested"] = True
        self.write(vals)
        _logger.info(
            "[leviathan][job=%s] discarded by %s", self.name, self.env.user.name,
        )
        self._notify_state_change("discarded")

    def action_reopen(self):
        """Bring a discarded task back into the workflow as a Draft task.

        Keeps the tasker it was assigned to; only if it had none (e.g. a failed
        batch job) does it fall to the user who clicks Assign."""
        self.ensure_one()
        if self.state != "discarded":
            raise UserError("Only discarded tasks can be reopened.")
        self.write({
            "state": "draft",
            "user_id": self.user_id.id or self.env.uid,
            "error_message": False,
            "completed_at": False,
            "cancel_requested": False,
        })
        _logger.info(
            "[leviathan][job=%s] reopened from discarded by %s",
            self.name, self.env.user.name,
        )
        self._notify_state_change("draft")

    def action_retry(self):
        """Retry a failed task.

        Skip-re-extraction: if a PRD prompt already exists the extraction is
        done — go straight back to PRD generation. Otherwise reset to draft so
        the tasker can re-run a fresh extraction.
        """
        self.ensure_one()
        if self.state != "failed":
            raise UserError("Can only retry failed tasks.")

        if self.prd_prompt:
            # Already extracted — skip extraction, regenerate the PRD.
            self.write({
                "state": "generating",
                "score": False,
                "grade": False,
                "qc_verdict": False,
                "prd_text": False,
                "prd_text_html": False,
                "qc_report": False,
                "score_report_json": False,
                "prd_url": False,
                "llm_attempts": 0,
                "llm_trace_json": False,
                "error_message": False,
                "cancel_requested": False,
                "started_at": fields.Datetime.now(),
                "completed_at": False,
                "duration_seconds": False,
                "last_heartbeat": fields.Datetime.now(),
                # Phase-2 producer fields (§11). started_processing_at=False
                # makes the row claim-ready; prd_failure_count=0 gives a
                # fresh count for a user-initiated Retry.
                **self._prd_queue_producer_vals(),
            })
            _logger.info(
                "[leviathan][job=%s] retry: prd_prompt present — skipping extraction, "
                "going straight to PRD generation",
                self.name,
            )
            self._enter_prd_queue_dispatch()    # Phase-2: no-op when queue ON
            return

        # No extraction data — reset to draft and re-extract from scratch.
        _logger.info(
            "[leviathan][job=%s] retry: no prd_prompt — resetting to draft for "
            "fresh extraction", self.name,
        )
        self.write({
            "state": "draft",
            "score": False,
            "grade": False,
            "qc_verdict": False,
            "prd_text": False,
            "prd_text_html": False,
            "prd_prompt": False,
            "qc_report": False,
            "score_report_json": False,
            "prd_url": False,
            "artifacts_url": False,
            "deliverables_url": False,
            "lambda_callback_json": False,
            "llm_trace_json": False,
            "extraction_warnings": False,
            "llm_attempts": 0,
            "duration_seconds": False,
            "error_message": False,
            "cancel_requested": False,
            "started_processing_at": False,
        })

    def action_retry_failed_batch(self):
        """Bulk smart-retry over selected failed tasks — runs the pipeline
        end-to-end (admin server-action).

        For each selected task in ``state == 'failed'``:

        - **Has ``prd_prompt``** (extraction succeeded last time): skip
          re-extraction. State goes to ``generating``; PRD generation +
          scoring + QC runs in the background. Final state on success
          will be ``done`` (if tasker assigned) or ``not_assigned`` with
          full data (if no tasker — auto-released back to the pool by
          the same ``via_batch`` flow used by Run Batch).

        - **No ``prd_prompt``** (extraction itself failed): full pipeline
          — Lambda extraction → PRD generation → scoring → QC. State
          goes to ``extracting`` and the Lambda is dispatched
          asynchronously. Final state on success follows the same
          via_batch rule as Path A.

        Tasker assignment is **never changed**. The ``via_batch`` flag is
        set ``True`` for tasks without a tasker (so the pipeline releases
        them back to the pool with full data on success) and ``False``
        for tasks with a tasker (so the result stays with the tasker).
        """
        eligible = self.filtered(lambda r: r.state == "failed")
        if not eligible:
            raise UserError("No failed tasks selected.")
        skipped = self - eligible

        # Phase-2 batch-size guard (LEVIATHAN_POD_ARCHITECTURE.md §6.7).
        batch_max = int(
            self.env["ir.config_parameter"].sudo()
                .get_param("leviathan.batch_max_size", "500")
        )
        if batch_max > 0 and len(eligible) > batch_max:
            raise UserError(
                f"Retry batch too large: {len(eligible)} selected, max is "
                f"{batch_max}. Split into smaller batches or raise "
                f"leviathan.batch_max_size in Settings."
            )

        to_generate = eligible.filtered(lambda r: r.prd_prompt)
        to_extract = eligible - to_generate

        # Path B requires Lambda config; if there are any to_extract jobs,
        # validate config up front so we fail fast rather than mid-dispatch.
        ICP = self.env["ir.config_parameter"].sudo()
        config = None
        if to_extract:
            config = {
                "function_name": ICP.get_param("leviathan.lambda_function_name"),
                "region": ICP.get_param("leviathan.lambda_region") or "ap-south-1",
                "access_key_id": ICP.get_param("leviathan.extraction_access_key_id") or "",
                "secret_access_key": ICP.get_param("leviathan.extraction_secret_access_key") or "",
                "batch_concurrency": int(
                    ICP.get_param("leviathan.batch_concurrency") or _BATCH_FANOUT_POOL_SIZE
                ),
            }
            if not config["function_name"]:
                raise UserError(
                    "Lambda function name not configured "
                    "(Settings -> Leviathan -> Lambda Function). Cannot retry "
                    "failed tasks that need re-extraction."
                )

        now = fields.Datetime.now()
        db_name = self.env.cr.dbname

        # --- Path A: prd_prompt exists → straight to PRD generation ---
        # via_batch=True for unassigned tasks so the final write at the end
        # of _run_prd_generation_bg auto-releases them back to the pool with
        # full data. via_batch=False for tasker-assigned tasks so the result
        # stays with the tasker as 'done'.
        gen_ids = []
        for rec in to_generate:
            rec.write({
                "state": "generating",
                "via_batch": not bool(rec.user_id),
                "score": False,
                "grade": False,
                "qc_verdict": False,
                "prd_text": False,
                "prd_text_html": False,
                "qc_report": False,
                "score_report_json": False,
                "prd_url": False,
                "llm_attempts": 0,
                "llm_trace_json": False,
                "error_message": False,
                "cancel_requested": False,
                "started_at": now,
                "completed_at": False,
                "duration_seconds": False,
                "last_heartbeat": now,
                # Phase-2 producer fields (§11).
                **self._prd_queue_producer_vals(),
            })
            gen_ids.append(rec.id)

        if gen_ids:
            # Phase-2: skip _submit_bg when the queue is enabled — rows
            # already carry prd_queued_at + auto_continue=True so the
            # drainer claims them within ~1 min.
            if not self._prd_queue_enabled():
                def _deferred_generate():
                    for rid in gen_ids:
                        _submit_bg(
                            f"prd-gen[job={rid}]",
                            self._run_prd_generation_bg, db_name, rid,
                        )

                self.env.cr.postcommit.add(_deferred_generate)
            else:
                _logger.info(
                    "[leviathan] retry-failed-batch Path A: queue enabled "
                    "— %d row(s) left for drainer", len(gen_ids),
                )

        # --- Path B: no prd_prompt → full pipeline (Lambda extraction first) ---
        if to_extract:
            # Wipe stale data so extraction starts fresh, BUT keep url + the
            # tasker assignment. Same shape of reset the batch-fanout does.
            for rec in to_extract:
                rec.write({
                    "state": "extracting",
                    "via_batch": not bool(rec.user_id),
                    "score": False,
                    "grade": False,
                    "qc_verdict": False,
                    "prd_text": False,
                    "prd_text_html": False,
                    "prd_prompt": False,
                    "qc_report": False,
                    "score_report_json": False,
                    "prd_url": False,
                    "artifacts_url": False,
                    "deliverables_url": False,
                    "lambda_callback_json": False,
                    "llm_trace_json": False,
                    "extraction_warnings": False,
                    "llm_attempts": 0,
                    "screenshot_keys": False,
                    "asset_keys": False,
                    "site_discovery_json": False,
                    "tech_stack": False,
                    "page_count": False,
                    "started_at": now,
                    "completed_at": False,
                    "duration_seconds": False,
                    "last_heartbeat": now,
                    "started_processing_at": False,
                    "error_message": False,
                    "cancel_requested": False,
                })

            record_ids = to_extract.ids
            record_urls = {rec.id: rec.url for rec in to_extract}
            webhook_url = to_extract[0]._get_webhook_url()

            def _deferred_extract():
                _submit_bg(
                    "retry-failed-fanout",
                    self._fanout_batch_extraction,
                    db_name, record_ids, record_urls, webhook_url, config,
                )

            self.env.cr.postcommit.add(_deferred_extract)

        # Notification
        n_with_tasker = len(eligible.filtered(lambda r: r.user_id))
        n_pool = len(eligible) - n_with_tasker
        parts = []
        if to_generate:
            parts.append(f"{len(to_generate)} → PRD generation (had prd_prompt)")
        if to_extract:
            parts.append(f"{len(to_extract)} → full pipeline (Lambda extraction)")
        message = "; ".join(parts) + f". Tasker kept: {n_with_tasker}, pool: {n_pool}."
        if skipped:
            message += f" {len(skipped)} ignored (not in 'failed' state)."

        _logger.info(
            "[leviathan] retry-failed batch by %s: %s",
            self.env.user.name, message,
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Retry Failed — pipeline dispatched",
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    def action_rerun(self):
        """Rerun pipeline — re-extract or regenerate from existing data."""
        self.ensure_one()
        if self.state not in ("draft", "done", "failed"):
            raise UserError("Cannot rerun from this state.")

        re_extract = self.env.context.get("re_extract", False)

        if re_extract or not self.prd_prompt:
            # No usable PRD prompt — must re-extract
            self.write({
                "state": "draft",
                "score": False,
                "grade": False,
                "qc_verdict": False,
                "prd_text": False,
                "prd_text_html": False,
                "prd_prompt": False,
                "qc_report": False,
                "score_report_json": False,
                "prd_url": False,
                "artifacts_url": False,
                "deliverables_url": False,
                "llm_attempts": 0,
                "duration_seconds": False,
                "error_message": False,
                "cancel_requested": False,
            })
            self.with_context(force_extract=True).action_run()
        else:
            self.write({
                "state": "generating",
                "score": False,
                "grade": False,
                "qc_verdict": False,
                "prd_text": False,
                "prd_text_html": False,
                "qc_report": False,
                "score_report_json": False,
                "prd_url": False,
                "llm_attempts": 0,
                "llm_trace_json": False,
                "duration_seconds": False,
                "error_message": False,
                "cancel_requested": False,
                "started_at": fields.Datetime.now(),
                "completed_at": False,
                "last_heartbeat": fields.Datetime.now(),
                # Phase-2 producer fields (§11) — started_processing_at=False
                # makes the row claim-ready and prd_failure_count=0 gives a
                # fresh count on this user-initiated rerun.
                **self._prd_queue_producer_vals(),
            })
            self._enter_prd_queue_dispatch()    # Phase-2: no-op when queue ON

    def action_rerun_with_extract(self):
        """Rerun with full re-extraction."""
        return self.with_context(re_extract=True).action_rerun()

    def action_rerun_without_extract(self):
        """Rerun PRD generation + QC only (keep extraction data)."""
        return self.with_context(re_extract=False).action_rerun()

    def action_open_rerun_wizard(self):
        """Open the rerun wizard with re-extract / regenerate-only choice."""
        self.ensure_one()
        if self.state != "done":
            raise UserError("Can only rerun from Done state.")
        wizard = self.env["leviathan.rerun.wizard"].create({"job_id": self.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Rerun Pipeline",
            "res_model": "leviathan.rerun.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_regenerate_with_qc_feedback(self):
        """Re-run PRD generation using QC failure reasons as feedback."""
        self.ensure_one()
        if self.state != "done":
            raise UserError("Can only retry with feedback from Done state.")
        if not self.qc_report:
            raise UserError("No QC report available for feedback.")

        qc_feedback = self.qc_report
        self.write({
            "state": "generating",
            "score": False,
            "grade": False,
            "qc_verdict": False,
            "prd_text": False,
            "prd_text_html": False,
            "qc_report": False,
            "score_report_json": False,
            "prd_url": False,
            "llm_attempts": 0,
            "llm_trace_json": False,
            "error_message": False,
            "cancel_requested": False,
            "started_at": fields.Datetime.now(),
            "completed_at": False,
            "last_heartbeat": fields.Datetime.now(),
            # Phase-2 producer fields (§11). started_processing_at=False
            # makes the row claim-ready; prd_failure_count=0 = fresh count.
            **self._prd_queue_producer_vals(),
        })

        if self.prd_prompt:
            self.prd_prompt = (
                self.prd_prompt + "\n\n"
                "---\n\n"
                "## PREVIOUS QC FEEDBACK (fix these issues):\n\n"
                + qc_feedback
            )

        self._enter_prd_queue_dispatch()    # Phase-2: no-op when queue ON

    def action_save_prd_edit(self):
        """Save manual PRD edits from the HTML editor back to prd_text."""
        self.ensure_one()
        if not self.prd_text_html:
            raise UserError("No PRD content to save.")
        import re
        html_content = self.prd_text_html
        text = html_content
        text = re.sub(r'<h1[^>]*>(.*?)</h1>', r'# \1\n', text)
        text = re.sub(r'<h2[^>]*>(.*?)</h2>', r'## \1\n', text)
        text = re.sub(r'<h3[^>]*>(.*?)</h3>', r'### \1\n', text)
        text = re.sub(r'<h4[^>]*>(.*?)</h4>', r'#### \1\n', text)
        text = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1\n', text)
        text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text)
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text)
        text = re.sub(r'<em>(.*?)</em>', r'*\1*', text)
        text = re.sub(r'<code>(.*?)</code>', r'`\1`', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        self.prd_text = text.strip()

    # ------------------------------------------------------------------
    # CloudWatch log fetch (admin button on the Logs tab)
    # ------------------------------------------------------------------

    def _fetch_lambda_logs_one(self, job, logs_client, log_group):
        """Pull CloudWatch events for ONE job into `leviathan_job_log`.

        Filters the log group by the captured `lambda_request_id` (each
        invocation's events all share that token). Watermark pagination
        via `last_lambda_log_ts` so a re-click only fetches new events.

        Best-effort: a missing log group, a permissions error, or any
        API exception is logged and swallowed — the UI shows whatever
        rows were already ingested.
        """
        request_id = (job.lambda_request_id or "").strip()
        if not request_id:
            return
        # CloudWatch `filter_log_events` without `startTime` scans only a
        # narrow recent window and returns 0 events for older RequestIds.
        # Always pass a startTime so matching events land on the first
        # page within our 5×200 pagination cap.
        if job.last_lambda_log_ts:
            start_ms = int(job.last_lambda_log_ts.timestamp() * 1000) + 1
        elif job.started_at:
            start_ms = int(job.started_at.timestamp() * 1000) - 60_000
        else:
            import time as _time
            # 7-day lookback for first fetch on a job missing started_at.
            # CloudWatch retention defaults to 30 days; this is well within.
            start_ms = int(_time.time() * 1000) - 7 * 86400 * 1000

        events = []
        next_token = None
        page_limit = 5  # 5 × 200 = up to 1000 events per refresh click
        for _ in range(page_limit):
            kwargs = {
                "logGroupName": log_group,
                "filterPattern": f'"{request_id}"',
                "limit": 200,
                "startTime": start_ms,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            try:
                resp = logs_client.filter_log_events(**kwargs)
            except logs_client.exceptions.ResourceNotFoundException:
                # Log group doesn't exist (Lambda never ran in this region,
                # or the log group was deleted). Silent — there's nothing
                # actionable for the tasker.
                return
            except Exception:
                _logger.exception(
                    "[leviathan][job=%s] filter_log_events failed", job.id,
                )
                return
            events.extend(resp.get("events", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
        if not events:
            return

        Log = self.env["leviathan.job.log"].sudo()
        latest_ms = 0
        rows = []
        # F-LOW-4: ``datetime.utcfromtimestamp`` / ``datetime.utcnow``
        # are deprecated in 3.12 (DeprecationWarning will become an
        # error in a future Python release). Use timezone-aware
        # ``fromtimestamp(ts, tz=timezone.utc)`` then strip tzinfo to
        # keep Odoo's naive-UTC storage convention.
        from datetime import datetime as _dt, timezone as _tz
        def _utc_naive_from_ms(ms):
            return (
                _dt.fromtimestamp(ms / 1000.0, tz=_tz.utc).replace(tzinfo=None)
                if ms else
                _dt.now(_tz.utc).replace(tzinfo=None)
            )
        for ev in events:
            ts_ms = ev.get("timestamp") or 0
            if ts_ms > latest_ms:
                latest_ms = ts_ms
            ts = _utc_naive_from_ms(ts_ms)
            msg = (ev.get("message") or "").rstrip("\n")
            # Crude level classification — CloudWatch doesn't carry
            # structured level metadata, so we sniff the message body.
            # Good enough for the form-view colouring; if the Lambda
            # adopts JSON logging later this should switch to a real
            # parse.
            level = "INFO"
            if "ERROR" in msg or "Traceback" in msg:
                level = "ERROR"
            elif "WARN" in msg or "WARNING" in msg:
                level = "WARNING"
            rows.append({
                "job_id": job.id,
                "timestamp": ts,
                "source": "lambda",
                "level": level,
                "message": msg[:16384],
            })
        if rows:
            Log.create(rows)
        if latest_ms:
            new_watermark = _utc_naive_from_ms(latest_ms)
            job.sudo().write({"last_lambda_log_ts": new_watermark})

    def action_refresh_lambda_logs(self):
        """Admin-only button on the Logs tab: pull CloudWatch events for
        this job into `leviathan_job_log` so the Logs list refreshes with
        Lambda-side logs alongside the Odoo / Worker rows.

        Restricted to admins because (a) it touches AWS APIs, (b) the
        fetch can be slow on a chatty log group. Taskers see the result
        once the admin clicks — they don't trigger the fetch themselves.
        """
        self.ensure_one()
        if not self.env.user.has_group("leviathan.group_leviathan_admin"):
            raise AccessError(
                "Only Leviathan administrators can fetch CloudWatch logs."
            )
        if not self.lambda_request_id:
            return False
        ICP = self.env["ir.config_parameter"].sudo()
        function_name = (
            ICP.get_param("leviathan.lambda_function_name") or ""
        ).strip()
        region = (
            ICP.get_param("leviathan.lambda_region") or "us-east-1"
        ).strip()
        access_key_id = (
            ICP.get_param("leviathan.extraction_access_key_id") or ""
        ).strip()
        secret_access_key = (
            ICP.get_param("leviathan.extraction_secret_access_key") or ""
        ).strip()
        if not function_name:
            raise UserError(
                "Lambda function name not configured. "
                "Set leviathan.lambda_function_name in Settings."
            )
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            raise UserError(
                "boto3 is not installed in this Python environment; "
                "cannot fetch CloudWatch logs."
            )
        client_kwargs = {
            "service_name": "logs",
            "region_name": region,
            "config": BotoConfig(
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        }
        if access_key_id and secret_access_key:
            client_kwargs["aws_access_key_id"] = access_key_id
            client_kwargs["aws_secret_access_key"] = secret_access_key
        logs_client = boto3.client(**client_kwargs)
        # Default Lambda log group naming: /aws/lambda/<function-name>.
        # If the team ever moves to a custom log group, surface that as a
        # System Parameter rather than reverse-engineering the ARN.
        self._fetch_lambda_logs_one(
            self, logs_client, f"/aws/lambda/{function_name}",
        )
        return True

    def action_rerun_qc(self):
        """Re-run only QC validation (after manual PRD edits)."""
        self.ensure_one()
        if self.state != "done":
            raise UserError("Can only rerun QC from Done state.")
        if not self.prd_text:
            raise UserError("No PRD text available for QC.")

        self.write({
            "state": "scoring",
            # CRITICAL (LEVIATHAN_POD_ARCHITECTURE.md §5.13, review H-5):
            # set auto_continue=False so the Phase-2 drainer NEVER claims this
            # row. Without this, a slow QC rerun could be recovered (stale
            # heartbeat) into a full PRD regeneration and clobber the
            # tasker's manually-edited prd_text. The row stays out of the
            # queue for the duration of the rerun.
            "auto_continue": False,
            "qc_verdict": False,
            "qc_report": False,
            "error_message": False,
            "last_heartbeat": fields.Datetime.now(),
            "pipeline_status": "Re-running QC on edited PRD",
        })

        db_name = self.env.cr.dbname
        record_id = self.id

        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                f"qc-rerun[job={record_id}]",
                self._run_qc_only_bg, db_name, record_id,
            )
        )

    def _run_qc_only_bg(self, db_name, record_id):
        """Background: re-run only QC on existing PRD text."""
        from ..services.qc_service import run_qc

        _qc_only_t0 = time.monotonic()
        _logger.info(
            "[leviathan][job=%s] QC-RERUN worker picked up job (pid=%d)",
            record_id, os.getpid(),
        )
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return

                ICP = env["ir.config_parameter"].sudo()
                config = {
                    "inference_arn": ICP.get_param("leviathan.bedrock_inference_arn"),
                    "region": ICP.get_param("leviathan.bedrock_region") or "us-east-1",
                    "bedrock_access_key": ICP.get_param("leviathan.bedrock_access_key_id"),
                    "bedrock_secret_key": ICP.get_param("leviathan.bedrock_secret_access_key"),
                    "s3_bucket": ICP.get_param("leviathan.s3_bucket"),
                    "s3_key_id": ICP.get_param("leviathan.s3_access_key_id"),
                    "s3_secret": ICP.get_param("leviathan.s3_secret_access_key"),
                    "s3_region": ICP.get_param("leviathan.s3_region"),
                }
                # Same category precedence as PRD generation: explicit
                # category_id wins; else fall back to Lambda's classified
                # category from site_discovery_json (the "auto" choice).
                _lambda_cat = (
                    (record.site_discovery_json or {}).get("category")
                    or "Normal Website"
                )
                job_data = {
                    "prd_text": record.prd_text,
                    "category_name": (
                        record.category_id.name if record.category_id
                        else _lambda_cat
                    ),
                    "url": record.url,
                    "site_discovery_json": record.site_discovery_json,
                    "screenshot_keys": record.screenshot_keys or [],
                }
                qc_prompt = record._get_qc_system_prompt()

            extraction_artifacts = {}
            if job_data["site_discovery_json"]:
                extraction_artifacts["site_discovery"] = job_data["site_discovery_json"]

            # QC image attachment: opt-in via the
            # `leviathan.qc_include_images` System Parameter (default
            # OFF). When OFF (recommended): QC is text-only and compares
            # the PRD against the extraction-JSON summary — same as the
            # historical hardcoded behaviour (the `P0-4` workaround).
            # When ON: up to `leviathan.prd_max_images` screenshots are
            # attached as Bedrock image blocks for visual alignment
            # checks. The flag is a Settings field on the host UI pod;
            # the worker pod sees it on its next drain tick via the
            # ormcache clear (see LEVIATHAN_POD_ARCHITECTURE.md §24.3).
            screenshot_blocks = []
            if (ICP.get_param("leviathan.qc_include_images")
                    in ("1", "True", "true", "yes", "on", True)):
                screenshot_blocks = _load_screenshot_blocks_from_s3(
                    screenshot_keys=job_data.get("screenshot_keys") or [],
                    max_n=int(ICP.get_param("leviathan.prd_max_images") or 4),
                    s3_bucket=config.get("s3_bucket") or "",
                    s3_region=config.get("s3_region") or "us-east-1",
                    s3_access_key_id=config.get("s3_key_id") or "",
                    s3_secret_access_key=config.get("s3_secret") or "",
                    log_prefix=f"leviathan][job={record_id}",
                )

            qc_result = run_qc(
                prd_text=job_data["prd_text"],
                extraction_data=extraction_artifacts,
                site_discovery=job_data["site_discovery_json"] or {},
                url=job_data["url"],
                category=job_data["category_name"],
                inference_arn=config["inference_arn"],
                region=config["region"],
                access_key_id=config["bedrock_access_key"],
                secret_access_key=config["bedrock_secret_key"],
                qc_system_prompt=qc_prompt,
                screenshot_blocks=screenshot_blocks,
            )

            self._write_with_cursor(db_name, record_id, {
                "state": "done",
                "qc_verdict": qc_result["verdict"],
                "qc_report": qc_result["report"],
            })
            _logger.info(
                "[leviathan][job=%s] QC-RERUN complete in %.1fs — verdict=%s",
                record_id, time.monotonic() - _qc_only_t0,
                qc_result["verdict"],
            )

        except Exception as exc:
            _logger.exception(
                "[leviathan][job=%s] QC-RERUN failed after %.1fs",
                record_id, time.monotonic() - _qc_only_t0,
            )
            self._write_with_cursor(db_name, record_id, {
                "state": "done",
                # Fail-closed: a QC error must not leave qc_verdict blank, or the
                # job becomes unsubmittable with no clear recovery. Mirrors the
                # fail-closed behaviour in _run_prd_generation_bg.
                "qc_verdict": "not_shippable",
                "qc_report": f"QC rerun error: {exc}",
                "error_message": f"QC failed: {exc}",
            })

    def _run_generate_only_bg(self, db_name, record_id):
        """Background (staged): generate the PRD + upload, park at 'generated'.

        Mirrors the generation half of the fused pipeline (config/validation +
        Bedrock generation + S3 upload) but stops before scoring and QC.
        Wrapped in _HeartbeatTicker so a long Bedrock call keeps last_heartbeat
        fresh against the watchdog.
        """
        from ..services.bedrock_service import generate_prd
        from ..services.s3_service import upload_prd_to_s3

        _t0 = time.monotonic()
        _logger.info(
            "[leviathan][job=%s] GENERATE-STAGE worker picked up job (pid=%d)",
            record_id, os.getpid(),
        )
        llm_trace = None
        try:
            with _HeartbeatTicker(self, db_name, record_id, interval=60):
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    record = env[self._name].browse(record_id)
                    if not record.exists():
                        return

                    ICP = env["ir.config_parameter"].sudo()
                    config = {
                        "inference_arn": ICP.get_param("leviathan.bedrock_inference_arn"),
                        "region": ICP.get_param("leviathan.bedrock_region") or "us-east-1",
                        "bedrock_access_key": ICP.get_param("leviathan.bedrock_access_key_id"),
                        "bedrock_secret_key": ICP.get_param("leviathan.bedrock_secret_access_key"),
                        "s3_bucket": ICP.get_param("leviathan.s3_bucket"),
                        "s3_key_id": ICP.get_param("leviathan.s3_access_key_id"),
                        "s3_secret": ICP.get_param("leviathan.s3_secret_access_key"),
                        "s3_region": ICP.get_param("leviathan.s3_region"),
                        "s3_folder": ICP.get_param("leviathan.s3_folder") or "leviathan",
                        "cdn_url": ICP.get_param("leviathan.s3_cdn_url"),
                    }
                    _lambda_category = (
                        (record.site_discovery_json or {}).get("category")
                        or "Normal Website"
                    )
                    job_data = {
                        "name": record.name,
                        "prd_prompt": record.prd_prompt,
                        "category_name": (
                            record.category_id.name if record.category_id
                            else _lambda_category
                        ),
                        "category_is_explicit": bool(record.category_id),
                        "url": record.url,
                        "site_discovery_json": record.site_discovery_json,
                        "screenshot_keys": record.screenshot_keys or [],
                    }
                    prd_system_prompt = record._get_prd_system_prompt()

                    if not config["inference_arn"]:
                        record.write({
                            "state": "failed",
                            "error_message": "Bedrock inference ARN not configured",
                            "completed_at": fields.Datetime.now(),
                        })
                        cr.commit()
                        return
                    if not job_data["prd_prompt"]:
                        record.write({
                            "state": "failed",
                            "error_message": "No extraction data available for PRD generation",
                            "completed_at": fields.Datetime.now(),
                        })
                        cr.commit()
                        return

                    record.write({
                        "started_processing_at": fields.Datetime.now(),
                        "last_heartbeat": fields.Datetime.now(),
                    })
                    cr.commit()

                # PRD-gen image attachment: opt-in via the
                # `leviathan.prd_include_images` System Parameter (default
                # OFF, matches the historical text-only behaviour). When
                # OFF: the Lambda's build_prd_prompt already encoded the
                # visual extraction as text into `prd_prompt` — sending
                # images on top is signal-redundant AND raises the
                # Bedrock 4xx rejection rate sharply (long output +
                # images is the failure-prone combo). When ON: top-N
                # screenshots are attached for visual grounding. See
                # LEVIATHAN_POD_ARCHITECTURE.md §25 and
                # docs/BATCH_500_TEST_PLAN.md.
                screenshot_blocks = []
                if (ICP.get_param("leviathan.prd_include_images")
                        in ("1", "True", "true", "yes", "on", True)):
                    screenshot_blocks = _load_screenshot_blocks_from_s3(
                        screenshot_keys=job_data.get("screenshot_keys") or [],
                        max_n=int(ICP.get_param("leviathan.prd_max_images") or 4),
                        s3_bucket=config.get("s3_bucket") or "",
                        s3_region=config.get("s3_region") or "us-east-1",
                        s3_access_key_id=config.get("s3_key_id") or "",
                        s3_secret_access_key=config.get("s3_secret") or "",
                        log_prefix=f"leviathan][job={record_id}",
                    )

                import re as _re
                current_category = job_data["category_name"]
                if job_data["category_is_explicit"]:
                    prd_prompt_text = _re.sub(
                        r"^(\s*-\s*\*\*Category:\*\*\s+).+$",
                        lambda m: m.group(1) + current_category,
                        job_data["prd_prompt"],
                        count=1,
                        flags=_re.MULTILINE,
                    )
                    category_contract = (
                        f"AUTHORITATIVE CATEGORY (HARD): the user has explicitly "
                        f"chosen '{current_category}' as this site's category. "
                        f"Use THIS category throughout the PRD. DO NOT infer a "
                        f"different category from the extracted data.\n\n"
                    )
                else:
                    prd_prompt_text = job_data["prd_prompt"]
                    category_contract = ""

                word_count_contract = (
                    "WORD COUNT CONTRACT (HARD): produce 4,000-5,000 words. "
                    "5,000 is a hard ceiling, NEVER exceed.\n\n"
                )

                content_blocks = list(screenshot_blocks)
                content_blocks.append({"text": (
                    f"{category_contract}"
                    f"{word_count_contract}"
                    f"Below is the extracted website data. "
                    f"Write the complete PRD following all rules.\n\n"
                    f"---\n\n{prd_prompt_text}"
                )})
                messages = [{"role": "user", "content": content_blocks}]

                llm_trace = {
                    "prd_system_prompt": prd_system_prompt,
                    "extraction_prompt": prd_prompt_text,
                    "category_at_generation": current_category,
                    "category_source": (
                        "tasker_or_admin" if job_data["category_is_explicit"]
                        else "lambda_auto_classified"
                    ),
                    "screenshots_attached": len(screenshot_blocks),
                    "attempts": [],
                    "qc": {},
                }

                if self._is_cancelled(db_name, record_id):
                    self._write_with_cursor(db_name, record_id, {
                        "state": "draft",
                        "error_message": "Cancelled during generation",
                        "completed_at": fields.Datetime.now(),
                    })
                    return

                best_prd_text = generate_prd(
                    inference_arn=config["inference_arn"],
                    region=config["region"],
                    system_prompt=prd_system_prompt,
                    messages=messages,
                    access_key_id=config["bedrock_access_key"],
                    secret_access_key=config["bedrock_secret_key"],
                )
                llm_trace["attempts"].append({
                    "attempt": 1,
                    "prd_text": best_prd_text,
                })

                prd_url = upload_prd_to_s3(
                    prd_text=best_prd_text,
                    job_name=job_data["name"],
                    bucket=config["s3_bucket"],
                    access_key_id=config["s3_key_id"],
                    secret_key=config["s3_secret"],
                    region=config["s3_region"],
                    folder=config["s3_folder"],
                    cdn_url=config["cdn_url"],
                )

                self._write_with_cursor(db_name, record_id, {
                    "state": "generated",
                    "prd_text": best_prd_text,
                    "prd_text_html": _markdown_to_html(best_prd_text),
                    "prd_url": prd_url,
                    "prd_prompt": prd_prompt_text,
                    "llm_attempts": 1,
                    "llm_trace_json": llm_trace,
                    "last_heartbeat": fields.Datetime.now(),
                })
                _logger.info(
                    "[leviathan][job=%s] GENERATE-STAGE complete in %.1fs — "
                    "parked at 'generated' (%d chars)",
                    record_id, time.monotonic() - _t0, len(best_prd_text or ""),
                )

        except Exception as exc:
            _logger.exception(
                "[leviathan][job=%s] GENERATE-STAGE failed after %.1fs",
                record_id, time.monotonic() - _t0,
            )
            try:
                fail_vals = {
                    "state": "failed",
                    "error_message": str(exc)[:500],
                    "completed_at": fields.Datetime.now(),
                }
                if llm_trace:
                    fail_vals["llm_trace_json"] = llm_trace
                self._write_with_cursor(db_name, record_id, fail_vals)
            except Exception:
                _logger.error("[leviathan][job=%s] failed to mark as failed", record_id)

    def _run_qc_stage_bg(self, db_name, record_id):
        """Background (staged): run QC on the existing PRD, then complete (Done).

        Same QC logic as _run_qc_only_bg, but as the terminal stage of a staged
        run it stamps completed_at + duration_seconds.
        """
        from ..services.qc_service import run_qc

        _t0 = time.monotonic()
        _logger.info(
            "[leviathan][job=%s] QC-STAGE worker picked up job (pid=%d)",
            record_id, os.getpid(),
        )
        try:
            with _HeartbeatTicker(self, db_name, record_id, interval=60):
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    record = env[self._name].browse(record_id)
                    if not record.exists():
                        return

                    ICP = env["ir.config_parameter"].sudo()
                    config = {
                        "inference_arn": ICP.get_param("leviathan.bedrock_inference_arn"),
                        "region": ICP.get_param("leviathan.bedrock_region") or "us-east-1",
                        "bedrock_access_key": ICP.get_param("leviathan.bedrock_access_key_id"),
                        "bedrock_secret_key": ICP.get_param("leviathan.bedrock_secret_access_key"),
                        "s3_bucket": ICP.get_param("leviathan.s3_bucket"),
                        "s3_key_id": ICP.get_param("leviathan.s3_access_key_id"),
                        "s3_secret": ICP.get_param("leviathan.s3_secret_access_key"),
                        "s3_region": ICP.get_param("leviathan.s3_region"),
                    }
                    _lambda_cat = (
                        (record.site_discovery_json or {}).get("category")
                        or "Normal Website"
                    )
                    job_data = {
                        "prd_text": record.prd_text,
                        "category_name": (
                            record.category_id.name if record.category_id
                            else _lambda_cat
                        ),
                        "url": record.url,
                        "site_discovery_json": record.site_discovery_json,
                        "screenshot_keys": record.screenshot_keys or [],
                        "started_at": record.started_at,
                    }
                    qc_prompt = record._get_qc_system_prompt()

                extraction_artifacts = {}
                if job_data["site_discovery_json"]:
                    extraction_artifacts["site_discovery"] = job_data["site_discovery_json"]

                # QC image attachment: same opt-in pattern as the inline
                # QC site above. Default OFF; ON loads up to N screenshots
                # via `_load_screenshot_blocks_from_s3`. See §25 of the
                # architecture doc.
                screenshot_blocks = []
                if (ICP.get_param("leviathan.qc_include_images")
                        in ("1", "True", "true", "yes", "on", True)):
                    screenshot_blocks = _load_screenshot_blocks_from_s3(
                        screenshot_keys=job_data.get("screenshot_keys") or [],
                        max_n=int(ICP.get_param("leviathan.prd_max_images") or 4),
                        s3_bucket=config.get("s3_bucket") or "",
                        s3_region=config.get("s3_region") or "us-east-1",
                        s3_access_key_id=config.get("s3_key_id") or "",
                        s3_secret_access_key=config.get("s3_secret") or "",
                        log_prefix=f"leviathan][job={record_id}",
                    )

                qc_result = run_qc(
                    prd_text=job_data["prd_text"],
                    extraction_data=extraction_artifacts,
                    site_discovery=job_data["site_discovery_json"] or {},
                    url=job_data["url"],
                    category=job_data["category_name"],
                    inference_arn=config["inference_arn"],
                    region=config["region"],
                    access_key_id=config["bedrock_access_key"],
                    secret_access_key=config["bedrock_secret_key"],
                    qc_system_prompt=qc_prompt,
                    screenshot_blocks=screenshot_blocks,
                )

                started = job_data["started_at"]
                duration = (
                    (fields.Datetime.now() - started).total_seconds()
                    if started else 0
                )
                self._write_with_cursor(db_name, record_id, {
                    "state": "done",
                    "qc_verdict": qc_result["verdict"],
                    "qc_report": qc_result["report"],
                    "completed_at": fields.Datetime.now(),
                    "duration_seconds": duration,
                })
                _logger.info(
                    "[leviathan][job=%s] QC-STAGE complete in %.1fs — verdict=%s",
                    record_id, time.monotonic() - _t0, qc_result["verdict"],
                )

        except Exception as exc:
            _logger.exception(
                "[leviathan][job=%s] QC-STAGE failed after %.1fs",
                record_id, time.monotonic() - _t0,
            )
            self._write_with_cursor(db_name, record_id, {
                "state": "done",
                "qc_verdict": "not_shippable",
                "qc_report": f"QC stage error: {exc}",
                "error_message": f"QC failed: {exc}",
                "completed_at": fields.Datetime.now(),
            })

    def action_download_zip(self):
        """Build and download a ZIP of the tasker deliverable package."""
        self.ensure_one()
        if not self.prd_text:
            raise UserError("PRD not yet generated.")

        import base64
        import io
        import zipfile
        from urllib.parse import urlparse

        parsed = urlparse(self.url or "")
        site_slug = (parsed.hostname or "site").replace(".", "_").replace("www_", "")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("prd.md", self.prd_text)
            zf.writestr(f"{site_slug}_website.md", self._generate_website_md())

            if self.qc_report:
                zf.writestr("QC_Report.md", self.qc_report)

            download_errors = []

            ICP = self.env["ir.config_parameter"].sudo()
            s3_config = {
                "bucket": ICP.get_param("leviathan.s3_bucket"),
                "key_id": ICP.get_param("leviathan.s3_access_key_id"),
                "secret": ICP.get_param("leviathan.s3_secret_access_key"),
                "region": ICP.get_param("leviathan.s3_region") or "us-east-1",
            }

            if self.screenshot_keys and s3_config["bucket"]:
                from ..services.s3_service import download_file_from_s3
                for i, key in enumerate(self.screenshot_keys, 1):
                    try:
                        data = download_file_from_s3(
                            key=key,
                            bucket=s3_config["bucket"],
                            access_key_id=s3_config["key_id"],
                            secret_key=s3_config["secret"],
                            region=s3_config["region"],
                        )
                        filename = key.split("/")[-1] if "/" in key else f"{i:02d}_screenshot.png"
                        zf.writestr(f"References/{filename}", data)
                    except Exception as e:
                        download_errors.append(f"References/{key}: {e}")

            if self.asset_keys and s3_config["bucket"]:
                from ..services.s3_service import download_file_from_s3
                for key in self.asset_keys:
                    try:
                        data = download_file_from_s3(
                            key=key,
                            bucket=s3_config["bucket"],
                            access_key_id=s3_config["key_id"],
                            secret_key=s3_config["secret"],
                            region=s3_config["region"],
                        )
                        parts = key.split("/")
                        if "deliverables" in parts:
                            idx = parts.index("deliverables")
                            rel_path = "/".join(parts[idx + 1:])
                        elif "assets" in parts:
                            idx = parts.index("assets")
                            rel_path = "/".join(parts[idx:])
                        else:
                            rel_path = f"assets/{parts[-1]}"
                        zf.writestr(rel_path, data)
                    except Exception as e:
                        download_errors.append(f"{key}: {e}")

            if download_errors:
                error_report = "# Download Errors\n\n"
                for err in download_errors:
                    error_report += f"- {err}\n"
                zf.writestr("DOWNLOAD_ERRORS.md", error_report)

        buf.seek(0)
        zip_data = base64.b64encode(buf.read())

        filename = f"{self.name}_deliverables.zip"
        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": zip_data,
            "mimetype": "application/zip",
            "res_model": self._name,
            "res_id": self.id,
        })

        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    def _generate_website_md(self):
        return (self.url or "") + "\n"

    # ------------------------------------------------------------------
    # Background Triggers
    # ------------------------------------------------------------------

    def _trigger_extraction(self):
        db_name = self.env.cr.dbname
        record_id = self.id
        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                f"extract[job={record_id}]",
                self._run_extraction_bg, db_name, record_id,
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify_state_change(self, state):
        """Send bus notification for state change (works from ORM context)."""
        try:
            self.env["bus.bus"]._sendone(
                "leviathan_job_updates",
                "leviathan/job_state",
                {"id": self.id, "state": state},
            )
        except Exception:
            pass

    def _mark_failed(self, error_msg):
        """Mark task as failed."""
        self.write({
            "state": "failed",
            "error_message": str(error_msg)[:500],
            "completed_at": fields.Datetime.now(),
        })
        self._notify_state_change("failed")
        # Chatter: surface the failure reason inline. Truncated to 300
        # chars — full stack traces live in the Logs tab.
        try:
            from markupsafe import escape
            self._chatter_post(
                f"<b>Failed:</b> {escape(str(error_msg)[:300])}"
            )
        except Exception:
            pass

    def _is_cancelled(self, db_name, record_id):
        """Check if a task has been cancelled (safe for background threads)."""
        try:
            with Registry(db_name).cursor() as cr:
                cr.execute(
                    "SELECT cancel_requested FROM leviathan_job WHERE id = %s",
                    (record_id,),
                )
                row = cr.fetchone()
                return row and row[0]
        except Exception:
            return False

    def _get_webhook_url(self):
        # Defensive strip: a single accidental leading/trailing space in
        # the `web.base.url` System Parameter (very easy to introduce via
        # the UI or a copy-paste) produces a URL httpx rejects with
        # "missing protocol". That manifests as 5 silent callback retries
        # in the Lambda, the job hanging in `extracting`, and the
        # watchdog eventually marking it `failed` 20 min later. Strip
        # here so the entire pipeline is whitespace-tolerant.
        base_url = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("web.base.url", "http://localhost:8069")
            or ""
        ).strip().rstrip("/")
        return f"{base_url}/api/v1/leviathan/webhook/extraction-complete"

    # ------------------------------------------------------------------
    # Background: Extraction
    # ------------------------------------------------------------------

    def _run_extraction_bg(self, db_name, record_id):
        """Background: async-invoke the extraction Lambda. Returns in <1s.

        Job stays in ``extracting`` while Lambda runs; the webhook completes
        the lifecycle. Only failed invokes flip state to ``failed``.
        """
        from ..services.extraction_service import trigger_extraction

        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return

                ICP = env["ir.config_parameter"].sudo()
                config = {
                    "function_name": ICP.get_param("leviathan.lambda_function_name"),
                    "region": ICP.get_param("leviathan.lambda_region") or "ap-south-1",
                    "access_key_id": ICP.get_param("leviathan.extraction_access_key_id") or "",
                    "secret_access_key": ICP.get_param("leviathan.extraction_secret_access_key") or "",
                    "local_url": ICP.get_param("leviathan.lambda_local_url") or "",
                }
                job_data = {
                    "url": record.url,
                    "callback_url": record._get_webhook_url(),
                }
                cr.commit()

            result = trigger_extraction(
                url=job_data["url"],
                job_id=record_id,
                callback_url=job_data["callback_url"],
                function_name=config["function_name"],
                region=config["region"],
                access_key_id=config["access_key_id"],
                secret_access_key=config["secret_access_key"],
                local_url=config["local_url"],
            )

            if not result.get("success"):
                error_msg = result.get("error", "Extraction Lambda invoke failed")
                _logger.error(
                    "[leviathan][job=%s] extraction Lambda invoke REJECTED by "
                    "AWS — marking failed: %s", record_id, error_msg[:300],
                )
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": error_msg[:500],
                    "completed_at": fields.Datetime.now(),
                })
            else:
                # The async invoke was ACCEPTED (HTTP 202). This does NOT mean
                # the Lambda has started running — AWS may hold the event in
                # its async-invocation queue until a concurrency slot frees.
                # The job now waits in `extracting` for either the Lambda's
                # "started" ping or its final callback. If neither arrives,
                # the watchdog handles it.
                request_id = result.get("request_id", "")
                _logger.info(
                    "[leviathan][job=%s] extraction Lambda invoke ACCEPTED by "
                    "AWS (request_id=%s) — awaiting callback",
                    record_id, request_id,
                )
                # Persist the request_id so the Logs tab's CloudWatch fetch
                # button can filter log-group events down to this exact
                # invocation. Also stamp current_phase so the tasker sees
                # "Lambda running" in the Stage Progress widget instead of
                # just "extracting" with no sub-step.
                self._write_with_cursor(db_name, record_id, {
                    "lambda_request_id": request_id,
                    "current_phase": "extracting.running",
                })
                # Chatter: invoke accepted by AWS. RequestId is the
                # token CloudWatch fetch will need later — surface it
                # in the activity feed so an admin doesn't need to
                # cross-reference the Logs tab to find it.
                try:
                    from markupsafe import escape
                    rid_disp = escape(request_id) if request_id else "(none)"
                    self._post_message_with_cursor(
                        db_name, record_id,
                        f"<b>Lambda invoked (async)</b> &mdash; "
                        f"RequestId {rid_disp}"
                    )
                except Exception:
                    pass

        except Exception as exc:
            _logger.exception(
                "Extraction background task failed for job %s", record_id
            )
            try:
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": str(exc)[:500],
                    "completed_at": fields.Datetime.now(),
                })
            except Exception:
                _logger.error("Failed to mark job %s as failed", record_id)

    # ------------------------------------------------------------------
    # Background: PRD Generation
    # ------------------------------------------------------------------

    def _run_prd_generation_bg(self, db_name, record_id, claim_count):
        """Background: generate PRD via Bedrock, score, QC.

        Wrapped in `_HeartbeatTicker` so `last_heartbeat` pulses every 60s
        for the lifetime of the worker — regardless of where the worker
        is in its code.

        `claim_count` is the drainer's fence token captured at claim time
        (see docs/LEVIATHAN_POD_ARCHITECTURE.md §5.4 / §5.10). The worker
        only ever runs after the drainer has claimed the row; every write
        is conditional on this value so a recovery-re-claimed run can't be
        clobbered by a stale worker.
        """
        from ..services.bedrock_service import generate_prd
        from ..services.scoring_service import score_prd
        from ..services.s3_service import upload_prd_to_s3

        with _HeartbeatTicker(self, db_name, record_id, interval=60):
            self._run_prd_generation_bg_impl(
                db_name, record_id, generate_prd, score_prd, upload_prd_to_s3,
                claim_count=claim_count,
            )

    def _run_prd_generation_bg_impl(
        self, db_name, record_id, generate_prd, score_prd, upload_prd_to_s3,
        claim_count,
    ):
        # Wall-clock anchor for the whole PRD-gen pipeline. Every phase log
        # below reports `+Ns` elapsed from here, so a stuck job's last log
        # line tells you exactly which phase it died/hung in.
        _t0 = time.monotonic()

        def _elapsed():
            return time.monotonic() - _t0

        _logger.info(
            "[leviathan][job=%s] PRD-GEN worker picked up job (pid=%d)",
            record_id, os.getpid(),
        )
        # Chatter: tasker-visible breadcrumb that the worker pod has
        # claimed this row. Includes the pod hostname so during stage
        # load tests we can see which physical pod handled the job
        # without cross-referencing Loki.
        try:
            from markupsafe import escape as _esc
            self._post_message_with_cursor(
                db_name, record_id,
                f"<b>Worker picked up</b> &mdash; pod "
                f"{_esc(os.environ.get('HOSTNAME', 'unknown'))}, "
                f"pid {os.getpid()}"
            )
        except Exception:
            pass
        try:
            # === PHASE 1: Read config and extraction data ===
            _logger.info(
                "[leviathan][job=%s] PHASE 1 (+%.1fs): reading config + "
                "extraction data", record_id, _elapsed(),
            )
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return

                ICP = env["ir.config_parameter"].sudo()
                config = {
                    "inference_arn": ICP.get_param("leviathan.bedrock_inference_arn"),
                    "region": ICP.get_param("leviathan.bedrock_region") or "us-east-1",
                    "bedrock_access_key": ICP.get_param("leviathan.bedrock_access_key_id"),
                    "bedrock_secret_key": ICP.get_param("leviathan.bedrock_secret_access_key"),
                    "s3_bucket": ICP.get_param("leviathan.s3_bucket"),
                    "s3_key_id": ICP.get_param("leviathan.s3_access_key_id"),
                    "s3_secret": ICP.get_param("leviathan.s3_secret_access_key"),
                    "s3_region": ICP.get_param("leviathan.s3_region"),
                    "s3_folder": ICP.get_param("leviathan.s3_folder") or "leviathan",
                    "cdn_url": ICP.get_param("leviathan.s3_cdn_url"),
                    # Captured here (PHASE 1 cursor scope) so PHASE 2 can
                    # decide image-attachment policy after this cursor
                    # closes — reading ICP after the `with` block exits
                    # raises `psycopg2.InterfaceError: Cursor already
                    # closed` because the bound env points at the dead cr.
                    "prd_include_images": (
                        ICP.get_param("leviathan.prd_include_images")
                        in ("1", "True", "true", "yes", "on", True)
                    ),
                    "prd_max_images": int(
                        ICP.get_param("leviathan.prd_max_images") or 4
                    ),
                }
                # Category precedence (May 2026 product rule):
                #   1. If admin/tasker has explicitly set category_id on the
                #      task → use that. The user choice always wins.
                #   2. Otherwise → fall back to whatever the Lambda auto-
                #      classified during extraction (stored in
                #      site_discovery_json.category, and also already baked
                #      into the prd_prompt's metadata block by the Lambda).
                # Scoring/QC + the PRD-prompt substitution both honour this.
                _lambda_category = (
                    (record.site_discovery_json or {}).get("category")
                    or "Normal Website"
                )
                job_data = {
                    "name": record.name,
                    "prd_prompt": record.prd_prompt,
                    "category_name": (
                        record.category_id.name if record.category_id
                        else _lambda_category
                    ),
                    "category_is_explicit": bool(record.category_id),
                    "url": record.url,
                    "site_discovery_json": record.site_discovery_json,
                    "user_id": record.user_id.id if record.user_id else False,
                    "partner_id": record.user_id.partner_id.id if record.user_id and record.user_id.partner_id else False,
                    "screenshot_keys": record.screenshot_keys or [],
                    "asset_keys": record.asset_keys or [],
                }

                prd_system_prompt = record._get_prd_system_prompt()
                qc_system_prompt = record._get_qc_system_prompt()

                if not config["inference_arn"]:
                    _logger.error(
                        "[leviathan][job=%s] PHASE 1 abort: Bedrock inference "
                        "ARN not configured", record_id,
                    )
                    record.write({
                        "state": "failed",
                        "error_message": "Bedrock inference ARN not configured",
                        "completed_at": fields.Datetime.now(),
                    })
                    return
                if not job_data["prd_prompt"]:
                    _logger.error(
                        "[leviathan][job=%s] PHASE 1 abort: no prd_prompt on "
                        "record — extraction produced nothing usable", record_id,
                    )
                    record.write({
                        "state": "failed",
                        "error_message": "No extraction data available for PRD generation",
                        "completed_at": fields.Datetime.now(),
                    })
                    return

                # PHASE 1 fence-verify: the drainer already claimed this row
                # (set started_processing_at + incremented prd_claim_count).
                # We re-check the count under a row lock and bail if it has
                # advanced (= recovery re-claimed, another worker owns us).
                # See LEVIATHAN_POD_ARCHITECTURE.md §5.10.
                cr.execute(
                    "SELECT prd_claim_count, state FROM leviathan_job "
                    "WHERE id = %s FOR UPDATE",
                    (record_id,),
                )
                row = cr.fetchone()
                if (not row
                        or row[0] != claim_count
                        or row[1] not in ("generating", "scoring", "qc_running")):
                    cr.commit()
                    _logger.info(
                        "[leviathan][job=%s] fence-verify failed at PHASE 1 "
                        "(have_count=%s state=%s, expected_count=%s) — bail",
                        record_id,
                        row[0] if row else None,
                        row[1] if row else None,
                        claim_count,
                    )
                    return
                cr.execute(
                    "UPDATE leviathan_job "
                    "SET last_heartbeat = now() AT TIME ZONE 'UTC' "
                    "WHERE id = %s AND prd_claim_count = %s",
                    (record_id, claim_count),
                )
                cr.commit()

            # === PHASE 2: PRD generation ===
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): building Bedrock request "
                "(text-only, category=%s, prd_prompt=%dB)",
                record_id, _elapsed(), job_data["category_name"],
                len(job_data["prd_prompt"] or ""),
            )
            # PRD-gen image attachment: opt-in via the
            # `leviathan.prd_include_images` System Parameter (default
            # OFF). When OFF: text-only — the Lambda's build_prd_prompt
            # already encoded the visual extraction as text into
            # `prd_prompt`. When ON: top-N screenshots from S3 are
            # attached as Bedrock image blocks. See §25 of the
            # architecture doc.
            screenshot_blocks = []
            if config.get("prd_include_images"):
                screenshot_blocks = _load_screenshot_blocks_from_s3(
                    screenshot_keys=job_data.get("screenshot_keys") or [],
                    max_n=config.get("prd_max_images") or 4,
                    s3_bucket=config.get("s3_bucket") or "",
                    s3_region=config.get("s3_region") or "us-east-1",
                    s3_access_key_id=config.get("s3_key_id") or "",
                    s3_secret_access_key=config.get("s3_secret") or "",
                    log_prefix=f"leviathan][job={record_id}",
                )

            # Category precedence applied to the prd_prompt text:
            #   - If admin/tasker has set category_id on the task, override
            #     the Lambda's baked-in "- **Category:** X" line with
            #     the explicit category (user choice always wins).
            #   - If category_id is unset, leave the Lambda's
            #     auto-classified value in place — that's the
            #     authoritative fallback when the user hasn't picked.
            import re as _re
            current_category = job_data["category_name"]
            if job_data["category_is_explicit"]:
                prd_prompt_text = _re.sub(
                    r"^(\s*-\s*\*\*Category:\*\*\s+).+$",
                    lambda m: m.group(1) + current_category,
                    job_data["prd_prompt"],
                    count=1,
                    flags=_re.MULTILINE,
                )
            else:
                prd_prompt_text = job_data["prd_prompt"]

            # AUTHORITATIVE CATEGORY DIRECTIVE — only emitted when the user
            # has explicitly chosen the category. The metadata-block
            # substitution above changes ONE line; Bedrock can still look at
            # the extraction body (tech stack, WebGL signals, GSAP counts)
            # and write a PRD whose narrative reflects the auto-classified
            # category instead. This directive forces Bedrock to honour the
            # user's choice in the Category Addendum and category-dependent
            # guidance, not just the metadata block. When category_id is
            # unset, we don't emit this — Bedrock should infer freely from
            # the body since the user hasn't expressed a preference.
            if job_data["category_is_explicit"]:
                category_contract = (
                    f"AUTHORITATIVE CATEGORY (HARD): the user has explicitly "
                    f"chosen '{current_category}' as this site's category. "
                    f"Use THIS category throughout the PRD — in the metadata "
                    f"block, in the Category Addendum after Section 5, and "
                    f"in all category-dependent guidance. DO NOT infer a "
                    f"different category from the extracted tech stack, "
                    f"WebGL signals, GSAP/scroll signals, or visual data. "
                    f"The user's choice supersedes automated detection.\n\n"
                )
            else:
                category_contract = ""

            # Reinforce word-count contract at the user-message level. The
            # Odoo system prompt already says 4,000-5,000 words, but Bedrock
            # under-delivers (often ~3,000) without an emphatic user-side
            # reminder right before the data block. Keep this in sync with
            # services/scoring_service.PRD_MAX_WORDS and qc_service ranges.
            word_count_contract = (
                "WORD COUNT CONTRACT (HARD): produce 4,000-5,000 words. "
                "5,000 is a hard ceiling, NEVER exceed. PRDs under 4,000 "
                "words are flagged BELOW TARGET by automated QC and "
                "rejected. Use the full word budget — comprehensive over "
                "concise. Every animation needs duration + easing, every "
                "color needs hex + brand-name, every page follows A-F.\n\n"
            )

            # Build multimodal content: screenshots + extraction text.
            # Order matters for Bedrock attention — category contract first
            # (highest priority directive when set), then word count, then
            # data. The data also contains the substituted metadata block.
            content_blocks = list(screenshot_blocks)
            content_blocks.append({"text": (
                f"{category_contract}"
                f"{word_count_contract}"
                f"Below is the extracted website data. "
                f"Write the complete PRD following all rules.\n\n"
                f"---\n\n{prd_prompt_text}"
            )})
            messages = [{"role": "user", "content": content_blocks}]

            # Full transparency: capture the LLM interaction for audit.
            # The persisted extraction_prompt is the EFFECTIVE one (with
            # category corrected + word-count contract), not the raw Lambda
            # one, so audit/replay matches what Bedrock actually saw.
            llm_trace = {
                "prd_system_prompt": prd_system_prompt,
                "extraction_prompt": prd_prompt_text,
                "category_at_generation": current_category,
                "category_source": (
                    "tasker_or_admin" if job_data["category_is_explicit"]
                    else "lambda_auto_classified"
                ),
                "screenshots_attached": len(screenshot_blocks),
                "attempts": [],
                "qc": {},
            }

            if self._is_cancelled(db_name, record_id):
                self._write_fenced(db_name, record_id, claim_count, {
                    "state": "draft",
                    "error_message": "Cancelled during generation",
                    "completed_at": fields.Datetime.now(),
                    "pipeline_status": "",
                })
                return

            if not self._write_fenced(db_name, record_id, claim_count, {
                "last_heartbeat": fields.Datetime.now(),
                "pipeline_status": "Generating PRD (Bedrock)",
                "current_phase": "generating.calling_bedrock",
            }):
                return

            # Single PRD-generation call — no score-driven retry loop.
            # Transient Bedrock errors are retried inside generate_prd
            # (LEVIATHAN_BEDROCK_INNER_RETRIES); a hard failure here marks
            # the job failed so the tasker can re-run it from the UI.
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): calling Bedrock for PRD "
                "generation (text-only)",
                record_id, _elapsed(),
            )
            _bedrock_t0 = time.monotonic()
            best_prd_text = generate_prd(
                inference_arn=config["inference_arn"],
                region=config["region"],
                system_prompt=prd_system_prompt,
                messages=messages,
                access_key_id=config["bedrock_access_key"],
                secret_access_key=config["bedrock_secret_key"],
            )
            _bedrock_dt = time.monotonic() - _bedrock_t0
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): Bedrock PRD returned in "
                "%.1fs — %d chars / ~%d words",
                record_id, _elapsed(), _bedrock_dt,
                len(best_prd_text or ""), len((best_prd_text or "").split()),
            )
            # Chatter: Bedrock done. The duration is the single most
            # useful number for diagnosing slow-but-not-stuck jobs;
            # surfacing it inline means we don't have to grep Loki.
            try:
                self._post_message_with_cursor(
                    db_name, record_id,
                    f"<b>Bedrock PRD generated</b> in {_bedrock_dt:.1f}s "
                    f"&mdash; {len(best_prd_text or '')} chars / "
                    f"~{len((best_prd_text or '').split())} words"
                )
            except Exception:
                pass

            # Surface the PRD to the UI the moment it exists — the tasker
            # shouldn't have to wait through scoring + the multi-minute QC
            # call to read it. The `state` change emits the bus notification
            # that reloads the form; the PRD tab (invisible="not prd_text")
            # then appears. Editing stays gated on state == 'done'.
            if not self._write_fenced(db_name, record_id, claim_count, {
                "state": "scoring",
                "prd_text": best_prd_text,
                "prd_text_html": _markdown_to_html(best_prd_text),
                "last_heartbeat": fields.Datetime.now(),
                "pipeline_status": "PRD generated — scoring",
            }):
                return

            best_score_report = score_prd(
                prd_text=best_prd_text,
                category=job_data["category_name"],
            )
            best_score = best_score_report["total_score"]
            best_grade = best_score_report["grade"]
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): scored %s/%s grade=%s",
                record_id, _elapsed(), best_score,
                best_score_report.get("max_score", 100), best_grade,
            )
            # Chatter: scoring complete. Score + grade together are
            # the primary "is this PRD any good" signal a tasker reads
            # before looking at QC verdict.
            try:
                from markupsafe import escape as _esc
                self._post_message_with_cursor(
                    db_name, record_id,
                    f"<b>Scoring complete</b> &mdash; "
                    f"{best_score}/{best_score_report.get('max_score', 100)}, "
                    f"grade {_esc(str(best_grade or '?'))}"
                )
            except Exception:
                pass

            llm_trace["attempts"].append({
                "attempt": 1,
                "prd_text": best_prd_text,
                "score": best_score,
                "grade": best_grade,
                "score_report": best_score_report,
            })

            # Upload to S3
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): uploading PRD to S3",
                record_id, _elapsed(),
            )
            prd_url = upload_prd_to_s3(
                prd_text=best_prd_text,
                job_name=job_data["name"],
                bucket=config["s3_bucket"],
                access_key_id=config["s3_key_id"],
                secret_key=config["s3_secret"],
                region=config["s3_region"],
                folder=config["s3_folder"],
                cdn_url=config["cdn_url"],
            )

            # === PHASE 3: QC ===
            # Enter `qc_running` and publish the score now — the PRD and its
            # score stay visible (statusbar shows "QC Running") while the
            # multi-minute QC Bedrock call runs. This write also pulses the
            # heartbeat so a long QC call can't trip the watchdog mid-work.
            if not self._write_fenced(db_name, record_id, claim_count, {
                "state": "qc_running",
                "llm_attempts": 1,
                "score": best_score,
                "grade": best_grade,
                "score_report_json": best_score_report,
                "last_heartbeat": fields.Datetime.now(),
                "pipeline_status": "Scored — running QC",
                "current_phase": "scoring.qc_review",
            }):
                return

            qc_verdict = "not_shippable"
            qc_report = ""
            _logger.info(
                "[leviathan][job=%s] PHASE 3 (+%.1fs): starting QC "
                "(state=scoring)", record_id, _elapsed(),
            )
            _qc_t0 = time.monotonic()
            try:
                from ..services.qc_service import run_qc

                extraction_artifacts = {}
                if job_data["site_discovery_json"]:
                    extraction_artifacts["site_discovery"] = job_data["site_discovery_json"]

                qc_result = run_qc(
                    prd_text=best_prd_text,
                    extraction_data=extraction_artifacts,
                    site_discovery=job_data["site_discovery_json"] or {},
                    url=job_data["url"],
                    category=job_data["category_name"],
                    inference_arn=config["inference_arn"],
                    region=config["region"],
                    access_key_id=config["bedrock_access_key"],
                    secret_access_key=config["bedrock_secret_key"],
                    qc_system_prompt=qc_system_prompt,
                    screenshot_blocks=screenshot_blocks,
                )
                qc_verdict = qc_result["verdict"]
                qc_report = qc_result["report"]
                _logger.info(
                    "[leviathan][job=%s] PHASE 3 (+%.1fs): QC done in %.1fs — "
                    "verdict=%s (critical=%s high=%s medium=%s low=%s)",
                    record_id, _elapsed(), time.monotonic() - _qc_t0,
                    qc_verdict, qc_result.get("issues_critical"),
                    qc_result.get("issues_high"), qc_result.get("issues_medium"),
                    qc_result.get("issues_low"),
                )
                # Chatter: QC verdict with traffic-light colour so the
                # tasker can see at a glance whether the PRD passed.
                # Same colour mapping as the form-view's QC badge.
                try:
                    _verdict_color = {
                        "shippable": "#28a745",       # green
                        "needs_fixes": "#ffc107",     # amber
                        "not_shippable": "#dc3545",   # red
                    }.get(qc_verdict, "#6c757d")
                    self._post_message_with_cursor(
                        db_name, record_id,
                        f'<b>QC verdict:</b> '
                        f'<span style="color:{_verdict_color};'
                        f'font-weight:600;">{qc_verdict}</span>'
                    )
                except Exception:
                    pass
            except Exception as qc_exc:
                _logger.warning(
                    "[leviathan][job=%s] PHASE 3 (+%.1fs): QC FAILED after "
                    "%.1fs: %s (fail-closed: not_shippable)",
                    record_id, _elapsed(), time.monotonic() - _qc_t0, qc_exc,
                )
                qc_verdict = "not_shippable"
                qc_report = f"QC evaluation failed: {qc_exc}\n\nVerdict defaulted to NOT SHIPPABLE (fail-closed policy)."

            llm_trace["qc"] = {
                "qc_system_prompt": qc_system_prompt,
                "verdict": qc_verdict,
                "report": qc_report,
            }

            # Mid-stage publish: surface qc_report + qc_verdict the MOMENT QC
            # returns, before PHASE 4's atomic final write. The tasker sees
            # the QC verdict immediately instead of waiting for PHASE 4 to
            # complete. State stays `qc_running` — the bus notification fires
            # because `pipeline_status` is in vals (§5.11). See §5.15.B.
            if not self._write_fenced(db_name, record_id, claim_count, {
                "qc_report": qc_report,
                "qc_verdict": qc_verdict,
                "pipeline_status": "QC complete — finalizing",
                "current_phase": "scoring.qc_complete",
                "last_heartbeat": fields.Datetime.now(),
            }):
                return

            # === PHASE 4: Write final results ===
            _logger.info(
                "[leviathan][job=%s] PHASE 4 (+%.1fs): writing final results",
                record_id, _elapsed(),
            )
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)

                # PHASE 4 fence check — under the SAME cursor as the
                # subsequent write so the read + write are atomic w.r.t.
                # a concurrent drainer recovery. If we lost the claim,
                # bail without touching the row. See §5.4 / §5.10.
                if claim_count is not None:
                    cr.execute(
                        "SELECT prd_claim_count FROM leviathan_job "
                        "WHERE id = %s FOR UPDATE",
                        (record_id,),
                    )
                    row = cr.fetchone()
                    if not row or row[0] != claim_count:
                        cr.commit()
                        _logger.warning(
                            "[leviathan][job=%s] fence lost at PHASE 4 "
                            "(have=%s expected=%s) — another worker owns it; "
                            "discarding result", record_id,
                            row[0] if row else None, claim_count,
                        )
                        return

                started = record.started_at
                duration = (
                    (fields.Datetime.now() - started).total_seconds()
                    if started else 0
                )

                # Single atomic write — if the job ran via_batch, fold the
                # auto-release back to the pool into the same write rather
                # than emitting `job_done` between two state writes. The old
                # ordering let the frontend reload after `job_done`, hit the
                # user_id-scoped record rule with user_id already False, and
                # the row would vanish from the user's list view.
                final_vals = {
                    "state": "done",
                    "prd_text": best_prd_text,
                    "prd_text_html": _markdown_to_html(best_prd_text),
                    "score": best_score,
                    "grade": best_grade,
                    "score_report_json": best_score_report,
                    "prd_url": prd_url,
                    "qc_verdict": qc_verdict,
                    "qc_report": qc_report,
                    "llm_trace_json": llm_trace,
                    "completed_at": fields.Datetime.now(),
                    "duration_seconds": duration,
                    "pipeline_status": "Done",
                    # Clear current_phase on terminal state — empty key
                    # has no _PHASE_LABELS entry, so the sub-step header
                    # disappears from stage_progress_html. Total/duration
                    # remains visible via the finished-state branch.
                    "current_phase": "",
                    # Persist the EFFECTIVE prd_prompt (post-category-substitution
                    # + word-count-contract) back to the record so the
                    # "Extraction Data (sent to LLM)" UI panel matches what
                    # Bedrock actually saw. Lambda's pristine original is still
                    # preserved in lambda_callback_json for audit.
                    # No-op write when category_is_explicit was False (text is
                    # identical to the existing field value).
                    "prd_prompt": prd_prompt_text,
                }
                was_via_batch = record.via_batch
                if was_via_batch:
                    final_vals.update({
                        "state": "not_assigned",
                        "via_batch": False,
                        "user_id": False,
                    })
                    _logger.info(
                        "Batch pipeline done for job %s — reset to not_assigned",
                        record_id,
                    )
                record.write(final_vals)
                cr.commit()

                # Bus emit AFTER commit, single channel. Frontend already
                # subscribes to `leviathan/job_state`; using the dedicated
                # `job_done` event was the source of the via_batch race.
                try:
                    env["bus.bus"]._sendone(
                        "leviathan_job_updates",
                        "leviathan/job_state",
                        {
                            "id": record_id,
                            "name": job_data["name"],
                            "state": final_vals["state"],
                            "released_to_pool": was_via_batch,
                        },
                    )
                except Exception:
                    _logger.debug("bus.bus notification failed for job %s (non-fatal)", record_id)

            _logger.info(
                "[leviathan][job=%s] PRD-GEN PIPELINE COMPLETE in %.1fs — "
                "final_state=%s score=%s qc=%s",
                record_id, _elapsed(), final_vals["state"],
                best_score, qc_verdict,
            )

        except Exception as exc:
            _logger.exception(
                "[leviathan][job=%s] PRD generation FAILED at +%.1fs: %s",
                record_id, _elapsed(), exc,
            )
            try:
                if claim_count is None:
                    # FLAG-OFF: preserve existing Phase-1 behaviour — mark
                    # the row `failed` immediately. The user can click Retry
                    # to re-run via the standard recovery path.
                    fail_vals = {
                        "state": "failed",
                        "error_message": str(exc)[:500],
                        "completed_at": fields.Datetime.now(),
                        "pipeline_status": f"Failed: {str(exc)[:80]}",
                        "current_phase": "",
                    }
                    _trace = locals().get("llm_trace")
                    if _trace:
                        fail_vals["llm_trace_json"] = _trace
                    self._write_with_cursor(db_name, record_id, fail_vals)
                else:
                    # FLAG-ON: bump prd_failure_count and un-claim. Next
                    # drainer tick will re-claim and retry. When the count
                    # reaches leviathan.prd_max_attempts, the drainer's step 1
                    # marks the row failed (poison cap). See §5.7.
                    with Registry(db_name).cursor() as cr:
                        cr.execute(
                            "UPDATE leviathan_job "
                            "SET prd_failure_count = prd_failure_count + 1, "
                            "    started_processing_at = NULL, "
                            "    error_message = %s, "
                            "    pipeline_status = %s, "
                            "    last_heartbeat = now() AT TIME ZONE 'UTC' "
                            "WHERE id = %s AND prd_claim_count = %s",
                            (str(exc)[:500],
                             f"Failed (will retry): {str(exc)[:80]}",
                             record_id, claim_count),
                        )
                        won = cr.rowcount > 0
                        cr.commit()
                    if won:
                        _logger.info(
                            "[leviathan][job=%s] flag-ON failure: bumped "
                            "prd_failure_count and un-claimed; drainer will "
                            "retry or poison-cap it next tick", record_id,
                        )
                    else:
                        _logger.info(
                            "[leviathan][job=%s] fence lost during failure "
                            "handling — recovery already owns it; nothing to do",
                            record_id,
                        )
            except Exception:
                _logger.error("[leviathan][job=%s] failed to mark as failed", record_id)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _write_with_cursor(self, db_name, record_id, vals):
        """Write values to a record using a short-lived cursor."""
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[self._name].browse(record_id)
            if record.exists():
                if "state" in vals:
                    # Every background state transition flows through here —
                    # log it so a job's full state history is reconstructable
                    # from grep alone.
                    _logger.info(
                        "[leviathan][job=%s] state %s -> %s (bg write)",
                        record_id, record.state, vals["state"],
                    )
                record.write(vals)
                # Notify on state OR pipeline_status — the latter lets the
                # tasker see mid-phase progress even when state hasn't
                # transitioned (see LEVIATHAN_POD_ARCHITECTURE.md §5.15).
                if "state" in vals or "pipeline_status" in vals:
                    try:
                        env["bus.bus"]._sendone(
                            "leviathan_job_updates",
                            "leviathan/job_state",
                            {"id": record_id,
                             "state": vals.get("state"),
                             "pipeline_status": vals.get("pipeline_status")},
                        )
                    except Exception:
                        pass
            else:
                _logger.warning(
                    "[leviathan][job=%s] _write_with_cursor: record no longer "
                    "exists — write of %s dropped",
                    record_id, sorted(vals.keys()),
                )
            cr.commit()

    def _post_message_with_cursor(self, db_name, record_id, body_html):
        """Post a chatter message (mt_note) on a job from a background
        thread, using a short-lived cursor so it never poisons the
        caller's transaction.

        ``body_html`` is wrapped in ``markupsafe.Markup`` so Odoo's
        chatter renders it as HTML (allowing the bold / colour spans we
        use for verdicts and timings). The CALLER is responsible for
        escaping any user-supplied substring with ``markupsafe.escape``
        — pass safe HTML or pre-escaped strings only.

        Best-effort: any exception during message_post is logged at
        WARNING and swallowed. Chatter posting is informational; a
        pipeline run must NEVER fail because a chatter write failed.
        """
        try:
            from markupsafe import Markup
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if record.exists():
                    record.message_post(
                        body=Markup(body_html),
                        subtype_xmlid="mail.mt_note",
                    )
                    cr.commit()
        except Exception:
            _logger.warning(
                "[leviathan][job=%s] chatter post failed (non-fatal): %s",
                record_id, body_html[:120], exc_info=True,
            )

    def _chatter_post(self, body_html):
        """Foreground-thread chatter wrapper (no cursor management).

        Used inside action methods (action_run, action_cancel, etc.)
        where ``self`` is a real recordset bound to the request's
        cursor. The bg-thread variant lives in ``_post_message_with_cursor``.

        Same best-effort semantics — chatter is informational; never let
        it bubble an exception that would roll back the user's action.
        """
        try:
            from markupsafe import Markup
            self.ensure_one()
            self.message_post(
                body=Markup(body_html),
                subtype_xmlid="mail.mt_note",
            )
        except Exception:
            _logger.warning(
                "[leviathan][job=%s] chatter post failed (non-fatal): %s",
                getattr(self, "id", "?"), body_html[:120], exc_info=True,
            )

    def _write_fenced(self, db_name, record_id, claim_count, vals):
        """Fence-checked write for Phase-2 queue workers.

        Writes `vals` only if `prd_claim_count` still equals `claim_count` —
        i.e. recovery has NOT taken the row away. Returns True if we still
        own it (caller continues); False if a re-claim happened (caller
        must bail). See docs/LEVIATHAN_POD_ARCHITECTURE.md §5.4, §5.11.

        When `claim_count is None` (flag-OFF / Phase-1 path), this falls
        through to `_write_with_cursor` — no fencing, behaviour unchanged.
        """
        if claim_count is None:
            self._write_with_cursor(db_name, record_id, vals)
            return True
        with Registry(db_name).cursor() as cr:
            # Lock the row so the prd_claim_count read + the conditional
            # write are atomic w.r.t. a concurrent drainer recovery.
            cr.execute(
                "SELECT prd_claim_count FROM leviathan_job "
                "WHERE id = %s FOR UPDATE",
                (record_id,),
            )
            row = cr.fetchone()
            if not row or row[0] != claim_count:
                cr.commit()
                _logger.warning(
                    "[leviathan][job=%s] fence lost during write "
                    "(have=%s expected=%s) — bailing without writing %s",
                    record_id, row[0] if row else None, claim_count,
                    sorted(vals.keys()),
                )
                return False
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[self._name].browse(record_id)
            if "state" in vals:
                _logger.info(
                    "[leviathan][job=%s] state %s -> %s (fenced write, count=%s)",
                    record_id, record.state, vals["state"], claim_count,
                )
            record.write(vals)
            cr.commit()
            # Notify on EITHER a state change OR a pipeline_status update
            # so mid-phase progress reloads the form (§5.15).
            if "state" in vals or "pipeline_status" in vals:
                try:
                    env["bus.bus"]._sendone(
                        "leviathan_job_updates",
                        "leviathan/job_state",
                        {"id": record_id,
                         "state": vals.get("state"),
                         "pipeline_status": vals.get("pipeline_status")},
                    )
                except Exception:
                    pass
            return True

    def _upload_artifacts_bg(self, db_name, record_id, artifacts, s3_config):
        """Background: upload extraction artifacts to S3 and write the
        resulting ``artifacts_url`` back to the record.

        Decouples S3 upload from the webhook handler so the webhook returns
        in <50 ms regardless of artifact count. PRD generation does not
        depend on ``artifacts_url`` — it reads ``prd_prompt`` /
        ``screenshot_keys`` / ``asset_keys`` which are already set by the
        webhook before this bg job is scheduled. So both run concurrently.

        Failure semantics: an S3 upload failure here logs loudly but does
        NOT mark the job failed. PRD generation continues. The tasker will
        see ``artifacts_url`` blank in the form view and can re-fetch
        manually; the deliverable is the PRD, not the raw artifacts dict.
        """
        from ..services.s3_service import (
            upload_artifacts_to_s3, get_artifacts_folder_url,
        )

        # Read the job name with a short-lived cursor so we don't hold a
        # DB connection across the multi-second S3 upload.
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return
                job_name = record.name
        except Exception:
            _logger.exception(
                "[leviathan][job=%s] artifacts upload: could not read record",
                record_id,
            )
            return

        try:
            upload_artifacts_to_s3(
                artifacts=artifacts,
                job_name=job_name,
                bucket=s3_config["bucket"],
                access_key_id=s3_config["key_id"],
                secret_key=s3_config["secret"],
                region=s3_config["region"],
                folder=s3_config["folder"],
                cdn_url=s3_config["cdn_url"],
            )
            artifacts_url = get_artifacts_folder_url(
                job_name=job_name,
                bucket=s3_config["bucket"],
                folder=s3_config["folder"],
                cdn_url=s3_config["cdn_url"],
            )
        except Exception:
            _logger.exception(
                "[leviathan][job=%s] artifacts S3 upload failed — tasker can "
                "supply manually; PRD generation is unaffected",
                record_id,
            )
            return

        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if record.exists():
                    record.write({"artifacts_url": artifacts_url})
                    try:
                        env["bus.bus"]._sendone(
                            "leviathan_job_updates",
                            "leviathan/job_state",
                            {
                                "id": record_id,
                                "artifacts_url": artifacts_url,
                                "state": record.state,
                            },
                        )
                    except Exception:
                        pass
                cr.commit()
            _logger.info(
                "[leviathan][job=%s] artifacts upload done: %s",
                record_id, artifacts_url,
            )
        except Exception:
            _logger.exception(
                "[leviathan][job=%s] artifacts upload: could not write "
                "artifacts_url back to record",
                record_id,
            )

    # ------------------------------------------------------------------
    # Cron: PRD Queue Drainer (Phase-2)
    # See docs/LEVIATHAN_POD_ARCHITECTURE.md §5.2 — §5.7.
    # All work is gated on `_prd_queue_enabled()`; when the flag is OFF this
    # is a no-op and the Phase-1 in-process pool path keeps running unchanged.
    # ------------------------------------------------------------------

    @api.model
    def _worker_scaler_load(self):
        """Authoritative LOAD count for the K8s worker scaler.

        Counts rows that should pressure scale-up: anything in a PRD-side
        non-terminal state that hasn't been user-cancelled. Mirrors
        vegeta's ``state='generating' AND cancel_requested=false`` query
        but widens the state list to Leviathan's actual queue states
        (``generating``, ``scoring``, ``qc_running``). ``extracting`` is
        deliberately excluded — extraction runs in Lambda, not in worker
        pods, so it must not influence pod count.
        """
        return self.sudo().search_count([
            ("state", "in", ("generating", "scoring", "qc_running")),
            ("cancel_requested", "=", False),
        ])

    @api.model
    def _cron_dispatch_prd_jobs(self):
        """Cron (1 min): scale the worker Deployment to match queue depth.

        Ported from ``vegeta.job._cron_dispatch_prd_jobs`` (v19.0.2.6.0).
        In ``prd_execution_mode=worker`` this method patches the
        ``leviathan-prd-worker`` Kubernetes Deployment's replica count
        based on PRD load. Workers themselves claim jobs via
        ``FOR UPDATE SKIP LOCKED`` — this cron only adjusts pod count.

        In ``prd_execution_mode=inprocess`` (local single-process dev)
        this method is a no-op; the in-Odoo ``_cron_prd_queue_drainer``
        owns the work loop. Kubernetes is not involved.

        Advisory-locked so multiple Odoo backend pods don't all try to
        scale the Deployment simultaneously — only one Odoo pod's tick
        actually patches the API each minute.
        """
        if not self._prd_queue_enabled():
            return
        if self._prd_execution_mode() != "worker":
            return

        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(hashtext('leviathan.prd_scaler')::bigint)"
        )
        if not self.env.cr.fetchone()[0]:
            _logger.debug(
                "[leviathan] PRD scaler: lock held elsewhere, skipping"
            )
            return
        try:
            from ..services.k8s_scaler import run_worker_deployment_scaler
            load = self._worker_scaler_load()
            run_worker_deployment_scaler(self.env, load)
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(hashtext('leviathan.prd_scaler')::bigint)"
            )

    def _cron_prd_queue_drainer(self):
        """ir.cron entry-point. Applies the *cron-only* gates, then delegates
        to :meth:`_prd_drain_once` for the actual drain body.

        Runs every 1 minute. The pod-role guard (`LEVIATHAN_ROLE=ui`) lets
        the worker pod always run it and the UI pod never run it, independent
        of which pod's cron worker happened to win the ir.cron tick.

        In ``prd_execution_mode=worker`` this method short-circuits so the
        standalone worker process is the only drainer cluster-wide —
        prevents double-claim races between the cron-driven and
        process-driven drainers. The standalone worker bypasses this gate
        by calling :meth:`_prd_drain_once` directly.
        """
        if not self._prd_queue_enabled():
            return                                            # C6 — Phase-1 unchanged
        if self._prd_execution_mode() == "worker":
            return                                            # standalone worker owns the loop
        if os.environ.get("LEVIATHAN_ROLE", "worker") == "ui":
            return                                            # pod isolation guard (§6.2)
        self._prd_drain_once()

    @api.model
    def _prd_drain_once(self):
        """One drainer pass (lock + poison-cap + stale-recovery + claim).

        Called from both the ir.cron entry-point (after the cron-only gates
        in :meth:`_cron_prd_queue_drainer`) AND from the standalone worker
        process (``custom_addons/leviathan/worker/run_prd.py``). Shared body,
        one correctness analysis, one PG advisory lock.

        The advisory lock guarantees only one drain pass runs cluster-wide
        at a time — safe even if a stray cron tick happens to overlap
        a worker tick during a mode flip.
        """
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(hashtext('leviathan.prd_drainer')::bigint)"
        )
        if not self.env.cr.fetchone()[0]:
            # Another drainer tick is still running across the cluster.
            return
        try:
            self._prd_queue_apply_live_throttles()
            self._prd_queue_fail_poison()
            self._prd_queue_recover_stale()
            self._prd_queue_claim_and_dispatch()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(hashtext('leviathan.prd_drainer')::bigint)"
            )

    def _prd_queue_fail_poison(self):
        """Step 1: mark jobs that have burned through `prd_max_attempts` as
        `failed` (poison-job cap). prd_failure_count is bumped only on
        genuine worker exceptions (§5.7) — pod restarts do NOT increment it.
        """
        max_failures = int(
            self.env["ir.config_parameter"].sudo()
                .get_param("leviathan.prd_max_attempts", "3")
        )
        # Build error_message with PostgreSQL ``||`` instead of
        # ``format('PRD queue: %s ...', n)`` because psycopg2 client-side
        # parameter substitution counts EVERY ``%s`` in the query string
        # — including ones inside SQL literals — and explodes with
        # ``IndexError: tuple index out of range`` when the placeholder
        # count exceeds the params tuple. ``||`` concatenation sidesteps
        # the issue entirely and produces an identical message.
        self.env.cr.execute(
            """
            UPDATE leviathan_job
               SET state = 'failed',
                   error_message = 'PRD queue: ' || prd_failure_count
                                   || ' failures (poison cap reached)',
                   pipeline_status = 'Failed (poison cap reached)',
                   completed_at  = now() AT TIME ZONE 'UTC'
             WHERE state = 'generating'
               AND auto_continue       = true
               AND started_processing_at IS NULL
               AND prd_failure_count   >= %s
            RETURNING id, prd_failure_count
            """,
            (max_failures,),
        )
        rows = self.env.cr.fetchall()
        self.env.cr.commit()
        if rows:
            _logger.warning(
                "[leviathan] drainer: poisoned %d job(s) at cap=%d: %s",
                len(rows), max_failures,
                [{"id": r[0], "failures": r[1]} for r in rows],
            )

    def _prd_queue_recover_stale(self):
        """Step 2: re-queue jobs whose worker is genuinely gone.

        TWO-GATE reconcile — ported from vegeta v19.0.2.5.0 after a real
        prod incident where the single-gate "heartbeat older than 15 min"
        check caused a double-Bedrock-spend. The failure mode was:
        saturated Postgres pool, heartbeat writes silently fail for ~12
        min, reconciler triggers at 15 min, the *original* worker is
        still alive and finishes its PRD on Bedrock seconds after the
        recovered worker also finishes — same job billed twice.

        Two gates:

        * **Short-stale gate** — ``last_heartbeat`` older than
          ``leviathan.prd_short_stale_minutes`` (default 5) AND
          ``heartbeat_failure_count`` ≥
          ``leviathan.prd_heartbeat_failure_threshold`` (default 3).
          This says: we KNOW the worker has been actively failing to
          pulse, not just slow. Safe to recover early.
        * **Unconditional gate** — ``last_heartbeat`` older than
          ``leviathan.prd_stale_minutes`` (default 15). Backstop for
          the case where heartbeat writes never raised but never
          succeeded either (e.g. silent network partition that
          eventually heals after we already concluded the worker is
          dead).

        Both gates reset ``started_processing_at = NULL`` so the claim
        step in the same tick can re-claim them. The fence token
        (``prd_claim_count``) is NOT touched here — the next claim will
        increment it, automatically fencing out any zombie worker that
        somehow resumes (§5.6).

        The two-gate model preserves the "no double-Bedrock-spend"
        invariant because:
        - Workers experiencing real failures bump the counter on every
          missed pulse → they trip the short-stale gate quickly.
        - Workers experiencing slow-but-not-failing pulses (e.g. an
          actually-running 12-minute Bedrock call) keep
          ``heartbeat_failure_count == 0`` → only the 15-min
          unconditional gate fires, by which point the worker really
          IS dead.
        """
        stale_minutes = int(
            self.env["ir.config_parameter"].sudo()
                .get_param("leviathan.prd_stale_minutes", "15")
        )
        short_stale_minutes = int(
            self.env["ir.config_parameter"].sudo()
                .get_param("leviathan.prd_short_stale_minutes", "5")
        )
        hb_failure_threshold = int(
            self.env["ir.config_parameter"].sudo()
                .get_param(
                    "leviathan.prd_heartbeat_failure_threshold", "3"
                )
        )
        self.env.cr.execute(
            """
            UPDATE leviathan_job
               SET started_processing_at = NULL,
                   state                 = 'generating',
                   pipeline_status       = 'Recovered — re-queued for PRD worker'
             WHERE state IN ('generating', 'scoring', 'qc_running')
               AND auto_continue          = true
               AND started_processing_at IS NOT NULL
               AND (
                    -- Gate A: unconditional stale (full timeout).
                    last_heartbeat IS NULL
                 OR last_heartbeat <
                       (now() AT TIME ZONE 'UTC')
                       - (%s || ' minutes')::interval
                    -- Gate B: short-stale AND repeated heartbeat
                    -- failures observed. Counter is bumped by the
                    -- heartbeat aggregator's catch path; reset on
                    -- every successful pulse.
                 OR (
                       last_heartbeat <
                         (now() AT TIME ZONE 'UTC')
                         - (%s || ' minutes')::interval
                       AND heartbeat_failure_count >= %s
                 )
               )
            RETURNING id, heartbeat_failure_count,
                      EXTRACT(EPOCH FROM (
                          (now() AT TIME ZONE 'UTC') - last_heartbeat
                      ))::int AS stale_seconds
            """,
            (stale_minutes, short_stale_minutes, hb_failure_threshold),
        )
        rows = self.env.cr.fetchall()
        self.env.cr.commit()
        if rows:
            _logger.warning(
                "[leviathan] drainer: recovered %d stale job(s) via "
                "two-gate reconcile (long_gate=%dmin, "
                "short_gate=%dmin+failures>=%d): %s",
                len(rows), stale_minutes, short_stale_minutes,
                hb_failure_threshold,
                [
                    {
                        "id": r[0],
                        "hb_failures": r[1],
                        "stale_secs": r[2],
                    }
                    for r in rows
                ],
            )

    @api.model
    def _get_effective_pool_size(self):
        """Live pool cap. Read from Settings (`leviathan.prd_pool_size`)
        on every drainer tick, clamped against the boot-time hard ceiling
        ``_PRD_POOL_SIZE`` (env ``LEVIATHAN_PRD_POOL_SIZE``).

        Why a hard ceiling: the Python ``ThreadPoolExecutor.max_workers``
        is set at boot time and CANNOT be resized live. Settings can
        only LOWER the effective cap below the executor's slot count —
        raising Settings above the boot value would have no effect
        (claims would happen but submits would queue inside the pool).
        Operators tuning higher must bump the env var AND restart the
        worker pod; Settings is the live-tuning knob to *throttle down*
        without redeploy.

        The Bedrock semaphore (`leviathan.bedrock_max_concurrent`) is
        also clamped this way — see services/bedrock_service.py.

        Returns the clamped int.
        """
        try:
            settings_val = int(
                self.env["ir.config_parameter"].sudo()
                    .get_param("leviathan.prd_pool_size", str(_PRD_POOL_SIZE))
            )
        except (TypeError, ValueError):
            settings_val = _PRD_POOL_SIZE
        # Never below 1 (caller divides by free; 0 would deadlock the
        # drainer). Never above the boot-time pool size (the executor
        # can't accept more anyway).
        return max(1, min(settings_val, _PRD_POOL_SIZE))

    def _prd_queue_apply_live_throttles(self):
        """Re-read concurrency Settings on every drainer tick and apply
        them in-process. Called at the top of ``_prd_drain_once`` so the
        effective caps update within one poll interval after an operator
        clicks Save in Settings.

        Two knobs are live-tunable here:

        * ``leviathan.prd_pool_size`` — consulted by
          :meth:`_get_effective_pool_size` from inside
          :meth:`_prd_queue_claim_and_dispatch`. No-op work for this
          method beyond the read.
        * ``leviathan.bedrock_max_concurrent`` — actively reshapes the
          process-global semaphore via
          :func:`bedrock_service._apply_bedrock_live_throttle`.

        Both knobs can only LOWER the effective cap below the boot
        ceilings (``_PRD_POOL_SIZE`` / ``_BEDROCK_MAX_CONCURRENT``).
        Raising above the boot value requires a pod restart. Settings UI
        shows this constraint in the field help text.
        """
        try:
            from ..services.bedrock_service import (
                _apply_bedrock_live_throttle, _BEDROCK_MAX_CONCURRENT as _hard,
            )
            desired = int(
                self.env["ir.config_parameter"].sudo()
                    .get_param(
                        "leviathan.bedrock_max_concurrent", str(_hard)
                    )
            )
            _apply_bedrock_live_throttle(desired)
        except Exception:
            # Throttle is purely a brake — failing here is non-fatal,
            # callers proceed under the boot cap (the safe default).
            _logger.exception(
                "[leviathan] live-throttle application failed; "
                "Bedrock semaphore stays at boot cap"
            )

    def _prd_queue_claim_and_dispatch(self):
        """Step 3: atomically claim `free = pool_size - in_flight` queued rows
        with `FOR UPDATE SKIP LOCKED`, then submit each to the local pool.

        On submit failure (pool broken / rejected) — immediately un-claim so
        the row is picked up on the next tick instead of waiting 15 min for
        recovery (§5.3, review L-8). The fence token is the captured
        `prd_claim_count` from this claim.
        """
        # Read the effective pool size LIVE on every tick — Settings is
        # the source of truth, bounded by the static executor cap. This
        # is what makes "tune concurrency from the Settings UI" work
        # without a pod restart for tune-down operations.
        effective_pool = self._get_effective_pool_size()
        free = effective_pool - _prd_inflight_count()
        if free <= 0:
            _logger.info(
                "[leviathan] drainer tick: pool full (in_flight=%d / "
                "effective_pool=%d, hard_cap=%d) — no claims this tick",
                _prd_inflight_count(), effective_pool, _PRD_POOL_SIZE,
            )
            return
        max_failures = int(
            self.env["ir.config_parameter"].sudo()
                .get_param("leviathan.prd_max_attempts", "3")
        )
        self.env.cr.execute(
            """
            UPDATE leviathan_job
               SET started_processing_at = now() AT TIME ZONE 'UTC',
                   last_heartbeat        = now() AT TIME ZONE 'UTC',
                   prd_claim_count       = prd_claim_count + 1,
                   pipeline_status       = 'PRD worker assigned'
             WHERE id IN (
                 SELECT id FROM leviathan_job
                  WHERE state = 'generating'
                    AND auto_continue          = true
                    AND started_processing_at IS NULL
                    AND prd_failure_count     <  %s
                  ORDER BY prd_queued_at NULLS FIRST, id
                  FOR UPDATE SKIP LOCKED
                  LIMIT %s
             )
            RETURNING id, prd_claim_count
            """,
            (max_failures, free),
        )
        rows = self.env.cr.fetchall()
        self.env.cr.commit()

        # Depth metric — single line per tick for Loki/Grafana (§13).
        self.env.cr.execute(
            "SELECT count(*) FROM leviathan_job "
            "WHERE state = 'generating' AND auto_continue = true "
            "  AND started_processing_at IS NULL"
        )
        depth = self.env.cr.fetchone()[0]
        _logger.info(
            "[leviathan] drainer tick: claimed=%d free=%d in_flight=%d "
            "pool=%d depth=%d",
            len(rows), free, _prd_inflight_count(), _PRD_POOL_SIZE, depth,
        )

        if not rows:
            return

        db_name = self.env.cr.dbname
        for job_id, claim_count in rows:
            future = _submit_bg(
                f"prd-gen[job={job_id}](drainer)",
                self._run_prd_generation_bg, db_name, job_id, claim_count,
            )
            if future is None:
                # Submit failed (admission full / dead pool past self-heal).
                # Un-claim immediately so the next tick can re-pick it.
                # Guard with prd_claim_count so we only un-claim our OWN
                # claim — a concurrent recovery on this row would have
                # bumped the counter and we'd leave it alone.
                with Registry(db_name).cursor() as cr:
                    cr.execute(
                        "UPDATE leviathan_job "
                        "SET started_processing_at = NULL, "
                        "    pipeline_status = 'Queued — pool full, retrying next tick' "
                        "WHERE id = %s AND prd_claim_count = %s "
                        "  AND state = 'generating'",
                        (job_id, claim_count),
                    )
                    cr.commit()
                _logger.warning(
                    "[leviathan] drainer: un-claimed job=%s "
                    "(pool rejected submit)",
                    job_id,
                )

    # ------------------------------------------------------------------
    # Cron: Watchdog
    # ------------------------------------------------------------------

    def _cron_watchdog_stuck_jobs(self):
        """Recover tasks stuck in intermediate states beyond timeout thresholds.

        Two-stage policy:
          * First stuck hit  → AUTO-RETRY (smart: skip extraction if prd_prompt
            exists). Covers the 1% legitimate Bedrock / Lambda hiccup that
            otherwise needs a manual click for every stuck job.
          * Second stuck hit → mark failed for real, surface to the tasker.

        Cap is configurable via ``leviathan.watchdog_auto_retry_max`` (default
        1, set 0 to disable auto-retry).

        Extraction threshold defaults to 30 min (Lambda hard timeout is 15 min;
        anything past 30 min cannot still be a live Lambda — the callback
        didn't land, and waiting longer just delays recovery).
        """
        self.env.cr.execute("SELECT pg_try_advisory_lock(hashtext('leviathan.watchdog')::bigint)")
        locked = self.env.cr.fetchone()
        if not locked or not locked[0]:
            return

        ICP = self.env["ir.config_parameter"].sudo()
        # Lambda's hard cap is 15 min — past 30 min, the Lambda is definitely
        # done and the callback failed to land. Don't wait the old 60 min.
        extracting_threshold = int(
            ICP.get_param("leviathan.watchdog_extracting_minutes", "30")
        )
        generating_threshold = int(
            ICP.get_param("leviathan.watchdog_generating_minutes", "120")
        )
        auto_retry_max = int(
            ICP.get_param("leviathan.watchdog_auto_retry_max", "1")
        )

        try:
            # --- System-state heartbeat: one line every cron tick (5 min)
            # giving the live count of jobs in each running state. This is
            # the cheapest way to watch a backlog build: if `generating`
            # climbs tick over tick while `done` stays flat, the PRD pool
            # is not draining.
            counts = {}
            for st in ("extracting", "generating", "scoring", "qc_running"):
                counts[st] = self.search_count([("state", "=", st)])
            _logger.info(
                "[leviathan] watchdog tick: extracting=%d generating=%d "
                "scoring=%d qc_running=%d (thresholds: extract>%dmin generate>%dmin)",
                counts["extracting"], counts["generating"], counts["scoring"],
                counts["qc_running"], extracting_threshold, generating_threshold,
            )

            stale_extracting = self.search([
                ("state", "=", "extracting"),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=extracting_threshold),
                ),
            ])
            self._watchdog_recover_chunked(
                stale_extracting,
                reason=(
                    f"Watchdog: extraction timed out "
                    f"(no response for {extracting_threshold}+ minutes)"
                ),
                log_label=f"extracting >{extracting_threshold}min",
                auto_retry_max=auto_retry_max,
            )

            # Phase-2 gate: when the queue is enabled, the drainer
            # (`_cron_prd_queue_drainer`) owns all PRD-side recovery —
            # `_prd_queue_recover_stale` (§5.6) covers the stale-heartbeat
            # case and `_prd_queue_claim_and_dispatch` (§5.3) covers the
            # orphan case. Skip the watchdog's PRD-side blocks to avoid
            # double-dispatch. See LEVIATHAN_POD_ARCHITECTURE.md §5.14.
            if self._prd_queue_enabled():
                _logger.debug(
                    "[leviathan] watchdog: PRD-side recovery skipped "
                    "(queue enabled; drainer owns it)"
                )
            else:
                # `started_processing_at != False` excludes jobs sitting in
                # the _POOL queue waiting for a worker — they look stuck
                # (no heartbeat update) but no work has been attempted on
                # them. Without this guard, a 150-job batch on a 50-worker
                # pool false-fails the 20-30 tail jobs that are simply queued.
                stale_generating = self.search([
                    ("state", "in", ("generating", "scoring", "qc_running")),
                    ("started_processing_at", "!=", False),
                    (
                        "last_heartbeat",
                        "<",
                        fields.Datetime.now() - timedelta(minutes=generating_threshold),
                    ),
                ])
                self._watchdog_recover_chunked(
                    stale_generating,
                    reason=(
                        f"Watchdog: timed out "
                        f"(no progress for {generating_threshold}+ minutes)"
                    ),
                    log_label=f"generating/scoring >{generating_threshold}min",
                    auto_retry_max=auto_retry_max,
                )

                # --- P1-1 ORPHAN RECOVERY (Phase-1 only) ---
                # Jobs in generating/scoring with started_processing_at unset
                # have NEVER been claimed by a PRD worker. With Phase-1 in
                # effect, re-dispatch them via the in-process pool. (Phase-2
                # drainer claims these natively — see gate above.)
                orphan_threshold = int(
                    ICP.get_param("leviathan.watchdog_orphan_minutes", "8")
                )
                orphaned = self.search([
                    ("state", "in", ("generating", "scoring")),
                    ("started_processing_at", "=", False),
                    (
                        "last_heartbeat",
                        "<",
                        fields.Datetime.now() - timedelta(minutes=orphan_threshold),
                    ),
                ])
                if orphaned:
                    _logger.warning(
                        "[leviathan] watchdog: re-dispatching %d orphaned job(s) "
                        "(generating/scoring, never claimed, idle >%dmin): %s",
                        len(orphaned), orphan_threshold, orphaned.mapped("name"),
                    )
                    db_name = self.env.cr.dbname
                    for job in orphaned:
                        rid = job.id
                        self.env.cr.postcommit.add(
                            lambda rid=rid: _submit_bg(
                                f"prd-gen[job={rid}](wd-orphan)",
                                self._run_prd_generation_bg, db_name, rid,
                            )
                        )
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(hashtext('leviathan.watchdog')::bigint)")

    def _watchdog_recover_chunked(
        self, recordset, reason, log_label, auto_retry_max, chunk=25,
    ):
        """Per-chunk recover: auto-retry first stuck hit, mark failed second.

        Chunked-commit safety: if a single ``_mark_failed`` or auto-retry
        write raises, only the current chunk rolls back — previous chunks
        are committed, subsequent chunks still get attempted. The original
        watchdog had no such isolation; one bad row would silently undo the
        recovery of all the other stuck jobs.
        """
        if not recordset:
            return
        total = len(recordset)
        retried = 0
        marked = 0
        for offset in range(0, total, chunk):
            batch = recordset[offset:offset + chunk]
            try:
                for job in batch:
                    if job.watchdog_retry_count < auto_retry_max:
                        # First (and only) stuck hit — give it one auto-retry
                        # using the existing smart-retry pathway (skip
                        # re-extraction if prd_prompt is already there).
                        job._watchdog_auto_retry(log_label)
                        retried += 1
                    else:
                        _logger.warning(
                            "[leviathan][job=%s] watchdog: stuck %s — marking "
                            "FAILED (auto_retry_count=%d already at cap; "
                            "started_processing_at=%s, last_heartbeat=%s)",
                            job.name, log_label, job.watchdog_retry_count,
                            job.started_processing_at, job.last_heartbeat,
                        )
                        job._mark_failed(reason)
                        marked += 1
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "[leviathan] watchdog: chunk %d-%d of %d failed; "
                    "continuing with next chunk",
                    offset, offset + len(batch), total,
                )
        if retried:
            _logger.info(
                "[leviathan] watchdog auto-retried %d/%d %s",
                retried, total, log_label,
            )
        if marked:
            _logger.info(
                "[leviathan] watchdog marked %d/%d %s as FAILED (retry cap reached)",
                marked, total, log_label,
            )

    def _watchdog_auto_retry(self, log_label):
        """Smart auto-retry on a single stuck job — bumps watchdog_retry_count,
        then routes through the existing retry pathway:
          - prd_prompt present (extraction done) → state=generating, schedule PRD gen
          - no prd_prompt (extraction itself stuck) → reset for full pipeline

        Tasker stays assigned (or stays unassigned if the job had no user).
        via_batch is preserved so the eventual completion still auto-releases
        the row to the pool if the job came from a batch.
        """
        self.ensure_one()
        self.watchdog_retry_count = self.watchdog_retry_count + 1
        _logger.warning(
            "[leviathan][job=%s] watchdog: auto-retry %d/%s — stuck %s "
            "(started_processing_at=%s, last_heartbeat=%s)",
            self.name, self.watchdog_retry_count, "max",
            log_label, self.started_processing_at, self.last_heartbeat,
        )

        # Staged manual jobs must stay in manual mode through recovery: re-run
        # only the stuck stage and re-park, instead of the fused auto pipeline
        # (which would run straight through to done, defeating the per-stage
        # gating the tasker chose). 'extracting' is not handled here — it has no
        # prd_prompt yet, so it falls through to the re-extraction path below,
        # and the webhook re-parks it at 'extracted' because auto_continue=False.
        if not self.auto_continue and self.state in ("generating", "qc_running"):
            db_name = self.env.cr.dbname
            record_id = self.id
            if self.state == "generating":
                self.write({
                    "score": False,
                    "grade": False,
                    "qc_verdict": False,
                    "prd_text": False,
                    "prd_text_html": False,
                    "qc_report": False,
                    "score_report_json": False,
                    "prd_url": False,
                    "llm_attempts": 0,
                    "llm_trace_json": False,
                    "error_message": False,
                    "cancel_requested": False,
                    "completed_at": False,
                    "last_heartbeat": fields.Datetime.now(),
                    "started_processing_at": False,
                })
                self.env.cr.postcommit.add(
                    lambda: _submit_bg(
                        f"prd-gen-stage[job={record_id}](wd-auto-retry)",
                        self._run_generate_only_bg, db_name, record_id,
                    )
                )
            else:
                self.write({
                    "error_message": False,
                    "cancel_requested": False,
                    "last_heartbeat": fields.Datetime.now(),
                })
                self.env.cr.postcommit.add(
                    lambda: _submit_bg(
                        f"qc-stage[job={record_id}](wd-auto-retry)",
                        self._run_qc_stage_bg, db_name, record_id,
                    )
                )
            return

        if self.prd_prompt:
            # Extraction already done — fastest path is straight to PRD gen.
            self.write({
                "state": "generating",
                "score": False,
                "grade": False,
                "qc_verdict": False,
                "prd_text": False,
                "prd_text_html": False,
                "qc_report": False,
                "score_report_json": False,
                "prd_url": False,
                "llm_attempts": 0,
                "llm_trace_json": False,
                "error_message": False,
                "cancel_requested": False,
                "started_at": fields.Datetime.now(),
                "completed_at": False,
                "duration_seconds": False,
                "last_heartbeat": fields.Datetime.now(),
                "started_processing_at": False,
            })
            db_name = self.env.cr.dbname
            record_id = self.id
            self.env.cr.postcommit.add(
                lambda: _submit_bg(
                    f"prd-gen[job={record_id}](wd-auto-retry)",
                    self._run_prd_generation_bg, db_name, record_id,
                )
            )
            return

        # Extraction stuck (no prd_prompt). Re-fire the full pipeline.
        # We use action_retry_failed_batch's "no prd_prompt" reset shape but
        # in-place — same effect: re-extraction starts cleanly.
        self.write({
            "state": "extracting",
            "score": False,
            "grade": False,
            "qc_verdict": False,
            "prd_text": False,
            "prd_text_html": False,
            "prd_prompt": False,
            "qc_report": False,
            "score_report_json": False,
            "prd_url": False,
            "artifacts_url": False,
            "deliverables_url": False,
            "lambda_callback_json": False,
            "llm_trace_json": False,
            "extraction_warnings": False,
            "llm_attempts": 0,
            "screenshot_keys": False,
            "asset_keys": False,
            "site_discovery_json": False,
            "tech_stack": False,
            "page_count": False,
            "started_at": fields.Datetime.now(),
            "completed_at": False,
            "duration_seconds": False,
            "last_heartbeat": fields.Datetime.now(),
            "started_processing_at": False,
            "error_message": False,
            "cancel_requested": False,
        })
        # Re-trigger extraction via the existing pathway. _trigger_extraction
        # reads config + invokes Lambda; webhook returns drive the rest.
        try:
            self._trigger_extraction()
        except Exception:
            _logger.exception(
                "[leviathan][job=%s] watchdog auto-retry: re-trigger failed",
                self.name,
            )


def _markdown_to_html(md_text: str) -> str:
    """Convert markdown PRD to basic HTML for the rich-text editor."""
    import re
    if not md_text:
        return ""
    lines = md_text.split("\n")
    html_lines = []
    in_list = False
    in_table = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h4>{stripped[5:]}</h4>")
        elif stripped.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = stripped[2:]
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)
            content = re.sub(r'`(.*?)`', r'<code>\1</code>', content)
            html_lines.append(f"<li>{content}</li>")
        elif stripped.startswith("|") and stripped.endswith("|"):
            if not in_table:
                html_lines.append("<table class='table table-sm'>")
                in_table = True
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if all(set(c) <= set("- :") for c in cells):
                continue
            row = "".join(f"<td>{c}</td>" for c in cells)
            html_lines.append(f"<tr>{row}</tr>")
        elif not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_table:
                html_lines.append("</table>")
                in_table = False
            html_lines.append("<br/>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            content = stripped
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)
            content = re.sub(r'\*(.*?)\*', r'<em>\1</em>', content)
            content = re.sub(r'`(.*?)`', r'<code>\1</code>', content)
            html_lines.append(f"<p>{content}</p>")

    if in_list:
        html_lines.append("</ul>")
    if in_table:
        html_lines.append("</table>")

    return "\n".join(html_lines)
