from odoo import _, api, fields, models
from odoo.exceptions import UserError


STATE_SELECTION = [
    ("draft", "Draft"),
    ("pending", "Pending Approval"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
]


class EtpProjectBudgetTopup(models.Model):
    _name = "etp.project.budget.topup"
    _description = "Project Budget Additional Approval"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        required=True,
        copy=False,
        readonly=True,
        default="New",
        tracking=True,
    )
    project_budget_id = fields.Many2one(
        "etp.project.aws.budget",
        string="Project Budget",
        required=True,
        ondelete="cascade",
        tracking=True,
    )
    project_id = fields.Many2one(
        "project.project",
        related="project_budget_id.project_id",
        store=True,
        readonly=True,
        index=True,
    )
    amount = fields.Float(
        string="Top-up Amount (USD)",
        required=True,
        tracking=True,
    )
    justification = fields.Text(string="Justification", required=True)
    state = fields.Selection(
        STATE_SELECTION,
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

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("etp.project.budget.topup")
                    or "New"
                )
        return super().create(vals_list)

    def _check_can_approve(self):
        self.ensure_one()
        if (
            self.env.user not in self.project_budget_id.approver_user_ids
            and self.env.user != self.requester_id
        ):
            raise UserError(_(
                "Only an approver of this Project Budget or the requester "
                "can act on this top-up."
            ))

    def action_submit(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Only draft top-ups can be submitted."))
            if (rec.amount or 0.0) <= 0.0:
                raise UserError(_("Top-up amount must be greater than zero."))
            if not rec.project_budget_id.approver_user_ids:
                raise UserError(_(
                    "Project Budget has no approvers configured."
                ))
            rec.state = "pending"
            rec._send_mail("etp_projects.mail_template_topup_approval_request",
                           rec._approver_partner_ids())

    def action_approve(self):
        for rec in self:
            if rec.state != "pending":
                raise UserError(_("Only pending top-ups can be approved."))
            rec._check_can_approve()
            rec.write({
                "state": "approved",
                "approver_id": self.env.user.id,
                "approval_date": fields.Datetime.now(),
            })
            rec._send_mail("etp_projects.mail_template_topup_approved",
                           rec.requester_id.partner_id.ids
                           if rec.requester_id else [])

    def action_reject(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Reject Top-up"),
            "res_model": "etp.project.budget.topup.reject.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_topup_id": self.id},
        }

    def _do_reject(self, reason):
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_("Only pending top-ups can be rejected."))
        self._check_can_approve()
        self.write({
            "state": "rejected",
            "approver_id": self.env.user.id,
            "approval_date": fields.Datetime.now(),
            "rejection_reason": reason,
        })
        self._send_mail("etp_projects.mail_template_topup_rejected",
                        self.requester_id.partner_id.ids
                        if self.requester_id else [])

    def action_reset_to_draft(self):
        for rec in self:
            if rec.state not in ("rejected",):
                raise UserError(_("Only rejected top-ups can be reset."))
            rec.write({
                "state": "draft",
                "approver_id": False,
                "approval_date": False,
                "rejection_reason": False,
            })

    def _approver_partner_ids(self):
        self.ensure_one()
        return self.project_budget_id.approver_user_ids.mapped("partner_id").ids

    def _send_mail(self, template_xmlid, partner_ids):
        self.ensure_one()
        if not partner_ids:
            return
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            return
        template.send_mail(
            self.id,
            email_values={"partner_ids": [(6, 0, partner_ids)]},
            force_send=False,
        )


class EtpProjectBudgetTopupRejectWizard(models.TransientModel):
    _name = "etp.project.budget.topup.reject.wizard"
    _description = "Reject Top-up Wizard"

    topup_id = fields.Many2one("etp.project.budget.topup", required=True)
    reason = fields.Text(string="Rejection Reason", required=True)

    def action_confirm(self):
        self.ensure_one()
        self.topup_id._do_reject(self.reason)
        return {"type": "ir.actions.act_window_close"}
