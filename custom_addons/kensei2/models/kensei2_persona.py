from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class Kensei2Persona(models.Model):
    _name = "kensei2.persona"
    _description = "Kensei2 Persona"
    _order = "name"

    is_kensei2_admin = fields.Boolean(
        compute="_compute_is_kensei2_admin",
        search="_search_is_kensei2_admin",
    )

    @api.depends_context("uid")
    def _compute_is_kensei2_admin(self):
        is_admin = self.env.user.has_group("kensei2.group_kensei2_ql")
        for rec in self:
            rec.is_kensei2_admin = is_admin

    def _search_is_kensei2_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("kensei2.group_kensei2_ql")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)

    soul_md = fields.Text(string="SOUL.md")
    memory_md = fields.Text(string="MEMORY.md")
    agents_md = fields.Text(string="AGENTS.md")

    litellm_config_yaml = fields.Text(
        string="LiteLLM Config (YAML)",
        help="Per-persona litellm_config.yaml content. "
        "If empty, the global default from Kensei2 settings is used.",
    )
    docker_compose_yaml = fields.Text(
        string="Docker Compose (YAML)",
        help="Per-persona docker-compose.yml content. "
        "If empty, the bundled default from the module is used.",
    )

    task_ids = fields.One2many("kensei2.kensei2", "persona_id", string="Tasks")
    task_count = fields.Integer(compute="_compute_task_count")

    @api.depends("task_ids")
    def _compute_task_count(self):
        for rec in self:
            rec.task_count = len(rec.task_ids)

    l1_category = fields.Char(string="L1 Category")
    l2_category = fields.Char(string="L2 Category")

    # ------------------------------------------------------------------ #
    #  Kensei2 Tracker — allocation status
    # ------------------------------------------------------------------ #
    # NB: `task_ids` above points at kensei2.kensei2 (a different model). These are
    # the TRACKER allocations. A task runs through several stages and every stage
    # record carries the same persona, so `allocation_ids` holds the whole chain;
    # `current_allocation_id` is the stage the task is actually sitting in
    # (is_current_stage), which is what the Persona list should report.
    allocation_ids = fields.One2many(
        "kensei2.tracker.allocation", "persona_id", string="Task Allocations")
    allocation_count = fields.Integer(
        string="Allocations", compute="_compute_tracker_allocation", store=True)
    assignment_status = fields.Selection(
        [("unassigned", "Unassigned"), ("assigned", "Assigned")],
        string="Assignment", compute="_compute_tracker_allocation",
        store=True, index=True, default="unassigned",
        help="Assigned = this persona is allocated to at least one task.",
    )
    current_allocation_id = fields.Many2one(
        "kensei2.tracker.allocation", string="Current Allocation",
        compute="_compute_tracker_allocation", store=True,
    )
    # Related + store=True so they are searchable / groupable / sortable in the
    # list, and so the Selection labels are inherited from the allocation instead
    # of being duplicated here (they can never drift out of sync).
    current_task_ref = fields.Char(
        related="current_allocation_id.task_id", string="Task ID", store=True)
    current_tasker_id = fields.Many2one(
        related="current_allocation_id.tasker_member_id", string="Tasker",
        store=True, index=True)
    current_pl_id = fields.Many2one(
        related="current_allocation_id.assigned_pl_id", string="PL", store=True)
    current_status = fields.Selection(
        related="current_allocation_id.status", string="Task Status",
        store=True, index=True)
    current_stage = fields.Char(
        string="Stage", compute="_compute_tracker_allocation", store=True,
        help="Which stage of the task this persona is currently in.")

    @api.depends("allocation_ids", "allocation_ids.is_current_stage",
                 "allocation_ids.stage_no", "allocation_ids.total_stages")
    def _compute_tracker_allocation(self):
        for rec in self:
            allocs = rec.allocation_ids
            rec.allocation_count = len(allocs)
            rec.assignment_status = "assigned" if allocs else "unassigned"
            # the live stage; fall back to any allocation so the row is never blank
            current = allocs.filtered("is_current_stage")[:1] or allocs[:1]
            rec.current_allocation_id = current
            rec.current_stage = (
                "%s / %s" % (current.stage_no, current.total_stages)
                if current else False
            )

    def action_view_allocations(self):
        """Open the Task Allocations this persona is used in (all stages)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("%s — Task Allocations", self.name),
            "res_model": "kensei2.tracker.allocation",
            "domain": [("persona_id", "=", self.id)],
            "view_mode": "list,form",
            "context": {"default_persona_id": self.id},
        }

    @api.constrains("name")
    def _check_name(self):
        for rec in self:
            if not rec.name or not rec.name.strip():
                raise ValidationError("Persona name cannot be empty.")
            sanitized = rec.name.strip().lower().replace(" ", "-")
            existing = self.search(
                [("name", "=ilike", sanitized), ("id", "!=", rec.id)], limit=1
            )
            if existing:
                raise ValidationError(
                    "A persona with name '%s' already exists." % sanitized
                )

    def write(self, vals):
        if "name" in vals:
            vals["name"] = vals["name"].strip().lower().replace(" ", "-")
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "name" in vals:
                vals["name"] = vals["name"].strip().lower().replace(" ", "-")
        return super().create(vals_list)
