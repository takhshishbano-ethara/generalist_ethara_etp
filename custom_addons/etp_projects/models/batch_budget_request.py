from odoo import _, api, fields, models
from odoo.exceptions import UserError


REQUEST_STATE_SELECTION = [
    ("draft", "Draft"),
    ("pending", "Pending Approval"),
    ("approved", "Approved"),
    ("partially_approved", "Partially Approved"),
    ("rejected", "Rejected"),
    ("withdrawn", "Withdrawn"),
]

TERMINAL_APPROVED_STATES = ("approved", "partially_approved")


class EtpBatchBudgetRequest(models.Model):
    _name = "etp.batch.budget.request"
    _description = "Phase Budget Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "request_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        string="Name",
        required=True,
        copy=False,
        readonly=True,
        default="New",
        tracking=True,
    )
    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
    )
    project_budget_id = fields.Many2one(
        "etp.project.aws.budget",
        string="Project Budget",
        related="batch_id.project_budget_id",
        store=True,
        readonly=True,
    )
    project_id = fields.Many2one(
        "project.project",
        string="Project",
        related="batch_id.project_id",
        store=True,
        readonly=True,
    )
    sequence_number = fields.Integer(
        string="Request #",
        compute="_compute_sequence_number",
        store=True,
        help="Position of this request among the phase's requests.",
    )
    request_date = fields.Datetime(
        string="Requested On",
        default=fields.Datetime.now,
        required=True,
        tracking=True,
    )
    state = fields.Selection(
        REQUEST_STATE_SELECTION,
        string="Status",
        default="draft",
        required=True,
        tracking=True,
        copy=False,
    )
    requester_id = fields.Many2one(
        "res.users",
        string="Requester",
        default=lambda self: self.env.user,
        tracking=True,
    )
    approver_id = fields.Many2one(
        "res.users",
        string="Approver",
        readonly=True,
        tracking=True,
    )
    approval_date = fields.Datetime(readonly=True, tracking=True)
    rejection_reason = fields.Text(readonly=True)
    justification = fields.Text(
        string="Justification",
        help="Explain why this additional budget is needed.",
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
        tracking=True,
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "etp_batch_budget_request_attachment_rel",
        "request_id",
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
        "etp.batch.budget.request.model.line",
        "request_id",
        string="Model Lines",
        copy=True,
    )
    infra_line_ids = fields.One2many(
        "etp.batch.budget.request.infra.line",
        "request_id",
        string="Infrastructure Lines",
        copy=True,
    )
    subscription_line_ids = fields.One2many(
        "etp.batch.budget.request.subscription.line",
        "request_id",
        string="Subscription Lines",
        copy=True,
    )
    requested_total = fields.Float(
        string="Requested Total (USD)",
        help="Auto-suggested as (sum of line requested amounts) x "
             "(1 + buffer %) when lines or buffer change. Fully editable - "
             "the value is not recomputed on the server.",
    )
    approved_total = fields.Float(
        string="Approved Total (USD)",
        help="Auto-suggested as (sum of line approved amounts) x "
             "(1 + buffer %) when lines or buffer change. Fully editable - "
             "the value is not recomputed on the server.",
    )
    parent_request_id = fields.Many2one(
        "etp.batch.budget.request",
        string="Parent Request",
        readonly=True,
        copy=False,
        index=True,
        ondelete="restrict",
        help="The partially-approved request this follow-up extends. "
             "Empty for fresh requests.",
    )
    follow_up_request_ids = fields.One2many(
        "etp.batch.budget.request",
        "parent_request_id",
        string="Follow-up Requests",
    )
    follow_up_count = fields.Integer(
        string="Follow-up Count",
        compute="_compute_follow_up_count",
    )
    is_followup = fields.Boolean(
        string="Is Follow-up",
        compute="_compute_is_followup",
        store=True,
    )
    remaining_amount = fields.Float(
        string="Remaining (USD)",
        compute="_compute_remaining_amount",
        help="Requested Total minus Approved Total. Acts as the cap "
             "for the next follow-up request against this one.",
    )
    has_active_followup = fields.Boolean(
        string="Has Active Follow-up",
        compute="_compute_has_active_followup",
    )
    can_follow_up = fields.Boolean(
        string="Can Create Follow-up",
        compute="_compute_can_follow_up",
    )

    @api.depends(
        "batch_id",
        "batch_id.request_ids",
        "batch_id.request_ids.request_date",
    )
    def _compute_sequence_number(self):
        for rec in self:
            if not rec.batch_id:
                rec.sequence_number = 0
                continue
            ordered = rec.batch_id.request_ids.sorted(
                key=lambda r: (r.request_date or fields.Datetime.now(), r.id)
            )
            pos = 0
            for idx, req in enumerate(ordered, start=1):
                if req == rec:
                    pos = idx
                    break
            rec.sequence_number = pos

    @api.depends("follow_up_request_ids")
    def _compute_follow_up_count(self):
        for rec in self:
            rec.follow_up_count = len(rec.follow_up_request_ids)

    @api.depends("parent_request_id")
    def _compute_is_followup(self):
        for rec in self:
            rec.is_followup = bool(rec.parent_request_id)

    @api.depends("requested_total", "approved_total")
    def _compute_remaining_amount(self):
        for rec in self:
            rec.remaining_amount = max(
                0.0,
                (rec.requested_total or 0.0) - (rec.approved_total or 0.0),
            )

    @api.depends(
        "follow_up_request_ids",
        "follow_up_request_ids.state",
    )
    def _compute_has_active_followup(self):
        for rec in self:
            rec.has_active_followup = any(
                child.state in (
                    "draft", "pending", "approved", "partially_approved",
                )
                for child in rec.follow_up_request_ids
            )

    @api.depends("state", "remaining_amount", "has_active_followup")
    def _compute_can_follow_up(self):
        for rec in self:
            rec.can_follow_up = (
                rec.state == "partially_approved"
                and (rec.remaining_amount or 0.0) > 0.0
                and not rec.has_active_followup
            )

    @api.onchange("total_tasks")
    def _onchange_total_tasks_update_lines(self):
        for rec in self:
            for line in rec.model_line_ids:
                line.requested_amount = (
                    (rec.total_tasks or 0) * (line.per_task_cost or 0.0)
                )

    @api.onchange(
        "model_line_ids",
        "infra_line_ids",
        "buffer_pct",
    )
    def _onchange_suggest_totals(self):
        for rec in self:
            factor = 1.0 + ((rec.buffer_pct or 0.0) / 100.0)
            requested_base = (
                sum(rec.model_line_ids.mapped("requested_amount"))
                + sum(rec.infra_line_ids.mapped("requested_amount"))
            )
            approved_base = (
                sum(rec.model_line_ids.mapped("approved_amount"))
                + sum(rec.infra_line_ids.mapped("approved_amount"))
            )
            rec.requested_total = requested_base * factor
            rec.approved_total = approved_base * factor

    def _distribute_approved_amount(self):
        self.ensure_one()
        for line in self.subscription_line_ids:
            line.approved_amount = line.final_amount or 0.0
        for line in self.infra_line_ids:
            if (
                line.start_date
                and line.end_date
                and line.end_date >= line.start_date
            ):
                days = (line.end_date - line.start_date).days + 1
                per_day = (line.requested_amount or 0.0) / 30.0
                line.approved_amount = days * per_day
            else:
                line.approved_amount = line.requested_amount or 0.0
        total_tasks = self.total_tasks or self.batch_id.total_tasks or 0
        for line in self.model_line_ids:
            line.approved_amount = total_tasks * (line.per_task_cost or 0.0)
        factor = 1.0 + ((self.buffer_pct or 0.0) / 100.0)
        base = (
            sum(self.model_line_ids.mapped("approved_amount"))
            + sum(self.infra_line_ids.mapped("approved_amount"))
        )
        self.approved_total = base * factor

    def action_auto_distribute_approved(self):
        for rec in self:
            rec._distribute_approved_amount()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("etp.batch.budget.request")
                    or "New"
                )
        return super().create(vals_list)

    def _check_can_approve(self):
        self.ensure_one()
        if not self.batch_id:
            raise UserError(_("Request has no phase budget."))
        project_budget = self.batch_id.project_budget_id
        if self.env.user not in project_budget.approver_user_ids:
            raise UserError(_(
                "You are not in the approver pool for this Project Budget."
            ))

    def _approver_partner_ids(self):
        self.ensure_one()
        return self.batch_id.project_budget_id.approver_user_ids.mapped(
            "partner_id"
        ).ids

    def _requester_partner_ids(self):
        self.ensure_one()
        return self.requester_id.partner_id.ids if self.requester_id else []

    def _send_mail(self, template_xmlid, partner_ids, email_values=None):
        self.ensure_one()
        if not partner_ids:
            return
        project = self.project_id
        if project:
            project._etp_post_budget_message(
                template_xmlid, self, partner_ids, email_values=email_values,
            )
            return
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            return
        values = {"partner_ids": [(6, 0, partner_ids)]}
        if email_values:
            values.update(email_values)
        template.send_mail(
            self.id,
            email_values=values,
            force_send=False,
        )

    def action_submit_for_approval(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Only draft requests can be submitted."))
            if not (rec.model_line_ids or rec.infra_line_ids):
                raise UserError(_(
                    "Add at least one model or infrastructure line."
                ))
            if (rec.requested_total or 0.0) <= 0.0:
                raise UserError(_(
                    "Requested total must be greater than zero."
                ))
            if not rec.batch_id.project_budget_id.approver_user_ids:
                raise UserError(_(
                    "Project Budget has no approvers configured."
                ))
            if rec.parent_request_id:
                parent = rec.parent_request_id
                if parent.state != "partially_approved":
                    raise UserError(_(
                        "Parent request must be in 'Partially Approved' "
                        "state to accept a follow-up."
                    ))
                sibling_active = any(
                    child.state in (
                        "pending", "approved", "partially_approved",
                    )
                    for child in parent.follow_up_request_ids
                    if child.id != rec.id
                )
                if sibling_active:
                    raise UserError(_(
                        "Parent request already has an active follow-up. "
                        "Resolve it before submitting another."
                    ))
                if (rec.requested_total or 0.0) > (
                    parent.remaining_amount or 0.0
                ) + 0.00001:
                    raise UserError(_(
                        "Follow-up requested total (USD %(req).2f) exceeds "
                        "parent request's remaining amount (USD %(rem).2f)."
                    ) % {
                        "req": rec.requested_total,
                        "rem": parent.remaining_amount,
                    })
            for line in rec.model_line_ids:
                if not line.approved_amount:
                    line.approved_amount = line.requested_amount
            for line in rec.infra_line_ids:
                if not line.approved_amount:
                    line.approved_amount = line.requested_amount
                if not line.start_date:
                    line.start_date = rec.batch_id.start_date
                if not line.end_date:
                    line.end_date = rec.batch_id.end_date
            for line in rec.subscription_line_ids:
                if not line.approved_amount:
                    line.approved_amount = (
                        line.requested_amount or line.final_amount
                    )
            if not rec.approved_total:
                rec.approved_total = rec.requested_total
            rec.state = "pending"
            email_values = {}
            if rec.subject:
                email_values["subject"] = rec.subject
            if rec.attachment_ids:
                email_values["attachment_ids"] = [(6, 0, rec.attachment_ids.ids)]
            rec._send_mail(
                "etp_projects.mail_template_batch_request_submitted",
                rec._approver_partner_ids(),
                email_values=email_values,
            )

    def action_approve(self):
        for rec in self:
            if rec.state != "pending":
                raise UserError(_("Only pending requests can be approved."))
            rec._check_can_approve()
            rec._distribute_approved_amount()
            if (rec.approved_total or 0.0) <= 0.0:
                raise UserError(_(
                    "Approved total must be greater than zero. "
                    "Use 'Reject' if you want to deny the request entirely."
                ))
            # remaining = rec.project_budget_id.allocatable_amount or 0.0
            # if (rec.approved_total or 0.0) > remaining:
            #     raise UserError(_(
            #         "Approved amount (USD %(app).2f) exceeds remaining project "
            #         "budget (USD %(rem).2f). Approve a smaller amount or have "
            #         "the project owner top up the project budget first."
            #     ) % {"app": rec.approved_total, "rem": remaining})
            is_partial = (
                (rec.approved_total or 0.0) < (rec.requested_total or 0.0)
                or any(
                    (line.approved_amount or 0.0) < (line.requested_amount or 0.0)
                    for line in rec.model_line_ids
                )
                or any(
                    (line.approved_amount or 0.0) < (line.requested_amount or 0.0)
                    for line in rec.infra_line_ids
                )
            )
            new_state = "partially_approved" if is_partial else "approved"
            rec.write({
                "state": new_state,
                "approver_id": self.env.user.id,
                "approval_date": fields.Datetime.now(),
            })
            rec._propagate_to_batch_and_project()
            rec._send_mail(
                "etp_projects.mail_template_batch_request_approved",
                rec._requester_partner_ids(),
            )

    def action_reject(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Reject Budget Request"),
            "res_model": "etp.batch.budget.request.reject.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_request_id": self.id},
        }

    def action_create_follow_up(self):
        self.ensure_one()
        if self.state != "partially_approved":
            raise UserError(_(
                "Only Partially Approved requests can be followed up."
            ))
        if (self.remaining_amount or 0.0) <= 0.0:
            raise UserError(_(
                "This request has no remaining amount to follow up."
            ))
        if self.has_active_followup:
            raise UserError(_(
                "This request already has an active follow-up."
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Follow-up Budget Request"),
            "res_model": "etp.batch.budget.request.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_batch_id": self.batch_id.id,
                "default_parent_request_id": self.id,
            },
        }

    def action_view_follow_ups(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Follow-up Requests"),
            "res_model": "etp.batch.budget.request",
            "view_mode": "list,form",
            "domain": [("parent_request_id", "=", self.id)],
            "context": {"default_parent_request_id": self.id},
        }

    def action_open_parent_request(self):
        self.ensure_one()
        if not self.parent_request_id:
            raise UserError(_("This request has no parent."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Parent Request"),
            "res_model": "etp.batch.budget.request",
            "res_id": self.parent_request_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def _do_reject(self, reason):
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_("Only pending requests can be rejected."))
        self._check_can_approve()
        self.write({
            "state": "rejected",
            "approver_id": self.env.user.id,
            "approval_date": fields.Datetime.now(),
            "rejection_reason": reason,
        })
        self._send_mail(
            "etp_projects.mail_template_batch_request_rejected",
            self._requester_partner_ids(),
        )

    def action_withdraw(self):
        for rec in self:
            if rec.state not in ("draft", "pending"):
                raise UserError(_(
                    "Only Draft or Pending requests can be withdrawn."
                ))
            if rec.requester_id and self.env.user != rec.requester_id:
                raise UserError(_(
                    "Only the requester can withdraw this request."
                ))
            rec.state = "withdrawn"

    def _propagate_to_batch_and_project(self):
        self.ensure_one()
        batch = self.batch_id
        project_budget = batch.project_budget_id
        BatchModelLine = self.env["etp.batch.budget.model.line"]
        BatchInfraLine = self.env["etp.batch.budget.infra.line"]
        ProjectModelLine = self.env["etp.project.budget.model.line"]
        ProjectInfraLine = self.env["etp.project.budget.infra.line"]

        batch_existing_models = {
            line.ai_model_id.id for line in batch.model_line_ids
        }
        project_existing_models = {
            line.ai_model_id.id for line in project_budget.model_line_ids
        }
        for line in self.model_line_ids:
            if (line.approved_amount or 0.0) <= 0.0:
                continue
            if line.ai_model_id.id not in batch_existing_models:
                BatchModelLine.create({
                    "batch_id": batch.id,
                    "ai_model_id": line.ai_model_id.id,
                    "cost_type": line.cost_type or "per_task",
                    "per_trajectory_cost": line.per_trajectory_cost or 0.0,
                    "iterations": line.iterations or 0,
                    "per_task_cost": line.per_task_cost or 0.0,
                })
                batch_existing_models.add(line.ai_model_id.id)
            if line.ai_model_id.id not in project_existing_models:
                ProjectModelLine.create({
                    "budget_id": project_budget.id,
                    "ai_model_id": line.ai_model_id.id,
                    "cost_type": line.cost_type or "per_task",
                    "per_trajectory_cost": line.per_trajectory_cost or 0.0,
                    "iterations": line.iterations or 0,
                    "per_task_cost": line.per_task_cost or 0.0,
                })
                project_existing_models.add(line.ai_model_id.id)

        batch_existing_infra = {
            line.infra_type_id.id for line in batch.infra_line_ids
        }
        project_existing_infra = {
            line.infra_type_id.id for line in project_budget.infra_line_ids
        }
        for line in self.infra_line_ids:
            if (line.approved_amount or 0.0) <= 0.0:
                continue
            if line.infra_type_id.id not in batch_existing_infra:
                BatchInfraLine.create({
                    "batch_id": batch.id,
                    "infra_type_id": line.infra_type_id.id,
                    "description": line.description or False,
                    "budget_amount": line.approved_amount or 0.0,
                    "start_date": line.start_date or False,
                    "end_date": line.end_date or False,
                })
                batch_existing_infra.add(line.infra_type_id.id)
            if line.infra_type_id.id not in project_existing_infra:
                ProjectInfraLine.create({
                    "budget_id": project_budget.id,
                    "infra_type_id": line.infra_type_id.id,
                    "description": line.description or False,
                    "budget_amount": line.approved_amount or 0.0,
                    "start_date": line.start_date or False,
                    "end_date": line.end_date or False,
                })
                project_existing_infra.add(line.infra_type_id.id)

        BatchSubLine = self.env["etp.batch.budget.subscription.line"]
        ProjectSubLine = self.env["etp.project.budget.subscription.line"]
        batch_existing_subs = {
            line.subscription_id.id for line in batch.subscription_line_ids
        }
        project_existing_subs = {
            line.subscription_id.id for line in project_budget.subscription_line_ids
        }
        for line in self.subscription_line_ids:
            if (line.approved_amount or 0.0) <= 0.0:
                continue
            if line.subscription_id.id not in batch_existing_subs:
                BatchSubLine.create({
                    "batch_id": batch.id,
                    "subscription_id": line.subscription_id.id,
                    "assigned_user_ids": [(6, 0, line.assigned_user_ids.ids)],
                    "approved_amount": line.approved_amount or 0.0,
                })
                batch_existing_subs.add(line.subscription_id.id)
            if line.subscription_id.id not in project_existing_subs:
                ProjectSubLine.create({
                    "budget_id": project_budget.id,
                    "subscription_id": line.subscription_id.id,
                    "assigned_user_ids": [(6, 0, line.assigned_user_ids.ids)],
                    "approved_amount": line.approved_amount or 0.0,
                })
                project_existing_subs.add(line.subscription_id.id)

        new_state = "approved" if batch.state in (
            "draft", "rejected", "withdrawn", "pending"
        ) else batch.state
        batch.write({"state": new_state})


class EtpBatchBudgetRequestRejectWizard(models.TransientModel):
    _name = "etp.batch.budget.request.reject.wizard"
    _description = "Reject Phase Budget Request Wizard"

    request_id = fields.Many2one(
        "etp.batch.budget.request",
        required=True,
    )
    reason = fields.Text(string="Rejection Reason", required=True)

    def action_confirm(self):
        self.ensure_one()
        self.request_id._do_reject(self.reason)
        return {"type": "ir.actions.act_window_close"}
