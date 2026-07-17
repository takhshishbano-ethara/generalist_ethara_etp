from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EtharaProjectPhaseRequestWizard(models.TransientModel):
    _name = "ethara.project.phase.request.wizard"
    _description = "Ethara Project Phase Budget Request Wizard"

    phase_id = fields.Many2one(
        "ethara.project.phase",
        string="Phase",
        required=True,
        ondelete="cascade",
    )
    budget_id = fields.Many2one(
        related="phase_id.budget_id",
        readonly=True,
    )
    ethara_project_id = fields.Many2one(
        related="phase_id.ethara_project_id",
        readonly=True,
    )
    justification = fields.Text(
        string="Justification",
        required=True,
    )
    subject = fields.Char(
        string="Email Subject",
    )
    message = fields.Html(
        string="Message",
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
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "ethara_ppr_wizard_attachment_rel",
        "wizard_id",
        "attachment_id",
        string="Attachments",
    )
    topup_reason_id = fields.Many2one(
        "ethara.project.phase.topup.reason",
        string="Top-up Reason",
        ondelete="restrict",
    )
    total_tasks = fields.Integer(string="Total Tasks")
    buffer_pct = fields.Float(string="Buffer %", default=0.0)
    model_line_ids = fields.One2many(
        "ethara.project.phase.request.wizard.model.line",
        "wizard_id",
        string="Model Lines",
    )
    infra_line_ids = fields.One2many(
        "ethara.project.phase.request.wizard.infra.line",
        "wizard_id",
        string="Infrastructure Lines",
    )
    subscription_line_ids = fields.One2many(
        "ethara.project.phase.request.wizard.subscription.line",
        "wizard_id",
        string="Subscription Lines",
    )
    requested_total = fields.Float(string="Requested Total (USD)")

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
        "subscription_line_ids",
    )
    def _onchange_suggest_requested_total(self):
        for wiz in self:
            buffered_subtotal = (
                sum(wiz.model_line_ids.mapped("requested_amount"))
                + sum(wiz.infra_line_ids.mapped("requested_amount"))
            )
            sub_total = sum(wiz.subscription_line_ids.mapped("requested_amount"))
            wiz.requested_total = buffered_subtotal * (
                1.0 + ((wiz.buffer_pct or 0.0) / 100.0)
            ) + sub_total

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        phase_id = self.env.context.get("default_phase_id") or vals.get("phase_id")
        if not phase_id:
            return vals
        phase = self.env["ethara.project.phase"].browse(phase_id)
        if not phase.exists():
            return vals
        vals["phase_id"] = phase.id
        if "total_tasks" in fields_list:
            vals["total_tasks"] = phase.total_tasks or 0
        if "buffer_pct" in fields_list:
            vals["buffer_pct"] = phase.buffer_pct or 0.0
        model_lines = []
        for line in phase.model_line_ids:
            model_lines.append((0, 0, {
                "ai_model_id": line.ai_model_id.id,
                "ai_model_name": line.ai_model_name or False,
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
            if (
                self.model_line_ids
                or self.infra_line_ids
                or self.subscription_line_ids
            ):
                raise UserError(_(
                    "Top-up requests are amount-only. Remove model, "
                    "infrastructure, and subscription lines."
                ))
        elif rtype == "device":
            if not self.infra_line_ids:
                raise UserError(_(
                    "Device requests must include at least one "
                    "infrastructure line."
                ))
        elif rtype == "new_model":
            if not self.model_line_ids:
                raise UserError(_(
                    "New Model requests must include at least one model line."
                ))
        else:
            if not (
                self.model_line_ids
                or self.infra_line_ids
                or self.subscription_line_ids
            ):
                raise UserError(_(
                    "Add at least one model, infrastructure, or subscription line."
                ))
        if (self.requested_total or 0.0) <= 0.0:
            raise UserError(_("Requested total must be greater than zero."))
        budget = self.phase_id.budget_id
        if not budget.approver_user_ids:
            raise UserError(_(
                "Project Budget has no approvers configured."
            ))
        request = self.env["ethara.project.phase.request"].create({
            "phase_id": self.phase_id.id,
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
            "topup_reason_id": self.topup_reason_id.id or False,
            "model_line_ids": [
                (0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "ai_model_name": line.ai_model_name or False,
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
                    "start_date": line.start_date or False,
                    "end_date": line.end_date or False,
                    "requested_amount": line.requested_amount or 0.0,
                    "approved_amount": line.requested_amount or 0.0,
                    "instance_type": line.instance_type or False,
                    "unit_price_usd": line.unit_price_usd or 0.0,
                    "price_unit": line.price_unit or False,
                    "quantity": line.quantity or 1.0,
                    "duration_hours": line.duration_hours or 730.0,
                    "ebs_storage_gb": line.ebs_storage_gb or 0.0,
                    "volume_type": line.volume_type or "gp3",
                    "volume_rate_usd_per_gb_mo": line.volume_rate_usd_per_gb_mo or 0.0,
                })
                for line in self.infra_line_ids
            ],
            "subscription_line_ids": [
                (0, 0, {
                    "subscription_id": line.subscription_id.id,
                    "assigned_user_ids": [(6, 0, line.assigned_user_ids.ids)],
                    "requested_amount": line.requested_amount or 0.0,
                })
                for line in self.subscription_line_ids
            ],
        })
        if self.attachment_ids:
            self.attachment_ids.sudo().write({
                "res_model": "ethara.project.phase.request",
                "res_id": request.id,
            })
        request.action_submit_for_approval()
        return {
            "type": "ir.actions.act_window",
            "name": _("Phase Budget Request"),
            "res_model": "ethara.project.phase.request",
            "res_id": request.id,
            "view_mode": "form",
            "target": "current",
        }


class EtharaProjectPhaseRequestWizardModelLine(models.TransientModel):
    _name = "ethara.project.phase.request.wizard.model.line"
    _description = "Ethara Project Phase Request Wizard Model Line"
    _order = "id"

    wizard_id = fields.Many2one(
        "ethara.project.phase.request.wizard",
        required=True,
        ondelete="cascade",
    )
    ai_model_id = fields.Many2one(
        "ethara.project.ai.model",
        string="Provider",
        required=True,
    )
    ai_model_name = fields.Char(string="Model")
    cost_type = fields.Selection(
        [("per_task", "Per Task"), ("per_trajectory", "Per Trajectory")],
        string="Cost Type",
        default="per_task",
        required=True,
    )
    per_trajectory_cost = fields.Float(string="Per Trajectory Cost (USD)")
    iterations = fields.Integer(string="No. of Trajectories per Task")
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    requested_amount = fields.Float(string="Requested (USD)")

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


class EtharaProjectPhaseRequestWizardInfraLine(models.TransientModel):
    _name = "ethara.project.phase.request.wizard.infra.line"
    _description = "Ethara Project Phase Request Wizard Infra Line"
    _order = "id"

    wizard_id = fields.Many2one(
        "ethara.project.phase.request.wizard",
        required=True,
        ondelete="cascade",
    )
    infra_type_id = fields.Many2one(
        "ethara.project.infra.type",
        string="Infrastructure",
        required=True,
    )
    description = fields.Char(string="Description")
    start_date = fields.Date(string="Start Date")
    end_date = fields.Date(string="End Date")
    requested_amount = fields.Float(string="Requested (USD)")

    instance_type = fields.Char(string="Instance Type")
    unit_price_usd = fields.Float(string="Compute Rate (USD/Hr)", digits=(16, 6))
    price_unit = fields.Char(string="Price Unit")
    quantity = fields.Float(string="Quantity", default=1.0)
    duration_hours = fields.Float(string="Hours / Month", default=730.0)
    ebs_storage_gb = fields.Float(string="EBS Storage (GB)", digits=(16, 2))
    volume_type = fields.Char(string="Volume Type", default="gp3")
    volume_rate_usd_per_gb_mo = fields.Float(
        string="Storage Rate (USD/GB-mo)", digits=(16, 6)
    )


class EtharaProjectPhaseRequestWizardSubscriptionLine(models.TransientModel):
    _name = "ethara.project.phase.request.wizard.subscription.line"
    _description = "Ethara Project Phase Request Wizard Subscription Line"
    _order = "id"

    wizard_id = fields.Many2one(
        "ethara.project.phase.request.wizard",
        required=True,
        ondelete="cascade",
    )
    subscription_id = fields.Many2one(
        "ethara.project.subscription",
        string="Subscription",
        required=True,
    )
    cost_per_subscription = fields.Float(
        string="Cost per Subscription (USD)",
        related="subscription_id.cost",
        readonly=True,
    )
    assigned_user_ids = fields.Many2many(
        "res.users",
        "ethara_ppr_wizard_subscription_line_user_rel",
        "wizard_line_id",
        "user_id",
        string="Assigned To",
    )
    subscription_count = fields.Integer(
        string="No. of Subscriptions",
        compute="_compute_subscription_count",
    )
    requested_amount = fields.Float(
        string="Requested (USD)",
        compute="_compute_requested_amount",
        store=True,
        readonly=False,
    )

    @api.depends("assigned_user_ids")
    def _compute_subscription_count(self):
        for line in self:
            line.subscription_count = len(line.assigned_user_ids)

    @api.depends("cost_per_subscription", "assigned_user_ids")
    def _compute_requested_amount(self):
        for line in self:
            line.requested_amount = (
                (line.cost_per_subscription or 0.0)
                * len(line.assigned_user_ids)
            )
