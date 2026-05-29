from odoo import fields, models


class GohanJobLog(models.Model):
    _name = "gohan.job.log"
    _description = "Gohan Job Execution Log"
    _order = "timestamp asc, id asc"
    _rec_name = "message"

    job_id = fields.Many2one(
        "gohan.job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    timestamp = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    source = fields.Selection(
        [
            ("odoo", "Odoo"),
            ("lambda", "Lambda"),
        ],
        required=True,
        default="odoo",
        index=True,
    )
    level = fields.Selection(
        [
            ("DEBUG", "Debug"),
            ("INFO", "Info"),
            ("WARNING", "Warning"),
            ("ERROR", "Error"),
            ("CRITICAL", "Critical"),
        ],
        required=True,
        default="INFO",
    )
    message = fields.Text(required=True)
    pod = fields.Char(help="Pod hostname (for Odoo source)")

    def init(self):
        # Composite index for the common form-view query (by job, time desc).
        self.env.cr.execute(
            "CREATE INDEX IF NOT EXISTS gohan_job_log_job_ts_idx "
            "ON gohan_job_log (job_id, timestamp DESC)"
        )
