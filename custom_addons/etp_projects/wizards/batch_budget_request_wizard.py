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
        related="batch_id.project_budget_id.allocatable_amount",
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
                "per_task_cost": line.per_task_cost or 0.0,
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
        request = self.env["etp.batch.budget.request"].create({
            "batch_id": self.batch_id.id,
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
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    requested_amount = fields.Float(
        string="Requested (USD)",
        help="Auto-suggested as Total Tasks (on the wizard) x Per Task Cost "
             "whenever Per Task Cost changes. Fully editable.",
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
