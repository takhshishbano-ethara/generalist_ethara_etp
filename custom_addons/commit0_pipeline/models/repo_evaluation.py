# -*- coding: utf-8 -*-
import difflib
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime

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
    ("image_broken", "Image Broken"),
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
        tracking=True,
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
    is_admin = fields.Boolean(
        compute="_compute_is_admin",
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
        help="80%% Python, No C/Rust/Extensions",
    )
    check_tests = fields.Boolean(
        string="Tests",
        help="Pytest, No GPU (timing optional)",
    )
    check_documentation = fields.Boolean(
        string="Documentation",
        help="Docs website (guide/API/types are SHOULD)",
    )

    # Quality Indicators (SHOULD)
    check_github_metrics = fields.Boolean(
        string="Good GitHub Metrics",
        help="2k+ Stars, Not Fork (archived optional)",
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
    test_ids_bz2 = fields.Binary(
        string="Test IDs (.bz2)",
        attachment=True,
    )
    test_ids_filename = fields.Char(
        string="Test IDs Filename",
    )
    test_ids_count = fields.Integer(
        string="Test IDs Count",
        default=0,
    )
    test_ids_status = fields.Selection(
        selection=AUTOMATION_STATUS_SELECTION,
        string="Test IDs Status",
        default="pending",
    )
    spec_json_editing = fields.Boolean(
        string="Editing Spec JSON",
        default=False,
    )
    spec_json_original = fields.Text(
        string="Spec JSON (Before Edit)",
    )
    spec_json_history = fields.Text(
        string="Spec JSON Change History",
    )
    show_spec_json_history = fields.Boolean(
        string="Show Change History",
        default=False,
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
    kaiju_build_id = fields.Many2one(
        "kaiju.build",
        string="Build",
        ondelete="set null",
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
    # Task Allocation
    # -------------------------------------------------------------------------
    @api.model
    def action_start_task(self):
        """Assign the oldest unassigned task to the current user."""
        max_active = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("commit0_pipeline.max_active_tasks", "1")
        )
        active_count = self.search_count(
            [
                ("user_id", "=", self.env.uid),
                ("current_stage", "not in", ("done", "failed")),
            ]
        )
        if active_count >= max_active:
            raise UserError(
                f"You already have {active_count} active task(s). "
                f"Maximum allowed: {max_active}. "
                "Complete or release a task before starting a new one."
            )
        task = self.sudo().search(
            [
                ("user_id", "=", False),
                ("current_stage", "not in", ("done", "failed")),
            ],
            order="id asc",
            limit=1,
        )
        if not task:
            raise UserError("No unassigned tasks available in the pool.")
        task.write({"user_id": self.env.uid})
        return {
            "type": "ir.actions.act_window",
            "res_model": "commit0.repo.evaluation",
            "res_id": task.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_release_task(self):
        """Release this task back to the unassigned pool."""
        self.ensure_one()
        self.write({"user_id": False})

    # -------------------------------------------------------------------------
    # Computed Fields
    # -------------------------------------------------------------------------
    def _compute_is_admin(self):
        admin_group = self.env.ref(
            "commit0_pipeline.group_commit0_admin", raise_if_not_found=False
        )
        is_admin = admin_group and admin_group in self.env.user.group_ids
        for rec in self:
            rec.is_admin = is_admin

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
                    checks_by_name.get("Python >= 80%%", False)
                    and checks_by_name.get("No native extensions", False)
                    and checks_by_name.get("Not native wrapper", False)
                ),
                "check_tests": (
                    checks_by_name.get("[MUST] Uses pytest", False)
                    and checks_by_name.get("[MUST] No GPU usage", False)
                ),
                "check_documentation": checks_by_name.get("Has documentation", False),
                "check_github_metrics": (
                    checks_by_name.get("Stars >= 2000", False)
                    and checks_by_name.get("Not a fork", False)
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
        """Kick off all Stage 3 tasks sequentially (legacy, kept for compatibility)."""
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

    def _submit_stage3_task(self, status_field, submit_fn):
        """Reset a Stage 3 sub-task status and submit it to the background executor."""
        self.ensure_one()
        if self.current_stage != "stage3":
            raise UserError("This action is only available during Stage 3.")
        self.write({status_field: "pending", "error_message": False})
        from . import pipeline_executor

        rec_id = self.id
        dbname = self.env.cr.dbname
        uid = self.env.uid
        submit = getattr(pipeline_executor, submit_fn)
        self.env.cr.postcommit.add(lambda: submit(dbname, uid, rec_id))

    def action_start_fork(self):
        self._submit_stage3_task("fork_status", "submit_fork_async")

    def action_retry_fork(self):
        self._submit_stage3_task("fork_status", "submit_fork_async")

    def action_start_reference_commit(self):
        self.ensure_one()
        if self.fork_status != "done":
            raise UserError("Fork must be completed before committing reference code.")
        self._submit_stage3_task(
            "reference_commit_status", "submit_reference_commit_async"
        )

    def action_retry_reference_commit(self):
        self._submit_stage3_task(
            "reference_commit_status", "submit_reference_commit_async"
        )

    def action_start_document_create(self):
        self.ensure_one()
        if self.reference_commit_status != "done":
            raise UserError(
                "Reference commit must be completed before creating the document."
            )
        self._submit_stage3_task(
            "document_create_status", "submit_document_create_async"
        )

    def action_retry_document_create(self):
        self._submit_stage3_task(
            "document_create_status", "submit_document_create_async"
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
    def action_generate_test_ids(self):
        """Trigger test-ID collection + bz2 generation as a background task."""
        self.ensure_one()
        if not self.clone_path:
            raise UserError(
                "Clone path is missing — Stage 3 must complete before generating test IDs."
            )
        self.write(
            {
                "test_ids_status": "pending",
                "error_message": False,
            }
        )
        from . import pipeline_executor

        rec_id = self.id
        dbname = self.env.cr.dbname
        uid = self.env.uid
        self.env.cr.postcommit.add(
            lambda: pipeline_executor.submit_test_ids_async(dbname, uid, rec_id)
        )

    def action_enable_edit_spec_json(self):
        """Enable editing mode for the spec JSON."""
        self.ensure_one()
        vals = {
            "spec_json_editing": True,
            "spec_json_original": self.spec_json_original or self.spec_json or "",
        }
        self.write(vals)

    def action_save_spec_json(self):
        """Save the edited spec JSON and disable editing mode."""
        self.ensure_one()
        old_json = self.spec_json_original or ""
        new_json = self.spec_json or ""
        history_entry = ""
        if old_json != new_json:
            diff_lines = list(
                difflib.unified_diff(
                    old_json.splitlines(keepends=True),
                    new_json.splitlines(keepends=True),
                    fromfile="before",
                    tofile="after",
                    lineterm="",
                )
            )
            diff_text = "\n".join(diff_lines) if diff_lines else "(no textual diff)"

            changed_keys = self._get_changed_json_keys(old_json, new_json)
            key_summary = ", ".join(changed_keys) if changed_keys else "unknown keys"

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user_name = self.env.user.name or "Unknown"
            separator = "═" * 60
            subseparator = "─" * 40
            entry = (
                "{sep}\n"
                "[{ts}] Edited by {user}\n"
                "Changed keys: {keys}\n"
                "{subsep}\n"
                "{diff}\n"
            ).format(
                sep=separator,
                ts=timestamp,
                user=user_name,
                keys=key_summary,
                subsep=subseparator,
                diff=diff_text,
            )

            existing = self.spec_json_history or ""
            history_entry = entry + "\n" + existing if existing else entry

        vals = {
            "spec_json_editing": False,
            "spec_json_original": new_json,
            "spec_json_history": history_entry or self.spec_json_history,
        }
        vals.update(self._sync_fields_from_spec_json(new_json))
        self.write(vals)

    def _sync_fields_from_spec_json(self, json_str):
        try:
            data = json.loads(json_str) if json_str and json_str.strip() else {}
        except (json.JSONDecodeError, AttributeError):
            return {}
        synced = {}
        setup = data.get("setup") or {}
        test = data.get("test") or {}
        if test.get("test_dir"):
            synced["test_dir"] = test["test_dir"]
        if setup.get("install"):
            synced["install_cmd"] = setup["install"]
        if data.get("src_dir"):
            synced["src_dir"] = data["src_dir"]
        if setup.get("python"):
            synced["python_version"] = setup["python"]
        return synced

    def action_toggle_spec_json_history(self):
        self.ensure_one()
        self.write({"show_spec_json_history": not self.show_spec_json_history})

    @staticmethod
    def _get_changed_json_keys(old_str, new_str):
        try:
            old_dict = json.loads(old_str) if old_str.strip() else {}
            new_dict = json.loads(new_str) if new_str.strip() else {}
        except (json.JSONDecodeError, AttributeError):
            return []
        changed = []
        all_keys = set(list(old_dict.keys()) + list(new_dict.keys()))
        for key in sorted(all_keys):
            if old_dict.get(key) != new_dict.get(key):
                changed.append(key)
        return changed

    def action_retry_generate_test_ids(self):
        """Retry test-ID generation (resets count and re-runs)."""
        self.ensure_one()
        self.write(
            {
                "test_ids_count": 0,
                "test_ids_bz2": False,
                "test_ids_filename": False,
                "test_ids_status": "pending",
            }
        )
        self.action_generate_test_ids()

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
        self.ensure_one()
        if not self.doc_valid:
            raise UserError(
                "The spec document must be fully validated "
                "(related, not blank, meaningful) before triggering stubbing."
            )
        if not self.base_commit:
            raise UserError(
                "Base commit (stubbed code) is missing — "
                "Stage 3 must complete before reviewing stubs."
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

    def action_rebuild_docker(self):
        self.ensure_one()
        if self.docker_status != "image_broken":
            raise UserError(
                "Rebuild is only available when Docker status is 'Image Broken'."
            )
        if not self.kaiju_build_id:
            raise UserError("No linked build found.")
        self.kaiju_build_id.write({"dataset_json": self.spec_json})
        self.kaiju_build_id.action_rebuild()
        self.invalidate_recordset()
        self.kaiju_build_id.invalidate_recordset()

        if self.kaiju_build_id.status == "error":
            self.write(
                {
                    "docker_status": "failed",
                    "error_message": self.kaiju_build_id.error_message
                    or "Build job creation failed",
                }
            )
            return

        self.write(
            {
                "docker_status": "generating",
                "docker_progress": 5.0,
                "error_message": False,
            }
        )
        from . import pipeline_executor

        rec_id = self.id
        dbname = self.env.cr.dbname
        uid = self.env.uid
        self.env.cr.postcommit.add(
            lambda: pipeline_executor.submit_docker_poll(dbname, uid, rec_id)
        )

    def action_reject_docker(self):
        self.ensure_one()
        if self.docker_status != "failed":
            raise UserError("Reject is only available when Docker status is 'Failed'.")
        self.write(
            {
                "current_stage": "failed",
                "terminal_state": "repo_not_suitable",
            }
        )
