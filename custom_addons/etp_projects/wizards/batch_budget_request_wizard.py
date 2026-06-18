from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EtpBatchBudgetRequestWizard(models.TransientModel):
    _name = "etp.batch.budget.request.wizard"
    _description = "Batch Budget Request Wizard"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Batch Budget",
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
        related="batch_id.project_budget_id.remaining_amount",
        readonly=True,
    )
    is_first_request = fields.Boolean(
        string="First Request",
        compute="_compute_is_first_request",
    )
    justification = fields.Text(
        string="Justification",
        required=True,
        help="Explain why this budget is needed.",
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
        compute="_compute_requested_total",
    )

    @api.depends("batch_id", "batch_id.request_ids")
    def _compute_is_first_request(self):
        for wiz in self:
            wiz.is_first_request = bool(
                wiz.batch_id
            ) and not wiz.batch_id.request_ids.filtered(
                lambda r: r.state in ("pending", "approved", "partially_approved")
            )

    @api.depends(
        "model_line_ids.requested_amount",
        "infra_line_ids.requested_amount",
    )
    def _compute_requested_total(self):
        for wiz in self:
            wiz.requested_total = (
                sum(wiz.model_line_ids.mapped("requested_amount"))
                + sum(wiz.infra_line_ids.mapped("requested_amount"))
            )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        batch_id = self.env.context.get("default_batch_id") or vals.get(
            "batch_id"
        )
        if not batch_id:
            return vals
        batch = self.env["etp.batch.budget"].browse(batch_id)
        if not batch.exists():
            return vals
        vals["batch_id"] = batch.id
        is_first = not batch.request_ids.filtered(
            lambda r: r.state in ("pending", "approved", "partially_approved")
        )
        tasks = (batch.total_tasks or 0) if is_first else 0
        model_lines = []
        for line in batch.model_line_ids:
            per_cost = line.per_task_cost or 0.0
            requested = per_cost * tasks if is_first else 0.0
            model_lines.append((0, 0, {
                "ai_model_id": line.ai_model_id.id,
                "task_count": tasks,
                "per_task_cost": per_cost,
                "requested_amount": requested,
            }))
        if model_lines:
            vals["model_line_ids"] = model_lines
        return vals

    def action_submit(self):
        self.ensure_one()
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
        remaining = project_budget.remaining_amount or 0.0
        if (self.requested_total or 0.0) > remaining:
            raise UserError(_(
                "Requested amount (USD %(req).2f) exceeds remaining project "
                "budget (USD %(rem).2f). Ask the project owner to top up first."
            ) % {"req": self.requested_total, "rem": remaining})
        request = self.env["etp.batch.budget.request"].create({
            "batch_id": self.batch_id.id,
            "justification": self.justification,
            "requester_id": self.env.user.id,
            "model_line_ids": [
                (0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "description": line.description or False,
                    "task_count": line.task_count or 0,
                    "per_task_cost": line.per_task_cost or 0.0,
                    "requested_amount": line.requested_amount or 0.0,
                    "approved_amount": line.requested_amount or 0.0,
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
    _description = "Batch Budget Request Wizard Model Line"
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
    task_count = fields.Integer(string="Task Count")
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    requested_amount = fields.Float(string="Requested (USD)")

    @api.onchange("task_count", "per_task_cost")
    def _onchange_requested_amount(self):
        if self.task_count and self.per_task_cost:
            self.requested_amount = self.task_count * self.per_task_cost


class EtpBatchBudgetRequestWizardInfraLine(models.TransientModel):
    _name = "etp.batch.budget.request.wizard.infra.line"
    _description = "Batch Budget Request Wizard Infra Line"
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
