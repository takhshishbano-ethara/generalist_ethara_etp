from __future__ import annotations

import logging
import threading

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


BATCH_STATE_SELECTION = [
    ("draft", "Draft"),
    ("pending", "Pending"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]


class LynceusBatch(models.Model):
    _name = "lynceus.batch"
    _description = "Lynceus Generation Batch (one LLM run)"
    _order = "create_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        string="Batch ID",
        required=True,
        index=True,
        copy=False,
        default=lambda self: self.env["ir.sequence"].next_by_code("lynceus.batch.seq") or "/",
    )
    target_n = fields.Integer(
        string="Target Prompts (N)",
        required=True,
        default=3000,
        help="Manager-specified target number of net new prompts to add to the pool.",
    )
    generated_count = fields.Integer(
        string="Net Generated",
        readonly=True,
        copy=False,
    )
    dedup_rejected = fields.Integer(
        string="Dedup Rejected",
        readonly=True,
        copy=False,
        help="Number of LLM outputs discarded because their content hash was already in the History Registry.",
    )
    api_calls = fields.Integer(
        string="API Calls",
        readonly=True,
        copy=False,
    )
    cost_usd = fields.Float(
        string="Cost (USD)",
        readonly=True,
        copy=False,
        digits=(12, 4),
    )
    state = fields.Selection(
        BATCH_STATE_SELECTION,
        string="State",
        required=True,
        default="draft",
        copy=False,
        tracking=True,
    )
    started_at = fields.Datetime(string="Started At", readonly=True, copy=False)
    finished_at = fields.Datetime(string="Finished At", readonly=True, copy=False)
    triggered_by_id = fields.Many2one(
        "res.users",
        string="Triggered By",
        default=lambda self: self.env.user,
        readonly=True,
    )
    error_log = fields.Text(string="Error Log", readonly=True, copy=False)
    progress_pct = fields.Float(
        string="Progress",
        compute="_compute_progress_pct",
        store=False,
        help="Generated vs target as a percentage.",
    )
    prompt_ids = fields.One2many(
        "lynceus.prompt",
        "batch_id",
        string="Generated Prompts",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        index=True,
    )

    _sql_constraints = [
        (
            "lynceus_batch_name_uniq",
            "UNIQUE(name)",
            "Batch name must be unique.",
        ),
    ]

    @api.depends("generated_count", "target_n")
    def _compute_progress_pct(self):
        for rec in self:
            if rec.target_n and rec.target_n > 0:
                rec.progress_pct = min(100.0, (rec.generated_count / rec.target_n) * 100.0)
            else:
                rec.progress_pct = 0.0

    def action_queue(self):
        self.ensure_one()
        if self.state in ("running", "done"):
            return
        self.write({
            "state": "pending",
            "started_at": False,
            "finished_at": False,
            "error_log": False,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Batch Queued"),
                "message": _(
                    "Batch %s is pending. It will start within 1 minute. "
                    "You can leave this page \u2014 progress is saved as it runs."
                ) % self.name,
                "type": "success",
                "sticky": False,
            },
        }

    def action_run(self):
        from ..services import batch_orchestrator
        self.ensure_one()
        if self.state not in ("draft", "failed", "pending"):
            return
        self.write({
            "state": "running",
            "started_at": fields.Datetime.now(),
            "error_log": False,
        })
        if not getattr(threading.current_thread(), "testing", False):
            self.env.cr.commit()
        try:
            batch_orchestrator.run(self.env, self)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Lynceus batch %s failed: %s", self.name, exc)
            self.write({
                "state": "failed",
                "error_log": str(exc),
                "finished_at": fields.Datetime.now(),
            })
            return
        self.invalidate_recordset(["generated_count", "api_calls"])
        final_state = "done" if self.generated_count > 0 else "failed"
        self.write({
            "state": final_state,
            "finished_at": fields.Datetime.now(),
        })

    @api.model
    def _cron_run_pending(self):
        self.env.cr.execute("""
            SELECT id FROM lynceus_batch
            WHERE state = 'pending'
            ORDER BY create_date ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        """)
        row = self.env.cr.fetchone()
        if not row:
            return
        batch = self.browse(row[0])
        _logger.info("Lynceus cron picked pending batch %s (id=%s, N=%s)",
                     batch.name, batch.id, batch.target_n)
        batch.action_run()

    @api.model
    def get_dashboard_data(self, filters=None):
        if not isinstance(filters, dict):
            filters = {}

        Prompt = self.env["lynceus.prompt"].sudo()
        Batch = self.env["lynceus.batch"].sudo()
        Users = self.env["res.users"].sudo()
        ICP = self.env["ir.config_parameter"].sudo()

        date_from = filters.get("date_from") or fields.Date.context_today(self).isoformat()
        date_to = filters.get("date_to") or fields.Date.context_today(self).isoformat()

        from_dt = f"{date_from} 00:00:00"
        to_dt = f"{date_to} 23:59:59"

        available = Prompt.search_count([("state", "=", "available")])
        assigned = Prompt.search_count([("state", "=", "assigned")])
        used = Prompt.search_count([("state", "=", "used")])
        bad = Prompt.search_count([("state", "=", "bad")])

        depletion_threshold = int(ICP.get_param("lynceus.pool_depletion_threshold", "500") or "500")
        if available <= 0:
            pool_status = "danger"
        elif available < depletion_threshold:
            pool_status = "warning"
        else:
            pool_status = "ok"

        active_today = Users.search_count([("lynceus_active_today", "=", True)])
        enrolled = Users.search_count([("lynceus_daily_quota", ">", 0)])

        used_in_range = Prompt.search_count([
            ("state", "=", "used"),
            ("outcome_at", ">=", from_dt),
            ("outcome_at", "<=", to_dt),
        ])
        bad_in_range = Prompt.search_count([
            ("state", "=", "bad"),
            ("outcome_at", ">=", from_dt),
            ("outcome_at", "<=", to_dt),
        ])

        last_batch = Batch.search([], order="create_date desc", limit=1)
        last_batch_info = {
            "id": last_batch.id if last_batch else 0,
            "name": last_batch.name if last_batch else "—",
            "state": last_batch.state if last_batch else "",
        }

        pool_breakdown = [
            {"label": "Available", "value": available, "key": "available"},
            {"label": "Assigned", "value": assigned, "key": "assigned"},
            {"label": "Used", "value": used, "key": "used"},
            {"label": "Bad", "value": bad, "key": "bad"},
        ]

        self.env.cr.execute("""
            SELECT u.id, u.login AS label, COUNT(p.id) AS cnt
            FROM lynceus_prompt p
            JOIN res_users u ON u.id = p.assigned_user_id
            WHERE p.state = 'assigned'
            GROUP BY u.id, u.login
            ORDER BY cnt DESC
            LIMIT 10
        """)
        per_tasker = [
            {"id": r[0], "label": r[1] or f"user-{r[0]}", "count": r[2]}
            for r in self.env.cr.fetchall()
        ]

        live_batches = Batch.search(
            [("state", "in", ["pending", "running"])],
            order="create_date desc",
            limit=10,
        )
        live_batches_data = [
            {
                "id": b.id,
                "name": b.name,
                "state": b.state,
                "target_n": b.target_n,
                "generated_count": b.generated_count,
                "progress_pct": round(b.progress_pct, 1),
            }
            for b in live_batches
        ]

        self.env.cr.execute("""
            SELECT u.id, u.login AS label, COUNT(p.id) AS cnt
            FROM lynceus_prompt p
            JOIN res_users u ON u.id = p.assigned_user_id
            WHERE p.state = 'used'
              AND p.outcome_at >= %s
              AND p.outcome_at <= %s
            GROUP BY u.id, u.login
            ORDER BY cnt DESC
            LIMIT 10
        """, (from_dt, to_dt))
        top_submitters = [
            {"id": r[0], "label": r[1] or f"user-{r[0]}", "count": r[2]}
            for r in self.env.cr.fetchall()
        ]

        return {
            "kpis": {
                "pool_available": available,
                "pool_status": pool_status,
                "pool_threshold": depletion_threshold,
                "in_flight": assigned,
                "in_flight_taskers": active_today,
                "completed_in_range": used_in_range,
                "bad_in_range": bad_in_range,
                "active_taskers": active_today,
                "enrolled_taskers": enrolled,
                "last_batch": last_batch_info,
                "total_used": used,
                "total_bad": bad,
            },
            "pool_breakdown": pool_breakdown,
            "per_tasker": per_tasker,
            "live_batches": live_batches_data,
            "top_submitters": top_submitters,
            "filters_echo": {
                "date_from": date_from,
                "date_to": date_to,
            },
        }
