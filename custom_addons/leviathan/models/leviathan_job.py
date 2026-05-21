import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

_PRD_POOL_SIZE = int(os.environ.get("LEVIATHAN_PRD_POOL_SIZE", "50"))
_BATCH_FANOUT_POOL_SIZE = int(os.environ.get("LEVIATHAN_BATCH_FANOUT_SIZE", "250"))

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


def _get_pool() -> ThreadPoolExecutor:
    """Return the ThreadPoolExecutor for the current process, creating it
    lazily on first call. Safe across Odoo's prefork-style worker model."""
    pid = os.getpid()
    pool = _POOL_REGISTRY.get(pid)
    if pool is not None:
        return pool
    with _POOL_REGISTRY_LOCK:
        pool = _POOL_REGISTRY.get(pid)
        if pool is None:
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
    - If the pool is gone (process recycling), runs inline as a last resort so
      the job is never silently dropped. The watchdog cron is the final backstop.
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
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except Exception:
            _logger.exception("[leviathan] background task '%s' crashed", label)
        finally:
            _logger.info(
                "[leviathan] bg task '%s' FINISHED (ran %.1fs, queue-wait %.1fs)",
                label, time.monotonic() - t0, wait_s,
            )

    try:
        return pool.submit(_guarded)
    except RuntimeError:
        _logger.error(
            "[leviathan] thread pool unavailable for '%s' — running inline", label,
        )
        _guarded()
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
                "SET last_heartbeat = (now() AT TIME ZONE 'UTC') "
                "WHERE id = ANY(%s) "
                "  AND state IN ('extracting', 'generating', 'scoring')",
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
            ("generating", "Generating PRD"),
            ("scoring", "Scoring"),
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

    @api.depends("state", "started_at", "last_heartbeat", "completed_at", "duration_seconds")
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

            rec.stage_progress_html = (
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

    _ACTIVE_STATES = ("draft", "extracting", "generating", "scoring", "done")

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

        self.env.cr.execute(
            f"""
            SELECT id FROM leviathan_job
             WHERE state IN ('not_assigned', 'failed')
               AND user_id IS NULL
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
            if task.state in ("extracting", "generating", "scoring"):
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
            "started_at": fields.Datetime.now(),
            "completed_at": False,
            "duration_seconds": False,
            "last_heartbeat": fields.Datetime.now(),
        })
        self._trigger_extraction()

    def action_cancel(self):
        """Stop a running task (extracting / generating / scoring) and return it
        to Draft so the tasker can re-run. Signals background threads to stop."""
        self.ensure_one()
        if self.state not in ("extracting", "generating", "scoring"):
            raise UserError("Cancel is only available while a task is running.")
        self.write({
            "state": "draft",
            "cancel_requested": True,
            "error_message": False,
        })
        _logger.info("[leviathan][job=%s] cancelled by %s", self.name, self.env.user.name)
        self._notify_state_change("draft")

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

        ICP = self.env["ir.config_parameter"].sudo()
        config = {
            "function_name": ICP.get_param("leviathan.lambda_function_name"),
            "region": ICP.get_param("leviathan.lambda_region") or "ap-south-1",
            "access_key_id": ICP.get_param("leviathan.extraction_access_key_id") or "",
            "secret_access_key": ICP.get_param("leviathan.extraction_secret_access_key") or "",
            "batch_concurrency": int(
                ICP.get_param("leviathan.batch_concurrency") or _BATCH_FANOUT_POOL_SIZE
            ),
        }

        # Skip re-extraction: a job that already has a prd_prompt is already
        # "extracted" — send it straight to PRD generation. Only jobs WITHOUT
        # extraction data go through the Lambda fan-out.
        to_generate = eligible.filtered(lambda r: r.prd_prompt)
        to_extract = eligible - to_generate

        if to_extract and not config["function_name"]:
            raise UserError(
                "Lambda function name not configured "
                "(Settings -> Leviathan -> Lambda Function)."
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
            to_generate.write(dict(_common, state="generating"))
            gen_ids = to_generate.ids
            _logger.info(
                "[leviathan] batch: %d job(s) already extracted -> PRD generation: %s",
                len(gen_ids), to_generate.mapped("name"),
            )

            def _deferred_generate():
                for rid in gen_ids:
                    _submit_bg(
                        f"prd-gen[job={rid}]",
                        self._run_prd_generation_bg, db_name, rid,
                    )

            self.env.cr.postcommit.add(_deferred_generate)

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
        failed: dict[int, str] = {}
        max_workers = min(config["batch_concurrency"], len(record_ids)) or 1

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
                else:
                    failed[record_id] = result.get("error", "Unknown error")[:500]

        _logger.info(
            "Batch fan-out done: %d invoked OK, %d failed", len(ok_ids), len(failed),
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
        if self.state in ("extracting", "generating", "scoring"):
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
                # Cleared — the bg worker will set this when it actually
                # picks the job up. Until then the watchdog must not see
                # this row as "actually started".
                "started_processing_at": False,
            })
            _logger.info(
                "[leviathan][job=%s] retry: prd_prompt present — skipping extraction, "
                "going straight to PRD generation",
                self.name,
            )
            db_name = self.env.cr.dbname
            record_id = self.id
            self.env.cr.postcommit.add(
                lambda: _submit_bg(
                    f"prd-gen[job={record_id}]",
                    self._run_prd_generation_bg, db_name, record_id,
                )
            )
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
                "started_processing_at": False,
            })
            gen_ids.append(rec.id)

        if gen_ids:
            def _deferred_generate():
                for rid in gen_ids:
                    _submit_bg(
                        f"prd-gen[job={rid}]",
                        self._run_prd_generation_bg, db_name, rid,
                    )

            self.env.cr.postcommit.add(_deferred_generate)

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
                # Cleared — the bg worker resets this when it actually
                # picks up. Otherwise stale value from the previous run
                # confuses the watchdog (looks "running" before the new
                # worker has touched the row).
                "started_processing_at": False,
            })
            db_name = self.env.cr.dbname
            record_id = self.id

            self.env.cr.postcommit.add(
                lambda: _submit_bg(
                    f"prd-gen[job={record_id}]",
                    self._run_prd_generation_bg, db_name, record_id,
                )
            )

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
            # Cleared — the bg worker will set this on pickup. Otherwise
            # the watchdog sees the row as "actually running" from the
            # previous run before the new worker has touched it.
            "started_processing_at": False,
        })

        if self.prd_prompt:
            self.prd_prompt = (
                self.prd_prompt + "\n\n"
                "---\n\n"
                "## PREVIOUS QC FEEDBACK (fix these issues):\n\n"
                + qc_feedback
            )

        db_name = self.env.cr.dbname
        record_id = self.id

        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                f"prd-gen[job={record_id}]",
                self._run_prd_generation_bg, db_name, record_id,
            )
        )

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

    def action_rerun_qc(self):
        """Re-run only QC validation (after manual PRD edits)."""
        self.ensure_one()
        if self.state != "done":
            raise UserError("Can only rerun QC from Done state.")
        if not self.prd_text:
            raise UserError("No PRD text available for QC.")

        self.write({
            "state": "scoring",
            "qc_verdict": False,
            "qc_report": False,
            "error_message": False,
            "last_heartbeat": fields.Datetime.now(),
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

            # Download screenshots for QC vision
            screenshot_blocks = []
            if job_data["screenshot_keys"] and config["s3_bucket"]:
                from ..services.s3_service import download_file_from_s3
                import base64 as b64
                MAX_SCREENSHOTS = 5
                MAX_IMG_BYTES = 3_500_000
                total_bytes = 0
                for key in job_data["screenshot_keys"][:MAX_SCREENSHOTS]:
                    try:
                        img_bytes = download_file_from_s3(
                            key=key, bucket=config["s3_bucket"],
                            access_key_id=config["s3_key_id"],
                            secret_key=config["s3_secret"],
                            region=config["s3_region"],
                        )
                        if len(img_bytes) > MAX_IMG_BYTES:
                            continue
                        ext = key.rsplit(".", 1)[-1].lower()
                        fmt = ext if ext in ("png", "jpeg", "gif", "webp") else "png"
                        # Bedrock rejects images with any dimension > 8000 px.
                        img_bytes = _resize_image_for_bedrock(img_bytes, fmt)
                        total_bytes += len(img_bytes)
                        if total_bytes > 20_000_000:
                            break
                        screenshot_blocks.append({
                            "image": {"format": fmt, "source": {"bytes": b64.b64encode(img_bytes).decode()}}
                        })
                    except Exception:
                        pass

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
        base_url = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("web.base.url", "http://localhost:8069")
        )
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
                _logger.info(
                    "[leviathan][job=%s] extraction Lambda invoke ACCEPTED by "
                    "AWS (request_id=%s) — awaiting callback",
                    record_id, result.get("request_id", ""),
                )

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

    def _run_prd_generation_bg(self, db_name, record_id):
        """Background: generate PRD via Bedrock, score, QC.

        Wrapped in `_HeartbeatTicker` so `last_heartbeat` pulses every 60s
        for the lifetime of the worker — regardless of where the worker
        is in its code. Without this wrapper a single Bedrock call sitting
        in adaptive-retry backoff for 30+ min (real behaviour under
        throttle) would silently miss the watchdog window even though
        the worker is alive and the call is making progress.
        """
        from ..services.bedrock_service import generate_prd
        from ..services.scoring_service import score_prd
        from ..services.s3_service import upload_prd_to_s3

        with _HeartbeatTicker(self, db_name, record_id, interval=60):
            self._run_prd_generation_bg_impl(
                db_name, record_id, generate_prd, score_prd, upload_prd_to_s3,
            )

    def _run_prd_generation_bg_impl(
        self, db_name, record_id, generate_prd, score_prd, upload_prd_to_s3,
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

                # Worker-pickup mark: this is the moment the bg worker
                # actually started touching the job. The watchdog uses
                # `started_processing_at` to distinguish "queued in _POOL,
                # never touched" from "running and stuck". Without this
                # mark a job that sat in the queue for 45+ min would be
                # killed by the watchdog even though no real work was
                # attempted on it yet.
                record.write({
                    "state": "generating",
                    "started_processing_at": fields.Datetime.now(),
                    "last_heartbeat": fields.Datetime.now(),
                })
                cr.commit()

            # === PHASE 2: PRD generation ===
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): downloading screenshots "
                "+ building Bedrock request (category=%s, prd_prompt=%dB)",
                record_id, _elapsed(), job_data["category_name"],
                len(job_data["prd_prompt"] or ""),
            )
            # Download screenshots from S3 for vision (shared by PRD gen + QC)
            # Bedrock limit: 3.75MB per image, 25MB total. Resize to keep fast.
            screenshot_blocks = []
            if job_data["screenshot_keys"] and config["s3_bucket"]:
                from ..services.s3_service import download_file_from_s3
                import base64 as b64
                MAX_SCREENSHOTS = 5
                MAX_IMG_BYTES = 3_500_000  # 3.5MB (under Bedrock 3.75MB limit)
                total_bytes = 0
                for key in job_data["screenshot_keys"][:MAX_SCREENSHOTS]:
                    try:
                        img_bytes = download_file_from_s3(
                            key=key,
                            bucket=config["s3_bucket"],
                            access_key_id=config["s3_key_id"],
                            secret_key=config["s3_secret"],
                            region=config["s3_region"],
                        )
                        if len(img_bytes) > MAX_IMG_BYTES:
                            _logger.info("Skipping oversized screenshot %s (%d bytes)", key, len(img_bytes))
                            continue
                        ext = key.rsplit(".", 1)[-1].lower()
                        fmt = ext if ext in ("png", "jpeg", "gif", "webp") else "png"
                        # Bedrock rejects images with any dimension > 8000 px.
                        img_bytes = _resize_image_for_bedrock(img_bytes, fmt)
                        total_bytes += len(img_bytes)
                        if total_bytes > 20_000_000:  # 20MB safety cap
                            _logger.info("Screenshot total size cap reached, stopping")
                            break
                        screenshot_blocks.append({
                            "image": {
                                "format": fmt,
                                "source": {"bytes": b64.b64encode(img_bytes).decode()},
                            }
                        })
                    except Exception as img_exc:
                        _logger.warning("Failed to download screenshot %s: %s", key, img_exc)
                _logger.info(
                    "Attached %d/%d screenshots for LLM (%.1f MB)",
                    len(screenshot_blocks), len(job_data["screenshot_keys"]),
                    total_bytes / 1_000_000,
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
                self._write_with_cursor(db_name, record_id, {
                    "state": "draft", "error_message": "Cancelled during generation",
                    "completed_at": fields.Datetime.now(),
                })
                return

            self._write_with_cursor(db_name, record_id, {
                "last_heartbeat": fields.Datetime.now(),
            })

            # Single PRD-generation call — no score-driven retry loop.
            # Transient Bedrock errors are retried inside generate_prd
            # (LEVIATHAN_BEDROCK_INNER_RETRIES); a hard failure here marks
            # the job failed so the tasker can re-run it from the UI.
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): calling Bedrock for PRD "
                "generation (%d screenshot(s) attached)",
                record_id, _elapsed(), len(screenshot_blocks),
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
            _logger.info(
                "[leviathan][job=%s] PHASE 2 (+%.1fs): Bedrock PRD returned in "
                "%.1fs — %d chars / ~%d words",
                record_id, _elapsed(), time.monotonic() - _bedrock_t0,
                len(best_prd_text or ""), len((best_prd_text or "").split()),
            )

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

            self._write_with_cursor(db_name, record_id, {
                "llm_attempts": 1,
            })

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
            # Pulse the heartbeat on entry. QC can be a multi-minute Bedrock
            # call; without this pulse the gap from the last PRD-gen attempt
            # to PHASE 4's final write was fully unmonitored — long QC calls
            # could trip the watchdog while doing real work.
            self._write_with_cursor(db_name, record_id, {
                "state": "scoring",
                "last_heartbeat": fields.Datetime.now(),
            })

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

            # === PHASE 4: Write final results ===
            _logger.info(
                "[leviathan][job=%s] PHASE 4 (+%.1fs): writing final results",
                record_id, _elapsed(),
            )
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)

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
                fail_vals = {
                    "state": "failed",
                    "error_message": str(exc)[:500],
                    "completed_at": fields.Datetime.now(),
                }
                # Persist whatever LLM trace we accumulated before the failure.
                _trace = locals().get("llm_trace")
                if _trace:
                    fail_vals["llm_trace_json"] = _trace
                self._write_with_cursor(db_name, record_id, fail_vals)
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
                if "state" in vals:
                    try:
                        env["bus.bus"]._sendone(
                            "leviathan_job_updates",
                            "leviathan/job_state",
                            {"id": record_id, "state": vals["state"]},
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
            for st in ("extracting", "generating", "scoring"):
                counts[st] = self.search_count([("state", "=", st)])
            _logger.info(
                "[leviathan] watchdog tick: extracting=%d generating=%d "
                "scoring=%d (thresholds: extract>%dmin generate>%dmin)",
                counts["extracting"], counts["generating"], counts["scoring"],
                extracting_threshold, generating_threshold,
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

            # `started_processing_at != False` excludes jobs sitting in the
            # _POOL queue waiting for a worker — they look stuck (no
            # heartbeat update) but no work has been attempted on them.
            # Without this guard, a 150-job batch on a 50-worker pool
            # false-fails the 20-30 tail jobs that are simply queued.
            stale_generating = self.search([
                ("state", "in", ("generating", "scoring")),
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

            # --- ORPHAN DIAGNOSTIC (no recovery, logging only) ---
            # Jobs in generating/scoring with started_processing_at unset have
            # NEVER been picked up by a PRD worker — they are sitting in an
            # in-process ThreadPoolExecutor queue. The recovery query above
            # deliberately SKIPS them (started_processing_at != False) to
            # avoid false-failing a legitimate backlog. But that same guard
            # means a job whose pool process was recycled/killed while it sat
            # queued is NEVER recovered — it is stuck forever with no log.
            # Surface them here: anything in this state past the generating
            # threshold is almost certainly orphaned (a healthy pool drains
            # its queue in minutes) and needs a manual Retry / reset.
            orphaned = self.search([
                ("state", "in", ("generating", "scoring")),
                ("started_processing_at", "=", False),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=generating_threshold),
                ),
            ])
            if orphaned:
                _logger.error(
                    "[leviathan] watchdog: %d job(s) ORPHANED in generating/"
                    "scoring with started_processing_at unset for >%dmin — "
                    "their PRD worker was never reached (pool process likely "
                    "recycled). The watchdog CANNOT auto-recover these; they "
                    "need a manual Retry/reset. Job names: %s",
                    len(orphaned), generating_threshold,
                    orphaned.mapped("name"),
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
