from odoo import api, fields, models


class VegetaJobLog(models.Model):
    _name = "vegeta.job.log"
    _description = "Vegeta Job Execution Log"
    _order = "timestamp asc, id asc"
    _rec_name = "message"

    job_id = fields.Many2one(
        "vegeta.job",
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
            ("worker", "Worker"),
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
    pod = fields.Char(help="Pod hostname (for Odoo / Worker sources)")

    def init(self):
        # Composite index for the common form-view query (by job, time desc).
        self.env.cr.execute(
            "CREATE INDEX IF NOT EXISTS vegeta_job_log_job_ts_idx "
            "ON vegeta_job_log (job_id, timestamp DESC)"
        )
