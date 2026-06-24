from odoo import _, api, fields, models
from odoo.exceptions import UserError


STATE_SELECTION = [
    ("draft", "Draft"),
    ("approved", "Approved"),
    ("in_progress", "In Progress"),
    ("delivered", "Delivered"),
    ("closed", "Closed"),
    ("rejected", "Rejected"),
    ("withdrawn", "Withdrawn"),
]

HEALTH_SELECTION = [
    ("unknown", "Unknown"),
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("at_risk", "At Risk"),
    ("critical", "Critical"),
]

HEALTH_HEALTHY_PCT = 60.0
HEALTH_WARNING_PCT = 80.0
HEALTH_AT_RISK_PCT = 100.0
ALERT_80_THRESHOLD = 80.0
ALERT_100_THRESHOLD = 100.0

ACTIVE_STATES = ("approved", "in_progress", "delivered", "closed")


class EtpBatchBudget(models.Model):
    _name = "etp.batch.budget"
    _description = "Batch Budget"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        string="Name",
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
        ondelete="restrict",
        tracking=True,
    )
    project_id = fields.Many2one(
        "project.project",
        string="Project",
        related="project_budget_id.project_id",
        store=True,
        readonly=True,
        index=True,
    )
    connected_model = fields.Char(
        string="Connected Model",
        related="project_id.connected_table",
        store=True,
        readonly=True,
    )
    model_line_ids = fields.One2many(
        "etp.batch.budget.model.line",
        "batch_id",
        string="Model Lines",
        copy=True,
    )
    infra_line_ids = fields.One2many(
        "etp.batch.budget.infra.line",
        "batch_id",
        string="Infrastructure Lines",
        copy=True,
    )
    request_ids = fields.One2many(
        "etp.batch.budget.request",
        "batch_id",
        string="Budget Requests",
    )
    request_count = fields.Integer(
        string="Request Count",
        compute="_compute_request_count",
    )
    total_tasks = fields.Integer(string="Total Tasks", tracking=True)
    estimated_cost = fields.Float(
        string="Estimated Cost (USD)",
        compute="_compute_estimated_cost",
        store=True,
    )
    buffer_pct = fields.Float(
        string="Buffer %",
        default=0.0,
        tracking=True,
        help="Buffer percentage applied on top of estimated cost.",
    )
    batch_budget = fields.Float(
        string="Batch Budget (USD)",
        compute="_compute_batch_budget",
        store=True,
        help="Initial estimate (estimated_cost + buffer). The actual "
             "spendable amount is Approved Amount.",
    )
    approved_amount = fields.Float(
        string="Approved Amount (USD)",
        compute="_compute_approved_amount",
        store=True,
        tracking=True,
        help="Sum of approved totals across all approved budget requests.",
    )
    start_date = fields.Date(string="Start Date", tracking=True)
    end_date = fields.Date(string="End Date", tracking=True)
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
    delivered_date = fields.Datetime(readonly=True, tracking=True)
    closed_remaining = fields.Float(
        string="Returned to Project (USD)",
        readonly=True,
        tracking=True,
        help="Unused approved amount returned to the Project Budget on delivery.",
    )
    carried_over_amount = fields.Float(
        string="Carried Over from Project (USD)",
        default=0.0,
        readonly=True,
        tracking=True,
        copy=False,
        help="Project budget remaining auto-allocated to this batch on creation or restart.",
    )
    consumed_cost = fields.Float(
        string="Consumed (AWS) USD",
        compute="_compute_consumed_cost",
        help="Sum of project AWS cost lines within the batch date range.",
    )
    consumed_pct = fields.Float(
        string="Consumed %",
        compute="_compute_consumed_cost",
    )
    remaining_cost = fields.Float(
        string="Remaining (USD)",
        compute="_compute_consumed_cost",
    )
    health_status = fields.Selection(
        HEALTH_SELECTION,
        string="Budget Health",
        compute="_compute_health_status",
        store=False,
    )
    matched_cost_line_ids = fields.Many2many(
        "etp.project.aws.cost.line",
        compute="_compute_matched_cost_lines",
        string="Matched Cost Lines",
    )
    alert_80_sent = fields.Boolean(readonly=True, copy=False)
    alert_100_sent = fields.Boolean(readonly=True, copy=False)
    daily_task_ids = fields.One2many(
        "etp.batch.budget.daily.task",
        "batch_id",
        string="Daily Task Log",
    )
    connected_record_ids = fields.One2many(
        "etp.batch.budget.connected.record",
        "batch_id",
        string="Connected Records",
    )
    info_link_ids = fields.One2many(
        "etp.batch.budget.info.link",
        "batch_id",
        string="General Info Links",
    )
    s3_link = fields.Char(string="S3 Link")
    feedback_ids = fields.One2many(
        "etp.batch.budget.feedback",
        "batch_id",
        string="Client Feedback",
    )
    active = fields.Boolean(default=True)

    @api.depends("request_ids")
    def _compute_request_count(self):
        for rec in self:
            rec.request_count = len(rec.request_ids)

    @api.depends("total_tasks", "model_line_ids.per_task_cost")
    def _compute_estimated_cost(self):
        for rec in self:
            per_task = sum(rec.model_line_ids.mapped("per_task_cost"))
            rec.estimated_cost = (rec.total_tasks or 0) * per_task

    @api.depends("estimated_cost", "buffer_pct")
    def _compute_batch_budget(self):
        for rec in self:
            buffer = (rec.buffer_pct or 0.0) / 100.0
            rec.batch_budget = (rec.estimated_cost or 0.0) * (1.0 + buffer)

    @api.depends(
        "request_ids.state",
        "request_ids.approved_total",
        "carried_over_amount",
    )
    def _compute_approved_amount(self):
        for rec in self:
            request_total = sum(
                req.approved_total
                for req in rec.request_ids
                if req.state in ("approved", "partially_approved")
            )
            rec.approved_amount = (rec.carried_over_amount or 0.0) + request_total

    def _cost_line_domain(self):
        self.ensure_one()
        if not (self.project_id and self.start_date and self.end_date):
            return None
        return [
            ("project_id", "=", self.project_id.id),
            ("granularity", "=", "day"),
            ("period", ">=", self.start_date),
            ("period", "<=", self.end_date),
        ]

    @api.depends("project_id", "start_date", "end_date", "approved_amount")
    def _compute_consumed_cost(self):
        Line = self.env["etp.project.aws.cost.line"]
        for rec in self:
            domain = rec._cost_line_domain()
            if not domain:
                rec.consumed_cost = 0.0
                rec.consumed_pct = 0.0
                rec.remaining_cost = rec.approved_amount or 0.0
                continue
            lines = Line.sudo().search(domain)
            consumed = sum(lines.mapped("amount_source"))
            rec.consumed_cost = consumed
            rec.remaining_cost = (rec.approved_amount or 0.0) - consumed
            rec.consumed_pct = (
                (consumed / rec.approved_amount) * 100.0
                if rec.approved_amount else 0.0
            )

    @api.depends("approved_amount", "consumed_pct", "state")
    def _compute_health_status(self):
        for rec in self:
            if rec.state == "draft" or not rec.approved_amount:
                rec.health_status = "unknown"
                continue
            pct = rec.consumed_pct or 0.0
            if pct < HEALTH_HEALTHY_PCT:
                rec.health_status = "healthy"
            elif pct < HEALTH_WARNING_PCT:
                rec.health_status = "warning"
            elif pct < HEALTH_AT_RISK_PCT:
                rec.health_status = "at_risk"
            else:
                rec.health_status = "critical"

    @api.depends("project_id", "start_date", "end_date")
    def _compute_matched_cost_lines(self):
        Line = self.env["etp.project.aws.cost.line"]
        for rec in self:
            domain = rec._cost_line_domain()
            if not domain:
                rec.matched_cost_line_ids = [(5, 0, 0)]
                continue
            lines = Line.sudo().search(domain, order="period desc, service_name")
            rec.matched_cost_line_ids = [(6, 0, lines.ids)]

    @api.onchange("project_budget_id")
    def _onchange_project_budget_id(self):
        if self.project_budget_id and not self.model_line_ids:
            self.model_line_ids = [
                (0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "per_task_cost": line.per_task_cost,
                })
                for line in self.project_budget_id.model_line_ids
            ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("etp.batch.budget")
                    or "New"
                )
        records = super().create(vals_list)
        for rec in records:
            if rec.project_budget_id and not rec.model_line_ids:
                rec.model_line_ids = [
                    (0, 0, {
                        "ai_model_id": line.ai_model_id.id,
                        "per_task_cost": line.per_task_cost,
                    })
                    for line in rec.project_budget_id.model_line_ids
                ]
            if (
                rec.project_budget_id
                and not rec.carried_over_amount
            ):
                pool = rec.project_budget_id.batch_budget_remain or 0.0
                if pool > 0.0:
                    rec.carried_over_amount = pool
                    rec.project_budget_id.batch_budget_remain = 0.0
        return records

    def action_refresh_model_lines(self):
        for rec in self:
            if not rec.project_budget_id:
                raise UserError(_("Pick a Project Budget first."))
            if rec.state != "draft":
                raise UserError(_(
                    "Model lines can only be refreshed in Draft state."
                ))
            rec.model_line_ids = [(5, 0, 0)] + [
                (0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "per_task_cost": line.per_task_cost,
                })
                for line in rec.project_budget_id.model_line_ids
            ]

    def action_open_request_wizard(self):
        self.ensure_one()
        if self.state in ("delivered", "closed", "rejected", "withdrawn"):
            raise UserError(_(
                "Cannot open a new request for a %s batch."
            ) % dict(STATE_SELECTION).get(self.state, self.state))
        if not self.project_budget_id:
            raise UserError(_("Pick a Project Budget first."))
        if not self.project_budget_id.approver_user_ids:
            raise UserError(_(
                "Project Budget has no approvers configured."
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Request Batch Budget"),
            "res_model": "etp.batch.budget.request.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_batch_id": self.id},
        }

    def action_view_requests(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Budget Requests"),
            "res_model": "etp.batch.budget.request",
            "view_mode": "list,form",
            "domain": [("batch_id", "=", self.id)],
            "context": {"default_batch_id": self.id},
        }

    def action_withdraw(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(_(
                    "Only Draft batches can be withdrawn."
                ))
            if rec.requester_id and self.env.user != rec.requester_id:
                raise UserError(_(
                    "Only the requester can withdraw this batch."
                ))
            rec.state = "withdrawn"

    def action_reset_to_draft(self):
        for rec in self:
            if rec.state not in ("rejected", "withdrawn"):
                raise UserError(_(
                    "Only Rejected or Withdrawn batches can be reset."
                ))
            rec.write({
                "state": "draft",
                "approver_id": False,
                "approval_date": False,
                "rejection_reason": False,
            })

    def action_deliver(self):
        for rec in self:
            if rec.state not in ("approved", "in_progress"):
                raise UserError(_(
                    "Only Approved or In Progress batches can be delivered."
                ))
            remaining = (rec.approved_amount or 0.0) - (rec.consumed_cost or 0.0)
            if remaining < 0.0:
                remaining = 0.0
            rec.write({
                "state": "delivered",
                "delivered_date": fields.Datetime.now(),
                "closed_remaining": remaining,
            })
            if remaining > 0.0 and rec.project_budget_id:
                rec.project_budget_id.batch_budget_remain = (
                    rec.project_budget_id.batch_budget_remain or 0.0
                ) + remaining

    def action_close(self):
        for rec in self:
            if rec.state != "delivered":
                raise UserError(_(
                    "Only Delivered batches can be closed."
                ))
            rec.state = "closed"

    def action_restart(self):
        for rec in self:
            if rec.state != "delivered":
                raise UserError(_(
                    "Only Delivered batches can be restarted."
                ))
            project_budget = rec.project_budget_id
            pool = (project_budget.batch_budget_remain or 0.0) if project_budget else 0.0
            if pool < 0.0:
                pool = 0.0
            rec.write({
                "state": "in_progress",
                "delivered_date": False,
                "closed_remaining": 0.0,
                "carried_over_amount": (rec.carried_over_amount or 0.0) + pool,
            })
            if project_budget and pool > 0.0:
                project_budget.batch_budget_remain = 0.0

    def _cto_partner_ids(self):
        self.ensure_one()
        cto = self.project_budget_id.cto_user_id
        return cto.partner_id.ids if cto else []

    def _approver_partner_ids(self):
        self.ensure_one()
        return self.project_budget_id.approver_user_ids.mapped("partner_id").ids

    def _requester_partner_ids(self):
        self.ensure_one()
        return self.requester_id.partner_id.ids if self.requester_id else []

    def _threshold_alert_partner_ids(self):
        self.ensure_one()
        ids = set()
        ids.update(self._cto_partner_ids())
        ids.update(self._approver_partner_ids())
        ids.update(self._requester_partner_ids())
        return list(ids)

    def _send_mail(self, template_xmlid, partner_ids):
        self.ensure_one()
        if not partner_ids:
            return
        project = self.project_id
        if project:
            project._etp_post_budget_message(
                template_xmlid, self, partner_ids,
            )
            return
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            return
        template.send_mail(
            self.id,
            email_values={"partner_ids": [(6, 0, partner_ids)]},
            force_send=False,
        )

    def _check_threshold_alerts(self):
        for rec in self:
            if rec.state not in ("approved", "in_progress"):
                continue
            if not rec.approved_amount:
                continue
            pct = rec.consumed_pct or 0.0
            partner_ids = rec._threshold_alert_partner_ids()
            if not partner_ids:
                continue
            if pct >= ALERT_100_THRESHOLD and not rec.alert_100_sent:
                rec._send_mail(
                    "etp_projects.mail_template_batch_threshold",
                    partner_ids,
                )
                rec.alert_100_sent = True
                rec.alert_80_sent = True
            elif pct >= ALERT_80_THRESHOLD and not rec.alert_80_sent:
                rec._send_mail(
                    "etp_projects.mail_template_batch_threshold",
                    partner_ids,
                )
                rec.alert_80_sent = True
