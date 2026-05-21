import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import requests

from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

_PRD_POOL_SIZE = int(os.environ.get("GOHAN_PRD_POOL_SIZE", "50"))
_POOL = ThreadPoolExecutor(
    max_workers=_PRD_POOL_SIZE, thread_name_prefix="gohan-prd"
)

# How many extra attempts `_write_with_cursor` makes on transient psycopg2
# InterfaceError / OperationalError before propagating. 2 retries = 3 total
# attempts, with backoff 0.5s, 1.0s — covers idle-backend reap blips without
# noticeably delaying real failures.
_WRITE_CURSOR_RETRIES = 2

_BATCH_FANOUT_POOL_SIZE = int(os.environ.get("GOHAN_BATCH_FANOUT_SIZE", "250"))

# Shared advisory-lock namespace used by:
#   - controllers/main.py (webhook handler)
#   - models/gohan_job.py::_attempt_s3_rescue (S3 poller + reconcile cron)
# Both writers acquire pg_try_advisory_xact_lock(_GOHAN_WEBHOOK_LOCK_NS, job_id)
# before mutating a job row. Without this shared namespace the webhook callback
# and the S3 poller can both write the same row in overlapping transactions and
# the loser hits 40001 (SerializationFailure) at commit time — which is exactly
# what the user reported for job 25 on turo.com (webhook rolled back, poller
# wrote partial data, UI showed empty extraction).
_GOHAN_WEBHOOK_LOCK_NS = 0x60134A82


def _submit_bg(label, fn, *args, **kwargs):
    """Submit a background job to the shared pool — with uptime guarantees.

    - Logs a warning when the pool queue is backing up (saturation) so a slow
      Bedrock/S3 never silently swallows work without a trace.
    - Wraps the callable so any escaped exception is logged instead of being
      lost in an un-awaited Future.
    - If the pool is gone (process recycling), runs inline as a last resort so
      the job is never silently dropped. The watchdog cron is the final backstop.
    """
    try:
        qsize = _POOL._work_queue.qsize()
        if qsize > _PRD_POOL_SIZE:
            _logger.warning(
                "[gohan] PRD pool saturated: %d queued / %d workers — jobs "
                "will run but are delayed; raise GOHAN_PRD_POOL_SIZE.",
                qsize, _PRD_POOL_SIZE,
            )
    except Exception:
        pass

    def _guarded():
        try:
            return fn(*args, **kwargs)
        except Exception:
            _logger.exception("[gohan] background task '%s' crashed", label)

    try:
        return _POOL.submit(_guarded)
    except RuntimeError:
        _logger.error(
            "[gohan] thread pool unavailable for '%s' — running inline", label,
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
                "[gohan] resized image %dx%d -> %dx%d (%d -> %d bytes) for Bedrock",
                w, h, im.size[0], im.size[1], len(img_bytes), len(new_bytes),
            )
            return new_bytes
    except Exception as exc:
        _logger.warning(
            "[gohan] image resize failed (%s) — sending original; Bedrock "
            "may reject if >8000px", exc,
        )
        return img_bytes


def _svg_to_png(svg_bytes: bytes, output_width: int = 256) -> bytes | None:
    """Rasterize SVG bytes to PNG bytes.

    Returns ``None`` when conversion is unavailable (``cairosvg`` not
    installed) or fails (malformed SVG). ``cairosvg`` is an OPTIONAL
    dependency: it is deliberately NOT listed in the manifest's
    ``external_dependencies`` so a missing install degrades gracefully
    (icon kept as .svg) instead of blocking the whole module from loading.
    """
    if not svg_bytes:
        return None
    try:
        import cairosvg
    except ImportError:
        return None
    try:
        return cairosvg.svg2png(bytestring=svg_bytes, output_width=output_width)
    except Exception as exc:
        _logger.warning("[gohan] SVG->PNG conversion failed: %s", exc)
        return None


class GohanJob(models.Model):
    _name = "gohan.job"
    _description = "Gohan Pipeline Task"
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
    target_resolution = fields.Char(
        string="Target Resolution",
        default="1920x1080",
        help=(
            "Primary viewport for screenshot capture (WxH). The Gohan "
            "pipeline accepts a --resolution override; default is "
            "1920x1080 per the Gohan platform spec."
        ),
    )
    marketing_url_pass = fields.Char(
        string="Marketing URL (auth-gated fallback)",
        help=(
            "Fallback URL used when the primary URL is behind a login. "
            "The pipeline scrapes this marketing-pages variant when "
            "auth_gated=True and no cookies file is supplied."
        ),
    )
    auth_gated = fields.Boolean(
        string="Auth-Gated",
        help=(
            "True when the target app requires login to render its "
            "primary functional surface. Triggers marketing-pages "
            "fallback or cookie-injected capture per pipeline rules."
        ),
    )
    state = fields.Selection(
        [
            ("not_assigned", "Not Assigned"),
            ("draft", "Draft"),
            ("extracting", "Extracting"),
            ("extracted", "Extracted — Review"),
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
    category_id = fields.Many2one("gohan.category", string="Website Category")
    category_key = fields.Char(related="category_id.code", store=True)
    score = fields.Float(string="PRD Score", digits=(5, 2))
    score_display = fields.Char(
        string="Score", compute="_compute_score_display", store=False,
    )
    grade = fields.Char(string="Grade")
    qc_verdict = fields.Selection(
        [
            ("pending", "PENDING"),
            ("shippable", "SHIPPABLE"),
            ("fixes", "SHIPPABLE WITH FIXES"),
            ("not_shippable", "NOT SHIPPABLE"),
        ],
        string="QC Verdict",
    )
    eq_tier = fields.Selection(
        [
            ("AUTHENTICATED", "Authenticated"),
            ("API_DOCS", "API Docs"),
            ("MARKETING_RICH", "Marketing Rich"),
            ("MARKETING_ONLY", "Marketing Only"),
        ],
        string="Evidence Quality Tier",
        help=(
            "Evidence Quality tier classified by the Gohan pipeline. "
            "AUTHENTICATED: in-app screenshots with cookies. "
            "API_DOCS: public API/developer documentation. "
            "MARKETING_RICH: marketing pages with deep product detail. "
            "MARKETING_ONLY: marketing pages with surface-level info."
        ),
    )
    mode = fields.Selection(
        [
            ("mode1", "Mode 1 (Single-Pass)"),
            ("mode2_iter_1", "Mode 2 - Iteration 1"),
            ("mode2_iter_2", "Mode 2 - Iteration 2"),
            ("mode2_iter_3", "Mode 2 - Iteration 3"),
            ("mode2_iter_4", "Mode 2 - Iteration 4"),
            ("mode2_iter_5", "Mode 2 - Iteration 5"),
        ],
        string="Pipeline Mode",
        default="mode1",
        help=(
            "Gohan pipeline execution mode. mode1 is a single-pass run; "
            "mode2_iter_N indicates the Nth refinement iteration of a "
            "multi-pass run (max 5 iterations per spec)."
        ),
    )
    prd_text = fields.Text(string="PRD Document")
    prd_text_html = fields.Html(
        string="PRD (Formatted)",
        compute="_compute_prd_text_html",
        sanitize=False,
        help="Read-only formatted view of the PRD, rendered from the "
             "editable Markdown. Edit the Markdown to change it.",
    )
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
    asset_ids = fields.One2many(
        "gohan.job.asset",
        "job_id",
        string="Extraction Assets",
        help=(
            "Screenshots and SVG icons captured by the extraction Lambda, "
            "plus any files the tasker uploads. Curated in the Extraction "
            "Review tab while the job waits in the 'Extracted' state; only "
            "ticked rows feed PRD generation."
        ),
    )
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

    # Computed HTML for asset previews
    screenshot_urls_html = fields.Html(
        string="Screenshot Previews", compute="_compute_asset_previews",
        sanitize=False,
    )
    prd_prompt_html = fields.Html(
        string="Extraction Data (sent to LLM)",
        compute="_compute_prd_prompt_html",
        sanitize=False,
        help="Markdown-rendered view of the prompt sent to the LLM. "
             "Falls back to a <pre> block if the `markdown` library is "
             "unavailable.",
    )
    asset_urls_html = fields.Html(
        string="Asset Previews", compute="_compute_asset_previews",
        sanitize=False,
    )
    asset_score_html = fields.Html(
        string="Extraction Summary", compute="_compute_asset_score_html",
        sanitize=False,
    )
    routes_html = fields.Html(
        string="Routes",
        compute="_compute_routes_html",
        sanitize=False,
        help="Renders the product's discovered route map -- the same-site "
             "URL surface (path, screen type, title) extracted by the "
             "Lambda -- as a single table, so taskers see the routes at a "
             "glance without scrolling the raw lambda_callback_json blob.",
    )
    score_report_html = fields.Html(
        string="Score Report (HTML)", compute="_compute_score_report_html",
        sanitize=False,
    )
    qc_report_html = fields.Html(
        string="QC Report (HTML)",
        compute="_compute_qc_report_html",
        sanitize=False,
        help="Markdown-rendered view of the QC report for the Quality tab.",
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

    # ------------------------------------------------------------------
    # Spec-compliance fields (HANDOFF_ODOO.md §4 — gohan.run / gohan.prd /
    # gohan.deliverable / gohan.url shapes, collapsed onto gohan.job).
    # ------------------------------------------------------------------
    notes = fields.Text(string="Notes")
    lambda_request_id = fields.Char(
        string="AWS Request ID", readonly=True, copy=False,
        help="X-Amzn-RequestId returned by API Gateway or Lambda for this run.",
    )
    s3_artifact_prefix = fields.Char(
        string="S3 Artifact Prefix",
        help="s3://{bucket}/runs/{job_id}/ — root of all pipeline artifacts.",
    )
    # Pipeline journal: append-only list of {ts, step, status, message, **ctx}
    # events emitted by every background runner + the extraction webhook. The
    # whole point of this field is that an operator looking at a failed (or
    # weirdly stuck) job sees ONE place that lists *exactly* which step ran,
    # which one errored, and what the error was — without grepping container
    # logs. `_append_pipeline_event` is the only writer; it also mirrors any
    # `status="error"` event into `error_message` if that field is still
    # empty, so the form's error banner stays in sync automatically.
    pipeline_log_json = fields.Json(
        string="Pipeline Log",
        help=(
            "Auto-populated step-by-step journal of every pipeline phase "
            "(extraction invoke, webhook callback, PRD generation attempts, "
            "scoring, QC, final upload). Each entry is "
            "{ts, step, status, message, ...context}. Errors at any step "
            "are appended here AND copied into the Error Message field."
        ),
    )
    score_max = fields.Float(string="Max Score", default=100.0)
    qc_critical_count = fields.Integer(string="QC Critical Issues")
    qc_high_count = fields.Integer(string="QC High Issues")
    qc_medium_count = fields.Integer(string="QC Medium Issues")
    word_count = fields.Integer(
        string="PRD Word Count",
        help="Word count of the generated PRD body, per spec word-band rubric.",
    )
    s3_zip_url = fields.Char(
        string="Deliverable ZIP URL",
        help="Signed URL or s3:// path to the SOP deliverable bundle.",
    )
    references_count = fields.Integer(string="References Count")
    page_assets_count = fields.Integer(string="Page Assets Count")
    ready_for_submission = fields.Boolean(string="Ready for Submission")

    # ------------------------------------------------------------------
    # Pipeline-data guard (UX) — blocks Start Task / Run from picking
    # tasks that already carry PRD or extraction artefacts. Stored so
    # the Start Task wizard's search domain can filter on it directly.
    # ------------------------------------------------------------------
    has_pipeline_data = fields.Boolean(
        string="Has Pipeline Data",
        compute="_compute_has_pipeline_data",
        store=True,
        help="True if PRD text, PRD prompt, or extraction data is already "
             "present. Use the Rerun wizard rather than Start Task / Run "
             "for such tasks.",
    )

    @api.depends("prd_text", "prd_prompt", "site_discovery_json")
    def _compute_has_pipeline_data(self):
        for rec in self:
            rec.has_pipeline_data = bool(
                rec.prd_text or rec.prd_prompt or rec.site_discovery_json
            )

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

    # ------------------------------------------------------------------
    # Prompt helpers (read from Settings, fallback to file)
    # ------------------------------------------------------------------

    @api.model
    def _get_prd_system_prompt(self):
        """Read the PRD system prompt from the bundled file (file is the
        source of truth); fall back to the Settings sysparam.

        Priority order:
          1. ``prompts/prd_v9_pipeline.md`` — the active prompt template (v9).
             Edit this file + restart Odoo to update the prompt.
          2. ``gohan.prd_system_prompt`` sysparam — last-resort override for
             operators who edit through Settings UI without touching files.
        """
        prompts_dir = Path(__file__).parent.parent / "prompts"
        for fname in ("prd_v9_pipeline.md",):
            p = prompts_dir / fname
            if p.exists():
                content = p.read_text(encoding="utf-8")
                if content.strip():
                    return content.strip()
        ICP = self.env["ir.config_parameter"].sudo()
        prompt = ICP.get_param("gohan.prd_system_prompt", "")
        return prompt.strip() if prompt else ""

    @staticmethod
    def _extract_runnable_prompt(content):
        """Return the runnable prompt body from a prompt-doc file.

        A QC / PRD prompt file may be a full human-readable document that
        wraps the runnable prompt in ``## ---BEGIN PROMPT---`` / ``## ---END
        PROMPT---`` markers, with operator notes outside them. Only the text
        between the markers is sent to the model. When the markers are
        absent the whole (stripped) content is treated as the prompt.
        """
        begin = "## ---BEGIN PROMPT---"
        end = "## ---END PROMPT---"
        if begin in content and end in content:
            body = content.split(begin, 1)[1].split(end, 1)[0]
            return body.strip()
        return content.strip()

    @api.model
    def _get_qc_system_prompt(self):
        """Read the QC system prompt from the bundled file (file is the
        source of truth); fall back to the Settings sysparam, then to the
        built-in default.

        Priority order:
          1. ``prompts/qc_v1.md`` — the QC reviewer document. Only the text
             between the ``## ---BEGIN PROMPT---`` / ``## ---END PROMPT---``
             markers is used; operator notes outside them are stripped.
          2. ``gohan.qc_system_prompt`` sysparam — Settings UI override.
          3. ``DEFAULT_QC_SYSTEM_PROMPT`` — built-in last-resort fallback.
        """
        prompts_dir = Path(__file__).parent.parent / "prompts"
        for fname in ("qc_v1.md",):
            p = prompts_dir / fname
            if p.exists():
                content = p.read_text(encoding="utf-8")
                runnable = self._extract_runnable_prompt(content)
                if runnable:
                    return runnable
        ICP = self.env["ir.config_parameter"].sudo()
        prompt = ICP.get_param("gohan.qc_system_prompt", "")
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
                "gohan.group_gohan_admin"
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

    @api.depends("qc_report")
    def _compute_qc_report_html(self):
        """Render the markdown QC report as HTML for the Quality tab."""
        from markupsafe import Markup
        for rec in self:
            if rec.qc_report:
                rec.qc_report_html = Markup(
                    '<div class="gohan-qc-report">'
                    + _markdown_to_html(rec.qc_report)
                    + '</div>'
                )
            else:
                rec.qc_report_html = False

    @api.depends("prd_text")
    def _compute_prd_text_html(self):
        """Render the editable Markdown PRD as the read-only formatted view."""
        from markupsafe import Markup
        for rec in self:
            if rec.prd_text:
                rec.prd_text_html = Markup(_markdown_to_html(rec.prd_text))
            else:
                rec.prd_text_html = False

    @api.depends("prd_prompt")
    def _compute_prd_prompt_html(self):
        try:
            import markdown as _md
        except ImportError:
            _md = None
        from markupsafe import Markup, escape

        for rec in self:
            text = rec.prd_prompt or ""
            if not text.strip():
                rec.prd_prompt_html = False
                continue
            if _md is None:
                rec.prd_prompt_html = Markup(
                    '<pre class="gohan-prd-prompt-markdown" '
                    'style="white-space:pre-wrap;word-break:break-word;">'
                    f"{escape(text)}</pre>"
                )
                continue
            try:
                body = _md.markdown(
                    text,
                    extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
                    output_format="html5",
                )
                rec.prd_prompt_html = Markup(
                    f'<div class="gohan-prd-prompt-markdown">{body}</div>'
                )
            except Exception:
                _logger.warning(
                    "[gohan][job=%s] markdown render failed; falling back to <pre>",
                    rec.id, exc_info=True,
                )
                rec.prd_prompt_html = Markup(
                    '<pre class="gohan-prd-prompt-markdown" '
                    'style="white-space:pre-wrap;word-break:break-word;">'
                    f"{escape(text)}</pre>"
                )

    @api.depends("screenshot_keys", "asset_keys")
    def _compute_asset_previews(self):
        """Build HTML preview galleries for screenshots and assets."""
        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("gohan.s3_bucket", "")
        region = ICP.get_param("gohan.s3_region", "us-east-1")
        cdn_url = ICP.get_param("gohan.s3_cdn_url", "")

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

    def _load_extraction_artifacts_from_s3(self):
        """Lazy-fetch the full set of extraction artifacts from S3.

        Returns a dict keyed by phase name (``network_data``, ``api_doc_extracted``,
        ``auth_data``, ``extraction_quality``, ``inferred_*``, ``sitemap_taxonomy``,
        ``site_discovery``, ``public_kb``, ``interaction_data``, ``screen_captures``,
        ``functional_inference``, ``feature_inventory``, etc.) with each value
        being the parsed JSON. Returns ``{}`` when nothing can be loaded.

        Tries four S3 layouts in priority order — the Lambda has produced all
        four shapes across deploys:
          A. ``{folder}/{id}/raw_data.json``           (merged, id-based)
          B. ``{folder}/{name}/raw_data.json``         (merged, name-based)
          C. ``{folder}/{name}/artifacts/raw_data/*``  (individual, name-based)
          D. ``{folder}/{id}/artifacts/raw_data/*``    (individual, id-based)

        Fires for ``done``/``reviewed``/``shipped``/``failed`` so partial
        extractions stay inspectable.
        """
        self.ensure_one()
        if self.state not in ("done", "reviewed", "shipped", "failed"):
            return {}
        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("gohan.s3_bucket")
        if not bucket:
            return {}
        s3_folder = (ICP.get_param("gohan.s3_folder") or "gohan").strip("/")

        # Path-suffix derived from s3_artifact_prefix is bucket-agnostic.
        import re
        stored_path = ""
        if self.s3_artifact_prefix:
            m = re.match(r"^s3://[^/]+/(.+)$", self.s3_artifact_prefix)
            if m:
                stored_path = m.group(1).rstrip("/")

        try:
            from ..services import s3_service
        except ImportError:
            return {}

        # Candidate locations for the merged bundle.
        merged_candidates = []
        if stored_path:
            merged_candidates.append(f"{stored_path}/raw_data.json")
        merged_candidates.append(f"{s3_folder}/{self.id}/raw_data.json")
        if self.name:
            merged_candidates.append(f"{s3_folder}/{self.name}/raw_data.json")

        for key in merged_candidates:
            try:
                data = s3_service.download_json_from_s3(self.env, bucket, key)
                if isinstance(data, dict) and data:
                    _logger.info(
                        "[gohan][job=%s] backend_signals: loaded merged bundle s3://%s/%s",
                        self.id, bucket, key,
                    )
                    return data
            except Exception:
                continue

        # No merged file — rebuild from individual phase files.
        individual_prefixes = []
        if self.name:
            individual_prefixes.append(f"{s3_folder}/{self.name}/artifacts/raw_data/")
        individual_prefixes.append(f"{s3_folder}/{self.id}/artifacts/raw_data/")
        if stored_path:
            individual_prefixes.append(f"{stored_path}/artifacts/raw_data/")

        rebuilt = {}
        for prefix in individual_prefixes:
            try:
                keys = s3_service.list_objects(self.env, bucket, prefix)
            except Exception:
                keys = []
            if not keys:
                continue
            for k in keys:
                if not k.lower().endswith(".json"):
                    continue
                phase = k.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if not phase or phase in rebuilt:
                    continue
                try:
                    rebuilt[phase] = s3_service.download_json_from_s3(
                        self.env, bucket, k
                    )
                except Exception as exc:
                    _logger.debug(
                        "[gohan][job=%s] backend_signals: skip %s (%s)",
                        self.id, k, exc,
                    )
            if rebuilt:
                _logger.info(
                    "[gohan][job=%s] backend_signals: rebuilt %d artifacts "
                    "from s3://%s/%s",
                    self.id, len(rebuilt), bucket, prefix,
                )
                return rebuilt
        return {}

    @api.depends("lambda_callback_json", "site_discovery_json")
    def _compute_routes_html(self):
        """Render the product's discovered routes as a single table.

        Routes are the product's own same-site URL surface (path + screen
        type + title), extracted by the Lambda. Source priority: the
        callback's top-level ``routes`` list, then the ``site_discovery``
        block, then the parsed ``site_discovery_json`` -- finally falling
        back to deriving routes from a bare ``pages`` list for jobs that
        were extracted before routes were emitted.
        """
        from urllib.parse import urlparse
        from markupsafe import escape

        # Screen-type -> chip color, so a route table is scannable at a
        # glance (auth screens red, dashboards purple, etc.).
        type_colors = {
            "login": "#dc3545", "signup": "#dc3545", "dashboard": "#6f42c1",
            "list": "#0066cc", "detail": "#17a2b8", "form": "#fd7e14",
            "settings": "#6c757d", "profile": "#20c997", "search": "#0066cc",
            "marketing": "#28a745",
        }

        def _routes_from_pages(pages):
            """Derive route dicts from a bare list of page URLs / dicts."""
            out = []
            seen = set()
            for entry in pages or []:
                if isinstance(entry, dict):
                    href = entry.get("url") or entry.get("href") or ""
                    title = entry.get("title") or entry.get("label") or ""
                else:
                    href = str(entry or "")
                    title = ""
                if not href:
                    continue
                try:
                    path = urlparse(href).path or "/"
                except Exception:
                    path = href
                if len(path) > 1:
                    path = path.rstrip("/")
                if path in seen:
                    continue
                seen.add(path)
                out.append({
                    "path": path, "url": href,
                    "title": title, "screen_type": "",
                })
            return out

        for rec in self:
            callback = rec.lambda_callback_json or {}
            sd = rec.site_discovery_json or {}
            cb_sd = callback.get("site_discovery")
            cb_sd = cb_sd if isinstance(cb_sd, dict) else {}

            routes = (
                callback.get("routes")
                or cb_sd.get("routes")
                or (sd.get("routes") if isinstance(sd, dict) else None)
            )
            if not routes:
                pages = (
                    (sd.get("pages") if isinstance(sd, dict) else None)
                    or cb_sd.get("pages")
                    or callback.get("pages")
                )
                routes = _routes_from_pages(pages)

            if not routes:
                rec.routes_html = ""
                continue

            rows = ""
            for r in routes:
                if not isinstance(r, dict):
                    r = {"path": str(r), "url": "", "title": "",
                         "screen_type": ""}
                path = r.get("path") or "/"
                stype = (r.get("screen_type") or "").strip()
                title = r.get("title") or ""
                href = r.get("url") or ""
                color = type_colors.get(stype.lower(), "#6c757d")
                chip = (
                    f'<span style="display:inline-block;padding:1px 8px;'
                    f'background:{color};color:#fff;border-radius:3px;'
                    f'font-size:11px;">{escape(stype or "page")}</span>'
                )
                if href:
                    path_cell = (
                        f'<a href="{escape(href)}" target="_blank" '
                        f'style="color:#0066cc;text-decoration:none;'
                        f'word-break:break-all;">{escape(path)}</a>'
                    )
                else:
                    path_cell = escape(path)
                rows += (
                    f'<tr style="border-bottom:1px solid #f1f3f5;">'
                    f'<td style="padding:6px 12px 6px 0;vertical-align:top;'
                    f'font-family:monospace;word-break:break-all;">{path_cell}</td>'
                    f'<td style="padding:6px 12px 6px 0;vertical-align:top;'
                    f'white-space:nowrap;">{chip}</td>'
                    f'<td style="padding:6px 0;vertical-align:top;'
                    f'color:#495057;">{escape(title)}</td>'
                    f'</tr>'
                )

            rec.routes_html = (
                '<div style="margin-bottom:16px;border:1px solid #dee2e6;'
                'border-radius:6px;overflow:hidden;">'
                '<div style="padding:8px 12px;background:#0066cc;color:#fff;">'
                '<span style="font-weight:700;font-size:14px;">Routes</span>'
                '<span style="margin-left:10px;font-size:11px;opacity:0.85;">'
                f'{len(routes)} same-site route(s) discovered on the source '
                'website</span></div>'
                '<div style="padding:12px;background:#fff;">'
                '<table style="width:100%;border-collapse:collapse;'
                'font-size:13px;">'
                '<tr style="border-bottom:2px solid #dee2e6;">'
                '<th style="text-align:left;padding:4px 12px 4px 0;'
                'color:#6c757d;">Path</th>'
                '<th style="text-align:left;padding:4px 12px 4px 0;'
                'color:#6c757d;">Type</th>'
                '<th style="text-align:left;padding:4px 0;'
                'color:#6c757d;">Title</th>'
                f'</tr>{rows}</table></div></div>'
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
                    self.env["ir.sequence"].next_by_code("gohan.job") or "New"
                )
            # If user_id is set at creation, auto-promote to draft
            if vals.get("user_id") and vals.get("state", "not_assigned") == "not_assigned":
                vals["state"] = "draft"
            # If no user, must be not_assigned
            if not vals.get("user_id") and vals.get("state") in ("draft", "done"):
                vals["state"] = "not_assigned"
        return super().create(vals_list)

    def write(self, vals):
        # Snapshot prior state per record so we can detect actual transitions
        # (writing the same value shouldn't fire a notification — avoids spam
        # when other fields are updated).
        prior_states = {rec.id: rec.state for rec in self} if "state" in vals else {}
        res = super().write(vals)
        # Auto-promote to draft when admin assigns a user to not_assigned task
        if "user_id" in vals and vals["user_id"]:
            to_promote = self.filtered(lambda r: r.state == "not_assigned")
            if to_promote:
                super(GohanJob, to_promote).write({"state": "draft"})
        # Auto-demote to not_assigned when user is removed from draft task
        if "user_id" in vals and not vals["user_id"]:
            to_demote = self.filtered(lambda r: r.state == "draft")
            if to_demote:
                super(GohanJob, to_demote).write({"state": "not_assigned"})
        # Fire bus notification on every real state transition so the frontend
        # `gohan_bus.js` subscriber can auto-reload the form view without the
        # operator having to refresh. Sending after super().write() means the
        # DB row already reflects the new state when the client reloads.
        if "state" in vals:
            for rec in self:
                if prior_states.get(rec.id) != rec.state:
                    try:
                        self.env["bus.bus"]._sendone(
                            "gohan_job_updates",
                            "gohan/job_state" if rec.state != "done" else "gohan/job_done",
                            {"id": rec.id, "state": rec.state},
                        )
                    except Exception:
                        _logger.debug(
                            "bus.bus notification failed for job %s (non-fatal)",
                            rec.id,
                        )
        return res

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    _ACTIVE_STATES = ("draft", "extracting", "extracted", "generating", "scoring", "done")

    def action_start_task(self):
        """Tasker grabs the next available unassigned task.

        Picks the oldest not_assigned task. If it already has PRD data
        (released from done), state goes to done. Otherwise draft.
        """
        user = self.env.user
        ICP = self.env["ir.config_parameter"].sudo()
        max_active = int(ICP.get_param("gohan.max_jobs_per_user", "5"))

        # Check bandwidth
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

        # Pick oldest available task (not_assigned or failed+unassigned).
        # Tasks with existing pipeline data are excluded — operators must use
        # the Rerun wizard for those, never the Start Task button. This
        # prevents accidentally re-triggering the pipeline on stale data.
        domain = [
            ("state", "in", ("not_assigned", "failed")),
            ("user_id", "=", False),
            ("has_pipeline_data", "=", False),
        ]
        cat_id = self.env.context.get("start_task_category_id")
        if cat_id:
            domain.append(("category_id", "=", cat_id))
        task = self.sudo().search(
            domain,
            order="create_date asc",
            limit=1,
        )
        if not task:
            raise UserError(
                "No clean tasks available. Tasks that already have pipeline "
                "data are excluded — use the Rerun wizard on those instead, "
                "or wait for new clean tasks to be imported."
            )

        # Domain guarantees the task has no pipeline data → always draft.
        task.write({"user_id": user.id, "state": "draft"})
        task._notify_state_change("draft")

        # Navigate to the picked task
        return {
            "type": "ir.actions.act_window",
            "res_model": "gohan.job",
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
            wizard = self.env["gohan.rerun.wizard"].create({"job_id": self.id})
            return {
                "type": "ir.actions.act_window",
                "name": "Extraction Data Exists",
                "res_model": "gohan.rerun.wizard",
                "res_id": wizard.id,
                "view_mode": "form",
                "views": [(False, "form")],
                "target": "new",
            }

        # Per-user concurrent job limit (only count running tasks, not draft/done)
        max_jobs = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("gohan.max_jobs_per_user", "5")
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
        sp_name = f"gohan_run_{self.id}"
        self.env.cr.execute(f"SAVEPOINT {sp_name}")
        try:
            self.env.cr.execute(
                "SELECT id FROM gohan_job WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Exception:
            self.env.cr.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
            raise UserError("Task is being modified by another session. Try again.")

        self.env.cr.execute(
            "SELECT state FROM gohan_job WHERE id = %s", [self.id]
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
        _logger.info("[gohan][job=%s] cancelled by %s", self.name, self.env.user.name)
        self._notify_state_change("draft")

    def action_reconcile_from_s3(self):
        """Manually pull Lambda's ``raw_data.json`` from S3 and auto-populate.

        Use when a task is stuck in extracting / generating / scoring (webhook
        never landed) or has been marked failed but Lambda actually finished.
        Reads ``s3://{gohan.s3_bucket}/{prefix}/raw_data.json`` synchronously,
        maps every JSON key to its model field, advances state to
        ``generating`` if currently extracting/failed, and submits PRD
        generation to the background pool.
        """
        self.ensure_one()
        if self.state not in ("extracting", "generating", "scoring", "failed"):
            raise UserError(
                "Reconcile is only available while a task is running or has failed."
            )

        rescued, reason = self._attempt_s3_rescue(source="manual-button")
        if rescued:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Reconcile succeeded",
                    "message": reason,
                    "type": "success",
                    "sticky": False,
                },
            }
        raise UserError(
            f"S3 reconcile failed: {reason}\n\n"
            f"This typically means Lambda hasn't uploaded raw_data.json yet, "
            f"or the prefix is wrong. Check the job's S3 Artifact Prefix "
            f"field and the lambda CloudWatch logs."
        )

    def action_run_batch_concurrent(self):
        """Server action: fire all selected jobs in parallel via async Lambda invoke.

        Replaces the legacy RabbitMQ + consumer.py fan-out. Uses a single
        ThreadPoolExecutor sized to ``gohan.batch_concurrency`` (default 250)
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
            "function_name": ICP.get_param("gohan.lambda_function_name"),
            "region": ICP.get_param("gohan.lambda_region") or "ap-south-1",
            "access_key_id": ICP.get_param("gohan.extraction_access_key_id") or "",
            "secret_access_key": ICP.get_param("gohan.extraction_secret_access_key") or "",
            "local_url": (ICP.get_param("gohan.lambda_local_url") or "").strip(),
            "batch_concurrency": int(
                ICP.get_param("gohan.batch_concurrency") or _BATCH_FANOUT_POOL_SIZE
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
                "(Settings -> Gohan -> Lambda Function), "
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
        if to_generate:
            to_generate.write(dict(_common, state="generating"))
            gen_ids = to_generate.ids
            _logger.info(
                "[gohan] batch: %d job(s) already extracted -> PRD generation: %s",
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
                "[gohan] batch: %d job(s) dispatching to extraction Lambda",
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
            "Batch fan-out: %d records, max_workers=%d, function=%s, region=%s",
            len(record_ids), max_workers, config["function_name"], config["region"],
        )

        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="gohan-fanout",
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
            "[gohan][job=%s] discarded by %s", self.name, self.env.user.name,
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
            "[gohan][job=%s] reopened from discarded by %s",
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
                "[gohan][job=%s] retry: prd_prompt present — skipping extraction, "
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
            "[gohan][job=%s] retry: no prd_prompt — resetting to draft for "
            "fresh extraction", self.name,
        )
        self.write({
            "state": "draft",
            "score": False,
            "grade": False,
            "qc_verdict": False,
            "prd_text": False,
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
                "function_name": ICP.get_param("gohan.lambda_function_name"),
                "region": ICP.get_param("gohan.lambda_region") or "ap-south-1",
                "access_key_id": ICP.get_param("gohan.extraction_access_key_id") or "",
                "secret_access_key": ICP.get_param("gohan.extraction_secret_access_key") or "",
                "batch_concurrency": int(
                    ICP.get_param("gohan.batch_concurrency") or _BATCH_FANOUT_POOL_SIZE
                ),
            }
            if not config["function_name"]:
                raise UserError(
                    "Lambda function name not configured "
                    "(Settings -> Gohan -> Lambda Function). Cannot retry "
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
            "[gohan] retry-failed batch by %s: %s",
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
                "qc_report": False,
                "score_report_json": False,
                "prd_url": False,
                "llm_attempts": 0,
                "duration_seconds": False,
                "error_message": False,
                "started_at": fields.Datetime.now(),
                "completed_at": False,
                "last_heartbeat": fields.Datetime.now(),
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
        wizard = self.env["gohan.rerun.wizard"].create({"job_id": self.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Rerun Pipeline",
            "res_model": "gohan.rerun.wizard",
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
            "qc_report": False,
            "score_report_json": False,
            "prd_url": False,
            "llm_attempts": 0,
            "error_message": False,
            "last_heartbeat": fields.Datetime.now(),
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

    # ------------------------------------------------------------------
    # Extraction review gate
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_asset_key(key):
        """Map an S3 asset key to a ``gohan.job.asset`` asset_type."""
        low = (key or "").lower()
        if low.endswith(".svg"):
            return "svg"
        if "/fonts/" in low or low.endswith(
            (".woff", ".woff2", ".ttf", ".otf", ".eot")
        ):
            return "font"
        if low.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico")
        ):
            return "image"
        return "other"

    def _materialize_assets(self, screenshot_keys=None, asset_keys=None,
                            svg_manifest=None):
        """(Re)build ``gohan.job.asset`` rows from raw Lambda extraction output.

        Called by the extraction webhook before an interactive job parks in
        the ``extracted`` review state. Operator-uploaded rows
        (``source='uploaded'``) are preserved; previously extracted rows are
        dropped and rebuilt so a re-extraction reflects the latest output.
        """
        self.ensure_one()
        screenshot_keys = screenshot_keys or []
        asset_keys = asset_keys or []

        # SVG markup + copyright flag keyed by filename, from the Lambda's
        # copyright-safe icon manifest (optional — absent on older Lambdas).
        svg_by_name = {}
        for entry in (svg_manifest or {}).get("kept", []):
            fname = (entry.get("filename") or "").strip()
            if fname:
                svg_by_name[fname] = entry

        # Drop only the previously extracted rows — keep operator uploads.
        self.asset_ids.filtered(lambda a: a.source == "extracted").unlink()

        rows = []
        seq = 10
        for key in screenshot_keys:
            rows.append({
                "job_id": self.id,
                "sequence": seq,
                "name": key.rsplit("/", 1)[-1] or key,
                "asset_type": (
                    "section_snapshot" if "/sections/" in key else "screenshot"
                ),
                "source": "extracted",
                "s3_key": key,
                "include_in_prd": True,
            })
            seq += 10
        for key in asset_keys:
            atype = self._classify_asset_key(key)
            fname = key.rsplit("/", 1)[-1] or key
            vals = {
                "job_id": self.id,
                "sequence": seq,
                "name": fname,
                "asset_type": atype,
                "source": "extracted",
                "s3_key": key,
                # Fonts are review-only context — never auto-fed to the PRD.
                "include_in_prd": atype != "font",
            }
            if atype == "svg" and fname in svg_by_name:
                entry = svg_by_name[fname]
                vals["content_text"] = entry.get("markup") or ""
                vals["is_copyright_safe"] = bool(entry.get("copyright_safe"))
                vals["mime_type"] = "image/svg+xml"
            rows.append(vals)
            seq += 10
        if rows:
            self.env["gohan.job.asset"].create(rows)
        return self.asset_ids

    def _upload_pending_assets_to_s3(self, assets):
        """Push operator-uploaded asset files (no S3 key yet) up to S3 so the
        PRD pipeline, which reads from S3 keys, can feed them to Bedrock."""
        import base64 as _b64
        pending = assets.filtered(lambda a: not a.s3_key and a.file_data)
        if not pending:
            return
        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("gohan.s3_bucket")
        if not bucket:
            _logger.warning(
                "[gohan][job=%s] %d uploaded asset(s) will be skipped by PRD "
                "generation — gohan.s3_bucket is not configured",
                self.name, len(pending),
            )
            return
        from ..services.s3_service import _get_s3_client
        client = _get_s3_client(
            ICP.get_param("gohan.s3_access_key_id"),
            ICP.get_param("gohan.s3_secret_access_key"),
            ICP.get_param("gohan.s3_region") or "us-east-1",
        )
        folder = ICP.get_param("gohan.s3_folder") or "gohan"
        for asset in pending:
            raw = asset.file_data
            try:
                data = (
                    _b64.b64decode(raw)
                    if isinstance(raw, (bytes, str)) else raw
                )
            except Exception:
                _logger.exception(
                    "[gohan][job=%s] could not decode uploaded asset %s",
                    self.name, asset.name,
                )
                continue
            fname = asset.file_name or asset.name or f"asset_{asset.id}"
            key = f"{folder}/{self.id}/uploaded/{asset.id}_{fname}"
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=data,
                    ContentType=asset.mime_type or "application/octet-stream",
                )
            except Exception:
                _logger.exception(
                    "[gohan][job=%s] S3 upload failed for asset %s",
                    self.name, asset.name,
                )
                continue
            asset.s3_key = key

    def _hydrate_svg_markup_from_s3(self):
        """Fetch inline markup for kept SVG icons that arrived as bare S3
        keys (no manifest), so PRD generation can feed them to Bedrock."""
        self.ensure_one()
        pending = self.asset_ids.filtered(
            lambda a: a.asset_type == "svg" and a.include_in_prd
            and a.s3_key and not a.content_text
        )
        if not pending:
            return
        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("gohan.s3_bucket")
        if not bucket:
            return
        from ..services.s3_service import download_file_from_s3
        for asset in pending:
            try:
                raw = download_file_from_s3(
                    key=asset.s3_key,
                    bucket=bucket,
                    access_key_id=ICP.get_param("gohan.s3_access_key_id"),
                    secret_key=ICP.get_param("gohan.s3_secret_access_key"),
                    region=ICP.get_param("gohan.s3_region") or "us-east-1",
                )
                text = (
                    raw.decode("utf-8", "ignore")
                    if isinstance(raw, (bytes, bytearray)) else raw
                )
                if text and "<svg" in text.lower():
                    # write() sanitises the markup before storing.
                    asset.content_text = text
            except Exception:
                _logger.warning(
                    "[gohan][job=%s] could not fetch SVG markup from S3 "
                    "for %s", self.name, asset.s3_key, exc_info=True,
                )

    def _sync_assets_to_keys(self):
        """Rebuild ``screenshot_keys`` / ``asset_keys`` from the curated rows.

        Run at Generate-PRD time so the tasker's review decisions are what
        the PRD pipeline actually consumes: unticked or deleted rows are
        excluded, and operator-uploaded files are pushed to S3 first so the
        S3-key-based pipeline can feed them to Bedrock.
        """
        self.ensure_one()
        included = self.asset_ids.filtered("include_in_prd")
        self._upload_pending_assets_to_s3(included)
        screenshot_keys, asset_keys = [], []
        for asset in included:
            if not asset.s3_key:
                continue
            if asset.asset_type in ("screenshot", "section_snapshot", "image"):
                screenshot_keys.append(asset.s3_key)
            else:
                asset_keys.append(asset.s3_key)
        self.write({
            "screenshot_keys": screenshot_keys,
            "asset_keys": asset_keys,
        })

    def action_generate_prd(self):
        """Manual review gate. The tasker has reviewed the extracted assets
        and is now triggering PRD generation. Only valid from ``extracted``.
        """
        self.ensure_one()
        if self.state != "extracted":
            raise UserError(
                "Generate PRD is only available while the job is in the "
                "'Extracted — Review' state."
            )
        if not self.prd_prompt and not self.screenshot_keys and not self.asset_keys:
            raise UserError(
                "No extraction data is available to generate a PRD from."
            )
        # The tasker's keep/delete/upload decisions become what the PRD
        # pipeline consumes.
        self._hydrate_svg_markup_from_s3()
        self._sync_assets_to_keys()
        self.write({
            "state": "generating",
            "last_heartbeat": fields.Datetime.now(),
            "error_message": False,
        })

        db_name = self.env.cr.dbname
        record_id = self.id
        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                f"prd-gen[job={record_id}]",
                self._run_prd_generation_bg, db_name, record_id,
            )
        )

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
        from ..services.bedrock_service import get_credentials as _get_bedrock_credentials
        from ..services.qc_service import run_qc

        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return

                ICP = env["ir.config_parameter"].sudo()
                bedrock_access_key, bedrock_secret_key = _get_bedrock_credentials(env)
                config = {
                    "inference_arn": ICP.get_param("gohan.bedrock_inference_arn"),
                    "region": ICP.get_param("gohan.bedrock_region") or "us-east-1",
                    "bedrock_access_key": bedrock_access_key,
                    "bedrock_secret_key": bedrock_secret_key,
                    "s3_bucket": ICP.get_param("gohan.s3_bucket"),
                    "s3_key_id": ICP.get_param("gohan.s3_access_key_id"),
                    "s3_secret": ICP.get_param("gohan.s3_secret_access_key"),
                    "s3_region": ICP.get_param("gohan.s3_region"),
                }
                job_data = {
                    "prd_text": record.prd_text,
                    "category_name": record.category_id.name if record.category_id else "Uncategorized",
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

        except Exception as exc:
            _logger.exception("QC rerun failed for job %s", record_id)
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
                "bucket": ICP.get_param("gohan.s3_bucket"),
                "key_id": ICP.get_param("gohan.s3_access_key_id"),
                "secret": ICP.get_param("gohan.s3_secret_access_key"),
                "region": ICP.get_param("gohan.s3_region") or "us-east-1",
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
                        # SVG icons ship in the deliverable as PNG, not SVG.
                        if rel_path.lower().endswith(".svg"):
                            png = _svg_to_png(data)
                            if png is not None:
                                data = png
                                rel_path = rel_path[:-4] + ".png"
                            else:
                                download_errors.append(
                                    f"{key}: SVG->PNG conversion unavailable "
                                    f"(install cairosvg) -- kept as .svg"
                                )
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
        # Concurrent S3 poller fires the bus event the UI listens to even
        # when the Lambda webhook never arrives. See the long comment
        # above _run_extraction_poll_bg for the full architectural
        # rationale — do not remove without understanding it.
        self.env.cr.postcommit.add(
            lambda: _submit_bg(
                f"extract-poll[job={record_id}]",
                self._run_extraction_poll_bg, db_name, record_id,
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify_state_change(self, state):
        """Send bus notification for state change (works from ORM context)."""
        try:
            self.env["bus.bus"]._sendone(
                "gohan_job_updates",
                "gohan/job_state",
                {"id": self.id, "state": state},
            )
        except Exception:
            pass

    def _mark_failed(self, error_msg):
        """Mark task as failed."""
        self.write({
            "state": "failed",
            "error_message": self._friendly_pipeline_error(error_msg)[:500],
            "completed_at": fields.Datetime.now(),
        })
        self._notify_state_change("failed")

    def _is_cancelled(self, db_name, record_id):
        """Check if a task has been cancelled (safe for background threads)."""
        try:
            with Registry(db_name).cursor() as cr:
                cr.execute(
                    "SELECT cancel_requested FROM gohan_job WHERE id = %s",
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
        url = f"{base_url}/api/v1/gohan/webhook/extraction-complete"
        # [gohan][diag] Lambda runs in AWS — it CANNOT reach 127.0.0.1 / localhost
        # / 10.x / 192.168.x of your dev box. If web.base.url is one of those,
        # the callback will never arrive and the job hangs in 'extracting'.
        if any(host in base_url for host in (
            "localhost", "127.0.0.1", "0.0.0.0",
        )) or base_url.startswith("http://10.") or base_url.startswith("http://192.168."):
            _logger.warning(
                "[gohan][diag] web.base.url=%s is NOT reachable from AWS Lambda. "
                "Set Settings -> Technical -> Parameters -> System Parameters "
                "'web.base.url' to a publicly reachable URL (ngrok / public ALB).",
                base_url,
            )
        else:
            _logger.info("[gohan][diag] callback_url resolved to: %s", url)
        return url

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
                    "function_name": ICP.get_param("gohan.lambda_function_name"),
                    "region": ICP.get_param("gohan.lambda_region") or "ap-south-1",
                    "access_key_id": ICP.get_param("gohan.extraction_access_key_id") or "",
                    "secret_access_key": ICP.get_param("gohan.extraction_secret_access_key") or "",
                    "local_url": (ICP.get_param("gohan.lambda_local_url") or "").strip(),
                }
                job_data = {
                    "url": record.url,
                    "callback_url": record._get_webhook_url(),
                }
                cr.commit()

            self._append_pipeline_event(
                db_name, record_id,
                step="extraction.invoke",
                status="started",
                message=f"Dispatching extraction Lambda for {job_data['url']}",
                function_name=config["function_name"] or "<UNSET>",
                region=config["region"],
                credentials="explicit" if config["access_key_id"] else "pod-role/instance",
                callback_url=job_data["callback_url"],
                local_url=config["local_url"] or None,
            )

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

            _logger.info(
                "[gohan][diag][job=%s] trigger_extraction returned: %s",
                record_id, result,
            )

            run_meta_vals = {}
            request_id = result.get("request_id") if isinstance(result, dict) else None
            if request_id:
                run_meta_vals["lambda_request_id"] = request_id
            with Registry(db_name).cursor() as meta_cr:
                meta_env = api.Environment(meta_cr, SUPERUSER_ID, {})
                meta_icp = meta_env["ir.config_parameter"].sudo()
                meta_bucket = (meta_icp.get_param("gohan.s3_bucket") or "").strip()
                meta_folder = (meta_icp.get_param("gohan.s3_folder") or "gohan").strip("/")
                if meta_bucket:
                    run_meta_vals["s3_artifact_prefix"] = (
                        f"s3://{meta_bucket}/{meta_folder}/{record_id}/"
                    )
                if run_meta_vals:
                    meta_env[self._name].browse(record_id).write(run_meta_vals)
                    meta_cr.commit()
                    self._append_pipeline_event(
                        db_name, record_id,
                        step="extraction.run_metadata",
                        status="ok",
                        message="Run Metadata persisted (AWS Request ID + S3 Artifact Prefix)",
                        lambda_request_id=run_meta_vals.get("lambda_request_id"),
                        s3_artifact_prefix=run_meta_vals.get("s3_artifact_prefix"),
                    )

            if not result.get("success"):
                error_msg = result.get("error", "Extraction Lambda invoke failed")
                self._append_pipeline_event(
                    db_name, record_id,
                    step="extraction.invoke",
                    status="error",
                    message=error_msg,
                )
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": error_msg[:500],
                    "completed_at": fields.Datetime.now(),
                })
            else:
                self._append_pipeline_event(
                    db_name, record_id,
                    step="extraction.invoke",
                    status="ok",
                    message="Lambda accepted invoke; waiting for callback",
                    request_id=request_id,
                )

        except Exception as exc:
            _logger.exception(
                "Extraction background task failed for job %s", record_id
            )
            friendly = self._friendly_pipeline_error(exc)
            self._append_pipeline_event(
                db_name, record_id,
                step="extraction.invoke",
                status="error",
                message=f"Background task crashed: {friendly}",
            )
            try:
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": friendly[:500],
                    "completed_at": fields.Datetime.now(),
                })
            except Exception:
                _logger.error("Failed to mark job %s as failed", record_id)

    # ------------------------------------------------------------------
    # Background: Active S3 poller (Talos-parity auto-update)
    # ------------------------------------------------------------------
    #
    # Why this exists (read before touching):
    #
    # The Lambda webhook is fast when it works but fragile by nature: a
    # network glitch, a stale signature config, a cold-start crash before
    # the callback, an AWS service blip — any of these silently strand a
    # job in 'extracting' until the 10-minute reconcile cron rescues it.
    # The UI banner promises "page will update automatically" — a promise
    # that's only true within seconds when the webhook fires, otherwise
    # measured in minutes.
    #
    # This poller closes that gap. It runs concurrently with the Lambda
    # invocation, probing s3://.../raw_data.json every few seconds. As
    # soon as that file appears (Lambda finished, partially or fully),
    # it calls _attempt_s3_rescue (same code path the cron and the
    # manual button use), which populates every field, advances state,
    # AND broadcasts the bus event the JS form-view listener already
    # subscribes to (see static/src/js/gohan_bus.js).
    #
    # Result: even with ZERO webhook delivery, the user sees the form
    # auto-reload within ~5 seconds of S3 having data. The webhook
    # becomes an optimization, not a single point of failure. This is
    # the Talos / Kinesis active-update pattern adapted for an
    # out-of-process worker (Lambda instead of in-process API calls).
    #
    # Race-safe: if the webhook commits first, the next poller iteration
    # sees state != 'extracting' and exits cleanly without double-work.

    def _run_extraction_poll_bg(self, db_name, record_id):
        """Active S3 poll loop that fires bus.bus events for UI auto-update.

        Runs alongside ``_run_extraction_bg``. Reads cadence from:
            - ``gohan.s3_poll_interval_seconds`` (default 5, min 2)
            - ``gohan.s3_poll_max_seconds`` (default 900, min 60)

        Each iteration opens its own cursor, browses the record, and
        either (a) exits because state has already advanced (webhook
        beat us), or (b) calls ``_attempt_s3_rescue`` which writes
        fields, advances state, broadcasts the bus event, and submits
        the next-stage BG worker.

        Talos-pattern serialization retry (3 attempts, 0.5..2.5s
        backoff) protects against PostgreSQL row-lock conflicts when
        the webhook handler holds the row.
        """
        try:
            with Registry(db_name).cursor() as cfg_cr:
                cfg_env = api.Environment(cfg_cr, SUPERUSER_ID, {})
                ICP = cfg_env["ir.config_parameter"].sudo()
                try:
                    interval = max(2, int(
                        ICP.get_param("gohan.s3_poll_interval_seconds") or "5"
                    ))
                except (TypeError, ValueError):
                    interval = 5
                try:
                    max_seconds = max(60, int(
                        ICP.get_param("gohan.s3_poll_max_seconds") or "900"
                    ))
                except (TypeError, ValueError):
                    max_seconds = 900

            deadline = time.monotonic() + max_seconds
            # Cold-start grace: Lambda must at least START before there's
            # anything in S3 to find. ~5s avoids a useless first probe.
            time.sleep(min(interval, 5))

            while time.monotonic() < deadline:
                rescued = False
                stopped = False
                for attempt in range(3):
                    try:
                        with Registry(db_name).cursor() as cr:
                            env = api.Environment(cr, SUPERUSER_ID, {})
                            record = env["gohan.job"].browse(record_id)
                            if not record.exists():
                                stopped = True
                                break
                            if record.state != "extracting":
                                # Webhook already advanced state, OR job
                                # was cancelled / marked failed elsewhere.
                                stopped = True
                                break
                            rescued, reason = record._attempt_s3_rescue(
                                source="poller"
                            )
                            cr.commit()
                            if rescued:
                                _logger.info(
                                    "[gohan][poller][job=%s] rescued: %s",
                                    record_id, reason,
                                )
                            else:
                                _logger.debug(
                                    "[gohan][poller][job=%s] no data yet: %s",
                                    record_id, reason,
                                )
                        break  # successful pass through ORM block
                    except Exception as exc:
                        msg = str(exc).lower()
                        if ("serialize" in msg or "could not serialize" in msg) and attempt < 2:
                            time.sleep(0.5 + attempt)
                            continue
                        _logger.exception(
                            "[gohan][poller][job=%s] iteration failed",
                            record_id,
                        )
                        break

                if rescued or stopped:
                    return

                time.sleep(interval)

            _logger.info(
                "[gohan][poller][job=%s] gave up after %ss; "
                "watchdog/reconcile cron will take over",
                record_id, max_seconds,
            )
        except Exception:
            _logger.exception(
                "[gohan][poller][job=%s] worker crashed", record_id,
            )

    # ------------------------------------------------------------------
    # Background: PRD Generation
    # ------------------------------------------------------------------

    def _upload_artifacts_bg(self, db_name, record_id, artifacts, s3_config):
        """Background: upload extraction artifacts to S3 and write the
        resulting ``artifacts_url`` back to the record.

        Ported from ``leviathan_job._upload_artifacts_bg``. Decouples the
        multi-MB S3 upload from the webhook handler so the webhook returns
        in <50 ms regardless of artifact count. PRD generation does not
        depend on ``artifacts_url`` -- it reads ``prd_prompt`` /
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
                "[gohan][job=%s] artifacts upload: could not read record",
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
                "[gohan][job=%s] artifacts S3 upload failed -- tasker can "
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
                            "gohan_job_updates",
                            "gohan/job_state",
                            {
                                "id": record_id,
                                "artifacts_url": artifacts_url,
                                "state": record.state,
                            },
                        )
                    except Exception:
                        pass
                    cr.commit()
        except Exception:
            _logger.exception(
                "[gohan][job=%s] artifacts upload: succeeded on S3 but "
                "writeback failed -- artifacts_url will be blank",
                record_id,
            )

    def _run_prd_generation_bg(self, db_name, record_id):
        """Background: generate PRD via Bedrock, score, iterate, QC."""
        from ..services.bedrock_service import generate_prd, get_credentials as _get_bedrock_credentials
        from ..services.scoring_service import score_prd
        from ..services.s3_service import upload_prd_to_s3

        try:
            # === PHASE 1: Read config and extraction data ===
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return

                ICP = env["ir.config_parameter"].sudo()
                bedrock_access_key, bedrock_secret_key = _get_bedrock_credentials(env)
                _include_ss_raw = ICP.get_param("gohan.prd_include_screenshots", "True")
                _include_ss = str(_include_ss_raw).strip().lower() not in ("false", "0", "no", "off", "")
                config = {
                    "inference_arn": ICP.get_param("gohan.bedrock_inference_arn"),
                    "region": ICP.get_param("gohan.bedrock_region") or "us-east-1",
                    # Single-pass PRD generation by default: one Bedrock call,
                    # no score-refinement loop. The retry loop inflated length
                    # (each rewrite chased the density score); a single pass
                    # follows the prompt's under-1000-word target directly.
                    # Still overridable via the gohan.max_llm_attempts sysparam.
                    "max_attempts": int(ICP.get_param("gohan.max_llm_attempts") or 1),
                    "bedrock_access_key": bedrock_access_key,
                    "bedrock_secret_key": bedrock_secret_key,
                    "s3_bucket": ICP.get_param("gohan.s3_bucket"),
                    "s3_key_id": ICP.get_param("gohan.s3_access_key_id"),
                    "s3_secret": ICP.get_param("gohan.s3_secret_access_key"),
                    "s3_region": ICP.get_param("gohan.s3_region"),
                    "s3_folder": ICP.get_param("gohan.s3_folder") or "gohan",
                    "cdn_url": ICP.get_param("gohan.s3_cdn_url"),
                    "include_screenshots": _include_ss,
                }
                job_data = {
                    "name": record.name,
                    "prd_prompt": record.prd_prompt,
                    "category_name": record.category_id.name if record.category_id else "Uncategorized",
                    "url": record.url,
                    "site_discovery_json": record.site_discovery_json,
                    "user_id": record.user_id.id if record.user_id else False,
                    "partner_id": record.user_id.partner_id.id if record.user_id and record.user_id.partner_id else False,
                    "screenshot_keys": record.screenshot_keys or [],
                    "asset_keys": record.asset_keys or [],
                    # Tasker-approved SVG icons, fed to Bedrock as inline
                    # markup. Empty when the review gate was skipped.
                    "svg_icons": [
                        {"name": a.name, "markup": a.svg_markup()}
                        for a in record.asset_ids.filtered(
                            lambda x: x.include_in_prd and x.asset_type == "svg"
                        )
                        if a.svg_markup()
                    ],
                }

                prd_system_prompt = record._get_prd_system_prompt()
                qc_system_prompt = record._get_qc_system_prompt()

                if not config["inference_arn"]:
                    record.write({
                        "state": "failed",
                        "error_message": "Bedrock inference ARN not configured",
                        "completed_at": fields.Datetime.now(),
                    })
                    cr.commit()
                    self._append_pipeline_event(
                        db_name, record_id,
                        step="prd.config",
                        status="error",
                        message="Bedrock inference ARN not configured (check Gohan settings)",
                    )
                    return
                if not job_data["prd_prompt"]:
                    record.write({
                        "state": "failed",
                        "error_message": "No extraction data available for PRD generation",
                        "completed_at": fields.Datetime.now(),
                    })
                    cr.commit()
                    self._append_pipeline_event(
                        db_name, record_id,
                        step="prd.config",
                        status="error",
                        message="No extraction data available — extraction Lambda never produced a prompt",
                    )
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

            self._append_pipeline_event(
                db_name, record_id,
                step="prd.config",
                status="ok",
                message="Configuration loaded; entering PRD generation",
                max_attempts=config["max_attempts"],
                inference_arn=config["inference_arn"],
                region=config["region"],
                category=job_data["category_name"],
                screenshot_count=len(job_data["screenshot_keys"]),
            )

            # === PHASE 2: LLM generation loop ===
            # Download screenshots from S3 for vision (shared by PRD gen + QC)
            # Bedrock limit: 3.75MB per image, 25MB total. Resize to keep fast.
            # Operators can disable visual input entirely via
            # `gohan.prd_include_screenshots` sysparam (default True). Set to
            # False to send text-only — useful when iterating on the textual
            # extraction quality without burning vision tokens.
            screenshot_blocks = []
            if config["include_screenshots"] and job_data["screenshot_keys"] and config["s3_bucket"]:
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
                        self._append_pipeline_event(
                            db_name, record_id,
                            step="prd.screenshots.download",
                            status="warn",
                            message=f"Failed to download screenshot {key}: {img_exc}",
                            key=key,
                        )
                self._append_pipeline_event(
                    db_name, record_id,
                    step="prd.screenshots.download",
                    status="ok",
                    message=f"Attached {len(screenshot_blocks)}/{len(job_data['screenshot_keys'])} screenshots ({total_bytes / 1_000_000:.1f} MB)",
                    attached=len(screenshot_blocks),
                    available=len(job_data["screenshot_keys"]),
                    total_bytes=total_bytes,
                )

            # Build multimodal content: screenshots + extraction text
            content_blocks = list(screenshot_blocks)
            content_blocks.append({"text": (
                f"Below is the extracted website data. "
                f"Write the complete PRD following all rules.\n\n"
                f"---\n\n{job_data['prd_prompt']}"
            )})
            # Tasker-curated SVG UI icons — fed as inline markup so the model
            # can reference the product's real iconography.
            if job_data.get("svg_icons"):
                icon_md = "\n\n".join(
                    f"### {ic['name']}\n```svg\n{ic['markup']}\n```"
                    for ic in job_data["svg_icons"]
                )
                content_blocks.append({"text": (
                    "\n\n---\n\n## EXTRACTED UI ICONS (SVG)\n\n"
                    "The following SVG icons were captured from the site and "
                    "approved by the reviewer:\n\n" + icon_md
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

            for attempt in range(1, config["max_attempts"] + 1):
                if self._is_cancelled(db_name, record_id):
                    self._append_pipeline_event(
                        db_name, record_id,
                        step=f"prd.attempt.{attempt}.generate",
                        status="cancelled",
                        message="Operator cancelled the job mid-generation",
                    )
                    self._write_with_cursor(db_name, record_id, {
                        "state": "draft", "error_message": "Cancelled during generation",
                        "completed_at": fields.Datetime.now(),
                    })
                    return

                self._write_with_cursor(db_name, record_id, {
                    "last_heartbeat": fields.Datetime.now(),
                })

                self._append_pipeline_event(
                    db_name, record_id,
                    step=f"prd.attempt.{attempt}.generate",
                    status="started",
                    message=f"Calling Bedrock for PRD attempt {attempt}/{config['max_attempts']}",
                    attempt=attempt,
                    max_attempts=config["max_attempts"],
                )

                try:
                    prd_text = generate_prd(
                        inference_arn=config["inference_arn"],
                        region=config["region"],
                        system_prompt=prd_system_prompt,
                        messages=messages,
                        access_key_id=config["bedrock_access_key"],
                        secret_access_key=config["bedrock_secret_key"],
                    )
                except Exception as gen_exc:
                    _logger.warning(
                        "LLM attempt %d/%d failed for job %s: %s",
                        attempt, config["max_attempts"], job_data["name"], gen_exc,
                    )
                    self._append_pipeline_event(
                        db_name, record_id,
                        step=f"prd.attempt.{attempt}.generate",
                        status="error" if attempt == config["max_attempts"] else "warn",
                        message=f"Bedrock call failed: {gen_exc}",
                        attempt=attempt,
                    )
                    if attempt == config["max_attempts"]:
                        raise
                    time.sleep(2 * attempt)
                    continue

                self._append_pipeline_event(
                    db_name, record_id,
                    step=f"prd.attempt.{attempt}.generate",
                    status="ok",
                    message=f"PRD attempt {attempt} produced {len(prd_text or '')} chars",
                    attempt=attempt,
                    chars=len(prd_text or ""),
                )

                score_report = score_prd(
                    prd_text=prd_text,
                    category=job_data["category_name"],
                )
                total_score = score_report["total_score"]

                self._append_pipeline_event(
                    db_name, record_id,
                    step=f"prd.attempt.{attempt}.score",
                    status="ok",
                    message=f"Score = {total_score} / grade = {score_report.get('grade')}",
                    attempt=attempt,
                    score=total_score,
                    grade=score_report.get("grade"),
                )

                self._write_with_cursor(db_name, record_id, {
                    "llm_attempts": attempt,
                })

                llm_trace["attempts"].append({
                    "attempt": attempt,
                    "prd_text": prd_text,
                    "score": total_score,
                    "grade": score_report.get("grade"),
                    "score_report": score_report,
                })

                # Keep the highest scorer; always keep *something* so a run that
                # only produces rejected PRDs (score 0) still saves a PRD for the
                # tasker to edit instead of crashing on upload with prd_text=None.
                if best_prd_text is None or total_score > best_score:
                    best_prd_text = prd_text
                    best_score = total_score
                    best_grade = score_report["grade"]
                    best_score_report = score_report

                if attempt < config["max_attempts"]:
                    messages.append({"role": "assistant", "content": prd_text})
                    feedback = self._build_feedback(score_report)
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Score: {total_score} (attempt {attempt})\n"
                            f"Feedback:\n{feedback}\n\n"
                            "Fix all issues and rewrite the complete PRD."
                        ),
                    })

            # Upload to S3
            try:
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
                self._append_pipeline_event(
                    db_name, record_id,
                    step="prd.s3_upload",
                    status="ok",
                    message=f"PRD uploaded to S3",
                    prd_url=prd_url,
                )
            except Exception as up_exc:
                self._append_pipeline_event(
                    db_name, record_id,
                    step="prd.s3_upload",
                    status="error",
                    message=f"S3 upload failed: {up_exc}",
                )
                raise

            # === PHASE 3: QC ===
            # Pulse the heartbeat on entry. QC can be a multi-minute Bedrock
            # call; without this pulse the gap from the last PRD-gen attempt
            # to PHASE 4's final write was fully unmonitored — long QC calls
            # could trip the watchdog while doing real work.
            self._write_with_cursor(db_name, record_id, {
                "state": "scoring",
                "last_heartbeat": fields.Datetime.now(),
            })
            self._append_pipeline_event(
                db_name, record_id,
                step="qc.evaluate",
                status="started",
                message="Running Bedrock QC evaluation on best PRD",
            )

            qc_verdict = "not_shippable"
            qc_report = ""
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
                self._append_pipeline_event(
                    db_name, record_id,
                    step="qc.evaluate",
                    status="ok",
                    message=f"QC verdict: {qc_verdict}",
                    verdict=qc_verdict,
                )
            except Exception as qc_exc:
                _logger.warning(
                    "QC failed for job %s: %s (fail-closed: not_shippable)",
                    job_data["name"], qc_exc,
                )
                qc_verdict = "not_shippable"
                qc_report = f"QC evaluation failed: {qc_exc}\n\nVerdict defaulted to NOT SHIPPABLE (fail-closed policy)."
                self._append_pipeline_event(
                    db_name, record_id,
                    step="qc.evaluate",
                    status="error",
                    message=f"QC evaluation crashed (fail-closed): {qc_exc}",
                )

            llm_trace["qc"] = {
                "qc_system_prompt": qc_system_prompt,
                "verdict": qc_verdict,
                "report": qc_report,
            }

            # === PHASE 4: Write final results ===
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)

                started = record.started_at
                duration = (
                    (fields.Datetime.now() - started).total_seconds()
                    if started else 0
                )

                record.write({
                    "state": "done",
                    "prd_text": best_prd_text,
                    "score": best_score,
                    "grade": best_grade,
                    "score_report_json": best_score_report,
                    "prd_url": prd_url,
                    "qc_verdict": qc_verdict,
                    "qc_report": qc_report,
                    "llm_trace_json": llm_trace,
                    "completed_at": fields.Datetime.now(),
                    "duration_seconds": duration,
                })

                try:
                    env["bus.bus"]._sendone(
                        "gohan_job_updates",
                        "gohan/job_done",
                        {"id": record_id, "name": job_data["name"]},
                    )
                except Exception:
                    _logger.debug("bus.bus notification failed for job %s (non-fatal)", record_id)

                if record.via_batch:
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

            self._append_pipeline_event(
                db_name, record_id,
                step="pipeline.complete",
                status="ok",
                message=f"Pipeline finished — score={best_score} grade={best_grade} verdict={qc_verdict}",
                score=best_score,
                grade=best_grade,
                qc_verdict=qc_verdict,
                duration_seconds=duration,
            )

        except Exception as exc:
            _logger.exception("[gohan][job=%s] PRD generation failed", record_id)
            # Translate psycopg2 / connection-layer faults to a friendlier
            # operator-facing message — the raw "Cursor already closed" or
            # "connection already closed" texts are infrastructure noise that
            # tell the user nothing actionable.
            friendly_msg = self._friendly_pipeline_error(exc)
            self._append_pipeline_event(
                db_name, record_id,
                step="pipeline.complete",
                status="error",
                message=f"PRD generation crashed: {friendly_msg}",
            )
            try:
                fail_vals = {
                    "state": "failed",
                    "error_message": friendly_msg[:500],
                    "completed_at": fields.Datetime.now(),
                }
                # Persist whatever LLM trace we accumulated before the failure.
                _trace = locals().get("llm_trace")
                if _trace:
                    fail_vals["llm_trace_json"] = _trace
                self._write_with_cursor(db_name, record_id, fail_vals)
            except Exception:
                _logger.error("[gohan][job=%s] failed to mark as failed", record_id)

    @staticmethod
    def _friendly_pipeline_error(exc):
        """Translate raw pipeline exceptions to operator-facing messages.

        Background threads occasionally surface psycopg2 connection-state
        errors ("cursor already closed", "connection already closed",
        "SSL connection has been closed unexpectedly") when Odoo or the DB
        side recycles a long-idle backend. These are transient and don't
        require a developer to triage — the operator just needs to click
        Retry. Mapping them to a clear message removes the support load.

        Falls back to ``str(exc)`` for anything we don't recognize so real
        bugs remain visible.
        """
        msg = str(exc) or exc.__class__.__name__
        low = msg.lower()
        connection_signals = (
            "cursor already closed",
            "connection already closed",
            "ssl connection has been closed",
            "server closed the connection unexpectedly",
            "interfaceerror",
        )
        if any(s in low for s in connection_signals):
            return (
                "Database session was lost during the pipeline run "
                "(transient DB connection drop). Click Retry to resume."
            )
        return msg

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _write_with_cursor(self, db_name, record_id, vals):
        """Write values to a record using a short-lived cursor.

        Resilient to transient connection drops: psycopg2 InterfaceError
        ("cursor already closed") and OperationalError ("connection
        reset", "SSL handshake failed", etc.) get one fast retry with a
        fresh cursor + connection. These come from the threadpool reaping
        a long-idle backend between pipeline steps — they are not bugs in
        the write itself and should not surface to the operator.
        """
        import psycopg2
        last_exc = None
        for attempt in range(_WRITE_CURSOR_RETRIES + 1):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    record = env[self._name].browse(record_id)
                    if record.exists():
                        record.write(vals)
                        if "state" in vals:
                            try:
                                env["bus.bus"]._sendone(
                                    "gohan_job_updates",
                                    "gohan/job_state",
                                    {"id": record_id, "state": vals["state"]},
                                )
                            except Exception:
                                pass
                    cr.commit()
                    return
            except (psycopg2.InterfaceError, psycopg2.OperationalError) as exc:
                last_exc = exc
                if attempt < _WRITE_CURSOR_RETRIES:
                    _logger.warning(
                        "[gohan][job=%s] _write_with_cursor transient error "
                        "(attempt %d/%d): %s — retrying with fresh cursor",
                        record_id, attempt + 1,
                        _WRITE_CURSOR_RETRIES + 1, exc,
                    )
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break
        # Out of retries — re-raise the last connection error so the caller's
        # outer except handler stores the (now-friendly) message.
        if last_exc is not None:
            raise last_exc

    def _append_pipeline_event(
        self, db_name, record_id, step, status, message=None, **extras
    ):
        """Append a single event to ``pipeline_log_json`` and mirror errors.

        This is the ONLY writer of ``pipeline_log_json``. The journal is
        append-only and is the single visible surface an operator uses to
        understand which step of a job is running, which one errored, and
        what the error was. Three behaviours are load-bearing:

        1. Opens its **own short-lived cursor** via ``Registry(db_name)``
           so it can be called safely from background threads (the PRD pool
           workers commit on a separate transaction from the caller's).
        2. **Never raises.** All work is wrapped in an outer try/except;
           a journaling failure must never crash the pipeline it is
           journaling.
        3. **Mirrors any ``status="error"`` event into ``error_message``**
           when that field is still empty. The form view's failure banner
           reads from ``error_message``, so without this mirror an early
           pipeline error would be visible only by clicking through to the
           journal tab — and most operators never get that far.
        """
        try:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            entry = {
                "ts": ts,
                "step": step,
                "status": status,
            }
            if message:
                entry["message"] = str(message)[:2000]
            for key, value in extras.items():
                if value is None:
                    continue
                try:
                    if isinstance(value, (str, int, float, bool, list, dict)):
                        entry[key] = value
                    else:
                        entry[key] = str(value)[:2000]
                except Exception:
                    continue

            log_method = _logger.warning if status == "error" else _logger.info
            log_method(
                "[gohan][pipeline][job=%s] %s %s%s",
                record_id, step, status,
                f" — {message}" if message else "",
            )

            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return
                existing = list(record.pipeline_log_json or [])
                existing.append(entry)
                update_vals = {"pipeline_log_json": existing}
                if status == "error" and not record.error_message and message:
                    update_vals["error_message"] = str(message)[:500]
                record.write(update_vals)
                cr.commit()
        except Exception:
            _logger.exception(
                "[gohan][pipeline][job=%s] journaling failed (non-fatal) for step=%s status=%s",
                record_id, step, status,
            )

    def _build_feedback(self, score_report):
        from ..services.scoring_service import (
            SECTION_MAX_POINTS, PRD_MAX_WORDS, PRD_MIN_WORDS,
        )

        lines = []

        # Word count leads the feedback: the 1500-word ceiling is a hard
        # auto-reject, and the model cannot count its own output reliably.
        # Hand it the measured count plus a concrete "cut N words, here"
        # instruction -- far more steerable than the terse "R5" trigger.
        word_count = score_report.get("details", {}).get("word_count", 0)
        if word_count > PRD_MAX_WORDS:
            over = word_count - PRD_MAX_WORDS
            lines.append(
                f"- WORD LIMIT EXCEEDED (auto-reject): PRD is {word_count} "
                f"words, {over} over the {PRD_MAX_WORDS}-word hard ceiling. "
                f"Rewrite the entire PRD shorter -- target under 1000 words. "
                f"Compress Section 6 (Application Logic) and Section 4 page "
                f"prose first; drop adjectives and any sentence that is not a "
                f"spec fact. Do not drop sections, hex codes, entities, "
                f"flows, easing values, or ARIA rules."
            )
        elif word_count and word_count < PRD_MIN_WORDS:
            short = PRD_MIN_WORDS - word_count
            lines.append(
                f"- BELOW WORD FLOOR: PRD is {word_count} words, {short} "
                f"under the {PRD_MIN_WORDS}-word minimum. Expand Section 4 "
                f"(more pages from the category feature list) and Section 6 "
                f"(more entity fields) until at least {PRD_MIN_WORDS} words."
            )

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
            # R4/R5 are the word-count triggers, already covered above with a
            # more actionable instruction -- skip to avoid a duplicate line.
            if trigger.startswith(("R4:", "R5:")):
                continue
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
            return

        ICP = self.env["ir.config_parameter"].sudo()
        extracting_threshold = int(
            ICP.get_param("gohan.watchdog_extracting_minutes", "60")
        )
        generating_threshold = int(
            ICP.get_param("gohan.watchdog_generating_minutes", "45")
        )

        try:
            stale_extracting = self.search([
                ("state", "=", "extracting"),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=extracting_threshold),
                ),
            ])
            for job in stale_extracting:
                rescued, reason = job._attempt_s3_rescue(source="watchdog")
                if rescued:
                    _logger.warning(
                        "[gohan][job=%s] watchdog: extracting >%dmin BUT S3 "
                        "raw_data.json found — auto-populated and advanced (%s)",
                        job.name, extracting_threshold, reason,
                    )
                    continue
                _logger.warning(
                    "[gohan][job=%s] watchdog: stuck in extracting >%dmin and "
                    "no S3 rescue (%s) — marking failed",
                    job.name, extracting_threshold, reason,
                )
                job._mark_failed(
                    f"Watchdog: extraction timed out "
                    f"(no response for {extracting_threshold}+ minutes; "
                    f"S3 rescue: {reason})"
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
            for job in stale_generating:
                _logger.warning(
                    "[gohan][job=%s] watchdog: stuck in %s >%dmin "
                    "(started_processing_at=%s, last_heartbeat=%s) — marking failed",
                    job.name, job.state, generating_threshold,
                    job.started_processing_at, job.last_heartbeat,
                )
                job._mark_failed(
                    f"Watchdog: {job.state} timed out "
                    f"(no progress for {generating_threshold}+ minutes)"
                )
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(987654321)")

    # ------------------------------------------------------------------
    # Spec-compliant API Gateway trigger (HANDOFF_ODOO.md §4.4)
    # ------------------------------------------------------------------

    def action_run_pipeline(self):
        """Trigger the pipeline via API Gateway HTTP POST (spec path).

        Alternative to ``action_run`` (which uses direct boto3 async invoke).
        Reads ``gohan.lambda_api_url`` + ``gohan.lambda_api_key`` from System
        Parameters and POSTs ``{run_id, url, category, resolution}`` to
        ``{api_url}/run`` with ``X-Api-Key`` auth.

        On non-200: job is marked ``failed`` with the API response captured in
        ``error_message``.  On 200: state advances to ``extracting`` — the
        Lambda subsequently calls back into ``/gohan/webhook`` (HMAC-signed)
        to finalize.
        """
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        api_url = ICP.get_param("gohan.lambda_api_url")
        api_key = ICP.get_param("gohan.lambda_api_key")
        if not api_url or not api_key:
            raise UserError(
                "Configure gohan.lambda_api_url and gohan.lambda_api_key in "
                "Settings → Gohan → API Gateway before using this action."
            )
        if not self.url:
            raise UserError("Please enter a website URL before running.")
        if self.state not in ("draft", "not_assigned"):
            raise UserError("Can only run tasks in Draft or Not Assigned state.")

        if self.state == "not_assigned" or not self.user_id:
            self.write({"user_id": self.env.uid, "state": "draft"})

        self.write({
            "state": "extracting",
            "started_at": fields.Datetime.now(),
            "started_processing_at": fields.Datetime.now(),
            "last_heartbeat": fields.Datetime.now(),
            "error_message": False,
        })

        category_code = self.category_id.code if self.category_id else ""
        payload = {
            "run_id": self.id,
            "url": self.url,
            "category": category_code,
            "resolution": self.target_resolution or "1920x1080",
        }
        endpoint = f"{api_url.rstrip('/')}/run"

        try:
            resp = requests.post(
                endpoint,
                json=payload,
                headers={
                    "X-Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            _logger.exception(
                "[gohan][job=%s] API Gateway POST %s failed", self.name, endpoint,
            )
            self._mark_failed(f"API Gateway unreachable: {exc}")
            return

        request_id = (
            resp.headers.get("x-amzn-RequestId")
            or resp.headers.get("X-Amzn-RequestId")
            or ""
        )
        if request_id:
            self.write({"lambda_request_id": request_id})

        if resp.status_code != 200:
            self._mark_failed(
                f"API Gateway returned {resp.status_code}: {resp.text[:500]}"
            )
            return

        _logger.info(
            "[gohan][job=%s] API Gateway accepted run; awaiting webhook callback "
            "(request_id=%s)", self.name, request_id or "n/a",
        )
        self._notify_state_change()

    # ------------------------------------------------------------------
    # S3 Rescue: shared by reconcile cron, watchdog, and manual button
    # ------------------------------------------------------------------

    @staticmethod
    def _synthesize_prd_prompt_from_raw_data(raw_data):
        """Build a minimal markdown prd_prompt from a ``raw_data.json`` bundle.

        Called when Lambda's webhook never landed and the bundle has no
        explicit ``prd_prompt`` key. Output feeds the PRD generation stage
        so a stuck job can recover end-to-end from S3 alone.
        """
        if not isinstance(raw_data, dict):
            return ""

        lines = ["# Auto-recovered extraction context", ""]

        site_discovery = raw_data.get("site_discovery") or {}
        if isinstance(site_discovery, dict):
            title = (
                site_discovery.get("title")
                or site_discovery.get("site_name")
                or site_discovery.get("url")
            )
            description = (
                site_discovery.get("description")
                or site_discovery.get("summary")
            )
            if title:
                lines.append(f"## Product: {title}")
            if description:
                lines.append(str(description))
                lines.append("")

        sitemap = raw_data.get("sitemap_taxonomy") or {}
        if isinstance(sitemap, dict):
            total_urls = len(sitemap.get("urls") or [])
            feature_pages = sitemap.get("feature_pages") or []
            integration_pages = sitemap.get("integration_pages") or []
            blog_pages = sitemap.get("blog_pages") or []
            if total_urls or feature_pages or integration_pages or blog_pages:
                lines.append("## Sitemap Taxonomy")
                if total_urls:
                    lines.append(f"- Total URLs: {total_urls}")
                if feature_pages:
                    lines.append(f"- Feature pages: {len(feature_pages)}")
                if integration_pages:
                    lines.append(f"- Integration pages: {len(integration_pages)}")
                if blog_pages:
                    lines.append(f"- Blog pages: {len(blog_pages)}")
                lines.append("")

        public_kb = raw_data.get("public_kb") or {}
        if isinstance(public_kb, dict) and public_kb:
            json_ld = public_kb.get("json_ld_blocks") or []
            lines.append("## Public Knowledge Base")
            if json_ld:
                lines.append(f"- Structured-data blocks: {len(json_ld)}")
            contact = public_kb.get("contact") or {}
            if contact:
                lines.append(f"- Contact info captured: {sorted(contact.keys())[:6]}")
            lines.append("")

        network = raw_data.get("network_summary") or {}
        if isinstance(network, dict) and network:
            lines.append("## Network Capture")
            lines.append(f"- Captured fields: {sorted(network.keys())[:10]}")
            lines.append("")

        api_doc = raw_data.get("api_doc_extracted") or {}
        if isinstance(api_doc, dict) and api_doc:
            lines.append("## API Documentation")
            endpoints = api_doc.get("endpoints") or []
            if endpoints:
                lines.append(f"- Endpoints documented: {len(endpoints)}")
            lines.append("")

        lines.append("---")
        lines.append(
            "_Recovered by Gohan reconciler from S3 raw_data.json — "
            "original Lambda prd_prompt was missing._"
        )
        return "\n".join(lines)

    def _populate_from_raw_data(self, raw_data, source="reconciler",
                                screenshot_keys=None, asset_keys=None,
                                artifacts_url=None):
        """Map an S3 ``raw_data.json`` bundle to ``gohan.job`` write vals.

        Single source of truth used by reconcile cron, watchdog rescue,
        and the manual "Reconcile from S3" button. Mirrors the field
        mapping in ``controllers/main.py`` webhook handler (lines 209-336)
        but adapted for S3-only recovery (no live ``data`` payload).

        Refuses to clobber populated fields — operators retain webhook
        wins when both sources have data.

        Returns: dict ready for ``self.write(...)``.
        """
        self.ensure_one()
        write_vals = {}

        site_discovery = raw_data.get("site_discovery") if isinstance(raw_data, dict) else None
        if isinstance(site_discovery, dict):
            if not self.site_discovery_json:
                write_vals["site_discovery_json"] = site_discovery
            title = (
                site_discovery.get("title")
                or site_discovery.get("site_name")
            )
            if title and not self.site_name:
                write_vals["site_name"] = str(title)[:255]
            tech_stack = site_discovery.get("tech_stack")
            if tech_stack and not self.tech_stack:
                if isinstance(tech_stack, (dict, list)):
                    write_vals["tech_stack"] = json.dumps(tech_stack)
                else:
                    write_vals["tech_stack"] = str(tech_stack)
            pages = site_discovery.get("pages")
            if isinstance(pages, list) and not self.page_count:
                write_vals["page_count"] = len(pages)

        existing_prd_prompt = self.prd_prompt
        bundled_prd_prompt = raw_data.get("prd_prompt") if isinstance(raw_data, dict) else None
        if not existing_prd_prompt:
            if bundled_prd_prompt:
                write_vals["prd_prompt"] = bundled_prd_prompt
            else:
                write_vals["prd_prompt"] = self._synthesize_prd_prompt_from_raw_data(raw_data)

        if screenshot_keys and not self.screenshot_keys:
            write_vals["screenshot_keys"] = screenshot_keys
        if asset_keys and not self.asset_keys:
            write_vals["asset_keys"] = asset_keys
        if artifacts_url and not self.artifacts_url:
            write_vals["artifacts_url"] = artifacts_url

        snapshot = {
            "_reconcile_source": source,
            "_reconcile_at": fields.Datetime.now().isoformat() + "Z",
            "raw_data": raw_data if isinstance(raw_data, dict) else {},
        }
        if screenshot_keys is not None:
            snapshot["screenshot_keys"] = list(screenshot_keys)[:200]
        if asset_keys is not None:
            snapshot["asset_keys"] = list(asset_keys)[:200]
        existing_callback = self.lambda_callback_json or {}
        if isinstance(existing_callback, dict):
            merged = dict(existing_callback)
            merged.update(snapshot)
            write_vals["lambda_callback_json"] = merged
        else:
            write_vals["lambda_callback_json"] = snapshot

        warnings = []
        if isinstance(site_discovery, dict) and not site_discovery.get("pages"):
            warnings.append(
                "site_discovery has no crawled pages — Lambda extraction "
                "appears incomplete"
            )
        expected_stages = {"site_discovery", "sitemap_taxonomy", "public_kb"}
        if isinstance(raw_data, dict):
            present = set(raw_data.keys())
            missing = expected_stages - present
            if missing:
                warnings.append(
                    f"Missing extraction stages: {', '.join(sorted(missing))}"
                )
        if warnings and not self.extraction_warnings:
            write_vals["extraction_warnings"] = (
                "\n".join(f"• {w}" for w in warnings)
            )[:1000]

        write_vals["last_heartbeat"] = fields.Datetime.now()
        return write_vals

    def _delayed_s3_rescue(self, db_name, record_id, delay_seconds=8.0):
        """Background: sleep ``delay_seconds`` then run _attempt_s3_rescue.

        Used by the webhook handler when the advisory lock could not be
        acquired (peer cron held it for >3s). Sleeping briefly gives the
        peer time to finish + the Lambda time to finish uploading
        raw_data.json. Then we attempt the rescue.

        If the peer (whoever held the lock) already advanced state, the
        rescue short-circuits idempotently. If not, this is the only
        thing that prevents the job from sitting in 'extracting' until
        the next reconcile cron tick.

        Failure semantics: any exception is logged and swallowed. The
        existing reconcile cron is the safety net of last resort.
        """
        import time as _t
        try:
            _t.sleep(max(0.0, float(delay_seconds)))
        except Exception:
            pass
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return
                if record.state != "extracting":
                    _logger.info(
                        "[gohan][job=%s] delayed-rescue: state already "
                        "'%s' -- peer advanced it, nothing to do",
                        record.name, record.state,
                    )
                    return
                rescued, reason = record._attempt_s3_rescue(
                    source="webhook-drop-rescue"
                )
                _logger.info(
                    "[gohan][job=%s] delayed-rescue: rescued=%s reason=%s",
                    record.name, rescued, reason,
                )
                cr.commit()
        except Exception:
            _logger.exception(
                "[gohan][job=%s] delayed-rescue failed -- regular "
                "reconcile cron will retry",
                record_id,
            )

    def _attempt_s3_rescue(self, source="reconciler"):
        """Probe S3 ``raw_data.json`` and auto-populate fields if found.

        Returns:
            (rescued: bool, reason: str)
            rescued=True  → fields populated, state advanced to 'generating',
                            PRD generation submitted to background pool.
            rescued=False → no S3 data yet, or transient S3 error.
            The ``reason`` is suitable for pipeline log / button toast.

        The state advance is intentional: when raw_data.json exists, Lambda
        finished the extraction phase (even partially), so the job should
        move forward into generating rather than stay stuck.
        """
        self.ensure_one()

        # Serialize against the webhook callback in controllers/main.py: both
        # mutate the same row, so they must share an advisory lock. Without
        # this lock the webhook can be persisting the full callback payload
        # while the poller tries to write a partial S3-derived payload — the
        # loser hits 40001 (SerializationFailure) at commit time, rolls back,
        # and the UI ends up with no extraction data even though Lambda
        # finished successfully (job 25 / turo.com regression).
        # pg_try_advisory_xact_lock is non-blocking: if the webhook is in
        # flight we back off and let the next poller iteration retry; the
        # webhook is by far the better path because it has the complete
        # callback payload (prd_prompt, screenshots, eq_tier...), while the
        # poller only sees the subset that landed in S3 raw_data.json.
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (_GOHAN_WEBHOOK_LOCK_NS, self.id),
        )
        if not self.env.cr.fetchone()[0]:
            return False, (
                f"peer writer (webhook callback) holds the lock for job "
                f"{self.id}; will retry on next poll iteration"
            )

        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("gohan.s3_bucket")
        if not bucket:
            return False, "gohan.s3_bucket not configured"

        s3_folder = (ICP.get_param("gohan.s3_folder") or "gohan").strip("/")

        if self.s3_artifact_prefix:
            prefix = self.s3_artifact_prefix.split(f"s3://{bucket}/", 1)[-1].rstrip("/")
        else:
            prefix = f"{s3_folder}/{self.id}"
        raw_data_key = f"{prefix}/raw_data.json"

        try:
            from ..services import s3_service
        except ImportError:
            return False, "s3_service unavailable"

        from botocore.exceptions import BotoCoreError, ClientError

        try:
            exists = s3_service.object_exists(self.env, bucket, raw_data_key)
        except (ClientError, BotoCoreError) as exc:
            return False, f"S3 head_object failed: {exc}"

        raw_data = None
        rescue_source_key = raw_data_key
        if exists:
            try:
                raw_data = s3_service.download_json_from_s3(
                    self.env, bucket, raw_data_key
                )
            except (ClientError, BotoCoreError) as exc:
                return False, f"S3 get_object failed: {exc}"
            except (ValueError, UnicodeDecodeError) as exc:
                return False, f"raw_data.json is not valid JSON: {exc}"
        else:
            # Fallback: Lambda may have failed to upload the consolidated
            # bundle (silent ClientError in older handler versions) even
            # though the controller successfully uploaded the INDIVIDUAL
            # raw_data/*.json files via upload_artifacts_to_s3 to a
            # job-name-keyed prefix. Rebuild the bundle from those.
            fallback_prefix = f"{s3_folder}/{self.name}/artifacts/raw_data/"
            try:
                fallback_keys = s3_service.list_objects(
                    self.env, bucket, fallback_prefix
                )
            except (ClientError, BotoCoreError) as exc:
                return False, (
                    f"raw_data.json not found at s3://{bucket}/{raw_data_key}; "
                    f"fallback list_objects on {fallback_prefix} failed: {exc}"
                )

            rebuilt: dict = {}
            for k in fallback_keys:
                if not k.lower().endswith(".json"):
                    continue
                stem = k.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if not stem:
                    continue
                try:
                    rebuilt[stem] = s3_service.download_json_from_s3(
                        self.env, bucket, k
                    )
                except (ClientError, BotoCoreError, ValueError, UnicodeDecodeError) as exc:
                    _logger.info(
                        "[gohan][job=%s] reconcile fallback: failed to "
                        "load %s (%s); continuing",
                        self.name, k, exc,
                    )

            if not rebuilt:
                return False, (
                    f"raw_data.json not found at s3://{bucket}/{raw_data_key} "
                    f"and no individual raw_data/*.json files found at "
                    f"s3://{bucket}/{fallback_prefix}"
                )

            raw_data = rebuilt
            rescue_source_key = f"{fallback_prefix} (rebuilt from individuals)"
            _logger.info(
                "[gohan][job=%s] reconcile: raw_data.json missing -- "
                "rebuilt bundle from %d individual files under %s",
                self.name, len(rebuilt), fallback_prefix,
            )

        screenshot_keys = []
        asset_keys = []
        try:
            keys = s3_service.list_objects(self.env, bucket, f"{prefix}/")
            for k in keys:
                if "/screenshots/" in k:
                    screenshot_keys.append(k)
                elif "/assets/" in k:
                    asset_keys.append(k)
        except Exception as exc:
            _logger.info(
                "[gohan][job=%s] reconcile: list_objects failed (%s); "
                "screenshot/asset counts will be 0",
                self.name, exc,
            )

        artifacts_url = None
        try:
            from ..services.s3_service import get_artifacts_folder_url
            artifacts_url = get_artifacts_folder_url(
                job_name=self.name,
                bucket=bucket,
                folder=s3_folder,
                cdn_url=ICP.get_param("gohan.s3_cdn_url"),
            )
        except Exception:
            pass

        write_vals = self._populate_from_raw_data(
            raw_data,
            source=source,
            screenshot_keys=screenshot_keys or None,
            asset_keys=asset_keys or None,
            artifacts_url=artifacts_url,
        )

        if not self.s3_artifact_prefix:
            write_vals["s3_artifact_prefix"] = f"s3://{bucket}/{prefix}/"

        if self.state in ("extracting", "failed"):
            write_vals["state"] = "generating"

        self.write(write_vals)
        self._notify_state_change(write_vals.get("state") or self.state)

        db_name = self.env.cr.dbname
        record_id = self.id
        self._append_pipeline_event(
            db_name, record_id, "extraction.reconcile", "ok",
            message=(
                f"Auto-populated from s3://{bucket}/{rescue_source_key} "
                f"({source}); screenshots={len(screenshot_keys)}, "
                f"assets={len(asset_keys)}"
            ),
            source=source,
            screenshots=len(screenshot_keys),
            assets=len(asset_keys),
            has_site_discovery=bool(raw_data.get("site_discovery") if isinstance(raw_data, dict) else False),
            next_state=write_vals.get("state") or self.state,
        )

        if write_vals.get("state") == "generating":
            def _deferred():
                _submit_bg(
                    f"prd-gen[reconcile,job={record_id}]",
                    self._run_prd_generation_bg, db_name, record_id,
                )
            self.env.cr.postcommit.add(_deferred)

        return True, (
            f"Recovered from {rescue_source_key} (screenshots="
            f"{len(screenshot_keys)}, assets={len(asset_keys)}); "
            f"advanced to {write_vals.get('state') or self.state}"
        )

    # ------------------------------------------------------------------
    # Cron: Reconcile orphaned runs (HANDOFF_ODOO.md §4.6)
    # ------------------------------------------------------------------

    def _reconcile_orphaned_runs(self):
        """Rescue jobs whose webhook callback was lost.

        Spec contract: for any job in an in-flight state for >30 minutes,
        check S3 for ``raw_data.json`` (the bundle Lambda always writes
        before invoking the webhook). If present, auto-populate
        ``gohan.job`` fields from the bundle and advance to ``generating``
        so PRD generation can run end-to-end from S3 alone — webhook
        delivery becomes optional.  If no artifact after 90min the job
        is marked ``failed``.
        """
        self.env.cr.execute("SELECT pg_try_advisory_lock(987654322)")
        locked = self.env.cr.fetchone()
        if not locked or not locked[0]:
            return

        try:
            ICP = self.env["ir.config_parameter"].sudo()
            bucket = ICP.get_param("gohan.s3_bucket")
            if not bucket:
                _logger.info(
                    "[gohan] reconcile skipped: gohan.s3_bucket not configured"
                )
                return

            cutoff = fields.Datetime.now() - timedelta(minutes=30)
            orphaned = self.search([
                ("state", "in", ("extracting", "generating", "scoring")),
                ("started_at", "!=", False),
                ("started_at", "<", cutoff),
            ])
            if not orphaned:
                return

            for job in orphaned:
                rescued, reason = job._attempt_s3_rescue(source="cron-reconcile")
                if rescued:
                    _logger.warning(
                        "[gohan][job=%s] reconcile: S3 raw_data.json present "
                        "but webhook never landed — auto-populated and "
                        "advanced (%s)",
                        job.name, reason,
                    )
                    continue

                if job.started_at and job.started_at < (
                    fields.Datetime.now() - timedelta(minutes=90)
                ):
                    _logger.warning(
                        "[gohan][job=%s] reconcile: no S3 raw_data.json "
                        "after 90min — marking failed (%s)",
                        job.name, reason,
                    )
                    job._mark_failed(
                        "Lambda timeout or webhook delivery failed "
                        "(reconcile cron found no S3 raw_data.json after 90min)"
                    )
                else:
                    _logger.info(
                        "[gohan][job=%s] reconcile: skipping this tick (%s)",
                        job.name, reason,
                    )
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(987654322)")


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
            row = ""
            for c in cells:
                c = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', c)
                c = re.sub(r'`(.*?)`', r'<code>\1</code>', c)
                row += f"<td>{c}</td>"
            html_lines.append(f"<tr>{row}</tr>")
        elif set(stripped) == {"-"} and len(stripped) >= 3:
            # Horizontal rule (`---`, `----`, ...).
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_table:
                html_lines.append("</table>")
                in_table = False
            html_lines.append("<hr/>")
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
