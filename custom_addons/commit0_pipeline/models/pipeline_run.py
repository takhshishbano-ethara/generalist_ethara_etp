# -*- coding: utf-8 -*-
import base64
import csv
import io
import logging
import os
import re
import sys

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from . import pipeline_executor

_logger = logging.getLogger(__name__)

MODEL_SELECTION = [
    ("opus", "Claude Opus 4.6"),
    ("kimi", "Kimi K2.5"),
    ("glm5", "GLM 5"),
    ("minimax", "MiniMax M2.5"),
    ("gpt54", "GPT 5.4"),
    ("custom", "Custom Model"),
]

STUBBING_SELECTION = [
    ("all", "All"),
    ("docstring", "Docstring Only"),
    ("combined", "Combined"),
]

STATE_SELECTION = [
    ("idle", "Idle"),
    ("discovering", "Discovering"),
    ("validating", "Validating"),
    ("preparing", "Preparing"),
    ("creating_dataset", "Creating Dataset"),
    ("generating_tests", "Generating Tests"),
    ("setting_up", "Setting Up"),
    ("building", "Building"),
    ("complete", "Complete"),
    ("failed", "Failed"),
    ("cancelled", "Cancelled"),
]

GITHUB_RE = re.compile(r"^https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$")


class Commit0PipelineRun(models.Model):
    _name = "commit0.pipeline.run"
    _description = "Commit0 Pipeline Run"
    _inherit = ["mail.thread"]
    _order = "id desc"

    # --- Identity ---
    name = fields.Char(
        string="Name",
        required=True,
        readonly=True,
        copy=False,
        default="New",
    )
    state = fields.Selection(
        selection=STATE_SELECTION,
        string="State",
        required=True,
        default="idle",
        tracking=True,
        copy=False,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Created By",
        default=lambda self: self.env.uid,
        readonly=True,
    )

    # --- Input Configuration ---
    entry_type = fields.Selection(
        selection=[
            ("single", "Single Repository"),
            ("batch", "Batch CSV"),
        ],
        string="Entry Type",
        required=True,
        default="single",
    )
    repo_url = fields.Char(
        string="Repository URL",
        help="GitHub URL for single repository mode.",
    )
    stubbing_mode = fields.Selection(
        selection=STUBBING_SELECTION,
        string="Stubbing Mode",
        default="combined",
    )
    model_preset = fields.Selection(
        selection=MODEL_SELECTION,
        string="Model Preset",
    )
    custom_model_id = fields.Char(
        string="Custom Model ID",
        help="Custom model ARN or identifier when model_preset is 'custom'.",
    )
    organization = fields.Char(
        string="Organization",
    )
    min_stars = fields.Integer(
        string="Min Stars",
        default=5000,
    )
    max_results = fields.Integer(
        string="Max Results",
        default=200,
    )
    tag = fields.Char(
        string="Git Tag",
    )
    max_iteration = fields.Integer(
        string="Max Iterations",
        default=3,
    )
    csv_file = fields.Binary(
        string="CSV File",
    )
    csv_filename = fields.Char(
        string="CSV Filename",
    )

    # --- Output / Logs ---
    log_output = fields.Text(
        string="Pipeline Log",
    )
    error_message = fields.Text(
        string="Error Details",
    )
    start_time = fields.Datetime(
        string="Start Time",
        copy=False,
    )
    end_time = fields.Datetime(
        string="End Time",
        copy=False,
    )

    # --- Artifact Paths ---
    dataset_json_path = fields.Char(
        string="Dataset JSON Path",
    )
    entries_json_path = fields.Char(
        string="Entries JSON Path",
    )
    test_ids_path = fields.Char(
        string="Test IDs Path",
    )

    # --- Validation (GitHub API checks) ---
    validation_status = fields.Selection(
        selection=[
            ("not_validated", "Not Validated"),
            ("validating", "Validating..."),
            ("passed", "Passed"),
            ("failed", "Failed"),
        ],
        string="Validation Status",
        default="not_validated",
        copy=False,
    )
    validation_details = fields.Text(
        string="Validation Details",
        copy=False,
    )

    # --- Relational ---
    repo_entry_ids = fields.One2many(
        "commit0.repo.entry",
        "pipeline_run_id",
        string="Repository Entries",
    )
    candidate_ids = fields.One2many(
        "commit0.discovery.candidate",
        "pipeline_run_id",
        string="Discovery Candidates",
    )

    # --- Computed ---
    repo_count = fields.Integer(
        string="Repo Count",
        compute="_compute_repo_count",
    )
    progress_pct = fields.Float(
        string="Progress %",
        compute="_compute_progress_pct",
    )

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("commit0.pipeline.run")
                    or "New"
                )
        return super().create(vals_list)

    # -------------------------------------------------------------------------
    # Compute
    # -------------------------------------------------------------------------
    @api.depends("repo_entry_ids")
    def _compute_repo_count(self):
        for run in self:
            run.repo_count = len(run.repo_entry_ids)

    @api.depends("repo_entry_ids", "repo_entry_ids.state")
    def _compute_progress_pct(self):
        for run in self:
            total = len(run.repo_entry_ids)
            if not total:
                run.progress_pct = 0.0
                continue
            complete = len(run.repo_entry_ids.filtered(lambda e: e.state == "complete"))
            run.progress_pct = (complete / total) * 100.0

    # -------------------------------------------------------------------------
    # Defaults from config
    # -------------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ICP = self.env["ir.config_parameter"].sudo()
        if "model_preset" in fields_list and "model_preset" not in res:
            val = ICP.get_param("commit0_pipeline.default_model", "")
            if val:
                res["model_preset"] = val
        if "organization" in fields_list and "organization" not in res:
            val = ICP.get_param("commit0_pipeline.github_org", "Ethara-Ai")
            if val:
                res["organization"] = val
        if "stubbing_mode" in fields_list and "stubbing_mode" not in res:
            val = ICP.get_param("commit0_pipeline.default_stubbing_mode", "combined")
            if val:
                res["stubbing_mode"] = val
        return res

    # -------------------------------------------------------------------------
    # Constraints
    # -------------------------------------------------------------------------
    @api.constrains("entry_type", "repo_url")
    def _check_repo_url(self):
        for run in self:
            if run.entry_type == "single":
                if not run.repo_url:
                    raise ValidationError(
                        "Repository URL is required for single repository mode."
                    )
                if not GITHUB_RE.match(run.repo_url):
                    raise ValidationError(
                        "Repository URL must match the pattern "
                        "'https://github.com/<owner>/<repo>'."
                    )

    @api.constrains("entry_type", "csv_file")
    def _check_csv_file(self):
        for run in self:
            if run.entry_type == "batch" and not run.csv_file:
                raise ValidationError("A CSV file is required for batch mode.")

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------
    def action_start_pipeline(self):
        """Validate inputs and submit pipeline for background execution."""
        self.ensure_one()
        if self.state not in ("idle", "failed"):
            raise ValidationError(
                "Pipeline can only be started from Idle or Failed state."
            )
        self.write(
            {
                "state": "discovering",
                "start_time": fields.Datetime.now(),
                "error_message": False,
            }
        )

        db_name = self.env.cr.dbname
        uid = self.env.uid
        run_id = self.id

        def _submit_after_commit():
            result = pipeline_executor.submit_pipeline_async(db_name, uid, run_id)
            if not result:
                _logger.warning("Pipeline queue full for run %s (post-commit)", run_id)

        self.env.cr.postcommit.add(_submit_after_commit)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Pipeline Started",
                "message": "Pipeline %s has been submitted for execution." % self.name,
                "type": "success",
                "sticky": False,
            },
        }

    def action_cancel_pipeline(self):
        """Cancel the running pipeline."""
        self.ensure_one()
        self.write(
            {
                "state": "cancelled",
                "end_time": fields.Datetime.now(),
            }
        )

    def action_retry_failed(self):
        """Reset failed repo entries to pending and restart pipeline."""
        self.ensure_one()
        failed_entries = self.repo_entry_ids.filtered(lambda e: e.state == "failed")
        failed_entries.write({"state": "pending", "error_message": False})
        self.action_start_pipeline()

    def action_validate_repo(self):
        self.ensure_one()
        if self.entry_type != "single":
            raise ValidationError(
                "Validation is only available for single repository mode."
            )
        if not self.repo_url:
            raise ValidationError("Please enter a repository URL first.")

        full_name = self.repo_url.rstrip("/").replace("https://github.com/", "")
        if full_name.endswith(".git"):
            full_name = full_name[:-4]

        ICP = self.env["ir.config_parameter"].sudo()
        github_token = ICP.get_param("commit0_pipeline.github_token", "")

        self.write({"validation_status": "validating", "validation_details": ""})

        module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tools_path = os.path.join(module_path, "tools")
        parent = os.path.dirname(tools_path)
        if parent not in sys.path:
            sys.path.insert(0, parent)

        try:
            from tools.repo_validator import validate_repo, format_validation_report

            result = validate_repo(full_name, github_token=github_token)
            details = format_validation_report(result)
            status = "passed" if result["passed"] else "failed"

            self.write(
                {
                    "validation_status": status,
                    "validation_details": details,
                }
            )
        except Exception as e:
            _logger.exception("Repo validation failed for %s", full_name)
            self.write(
                {
                    "validation_status": "failed",
                    "validation_details": "Validation error: %s" % str(e),
                }
            )

        return False

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _parse_csv(self):
        """Decode base64 CSV file and return list of dicts.

        Expected CSV columns (from commit0 docs):
            library_name, Github url, Organization Name, RnD
        """
        self.ensure_one()
        if not self.csv_file:
            return []
        raw = base64.b64decode(self.csv_file)
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            # Handle both space-separated and underscore-separated column names
            github_url = (
                row.get("Github url")
                or row.get("github_url")
                or row.get("Github_url")
                or ""
            ).strip()
            # Strip .git suffix if present
            if github_url.endswith(".git"):
                github_url = github_url[:-4]
            rows.append(
                {
                    "library_name": (row.get("library_name") or "").strip(),
                    "github_url": github_url,
                    "organization_name": (
                        row.get("Organization Name")
                        or row.get("organization_name")
                        or ""
                    ).strip(),
                }
            )
        return rows
