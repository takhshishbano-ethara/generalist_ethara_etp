import calendar
import logging
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EtpProjectAwsBudget(models.Model):
    _name = "etp.project.aws.budget"
    _description = "Project AWS Budget"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "project_id, name"
    _rec_name = "name"

    name = fields.Char(required=True, tracking=True)
    project_id = fields.Many2one(
        "project.project", required=True, ondelete="cascade", tracking=True,
    )
    active = fields.Boolean(default=True)

    budget_amount = fields.Monetary(
        currency_field="currency_id", required=True, tracking=True,
        string="Project Budget",
    )
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda s: s.env.ref("base.INR", raise_if_not_found=False)
        or s.env.company.currency_id,
    )
    usd_currency_id = fields.Many2one(
        "res.currency", compute="_compute_usd_currency", store=False,
    )

    aws_access_key_id = fields.Char(string="AWS Access Key ID")
    aws_secret_access_key = fields.Char(string="AWS Secret Access Key")
    aws_region = fields.Char(default="us-east-1", string="AWS Region")
    fetch_months = fields.Integer(default=6, string="Months to Fetch")

    tag_key = fields.Char(string="Tag Key", help="AWS cost-allocation tag key, e.g. 'team'.")
    tag_value = fields.Char(string="Tag Value", help="AWS cost-allocation tag value, e.g. 'alpha'.")

    last_fetched_at = fields.Datetime(readonly=True, tracking=True)
    cost_line_ids = fields.One2many("etp.project.aws.cost.line", "budget_id")
    cost_line_count = fields.Integer(compute="_compute_totals", store=False)

    total_consumed = fields.Monetary(
        currency_field="currency_id", compute="_compute_totals", store=False,
        string="Total Consumed",
    )
    percent_consumed = fields.Float(
        compute="_compute_totals", store=False, string="% Consumed",
    )
    remaining = fields.Monetary(
        currency_field="currency_id", compute="_compute_totals", store=False,
    )
    daily_burn_rate = fields.Monetary(
        currency_field="currency_id", compute="_compute_totals", store=False,
        string="Daily Burn",
        help="Latest complete month's consumed amount divided by the number of days in that month.",
    )

    alert_75_sent = fields.Boolean(readonly=True)
    alert_90_sent = fields.Boolean(readonly=True)
    alert_100_sent = fields.Boolean(readonly=True)
    notify_user_ids = fields.Many2many(
        "res.users", string="Extra Notify",
        help="Additional recipients for budget threshold alerts.",
    )

    def _compute_usd_currency(self):
        usd = self.env.ref("base.USD", raise_if_not_found=False)
        for rec in self:
            rec.usd_currency_id = usd or rec.currency_id

    @api.depends("cost_line_ids.amount_inr", "cost_line_ids.period", "budget_amount")
    def _compute_totals(self):
        for rec in self:
            rec.cost_line_count = len(rec.cost_line_ids)
            rec.total_consumed = sum(rec.cost_line_ids.mapped("amount_inr"))
            rec.remaining = (rec.budget_amount or 0.0) - rec.total_consumed
            if rec.budget_amount:
                rec.percent_consumed = (rec.total_consumed / rec.budget_amount) * 100.0
            else:
                rec.percent_consumed = 0.0
            periods = [p for p in rec.cost_line_ids.mapped("period") if p]
            if periods:
                latest = max(periods)
                latest_total = sum(
                    rec.cost_line_ids.filtered(lambda l: l.period == latest).mapped("amount_inr")
                )
                days_in_month = calendar.monthrange(latest.year, latest.month)[1]
                rec.daily_burn_rate = latest_total / days_in_month if days_in_month else 0.0
            else:
                rec.daily_burn_rate = 0.0

    def action_fetch_cost(self):
        messages = []
        notif_type = "success"
        for rec in self:
            try:
                created, updated = rec._fetch_cost_one()
                messages.append(
                    "%s [%s=%s]: +%s new, %s updated"
                    % (rec.name, rec.tag_key or "?", rec.tag_value or "?", created, updated)
                )
                rec._maybe_alert_thresholds()
            except UserError as e:
                messages.append("%s ERROR: %s" % (rec.name, e))
                notif_type = "warning"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": notif_type,
                "title": _("Project AWS cost fetch"),
                "message": "\n".join(messages) or _("No records."),
                "sticky": True,
            },
        }

    def _fetch_cost_one(self):
        self.ensure_one()
        if not (self.aws_access_key_id and self.aws_secret_access_key and self.aws_region):
            raise UserError(_("Set AWS Access Key ID, Secret, and Region first."))
        if not (self.tag_key and self.tag_value):
            raise UserError(_("Set Tag Key and Tag Value first."))
        try:
            import boto3
        except ImportError:
            raise UserError(_("Python package 'boto3' is not installed."))
        end = date.today().replace(day=1)
        start = end - relativedelta(months=self.fetch_months or 6)
        client = boto3.client(
            "ce",
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.aws_region,
        )
        try:
            resp = client.get_cost_and_usage(
                TimePeriod={
                    "Start": start.strftime("%Y-%m-%d"),
                    "End": end.strftime("%Y-%m-%d"),
                },
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Tags": {"Key": self.tag_key, "Values": [self.tag_value]}},
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except Exception as e:
            raise UserError(_("AWS Cost Explorer failed: %s") % e)
        Line = self.env["etp.project.aws.cost.line"]
        created = updated = 0
        for result in resp.get("ResultsByTime", []):
            period = fields.Date.from_string(result["TimePeriod"]["Start"])
            for group in result.get("Groups", []):
                service_name = (group["Keys"][0] if group.get("Keys") else "").strip() or "Unknown"
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount == 0.0:
                    continue
                existing = Line.search(
                    [
                        ("budget_id", "=", self.id),
                        ("period", "=", period),
                        ("service_name", "=", service_name),
                    ],
                    limit=1,
                )
                vals = {
                    "budget_id": self.id,
                    "period": period,
                    "service_name": service_name,
                    "amount_source": amount,
                }
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Line.create(vals)
                    created += 1
        self.last_fetched_at = fields.Datetime.now()
        return created, updated

    def _maybe_alert_thresholds(self):
        self.ensure_one()
        pct = self.percent_consumed or 0.0
        if pct >= 100.0 and not self.alert_100_sent:
            self._send_threshold_alert(100)
            self.alert_100_sent = True
        elif pct >= 90.0 and not self.alert_90_sent:
            self._send_threshold_alert(90)
            self.alert_90_sent = True
        elif pct >= 75.0 and not self.alert_75_sent:
            self._send_threshold_alert(75)
            self.alert_75_sent = True

    def _send_threshold_alert(self, threshold):
        self.ensure_one()
        recipients = set()
        project = self.project_id

        if project.user_id and project.user_id.partner_id:
            recipients.add(project.user_id.partner_id.id)

        for u in self.notify_user_ids:
            if u.partner_id:
                recipients.add(u.partner_id.id)

        role_employees = (
            project.project_lead
            | project.project_aire
            | project.project_swe
            | project.project_tasker
            | project.project_qc_reviewer
        )
        for emp in role_employees:
            partner = emp.work_contact_id or (emp.user_id.partner_id if emp.user_id else False)
            if partner:
                recipients.add(partner.id)

        for u in project.assigned_team_ids:
            if u.partner_id:
                recipients.add(u.partner_id.id)

        if not recipients:
            _logger.warning(
                "etp.project.aws.budget %s threshold %s: no recipients", self.name, threshold,
            )
            return
        subject = _("[AWS Budget %(t)s%%] %(n)s") % {"t": threshold, "n": self.name}
        body = _(
            "<p>Project <b>%(p)s</b> AWS budget <b>%(n)s</b> has reached "
            "<b>%(t)s%%</b> of the configured budget.</p>"
            "<ul>"
            "<li>Budget: %(b).2f %(c)s</li>"
            "<li>Consumed: %(used).2f %(c)s</li>"
            "<li>Remaining: %(r).2f %(c)s</li>"
            "<li>Tag: %(tk)s = %(tv)s</li>"
            "</ul>"
        ) % {
            "p": self.project_id.display_name,
            "n": self.name,
            "t": threshold,
            "b": self.budget_amount or 0.0,
            "used": self.total_consumed or 0.0,
            "r": self.remaining or 0.0,
            "c": self.currency_id.name or "",
            "tk": self.tag_key or "",
            "tv": self.tag_value or "",
        }
        self.message_post(
            body=body, subject=subject,
            partner_ids=list(recipients),
            subtype_xmlid="mail.mt_comment",
        )

    def action_reset_alerts(self):
        for rec in self:
            rec.alert_75_sent = False
            rec.alert_90_sent = False
            rec.alert_100_sent = False
        return True
