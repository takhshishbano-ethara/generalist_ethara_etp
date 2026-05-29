"""Leviathan Job Execution Log — per-job tail of every `[job=N]` tagged
log record from anywhere in the addon (UI pod, worker pod, Lambda
CloudWatch).

Read-only from the form view; written exclusively by:
  * ``log_handler.LeviathanJobLogHandler`` — auto-scrapes Python logging
    records emitted by ``odoo.addons.leviathan.*``.
  * ``action_refresh_lambda_logs`` on ``leviathan.job`` — admin-only
    CloudWatch fetch, keyed on ``lambda_request_id`` with watermark
    pagination (``last_lambda_log_ts``).

Composite index ``(job_id, timestamp DESC)`` because the form-view query
is always "latest N for this job."  Source filter lets the tasker scope
to Odoo / Worker / Lambda independently when triaging.
"""
from odoo import api, fields, models


class LeviathanJobLog(models.Model):
    _name = "leviathan.job.log"
    _description = "Leviathan Job Execution Log"
    _order = "timestamp asc, id asc"
    _rec_name = "message"

    job_id = fields.Many2one(
        "leviathan.job",
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
        help="Origin of the log record. `odoo` = UI pod / webhook / "
             "Odoo cron. `worker` = standalone PRD worker process. "
             "`lambda` = AWS CloudWatch (pulled by Refresh Lambda Logs).",
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
    pod = fields.Char(
        help="Pod hostname (for Odoo / Worker sources). Empty for Lambda "
             "records — the Lambda RequestId carries that role instead.",
    )

    def init(self):
        # Composite index for the dominant access pattern: "latest N rows
        # for one job, in time order." A plain index on job_id would still
        # need a Sort step; the composite index lets Postgres do an
        # index-only scan for the typical 200-row form-view tail.
        self.env.cr.execute(
            "CREATE INDEX IF NOT EXISTS leviathan_job_log_job_ts_idx "
            "ON leviathan_job_log (job_id, timestamp DESC)"
        )
