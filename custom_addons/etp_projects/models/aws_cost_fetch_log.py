from odoo import api, fields, models

class EtpProjectAwsCostFetchLog(models.Model):
    _name = "etp.project.aws.cost.fetch.log"
    _description = "Project AWS Cost Fetch History"
    _order = "fetched_at desc, id desc"
    _rec_name = "display_name"

    budget_id = fields.Many2one(
        "etp.project.aws.budget", required=True, ondelete="cascade", index=True,
    )
    project_id = fields.Many2one(
        "project.project", related="budget_id.project_id",
        store=True, index=True, readonly=True,
    )
    fetched_at = fields.Datetime(required=True, index=True, default=fields.Datetime.now)
    triggered_by_id = fields.Many2one("res.users", string="Triggered By", index=True)

    SOURCE_SELECTION = [
        ("manual_button", "Manual (Form Button)"),
        ("api_update_all", "API: Update All"),
        ("api_crowley", "API: Crowley"),
        ("api_vegeta", "API: Vegeta"),
        ("cron", "Scheduled"),
        ("other", "Other"),
    ]
    source = fields.Selection(SOURCE_SELECTION, required=True, default="other", index=True)

    status = fields.Selection(
        [("success", "Success"), ("error", "Error")],
        required=True, default="success", index=True,
    )
    created_count = fields.Integer(string="Created Cost Lines")
    updated_count = fields.Integer(string="Updated Cost Lines")
    error_message = fields.Text()

    tag_key = fields.Char(string="Tag Key (snapshot)")
    tag_value = fields.Char(string="Tag Value (snapshot)")
    tags_summary = fields.Char(string="Tags (snapshot)")
    fetch_months = fields.Integer(string="Months Fetched (snapshot)")

    api_hit_count = fields.Integer(string="API Hits")
    api_hit_cost_usd = fields.Float(string="API Hit Cost (USD)", digits=(12, 4))

    display_name = fields.Char(compute="_compute_display_name", store=False)

    @api.depends("budget_id.name", "fetched_at", "status")
    def _compute_display_name(self):
        for rec in self:
            stamp = (
                fields.Datetime.to_string(rec.fetched_at) if rec.fetched_at else ""
            )
            rec.display_name = "%s - %s [%s]" % (
                rec.budget_id.name or "?", stamp, rec.status or "",
            )

    def to_api_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "budget_id": self.budget_id.id,
            "budget_name": self.budget_id.name or "",
            "project_id": self.project_id.id if self.project_id else False,
            "project_name": self.project_id.name if self.project_id else "",
            "fetched_at": (
                self.fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                if self.fetched_at else ""
            ),
            "triggered_by_id": (
                self.triggered_by_id.id if self.triggered_by_id else False
            ),
            "triggered_by_name": (
                self.triggered_by_id.name if self.triggered_by_id else ""
            ),
            "source": self.source or "",
            "status": self.status or "",
            "created_count": int(self.created_count or 0),
            "updated_count": int(self.updated_count or 0),
            "error_message": self.error_message or "",
            "tag_key": self.tag_key or "",
            "tag_value": self.tag_value or "",
            "fetch_months": int(self.fetch_months or 0),
            "api_hit_count": int(self.api_hit_count or 0),
            "api_hit_cost_usd": round(float(self.api_hit_cost_usd or 0.0), 4),
        }
