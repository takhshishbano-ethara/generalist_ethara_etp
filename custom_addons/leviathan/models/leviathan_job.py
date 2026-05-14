import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="leviathan-job")


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
    started_at = fields.Datetime(string="Started At")
    completed_at = fields.Datetime(string="Completed At")
    last_heartbeat = fields.Datetime(string="Last Heartbeat")

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
    via_rabbitmq = fields.Boolean(
        string="Triggered via RabbitMQ",
        default=False,
        copy=False,
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
        # Auto-promote to draft when admin assigns a user to not_assigned task
        if "user_id" in vals and vals["user_id"]:
            to_promote = self.filtered(lambda r: r.state == "not_assigned")
            if to_promote:
                super(LeviathanJob, to_promote).write({"state": "draft"})
        # Auto-demote to not_assigned when user is removed from draft task
        if "user_id" in vals and not vals["user_id"]:
            to_demote = self.filtered(lambda r: r.state == "draft")
            if to_demote:
                super(LeviathanJob, to_demote).write({"state": "not_assigned"})
        return res

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    _ACTIVE_STATES = ("draft", "extracting", "generating", "scoring", "done")

    def action_start_task(self):
        """Tasker grabs the next available unassigned task.

        Picks the oldest not_assigned task. If it already has PRD data
        (released from done), state goes to done. Otherwise draft.
        """
        user = self.env.user
        ICP = self.env["ir.config_parameter"].sudo()
        max_active = int(ICP.get_param("leviathan.max_jobs_per_user", "5"))

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

        # Pick oldest available task (not_assigned or failed+unassigned)
        domain = [("state", "in", ("not_assigned", "failed")), ("user_id", "=", False)]
        cat_id = self.env.context.get("start_task_category_id")
        if cat_id:
            domain.append(("category_id", "=", cat_id))
        task = self.sudo().search(
            domain,
            order="create_date asc",
            limit=1,
        )
        if not task:
            raise UserError("No tasks available. Check back later.")

        # Smart state: if PRD exists (released done task), go to done
        new_state = "done" if task.prd_text else "draft"
        task.write({"user_id": user.id, "state": new_state})
        task._notify_state_change(new_state)

        # Navigate to the picked task
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
                "via_rabbitmq": False,
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

        # Lock row to prevent double-run (graceful on lock conflict)
        sp_name = f"leviathan_run_{self.id}"
        self.env.cr.execute(f"SAVEPOINT {sp_name}")
        try:
            self.env.cr.execute(
                "SELECT id FROM leviathan_job WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Exception:
            self.env.cr.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
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
        self.ensure_one()
        if self.state == "submitted":
            raise UserError("Cannot cancel a submitted task.")
        self.write({
            "state": "draft",
            "cancel_requested": True,
            "error_message": False,
        })

    def action_run_via_rabbitmq(self):
        """Server action: batch-run selected tasks through RabbitMQ pipeline.

        Admin selects tasks in list view -> clicks this action.
        Validates each task, marks as extracting, publishes to queue.
        """
        eligible = self.filtered(
            lambda r: r.state == "not_assigned" and r.url
        )
        if not eligible:
            raise UserError(
                "No eligible tasks. Tasks must be 'Not Assigned' "
                "with URL and category set."
            )
        skipped = self - eligible

        eligible.write({
            "state": "extracting",
            "via_rabbitmq": True,
            "started_at": fields.Datetime.now(),
            "completed_at": False,
            "duration_seconds": False,
            "last_heartbeat": fields.Datetime.now(),
            "error_message": False,
            "cancel_requested": False,
        })

        record_ids = eligible.ids
        db_name = self.env.cr.dbname

        def _deferred():
            from ..services.rabbitmq_service import batch_publish_leviathan_tasks
            try:
                batch_publish_leviathan_tasks(record_ids, action="run_pipeline")
            except Exception as exc:
                _logger.error("RabbitMQ publish failed: %s", exc)
                # Revert tasks back to not_assigned since they weren't published
                try:
                    from odoo.modules.registry import Registry
                    with Registry(db_name).cursor() as cr:
                        from odoo import api, SUPERUSER_ID
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        env["leviathan.job"].browse(record_ids).write({
                            "state": "not_assigned",
                            "via_rabbitmq": False,
                            "error_message": f"RabbitMQ unavailable: {exc}",
                        })
                        cr.commit()
                except Exception:
                    _logger.exception("Failed to revert tasks after RabbitMQ error")
                raise

        self.env.cr.postcommit.add(_deferred)

        msg = f"{len(eligible)} task(s) queued for processing."
        if skipped:
            msg += (
                f" {len(skipped)} task(s) skipped "
                f"(wrong state or missing URL/category)."
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Tasks Queued",
                "message": msg,
                "type": "success",
                "sticky": False,
            },
        }

    def rpc_run_full_pipeline(self):
        """Called by RabbitMQ consumer via XML-RPC.

        Triggers extraction for the task. The rest of the pipeline
        proceeds automatically via webhook -> generation -> QC -> done.
        Post-completion hook resets state to not_assigned.
        """
        self.ensure_one()
        if self.state != "extracting":
            _logger.warning(
                "rpc_run_full_pipeline called on job %s in state %s, skipping",
                self.id, self.state,
            )
            return False

        db_name = self.env.cr.dbname
        record_id = self.id

        try:
            self._run_extraction_bg(db_name, record_id)
        except Exception as e:
            _logger.error(
                "rpc_run_full_pipeline failed for job %s: %s", self.id, e
            )
            raise
        return True

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

    def action_retry(self):
        """Retry a failed task.

        If extraction data exists, open the rerun wizard so user can choose
        between re-extract or just regenerate PRD. Otherwise go straight
        to extracting.
        """
        self.ensure_one()
        if self.state != "failed":
            raise UserError("Can only retry failed tasks.")

        if self._has_extraction_data:
            # Has extraction data — let user choose
            wizard = self.env["leviathan.rerun.wizard"].create({"job_id": self.id})
            return {
                "type": "ir.actions.act_window",
                "name": "Retry Task",
                "res_model": "leviathan.rerun.wizard",
                "res_id": wizard.id,
                "view_mode": "form",
                "views": [(False, "form")],
                "target": "new",
            }

        # No extraction data — reset and re-extract
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
                "duration_seconds": False,
                "error_message": False,
                "started_at": fields.Datetime.now(),
                "completed_at": False,
                "last_heartbeat": fields.Datetime.now(),
            })
            db_name = self.env.cr.dbname
            record_id = self.id

            def _deferred():
                _POOL.submit(
                    lambda: self._run_prd_generation_bg(db_name, record_id)
                )

            self.env.cr.postcommit.add(_deferred)

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

        def _deferred():
            _POOL.submit(
                lambda: self._run_prd_generation_bg(db_name, record_id)
            )

        self.env.cr.postcommit.add(_deferred)

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

        def _deferred():
            _POOL.submit(
                lambda: self._run_qc_only_bg(db_name, record_id)
            )

        self.env.cr.postcommit.add(_deferred)

    def _run_qc_only_bg(self, db_name, record_id):
        """Background: re-run only QC on existing PRD text."""
        from ..services.qc_service import run_qc

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
                        )
                        if len(img_bytes) > MAX_IMG_BYTES:
                            continue
                        total_bytes += len(img_bytes)
                        if total_bytes > 20_000_000:
                            break
                        ext = key.rsplit(".", 1)[-1].lower()
                        fmt = ext if ext in ("png", "jpeg", "gif", "webp") else "png"
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

        def _run():
            self._run_extraction_bg(db_name, record_id)

        self.env.cr.postcommit.add(lambda: _POOL.submit(_run))

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
        """Background: call extraction service API."""
        from ..services.extraction_service import trigger_extraction

        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return

                ICP = env["ir.config_parameter"].sudo()
                config = {
                    "service_url": ICP.get_param("leviathan.extraction_service_url"),
                    "access_key_id": ICP.get_param("leviathan.extraction_access_key_id"),
                    "secret_access_key": ICP.get_param("leviathan.extraction_secret_access_key"),
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
                service_url=config["service_url"],
                access_key_id=config["access_key_id"],
                secret_access_key=config["secret_access_key"],
            )

            if not result.get("success"):
                error_msg = result.get("error", "Extraction service call failed")
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": error_msg[:500],
                    "completed_at": fields.Datetime.now(),
                })

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
        """Background: generate PRD via Bedrock, score, iterate, QC."""
        from ..services.bedrock_service import generate_prd
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
                config = {
                    "inference_arn": ICP.get_param("leviathan.bedrock_inference_arn"),
                    "region": ICP.get_param("leviathan.bedrock_region") or "us-east-1",
                    "max_attempts": int(ICP.get_param("leviathan.max_llm_attempts") or 3),
                    "bedrock_access_key": ICP.get_param("leviathan.bedrock_access_key_id"),
                    "bedrock_secret_key": ICP.get_param("leviathan.bedrock_secret_access_key"),
                    "s3_bucket": ICP.get_param("leviathan.s3_bucket"),
                    "s3_key_id": ICP.get_param("leviathan.s3_access_key_id"),
                    "s3_secret": ICP.get_param("leviathan.s3_secret_access_key"),
                    "s3_region": ICP.get_param("leviathan.s3_region"),
                    "s3_folder": ICP.get_param("leviathan.s3_folder") or "leviathan",
                    "cdn_url": ICP.get_param("leviathan.s3_cdn_url"),
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
                    record.write({
                        "state": "failed",
                        "error_message": "Bedrock inference ARN not configured",
                        "completed_at": fields.Datetime.now(),
                    })
                    return
                if not job_data["prd_prompt"]:
                    record.write({
                        "state": "failed",
                        "error_message": "No extraction data available for PRD generation",
                        "completed_at": fields.Datetime.now(),
                    })
                    return

                record.write({"state": "generating"})
                cr.commit()

            # === PHASE 2: LLM generation loop ===
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
                        total_bytes += len(img_bytes)
                        if total_bytes > 20_000_000:  # 20MB safety cap
                            _logger.info("Screenshot total size cap reached, stopping")
                            break
                        ext = key.rsplit(".", 1)[-1].lower()
                        fmt = ext if ext in ("png", "jpeg", "gif", "webp") else "png"
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

            for attempt in range(1, config["max_attempts"] + 1):
                if self._is_cancelled(db_name, record_id):
                    self._write_with_cursor(db_name, record_id, {
                        "state": "draft", "error_message": "Cancelled during generation",
                        "completed_at": fields.Datetime.now(),
                    })
                    return

                self._write_with_cursor(db_name, record_id, {
                    "last_heartbeat": fields.Datetime.now(),
                })

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
                    if attempt == config["max_attempts"]:
                        raise
                    time.sleep(2 * attempt)
                    continue

                score_report = score_prd(
                    prd_text=prd_text,
                    category=job_data["category_name"],
                )
                total_score = score_report["total_score"]

                self._write_with_cursor(db_name, record_id, {
                    "llm_attempts": attempt,
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
            self._write_with_cursor(db_name, record_id, {"state": "scoring"})

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
            except Exception as qc_exc:
                _logger.warning(
                    "QC failed for job %s: %s (fail-closed: not_shippable)",
                    job_data["name"], qc_exc,
                )
                qc_verdict = "not_shippable"
                qc_report = f"QC evaluation failed: {qc_exc}\n\nVerdict defaulted to NOT SHIPPABLE (fail-closed policy)."

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
                    "prd_text_html": _markdown_to_html(best_prd_text),
                    "score": best_score,
                    "grade": best_grade,
                    "score_report_json": best_score_report,
                    "prd_url": prd_url,
                    "qc_verdict": qc_verdict,
                    "qc_report": qc_report,
                    "completed_at": fields.Datetime.now(),
                    "duration_seconds": duration,
                })

                try:
                    env["bus.bus"]._sendone(
                        "leviathan_job_updates",
                        "leviathan/job_done",
                        {"id": record_id, "name": job_data["name"]},
                    )
                except Exception:
                    _logger.debug("bus.bus notification failed for job %s (non-fatal)", record_id)

                # Post-completion hook: if triggered via RabbitMQ,
                # reset to not_assigned so taskers can claim the task.
                if record.via_rabbitmq:
                    record.write({
                        "state": "not_assigned",
                        "via_rabbitmq": False,
                        "user_id": False,
                    })
                    _logger.info(
                        "RabbitMQ pipeline done for job %s — reset to not_assigned",
                        record_id,
                    )

                cr.commit()

        except Exception as exc:
            _logger.exception("PRD generation failed for job %s", record_id)
            try:
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": str(exc)[:500],
                    "completed_at": fields.Datetime.now(),
                })
            except Exception:
                _logger.error("Failed to mark job %s as failed", record_id)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _write_with_cursor(self, db_name, record_id, vals):
        """Write values to a record using a short-lived cursor."""
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[self._name].browse(record_id)
            if record.exists():
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
            return

        try:
            # Extracting > 20 min
            stale_extracting = self.search([
                ("state", "=", "extracting"),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=20),
                ),
            ])
            for job in stale_extracting:
                _logger.warning(
                    "Watchdog: task %s stuck in extracting for >20min, marking failed.",
                    job.name,
                )
                job._mark_failed(
                    "Watchdog: extraction timed out (no response for 20+ minutes)"
                )

            # Generating/scoring > 30 min
            stale_generating = self.search([
                ("state", "in", ("generating", "scoring")),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=30),
                ),
            ])
            for job in stale_generating:
                _logger.warning(
                    "Watchdog: task %s stuck in %s for >30min, marking failed.",
                    job.name,
                    job.state,
                )
                job._mark_failed(
                    f"Watchdog: {job.state} timed out (no progress for 30+ minutes)"
                )
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(987654321)")


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
