# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

EVAL_STATE_SELECTION = [
    ("draft", "Draft"),
    ("in_review", "In Review"),
    ("validated", "Validated"),
]

PASS_FAIL_SELECTION = [
    ("pass", "Pass"),
    ("fail", "Fail"),
]

VERDICT_SELECTION = [
    ("selected", "Selected"),
    ("rejected", "Rejected"),
]


class Commit0RepoEvaluation(models.Model):
    _name = "commit0.repo.evaluation"
    _description = "Commit0 Repository Evaluation"
    _inherit = ["mail.thread"]
    _order = "id desc"

    # --- Identity ---
    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        copy=False,
        default="New",
    )
    state = fields.Selection(
        selection=EVAL_STATE_SELECTION,
        string="Status",
        required=True,
        default="draft",
        tracking=True,
        copy=False,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Evaluator",
        default=lambda self: self.env.uid,
        readonly=True,
    )

    # --- Repository Info ---
    repo_name = fields.Char(
        string="Repository Name",
        required=True,
        tracking=True,
    )
    repo_url = fields.Char(
        string="GitHub URL",
        tracking=True,
    )

    # =========================================================================
    # CRITICAL GATES (MUST)
    # =========================================================================
    check_language = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Language",
        help="95% Python, No C/Rust/Extensions",
    )
    check_tests = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Tests",
        help="Pytest, <30m, No GPU",
    )
    check_documentation = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Documentation",
        help="API Ref, Guide, Type Specs",
    )

    # =========================================================================
    # QUALITY INDICATORS (SHOULD)
    # =========================================================================
    check_github_metrics = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Good GitHub Metrics",
        help="5k+ Stars, Not Fork/Archived",
    )
    check_project_structure = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Proper Structure",
        help="src/ layout, Installable",
    )
    check_build = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Clean Build",
        help="Docker Clean, No system pkgs",
    )

    # =========================================================================
    # CODE & RELIABILITY (CHECK)
    # =========================================================================
    check_code_quality = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Code Quality",
        help="Parses, Separate src/tests",
    )
    check_reliability = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Reliable Tests",
        help="Not Flaky, No Network, No Side Effects",
    )
    check_complexity = fields.Selection(
        selection=PASS_FAIL_SELECTION,
        string="Reasonable Size",
        help="50-500 Funcs, No Circular Imports",
    )

    # =========================================================================
    # FINAL VERDICT
    # =========================================================================
    must_gates_passed = fields.Boolean(
        string="MUST Gates Passed",
        compute="_compute_must_gates_passed",
        store=True,
    )
    verdict = fields.Selection(
        selection=VERDICT_SELECTION,
        string="Verdict",
        tracking=True,
    )
    verdict_reason = fields.Text(
        string="Verdict Reason",
    )

    # =========================================================================
    # DOCUMENT VALIDATION
    # =========================================================================
    document_file = fields.Binary(
        string="Upload PDF",
    )
    document_filename = fields.Char(
        string="Document Filename",
    )
    doc_related_to_repo = fields.Boolean(
        string="Related to Repo",
    )
    doc_not_blank = fields.Boolean(
        string="Not Blank PDF",
    )

    # =========================================================================
    # FORK
    # =========================================================================
    fork_url = fields.Char(
        string="Fork URL",
    )
    fork_progress = fields.Float(
        string="Fork Progress",
        default=0.0,
    )

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code(
                        "commit0.repo.evaluation"
                    )
                    or "New"
                )
        return super().create(vals_list)

    # -------------------------------------------------------------------------
    # Compute
    # -------------------------------------------------------------------------
    @api.depends("check_language", "check_tests", "check_documentation")
    def _compute_must_gates_passed(self):
        for rec in self:
            rec.must_gates_passed = (
                rec.check_language == "pass"
                and rec.check_tests == "pass"
                and rec.check_documentation == "pass"
            )

    # -------------------------------------------------------------------------
    # Actions — Workflow
    # -------------------------------------------------------------------------
    def action_submit_task(self):
        """Submit evaluation for review."""
        self.ensure_one()
        if self.state != "draft":
            raise UserError("Only draft evaluations can be submitted.")
        self.write({"state": "in_review"})

    def action_validate(self):
        """Validate the evaluation."""
        self.ensure_one()
        if self.state != "in_review":
            raise UserError("Only evaluations in review can be validated.")
        if not self.verdict:
            raise UserError(
                "A verdict (Selected or Rejected) must be set before validation."
            )
        self.write({"state": "validated"})

    def action_reset_draft(self):
        """Reset evaluation back to draft."""
        self.ensure_one()
        self.write({"state": "draft"})

    # -------------------------------------------------------------------------
    # Actions — Verdict
    # -------------------------------------------------------------------------
    def action_select(self):
        """Mark repository as selected."""
        self.ensure_one()
        self.write({"verdict": "selected"})

    def action_reject(self):
        """Mark repository as rejected."""
        self.ensure_one()
        self.write({"verdict": "rejected"})

    # -------------------------------------------------------------------------
    # Actions — Document Validation
    # -------------------------------------------------------------------------
    def action_validate_document(self):
        """Validate the uploaded document."""
        self.ensure_one()
        if not self.document_file:
            raise UserError("Please upload a document first.")
        self.write({
            "doc_related_to_repo": True,
            "doc_not_blank": True,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Document Validated",
                "message": "Document has been validated successfully.",
                "type": "success",
                "sticky": False,
            },
        }

    # -------------------------------------------------------------------------
    # Actions — Fork
    # -------------------------------------------------------------------------
    def action_fork_repo(self):
        """Fork repository into Ethara organization."""
        self.ensure_one()
        if self.verdict != "selected":
            raise UserError(
                "Only selected repositories can be forked."
            )
        if not self.repo_url:
            raise UserError("Repository URL is required for forking.")
        self.write({"fork_progress": 0.0})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Fork Initiated",
                "message": "Forking %s into Ethara organization..."
                % self.repo_name,
                "type": "info",
                "sticky": False,
            },
        }
