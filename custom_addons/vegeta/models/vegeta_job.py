import json
import logging
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules.registry import Registry
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

_PRD_POOL_SIZE = int(os.environ.get("VEGETA_PRD_POOL_SIZE", "50"))
_POOL = ThreadPoolExecutor(
    max_workers=_PRD_POOL_SIZE, thread_name_prefix="vegeta-prd"
)

_BATCH_FANOUT_POOL_SIZE = int(os.environ.get("VEGETA_BATCH_FANOUT_SIZE", "250"))


def _submit_bg(label, fn, *args, **kwargs):
    """Submit a background job to the shared pool — with uptime guarantees.

    - Logs a warning when the pool queue is backing up (saturation) so a slow
      Bedrock/S3 never silently swallows work without a trace.
    - Wraps the callable so any escaped exception is logged instead of being
      lost in an un-awaited Future.
    - If the pool is gone (process recycling), runs inline as a last resort so
      the job is never silently dropped. The watchdog cron is the final backstop.
    """
    qsize = -1
    try:
        qsize = _POOL._work_queue.qsize()
        if qsize > _PRD_POOL_SIZE:
            _logger.warning(
                "[vegeta] PRD pool saturated: %d queued / %d workers — jobs "
                "will run but are delayed; raise VEGETA_PRD_POOL_SIZE.",
                qsize, _PRD_POOL_SIZE,
            )
    except Exception:
        pass

    # Submit-time marker: diff this timestamp against the "STARTED" line
    # _guarded() logs to measure pool queue-wait. A large gap means the job
    # sat waiting for a free worker (pool too small / slow Bedrock backlog).
    submitted_at = time.monotonic()
    _logger.info(
        "[vegeta] _submit_bg: queued '%s' on pool[pid=%d] "
        "(queue_depth=%d, workers=%d)",
        label, os.getpid(), qsize, _PRD_POOL_SIZE,
    )

    def _guarded():
        # bg task running now. Its absence after a _submit_bg line for the
        # same label means the pool never reached the task.
        wait_s = time.monotonic() - submitted_at
        _logger.info(
            "[vegeta] bg task '%s' STARTED (pool queue-wait=%.1fs)",
            label, wait_s,
        )
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except Exception:
            _logger.exception("[vegeta] background task '%s' crashed", label)
        finally:
            _logger.info(
                "[vegeta] bg task '%s' FINISHED (ran %.1fs, queue-wait %.1fs)",
                label, time.monotonic() - t0, wait_s,
            )

    try:
        return _POOL.submit(_guarded)
    except RuntimeError:
        _logger.error(
            "[vegeta] thread pool unavailable for '%s' — running inline", label,
        )
        _guarded()
        return None


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
                "[vegeta] resized image %dx%d -> %dx%d (%d -> %d bytes) for Bedrock",
                w, h, im.size[0], im.size[1], len(img_bytes), len(new_bytes),
            )
            return new_bytes
    except Exception as exc:
        _logger.warning(
            "[vegeta] image resize failed (%s) — sending original; Bedrock "
            "may reject if >8000px", exc,
        )
        return img_bytes


# ---------------------------------------------------------------------------
# K8s-Job-per-job PRD dispatch (ported from the aurora addon).
#
# PRD generation runs in a dedicated Kubernetes Job per vegeta.job so the work
# survives Odoo worker/pod recycling and concurrent jobs scale across pods.
# When Kubernetes is unavailable the dispatch cron falls back to the in-process
# thread pool above, keeping local single-process dev working with no cluster.
# ---------------------------------------------------------------------------
try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.client.rest import ApiException as K8sApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

# Cluster config is loaded once and refreshed lazily — tokens behind
# load_incluster_config expire, so the cache is bounded.
_k8s_config_lock = threading.Lock()
_k8s_config_loaded = False
_k8s_config_loaded_at = 0.0
_K8S_CONFIG_MAX_AGE = 3000  # ~50 min

# DevOps-managed infrastructure constants — not UI-configurable.
VEGETA_NAMESPACE_DEFAULT = "vegeta"
VEGETA_SERVICE_ACCOUNT = "vegeta-worker"
VEGETA_WORKER_IMAGE_DEFAULT = (
    "426628337772.dkr.ecr.ap-south-1.amazonaws.com/vegeta-prd-worker:latest"
)
NODE_SELECTOR = (
    {} if os.environ.get("VEGETA_LOCAL_MODE")
    else {"ethara.ai/node-pool": "general-purpose"}
)
IMAGE_PULL_POLICY = (
    "IfNotPresent" if os.environ.get("VEGETA_LOCAL_MODE") else "Always"
)
# Kueue is opt-in: the queue label is read per-Job from the
# `vegeta.kueue_queue` config parameter in `_create_prd_job`. A label that
# points at a non-existent LocalQueue would suspend the Job forever, so the
# default (param unset) attaches no Kueue label at all.
CPU_REQUEST = "1"
MEMORY_REQUEST = "2Gi"
MEMORY_LIMIT = "4Gi"
PRD_DEADLINE_SECONDS = 3600  # 1 h — generous for ~13 min typical PRD work
WORKER_SCRIPT = "/opt/odoo/custom_addons/vegeta/worker/run_prd.py"
ODOO_CONF_PATH = "/etc/odoo/odoo.conf"

# Advisory-lock IDs — distinct from aurora's 73927461-63 range so the two
# addons can share a database without their crons blocking each other.
_PRD_DISPATCH_LOCK_ID = 73928001
_PRD_RECONCILE_LOCK_ID = 73928002

# Sentinel job_name for an in-process (non-K8s) dispatch: it satisfies the
# dispatch cron's "already dispatched" guard while the `vegeta-prd-` prefix
# check keeps the reconcile cron from treating it as a missing K8s Job.
_INPROCESS_JOB_NAME = "inprocess"


def _k8s_get_env(key, default=""):
    return os.environ.get(key, default).strip()


def _load_k8s_config():
    global _k8s_config_loaded, _k8s_config_loaded_at
    if _k8s_config_loaded and (time.time() - _k8s_config_loaded_at) < _K8S_CONFIG_MAX_AGE:
        return
    with _k8s_config_lock:
        if _k8s_config_loaded and (time.time() - _k8s_config_loaded_at) < _K8S_CONFIG_MAX_AGE:
            return
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        _k8s_config_loaded = True
        _k8s_config_loaded_at = time.time()


class VegetaJob(models.Model):
    _name = "vegeta.job"
    _description = "Vegeta Pipeline Task"
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
    category_id = fields.Many2one("vegeta.category", string="Website Category")
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
    signals_json = fields.Json(
        string="Extracted Signals",
        help="API docs, business model, auth/SSO, network endpoints, forms, and vegeta category classification captured during extraction.",
    )
    signals_html = fields.Html(
        string="Extracted Signals (HTML)",
        compute="_compute_signals_html",
        sanitize=False,
    )
    lambda_callback_html = fields.Html(
        string="Lambda Callback (formatted)",
        compute="_compute_pipeline_json_html",
        sanitize=False,
    )
    llm_trace_html = fields.Html(
        string="LLM Trace (formatted)",
        compute="_compute_pipeline_json_html",
        sanitize=False,
    )
    site_discovery_html = fields.Html(
        string="Site Discovery (formatted)",
        compute="_compute_pipeline_json_html",
        sanitize=False,
    )
    prd_prompt_html = fields.Html(
        string="Extraction Data (formatted)",
        compute="_compute_prd_prompt_html",
        sanitize=False,
    )
    started_at = fields.Datetime(string="Started At")
    completed_at = fields.Datetime(string="Completed At")
    last_heartbeat = fields.Datetime(string="Last Heartbeat")
    # Counts consecutive heartbeat-write failures so the reconcile cron can
    # distinguish "Bedrock call is slow, worker is healthy" from "worker is
    # actually dead". Reset to 0 on every successful heartbeat write. The
    # reconcile cron only re-claims a job when this count exceeds a threshold
    # OR last_heartbeat is grossly stale (>15min) — without this gate a
    # transiently saturated Postgres pool causes false recovery and the same
    # job ends up double-processed by two workers.
    heartbeat_failure_count = fields.Integer(
        string="Heartbeat Failures",
        default=0,
        copy=False,
    )
    # H4 fix: bounds crash-loop cost. Reconcile increments this every time it
    # clears job_name on a stuck job. A job that crashes deterministically
    # (bad screenshot, malformed prompt) otherwise loops claim→crash→
    # recover→claim forever, burning Bedrock tokens on every attempt. After
    # MAX_RECOVERIES the reconcile cron marks it failed instead of re-queuing.
    recovery_count = fields.Integer(
        string="Recovery Attempts",
        default=0,
        copy=False,
    )
    # Set when a background worker actually picks the job up off the pool queue
    # (entry to `_run_prd_generation_bg`). Distinct from `started_at` (set at
    # batch dispatch). The watchdog uses this to tell "queued waiting for a
    # worker" from "actually running and stuck" — without it, jobs sitting in
    # _POOL._work_queue for >45 min get false-failed even though no work has
    # been attempted on them yet.
    started_processing_at = fields.Datetime(string="Worker Picked Up At")

    # Name of the Kubernetes Job running this task's PRD generation. Empty
    # until the dispatch cron picks the job up; set to the K8s Job name for a
    # cluster run, or to _INPROCESS_JOB_NAME for the local in-process fallback.
    # Cleared on every terminal state so a re-run can be re-dispatched cleanly.
    job_name = fields.Char(
        string="K8s Job Name",
        readonly=True,
        copy=False,
        index=True,
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
        prompt = ICP.get_param("vegeta.prd_system_prompt", "")
        if prompt and prompt.strip():
            return prompt.strip()
        path = Path(__file__).parent.parent / "prompts" / "prd_agent_spec.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @api.model
    def _get_qc_system_prompt(self):
        """Read QC system prompt from Settings; fall back to built-in default."""
        ICP = self.env["ir.config_parameter"].sudo()
        prompt = ICP.get_param("vegeta.qc_system_prompt", "")
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
                "vegeta.group_vegeta_admin"
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
        from markupsafe import escape
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
            g = escape(grade)

            html = (
                f'<div style="margin-bottom:16px;">'
                f'<span style="font-size:32px;font-weight:700;color:{color};">{total}</span>'
                f'<span style="font-size:18px;color:{color};margin-left:4px;"></span>'
                f'<span style="display:inline-block;margin-left:12px;padding:4px 12px;'
                f'border-radius:4px;background:{color};color:#fff;font-weight:600;'
                f'font-size:16px;">{g}</span>'
            )
            if details.get("grade_cap"):
                html += (
                    f'<span style="margin-left:12px;color:#6c757d;font-size:13px;">'
                    f'Cap: {escape(details["grade_cap"])}</span>'
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
                    f'<td style="padding:5px 8px;">{escape(key)}: {escape(name)}</td>'
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
                    html += f'<span style="display:inline-block;margin:2px 4px;padding:2px 8px;background:#dc3545;color:#fff;border-radius:3px;font-size:12px;">{escape(r)}</span>'
                html += '</div>'

            if warnings:
                html += '<div style="margin-top:6px;">'
                for w in warnings:
                    html += f'<span style="display:inline-block;margin:2px 4px;padding:2px 8px;background:#ffc107;color:#000;border-radius:3px;font-size:12px;">{escape(w)}</span>'
                html += '</div>'

            wc = details.get("word_count", 0)
            t1 = details.get("tier1_violations", [])
            html += f'<div style="margin-top:10px;color:#6c757d;font-size:12px;">'
            html += f'Words: {wc}'
            if t1:
                html += f' &middot; Banned phrases: {", ".join(escape(v) for v in t1)}'
            html += '</div>'

            rec.score_report_html = html

    @api.depends("signals_json")
    def _compute_signals_html(self):
        from markupsafe import escape

        def _fmt_value(value, depth=0):
            if value is None or value == "" or value == [] or value == {}:
                return '<span style="color:#adb5bd;font-style:italic;">empty</span>'
            if isinstance(value, bool):
                color = "#28a745" if value else "#dc3545"
                return f'<span style="color:{color};font-weight:600;">{str(value).lower()}</span>'
            if isinstance(value, (int, float)):
                return f'<span style="font-family:monospace;color:#0066cc;">{value}</span>'
            if isinstance(value, str):
                if value.startswith("http://") or value.startswith("https://"):
                    safe = escape(value)
                    return f'<a href="{safe}" target="_blank" style="color:#0066cc;text-decoration:none;word-break:break-all;overflow-wrap:anywhere;">{safe}</a>'
                return f'<span style="font-family:monospace;word-break:break-word;overflow-wrap:anywhere;">{escape(value)}</span>'
            if isinstance(value, list):
                if not value:
                    return '<span style="color:#adb5bd;font-style:italic;">empty list</span>'
                if all(isinstance(item, (str, int, float, bool)) for item in value):
                    chips = ""
                    for item in value:
                        safe = escape(str(item))
                        chips += (
                            f'<span style="display:inline-block;margin:2px 4px 2px 0;'
                            f'padding:2px 8px;background:#e7f3ff;color:#0066cc;'
                            f'border-radius:3px;font-size:12px;font-family:monospace;'
                            f'word-break:break-all;overflow-wrap:anywhere;'
                            f'max-width:100%;">{safe}</span>'
                        )
                    return f'<div style="word-break:break-word;">{chips}</div>'
                parts = []
                for idx, item in enumerate(value):
                    parts.append(
                        f'<div style="margin:6px 0;padding:8px;background:#fafbfc;'
                        f'border-left:3px solid #dee2e6;border-radius:3px;">'
                        f'<div style="font-size:11px;color:#6c757d;margin-bottom:4px;">'
                        f'[{idx}]</div>{_fmt_value(item, depth + 1)}</div>'
                    )
                return "".join(parts)
            if isinstance(value, dict):
                if not value:
                    return '<span style="color:#adb5bd;font-style:italic;">empty</span>'
                rows = ""
                for k in sorted(value.keys()):
                    v = value[k]
                    safe_k = escape(str(k))
                    rows += (
                        f'<tr style="border-bottom:1px solid #f1f3f5;">'
                        f'<td style="padding:6px 12px 6px 0;vertical-align:top;'
                        f'font-weight:600;color:#495057;white-space:nowrap;">{safe_k}</td>'
                        f'<td style="padding:6px 0;vertical-align:top;">'
                        f'{_fmt_value(v, depth + 1)}</td></tr>'
                    )
                return (
                    '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                    f'{rows}</table>'
                )
            return f'<span style="font-family:monospace;">{escape(str(value))}</span>'

        section_meta = {
            "vegeta_category": ("Vegeta Category", "#6f42c1", "Classifier verdict and confidence"),
            "api_doc": ("API Documentation", "#0066cc", "OpenAPI/GraphQL/sitemaps/well-known"),
            "business": ("Business Signals", "#28a745", "Pricing tiers and billing model"),
            "network": ("Network", "#fd7e14", "API endpoints, third-party services, CDN/hosting"),
            "auth": ("Authentication", "#dc3545", "Login forms, OAuth providers, cookies"),
            "forms": ("Forms", "#17a2b8", "Signup/login fields, SSO, MFA"),
            "scrape_coverage": ("Scrape Coverage", "#6c757d", "Depth of capture during extraction"),
        }

        for rec in self:
            signals = rec.signals_json
            if not signals:
                rec.signals_html = ""
                continue
            html = ""
            for key, (title, color, hint) in section_meta.items():
                if key not in signals:
                    continue
                value = signals[key]
                body = _fmt_value(value) if not isinstance(value, str) else (
                    f'<span style="display:inline-block;padding:4px 10px;background:{color};'
                    f'color:#fff;border-radius:3px;font-weight:600;">{escape(value)}</span>'
                )
                html += (
                    f'<div style="margin-bottom:16px;border:1px solid #dee2e6;'
                    f'border-radius:6px;overflow:hidden;">'
                    f'<div style="padding:8px 12px;background:{color};color:#fff;">'
                    f'<span style="font-weight:700;font-size:14px;">{title}</span>'
                    f'<span style="margin-left:10px;font-size:11px;opacity:0.85;">{hint}</span>'
                    f'</div>'
                    f'<div style="padding:12px;background:#fff;">{body}</div>'
                    f'</div>'
                )
            for key in sorted(signals.keys()):
                if key in section_meta:
                    continue
                value = signals[key]
                safe_k = escape(str(key))
                html += (
                    f'<div style="margin-bottom:16px;border:1px solid #dee2e6;'
                    f'border-radius:6px;overflow:hidden;">'
                    f'<div style="padding:8px 12px;background:#6c757d;color:#fff;'
                    f'font-weight:700;font-size:14px;">{safe_k}</div>'
                    f'<div style="padding:12px;background:#fff;">{_fmt_value(value)}</div>'
                    f'</div>'
                )
            rec.signals_html = html

    @api.depends("lambda_callback_json", "llm_trace_json", "site_discovery_json")
    def _compute_pipeline_json_html(self):
        from markupsafe import escape

        def _render(value):
            if not value:
                return ""
            try:
                pretty = json.dumps(value, indent=2, default=str, sort_keys=True)
            except Exception:
                pretty = str(value)
            return (
                '<pre style="white-space:pre-wrap;word-break:break-word;'
                'overflow-wrap:anywhere;max-height:520px;overflow-y:auto;'
                'margin:0;padding:12px;background:#f8f9fa;'
                'border:1px solid #dee2e6;border-radius:4px;'
                f'font-family:monospace;font-size:12px;line-height:1.5;">{escape(pretty)}</pre>'
            )

        for rec in self:
            rec.lambda_callback_html = _render(rec.lambda_callback_json)
            rec.llm_trace_html = _render(rec.llm_trace_json)
            rec.site_discovery_html = _render(rec.site_discovery_json)

    @api.depends("prd_prompt")
    def _compute_prd_prompt_html(self):
        from markupsafe import escape

        for rec in self:
            text = rec.prd_prompt or ""
            if not text:
                rec.prd_prompt_html = ""
                continue
            rec.prd_prompt_html = (
                '<pre style="white-space:pre-wrap;word-break:break-word;'
                'overflow-wrap:anywhere;max-height:600px;overflow-y:auto;'
                'margin:0;padding:12px;background:#f8f9fa;'
                'border:1px solid #dee2e6;border-radius:4px;'
                f'font-family:monospace;font-size:12px;line-height:1.5;">{escape(text)}</pre>'
            )

    @api.depends("screenshot_keys", "asset_keys")
    def _compute_asset_previews(self):
        """Build HTML preview galleries for screenshots and assets."""
        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("vegeta.s3_bucket", "")
        region = ICP.get_param("vegeta.s3_region", "us-east-1")
        cdn_url = ICP.get_param("vegeta.s3_cdn_url", "")

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
                    self.env["ir.sequence"].next_by_code("vegeta.job") or "New"
                )
            # If user_id is set at creation, auto-promote to draft
            if vals.get("user_id") and vals.get("state", "not_assigned") == "not_assigned":
                vals["state"] = "draft"
            # If no user, must be not_assigned
            if not vals.get("user_id") and vals.get("state") in ("draft", "done"):
                vals["state"] = "not_assigned"
        records = super().create(vals_list)
        for rec in records:
            # Birth of a job record — the first line of its timeline. Grep
            # its name from here forward to reconstruct the whole run.
            _logger.info(
                "[vegeta][job=%s] created: state=%s url=%s user=%s",
                rec.name, rec.state, rec.url or "-",
                rec.user_id.login if rec.user_id else "-",
            )
        return records

    def write(self, vals):
        # State-transition trace: capture each record's pre-write state so a
        # job's full state history is reconstructable from grep alone. The
        # background path is logged separately in _write_with_cursor.
        if "state" in vals:
            for rec in self:
                if rec.state != vals["state"]:
                    _logger.info(
                        "[vegeta][job=%s] state %s -> %s (ORM write)",
                        rec.name, rec.state, vals["state"],
                    )
        res = super().write(vals)
        # Auto-promote when admin assigns a user to a not_assigned task.
        # The target state preserves whatever progress the task already has
        # (see _smart_state_on_assign): released done tasks come back as done,
        # released failed-with-data tasks come back as failed (Retry visible),
        # everything else lands in draft. Plain rec.write() is used (not
        # super(VegetaJob, ...).write) so mail.thread chatter records the
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
    # States past which a job is finished. A late background write must
    # never resurrect a job that has already reached one of these.
    _TERMINAL_STATES = ("done", "submitted", "failed", "discarded", "cancelled")

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
        max_active = int(ICP.get_param("vegeta.max_jobs_per_user", "5"))

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
            SELECT id FROM vegeta_job
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
            "[vegeta] Start Task: user=%s claimed job=%s (state=%s)",
            user.login, task.name, new_state,
        )

        return {
            "type": "ir.actions.act_window",
            "res_model": "vegeta.job",
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
        _logger.info(
            "[vegeta][job=%s] released to the unassigned pool by %s",
            self.name, self.env.user.login,
        )
        self.write({
            "user_id": False,
            "state": "not_assigned",
            "error_message": False,
            "cancel_requested": False,
            "job_name": False,
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
        _logger.info(
            "[vegeta] action_reset_selected by %s: resetting %d task(s) %s",
            self.env.user.login, len(eligible), eligible.mapped("name"),
        )

        for task in eligible:
            vals = {
                "user_id": False,
                "state": "not_assigned",
                "cancel_requested": False,
                "via_batch": False,
                "error_message": False,
                "job_name": False,
            }
            # Mark pipeline interruption for in-progress tasks
            if task.state in ("extracting", "generating", "scoring"):
                vals["cancel_requested"] = True
            # Terminate any in-flight K8s PRD Job so its pod is not orphaned.
            if task.state in ("generating", "scoring"):
                task._terminate_prd_k8s_job()
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
        _logger.info(
            "[vegeta][job=%s] action_run by %s — state=%s",
            self.name, self.env.user.login, self.state,
        )
        if self.state not in ("draft", "not_assigned"):
            raise UserError("Can only run tasks in Draft or Not Assigned state.")
        if not self.url:
            raise UserError("Please enter a website URL before running.")

        # Auto-assign if not_assigned or no user
        if self.state == "not_assigned" or not self.user_id:
            self.write({"user_id": self.env.uid, "state": "draft"})

        # If extraction data exists, ask user what to do
        if self._has_extraction_data and not self.env.context.get("force_extract"):
            wizard = self.env["vegeta.rerun.wizard"].create({"job_id": self.id})
            _logger.info(
                "[vegeta][job=%s] action_run: extraction data exists — "
                "opening rerun wizard instead of dispatching", self.name,
            )
            return {
                "type": "ir.actions.act_window",
                "name": "Extraction Data Exists",
                "res_model": "vegeta.rerun.wizard",
                "res_id": wizard.id,
                "view_mode": "form",
                "views": [(False, "form")],
                "target": "new",
            }

        # Per-user concurrent job limit (only count running tasks, not draft/done)
        max_jobs = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("vegeta.max_jobs_per_user", "5")
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

        # Lock row to prevent double-run (graceful on lock conflict)
        sp_name = f"vegeta_run_{self.id}"
        self.env.cr.execute(f"SAVEPOINT {sp_name}")
        try:
            self.env.cr.execute(
                "SELECT id FROM vegeta_job WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Exception:
            self.env.cr.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
            # Lock conflict: another session holds this row. Logged so a
            # "task being modified" UserError can be traced to a real clash.
            _logger.warning(
                "[vegeta][job=%s] action_run: row locked by another session "
                "— aborting", self.name, exc_info=True,
            )
            raise UserError("Task is being modified by another session. Try again.")

        self.env.cr.execute(
            "SELECT state FROM vegeta_job WHERE id = %s", [self.id]
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
        # Stage boundary: job has left draft; the extraction Lambda is
        # dispatched on postcommit. Absence of this line means action_run
        # raised before the state write committed.
        _logger.info(
            "[vegeta][job=%s] action_run: state -> extracting, dispatching "
            "extraction Lambda on postcommit", self.name,
        )
        self._trigger_extraction()

    def action_cancel(self):
        """Stop a running task (extracting / generating / scoring) and return it
        to Draft so the tasker can re-run. Signals background threads to stop."""
        self.ensure_one()
        if self.state not in ("extracting", "generating", "scoring"):
            raise UserError("Cancel is only available while a task is running.")
        # Terminate any in-flight K8s PRD Job so its pod is not orphaned.
        if self.state in ("generating", "scoring"):
            self._terminate_prd_k8s_job()
        self.write({
            "state": "draft",
            "cancel_requested": True,
            "error_message": False,
            "job_name": False,
        })
        _logger.info("[vegeta][job=%s] cancelled by %s", self.name, self.env.user.name)
        self._notify_state_change("draft")

    def action_run_batch_concurrent(self):
        """Server action: fire all selected jobs in parallel via async Lambda invoke.

        Replaces the legacy RabbitMQ + consumer.py fan-out. Uses a single
        ThreadPoolExecutor sized to ``vegeta.batch_concurrency`` (default 250)
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
            _logger.info(
                "[vegeta] action_run_batch_concurrent: no eligible tasks "
                "(need not_assigned + URL)",
            )
            raise UserError(
                "No eligible tasks. Tasks must be 'Not Assigned' with a URL."
            )
        skipped = self - eligible
        _logger.info(
            "[vegeta] action_run_batch_concurrent by %s: %d eligible, "
            "%d skipped", self.env.user.login, len(eligible), len(skipped),
        )

        ICP = self.env["ir.config_parameter"].sudo()
        config = {
            "function_name": ICP.get_param("vegeta.lambda_function_name"),
            "region": ICP.get_param("vegeta.lambda_region") or "ap-south-1",
            "access_key_id": ICP.get_param("vegeta.extraction_access_key_id") or "",
            "secret_access_key": ICP.get_param("vegeta.extraction_secret_access_key") or "",
            "local_url": (ICP.get_param("vegeta.lambda_local_url") or "").strip(),
            "batch_concurrency": int(
                ICP.get_param("vegeta.batch_concurrency") or _BATCH_FANOUT_POOL_SIZE
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
                "(Settings -> Vegeta -> Lambda Function), "
                "and no Lambda Local URL set."
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
        # job_name=False hands the job to the dispatch cron
        # (_cron_dispatch_prd_jobs), the sole PRD dispatcher — mirrors the
        # extraction webhook. No in-process submit here, or the job runs twice.
        if to_generate:
            to_generate.write(dict(_common, state="generating", job_name=False))
            _logger.info(
                "[vegeta] batch: %d job(s) already extracted -> PRD generation: %s",
                len(to_generate), to_generate.mapped("name"),
            )

        # --- Path B: no extraction data -> Lambda fan-out ---
        if to_extract:
            to_extract.write(dict(_common, state="extracting"))
            record_ids = to_extract.ids
            record_urls = {rec.id: rec.url for rec in to_extract}
            webhook_url = to_extract[0]._get_webhook_url()
            _logger.info(
                "[vegeta] batch: %d job(s) dispatching to extraction Lambda",
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
                local_url=config.get("local_url", ""),
            )
            return record_id, result

        _logger.info(
            "[vegeta] Batch fan-out START: %d records, max_workers=%d, "
            "function=%s, region=%s",
            len(record_ids), max_workers, config["function_name"],
            config["region"],
        )

        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="vegeta-fanout",
        ) as pool:
            futures = [pool.submit(_invoke_one, rid) for rid in record_ids]
            for future in as_completed(futures):
                try:
                    record_id, result = future.result()
                except Exception as exc:
                    _logger.exception("[vegeta] fan-out worker crashed: %s", exc)
                    continue
                if result.get("success"):
                    ok_ids.append(record_id)
                else:
                    failed[record_id] = result.get("error", "Unknown error")[:500]

        _logger.info(
            "[vegeta] Batch fan-out done: %d invoked OK, %d failed",
            len(ok_ids), len(failed),
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
                    "[vegeta] failed to revert %d records after invoke "
                    "failures", len(failed),
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
        # Terminate any in-flight K8s PRD Job so its pod is not orphaned.
        if self.state in ("generating", "scoring"):
            self._terminate_prd_k8s_job()
            vals["job_name"] = False
        self.write(vals)
        _logger.info(
            "[vegeta][job=%s] discarded by %s", self.name, self.env.user.name,
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
            "[vegeta][job=%s] reopened from discarded by %s",
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
            # job_name=False hands the job to the dispatch cron, the sole
            # PRD dispatcher (mirrors the extraction webhook).
            self.write({
                "state": "generating",
                "job_name": False,
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
                "[vegeta][job=%s] retry: prd_prompt present — skipping extraction, "
                "going straight to PRD generation",
                self.name,
            )
            return

        # No extraction data — reset to draft and re-extract from scratch.
        _logger.info(
            "[vegeta][job=%s] retry: no prd_prompt — resetting to draft for "
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
        _logger.info(
            "[vegeta] action_retry_failed_batch by %s: %d failed task(s) "
            "selected", self.env.user.login, len(eligible),
        )

        to_generate = eligible.filtered(lambda r: r.prd_prompt)
        to_extract = eligible - to_generate

        # Path B requires Lambda config; if there are any to_extract jobs,
        # validate config up front so we fail fast rather than mid-dispatch.
        ICP = self.env["ir.config_parameter"].sudo()
        config = None
        if to_extract:
            config = {
                "function_name": ICP.get_param("vegeta.lambda_function_name"),
                "region": ICP.get_param("vegeta.lambda_region") or "ap-south-1",
                "access_key_id": ICP.get_param("vegeta.extraction_access_key_id") or "",
                "secret_access_key": ICP.get_param("vegeta.extraction_secret_access_key") or "",
                "batch_concurrency": int(
                    ICP.get_param("vegeta.batch_concurrency") or _BATCH_FANOUT_POOL_SIZE
                ),
            }
            if not config["function_name"]:
                raise UserError(
                    "Lambda function name not configured "
                    "(Settings -> Vegeta -> Lambda Function). Cannot retry "
                    "failed tasks that need re-extraction."
                )

        now = fields.Datetime.now()
        db_name = self.env.cr.dbname

        # --- Path A: prd_prompt exists → straight to PRD generation ---
        # via_batch=True for unassigned tasks so the final write at the end
        # of _run_prd_generation_bg auto-releases them back to the pool with
        # full data. via_batch=False for tasker-assigned tasks so the result
        # stays with the tasker as 'done'.
        for rec in to_generate:
            rec.write({
                "state": "generating",
                "job_name": False,
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
            "[vegeta] retry-failed batch by %s: %s",
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
        _logger.info(
            "[vegeta][job=%s] action_rerun by %s: re_extract=%s "
            "has_prd_prompt=%s", self.name, self.env.user.login,
            re_extract, bool(self.prd_prompt),
        )

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
                "job_name": False,
                "score": False,
                "grade": False,
                "qc_verdict": False,
                "prd_text": False,
                "prd_text_html": False,
                "qc_report": False,
                "score_report_json": False,
                "prd_url": False,
                "llm_attempts": 0,
                "duration_seconds": False,
                "error_message": False,
                "cancel_requested": False,
                "started_at": fields.Datetime.now(),
                "completed_at": False,
                "last_heartbeat": fields.Datetime.now(),
            })

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
        wizard = self.env["vegeta.rerun.wizard"].create({"job_id": self.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Rerun Pipeline",
            "res_model": "vegeta.rerun.wizard",
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
        _logger.info(
            "[vegeta][job=%s] action_regenerate_with_qc_feedback by %s — "
            "state -> generating, QC feedback appended to prd_prompt",
            self.name, self.env.user.login,
        )
        self.write({
            "state": "generating",
            "job_name": False,
            "score": False,
            "grade": False,
            "qc_verdict": False,
            "prd_text": False,
            "prd_text_html": False,
            "qc_report": False,
            "score_report_json": False,
            "prd_url": False,
            "llm_attempts": 0,
            "error_message": False,
            "cancel_requested": False,
            "last_heartbeat": fields.Datetime.now(),
        })

        if self.prd_prompt:
            self.prd_prompt = (
                self.prd_prompt + "\n\n"
                "---\n\n"
                "## PREVIOUS QC FEEDBACK (fix these issues):\n\n"
                + qc_feedback
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

        _logger.info(
            "[vegeta][job=%s] action_rerun_qc by %s — state -> scoring, QC "
            "re-runs on the background pool", self.name, self.env.user.login,
        )
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

        _logger.info(
            "[vegeta][job=%s] QC-RERUN worker picked up job", record_id,
        )
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    _logger.warning(
                        "[vegeta][job=%s] QC-RERUN abort: record gone",
                        record_id,
                    )
                    return

                ICP = env["ir.config_parameter"].sudo()
                config = {
                    "inference_arn": ICP.get_param("vegeta.bedrock_inference_arn"),
                    "region": ICP.get_param("vegeta.bedrock_region") or "us-east-1",
                    "bedrock_access_key": ICP.get_param("vegeta.bedrock_access_key_id"),
                    "bedrock_secret_key": ICP.get_param("vegeta.bedrock_secret_access_key"),
                    "s3_bucket": ICP.get_param("vegeta.s3_bucket"),
                    "s3_key_id": ICP.get_param("vegeta.s3_access_key_id"),
                    "s3_secret": ICP.get_param("vegeta.s3_secret_access_key"),
                    "s3_region": ICP.get_param("vegeta.s3_region"),
                    "s3_endpoint_url": ICP.get_param("vegeta.s3_endpoint_url") or "",
                }
                job_data = {
                    "prd_text": record.prd_text,
                    "category_name": record.category_id.name if record.category_id else "Normal Website",
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

                            endpoint_url=config["s3_endpoint_url"],
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
                "[vegeta][job=%s] QC-RERUN complete — verdict=%s -> state=done",
                record_id, qc_result["verdict"],
            )

        except Exception as exc:
            _logger.exception(
                "[vegeta][job=%s] QC-RERUN failed — fail-closed to "
                "done/not_shippable", record_id,
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
                "bucket": ICP.get_param("vegeta.s3_bucket"),
                "key_id": ICP.get_param("vegeta.s3_access_key_id"),
                "secret": ICP.get_param("vegeta.s3_secret_access_key"),
                "region": ICP.get_param("vegeta.s3_region") or "us-east-1",
                "endpoint_url": ICP.get_param("vegeta.s3_endpoint_url") or "",
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

                            endpoint_url=s3_config["endpoint_url"],
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

                            endpoint_url=s3_config["endpoint_url"],
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
                "vegeta_job_updates",
                "vegeta/job_state",
                {"id": self.id, "state": state},
            )
        except Exception:
            pass

    def _mark_failed(self, error_msg):
        """Mark task as failed. Refuses to clobber terminal-success states."""
        if self.state in ("done", "submitted"):
            _logger.warning(
                "[vegeta][job=%s] _mark_failed ignored: state=%s is terminal-success",
                self.name, self.state,
            )
            return
        # Single chokepoint for terminal failure — grep this when a job shows
        # up `failed` to see which state it fell from and the reason given.
        _logger.warning(
            "[vegeta][job=%s] _mark_failed: state %s -> failed: %s",
            self.name, self.state, str(error_msg)[:300],
        )
        self.write({
            "state": "failed",
            "error_message": str(error_msg)[:500],
            "completed_at": fields.Datetime.now(),
            "job_name": False,
        })
        self._notify_state_change("failed")

    def _is_cancelled(self, db_name, record_id):
        """Check if a task has been cancelled (safe for background threads)."""
        try:
            with Registry(db_name).cursor() as cr:
                cr.execute(
                    "SELECT cancel_requested FROM vegeta_job WHERE id = %s",
                    (record_id,),
                )
                row = cr.fetchone()
                return row and row[0]
        except Exception:
            # DB read failed — treat as "not cancelled" so a transient error
            # never aborts a healthy run; logged so a flapping DB is visible.
            _logger.warning(
                "[vegeta][job=%s] _is_cancelled check failed — assuming "
                "not cancelled", record_id, exc_info=True,
            )
            return False


    def _get_webhook_url(self):
        ICP = self.env["ir.config_parameter"].sudo()
        override = (ICP.get_param("vegeta.webhook_url_override") or "").strip()
        if override:
            return override
        base = ICP.get_param("web.base.url", "http://localhost:8069")
        return f"{base}/api/v1/vegeta/webhook/extraction-complete"

    # ------------------------------------------------------------------
    # Background: Extraction
    # ------------------------------------------------------------------

    def _run_extraction_bg(self, db_name, record_id):
        """Background: async-invoke the extraction Lambda. Returns in <1s.

        Job stays in ``extracting`` while Lambda runs; the webhook completes
        the lifecycle. Only failed invokes flip state to ``failed``.
        """
        from ..services.extraction_service import trigger_extraction

        _logger.info(
            "[vegeta][job=%s] extraction worker picked up job", record_id,
        )
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    _logger.warning(
                        "[vegeta][job=%s] extraction abort: record gone",
                        record_id,
                    )
                    return

                ICP = env["ir.config_parameter"].sudo()
                config = {
                    "function_name": ICP.get_param("vegeta.lambda_function_name"),
                    "region": ICP.get_param("vegeta.lambda_region") or "ap-south-1",
                    "access_key_id": ICP.get_param("vegeta.extraction_access_key_id") or "",
                    "secret_access_key": ICP.get_param("vegeta.extraction_secret_access_key") or "",
                    "local_url": (ICP.get_param("vegeta.lambda_local_url") or "").strip(),
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
                    "[vegeta][job=%s] extraction Lambda invoke REJECTED — "
                    "marking failed: %s", record_id, str(error_msg)[:300],
                )
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": error_msg[:500],
                    "completed_at": fields.Datetime.now(),
                })
            else:
                # Async invoke accepted: the job now waits in `extracting`
                # for the webhook callback. If neither the callback nor the
                # "started" ping arrives, the watchdog fails it.
                _logger.info(
                    "[vegeta][job=%s] extraction Lambda invoke ACCEPTED — "
                    "awaiting webhook callback", record_id,
                )

        except Exception as exc:
            _logger.exception(
                "[vegeta][job=%s] extraction background task failed",
                record_id,
            )
            try:
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": str(exc)[:500],
                    "completed_at": fields.Datetime.now(),
                })
            except Exception:
                _logger.error(
                    "[vegeta][job=%s] failed to mark job as failed after "
                    "extraction error", record_id,
                )

    # ------------------------------------------------------------------
    # Background: PRD Generation
    # ------------------------------------------------------------------

    def _run_prd_generation_bg(self, db_name, record_id):
        """Background: generate PRD via Bedrock, score, iterate, QC.

        Runs unchanged whether driven by the in-process pool or by a K8s
        worker pod (the pod boots an Odoo registry and calls this method).
        """
        from ..services.bedrock_service import generate_prd
        from ..services.scoring_service import score_prd
        from ..services.s3_service import upload_prd_to_s3

        # Wall-clock anchor for the whole PRD-gen pipeline. Every PHASE log
        # below reports `+Ns` elapsed from here, so a stuck job's last log
        # line tells you exactly which phase it died/hung in.
        _t0 = time.monotonic()

        def _elapsed():
            return time.monotonic() - _t0

        _logger.info(
            "[vegeta][job=%s] PRD-GEN worker picked up job (pid=%d)",
            record_id, os.getpid(),
        )

        # Lease/heartbeat thread: refresh last_heartbeat every 60 s for the
        # whole run so liveness is decoupled from how long a single Bedrock
        # call takes. The reconcile/watchdog crons read last_heartbeat to
        # tell a live run from a dead one.
        stop_heartbeat = threading.Event()
        heartbeat_thread = None

        def _heartbeat_loop():
            while not stop_heartbeat.wait(timeout=60):
                try:
                    self._write_with_cursor(db_name, record_id, {
                        "last_heartbeat": fields.Datetime.now(),
                        "heartbeat_failure_count": 0,
                    })
                    _logger.debug(
                        "[vegeta][job=%s] heartbeat pulse (+%.0fs)",
                        record_id, _elapsed(),
                    )
                except Exception:
                    # CRITICAL: must NOT be _logger.debug — production
                    # log levels never capture debug, which means a Postgres
                    # pool exhaustion silently fails the heartbeat and the
                    # reconcile cron sees a stale heartbeat → re-claims a
                    # job that is still actively running → double-Bedrock
                    # spend + corrupted PRD output.
                    _logger.warning(
                        "[vegeta][job=%s] heartbeat refresh FAILED — pool "
                        "may be saturated; reconcile gate will rely on "
                        "heartbeat_failure_count",
                        record_id, exc_info=True,
                    )
                    try:
                        self._increment_heartbeat_failure(db_name, record_id)
                    except Exception:
                        # If we can't even bump the counter, the DB is in
                        # bad shape; reconcile's grossly-stale (>15min)
                        # fallback path will eventually catch the job.
                        _logger.warning(
                            "[vegeta][job=%s] heartbeat_failure_count "
                            "increment also failed", record_id,
                        )

        def _bail_if_cancelled(stage):
            """Write the cancelled state and return True when a Cancel /
            SIGTERM has been requested, so the run aborts at each stage."""
            if not self._is_cancelled(db_name, record_id):
                return False
            _logger.warning(
                "[vegeta][job=%s] cancel detected before %s — aborting",
                record_id, stage,
            )
            self._write_with_cursor(db_name, record_id, {
                "state": "draft",
                "error_message": f"Cancelled before {stage}",
                "completed_at": fields.Datetime.now(),
                "job_name": False,
            }, guard_terminal=True)
            return True

        try:
            # === PHASE 1: Read config and extraction data ===
            _logger.info(
                "[vegeta][job=%s] PHASE 1 (+%.1fs): reading config + "
                "extraction data", record_id, _elapsed(),
            )
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    _logger.warning(
                        "[vegeta][job=%s] PHASE 1 abort: record no longer "
                        "exists", record_id,
                    )
                    return
                if record.state != "generating":
                    _logger.warning(
                        "[vegeta][job=%s] worker abort: state=%s not generating",
                        record_id, record.state,
                    )
                    return

                ICP = env["ir.config_parameter"].sudo()
                config = {
                    "inference_arn": ICP.get_param("vegeta.bedrock_inference_arn"),
                    "region": ICP.get_param("vegeta.bedrock_region") or "us-east-1",
                    "bedrock_access_key": ICP.get_param("vegeta.bedrock_access_key_id"),
                    "bedrock_secret_key": ICP.get_param("vegeta.bedrock_secret_access_key"),
                    "s3_bucket": ICP.get_param("vegeta.s3_bucket"),
                    "s3_key_id": ICP.get_param("vegeta.s3_access_key_id"),
                    "s3_secret": ICP.get_param("vegeta.s3_secret_access_key"),
                    "s3_region": ICP.get_param("vegeta.s3_region"),
                    "s3_folder": ICP.get_param("vegeta.s3_folder") or "vegeta",
                    "cdn_url": ICP.get_param("vegeta.s3_cdn_url"),
                    "s3_endpoint_url": ICP.get_param("vegeta.s3_endpoint_url") or "",
                }
                job_data = {
                    "name": record.name,
                    "prd_prompt": record.prd_prompt,
                    "category_name": record.category_id.name if record.category_id else "Normal Website",
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
                        "[vegeta][job=%s] PHASE 1 abort: Bedrock inference "
                        "ARN not configured", record_id,
                    )
                    record.write({
                        "state": "failed",
                        "error_message": "Bedrock inference ARN not configured",
                        "completed_at": fields.Datetime.now(),
                        "job_name": False,
                    })
                    return
                if not job_data["prd_prompt"]:
                    _logger.error(
                        "[vegeta][job=%s] PHASE 1 abort: no prd_prompt — "
                        "extraction produced nothing usable", record_id,
                    )
                    record.write({
                        "state": "failed",
                        "error_message": "No extraction data available for PRD generation",
                        "completed_at": fields.Datetime.now(),
                        "job_name": False,
                    })
                    return

                # Worker-pickup mark: only stamp ``started_processing_at`` on
                # the FIRST pick-up. The claim SQL (worker/run_prd.py) uses
                # COALESCE so re-claims (after reconcile clears job_name)
                # preserve the original first-claim time. Phase 1 must NOT
                # overwrite — otherwise any "time since first work attempt"
                # SLA/watchdog calculation is reset on every recovery cycle.
                phase1_vals = {
                    "state": "generating",
                    "last_heartbeat": fields.Datetime.now(),
                }
                if not record.started_processing_at:
                    phase1_vals["started_processing_at"] = fields.Datetime.now()
                record.write(phase1_vals)
                cr.commit()

            # started_processing_at is now stamped — past this point the
            # watchdog treats the job as "really running" (vs merely queued).
            _logger.info(
                "[vegeta][job=%s] PHASE 1 COMPLETE (+%.1fs): category=%s "
                "prd_prompt=%dB screenshot_keys=%d -> state=generating",
                record_id, _elapsed(), job_data["category_name"],
                len(job_data["prd_prompt"] or ""),
                len(job_data["screenshot_keys"]),
            )

            heartbeat_thread = threading.Thread(
                target=_heartbeat_loop,
                name=f"vegeta-prd-hb-{record_id}",
                daemon=True,
            )
            heartbeat_thread.start()

            # === PHASE 2: LLM generation loop ===
            _logger.info(
                "[vegeta][job=%s] PHASE 2 (+%.1fs): downloading screenshots "
                "from S3 for vision", record_id, _elapsed(),
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

                            endpoint_url=config["s3_endpoint_url"],
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

            # Build multimodal content: screenshots + extraction text
            content_blocks = list(screenshot_blocks)
            content_blocks.append({"text": (
                f"Below is the extracted website data. "
                f"Write the complete PRD following all rules.\n\n"
                f"---\n\n{job_data['prd_prompt']}"
            )})
            messages = [{"role": "user", "content": content_blocks}]

            best_prd_text = None
            best_score = 0
            best_grade = None
            best_score_report = None

            # Full transparency: capture every LLM interaction for audit.
            llm_trace = {
                "prd_system_prompt": prd_system_prompt,
                "extraction_prompt": job_data["prd_prompt"],
                "screenshots_attached": len(screenshot_blocks),
                "attempts": [],
                "qc": {},
            }

            # Single-pass PRD generation; regeneration is on-demand via QC feedback.
            if _bail_if_cancelled("PRD generation"):
                return

            self._write_with_cursor(db_name, record_id, {
                "last_heartbeat": fields.Datetime.now(),
            })

            # Bedrock PRD generation — the single longest external call in
            # the pipeline. A job whose last log line is this one (with no
            # "Bedrock PRD returned" line after it) is hung inside Bedrock.
            _logger.info(
                "[vegeta][job=%s] PHASE 2 (+%.1fs): calling Bedrock for PRD "
                "generation (%d screenshot(s) attached)",
                record_id, _elapsed(), len(screenshot_blocks),
            )
            _bedrock_t0 = time.monotonic()
            prd_text = generate_prd(
                inference_arn=config["inference_arn"],
                region=config["region"],
                system_prompt=prd_system_prompt,
                messages=messages,
                access_key_id=config["bedrock_access_key"],
                secret_access_key=config["bedrock_secret_key"],
            )
            _logger.info(
                "[vegeta][job=%s] PHASE 2 (+%.1fs): Bedrock PRD returned in "
                "%.1fs — %d chars / ~%d words",
                record_id, _elapsed(), time.monotonic() - _bedrock_t0,
                len(prd_text or ""), len((prd_text or "").split()),
            )

            if _bail_if_cancelled("scoring"):
                return

            score_report = score_prd(
                prd_text=prd_text,
                category=job_data["category_name"],
            )
            total_score = score_report["total_score"]
            _logger.info(
                "[vegeta][job=%s] PHASE 2 (+%.1fs): scored %s/100 grade=%s",
                record_id, _elapsed(), total_score,
                score_report.get("grade"),
            )

            best_prd_text = prd_text
            best_score = total_score
            best_grade = score_report["grade"]
            best_score_report = score_report

            llm_trace["attempts"].append({
                "attempt": 1,
                "prd_text": prd_text,
                "score": total_score,
                "grade": score_report.get("grade"),
                "score_report": score_report,
            })

            self._write_with_cursor(db_name, record_id, {
                "llm_attempts": 1,
                "llm_trace_json": llm_trace,
                "prd_text": best_prd_text,
                "score": best_score,
                "grade": best_grade,
            })

            # Upload to S3
            _logger.info(
                "[vegeta][job=%s] PHASE 2 (+%.1fs): uploading PRD to S3",
                record_id, _elapsed(),
            )
            prd_url = upload_prd_to_s3(
                prd_text=best_prd_text,
                job_name=job_data["name"],
                bucket=config["s3_bucket"],
                access_key_id=config["s3_key_id"],
                secret_key=config["s3_secret"],
                region=config["s3_region"],

                endpoint_url=config["s3_endpoint_url"],
                folder=config["s3_folder"],
                cdn_url=config["cdn_url"],
            )

            # === PHASE 3: QC ===
            _logger.info(
                "[vegeta][job=%s] PHASE 3 (+%.1fs): PRD generation done -> "
                "state=scoring, starting QC", record_id, _elapsed(),
            )
            # Pulse the heartbeat on entry. QC can be a multi-minute Bedrock
            # call; without this pulse the gap from the last PRD-gen attempt
            # to PHASE 4's final write was fully unmonitored — long QC calls
            # could trip the watchdog while doing real work.
            self._write_with_cursor(db_name, record_id, {
                "state": "scoring",
                "last_heartbeat": fields.Datetime.now(),
            })

            if _bail_if_cancelled("QC"):
                return

            qc_verdict = "not_shippable"
            qc_report = ""
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
                    "[vegeta][job=%s] PHASE 3 (+%.1fs): QC done in %.1fs — "
                    "verdict=%s", record_id, _elapsed(),
                    time.monotonic() - _qc_t0, qc_verdict,
                )
            except Exception as qc_exc:
                _logger.warning(
                    "QC failed for job %s: %s (fail-closed: not_shippable)",
                    job_data["name"], qc_exc,
                )
                qc_verdict = "not_shippable"
                qc_report = f"QC evaluation failed: {qc_exc}\n\nVerdict defaulted to NOT SHIPPABLE (fail-closed policy)."

            llm_trace["qc"] = {
                "qc_system_prompt": qc_system_prompt,
                "verdict": qc_verdict,
                "report": qc_report,
            }

            # === PHASE 4: Write final results ===
            if _bail_if_cancelled("final write"):
                return

            _logger.info(
                "[vegeta][job=%s] PHASE 4 (+%.1fs): writing final results",
                record_id, _elapsed(),
            )
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    _logger.warning(
                        "[vegeta][job=%s] PHASE 4 abort: record gone before "
                        "final write", record_id,
                    )
                    return

                started = record.started_at
                duration = (
                    (fields.Datetime.now() - started).total_seconds()
                    if started else 0
                )

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
                    "cancel_requested": False,
                    "job_name": False,
                }
                # A Cancel/Discard/Reset that landed while scoring or QC was
                # running has already written a newer state. Re-read it in
                # this transaction; never resurrect a terminal job.
                record_is_terminal = record.state in self._TERMINAL_STATES
                if record_is_terminal:
                    _logger.warning(
                        "[vegeta][job=%s] PHASE 4: record already terminal "
                        "(state=%s) — skipping final state write",
                        record_id, record.state,
                    )
                    final_vals.pop("state")

                record.write(final_vals)

                try:
                    env["bus.bus"]._sendone(
                        "vegeta_job_updates",
                        "vegeta/job_done",
                        {"id": record_id, "name": job_data["name"]},
                    )
                except Exception:
                    _logger.debug("bus.bus notification failed for job %s (non-fatal)", record_id)

                if not record_is_terminal and record.via_batch:
                    record.write({
                        "state": "not_assigned",
                        "via_batch": False,
                        "user_id": False,
                    })
                    _logger.info(
                        "Batch pipeline done for job %s — reset to not_assigned",
                        record_id,
                    )

                cr.commit()

            _logger.info(
                "[vegeta][job=%s] PRD-GEN PIPELINE COMPLETE in %.1fs — "
                "score=%s grade=%s qc=%s",
                record_id, _elapsed(), best_score, best_grade, qc_verdict,
            )

        except Exception as exc:
            # Crash exit — elapsed pinpoints which phase's budget was burned
            # before the failure; the exception line below carries the trace.
            _logger.error(
                "[vegeta][job=%s] PRD-GEN pipeline FAILED at +%.1fs: %s",
                record_id, _elapsed(), exc,
            )
            _logger.exception("[vegeta][job=%s] PRD generation failed", record_id)
            try:
                fail_vals = {
                    "state": "failed",
                    "error_message": str(exc)[:500],
                    "completed_at": fields.Datetime.now(),
                    "job_name": False,
                }
                # Persist whatever LLM trace we accumulated before the failure.
                _trace = locals().get("llm_trace")
                if _trace:
                    fail_vals["llm_trace_json"] = _trace
                self._write_with_cursor(db_name, record_id, fail_vals)
            except Exception:
                _logger.error("[vegeta][job=%s] failed to mark as failed", record_id)

        finally:
            stop_heartbeat.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _write_with_cursor(self, db_name, record_id, vals, guard_terminal=False):
        """Write values to a record using a short-lived cursor.

        When ``guard_terminal`` is set, the record's current state is
        re-read inside this transaction; if it is already terminal the
        ``state`` key is dropped so a late background write cannot
        resurrect a finished/cancelled job.
        """
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[self._name].browse(record_id)
            if record.exists():
                if (
                    guard_terminal
                    and "state" in vals
                    and record.state in self._TERMINAL_STATES
                ):
                    _logger.warning(
                        "[vegeta][job=%s] skipping state write to %s — record "
                        "already terminal (state=%s)",
                        record_id, vals.get("state"), record.state,
                    )
                    vals = {k: v for k, v in vals.items() if k != "state"}
                if "state" in vals and record.state != vals["state"]:
                    # Every background state transition flows through here —
                    # log it so a job's full state history is greppable.
                    _logger.info(
                        "[vegeta][job=%s] state %s -> %s (bg write)",
                        record_id, record.state, vals["state"],
                    )
                if vals:
                    record.write(vals)
                    if "state" in vals:
                        try:
                            env["bus.bus"]._sendone(
                                "vegeta_job_updates",
                                "vegeta/job_state",
                                {"id": record_id, "state": vals["state"]},
                            )
                        except Exception:
                            pass
            else:
                # Late background write to a vanished record — surfaced so a
                # silently dropped write is visible, not mysterious.
                _logger.warning(
                    "[vegeta][job=%s] _write_with_cursor: record gone — "
                    "write of %s dropped", record_id, sorted(vals.keys()),
                )
            cr.commit()

    def _build_feedback(self, score_report):
        from ..services.scoring_service import SECTION_MAX_POINTS

        lines = []
        section_scores = score_report.get("section_scores", {})
        for section, section_data in section_scores.items():
            score_val = section_data["score"] if isinstance(section_data, dict) else section_data
            max_points = SECTION_MAX_POINTS.get(section, 10)
            if score_val < max_points * 0.6:
                lines.append(
                    f"- {section}: scored {score_val}/{max_points} -- needs improvement"
                )

        reject_triggers = score_report.get("reject_triggers", [])
        for trigger in reject_triggers:
            lines.append(f"- AUTO-REJECT: {trigger}")

        warnings = score_report.get("warnings", [])
        for warning in warnings:
            lines.append(f"- WARNING: {warning}")

        if not lines:
            lines.append("Minor improvements needed across all sections.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cron: Watchdog
    # ------------------------------------------------------------------

    def _cron_watchdog_stuck_jobs(self):
        """Recover tasks stuck in intermediate states beyond timeout thresholds."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(987654321)")
        locked = self.env.cr.fetchone()
        if not locked or not locked[0]:
            _logger.debug("[vegeta] watchdog: lock held elsewhere, skipping")
            return

        ICP = self.env["ir.config_parameter"].sudo()
        extracting_threshold = int(
            ICP.get_param("vegeta.watchdog_extracting_minutes", "60")
        )
        # The PRD reconcile cron (_cron_reconcile_prd_jobs) now owns recovery
        # of generating/scoring jobs. This watchdog keeps only a last-resort
        # backstop for when the dispatch/reconcile crons themselves are down,
        # so it can no longer mask a real, recoverable error with a generic
        # "timed out" message.
        generating_backstop_hours = int(
            ICP.get_param("vegeta.watchdog_generating_backstop_hours", "3")
        )

        try:
            # System-state heartbeat: one line per watchdog tick with the
            # live count of jobs in each running state. The cheapest way to
            # watch a backlog build — if `generating` climbs tick over tick
            # while `done` stays flat, the dispatch/reconcile crons are not
            # draining work.
            _wd_extracting = self.search_count([("state", "=", "extracting")])
            _wd_generating = self.search_count([("state", "=", "generating")])
            _wd_scoring = self.search_count([("state", "=", "scoring")])
            _logger.info(
                "[vegeta] watchdog tick: extracting=%d generating=%d "
                "scoring=%d (extract>%dmin, generate-backstop>%dh)",
                _wd_extracting, _wd_generating, _wd_scoring,
                extracting_threshold, generating_backstop_hours,
            )

            stale_extracting = self.search([
                ("state", "=", "extracting"),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=extracting_threshold),
                ),
            ])
            for job in stale_extracting:
                _logger.warning(
                    "[vegeta][job=%s] watchdog: stuck in extracting >%dmin — marking failed",
                    job.name, extracting_threshold,
                )
                job._mark_failed(
                    f"Watchdog: extraction timed out "
                    f"(no response for {extracting_threshold}+ minutes)"
                )

            # Backstop only: fail a generating/scoring job stuck past the
            # backstop window. Reaching here means the reconcile cron never
            # recovered it — i.e. the crons themselves are down.
            backstop_cutoff = (
                fields.Datetime.now() - timedelta(hours=generating_backstop_hours)
            )
            # (a) worker started but the run is stuck past the window.
            backstop_stuck = self.search([
                ("state", "in", ("generating", "scoring")),
                ("started_processing_at", "!=", False),
                ("started_processing_at", "<", backstop_cutoff),
            ])
            # (b) job dispatched but no worker ever picked it up:
            # started_processing_at is only set on entry to PHASE 1, so a
            # worker/pod that died before that leaves the job stuck forever.
            # Measure those from started_at instead.
            never_picked_up = self.search([
                ("state", "in", ("generating", "scoring")),
                ("started_processing_at", "=", False),
                ("started_at", "!=", False),
                ("started_at", "<", backstop_cutoff),
            ])
            for job in (backstop_stuck | never_picked_up):
                _logger.warning(
                    "[vegeta][job=%s] watchdog backstop: stuck in %s >%dh "
                    "(started_processing_at=%s, started_at=%s) — marking failed",
                    job.name, job.state, generating_backstop_hours,
                    job.started_processing_at, job.started_at,
                )
                job._mark_failed(
                    f"Watchdog backstop: {job.state} exceeded "
                    f"{generating_backstop_hours}h "
                    f"(PRD dispatch/reconcile crons may be down)"
                )
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(987654321)")

    # ------------------------------------------------------------------
    # K8s PRD-worker dispatch (ported from the aurora addon)
    # ------------------------------------------------------------------

    @api.model
    def _vegeta_namespace(self):
        return (
            self.env["ir.config_parameter"].sudo().get_param("vegeta.k8s_namespace")
            or _k8s_get_env("VEGETA_NAMESPACE")
            or VEGETA_NAMESPACE_DEFAULT
        )

    @api.model
    def _vegeta_worker_image(self):
        return (
            self.env["ir.config_parameter"].sudo().get_param("vegeta.worker_docker_image")
            or _k8s_get_env("VEGETA_WORKER_IMAGE")
            or VEGETA_WORKER_IMAGE_DEFAULT
        )

    def _build_worker_odoo_conf(self):
        """Build odoo.conf for the worker pod.

        DB credentials are written as placeholders and overridden at boot by
        run_prd.py from the pod's env vars; addons_path uses the fixed
        worker-image layout.
        """
        # Hardcoded: the worker image (Dockerfile.worker) creates and chowns
        # /tmp/odoo-data for the unprivileged `odoo` user. Inheriting the Odoo
        # backend's data_dir could point the pod at a path it cannot write.
        data_dir = "/tmp/odoo-data"
        swm = odoo_config.get("server_wide_modules")
        server_wide_modules = (
            ",".join(swm) if isinstance(swm, list) else (swm or "base,web")
        )
        return (
            "[options]\n"
            "admin_passwd = False\n"
            "db_host = False\n"
            "db_port = 5432\n"
            "db_user = False\n"
            "db_password = False\n"
            "db_name = False\n"
            "addons_path = /opt/odoo/addons,/opt/odoo/custom_addons\n"
            f"data_dir = {data_dir}\n"
            "without_demo = all\n"
            f"server_wide_modules = {server_wide_modules}\n"
        )

    def _create_prd_secret(self, core_v1, labels, owner_references=None):
        self.ensure_one()
        ns = self._vegeta_namespace()
        ICP = self.env["ir.config_parameter"].sudo()
        secret_name = f"vegeta-prd-creds-{self.id}"
        secret = k8s_client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=k8s_client.V1ObjectMeta(
                name=secret_name, namespace=ns, labels=labels,
                owner_references=owner_references,
            ),
            string_data={
                "DB_PASSWORD": odoo_config["db_password"] or "",
                "BEDROCK_ACCESS_KEY_ID": ICP.get_param("vegeta.bedrock_access_key_id") or "",
                "BEDROCK_SECRET_ACCESS_KEY": ICP.get_param("vegeta.bedrock_secret_access_key") or "",
                "S3_ACCESS_KEY_ID": ICP.get_param("vegeta.s3_access_key_id") or "",
                "S3_SECRET_ACCESS_KEY": ICP.get_param("vegeta.s3_secret_access_key") or "",
                "VEGETA_WEBHOOK_TOKEN": (
                    ICP.get_param("vegeta.webhook_token")
                    or _k8s_get_env("VEGETA_WEBHOOK_TOKEN")
                ),
            },
        )
        try:
            core_v1.create_namespaced_secret(namespace=ns, body=secret)
            _logger.info(
                "[vegeta][job=%s] created K8s Secret %s", self.id, secret_name,
            )
        except K8sApiException as exc:
            if exc.status == 409:
                # Secret already exists (retry / re-dispatch) — replace it so
                # the worker pod always reads current credentials.
                _logger.info(
                    "[vegeta][job=%s] K8s Secret %s exists — replacing",
                    self.id, secret_name,
                )
                core_v1.replace_namespaced_secret(
                    name=secret_name, namespace=ns, body=secret,
                )
            else:
                _logger.error(
                    "[vegeta][job=%s] K8s Secret %s create failed (status=%s)",
                    self.id, secret_name, exc.status, exc_info=True,
                )
                raise
        return secret_name

    def _create_worker_configmap(self, core_v1, labels, owner_references=None):
        self.ensure_one()
        ns = self._vegeta_namespace()
        cm_name = f"vegeta-worker-config-{self.id}"
        cm = k8s_client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=k8s_client.V1ObjectMeta(
                name=cm_name, namespace=ns, labels=labels,
                owner_references=owner_references,
            ),
            data={"odoo.conf": self._build_worker_odoo_conf()},
        )
        try:
            core_v1.create_namespaced_config_map(namespace=ns, body=cm)
            _logger.info(
                "[vegeta][job=%s] created K8s ConfigMap %s", self.id, cm_name,
            )
        except K8sApiException as exc:
            if exc.status == 409:
                # ConfigMap already exists (retry / re-dispatch) — replace it
                # so the worker pod mounts the current odoo.conf.
                _logger.info(
                    "[vegeta][job=%s] K8s ConfigMap %s exists — replacing",
                    self.id, cm_name,
                )
                core_v1.replace_namespaced_config_map(
                    name=cm_name, namespace=ns, body=cm,
                )
            else:
                _logger.error(
                    "[vegeta][job=%s] K8s ConfigMap %s create failed "
                    "(status=%s)", self.id, cm_name, exc.status, exc_info=True,
                )
                raise
        return cm_name

    def _delete_prd_secret(self):
        secret_name = f"vegeta-prd-creds-{self.id}"
        try:
            _load_k8s_config()
            k8s_client.CoreV1Api().delete_namespaced_secret(
                name=secret_name, namespace=self._vegeta_namespace(),
            )
        except K8sApiException as exc:
            if exc.status != 404:
                _logger.warning(
                    "[vegeta][job=%s] failed to delete Secret %s",
                    self.id, secret_name,
                )
        except Exception:
            _logger.debug(
                "[vegeta][job=%s] Secret %s cleanup failed",
                self.id, secret_name, exc_info=True,
            )

    def _delete_worker_configmap(self):
        cm_name = f"vegeta-worker-config-{self.id}"
        try:
            _load_k8s_config()
            k8s_client.CoreV1Api().delete_namespaced_config_map(
                name=cm_name, namespace=self._vegeta_namespace(),
            )
        except K8sApiException as exc:
            if exc.status != 404:
                _logger.warning(
                    "[vegeta][job=%s] failed to delete ConfigMap %s",
                    self.id, cm_name,
                )
        except Exception:
            _logger.debug(
                "[vegeta][job=%s] ConfigMap %s cleanup failed",
                self.id, cm_name, exc_info=True,
            )

    def _cleanup_prd_k8s_resources(self):
        self._delete_prd_secret()
        self._delete_worker_configmap()

    def _terminate_prd_k8s_job(self):
        """Delete this record's in-flight K8s PRD Job (SIGTERM to the pod),
        clean up its Secret/ConfigMap, and clear job_name.

        A non-`vegeta-prd-` job_name (empty, or the in-process sentinel) has
        no K8s Job and is skipped; a 404 from the API is ignored.
        """
        self.ensure_one()
        job_name = self.job_name or ""
        if not job_name.startswith("vegeta-prd-"):
            return
        if not K8S_AVAILABLE:
            _logger.warning(
                "[vegeta][job=%s] cannot terminate K8s Job %s — kubernetes "
                "package unavailable", self.id, job_name,
            )
            return
        try:
            _load_k8s_config()
            k8s_client.BatchV1Api().delete_namespaced_job(
                name=job_name,
                namespace=self._vegeta_namespace(),
                body=k8s_client.V1DeleteOptions(
                    propagation_policy="Background",
                ),
            )
            _logger.info(
                "[vegeta][job=%s] deleted in-flight K8s Job %s",
                self.id, job_name,
            )
        except K8sApiException as exc:
            if exc.status != 404:
                _logger.warning(
                    "[vegeta][job=%s] failed to delete K8s Job %s: %s",
                    self.id, job_name, exc,
                )
        except Exception:
            _logger.exception(
                "[vegeta][job=%s] error deleting K8s Job %s",
                self.id, job_name,
            )
        self._cleanup_prd_k8s_resources()
        self.job_name = False

    def _create_prd_job(self):
        """Create the per-job K8s Job (with its Secret + ConfigMap).

        The Job is created FIRST so its UID can own-reference the Secret +
        ConfigMap — K8s then garbage-collects them when the Job is removed.
        The pod may briefly report CreateContainerConfigError until the
        Secret/ConfigMap land a moment later, which is expected.

        Returns the Job name `vegeta-prd-<id>-<uuid12>`.
        """
        if not K8S_AVAILABLE:
            raise UserError(
                "kubernetes Python package is not installed on this server."
            )
        self.ensure_one()
        db_name = self.env.cr.dbname
        ns = self._vegeta_namespace()
        uid_suffix = uuid.uuid4().hex[:12]
        job_name = f"vegeta-prd-{self.id}-{uid_suffix}"

        _logger.info(
            "[vegeta][job=%s] _create_prd_job: provisioning K8s Job %s in "
            "namespace %s", self.id, job_name, ns,
        )
        _load_k8s_config()
        batch_v1 = k8s_client.BatchV1Api()
        core_v1 = k8s_client.CoreV1Api()

        labels = {
            "app.kubernetes.io/name": "vegeta-prd",
            "app.kubernetes.io/component": "prd-worker",
            "app.kubernetes.io/managed-by": "vegeta-odoo",
            "platform": "vegeta",
            "vegeta-job-id": str(self.id),
        }
        # Kueue is opt-in: only label the Job for a Kueue LocalQueue when an
        # admin has set `vegeta.kueue_queue`. A label pointing at a missing
        # queue would suspend the Job forever, so the default adds no label.
        kueue_queue = (
            self.env["ir.config_parameter"].sudo()
            .get_param("vegeta.kueue_queue", "") or ""
        ).strip()
        if kueue_queue:
            labels["kueue.x-k8s.io/queue-name"] = kueue_queue

        # Secret/ConfigMap names are deterministic from the record id, so the
        # Job (created first, below) can reference them before they exist.
        secret_name = f"vegeta-prd-creds-{self.id}"
        cm_name = f"vegeta-worker-config-{self.id}"

        def _secret_env(name, key):
            return k8s_client.V1EnvVar(
                name=name,
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=secret_name, key=key,
                    ),
                ),
            )

        env_vars = [
            k8s_client.V1EnvVar(name="JOB_ID", value=str(self.id)),
            k8s_client.V1EnvVar(name="ODOO_DB", value=db_name),
            k8s_client.V1EnvVar(
                name="PYTHONPATH", value="/opt/odoo:/opt/odoo/custom_addons",
            ),
            k8s_client.V1EnvVar(name="ODOO_CONF", value=ODOO_CONF_PATH),
            k8s_client.V1EnvVar(name="DB_HOST", value=odoo_config["db_host"]),
            k8s_client.V1EnvVar(
                name="DB_PORT", value=str(odoo_config["db_port"] or "5432"),
            ),
            k8s_client.V1EnvVar(name="DB_USER", value=odoo_config["db_user"]),
            _secret_env("DB_PASSWORD", "DB_PASSWORD"),
            _secret_env("BEDROCK_ACCESS_KEY_ID", "BEDROCK_ACCESS_KEY_ID"),
            _secret_env("BEDROCK_SECRET_ACCESS_KEY", "BEDROCK_SECRET_ACCESS_KEY"),
            _secret_env("S3_ACCESS_KEY_ID", "S3_ACCESS_KEY_ID"),
            _secret_env("S3_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY"),
            _secret_env("VEGETA_WEBHOOK_TOKEN", "VEGETA_WEBHOOK_TOKEN"),
        ]

        container = k8s_client.V1Container(
            name="prd",
            image=self._vegeta_worker_image(),
            image_pull_policy=IMAGE_PULL_POLICY,
            command=["python", WORKER_SCRIPT],
            env=env_vars,
            security_context=k8s_client.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
                allow_privilege_escalation=False,
            ),
            volume_mounts=[
                k8s_client.V1VolumeMount(
                    name="odoo-config",
                    mount_path=ODOO_CONF_PATH,
                    sub_path="odoo.conf",
                    read_only=True,
                ),
            ],
            resources=k8s_client.V1ResourceRequirements(
                requests={"cpu": CPU_REQUEST, "memory": MEMORY_REQUEST},
                limits={"memory": MEMORY_LIMIT},
            ),
        )
        volumes = [
            k8s_client.V1Volume(
                name="odoo-config",
                config_map=k8s_client.V1ConfigMapVolumeSource(name=cm_name),
            ),
        ]
        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name, namespace=ns, labels=labels,
            ),
            spec=k8s_client.V1JobSpec(
                ttl_seconds_after_finished=600,
                active_deadline_seconds=PRD_DEADLINE_SECONDS,
                backoff_limit=0,
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(
                        labels=labels,
                        annotations={"karpenter.sh/do-not-disrupt": "true"},
                    ),
                    spec=k8s_client.V1PodSpec(
                        service_account_name=VEGETA_SERVICE_ACCOUNT,
                        restart_policy="Never",
                        node_selector=NODE_SELECTOR,
                        containers=[container],
                        volumes=volumes,
                    ),
                ),
            ),
        )
        created_job = batch_v1.create_namespaced_job(namespace=ns, body=job)
        _logger.info(
            "[vegeta][job=%s] created K8s Job %s in namespace %s",
            self.id, job_name, ns,
        )

        # Owner-reference the Secret + ConfigMap to the Job so K8s garbage-
        # collects them whenever the Job is removed (TTL / delete / cascade).
        owner_references = None
        job_uid = created_job.metadata.uid if created_job.metadata else None
        if job_uid:
            owner_references = [
                k8s_client.V1OwnerReference(
                    api_version="batch/v1",
                    kind="Job",
                    name=job_name,
                    uid=job_uid,
                    controller=True,
                    block_owner_deletion=False,
                )
            ]
        else:
            _logger.warning(
                "[vegeta][job=%s] K8s Job %s returned no UID — Secret/ConfigMap "
                "will not be auto-garbage-collected",
                self.id, job_name,
            )
        self._create_prd_secret(core_v1, labels, owner_references)
        self._create_worker_configmap(core_v1, labels, owner_references)

        return job_name

    @api.model
    def _prd_execution_mode(self):
        mode = (
            self.env["ir.config_parameter"].sudo()
            .get_param("vegeta.prd_execution_mode", "worker") or "worker"
        ).strip().lower()
        return mode if mode in ("worker", "inprocess") else "worker"

    @api.model
    def _cron_dispatch_prd_jobs(self):
        """Cron (1 min): scale the worker Deployment to match queue depth.

        In ``worker`` mode (production) this cron patches the
        ``vegeta-prd-worker`` Kubernetes Deployment's replica count based
        on the active PRD load (queued + in-flight). Workers themselves
        claim jobs via ``SELECT ... FOR UPDATE SKIP LOCKED`` — this cron
        only adjusts how many worker pods exist.

        In ``inprocess`` mode (local single-process dev) this cron falls
        back to submitting pending jobs to the in-process thread pool;
        Kubernetes is not involved.

        Advisory-locked so multiple Odoo backend pods don't all try to
        scale the Deployment simultaneously.
        """
        mode = self._prd_execution_mode()
        if mode == "inprocess":
            self._run_inprocess_dispatch()
            return

        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (_PRD_DISPATCH_LOCK_ID,),
        )
        locked = self.env.cr.fetchone()
        if not locked or not locked[0]:
            _logger.debug(
                "[vegeta] PRD scaler: lock held elsewhere, skipping",
            )
            return
        try:
            self._run_worker_deployment_scaler()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (_PRD_DISPATCH_LOCK_ID,),
            )

    @api.model
    def _worker_scaler_config(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "deployment_name": ICP.get_param(
                "vegeta.worker_deployment_name", "vegeta-prd-worker",
            ),
            "namespace": ICP.get_param("vegeta.k8s_namespace", "vegeta"),
            "min_replicas": int(ICP.get_param("vegeta.worker_min_replicas", "1")),
            "max_replicas": int(ICP.get_param("vegeta.worker_max_replicas", "10")),
            "per_pod_concurrency": int(
                ICP.get_param("vegeta.worker_target_concurrency", "100"),
            ),
        }

    def _run_worker_deployment_scaler(self):
        if not K8S_AVAILABLE:
            _logger.warning(
                "[vegeta] worker scaler: kubernetes package unavailable — "
                "Deployment cannot be scaled from Odoo. Set "
                "vegeta.prd_execution_mode=inprocess for local dev, or "
                "install the kubernetes package in the Odoo backend image."
            )
            return
        cfg = self._worker_scaler_config()

        load = self.sudo().search_count([
            ("state", "=", "generating"),
            ("cancel_requested", "=", False),
        ])
        desired = -(-max(load, 0) // cfg["per_pod_concurrency"])
        desired = max(cfg["min_replicas"], min(desired, cfg["max_replicas"]))

        try:
            _load_k8s_config()
            apps_v1 = k8s_client.AppsV1Api()
            deployment = apps_v1.read_namespaced_deployment(
                name=cfg["deployment_name"], namespace=cfg["namespace"],
            )
        except Exception:
            _logger.exception(
                "[vegeta] worker scaler: failed to read Deployment %s/%s",
                cfg["namespace"], cfg["deployment_name"],
            )
            return

        # H1 + H10 fix: use max(spec, status) as "current". During scale-down
        # spec might be 1 while status.replicas is still 5 (4 pods draining)
        # — treating as 1 would prompt the scaler to leave them draining
        # and not add new pods even if a fresh burst arrived. ``or 0`` guards
        # against None which is legal mid-rollout / first deploy.
        spec_replicas = (deployment.spec.replicas if deployment.spec else 0) or 0
        status_replicas = (
            deployment.status.replicas if deployment.status else 0
        ) or 0
        current = max(spec_replicas, status_replicas)

        if current == desired:
            _logger.debug(
                "[vegeta] worker scaler: %s/%s already at %d (spec=%d "
                "status=%d, load=%d)",
                cfg["namespace"], cfg["deployment_name"], current,
                spec_replicas, status_replicas, load,
            )
            return

        # H2 fix: hysteresis. Scale UP is always immediate (burst response),
        # but scale DOWN waits for the cooldown to elapse since the last
        # change. The cron tick (1 min) is much shorter than the pod drain
        # (~30 min), so without this gate a burst+drain cycle flaps the
        # Deployment up/down/up, double-charging EC2 for partial pods that
        # then get SIGTERMed mid-job.
        if desired < current:
            ICP = self.env["ir.config_parameter"].sudo()
            cooldown_s = int(
                ICP.get_param("vegeta.worker_scale_down_cooldown_s", "600"),
            )
            last_change = ICP.get_param("vegeta.worker_last_scale_change_utc", "")
            if last_change:
                # Use Python's fromisoformat (not fields.Datetime.from_string)
                # because we write the timestamp with .isoformat(), which uses
                # the 'T' separator Odoo's parser rejects. Wrong here, the
                # bare except below would silently mask the parse error and
                # the cooldown gate would never engage — flapping returns.
                try:
                    from datetime import datetime as _dt
                    last_dt = _dt.fromisoformat(last_change)
                    elapsed = (fields.Datetime.now() - last_dt).total_seconds()
                    if elapsed < cooldown_s:
                        _logger.info(
                            "[vegeta] worker scaler: load=%d desired=%d < "
                            "current=%d (spec=%d status=%d), but scale-down "
                            "cooldown active (elapsed=%.0fs / %ds) — "
                            "deferring", load, desired, current,
                            spec_replicas, status_replicas, elapsed, cooldown_s,
                        )
                        return
                except Exception:
                    _logger.warning(
                        "[vegeta] worker scaler: could not parse "
                        "last-scale timestamp %r — proceeding with scale-down "
                        "(hysteresis effectively disabled this tick)",
                        last_change,
                    )

        _logger.info(
            "[vegeta] worker scaler: %s/%s %d -> %d replicas (spec=%d "
            "status=%d load=%d per_pod=%d range=%d..%d)",
            cfg["namespace"], cfg["deployment_name"], current, desired,
            spec_replicas, status_replicas, load,
            cfg["per_pod_concurrency"], cfg["min_replicas"], cfg["max_replicas"],
        )

        try:
            # Strategic-merge patch with just spec.replicas — avoids the
            # V1Scale object-shape issue (H8) and is the kubernetes Python
            # client's canonical scale path.
            apps_v1.patch_namespaced_deployment(
                name=cfg["deployment_name"],
                namespace=cfg["namespace"],
                body={"spec": {"replicas": desired}},
            )
            self.env["ir.config_parameter"].sudo().set_param(
                "vegeta.worker_last_scale_change_utc",
                fields.Datetime.now().isoformat(),
            )
        except Exception:
            _logger.exception(
                "[vegeta] worker scaler: patch %s/%s -> %d replicas failed",
                cfg["namespace"], cfg["deployment_name"], desired,
            )

    def _run_inprocess_dispatch(self):
        pending = self.sudo().search([
            ("state", "=", "generating"),
            ("job_name", "in", (False, "")),
            ("cancel_requested", "=", False),
        ])
        if not pending:
            return
        db_name = self.env.cr.dbname
        _logger.info(
            "[vegeta] PRD in-process dispatch: %d job(s) -> %s",
            len(pending), pending.mapped("name"),
        )
        for job in pending:
            try:
                job.write({"job_name": _INPROCESS_JOB_NAME})
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "[vegeta][job=%s] in-process dispatch failed", job.id,
                )
                continue
            _submit_bg(
                f"prd-gen[job={job.id}]",
                job._run_prd_generation_bg, db_name, job.id,
            )

    @api.model
    def _cron_reconcile_prd_jobs(self):
        """Cron (1 min): recover jobs whose owning worker pod died.

        Worker pods (or local in-process workers) stamp ``job_name`` with
        their identity and refresh ``last_heartbeat`` every 60s. If a pod is
        OOM-killed, hard-evicted, or crashes mid-run, the heartbeat goes
        stale. This cron clears ``job_name`` on any such record so a
        surviving (or freshly-spawned) worker can re-claim it on the next
        ``_claim_jobs`` tick.
        """
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (_PRD_RECONCILE_LOCK_ID,),
        )
        locked = self.env.cr.fetchone()
        if not locked or not locked[0]:
            _logger.debug("[vegeta] PRD reconcile: lock held elsewhere, skipping")
            return
        try:
            self._run_reconcile_prd_jobs()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (_PRD_RECONCILE_LOCK_ID,),
            )

    @api.model
    def _increment_heartbeat_failure(self, db_name, record_id):
        """Increment ``heartbeat_failure_count`` on a row via a fresh cursor.

        Called from the heartbeat thread when the primary
        ``_write_with_cursor`` heartbeat write fails. Uses a raw SQL
        ``UPDATE ... SET heartbeat_failure_count = heartbeat_failure_count + 1``
        rather than the ORM so this fallback path doesn't itself need a
        full Environment — the parent ORM write already failed, so we
        want the minimum-overhead path that still has a fighting chance
        on a saturated Postgres pool.
        """
        with Registry(db_name).cursor() as cr:
            cr.execute(
                "UPDATE vegeta_job "
                "   SET heartbeat_failure_count = heartbeat_failure_count + 1 "
                " WHERE id = %s",
                (record_id,),
            )
            cr.commit()

    def _run_reconcile_prd_jobs(self):
        # Two-gate recovery to avoid the false-recovery → double-Bedrock-spend
        # incident class (CRITICAL bug C2):
        #   1. heartbeat_failure_count > FAILURE_THRESHOLD AND
        #      last_heartbeat > STALE_AFTER_S      (normal recovery)
        #   2. OR last_heartbeat > GROSSLY_STALE_S  (safety net for the case
        #      where even the failure-counter increment couldn't write)
        # The single-gate "stale heartbeat alone triggers recovery" design
        # was unsafe: a transiently saturated Postgres pool caused heartbeat
        # writes to fail without crashing the worker, the reconcile cron
        # then re-claimed the job, and two workers ended up running it.
        STALE_AFTER_S = 300         # 5 min — 5x heartbeat interval
        GROSSLY_STALE_S = 900       # 15 min — safety net if counter can't write
        FAILURE_THRESHOLD = 3       # tolerate transient pool blips
        now = fields.Datetime.now()
        stale_cutoff = now - timedelta(seconds=STALE_AFTER_S)
        grossly_stale_cutoff = now - timedelta(seconds=GROSSLY_STALE_S)

        candidates = self.sudo().search([
            ("state", "in", ("generating", "scoring")),
            ("job_name", "!=", False),
            ("job_name", "!=", ""),
            ("last_heartbeat", "<", stale_cutoff),
        ])
        if not candidates:
            return

        recovered_ids = []
        skipped_ids = []
        for record in candidates:
            ref = record.last_heartbeat or record.write_date
            age = (now - ref).total_seconds() if ref else None
            failure_count = record.heartbeat_failure_count or 0
            grossly_stale = ref and ref < grossly_stale_cutoff
            failed_enough = failure_count > FAILURE_THRESHOLD

            if not (grossly_stale or failed_enough):
                skipped_ids.append((record.id, age, failure_count))
                continue

            MAX_RECOVERIES = 3
            new_recovery_count = (record.recovery_count or 0) + 1
            if new_recovery_count > MAX_RECOVERIES:
                # H4: bound the crash-loop. A job that consistently dies
                # mid-Bedrock-call would otherwise be re-queued forever,
                # burning ~$1-2/day of Bedrock per stuck row. Fail it.
                _logger.error(
                    "[vegeta][job=%s] reconcile: exceeded MAX_RECOVERIES "
                    "(%d) — marking failed to stop the crash-loop. Operator "
                    "must investigate the underlying cause (worker logs, "
                    "extraction data, prompt) and manually retry.",
                    record.name, MAX_RECOVERIES,
                )
                record.write({
                    "job_name": False,
                    "heartbeat_failure_count": 0,
                    "state": "failed",
                    "error_message": (
                        f"Reconcile recovery loop limit ({MAX_RECOVERIES}) "
                        f"reached — see logs for prior worker crashes."
                    ),
                    "completed_at": fields.Datetime.now(),
                })
                recovered_ids.append(record.id)
                continue

            _logger.warning(
                "[vegeta][job=%s] reconcile: worker=%s heartbeat stale "
                "(age=%ss, failure_count=%d, grossly_stale=%s, "
                "recovery_count=%d/%d) — clearing job_name so another "
                "worker re-claims",
                record.name, record.job_name, int(age) if age else "?",
                failure_count, grossly_stale, new_recovery_count,
                MAX_RECOVERIES,
            )
            record.write({
                "job_name": False,
                "heartbeat_failure_count": 0,
                "recovery_count": new_recovery_count,
            })
            recovered_ids.append(record.id)

        if recovered_ids:
            _logger.info(
                "[vegeta] PRD reconcile: re-queued %d stale job(s) for "
                "re-claim by workers (ids=%s)",
                len(recovered_ids), recovered_ids,
            )
        if skipped_ids:
            _logger.info(
                "[vegeta] PRD reconcile: %d job(s) stale but under recovery "
                "gate (id, age_s, fail_count) = %s",
                len(skipped_ids), skipped_ids[:10],
            )


def _markdown_to_html(md_text: str) -> str:
    """Convert markdown PRD to basic HTML for the rich-text editor."""
    import re
    from markupsafe import escape
    if not md_text:
        return ""
    md_text = str(escape(md_text))
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
