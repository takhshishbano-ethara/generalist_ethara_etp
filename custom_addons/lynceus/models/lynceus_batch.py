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

# Postgres advisory-lock namespace prefix paired with batch.id.
# "LYNC" = 0x4C594E43. Acts as a project-unique 32-bit key so we never
# collide with another advisory-lock user in the same database.
LYNCEUS_BATCH_ADVISORY_LOCK_NAMESPACE = 0x4C594E43

# Heartbeat thresholds (seconds). With flush_pending firing every wave
# (~25-30s) plus per-bulk_chunk flushes, healthy heartbeat age stays <60s.
# Above HEARTBEAT_RECOVERY_THRESHOLD we treat the batch as "process likely
# died"; the cron + advisory-lock layer should resume it. Above
# HEARTBEAT_STUCK_THRESHOLD we declare auto-recovery failed and surface
# admin-only diagnostics + force-release button.
HEARTBEAT_RECOVERY_THRESHOLD = 90
HEARTBEAT_STUCK_THRESHOLD = 600


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
        string="LLM Calls",
        readonly=True,
        copy=False,
        help="Number of LLM (Gemini) calls made by the orchestrator for this batch.",
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
    llm_call_ids = fields.One2many(
        "lynceus.llm.call",
        "batch_id",
        string="LLM Calls",
    )
    llm_returned_total = fields.Integer(
        string="LLM Returned Total",
        compute="_compute_llm_accounting",
        help="Sum of prompts actually returned by the LLM across all calls "
             "(before dedup or parse loss). When this exceeds Net Generated + "
             "Dedup Rejected, the difference is Parse Loss.",
    )
    parse_loss = fields.Integer(
        string="Parse Loss",
        compute="_compute_llm_accounting",
        help="Prompts received from the LLM but never inserted into the pool. "
             "Typical sources: partial JSON returns (call returned 18 of 20), "
             "truncation at maxOutputTokens, malformed JSON salvage, or silent "
             "drops when target was hit mid-batch. Inspect LLM Call Details for "
             "rows where Returned < Requested.",
    )
    last_heartbeat_at = fields.Datetime(
        string="Last Heartbeat",
        readonly=True,
        copy=False,
        help="Updated by the orchestrator every wave commit. Used by the "
             "stuck/recovery detection and surfaced in the admin UI so "
             "operators can see how recently the worker process wrote progress.",
    )
    last_call_at = fields.Datetime(
        string="Last LLM Activity",
        compute="_compute_stuck_diagnosis",
    )
    seconds_since_last_activity = fields.Integer(
        string="Seconds Since Last Activity",
        compute="_compute_stuck_diagnosis",
    )
    is_recovering = fields.Boolean(
        string="Auto-Recovery In Progress",
        compute="_compute_stuck_diagnosis",
        help="True when state='running' but the heartbeat is older than the "
             "recovery threshold. The Postgres advisory lock that protected "
             "the dead worker should release shortly and the cron job will "
             "resume the batch from the last committed generated_count.",
    )
    is_stuck = fields.Boolean(
        string="Is Stuck",
        compute="_compute_stuck_diagnosis",
        help="True when auto-recovery should have kicked in but the heartbeat "
             "is still stale after the stuck threshold. Usually points at a "
             "PgBouncer transaction-mode issue or a disabled cron - admin "
             "intervention required.",
    )
    stuck_diagnosis = fields.Text(
        string="Stuck Diagnosis",
        compute="_compute_stuck_diagnosis",
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

    @api.depends("llm_call_ids.returned_count", "generated_count", "dedup_rejected")
    def _compute_llm_accounting(self):
        for rec in self:
            sudo_calls = rec.sudo().llm_call_ids
            total_returned = sum(sudo_calls.mapped("returned_count"))
            rec.llm_returned_total = total_returned
            rec.parse_loss = max(
                0,
                total_returned - rec.generated_count - rec.dedup_rejected,
            )

    @api.depends("state", "llm_call_ids.create_date", "started_at", "last_heartbeat_at")
    def _compute_stuck_diagnosis(self):
        now = fields.Datetime.now()
        for rec in self:
            sudo_calls = rec.sudo().llm_call_ids
            create_dates = [d for d in sudo_calls.mapped("create_date") if d]
            rec.last_call_at = max(create_dates) if create_dates else False

            reference = rec.last_heartbeat_at or rec.last_call_at or rec.started_at
            if rec.state != "running" or not reference:
                rec.seconds_since_last_activity = 0
                rec.is_recovering = False
                rec.is_stuck = False
                rec.stuck_diagnosis = False
                continue

            elapsed = int((now - reference).total_seconds())
            rec.seconds_since_last_activity = elapsed
            rec.is_recovering = (
                HEARTBEAT_RECOVERY_THRESHOLD <= elapsed < HEARTBEAT_STUCK_THRESHOLD
            )
            rec.is_stuck = elapsed >= HEARTBEAT_STUCK_THRESHOLD

            if rec.is_recovering:
                rec.stuck_diagnosis = (
                    "AUTO-RECOVERY IN PROGRESS\n"
                    "=========================\n"
                    f"Last heartbeat: {elapsed} seconds ago.\n"
                    "The orchestrator process appears to have stopped "
                    "writing progress. The Postgres advisory lock that "
                    "protects this batch will be auto-released by the "
                    "database within ~30 seconds of the worker's death, "
                    "and the next cron tick (every 60s) will pick up the "
                    "orphan and resume from "
                    f"{rec.generated_count}/{rec.target_n}.\n\n"
                    "No action needed - refresh this page in 30-60 seconds."
                )
            elif rec.is_stuck:
                mins = elapsed // 60
                remaining_prompts = max(0, rec.target_n - rec.generated_count)
                rec.stuck_diagnosis = (
                    "AUTO-RECOVERY FAILED\n"
                    "====================\n"
                    f"Last heartbeat: {mins} minutes ago ({elapsed} seconds).\n"
                    "Auto-recovery should have resumed this batch within "
                    "1-2 minutes via the cron + Postgres advisory-lock "
                    "mechanism, but the batch has been stuck for over "
                    "10 minutes.\n\n"
                    "Possible causes:\n"
                    "  - PgBouncer in transaction/statement mode (advisory "
                    "    locks do not survive across transactions).\n"
                    "  - Cron job disabled or its worker is failing to run.\n"
                    "  - Database connection pool exhausted (cron cannot "
                    "    acquire a connection to probe the lock).\n\n"
                    f"What is safe:\n"
                    f"  The {rec.generated_count} prompts already shown "
                    "above are fully committed to the pool. They are "
                    "unaffected.\n\n"
                    "Recovery (admin):\n"
                    "  1. Click the red 'Mark as Failed (Force)' button "
                    "above.\n"
                    f"  2. Trigger a new Generate Batch with target_n = "
                    f"{remaining_prompts} to fill the remainder.\n"
                    "  3. Investigate cron + connection pool health "
                    "in logs."
                )
            else:
                rec.stuck_diagnosis = False

    def action_mark_failed(self):
        self.ensure_one()
        if self.state != "running":
            from odoo.exceptions import UserError
            raise UserError(_(
                "Only batches in 'running' state can be marked failed manually."
            ))
        self.env.cr.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            (LYNCEUS_BATCH_ADVISORY_LOCK_NAMESPACE, self.id),
        )
        suffix = _(
            "[ADMIN FORCE-RELEASE] Manually marked failed at %s. "
            "Auto-recovery did not complete within the stuck threshold; "
            "check cron + Postgres pooler health."
        ) % fields.Datetime.now()
        self.write({
            "state": "failed",
            "finished_at": fields.Datetime.now(),
            "error_log": (self.error_log or "") + "\n\n" + suffix,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Batch Released"),
                "message": _(
                    "Batch %s was marked failed. The %d generated prompts "
                    "remain in the pool."
                ) % (self.name, self.generated_count),
                "type": "success",
                "sticky": False,
            },
        }

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
                    "Batch %s is pending. It will start shortly. "
                    "You can leave this page \u2014 progress is saved as it runs."
                ) % self.name,
                "type": "success",
                "sticky": False,
            },
        }

    def action_run(self):
        from ..services import batch_orchestrator
        self.ensure_one()

        is_resume = self.state == "running"
        if not is_resume:
            if self.state not in ("draft", "failed", "pending"):
                return
            self.env.cr.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                (LYNCEUS_BATCH_ADVISORY_LOCK_NAMESPACE, self.id),
            )
            if not self.env.cr.fetchone()[0]:
                _logger.warning(
                    "Lynceus batch %s: advisory lock busy on fresh start, "
                    "skipping (another worker already owns this batch).",
                    self.name,
                )
                return
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
                "last_heartbeat_at": fields.Datetime.now(),
                "error_log": False,
            })
            if not getattr(threading.current_thread(), "testing", False):
                self.env.cr.commit()
        else:
            _logger.info(
                "Lynceus batch %s: resuming orphan from %d/%d (lock held by "
                "this connection from cron probe).",
                self.name, self.generated_count, self.target_n,
            )

        try:
            batch_orchestrator.run(self.env, self)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Lynceus batch %s failed: %s", self.name, exc)
            self.write({
                "state": "failed",
                "error_log": str(exc),
                "finished_at": fields.Datetime.now(),
            })
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (LYNCEUS_BATCH_ADVISORY_LOCK_NAMESPACE, self.id),
            )
            return

        self.invalidate_recordset(["generated_count", "api_calls"])
        final_state = "done" if self.generated_count > 0 else "failed"
        self.write({
            "state": final_state,
            "finished_at": fields.Datetime.now(),
        })
        self.env.cr.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            (LYNCEUS_BATCH_ADVISORY_LOCK_NAMESPACE, self.id),
        )

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
        if row:
            batch = self.browse(row[0])
            _logger.info("Lynceus cron picked pending batch %s (id=%s, N=%s)",
                         batch.name, batch.id, batch.target_n)
            batch.action_run()
            return

        # If pg_try_advisory_lock succeeds on a state='running' batch, the
        # connection that originally held the lock is gone (process killed,
        # OOM, restart): Postgres auto-released the lock when the TCP
        # connection dropped, so this batch is an orphan we can safely resume.
        self.env.cr.execute("""
            SELECT id FROM lynceus_batch
            WHERE state = 'running'
            ORDER BY create_date ASC, id ASC
        """)
        for (batch_id,) in self.env.cr.fetchall():
            self.env.cr.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                (LYNCEUS_BATCH_ADVISORY_LOCK_NAMESPACE, batch_id),
            )
            if not self.env.cr.fetchone()[0]:
                continue
            batch = self.browse(batch_id)
            _logger.warning(
                "Lynceus cron resuming orphaned batch %s (id=%s, %d/%d generated)",
                batch.name, batch.id, batch.generated_count, batch.target_n,
            )
            batch.action_run()
            return

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

        self.env.cr.execute("""
            SELECT COUNT(*) AS cnt, COUNT(DISTINCT user_id) AS users
            FROM lynceus_assignment_log
            WHERE reclaimed_at >= %s AND reclaimed_at <= %s
        """, (from_dt, to_dt))
        row = self.env.cr.fetchone() or (0, 0)
        reclaimed_in_range = row[0] or 0
        reclaim_affected_taskers = row[1] or 0

        self.env.cr.execute("""
            SELECT u.id, u.login AS label, COUNT(l.id) AS cnt
            FROM lynceus_assignment_log l
            JOIN res_users u ON u.id = l.user_id
            WHERE l.reclaimed_at >= %s AND l.reclaimed_at <= %s
            GROUP BY u.id, u.login
            ORDER BY cnt DESC
            LIMIT 10
        """, (from_dt, to_dt))
        reclaim_offenders = [
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
                "reclaimed_in_range": reclaimed_in_range,
                "reclaim_affected_taskers": reclaim_affected_taskers,
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
            "reclaim_offenders": reclaim_offenders,
            "filters_echo": {
                "date_from": date_from,
                "date_to": date_to,
            },
        }
