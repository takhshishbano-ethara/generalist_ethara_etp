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
    _description = "Leviathan Pipeline Job"
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
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("extracting", "Extracting"),
            ("generating", "Generating PRD"),
            ("scoring", "Scoring"),
            ("qc", "QC Review"),
            ("done", "Done"),
            ("submitted", "Submitted"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        default="draft",
        required=True,
        tracking=True,
    )
    category_id = fields.Many2one("leviathan.category", string="Website Category")
    category_key = fields.Char(related="category_id.technical_key", store=True)
    score = fields.Float(string="PRD Score", digits=(5, 2))
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
    cancel_requested = fields.Boolean(default=False)
    user_id = fields.Many2one(
        "res.users",
        string="Tasker",
        default=lambda self: self.env.user,
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

    # ------------------------------------------------------------------
    # Computed
    # ------------------------------------------------------------------

    @api.depends_context("uid")
    def _compute_is_admin(self):
        for rec in self:
            rec.is_admin = self.env.user.has_group(
                "leviathan.group_leviathan_admin"
            )

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
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_queue(self):
        self.ensure_one()
        if not self.url:
            raise UserError("Please enter a website URL before queuing.")
        if not self.category_id:
            raise UserError("Please select a website category before queuing.")
        self.write({
            "state": "queued",
            "error_message": False,
            "cancel_requested": False,
            "started_at": fields.Datetime.now(),
            "last_heartbeat": fields.Datetime.now(),
        })
        self._trigger_extraction()

    def action_cancel(self):
        self.ensure_one()
        if self.state in ("done", "submitted", "failed", "cancelled"):
            raise UserError(
                "Cannot cancel a completed, submitted, failed, or already cancelled job."
            )
        self.write({
            "state": "cancelled",
            "error_message": "Cancelled by user",
            "cancel_requested": True,
            "completed_at": fields.Datetime.now(),
        })

    def action_mark_submitted(self):
        """Tasker marks the job as submitted to Multimango."""
        self.ensure_one()
        if self.state != "done":
            raise UserError("Can only submit jobs that are in 'Done' state.")
        if not self.qc_verdict:
            raise UserError(
                "Cannot submit: QC has not passed. "
                "Review the QC report or retry QC before submitting."
            )
        self.write({"state": "submitted"})

    def action_retry(self):
        self.ensure_one()
        if self.state not in ("failed", "cancelled"):
            raise UserError("Can only retry failed or cancelled jobs.")
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
        self.action_queue()

    def action_regenerate_with_qc_feedback(self):
        """Re-run PRD generation using QC failure reasons as feedback.

        Keeps extraction data, resets only generation/scoring/QC state.
        Adds QC report as feedback to the LLM prompt.
        """
        self.ensure_one()
        if self.state != "done":
            raise UserError("Can only regenerate from Done state.")
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
        })

        # Store QC feedback as context for next generation
        if self.prd_prompt:
            self.prd_prompt = (
                self.prd_prompt + "\n\n"
                "---\n\n"
                "## PREVIOUS QC FEEDBACK (fix these issues):\n\n"
                + qc_feedback
            )

        # Trigger PRD generation in background
        db_name = self.env.cr.dbname
        record_id = self.id

        def _deferred():
            _POOL.submit(
                lambda: self._run_prd_generation_bg(db_name, record_id)
            )

        self.env.cr.postcommit.add(_deferred)

    def action_save_prd_edit(self):
        """Save the tasker's manual PRD edits from the HTML editor back to prd_text."""
        self.ensure_one()
        if not self.prd_text_html:
            raise UserError("No PRD content to save.")
        # Convert HTML back to markdown-like plain text for scoring/export
        import re
        html_content = self.prd_text_html
        # Basic HTML → text conversion (preserving structure)
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
        text = re.sub(r'<[^>]+>', '', text)  # Strip remaining HTML tags
        text = re.sub(r'\n{3,}', '\n\n', text)  # Collapse excessive newlines
        self.prd_text = text.strip()

    def action_view_prd(self):
        self.ensure_one()
        if not self.prd_url:
            raise UserError("PRD not yet generated.")
        return {
            "type": "ir.actions.act_url",
            "url": self.prd_url,
            "target": "new",
        }

    def action_download_zip(self):
        """Build and download a ZIP of the tasker deliverable package.

        Structure matches the SOP deliverable format:
            {site_slug}_website.md
            prd.md
            References/     (screenshots from S3)
            assets/         (categorized assets from S3)
        """
        self.ensure_one()
        if not self.prd_text:
            raise UserError("PRD not yet generated.")

        import base64
        import io
        import zipfile
        from urllib.parse import urlparse

        # Site slug for naming
        parsed = urlparse(self.url or "")
        site_slug = (parsed.hostname or "site").replace(".", "_").replace("www_", "")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # prd.md
            zf.writestr("prd.md", self.prd_text)

            # {site_slug}_website.md
            zf.writestr(
                f"{site_slug}_website.md",
                self._generate_website_md(),
            )

            # QC_Report.md (internal, not submitted by tasker but useful)
            if self.qc_report:
                zf.writestr("QC_Report.md", self.qc_report)

            # References/ — screenshots from S3
            download_errors = []
            if self.screenshot_keys:
                try:
                    from ..services.s3_service import download_file_from_s3
                    ICP = self.env["ir.config_parameter"].sudo()
                    s3_bucket = ICP.get_param("leviathan.s3_bucket")
                    s3_key_id = ICP.get_param("leviathan.s3_access_key_id")
                    s3_secret = ICP.get_param("leviathan.s3_secret_access_key")
                    s3_region = ICP.get_param("leviathan.s3_region") or "us-east-1"

                    for i, key in enumerate(self.screenshot_keys, 1):
                        try:
                            data = download_file_from_s3(
                                key=key,
                                bucket=s3_bucket,
                                access_key_id=s3_key_id,
                                secret_key=s3_secret,
                                region=s3_region,
                            )
                            # Preserve original filename or use numbered
                            filename = key.split("/")[-1] if "/" in key else f"{i:02d}_screenshot.png"
                            zf.writestr(f"References/{filename}", data)
                        except Exception as e:
                            download_errors.append(f"References/{key}: {e}")
                except Exception as e:
                    download_errors.append(f"S3 screenshot config error: {e}")

            # assets/ -- categorized assets from S3
            if self.asset_keys:
                try:
                    from ..services.s3_service import download_file_from_s3
                    ICP = self.env["ir.config_parameter"].sudo()
                    s3_bucket = ICP.get_param("leviathan.s3_bucket")
                    s3_key_id = ICP.get_param("leviathan.s3_access_key_id")
                    s3_secret = ICP.get_param("leviathan.s3_secret_access_key")
                    s3_region = ICP.get_param("leviathan.s3_region") or "us-east-1"

                    for key in self.asset_keys:
                        try:
                            data = download_file_from_s3(
                                key=key,
                                bucket=s3_bucket,
                                access_key_id=s3_key_id,
                                secret_key=s3_secret,
                                region=s3_region,
                            )
                            # Key format: leviathan/{id}/assets/{subdir}/{filename}
                            parts = key.split("/")
                            if "assets" in parts:
                                idx = parts.index("assets")
                                # Preserve subfolder/filename structure
                                rel_path = "/".join(parts[idx:])
                            else:
                                filename = parts[-1] if parts else key
                                rel_path = f"assets/{filename}"
                            zf.writestr(rel_path, data)
                        except Exception as e:
                            download_errors.append(f"{rel_path}: {e}")
                except Exception as e:
                    download_errors.append(f"S3 asset config error: {e}")

            # Include error log if any downloads failed
            if download_errors:
                error_report = "# Download Errors\n\nThe following files could not be downloaded from S3:\n\n"
                for err in download_errors:
                    error_report += f"- {err}\n"
                error_report += "\nPlease check S3 credentials and bucket configuration.\n"
                zf.writestr("DOWNLOAD_ERRORS.md", error_report)

        buf.seek(0)
        zip_data = base64.b64encode(buf.read())

        # Create ir.attachment for download
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
        """Generate website.md content -- just the canonical URL (matches SOP format)."""
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

    def _trigger_prd_generation(self):
        db_name = self.env.cr.dbname
        record_id = self.id

        def _run():
            self._run_prd_generation_bg(db_name, record_id)

        _POOL.submit(_run)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mark_failed(self, error_msg):
        self._write_with_retry({
            "state": "failed",
            "error_message": error_msg,
            "completed_at": fields.Datetime.now(),
        })
        self._log_pipeline_event(f"❌ Pipeline failed: {error_msg[:200]}")

    def _mark_done(self, started, duration):
        self._write_with_retry({
            "state": "done",
            "started_at": started,
            "completed_at": fields.Datetime.now(),
            "duration_seconds": duration.total_seconds() if duration else 0,
        })

    def _log_pipeline_event(self, message):
        """Post a progress message to the job's chatter for audit trail."""
        try:
            self.message_post(body=message, message_type="comment", subtype_xmlid="mail.mt_note")
        except Exception:
            _logger.debug("Could not post pipeline event for job %s", self.id)

    def _write_with_retry(self, vals, max_retries=3):
        """Write values to a job record with serialization conflict retry."""
        for attempt in range(max_retries):
            try:
                with Registry(self.env.cr.dbname).cursor() as cr:
                    env = self.env(cr=cr)
                    record = env[self._name].browse(self.id)
                    if record.exists():
                        record.write(vals)
                    return
            except Exception as e:
                if "serialize" in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise

    def _check_cancelled(self):
        """Return True if cancel has been requested for this job."""
        self.env.cr.execute(
            "SELECT cancel_requested FROM leviathan_job WHERE id = %s",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        return row and row[0]

    def _is_cancelled(self, db_name, record_id):
        """Check if a job has been cancelled (safe for background threads)."""
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

    def _heartbeat(self):
        """Update last_heartbeat timestamp for watchdog tracking."""
        try:
            self._write_with_retry({"last_heartbeat": fields.Datetime.now()})
        except Exception:
            _logger.debug("Heartbeat write failed for job %s (non-fatal)", self.id)

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
        try:
            with Registry(db_name).cursor() as cr:
                env = self.env(cr=cr)
                record = env[self._name].browse(record_id)
                if not record.exists():
                    return

                from ..services.extraction_service import trigger_extraction

                ICP = env["ir.config_parameter"].sudo()
                service_url = ICP.get_param("leviathan.extraction_service_url")
                access_key_id = ICP.get_param("leviathan.extraction_access_key_id")
                secret_access_key = ICP.get_param(
                    "leviathan.extraction_secret_access_key"
                )

                record.write({"state": "extracting"})

                result = trigger_extraction(
                    url=record.url,
                    job_id=record_id,
                    callback_url=record._get_webhook_url(),
                    service_url=service_url,
                    access_key_id=access_key_id,
                    secret_access_key=secret_access_key,
                )

                if not result.get("success"):
                    record._mark_failed(
                        result.get("error", "Extraction service call failed")
                    )

        except Exception as exc:
            _logger.exception(
                "Extraction background task failed for job %s", record_id
            )
            try:
                with Registry(db_name).cursor() as cr:
                    env = self.env(cr=cr)
                    record = env[self._name].browse(record_id)
                    if record.exists():
                        record._mark_failed(str(exc))
            except Exception:
                _logger.error("Failed to mark job %s as failed", record_id)

    # ------------------------------------------------------------------
    # Background: PRD Generation
    # ------------------------------------------------------------------

    def _run_prd_generation_bg(self, db_name, record_id):
        """Background: generate PRD via Bedrock, score, iterate.

        Architecture: read config (short cursor) → compute (no cursor) → write results (short cursor).
        This avoids holding a DB cursor open during 5-15 min of LLM calls.
        """
        from ..services.bedrock_service import generate_prd
        from ..services.scoring_service import score_prd
        from ..services.s3_service import upload_prd_to_s3

        try:
            # === PHASE 1: Read config and extraction data (short cursor) ===
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
                }

                if not config["inference_arn"]:
                    record._mark_failed("Bedrock inference ARN not configured")
                    return
                if not job_data["prd_prompt"]:
                    record._mark_failed("No extraction data available for PRD generation")
                    return

                record.write({"state": "generating"})
                cr.commit()

            # === PHASE 2: LLM generation loop (NO cursor held) ===
            prompts_dir = Path(__file__).parent.parent / "prompts"
            system_prompt = (prompts_dir / "prd_agent_spec.md").read_text(encoding="utf-8")

            base_user_message = (
                f"Below is the extracted website data. "
                f"Write the complete PRD following all rules.\n\n"
                f"---\n\n{job_data['prd_prompt']}"
            )
            messages = [{"role": "user", "content": base_user_message}]

            best_prd_text = None
            best_score = 0
            best_grade = None
            best_score_report = None

            for attempt in range(1, config["max_attempts"] + 1):
                if self._is_cancelled(db_name, record_id):
                    self._write_with_cursor(db_name, record_id, {
                        "state": "failed", "error_message": "Cancelled during generation",
                        "completed_at": fields.Datetime.now(),
                    })
                    return

                # Heartbeat
                self._write_with_cursor(db_name, record_id, {
                    "last_heartbeat": fields.Datetime.now(),
                })

                # Generate PRD
                prd_text = generate_prd(
                    inference_arn=config["inference_arn"],
                    region=config["region"],
                    system_prompt=system_prompt,
                    messages=messages,
                    access_key_id=config["bedrock_access_key"],
                    secret_access_key=config["bedrock_secret_key"],
                )

                # Score PRD (local, no network)
                score_report = score_prd(
                    prd_text=prd_text,
                    category=job_data["category_name"],
                )
                total_score = score_report["total_score"]

                self._write_with_cursor(db_name, record_id, {
                    "llm_attempts": attempt,
                })

                if total_score > best_score:
                    best_prd_text = prd_text
                    best_score = total_score
                    best_grade = score_report["grade"]
                    best_score_report = score_report

                # Don't build feedback after last attempt
                if attempt < config["max_attempts"]:
                    messages.append({"role": "assistant", "content": prd_text})
                    feedback = self._build_feedback(score_report)
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Score: {total_score}/100 (attempt {attempt})\n"
                            f"Feedback:\n{feedback}\n\n"
                            "Fix all issues and rewrite the complete PRD."
                        ),
                    })

            # Upload to S3 (no cursor needed)
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

            # === PHASE 3: QC (no cursor needed for LLM call) ===
            self._write_with_cursor(db_name, record_id, {"state": "scoring"})

            qc_verdict = False
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
                )
                qc_verdict = qc_result["verdict"]
                qc_report = qc_result["report"]
            except Exception as qc_exc:
                _logger.warning(
                    "QC failed for job %s: %s (proceeding to done)",
                    job_data["name"], qc_exc,
                )
                qc_report = f"QC error: {qc_exc}"

            # === PHASE 4: Write final results (short cursor) ===
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

                # Notify user via bus
                if job_data["partner_id"]:
                    partner = env["res.partner"].browse(job_data["partner_id"])
                    env["bus.bus"]._sendone(
                        partner,
                        "leviathan/job_done",
                        {"id": record_id, "name": job_data["name"]},
                    )

                cr.commit()

        except Exception as exc:
            _logger.exception("PRD generation failed for job %s", record_id)
            try:
                self._write_with_cursor(db_name, record_id, {
                    "state": "failed",
                    "error_message": str(exc),
                    "completed_at": fields.Datetime.now(),
                })
            except Exception:
                _logger.error("Failed to mark job %s as failed", record_id)

    def _write_with_cursor(self, db_name, record_id, vals):
        """Write values to a record using a short-lived cursor."""
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[self._name].browse(record_id)
            if record.exists():
                record.write(vals)
            cr.commit()

    def _build_feedback(self, score_report):
        from ..services.scoring_service import SECTION_MAX_POINTS

        lines = []
        section_scores = score_report.get("section_scores", {})
        for section, section_data in section_scores.items():
            # section_data is a dict: {"score": N, "max": M, "details": {...}}
            score_val = section_data["score"] if isinstance(section_data, dict) else section_data
            max_points = SECTION_MAX_POINTS.get(section, 10)
            if score_val < max_points * 0.6:
                lines.append(
                    f"- {section}: scored {score_val}/{max_points} — needs improvement"
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
        """Recover jobs stuck in intermediate states beyond timeout thresholds.

        Thresholds:
        - extracting: 15 minutes (external service should respond quickly)
        - generating/scoring: 30 minutes (LLM iterations can take time)
        """
        # Advisory lock to prevent multiple workers
        self.env.cr.execute("SELECT pg_try_advisory_lock(987654321)")
        locked = self.env.cr.fetchone()
        if not locked or not locked[0]:
            _logger.info(
                "Watchdog: another worker holds the advisory lock, skipping."
            )
            return

        try:
            # Extracting > 15 min
            stale_extracting = self.search([
                ("state", "=", "extracting"),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=15),
                ),
            ])
            for job in stale_extracting:
                _logger.warning(
                    "Watchdog: job %s stuck in extracting for >15min, "
                    "marking failed.",
                    job.name,
                )
                job._mark_failed(
                    "Watchdog: extraction timed out "
                    "(no response for 15+ minutes)"
                )

            # Generating/scoring > 30 min
            stale_generating = self.search([
                ("state", "in", ("generating", "scoring", "qc")),
                (
                    "last_heartbeat",
                    "<",
                    fields.Datetime.now() - timedelta(minutes=30),
                ),
            ])
            for job in stale_generating:
                _logger.warning(
                    "Watchdog: job %s stuck in %s for >30min, marking failed.",
                    job.name,
                    job.state,
                )
                job._mark_failed(
                    f"Watchdog: {job.state} timed out "
                    "(no progress for 30+ minutes)"
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

        # Headers
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
        # List items
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = stripped[2:]
            # Bold formatting
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)
            content = re.sub(r'`(.*?)`', r'<code>\1</code>', content)
            html_lines.append(f"<li>{content}</li>")
        # Table rows
        elif stripped.startswith("|") and stripped.endswith("|"):
            if not in_table:
                html_lines.append("<table class='table table-sm'>")
                in_table = True
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if all(set(c) <= set("- :") for c in cells):
                continue  # Skip separator row
            row = "".join(f"<td>{c}</td>" for c in cells)
            html_lines.append(f"<tr>{row}</tr>")
        # Empty line
        elif not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_table:
                html_lines.append("</table>")
                in_table = False
            html_lines.append("<br/>")
        # Regular paragraph
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
