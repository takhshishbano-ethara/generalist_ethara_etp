# -*- coding: utf-8 -*-
from odoo import api, fields, models


ENTRY_STATE_SELECTION = [
    ("pending", "Pending"),
    ("preparing", "Preparing"),
    ("forking", "Forking"),
    ("cloning", "Cloning"),
    ("stubbing", "Stubbing"),
    ("pushing", "Pushing"),
    ("dataset_created", "Dataset Created"),
    ("tests_generated", "Tests Generated"),
    ("setup_done", "Setup Done"),
    ("built", "Built"),
    ("complete", "Complete"),
    ("failed", "Failed"),
]


class Commit0RepoEntry(models.Model):
    _name = "commit0.repo.entry"
    _description = "Commit0 Repository Entry"
    _order = "sequence, id"

    name = fields.Char(
        string="Name",
        compute="_compute_name",
        store=True,
    )
    pipeline_run_id = fields.Many2one(
        "commit0.pipeline.run",
        string="Pipeline Run",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(
        string="Sequence",
        default=10,
    )

    # --- Repository Info ---
    repo_name = fields.Char(
        string="Repository Name",
    )
    repo_url = fields.Char(
        string="Original URL",
    )
    fork_url = fields.Char(
        string="Fork URL",
    )
    state = fields.Selection(
        selection=ENTRY_STATE_SELECTION,
        string="State",
        default="pending",
    )

    # --- Git / Build Details ---
    base_commit = fields.Char(
        string="Base Commit",
    )
    reference_commit = fields.Char(
        string="Reference Commit",
    )
    src_dir = fields.Char(
        string="Source Directory",
    )
    test_dir = fields.Char(
        string="Test Directory",
    )
    test_count = fields.Integer(
        string="Test Count",
    )
    python_version = fields.Char(
        string="Python Version",
    )
    install_cmd = fields.Char(
        string="Install Command",
    )
    docker_image = fields.Char(
        string="Docker Image",
    )
    stubbing_mode = fields.Char(
        string="Stubbing Mode",
    )
    clone_path = fields.Char(
        string="Clone Path",
        help="Local filesystem path where the repository was cloned.",
    )

    # --- Output ---
    error_message = fields.Text(
        string="Error Message",
    )
    log_output = fields.Text(
        string="Log Output",
    )

    # -------------------------------------------------------------------------
    # Compute
    # -------------------------------------------------------------------------
    @api.depends("repo_name", "sequence")
    def _compute_name(self):
        for entry in self:
            if entry.repo_name:
                entry.name = entry.repo_name
            else:
                entry.name = "Repo #%s" % entry.sequence
