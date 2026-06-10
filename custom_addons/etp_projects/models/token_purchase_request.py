import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


CONFIG_PARAM_APPROVERS = "etp_projects.token_purchase_approver_ids"
CONFIG_PARAM_FINANCE_USERS = "etp_projects.token_purchase_finance_user_ids"


def _parse_user_ids(param):
    return [int(x) for x in (param or "").split(",") if x.strip().isdigit()]


class EtpProjectTokenPurchaseRequest(models.Model):
    _name = "etp.project.token.purchase.request"
    _description = "Project Token Purchase Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        required=True, readonly=True, copy=False, default=lambda self: _("New"),
        tracking=True,
    )
    budget_id = fields.Many2one(
        "etp.project.aws.budget", required=True, ondelete="cascade",
        string="Project Budget", tracking=True,
    )
    project_id = fields.Many2one(
        "project.project",
        related="budget_id.project_id", store=True, readonly=True,
        string="Project",
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="budget_id.currency_id", store=True, readonly=True,
    )

    model_name = fields.Char(string="Model Name", tracking=True)
    requested_amount = fields.Monetary(
        currency_field="currency_id", tracking=True,
        string="Requested Amount",
    )
    description = fields.Text(string="Description / Business Justification")

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending Approval"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("completed", "Completed"),
        ],
        default="draft", required=True, tracking=True, copy=False,
    )

    requester_id = fields.Many2one(
        "res.users", string="Requested By",
        default=lambda self: self.env.user, tracking=True,
    )
    approver_id = fields.Many2one("res.users", string="Approved/Rejected By", readonly=True, tracking=True)
    approval_date = fields.Datetime(readonly=True, tracking=True)
    rejection_reason = fields.Text(string="Rejection Reason")

    approval_token = fields.Char(readonly=True, copy=False, index=True)
    token_used = fields.Boolean(readonly=True, copy=False, default=False)

    finance_token = fields.Char(readonly=True, copy=False, index=True)
    finance_token_used = fields.Boolean(readonly=True, copy=False, default=False)

    approved_amount = fields.Monetary(
        currency_field="currency_id", tracking=True,
        string="Approved Amount",
    )
    cost_center = fields.Char(string="Module / Cost Center", tracking=True)
    supporting_document_ids = fields.Many2many(
        "ir.attachment",
        "etp_token_purchase_request_attachment_rel",
        "request_id", "attachment_id",
        string="Supporting Documents",
    )
    supporting_document_count = fields.Integer(
        compute="_compute_supporting_document_count", store=False,
    )

    completed_date = fields.Datetime(readonly=True, tracking=True)
    completed_by_id = fields.Many2one("res.users", string="Completed By", readonly=True, tracking=True)
    balance_before = fields.Monetary(
        currency_field="currency_id", readonly=True, copy=False,
        string="Balance Before",
        help="Project Budget on the linked budget record immediately before this "
             "request was completed (captured at completion time).",
    )

    can_approve = fields.Boolean(
        compute="_compute_can_approve", store=False,
        help="True when the current user is in the configured approver list.",
    )

    @api.depends("supporting_document_ids")
    def _compute_supporting_document_count(self):
        for rec in self:
            rec.supporting_document_count = len(rec.supporting_document_ids)

    @api.depends_context("uid")
    def _compute_can_approve(self):
        approver_ids = set(self._get_approver_users().ids)
        current_uid = self.env.uid
        is_approver = current_uid in approver_ids
        for rec in self:
            rec.can_approve = is_approver

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "etp.project.token.purchase.request"
                ) or _("New")
        return super().create(vals_list)

    @api.model
    def _get_approver_users(self):
        ids = _parse_user_ids(
            self.env["ir.config_parameter"].sudo().get_param(
                CONFIG_PARAM_APPROVERS, default=""
            )
        )
        if not ids:
            return self.env["res.users"].sudo().browse()
        return self.env["res.users"].sudo().browse(ids).exists()

    @api.model
    def _get_finance_users(self):
        ids = _parse_user_ids(
            self.env["ir.config_parameter"].sudo().get_param(
                CONFIG_PARAM_FINANCE_USERS, default=""
            )
        )
        if not ids:
            return self.env["res.users"].sudo().browse()
        return self.env["res.users"].sudo().browse(ids).exists()

    def _approver_partner_ids(self):
        self.ensure_one()
        users = self._get_approver_users()
        partners = users.mapped("partner_id").filtered(lambda p: p.email)
        return partners.ids

    def _finance_partner_ids(self):
        self.ensure_one()
        users = self._get_finance_users()
        partners = users.mapped("partner_id").filtered(lambda p: p.email)
        return partners.ids

    def _base_url(self):
        return self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""

    def action_submit(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Only draft requests can be submitted."))
            # if not rec.model_name:
            #     raise ValidationError(_("Model Name is required to submit the request."))
            if not rec.requested_amount or rec.requested_amount <= 0:
                raise ValidationError(_("Requested Amount must be greater than zero."))
            if not rec.description:
                raise ValidationError(_("Please provide a Description / Business Justification."))
            partner_ids = rec._approver_partner_ids()
            if not partner_ids:
                raise UserError(_(
                    "No approver recipients are configured. "
                    "Please configure them in Settings → ETP Projects → "
                    "Token Purchase Approvers (and ensure they have a valid email)."
                ))
            rec.write({
                "state": "pending",
                "approval_token": secrets.token_urlsafe(32),
                "token_used": False,
            })
            template = self.env.ref(
                "etp_projects.email_token_purchase_request_approval",
                raise_if_not_found=False,
            )
            if template:
                template.sudo().with_context(
                    approver_partner_ids=partner_ids,
                ).send_mail(rec.id, force_send=False)
            rec.message_post(
                body=_("Submitted for approval. Notification sent to approvers."),
                subtype_xmlid="mail.mt_note",
            )
        return True

    def _act_approve(self, user):
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_("Request is no longer pending approval."))
        if self.token_used:
            raise UserError(_("This approval link has already been used."))
        self.write({
            "state": "approved",
            "approver_id": user.id if user else False,
            "approval_date": fields.Datetime.now(),
            "token_used": True,
            "finance_token": secrets.token_urlsafe(32),
            "finance_token_used": False,
        })
        finance_partner_ids = self._finance_partner_ids()
        template = self.env.ref(
            "etp_projects.email_token_purchase_request_finance",
            raise_if_not_found=False,
        )
        if template and finance_partner_ids:
            template.sudo().with_context(
                finance_partner_ids=finance_partner_ids,
            ).send_mail(self.id, force_send=False)
        requester_template = self.env.ref(
            "etp_projects.email_token_purchase_request_approved_requester",
            raise_if_not_found=False,
        )
        if requester_template and self.requester_id.partner_id:
            requester_template.sudo().with_context(
                requester_partner_ids=[self.requester_id.partner_id.id],
            ).send_mail(self.id, force_send=False)
        approver_label = user.name if user else _("Email link (anonymous)")
        self.message_post(
            body=_("Approved by %s.") % approver_label,
            subtype_xmlid="mail.mt_note",
        )
        return True

    def _act_reject(self, user, reason=None):
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_("Request is no longer pending approval."))
        if self.token_used:
            raise UserError(_("This approval link has already been used."))
        vals = {
            "state": "rejected",
            "approver_id": user.id if user else False,
            "approval_date": fields.Datetime.now(),
            "token_used": True,
        }
        if reason:
            vals["rejection_reason"] = reason
        self.write(vals)
        template = self.env.ref(
            "etp_projects.email_token_purchase_request_rejected_requester",
            raise_if_not_found=False,
        )
        if template and self.requester_id.partner_id:
            template.sudo().with_context(
                requester_partner_ids=[self.requester_id.partner_id.id],
            ).send_mail(self.id, force_send=False)
        approver_label = user.name if user else _("Email link (anonymous)")
        self.message_post(
            body=_("Rejected by %s.") % approver_label,
            subtype_xmlid="mail.mt_note",
        )
        return True

    def _check_backend_approver(self):
        approver_ids = set(self._get_approver_users().ids)
        if self.env.uid not in approver_ids and not self.env.user.has_group("base.group_system"):
            raise UserError(_(
                "You are not in the configured Token Purchase Approvers list. "
                "Ask an administrator to add you in Settings → ETP Projects."
            ))

    def action_approve(self):
        self._check_backend_approver()
        for rec in self:
            rec._act_approve(self.env.user)
        return True

    def action_reject(self):
        self._check_backend_approver()
        for rec in self:
            rec._act_reject(self.env.user)
        return True

    def _do_complete(self, approved_amount, cost_center, attachment_ids,
                     completed_by_user, mark_finance_token_used=False):
        self.ensure_one()
        if self.state != "approved":
            raise UserError(_("Only approved requests can be completed."))
        if mark_finance_token_used and self.finance_token_used:
            raise UserError(_("This finance link has already been used."))
        try:
            amount_val = float(approved_amount or 0.0)
        except (TypeError, ValueError):
            raise ValidationError(_("Approved Amount must be a valid number."))
        if amount_val <= 0:
            raise ValidationError(_("Approved Amount must be greater than zero."))
        # if not cost_center or not str(cost_center).strip():
        #     raise ValidationError(_("Module / Cost Center is required to complete the request."))
        attachment_id_list = [int(a) for a in (attachment_ids or []) if a]
        if not attachment_id_list:
            raise ValidationError(_("Please attach at least one Supporting Document."))
        balance_before_val = self.budget_id.project_budget or 0.0
        vals = {
            "approved_amount": amount_val,
            "cost_center": str(cost_center).strip(),
            "supporting_document_ids": [(6, 0, attachment_id_list)],
            "state": "completed",
            "completed_date": fields.Datetime.now(),
            "completed_by_id": completed_by_user.id if completed_by_user else False,
            "balance_before": balance_before_val,
        }
        if mark_finance_token_used:
            vals["finance_token_used"] = True
        self.write(vals)
        self.budget_id.sudo().write({
            "project_budget": balance_before_val + amount_val,
        })
        completer_label = completed_by_user.name if completed_by_user else _("Finance link (anonymous)")
        self.budget_id.message_post(
            body=_(
                "Project Budget increased by %(amt).2f from completed "
                "token purchase request <b>%(name)s</b> (by %(who)s)."
            ) % {"amt": amount_val, "name": self.name, "who": completer_label},
            subtype_xmlid="mail.mt_note",
        )
        self.message_post(
            body=_("Marked as Completed by %s. Project Budget updated.") % completer_label,
            subtype_xmlid="mail.mt_note",
        )
        return True

    def action_complete(self):
        for rec in self:
            rec._do_complete(
                rec.approved_amount,
                rec.cost_center,
                rec.supporting_document_ids.ids,
                self.env.user,
                mark_finance_token_used=False,
            )
        return True

    @api.model
    def _get_budget_timeline_for_project(self, project_id, start=None, end=None, graph_days=30):
        today = fields.Date.today()
        end_date = end or today
        window_days = max(int(graph_days or 1), 1)
        start_date = start or (end_date - timedelta(days=window_days - 1))
        range_label = "%dd" % ((end_date - start_date).days + 1)

        empty = {
            "title": "Budget Added & Consumption Over Time",
            "range": range_label,
            "available_now": 0.0,
            "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "series": [],
        }
        if not project_id:
            return empty

        budgets = self.env["etp.project.aws.budget"].sudo().search(
            [("project_id", "=", project_id), ("active", "=", True)]
        )
        completed = self.sudo().search(
            [("project_id", "=", project_id), ("state", "=", "completed")],
            order="completed_date asc, id asc",
        )

        cost_lines = self.env["etp.project.aws.cost.line"].sudo().search(
            [("budget_id", "in", budgets.ids)] if budgets else [("id", "=", 0)],
            order="period asc",
        )

        daily_consumption = {}
        for cl in cost_lines:
            if not cl.period:
                continue
            daily_consumption[cl.period] = (
                daily_consumption.get(cl.period, 0.0) + (cl.amount_inr or 0.0)
            )

        daily_topups = {}
        for req in completed:
            if not req.completed_date:
                continue
            day = req.completed_date.date()
            daily_topups.setdefault(day, []).append(req)

        pre_added = sum(
            (req.approved_amount or 0.0)
            for req in completed
            if req.completed_date and req.completed_date.date() < start_date
        )
        pre_consumed = sum(amt for d, amt in daily_consumption.items() if d < start_date)

        event_dates = sorted(set(daily_consumption) | set(daily_topups))
        window_dates = [d for d in event_dates if start_date <= d <= end_date]

        series = []
        added_to_date = pre_added
        consumed_to_date = pre_consumed
        last_topup_consumed = pre_consumed

        for day in window_dates:
            added_today = sum(
                (req.approved_amount or 0.0) for req in daily_topups.get(day, [])
            )
            consumed_today = daily_consumption.get(day, 0.0)
            added_to_date += added_today
            consumed_to_date += consumed_today
            available = added_to_date - consumed_to_date
            point = {
                "date": day.isoformat(),
                "available_balance": round(available, 2),
                "consumed_to_date": round(consumed_to_date, 2),
                "added_to_date": round(added_to_date, 2),
                "event": {},
            }
            if added_today > 0:
                point["event"] = {
                    "type": "top_up",
                    "label": "Top-up",
                    "added": round(added_today, 2),
                    "available_after": round(available, 2),
                    "spent_since_last_topup": round(
                        consumed_to_date - last_topup_consumed, 2
                    ),
                }
                last_topup_consumed = consumed_to_date
            series.append(point)

        total_added_all = sum((req.approved_amount or 0.0) for req in completed)
        total_consumed_all = sum(daily_consumption.values())
        available_now = round(total_added_all - total_consumed_all, 2)

        return {
            "title": "Budget Added & Consumption Over Time",
            "range": range_label,
            "available_now": available_now,
            "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "series": series,
        }

    @api.model
    def _get_allocation_ledger_for_project(self, project_id):
        if not project_id:
            return {"title": "Allocation Ledger", "entries": []}
        requests = self.sudo().search(
            [("project_id", "=", project_id), ("state", "=", "completed")],
            order="completed_date desc, id desc",
        )
        entries = []
        for req in requests:
            project_name = req.project_id.name or ""
            entries.append({
                "datetime": req.completed_date.strftime("%Y-%m-%dT%H:%M:%SZ")
                            if req.completed_date else "",
                "action": "top_up",
                "action_label": ("%s %s" % (project_name, req.name)).strip(),
                "amount": req.approved_amount or 0.0,
                "balance_before": req.balance_before or 0.0,
            })
        return {"title": "Allocation Ledger", "entries": entries}

    def action_reset_to_draft(self):
        for rec in self:
            if rec.state not in ("rejected",):
                raise UserError(_("Only rejected requests can be reset to draft."))
            rec.write({
                "state": "draft",
                "approval_token": False,
                "token_used": False,
                "approver_id": False,
                "approval_date": False,
            })
        return True
