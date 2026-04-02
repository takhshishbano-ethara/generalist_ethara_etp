# -*- coding: utf-8 -*-
import logging
import os
import subprocess
import tempfile

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

STAGE_SELECTION = [
    ("stage1", "Repository Understanding"),
    ("stage2", "QC Checklist"),
    ("stage3", "Automated Preparation"),
    ("stage4", "Spec Document Review"),
    ("stage5", "Stub Code Review"),
    ("stage6", "Docker Generation"),
    ("done", "Complete"),
    ("failed", "Failed"),
]

TERMINAL_STATE_SELECTION = [
    ("none", "Active"),
    ("repo_not_suitable", "Repository Not Suitable"),
    ("rejected", "Spec Rejected"),
    ("not_stubbed", "Not Stubbed Properly"),
    ("complete", "Complete"),
]

AUTOMATION_STATUS_SELECTION = [
    ("pending", "Pending"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]

DOCKER_STATUS_SELECTION = [
    ("pending", "Pending"),
    ("generating", "Generating"),
    ("llm_qc", "LLM QC"),
    ("multiarch", "Multiarch Build"),
    ("testing", "Testing"),
    ("done", "Done"),
    ("failed", "Failed"),
]


class Commit0RepoEvaluation(models.Model):
    _name = "commit0.repo.evaluation"
    _description = "Commit0 Repository Evaluation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    # =========================================================================
    # IDENTITY
    # =========================================================================
    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        copy=False,
        default="New",
    )
    user_id = fields.Many2one(
        "res.users",
        string="Evaluator",
        default=lambda self: self.env.uid,
        readonly=True,
    )
    repo_name = fields.Char(
        string="Repository Name",
        compute="_compute_repo_name",
        store=True,
        readonly=False,
        tracking=True,
    )
    repo_url = fields.Char(
        string="GitHub URL",
        tracking=True,
    )
    fork_url = fields.Char(
        string="Fork URL",
    )
    fork_progress = fields.Float(
        string="Fork Progress",
        default=0.0,
    )

    # =========================================================================
    # STAGE TRACKING
    # =========================================================================
    current_stage = fields.Selection(
        selection=STAGE_SELECTION,
        string="Current Stage",
        required=True,
        default="stage1",
        tracking=True,
        copy=False,
    )
    terminal_state = fields.Selection(
        selection=TERMINAL_STATE_SELECTION,
        string="Terminal State",
        default="none",
        tracking=True,
    )

    # =========================================================================
    # STAGE 1 — Repository Understanding
    # =========================================================================
    clone_path = fields.Char(
        string="Clone Path",
    )
    clone_status = fields.Selection(
        selection=AUTOMATION_STATUS_SELECTION,
        string="Clone Status",
        default="pending",
    )
    repo_understood = fields.Boolean(
        string="Repository Understood",
        default=False,
    )

    # -------------------------------------------------------------------------
    # STAGE 2 — QC Checklist
    # =========================================================================
    validation_status = fields.Selection(
        selection=AUTOMATION_STATUS_SELECTION,
        string="Validation Status",
        default="pending",
    )
    validation_details = fields.Text(
        string="Validation Report",
    )
    # Critical Gates (MUST)
    check_language = fields.Boolean(
        string="Language",
        help="95%% Python, No C/Rust/Extensions",
    )
    check_tests = fields.Boolean(
        string="Tests",
        help="Pytest, <30m, No GPU",
    )
    check_documentation = fields.Boolean(
        string="Documentation",
        help="API Ref, Guide, Type Specs",
    )

    # Quality Indicators (SHOULD)
    check_github_metrics = fields.Boolean(
        string="Good GitHub Metrics",
        help="5k+ Stars, Not Fork/Archived",
    )
    check_project_structure = fields.Boolean(
        string="Proper Structure",
        help="src/ layout, Installable",
    )
    check_build = fields.Boolean(
        string="Clean Build",
        help="Docker Clean, No system pkgs",
    )

    # Code & Reliability (CHECK)
    check_code_quality = fields.Boolean(
        string="Code Quality",
        help="Parses, Separate src/tests",
    )
    check_reliability = fields.Boolean(
        string="Reliable Tests",
        help="Not Flaky, No Network, No Side Effects",
    )
    check_complexity = fields.Boolean(
        string="Reasonable Size",
        help="50-500 Funcs, No Circular Imports",
    )

    # QC Result
    repo_status = fields.Selection(
        selection=[
            ("pending", "Pending"),
            ("pass", "Pass"),
            ("fail", "Fail"),
        ],
        string="Repo Status",
        default="pending",
    )
    failure_reason = fields.Text(
        string="Failure Reason",
    )

    # =========================================================================
    # STAGE 3 — Automated Preparation
    # =========================================================================
    fork_status = fields.Selection(
        selection=AUTOMATION_STATUS_SELECTION,
        string="Fork Status",
        default="pending",
    )
    reference_commit_status = fields.Selection(
        selection=AUTOMATION_STATUS_SELECTION,
        string="Reference Commit Status",
        default="pending",
    )
    reference_commit_progress = fields.Float(
        string="Reference Commit Progress",
        default=0.0,
    )
    document_create_status = fields.Selection(
        selection=AUTOMATION_STATUS_SELECTION,
        string="Document Create Status",
        default="pending",
    )
    document_create_progress = fields.Float(
        string="Document Create Progress",
        default=0.0,
    )
    reference_commit = fields.Char(
        string="Reference Commit",
    )
    base_commit = fields.Char(
        string="Base Commit",
    )
    stage3_complete = fields.Boolean(
        string="Stage 3 Complete",
        compute="_compute_stage3_complete",
        store=True,
    )
    src_dir = fields.Char(
        string="Source Directory",
    )
    test_dir = fields.Char(
        string="Test Directory",
    )
    python_version = fields.Char(
        string="Python Version",
    )
    install_cmd = fields.Char(
        string="Install Command",
    )
    specs_dir = fields.Char(
        string="Specs Directory",
    )

    # =========================================================================
    # STAGE 4 — Spec Document Review
    # =========================================================================
    spec_doc_format = fields.Selection(
        selection=[
            ("pdf", "PDF"),
            ("json", "JSON"),
            ("yaml", "YAML"),
        ],
        string="Spec Document Format",
        default="pdf",
    )
    spec_pdf = fields.Binary(
        string="Spec PDF",
        attachment=True,
    )
    spec_pdf_filename = fields.Char(
        string="Spec PDF Filename",
    )
    spec_json = fields.Text(
        string="Spec JSON",
    )
    spec_yaml = fields.Text(
        string="Spec YAML",
    )
    document_file = fields.Binary(
        string="Upload PDF",
    )
    document_filename = fields.Char(
        string="Document Filename",
    )
    doc_check_related = fields.Boolean(
        string="Related to Repo",
    )
    doc_check_not_blank = fields.Boolean(
        string="Not Blank PDF",
    )
    doc_check_meaningful = fields.Boolean(
        string="Meaningful Content",
    )
    doc_valid = fields.Boolean(
        string="Document Valid",
        compute="_compute_doc_valid",
        store=True,
    )

    # =========================================================================
    # STAGE 5 — Stub Code Review
    # =========================================================================
    clone_path_original = fields.Char(
        string="Clone Path (Original)",
    )
    clone_path_stubbed = fields.Char(
        string="Clone Path (Stubbed)",
    )
    stub_status = fields.Selection(
        selection=AUTOMATION_STATUS_SELECTION,
        string="Stub Status",
        default="pending",
    )
    stub_proper = fields.Selection(
        selection=[
            ("pending", "Pending"),
            ("yes", "Yes"),
            ("no", "No"),
        ],
        string="Stub Proper",
        default="pending",
    )
    stub_failure_reason = fields.Text(
        string="Stub Failure Reason",
    )

    # =========================================================================
    # STAGE 6 — Docker Generation
    # =========================================================================
    docker_status = fields.Selection(
        selection=DOCKER_STATUS_SELECTION,
        string="Docker Status",
        default="pending",
    )
    docker_progress = fields.Float(
        string="Docker Progress",
        default=0.0,
    )
    docker_image_arm = fields.Char(
        string="Docker Image (ARM)",
    )
    docker_image_amd = fields.Char(
        string="Docker Image (AMD)",
    )
    ecr_url = fields.Char(
        string="ECR URL",
    )

    # =========================================================================
    # LOGGING
    # =========================================================================
    log_output = fields.Text(
        string="Log Output",
    )
    error_message = fields.Text(
        string="Error Message",
    )

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("commit0.repo.evaluation")
                    or "New"
                )
        return super().create(vals_list)

    # -------------------------------------------------------------------------
    # Computed Fields
    # -------------------------------------------------------------------------
    @api.depends("repo_url")
    def _compute_repo_name(self):
        for rec in self:
            if rec.repo_url and not rec.repo_name:
                url = rec.repo_url.strip().rstrip("/").replace(".git", "")
                if "github.com/" in url:
                    rec.repo_name = url.split("/")[-1]

    @api.depends("fork_status", "reference_commit_status", "document_create_status")
    def _compute_stage3_complete(self):
        for rec in self:
            rec.stage3_complete = (
                rec.fork_status == "done"
                and rec.reference_commit_status == "done"
                and rec.document_create_status == "done"
            )

    @api.depends("doc_check_related", "doc_check_not_blank", "doc_check_meaningful")
    def _compute_doc_valid(self):
        for rec in self:
            rec.doc_valid = (
                rec.doc_check_related
                and rec.doc_check_not_blank
                and rec.doc_check_meaningful
            )

    # -------------------------------------------------------------------------
    # Stage 1 Actions
    # -------------------------------------------------------------------------
    def action_clone_repo(self):
        """Clone the repository so the tasker can browse its files."""
        self.ensure_one()
        if not self.repo_url:
            raise UserError("GitHub URL is required before cloning.")
        if (
            self.clone_status == "done"
            and self.clone_path
            and os.path.isdir(self.clone_path)
        ):
            return

        self.write({"clone_status": "running"})
        url = self.repo_url.strip().rstrip("/")
        if not url.endswith(".git"):
            url += ".git"

        clone_dir = os.path.join(
            tempfile.gettempdir(), "kaiju_clones", self.name or str(self.id)
        )
        os.makedirs(os.path.dirname(clone_dir), exist_ok=True)

        try:
            if os.path.isdir(clone_dir):
                _logger.info("Re-using existing clone at %s", clone_dir)
            else:
                _logger.info("Cloning %s → %s", url, clone_dir)
                subprocess.run(
                    ["git", "clone", "--depth", "1", url, clone_dir],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            self.write(
                {
                    "clone_path": clone_dir,
                    "clone_status": "done",
                }
            )
        except subprocess.CalledProcessError as e:
            _logger.error("Clone failed: %s", e.stderr)
            self.write(
                {
                    "clone_status": "failed",
                    "error_message": f"Clone failed: {e.stderr[:500]}",
                }
            )
            raise UserError(f"Failed to clone repository:\n{e.stderr[:500]}")
        except subprocess.TimeoutExpired:
            self.write(
                {
                    "clone_status": "failed",
                    "error_message": "Clone timed out after 300 seconds.",
                }
            )
            raise UserError("Clone timed out after 300 seconds.")

    def action_confirm_understanding(self):
        """Confirm repository understanding and advance to Stage 2."""
        self.ensure_one()
        if not self.repo_url:
            raise UserError("GitHub URL is required before proceeding.")
        if not self.repo_understood:
            raise UserError(
                "You must mark the repository as understood before proceeding."
            )
        self.write({"current_stage": "stage2"})

    # -------------------------------------------------------------------------
    # Stage 2 Actions
    # -------------------------------------------------------------------------
    def _extract_full_name(self):
        """Extract 'owner/repo' from GitHub URL."""
        url = (self.repo_url or "").strip().rstrip("/")
        url = url.replace(".git", "")
        if "github.com/" in url:
            parts = url.split("github.com/")[-1].split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
        raise UserError(
            "Cannot parse GitHub URL. Expected format: https://github.com/owner/repo"
        )

    def action_validate_repo(self):
        """Run Filter 1 + Filter 2 from repo_validator and auto-fill checklist."""
        self.ensure_one()
        if not self.repo_url:
            raise UserError("GitHub URL is required before validation.")

        self.write({"validation_status": "running"})

        full_name = self._extract_full_name()
        github_token = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("commit0_pipeline.github_token", "")
        )

        try:
            from . import pipeline_executor

            pipeline_executor._ensure_tools_on_path()
            from tools.repo_validator import validate_repo, format_validation_report

            result = validate_repo(full_name, github_token=github_token)
        except Exception as e:
            self.write(
                {
                    "validation_status": "failed",
                    "error_message": str(e)[:1000],
                }
            )
            raise UserError(f"Validation failed: {e}")

        checks_by_name = {name: passed for name, passed, _ in result.get("checks", [])}

        self.write(
            {
                "validation_status": "done",
                "validation_details": format_validation_report(result),
                "check_language": (
                    checks_by_name.get("Python >= 95%%", False)
                    and checks_by_name.get("No native extensions", False)
                    and checks_by_name.get("Not native wrapper", False)
                ),
                "check_tests": (
                    checks_by_name.get("[MUST] Uses pytest", False)
                    and checks_by_name.get("[MUST] No GPU usage", False)
                ),
                "check_documentation": checks_by_name.get("Has documentation", False),
                "check_github_metrics": (
                    checks_by_name.get("Stars >= 3000", False)
                    and checks_by_name.get("Not a fork", False)
                    and checks_by_name.get("Not archived", False)
                    and checks_by_name.get("Not ML framework", False)
                ),
                "check_project_structure": (
                    checks_by_name.get("Project structure", False)
                    or checks_by_name.get("[SHOULD] Project structure", False)
                ),
                "check_build": checks_by_name.get("[SHOULD] Installable", False),
                "check_code_quality": checks_by_name.get("Code quality (basic)", False),
                "check_reliability": checks_by_name.get("[INFO] Test isolation", False),
                "check_complexity": (
                    checks_by_name.get("[INFO] Dependency count", False)
                    and checks_by_name.get("Repository size", False)
                ),
                "repo_status": "pass" if result.get("passed") else "fail",
            }
        )

    def action_stage2_pass(self):
        """Pass QC checklist and advance to Stage 3."""
        self.ensure_one()
        if self.repo_status != "pass":
            raise UserError("Repository status must be set to 'Pass' before advancing.")
        self.write({"current_stage": "stage3"})
        self.action_start_automation()

    def action_stage2_fail(self):
        """Fail QC checklist — repository is not suitable."""
        self.ensure_one()
        if self.repo_status != "fail":
            raise UserError("Repository status must be set to 'Fail' before rejecting.")
        if not self.failure_reason:
            raise UserError("A failure reason is required when rejecting a repository.")
        self.write(
            {
                "terminal_state": "repo_not_suitable",
                "current_stage": "failed",
            }
        )

    # -------------------------------------------------------------------------
    # Stage 3 Actions
    # -------------------------------------------------------------------------
    def action_start_automation(self):
        """Kick off asynchronous Stage 3 automation tasks."""
        self.ensure_one()
        self.write(
            {
                "fork_status": "pending",
                "reference_commit_status": "pending",
                "document_create_status": "pending",
            }
        )
        from . import pipeline_executor

        rec_id = self.id
        dbname = self.env.cr.dbname
        uid = self.env.uid
        self.env.cr.postcommit.add(
            lambda: pipeline_executor.submit_stage3_async(dbname, uid, rec_id)
        )

    def action_advance_to_stage4(self):
        """Advance to Stage 4 once all Stage 3 tasks are complete."""
        self.ensure_one()
        if not self.stage3_complete:
            raise UserError(
                "All Stage 3 automation tasks must be complete before advancing."
            )
        self.write({"current_stage": "stage4"})

    # -------------------------------------------------------------------------
    # Stage 4 Actions
    # -------------------------------------------------------------------------
    def action_reject_spec(self):
        """Reject the spec document — evaluation fails."""
        self.ensure_one()
        self.write(
            {
                "terminal_state": "rejected",
                "current_stage": "failed",
            }
        )

    def action_trigger_stubbing(self):
        """Trigger stub generation once the document is validated."""
        self.ensure_one()
        if not self.doc_valid:
            raise UserError(
                "The spec document must be fully validated "
                "(related, not blank, meaningful) before triggering stubbing."
            )
        self.write(
            {
                "current_stage": "stage5",
                "stub_status": "pending",
            }
        )
        from . import pipeline_executor

        rec_id = self.id
        dbname = self.env.cr.dbname
        uid = self.env.uid
        self.env.cr.postcommit.add(
            lambda: pipeline_executor.submit_stub_async(dbname, uid, rec_id)
        )

    # -------------------------------------------------------------------------
    # Stage 5 Actions
    # -------------------------------------------------------------------------
    def action_stub_approve(self):
        """Approve stubs and advance to Stage 6."""
        self.ensure_one()
        if self.stub_proper != "yes":
            raise UserError(
                "Stubs must be marked as properly generated before approving."
            )
        self.write({"current_stage": "stage6"})
        self.action_start_docker()

    def action_stub_reject(self):
        """Reject stubs — evaluation fails."""
        self.ensure_one()
        if self.stub_proper != "no":
            raise UserError(
                "Stubs must be marked as 'No' (not proper) before rejecting."
            )
        if not self.stub_failure_reason:
            raise UserError("A stub failure reason is required when rejecting stubs.")
        self.write(
            {
                "terminal_state": "not_stubbed",
                "current_stage": "failed",
            }
        )

    def action_submit(self):
        """Submit the evaluation (alias for stub approval)."""
        self.ensure_one()
        self.action_stub_approve()

    # -------------------------------------------------------------------------
    # Stage 6 Actions
    # -------------------------------------------------------------------------
    def action_start_docker(self):
        """Kick off Docker image generation."""
        self.ensure_one()
        self.write({"docker_status": "generating"})
        from . import pipeline_executor

        rec_id = self.id
        dbname = self.env.cr.dbname
        uid = self.env.uid
        self.env.cr.postcommit.add(
            lambda: pipeline_executor.submit_docker_async(dbname, uid, rec_id)
        )
