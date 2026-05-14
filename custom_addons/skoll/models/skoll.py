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

    service_stack = fields.Char(string="Service Stack", readonly=True)

    heart_health = fields.Selection(
        [("high", "High"), ("medium", "Medium"), ("low", "Low")],
        string="Health", readonly=True,
    )
    heart_exploration = fields.Selection(
        [("high", "High"), ("medium", "Medium"), ("low", "Low")],
        string="Exploration", readonly=True,
    )
    heart_advice = fields.Selection(
        [("high", "High"), ("medium", "Medium"), ("low", "Low")],
        string="Advice", readonly=True,
    )
    heart_relationships = fields.Selection(
        [("high", "High"), ("medium", "Medium"), ("low", "Low")],
        string="Relationships", readonly=True,
    )
    heart_time = fields.Selection(
        [("high", "High"), ("medium", "Medium"), ("low", "Low")],
        string="Time", readonly=True,
    )

    personality_archetype = fields.Char(string="Personality Archetype", readonly=True)
    task_hooks = fields.Text(string="Task Hooks", readonly=True)
    difficulty_tags = fields.Char(string="Difficulty Tags", readonly=True)
    confirmation_threshold = fields.Integer(string="Confirmation Threshold", readonly=True)
    safety_scenarios = fields.Char(string="Safety Scenarios", readonly=True)
    spawned_agents = fields.Text(string="Spawned Agents", readonly=True)

    seed_prompt = fields.Text(
        string="Seed Prompt",
        readonly=True,
        help="Original seed prompt uploaded via JSONL. Not editable.",
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
