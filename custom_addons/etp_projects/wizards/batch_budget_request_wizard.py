from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EtpBatchBudgetRequestWizard(models.TransientModel):
    _name = "etp.batch.budget.request.wizard"
    _description = "Phase Budget Request Wizard"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
        required=True,
        ondelete="cascade",
    )
    project_budget_id = fields.Many2one(
        related="batch_id.project_budget_id",
        readonly=True,
    )
    project_id = fields.Many2one(
        related="batch_id.project_id",
        readonly=True,
    )
    project_remaining_amount = fields.Float(
        string="Project Remaining (USD)",
        related="batch_id.project_budget_id.allocatable_amount",
        readonly=True,
    )
    is_first_request = fields.Boolean(
        string="First Request",
        compute="_compute_is_first_request",
    )
    request_type = fields.Selection(
        [
            ("budget", "Budget"),
            ("new_model", "New Model"),
            ("topup", "Top-up"),
            ("device", "Device"),
        ],
        string="Request Type",
        default="budget",
        required=True,
    )
    parent_request_id = fields.Many2one(
        "etp.batch.budget.request",
        string="Parent Request",
        readonly=True,
        help="If set, this wizard creates a follow-up request linked to a "
             "partially-approved parent. Model/infra lines are copied from "
             "the parent and the requested total is capped at the parent's "
             "remaining amount.",
    )
    is_followup = fields.Boolean(
        string="Is Follow-up",
        compute="_compute_is_followup",
    )
    parent_remaining_amount = fields.Float(
        string="Parent Remaining (USD)",
        related="parent_request_id.remaining_amount",
        readonly=True,
    )
    parent_requested_total = fields.Float(
        string="Parent Requested Total (USD)",
        related="parent_request_id.requested_total",
        readonly=True,
    )
    parent_approved_total = fields.Float(
        string="Parent Approved Total (USD)",
        related="parent_request_id.approved_total",
        readonly=True,
    )
    justification = fields.Text(
        string="Justification",
        required=True,
        help="Explain why this budget is needed.",
    )
    subject = fields.Char(
        string="Email Subject",
        help="Optional. Overrides the default approval email subject.",
    )
    message = fields.Html(
        string="Message",
        help="Optional note included in the approval email body.",
    )
    priority = fields.Selection(
        [
            ("low", "Low"),
            ("normal", "Normal"),
            ("high", "High"),
            ("urgent", "Urgent"),
        ],
        string="Priority",
        default="normal",
    )
    request_type = fields.Selection(
        [
            ("budget", "Budget"),
            ("new_model", "New Model"),
            ("topup", "Top-up"),
            ("device", "Device"),
        ],
        string="Request Type",
        default="budget",
        required=True,
        help="Budget: task budgets (any mix of model / infra / subscription).\n"
             "New Model: introducing an AI model not yet on the project.\n"
             "Top-up: amount-only cash top-up, no model/infra/subscription lines.\n"
             "Device: new infrastructure / device request.",
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "etp_bbr_wizard_attachment_rel",
        "wizard_id",
        "attachment_id",
        string="Attachments",
    )
    total_tasks = fields.Integer(
        string="Total Tasks",
        help="Total number of tasks for this request. Each model line's "
             "requested amount = Total Tasks x Per Task Cost.",
    )
    buffer_pct = fields.Float(
        string="Buffer %",
        default=0.0,
        help="Buffer percentage applied on top of the requested subtotal "
             "before it is sent for approval.",
    )
    model_line_ids = fields.One2many(
        "etp.batch.budget.request.wizard.model.line",
        "wizard_id",
        string="Model Lines",
    )
    infra_line_ids = fields.One2many(
        "etp.batch.budget.request.wizard.infra.line",
        "wizard_id",
        string="Infrastructure Lines",
    )
    requested_total = fields.Float(
        string="Requested Total (USD)",
        readonly=False,
        help="Auto-suggested from (Total Tasks x Per Task Cost + infra) plus "
             "buffer. Fully editable - you can enter any amount.",
    )

    @api.depends("batch_id", "batch_id.request_ids")
    def _compute_is_first_request(self):
        for wiz in self:
            wiz.is_first_request = bool(
                wiz.batch_id
            ) and not wiz.batch_id.request_ids.filtered(
                lambda r: r.state in ("pending", "approved", "partially_approved")
            )

    @api.depends("parent_request_id")
    def _compute_is_followup(self):
        for wiz in self:
            wiz.is_followup = bool(wiz.parent_request_id)

    @api.onchange("total_tasks")
    def _onchange_total_tasks_update_lines(self):
        for wiz in self:
            for line in wiz.model_line_ids:
                line.requested_amount = (
                    (wiz.total_tasks or 0) * (line.per_task_cost or 0.0)
                )

    @api.onchange(
        "total_tasks",
        "buffer_pct",
        "model_line_ids",
        "infra_line_ids",
    )
    def _onchange_suggest_requested_total(self):
        for wiz in self:
            subtotal = (
                sum(wiz.model_line_ids.mapped("requested_amount"))
                + sum(wiz.infra_line_ids.mapped("requested_amount"))
            )
            wiz.requested_total = subtotal * (
                1.0 + ((wiz.buffer_pct or 0.0) / 100.0)
            )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        parent_id = self.env.context.get("default_parent_request_id") or vals.get(
            "parent_request_id"
        )
        parent = False
        if parent_id:
            parent = self.env["etp.batch.budget.request"].browse(parent_id)
            if not parent.exists():
                parent = False
        if parent:
            vals["parent_request_id"] = parent.id
            vals["batch_id"] = parent.batch_id.id
            if "buffer_pct" in fields_list:
                vals["buffer_pct"] = parent.buffer_pct or 0.0
            if "justification" in fields_list and parent.justification:
                vals["justification"] = parent.justification
            model_lines = []
            for line in parent.model_line_ids:
                model_lines.append((0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "description": line.description or False,
                    "cost_type": line.cost_type or "per_task",
                    "per_trajectory_cost": line.per_trajectory_cost or 0.0,
                    "iterations": line.iterations or 0,
                    "per_task_cost": line.per_task_cost or 0.0,
                    "requested_amount": 0.0,
                }))
            if model_lines:
                vals["model_line_ids"] = model_lines
            infra_lines = []
            for line in parent.infra_line_ids:
                infra_lines.append((0, 0, {
                    "infra_type_id": line.infra_type_id.id,
                    "description": line.description or False,
                    "requested_amount": 0.0,
                }))
            if infra_lines:
                vals["infra_line_ids"] = infra_lines
            if "requested_total" in fields_list:
                vals["requested_total"] = parent.remaining_amount or 0.0
            return vals
        batch_id = self.env.context.get("default_batch_id") or vals.get(
            "batch_id"
        )
        if not batch_id:
            return vals
        batch = self.env["etp.batch.budget"].browse(batch_id)
        if not batch.exists():
            return vals
        vals["batch_id"] = batch.id
        if "total_tasks" in fields_list:
            vals["total_tasks"] = batch.total_tasks or 0
        if "buffer_pct" in fields_list:
            vals["buffer_pct"] = batch.buffer_pct or 0.0
        model_lines = []
        for line in batch.model_line_ids:
            model_lines.append((0, 0, {
                "ai_model_id": line.ai_model_id.id,
                "cost_type": line.cost_type or "per_task",
                "per_trajectory_cost": line.per_trajectory_cost or 0.0,
                "iterations": line.iterations or 0,
                "per_task_cost": line.per_task_cost or 0.0,
            }))
        if model_lines:
            vals["model_line_ids"] = model_lines
        return vals

    def action_submit(self):
        self.ensure_one()
        rtype = self.request_type or "budget"
        if rtype == "topup":
            if self.model_line_ids or self.infra_line_ids:
                raise UserError(_(
                    "Top-up requests are amount-only. Remove model and "
                    "infrastructure lines."
                ))
        elif rtype == "device":
            if not self.infra_line_ids:
                raise UserError(_(
                    "Device requests must include at least one infrastructure line."
                ))
        elif rtype == "new_model":
            if not self.model_line_ids:
                raise UserError(_(
                    "New Model requests must include at least one model line."
                ))
        else:
            if not (self.model_line_ids or self.infra_line_ids):
                raise UserError(_(
                    "Add at least one model or infrastructure line."
                ))
        if (self.requested_total or 0.0) <= 0.0:
            raise UserError(_("Requested total must be greater than zero."))
        project_budget = self.batch_id.project_budget_id
        if not project_budget.approver_user_ids:
            raise UserError(_(
                "Project Budget has no approvers configured."
            ))
        if self.parent_request_id:
            parent = self.parent_request_id
            if parent.state != "partially_approved":
                raise UserError(_(
                    "Follow-up requests can only be raised against a "
                    "partially approved parent request."
                ))
            sibling = self.env["etp.batch.budget.request"].search([
                ("parent_request_id", "=", parent.id),
                ("state", "in", (
                    "draft", "cto_review", "cfo_review",
                    "changes_required", "approved", "partially_approved",
                )),
            ], limit=1)
            if sibling:
                raise UserError(_(
                    "A follow-up request (%s) already exists for parent %s."
                ) % (sibling.name, parent.name))
            cap = parent.remaining_amount or 0.0
            if (self.requested_total or 0.0) > cap + 0.00001:
                raise UserError(_(
                    "Requested total (%.2f) exceeds the parent's remaining "
                    "amount (%.2f)."
                ) % (self.requested_total, cap))
        request = self.env["etp.batch.budget.request"].create({
            "batch_id": self.batch_id.id,
            "parent_request_id": self.parent_request_id.id or False,
            "request_type": rtype,
            "justification": self.justification,
            "requester_id": self.env.user.id,
            "subject": self.subject or False,
            "message": self.message or False,
            "priority": self.priority or "normal",
            "total_tasks": self.total_tasks or 0,
            "buffer_pct": self.buffer_pct or 0.0,
            "requested_total": self.requested_total or 0.0,
            "attachment_ids": [(6, 0, self.attachment_ids.ids)],
            "model_line_ids": [
                (0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "description": line.description or False,
                    "cost_type": line.cost_type or "per_task",
                    "per_trajectory_cost": line.per_trajectory_cost or 0.0,
                    "iterations": line.iterations or 0,
                    "per_task_cost": line.per_task_cost or 0.0,
                    "requested_amount": line.requested_amount or 0.0,
                })
                for line in self.model_line_ids
            ],
            "infra_line_ids": [
                (0, 0, {
                    "infra_type_id": line.infra_type_id.id,
                    "description": line.description or False,
                    "requested_amount": line.requested_amount or 0.0,
                    "approved_amount": line.requested_amount or 0.0,
                })
                for line in self.infra_line_ids
            ],
        })
        if self.attachment_ids:
            self.attachment_ids.sudo().write({
                "res_model": "etp.batch.budget.request",
                "res_id": request.id,
            })
        request.action_submit_for_approval()
        return {
            "type": "ir.actions.act_window",
            "name": _("Budget Request"),
            "res_model": "etp.batch.budget.request",
            "res_id": request.id,
            "view_mode": "form",
            "target": "current",
        }


class EtpBatchBudgetRequestWizardModelLine(models.TransientModel):
    _name = "etp.batch.budget.request.wizard.model.line"
    _description = "Phase Budget Request Wizard Model Line"
    _order = "id"

    wizard_id = fields.Many2one(
        "etp.batch.budget.request.wizard",
        required=True,
        ondelete="cascade",
    )
    ai_model_id = fields.Many2one(
        "etp.ai.model",
        string="Model",
        required=True,
    )
    description = fields.Char(string="Task Description")
    cost_type = fields.Selection(
        [("per_task", "Per Task"), ("per_trajectory", "Per Trajectory")],
        string="Cost Type",
        default="per_task",
        required=True,
    )
    per_trajectory_cost = fields.Float(string="Per Trajectory Cost (USD)")
    iterations = fields.Integer(string="No. of Trajectories per Task")
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    requested_amount = fields.Float(
        string="Requested (USD)",
        help="Auto-suggested as Total Tasks (on the wizard) x Per Task Cost "
             "whenever Per Task Cost changes. Fully editable.",
    )

    @api.onchange("cost_type", "per_trajectory_cost", "iterations")
    def _onchange_trajectory_inputs(self):
        for line in self:
            if line.cost_type == "per_trajectory":
                line.per_task_cost = (
                    (line.per_trajectory_cost or 0.0)
                    * (line.iterations or 0)
                )
                if line.wizard_id:
                    line.requested_amount = (
                        (line.wizard_id.total_tasks or 0)
                        * (line.per_task_cost or 0.0)
                    )

    @api.onchange("per_task_cost")
    def _onchange_per_task_cost(self):
        for line in self:
            if line.wizard_id:
                line.requested_amount = (
                    (line.wizard_id.total_tasks or 0)
                    * (line.per_task_cost or 0.0)
                )


class EtpBatchBudgetRequestWizardInfraLine(models.TransientModel):
    _name = "etp.batch.budget.request.wizard.infra.line"
    _description = "Phase Budget Request Wizard Infra Line"
    _order = "id"

    wizard_id = fields.Many2one(
        "etp.batch.budget.request.wizard",
        required=True,
        ondelete="cascade",
    )
    infra_type_id = fields.Many2one(
        "etp.infra.type",
        string="Infrastructure",
        required=True,
    )
    description = fields.Char(string="Description")
    requested_amount = fields.Float(string="Requested (USD)")
