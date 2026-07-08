from odoo import api, fields, models


class EtpAwsPricingSyncLog(models.Model):
    _name = "etp.aws.pricing.sync.log"
    _description = "AWS Pricing Sync Audit Log"
    _order = "started_at desc"
    _rec_name = "batch_id"

    batch_id = fields.Char(
        string="Batch ID",
        required=True,
        index=True,
        help="Unique identifier for this sync run.",
    )
    started_at = fields.Datetime(string="Started At", required=True, index=True)
    ended_at = fields.Datetime(string="Ended At")
    duration_seconds = fields.Float(
        string="Duration (s)",
        compute="_compute_duration",
        store=True,
    )
    triggered_by = fields.Selection(
        [
            ("cron", "Cron"),
            ("manual", "Manual"),
            ("install", "Install"),
        ],
        string="Triggered By",
        default="cron",
        required=True,
    )
    user_id = fields.Many2one("res.users", string="Triggered By User")

    services_fetched = fields.Integer(string="Services Fetched")
    services_upserted = fields.Integer(string="Services Upserted")
    items_fetched = fields.Integer(string="Items Fetched")
    items_upserted = fields.Integer(string="Items Upserted")
    skus_fetched = fields.Integer(string="SKUs Fetched")
    skus_upserted = fields.Integer(string="SKUs Upserted")
    skus_deprecated = fields.Integer(string="SKUs Deprecated")
    api_call_count = fields.Integer(string="API Calls")

    status = fields.Selection(
        [
            ("running", "Running"),
            ("success", "Success"),
            ("partial", "Partial"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="running",
        required=True,
    )
    error_message = fields.Text(string="Error Message")
    detail_log = fields.Text(string="Detail Log", help="Per-service breakdown or notes.")

    _sql_constraints = [
        ("batch_id_uniq", "unique(batch_id)", "Sync batch ID must be unique."),
    ]

    @api.depends("started_at", "ended_at")
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.ended_at:
                delta = rec.ended_at - rec.started_at
                rec.duration_seconds = delta.total_seconds()
            else:
                rec.duration_seconds = 0.0

    @api.model
    def action_run_sync_from_cron(self):
        from odoo.addons.etp_projects.services import aws_pricing_service as _ps
        return _ps.action_sync_all(self.env, triggered_by="cron")
