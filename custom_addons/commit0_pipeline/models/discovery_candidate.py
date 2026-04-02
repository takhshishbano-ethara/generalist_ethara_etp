# -*- coding: utf-8 -*-
from odoo import fields, models


class Commit0DiscoveryCandidate(models.Model):
    _name = "commit0.discovery.candidate"
    _description = "Commit0 Discovery Candidate"
    _order = "stars desc, id"

    pipeline_run_id = fields.Many2one(
        "commit0.pipeline.run",
        string="Pipeline Run",
        required=True,
        ondelete="cascade",
        index=True,
    )
    full_name = fields.Char(
        string="Repository",
        help="Full repository name in owner/repo format.",
    )
    stars = fields.Integer(
        string="Stars",
    )
    python_pct = fields.Float(
        string="Python %",
    )
    has_pytest = fields.Boolean(
        string="Has Pytest",
    )
    license = fields.Char(
        string="License",
    )
    description = fields.Text(
        string="Description",
    )
    release_tag = fields.Char(
        string="Release Tag",
    )
    selected = fields.Boolean(
        string="Selected",
        default=False,
    )
    validation_status = fields.Selection(
        selection=[
            ("pending", "Pending"),
            ("pass", "Pass"),
            ("fail", "Fail"),
        ],
        string="Validation Status",
        default="pending",
    )
    validation_issues = fields.Text(
        string="Validation Issues",
    )
