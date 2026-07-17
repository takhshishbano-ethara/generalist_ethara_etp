from odoo import models, fields, api, _


class Kensei2Persona(models.Model):
    """Tracker extension of kensei2.persona — allocation status.

    NB: the core model's `task_ids` points at kensei2.kensei2 (a different
    model). These are the TRACKER allocations. A task runs through several
    stages and every stage record carries the same persona, so
    `pt_allocation_ids` holds the whole chain; `pt_current_allocation_id` is the
    stage the task is actually sitting in (is_current_stage), which is what
    the Persona list should report.
    """

    _inherit = "kensei2.persona"

    # The project this persona belongs to. A persona lives under ONE project
    # (the Tracker slices personas by project); its allocations must stay within
    # that project. Backfilled from the current allocation on upgrade.
    pt_project_id = fields.Many2one(
        "project.tracker.project", string="Project", index=True,
        help="Project this persona belongs to. Only this project's allocations "
             "may use it.")
    # RLHF projects describe a persona by Domain + Agent Type rather than the
    # SFT L1/L2 taxonomy. Both sets live on the model; the project's type decides
    # which the UI surfaces (see project_tracker_project_views).
    pt_domain = fields.Char(
        string="Domain", index=True,
        help="RLHF taxonomy — the persona's domain (e.g. coding, math).")
    pt_agent_type = fields.Char(
        string="Agent Type",
        help="RLHF taxonomy — the persona's agent type.")
    pt_allocation_ids = fields.One2many(
        "project.tracker.allocation", "persona_id", string="Task Allocations")
    pt_allocation_count = fields.Integer(
        string="Allocations", compute="_compute_pt_tracker_allocation", store=True)
    pt_assignment_status = fields.Selection(
        [("unassigned", "Unassigned"), ("assigned", "Assigned")],
        string="Assignment", compute="_compute_pt_tracker_allocation",
        store=True, index=True, default="unassigned",
        help="Assigned = this persona is allocated to at least one task.",
    )
    pt_current_allocation_id = fields.Many2one(
        "project.tracker.allocation", string="Current Allocation",
        compute="_compute_pt_tracker_allocation", store=True,
    )
    # Related + store=True so they are searchable / groupable / sortable in the
    # list, and so the Selection labels are inherited from the allocation instead
    # of being duplicated here (they can never drift out of sync).
    pt_current_task_ref = fields.Char(
        related="pt_current_allocation_id.task_id", string="Task ID", store=True)
    pt_current_tasker_id = fields.Many2one(
        related="pt_current_allocation_id.tasker_member_id", string="Tasker",
        store=True, index=True)
    pt_current_pl_id = fields.Many2one(
        related="pt_current_allocation_id.assigned_pl_id", string="PL", store=True)
    pt_current_status = fields.Selection(
        related="pt_current_allocation_id.status", string="Task Status",
        store=True, index=True)
    pt_current_stage = fields.Char(
        string="Stage", compute="_compute_pt_tracker_allocation", store=True,
        help="Which stage of the task this persona is currently in.")

    @api.depends("pt_allocation_ids", "pt_allocation_ids.is_current_stage",
                 "pt_allocation_ids.stage_no", "pt_allocation_ids.total_stages")
    def _compute_pt_tracker_allocation(self):
        for rec in self:
            allocs = rec.pt_allocation_ids
            rec.pt_allocation_count = len(allocs)
            rec.pt_assignment_status = "assigned" if allocs else "unassigned"
            # the live stage; fall back to any allocation so the row is never blank
            current = allocs.filtered("is_current_stage")[:1] or allocs[:1]
            rec.pt_current_allocation_id = current
            rec.pt_current_stage = (
                "%s / %s" % (current.stage_no, current.total_stages)
                if current else False
            )

    def action_view_pt_allocations(self):
        """Open the Task Allocations this persona is used in (all stages)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("%s — Task Allocations", self.name),
            "res_model": "project.tracker.allocation",
            "domain": [("persona_id", "=", self.id)],
            "view_mode": "list,form",
            "context": {"default_persona_id": self.id},
        }
