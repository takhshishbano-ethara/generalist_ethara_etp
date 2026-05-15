# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class Skoll(models.Model):
    _name = "skoll.skoll"
    _description = "Skoll Task"
    _rec_name = "task_id"
    _order = "create_date desc"

    task_id = fields.Char(
        string="Task ID",
        required=True,
        index=True,
        copy=False,
    )
    active = fields.Boolean(default=True)

    persona_id = fields.Many2one(
        "skoll.persona",
        string="Persona",
        ondelete="restrict",
    )

    _sql_constraints = [
        ("task_id_unique", "UNIQUE(task_id)", "Task ID must be unique."),
    ]

    mode = fields.Selection(
        [("manual", "Manual"), ("ai", "AI")],
        string="Mode",
        default="manual",
        required=True,
        help="Manual = free-text editing. AI = content generated via LLM (read-only).",
    )

    life_domain_ids = fields.Many2many(
        "skoll.tag.life_domain",
        "skoll_task_life_domain_rel", "task_id", "tag_id",
        string="Life Domain", readonly=True,
    )
    cluster_ids = fields.Many2many(
        "skoll.tag.cluster",
        "skoll_task_cluster_rel", "task_id", "tag_id",
        string="Cluster", readonly=True,
    )
    task_type_ids = fields.Many2many(
        "skoll.tag.task_type",
        "skoll_task_type_rel", "task_id", "tag_id",
        string="Task Type", readonly=True,
    )
    pattern_taxonomy_ids = fields.Many2many(
        "skoll.tag.pattern_taxonomy",
        "skoll_task_pattern_rel", "task_id", "tag_id",
        string="Pattern Taxonomy", readonly=True,
    )

    spawned_agents = fields.Text(string="Spawned Agents", readonly=True)

    credential = fields.Char(string="Credential", readonly=True)
    password = fields.Char(string="Password", readonly=True)
    prerequisites = fields.Text(string="Prerequisites", readonly=True)

    seed_prompt = fields.Text(
        string="Seed Prompt",
        readonly=True,
    )

    agent_md = fields.Text(string="Agent MD")
    soul_md = fields.Text(string="Soul MD")
    memory_md = fields.Text(string="Memory MD")

    prompt = fields.Text(
        string="Prompt",
        help="User prompt sent to the LLM when generating content in AI mode.",
    )

    content = fields.Text(
        string="Golden Trajectory",
        help="Golden trajectory JSON. Editable in Manual mode, LLM-generated in AI mode.",
    )

    qc_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("pass", "Pass"),
            ("fail", "Fail"),
            ("needs_revision", "Needs Revision"),
            ("error", "Error"),
        ],
        string="QC Status",
        default="pending",
        readonly=True,
        help="Quality control status of the golden trajectory.",
    )
    qc_result = fields.Text(
        string="QC Review",
        readonly=True,
        help="LLM-generated quality control review (Kimi K2.5).",
    )
    qc_structural_result = fields.Text(
        string="Structural Validation",
        readonly=True,
        help="Deterministic structural validation output.",
    )

    employee_ids = fields.Many2many(
        "hr.employee",
        "skoll_task_employee_rel",
        "task_id",
        "employee_id",
        string="Assigned Employees",
    )

    is_skoll_admin = fields.Boolean(
        string="Is Admin",
        compute="_compute_is_skoll_admin",
        search="_search_is_skoll_admin",
    )

    is_ql_or_pl = fields.Boolean(
        string="Is QL or PL",
        compute="_compute_is_ql_or_pl",
    )

    @api.depends_context("uid")
    def _compute_is_skoll_admin(self):
        is_admin = self.env.user.has_group("skoll.group_skoll_pl")
        for rec in self:
            rec.is_skoll_admin = is_admin

    def _search_is_skoll_admin(self, operator, value):
        is_admin = self.env.user.has_group("skoll.group_skoll_pl")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [(1, "=", 1)] if is_admin else [(0, "=", 1)]
        return [(0, "=", 1)] if is_admin else [(1, "=", 1)]

    @api.depends_context("uid")
    def _compute_is_ql_or_pl(self):
        is_ql = self.env.user.has_group("skoll.group_skoll_ql")
        for rec in self:
            rec.is_ql_or_pl = is_ql
