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
            ("withdrawn", "Withdrawn"),
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
    decision_note = fields.Text(string="Decision Note", tracking=True)

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
    can_complete = fields.Boolean(
        compute="_compute_can_complete", store=False,
        help="True when the current user is in the configured finance list "
             "and the request is approved (awaiting completion).",
    )
    can_withdraw = fields.Boolean(
        compute="_compute_can_withdraw", store=False,
        help="True when the current user is the requester and the request "
             "is still pending approval.",
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
            rec.can_approve = (
                is_approver
                and rec.requester_id.id != current_uid
                and rec.state == "pending"
            )

    @api.depends_context("uid")
    def _compute_can_complete(self):
        finance_ids = set(self._get_finance_users().ids)
        is_finance = self.env.uid in finance_ids
        for rec in self:
            rec.can_complete = is_finance and rec.state == "approved"

    @api.depends_context("uid")
    def _compute_can_withdraw(self):
        current_uid = self.env.uid
        for rec in self:
            rec.can_withdraw = (
                rec.requester_id.id == current_uid and rec.state == "pending"
            )

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

    def _act_approve(self, user, approved_amount=None, decision_note=None):
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_("Request is no longer pending approval."))
        if self.token_used:
            raise UserError(_("This approval link has already been used."))

        requested = float(self.requested_amount or 0.0)
        if approved_amount is None:
            amount_val = requested
        else:
            try:
                amount_val = float(approved_amount)
            except (TypeError, ValueError):
                raise ValidationError(_("Approved Amount must be a valid number."))
        if not (0 < amount_val <= requested):
            raise ValidationError(_(
                "Approved Amount must be greater than zero and at most the "
                "requested amount (%(req).2f)."
            ) % {"req": requested})
        note = (decision_note or "").strip()
        if amount_val < requested and not note:
            raise ValidationError(_(
                "A Decision Note is required when approving a partial amount."
            ))

        self.write({
            "state": "approved",
            "approver_id": user.id if user else False,
            "approval_date": fields.Datetime.now(),
            "token_used": True,
            "finance_token": secrets.token_urlsafe(32),
            "finance_token_used": False,
            "approved_amount": amount_val,
            "decision_note": note,
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
        self._cleanup_inactive_shell_budget()
        return True

    def _cleanup_inactive_shell_budget(self):
        """Remove the inactive shell budget this request initiated.

        If this request was an initial-budget shell (the linked budget is
        still inactive) and nothing else references that budget, remove the
        shell so the project can re-initiate. ``budget_id`` is
        ``ondelete='cascade'``, so unlinking the budget cascade-deletes this
        request too — the intended "clean re-initiation" behaviour. Top-ups
        run against active budgets, so they're untouched. Best-effort: never
        raise from cleanup — log and move on.
        """
        self.ensure_one()
        try:
            budget = self.budget_id.sudo()
            if budget and budget.exists() and not budget.active:
                other_requests = self.env[self._name].sudo().search_count([
                    ("budget_id", "=", budget.id),
                    ("id", "!=", self.id),
                ])
                if not other_requests:
                    budget.unlink()
        except Exception:
            _logger.exception(
                "Failed to clean up inactive shell budget for request %s",
                self.id,
            )

    def _check_backend_approver(self):
        approver_ids = set(self._get_approver_users().ids)
        if self.env.uid not in approver_ids:
            raise UserError(_(
                "You are not in the configured Token Purchase Approvers list. "
                "Ask an administrator to add you in Settings → ETP Projects."
            ))
        # Two-person rule: a request's requester cannot approve/reject it,
        # even if they are an approver or a system admin.
        for rec in self:
            if rec.requester_id and rec.requester_id.id == self.env.uid:
                raise UserError(_(
                    "You cannot approve or reject your own token purchase "
                    "request (self-approval is not permitted)."
                ))

    def _check_finance_user(self):
        finance_ids = set(self._get_finance_users().ids)
        if self.env.uid not in finance_ids:
            raise UserError(_(
                "You are not in the configured Token Purchase Finance Users "
                "list. Ask an administrator to add you in Settings → "
                "ETP Projects."
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
        # Reuse the amount locked at approval time when none is supplied.
        if approved_amount in (None, False, 0, 0.0, ""):
            amount_source = self.approved_amount or 0.0
        else:
            amount_source = approved_amount
        try:
            amount_val = float(amount_source or 0.0)
        except (TypeError, ValueError):
            raise ValidationError(_("Approved Amount must be a valid number."))
        requested = float(self.requested_amount or 0.0)
        if amount_val <= 0:
            raise ValidationError(_("Approved Amount must be greater than zero."))
        if requested and amount_val > requested:
            raise ValidationError(_(
                "Approved Amount cannot exceed the requested amount "
                "(%(req).2f)."
            ) % {"req": requested})
        if amount_val > 1_000_000:
            raise ValidationError(_(
                "Approved Amount cannot exceed 1,000,000."
            ))
        amount_val = round(amount_val, 2)
        # if not cost_center or not str(cost_center).strip():
        #     raise ValidationError(_("Module / Cost Center is required to complete the request."))
        attachment_id_list = [int(a) for a in (attachment_ids or []) if a]
        if not attachment_id_list:
            raise ValidationError(_("Please attach at least one Supporting Document."))
        balance_before_val = round(self.budget_id.project_budget or 0.0, 2)
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
        budget_vals = {
            "project_budget": round(balance_before_val + amount_val, 2),
        }
        # Completion is the only event that moves the cap (budget_amount —
        # what every screen's Remaining / Util% / Runway derive from).
        # Initial budget goes live on the first completion: a shell budget
        # created via the initial-budget route is inactive until approved+done,
        # and its pre-seeded cap (the *requested* amount) is corrected to the
        # amount actually granted. Top-ups add to the live cap.
        if not self.budget_id.active:
            budget_vals["active"] = True
            budget_vals["budget_amount"] = amount_val
        else:
            budget_vals["budget_amount"] = round(
                (self.budget_id.budget_amount or 0.0) + amount_val, 2
            )
        self.budget_id.sudo().write(budget_vals)
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

    def action_withdraw(self):
        for rec in self:
            if rec.state != "pending":
                raise UserError(_("Only pending requests can be withdrawn."))
            if rec.requester_id.id != self.env.uid:
                raise UserError(_(
                    "Only the requester can withdraw this request."
                ))
            rec.write({
                "state": "withdrawn",
                "approval_token": False,
                "token_used": False,
            })
            rec.message_post(
                body=_("Withdrawn by %s.") % (self.env.user.name or _("requester")),
                subtype_xmlid="mail.mt_note",
            )
            rec._cleanup_inactive_shell_budget()
        return True

    def action_update(self, vals):
        """Edit a draft/pending request in place (requester-only).

        Allows only ``model_name`` / ``description`` / ``requested_amount`` and
        never touches ``budget_id``. Applies the same validation as create.
        """
        self.ensure_one()
        if self.state not in ("draft", "pending"):
            raise UserError(_(
                "Only draft or pending requests can be edited."
            ))
        if self.requester_id.id != self.env.uid:
            raise UserError(_("Only the requester can edit this request."))

        vals = vals or {}
        write_vals = {}
        if "model_name" in vals:
            model_name = (vals.get("model_name") or "").strip()
            if not model_name:
                raise ValidationError(_("'model_name' cannot be empty."))
            write_vals["model_name"] = model_name
        if "description" in vals:
            description = (vals.get("description") or "").strip()
            if not description:
                raise ValidationError(_("'description' cannot be empty."))
            write_vals["description"] = description
        if "requested_amount" in vals:
            try:
                amount_val = float(vals.get("requested_amount"))
            except (TypeError, ValueError):
                raise ValidationError(_("'requested_amount' must be a number."))
            if amount_val <= 0:
                raise ValidationError(_(
                    "'requested_amount' must be greater than zero."
                ))
            if amount_val > 1_000_000:
                raise ValidationError(_(
                    "'requested_amount' cannot exceed 1,000,000."
                ))
            write_vals["requested_amount"] = round(amount_val, 2)
        if write_vals:
            self.write(write_vals)
        return True

    def _activity_entries(self):
        """Structured lifecycle trail (newest-first) built from the record's
        own fields — NOT chatter/mail HTML."""
        self.ensure_one()

        def _fmt(value):
            return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""

        requested = float(self.requested_amount or 0.0)
        approved = float(self.approved_amount or 0.0)
        requester_name = self.requester_id.name if self.requester_id else ""
        approver_name = self.approver_id.name if self.approver_id else ""
        completer_name = self.completed_by_id.name if self.completed_by_id else ""
        is_partial = (
            self.state in ("approved", "completed")
            and 0 < approved < requested
        )

        entries = []

        # Always: the original request.
        entries.append({
            "kind": "requested",
            "verb": "Requested",
            "amount": requested,
            "requested_amount": requested,
            "actor_name": requester_name,
            "actor_role": "",
            "occurred_at": _fmt(self.create_date),
            "ref": self.name or "",
            "is_partial": False,
            "badge": "This request",
        })

        if self.approver_id and self.approval_date and self.state in ("approved", "completed"):
            entries.append({
                "kind": "partial" if is_partial else "approved",
                "verb": "Approved",
                "amount": approved,
                "requested_amount": requested,
                "actor_name": approver_name,
                "actor_role": "",
                "occurred_at": _fmt(self.approval_date),
                "ref": self.name or "",
                "is_partial": is_partial,
                "badge": "Partial" if is_partial else None,
            })

        if self.state == "rejected" and self.approver_id:
            entries.append({
                "kind": "rejected",
                "verb": "Rejected",
                "amount": requested,
                "requested_amount": requested,
                "actor_name": approver_name,
                "actor_role": "",
                "occurred_at": _fmt(self.approval_date),
                "ref": self.name or "",
                "is_partial": False,
                "badge": None,
            })

        if self.state == "completed" and self.completed_date:
            entries.append({
                "kind": "completed",
                "verb": "Completed",
                "amount": approved,
                "requested_amount": requested,
                "actor_name": completer_name,
                "actor_role": "",
                "occurred_at": _fmt(self.completed_date),
                "ref": self.name or "",
                "is_partial": is_partial,
                "badge": None,
            })

        if self.state == "withdrawn":
            entries.append({
                "kind": "rejected",
                "verb": "Withdrawn",
                "amount": requested,
                "requested_amount": requested,
                "actor_name": requester_name,
                "actor_role": "",
                "occurred_at": _fmt(self.write_date or self.approval_date),
                "ref": self.name or "",
                "is_partial": False,
                "badge": None,
            })

        # Newest-first.
        entries.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)
        return entries
