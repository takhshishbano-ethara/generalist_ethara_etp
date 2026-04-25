import base64
import hashlib
import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from odoo import api, fields, models

from .credential_manager import (
    encrypt_value,
    decrypt_value,
    _get_or_create_key,
    _get_or_create_key_raw,
    _make_fernet_raw,
    _ENCRYPTED_PREFIX,
)
from cryptography.fernet import InvalidToken

_logger = logging.getLogger(__name__)

TOKEN_STATES = [
    ("draft", "Draft"),
    ("active", "Active"),
    ("exhausted", "Exhausted"),
    ("expired", "Expired"),
    ("revoked", "Revoked"),
    ("quarantined", "Quarantined"),
]

_VALID_TOKEN_PREFIXES = ("ghp_", "gho_", "github_pat_")

_LEASE_BATCH_SIZE = 3
_HEALTH_CHECK_WORKERS = 10
_HEALTH_CHECK_RATE = 15
_MIN_REMAINING_FOR_LEASE = 100
_QUARANTINE_THRESHOLD = 6
_QUARANTINE_EXPIRY_HOURS = 24
_METRICS_RETENTION_DAYS = 7
_IMPORT_BATCH_SIZE = 500

_health_check_lock = threading.Lock()
_HEALTH_CHECK_ADVISORY_LOCK_ID = 73927461

_ALLOWED_UPDATE_COLUMNS = frozenset({
    "state", "rate_limit_remaining", "rate_limit_reset",
    "last_health_check", "last_heartbeat", "consecutive_failure_count",
    "error_message", "leased_by_run_id", "leased_at",
})


class AuroraGithubToken(models.Model):
    _name = "aurora.github.token"
    _description = "GitHub Token"
    _order = "name"

    name = fields.Char(string="Name", required=True, index=True)
    token = fields.Char(string="Token (encrypted)")
    token_hash = fields.Char(string="Token Hash", index=True, readonly=True)
    state = fields.Selection(TOKEN_STATES, default="draft", required=True, index=True)
    rate_limit_remaining = fields.Integer(default=0)
    rate_limit_reset = fields.Datetime()
    expires_at = fields.Date()
    leased_by_run_id = fields.Many2one(
        "aurora.pipeline", string="Leased By", index=True, ondelete="set null",
    )
    leased_at = fields.Datetime()
    last_health_check = fields.Datetime()
    last_heartbeat = fields.Datetime()
    consecutive_failure_count = fields.Integer(default=0)
    imported_at = fields.Datetime(default=fields.Datetime.now)
    imported_by = fields.Many2one("res.users", default=lambda self: self.env.uid)
    error_message = fields.Text()

    _token_hash_unique = models.Constraint(
        'UNIQUE(token_hash)',
        'Duplicate token detected.',
    )

    def init(self):
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS idx_aurora_token_available
            ON aurora_github_token (id)
            WHERE state = 'active' AND leased_by_run_id IS NULL
        """)
        self.env.cr.execute("""
            ALTER TABLE aurora_github_token SET (
                autovacuum_vacuum_scale_factor = 0.01,
                autovacuum_vacuum_cost_limit = 2000,
                autovacuum_vacuum_cost_delay = 2,
                fillfactor = 70
            )
        """)

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    def _encrypt_token(self, raw_token):
        ICP = self.env["ir.config_parameter"].sudo()
        return encrypt_value(ICP, raw_token)

    def _decrypt_token(self, stored_value):
        ICP = self.env["ir.config_parameter"].sudo()
        return decrypt_value(ICP, stored_value)

    @staticmethod
    def _decrypt_token_raw(cr, stored_value):
        if not stored_value:
            return ""
        if not stored_value.startswith(_ENCRYPTED_PREFIX):
            return stored_value
        cipher = stored_value[len(_ENCRYPTED_PREFIX):]
        f = _make_fernet_raw(cr)
        try:
            return f.decrypt(cipher.encode()).decode()
        except InvalidToken:
            return ""

    @staticmethod
    def _hash_token(raw_token):
        return hashlib.sha256(raw_token.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Leasing
    # ------------------------------------------------------------------

    @staticmethod
    def lease_tokens(cr, run_id, count=_LEASE_BATCH_SIZE):
        cr.execute("""
            UPDATE aurora_github_token
            SET leased_by_run_id = %s, leased_at = NOW() AT TIME ZONE 'UTC'
            WHERE id IN (
                SELECT id FROM aurora_github_token
                WHERE state = 'active'
                  AND rate_limit_remaining > %s
                  AND (expires_at IS NULL OR expires_at > (NOW() AT TIME ZONE 'UTC')::date + INTERVAL '1 hour')
                  AND leased_by_run_id IS NULL
                  AND (
                      last_health_check IS NULL
                      OR last_health_check > (NOW() AT TIME ZONE 'UTC') - INTERVAL '30 minutes'
                      OR last_heartbeat > (NOW() AT TIME ZONE 'UTC') - INTERVAL '30 minutes'
                  )
                LIMIT %s
                FOR NO KEY UPDATE SKIP LOCKED
            )
            RETURNING id, token
        """, (run_id, _MIN_REMAINING_FOR_LEASE, count))
        rows = cr.fetchall()
        if not rows:
            return []
        tokens = []
        for _, encrypted in rows:
            raw = AuroraGithubToken._decrypt_token_raw(cr, encrypted)
            if raw:
                tokens.append(raw)
        return tokens

    @staticmethod
    def release_tokens(cr, run_id, token_summaries=None):
        if token_summaries:
            AuroraGithubToken._write_rate_limits(cr, run_id, token_summaries)
        cr.execute("""
            UPDATE aurora_github_token
            SET leased_by_run_id = NULL, leased_at = NULL,
                last_health_check = NOW() AT TIME ZONE 'UTC'
            WHERE leased_by_run_id = %s
        """, (run_id,))

    @staticmethod
    def heartbeat_rate_limits(cr, run_id, token_summaries):
        if not token_summaries:
            return
        AuroraGithubToken._write_rate_limits(cr, run_id, token_summaries)
        cr.commit()

    @staticmethod
    def _write_rate_limits(cr, run_id, token_summaries):
        """Update rate-limit counters for leased tokens.

        NOTE: This method intentionally does NOT release the lease.
        Token release is handled exclusively by ``release_tokens()``.
        Previously, this method contained a blanket UPDATE that set
        ``leased_by_run_id = NULL`` for all tokens belonging to the run,
        which caused tokens to be released mid-pipeline when called from
        ``heartbeat_rate_limits()``.
        """
        for tok_hash, info in token_summaries.items():
            remaining = info.get("remaining", 0)
            reset_ts = info.get("reset")
            reset_dt = (
                datetime.fromtimestamp(reset_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                if reset_ts else None
            )
            cr.execute("""
                UPDATE aurora_github_token
                SET rate_limit_remaining = %s,
                    rate_limit_reset = %s,
                    last_heartbeat = NOW() AT TIME ZONE 'UTC'
                WHERE token_hash = %s AND leased_by_run_id = %s
            """, (remaining, reset_dt, tok_hash, run_id))

    # ------------------------------------------------------------------
    # Cron 1: Health Check
    # ------------------------------------------------------------------

    @api.model
    def _cron_token_health_check(self):
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (_HEALTH_CHECK_ADVISORY_LOCK_ID,)
        )
        if not self.env.cr.fetchone()[0]:
            _logger.warning("Previous health check still running (advisory lock held), skipping")
            return
        try:
            self._run_health_check()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (_HEALTH_CHECK_ADVISORY_LOCK_ID,)
            )

    def _run_health_check(self):
        cr = self.env.cr
        now_utc = fields.Datetime.now()

        cr.execute("""
            SELECT id, token, state, rate_limit_reset, consecutive_failure_count
            FROM aurora_github_token
            WHERE state IN ('active', 'exhausted', 'draft', 'quarantined')
              AND leased_by_run_id IS NULL
              AND (
                  (state = 'quarantined' AND last_health_check < (NOW() AT TIME ZONE 'UTC') - INTERVAL '1 hour')
                  OR (state != 'quarantined' AND last_health_check < (NOW() AT TIME ZONE 'UTC') - INTERVAL '30 minutes')
                  OR last_health_check IS NULL
              )
            ORDER BY
              CASE WHEN state = 'draft' THEN 0 ELSE 1 END,
              last_health_check NULLS FIRST
        """)
        rows = cr.fetchall()
        if not rows:
            return

        to_check = []
        for row_id, encrypted, state, reset_dt, fail_count in rows:
            if state == "exhausted" and reset_dt and reset_dt > now_utc:
                continue
            raw = self._decrypt_token(encrypted)
            if not raw:
                continue
            to_check.append((row_id, raw, state, fail_count))

        if not to_check:
            return

        _logger.info("Health check: %d tokens to verify", len(to_check))

        results = []
        rate_limiter = threading.Semaphore(_HEALTH_CHECK_RATE)

        def _check_one(token_id, raw_token, current_state, current_fails):
            rate_limiter.acquire()
            try:
                time.sleep(1.0 / _HEALTH_CHECK_RATE)
                resp = requests.get(
                    "https://api.github.com/rate_limit",
                    headers={"Authorization": f"Bearer {raw_token}"},
                    timeout=10,
                )
                return (token_id, resp.status_code, resp.json() if resp.status_code == 200 else {},
                        resp.headers, current_state, current_fails)
            except Exception as exc:
                _logger.debug("Health check probe failed for token id=%s: %s", token_id, exc)
                return (token_id, 0, {}, {}, current_state, current_fails)
            finally:
                rate_limiter.release()

        with ThreadPoolExecutor(max_workers=_HEALTH_CHECK_WORKERS) as pool:
            futures = [pool.submit(_check_one, *item) for item in to_check]
            for fut in futures:
                results.append(fut.result())

        batch_count = 0
        for token_id, status, body, headers, prev_state, prev_fails in results:
            savepoint_name = f"hc_token_{token_id}"
            try:
                cr.execute(f"SAVEPOINT {savepoint_name}")
                vals = {"last_health_check": now_utc}

                if status == 200:
                    core = body.get("resources", {}).get("core", body.get("rate", {}))
                    vals["rate_limit_remaining"] = core.get("remaining", 0)
                    reset_unix = core.get("reset")
                    if reset_unix:
                        vals["rate_limit_reset"] = datetime.fromtimestamp(reset_unix, tz=timezone.utc)
                    vals["consecutive_failure_count"] = 0
                    vals["error_message"] = False
                    if prev_state in ("exhausted", "draft", "quarantined") and core.get("remaining", 0) > _MIN_REMAINING_FOR_LEASE:
                        vals["state"] = "active"

                elif status == 401:
                    vals["state"] = "expired"
                    vals["error_message"] = "401 Unauthorized — token invalid or revoked"
                    vals["consecutive_failure_count"] = prev_fails + 1

                elif status == 403:
                    vals["state"] = "exhausted"
                    vals["rate_limit_remaining"] = 0
                    vals["error_message"] = "403 rate limit exceeded"
                    vals["consecutive_failure_count"] = prev_fails + 1

                elif status == 429:
                    vals["state"] = "exhausted"
                    vals["rate_limit_remaining"] = 0
                    retry_after = headers.get("Retry-After", "3600")
                    try:
                        reset_time = datetime.now(tz=timezone.utc) + timedelta(seconds=int(retry_after))
                        vals["rate_limit_reset"] = reset_time
                    except (ValueError, TypeError):
                        pass
                    vals["consecutive_failure_count"] = prev_fails + 1

                else:
                    vals["consecutive_failure_count"] = prev_fails + 1

                new_fails = vals.get("consecutive_failure_count", prev_fails)
                if new_fails >= _QUARANTINE_THRESHOLD and vals.get("state") != "expired":
                    vals["state"] = "quarantined"

                if prev_state == "quarantined" and new_fails >= _QUARANTINE_THRESHOLD:
                    cr.execute(
                        "SELECT last_health_check FROM aurora_github_token WHERE id = %s",
                        (token_id,),
                    )
                    lhc_row = cr.fetchone()
                    if lhc_row and lhc_row[0]:
                        quarantine_start = lhc_row[0]
                        if isinstance(quarantine_start, str):
                            quarantine_start = datetime.fromisoformat(quarantine_start)
                        if quarantine_start.tzinfo is None:
                            quarantine_start = quarantine_start.replace(tzinfo=timezone.utc)
                        if (datetime.now(tz=timezone.utc) - quarantine_start).total_seconds() > _QUARANTINE_EXPIRY_HOURS * 3600:
                            vals["state"] = "expired"
                            vals["error_message"] = "Quarantined for 24h with no recovery — marked expired"

                sorted_update_keys = sorted(k for k in vals if k in _ALLOWED_UPDATE_COLUMNS)
                sets = ", ".join(f"{k} = %s" for k in sorted_update_keys)
                params = [vals[k] for k in sorted_update_keys]
                if sets:
                    params.append(token_id)
                    cr.execute(f"UPDATE aurora_github_token SET {sets} WHERE id = %s", params)

                cr.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                batch_count += 1
                if batch_count % 200 == 0:
                    cr.commit()
            except Exception:
                _logger.exception("Health check: failed to update token id=%s, skipping", token_id)
                try:
                    cr.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                except Exception:
                    pass

        cr.commit()
        _logger.info("Health check complete: %d tokens processed", len(results))

    # ------------------------------------------------------------------
    # Cron 2: Expiry Alert
    # ------------------------------------------------------------------

    @api.model
    def _cron_token_expiry_alert(self):
        cr = self.env.cr
        cr.execute("""
            SELECT
                (expires_at - CURRENT_DATE) AS days_left,
                COUNT(*) AS cnt
            FROM aurora_github_token
            WHERE expires_at IS NOT NULL
              AND expires_at <= CURRENT_DATE + 7
              AND expires_at > CURRENT_DATE
              AND state NOT IN ('expired', 'revoked')
            GROUP BY days_left
            ORDER BY days_left
        """)
        rows = cr.fetchall()
        if not rows:
            return

        total_expiring = sum(r[1] for r in rows)

        cr.execute("""
            SELECT state, COUNT(*) FROM aurora_github_token GROUP BY state
        """)
        pool_status = dict(cr.fetchall())

        cr.execute("""
            SELECT COUNT(*) FROM aurora_github_token WHERE expires_at IS NULL AND state != 'expired'
        """)
        unknown_count = cr.fetchone()[0]

        lines = [f"AURORA TOKEN EXPIRY WARNING\n\n{total_expiring} tokens expiring within 7 days\n"]
        for days_left, cnt in rows:
            lines.append(f"  {cnt} tokens expire in {days_left} day{'s' if days_left != 1 else ''}")

        status_parts = []
        for state_key in ("active", "exhausted", "expired", "quarantined", "revoked", "draft"):
            if pool_status.get(state_key, 0):
                status_parts.append(f"{pool_status[state_key]} {state_key}")
        if unknown_count:
            status_parts.append(f"{unknown_count} unknown expiry")
        lines.append(f"\nPool status: {', '.join(status_parts)}")
        lines.append("\nAction required: Import replacement tokens before expiry")

        body = "\n".join(lines)

        admin_group = self.env.ref("aurora.group_aurora_admin", raise_if_not_found=False)
        if not admin_group:
            _logger.warning("Aurora Admin group not found, cannot send expiry alert")
            return
        partner_ids = admin_group.users.mapped("partner_id").ids
        if partner_ids:
            self.env["mail.thread"].message_notify(
                body=body,
                partner_ids=partner_ids,
                subject="Aurora Token Expiry Warning",
            )

    # ------------------------------------------------------------------
    # Cron 3: Orphan Reclaim
    # ------------------------------------------------------------------

    @api.model
    def _cron_token_orphan_reclaim(self):
        cr = self.env.cr

        cr.execute("""
            UPDATE aurora_github_token t
            SET leased_by_run_id = NULL, leased_at = NULL,
                last_health_check = NOW() AT TIME ZONE 'UTC'
            FROM aurora_pipeline p
            WHERE t.leased_by_run_id = p.id
              AND (p.stage IN ('done', 'failed')
                   OR t.last_heartbeat < (NOW() AT TIME ZONE 'UTC') - INTERVAL '60 minutes')
            RETURNING t.id, t.name, p.id AS run_id, p.stage,
                      (t.last_heartbeat < (NOW() AT TIME ZONE 'UTC') - INTERVAL '60 minutes') AS force_released
        """)
        reclaimed = cr.fetchall()

        cr.execute("""
            UPDATE aurora_github_token
            SET leased_by_run_id = NULL, leased_at = NULL,
                last_health_check = NOW() AT TIME ZONE 'UTC'
            WHERE leased_by_run_id IS NOT NULL
              AND leased_by_run_id NOT IN (SELECT id FROM aurora_pipeline)
            RETURNING id
        """)
        orphaned = cr.fetchall()

        cr.commit()

        normal = sum(1 for r in reclaimed if not r[4])
        forced = sum(1 for r in reclaimed if r[4])
        if reclaimed or orphaned:
            _logger.info(
                "Orphan reclaim: %d normal, %d forced, %d deleted-pipeline",
                normal, forced, len(orphaned),
            )

    # ------------------------------------------------------------------
    # Cron 4: Pool Metrics
    # ------------------------------------------------------------------

    @api.model
    def _cron_pool_metrics(self):
        cr = self.env.cr
        cr.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE state = 'active') AS active,
                COUNT(*) FILTER (WHERE state = 'exhausted') AS exhausted,
                COUNT(*) FILTER (WHERE state = 'expired') AS expired_cnt,
                COUNT(*) FILTER (WHERE state = 'quarantined') AS quarantined,
                COUNT(*) FILTER (WHERE leased_by_run_id IS NOT NULL) AS leased,
                COALESCE(SUM(rate_limit_remaining) FILTER (WHERE state = 'active'), 0) AS total_rem,
                COALESCE(AVG(rate_limit_remaining) FILTER (WHERE state = 'active'), 0) AS avg_rem
            FROM aurora_github_token
        """)
        row = cr.fetchone()
        total, active, exhausted, expired_cnt, quarantined, leased, total_rem, avg_rem = row
        utilization = (leased / active * 100) if active > 0 else 0

        self.env["aurora.pool.metrics"].create({
            "total_tokens": total,
            "active_count": active,
            "exhausted_count": exhausted,
            "expired_count": expired_cnt,
            "quarantined_count": quarantined,
            "leased_count": leased,
            "total_remaining": total_rem,
            "avg_remaining": round(avg_rem, 1),
            "pool_utilization": round(utilization, 2),
        })

        if active < 5:
            _logger.error("CRITICAL: Only %d active tokens in pool", active)
        elif active < 20:
            _logger.warning("Low token pool: only %d active tokens", active)
        if utilization > 95:
            _logger.error("CRITICAL: Pool utilization at %.1f%%", utilization)
        elif utilization > 80:
            _logger.warning("WARNING: Pool utilization at %.1f%%", utilization)

        cutoff = fields.Datetime.now() - timedelta(days=_METRICS_RETENTION_DAYS)
        self.env["aurora.pool.metrics"].search([("timestamp", "<", cutoff)]).unlink()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def action_export_tokens(self):
        cr = self.env.cr
        cr.execute("""
            SELECT
                t.name,
                t.state,
                t.rate_limit_remaining,
                t.rate_limit_reset,
                t.expires_at,
                p.name AS leased_by,
                t.last_health_check,
                t.consecutive_failure_count,
                t.imported_at,
                u.login AS imported_by_login,
                t.error_message
            FROM aurora_github_token t
            LEFT JOIN aurora_pipeline p ON p.id = t.leased_by_run_id
            LEFT JOIN res_users u ON u.id = t.imported_by
            ORDER BY t.name
        """)
        rows = cr.fetchall()
        headers = [
            "Name", "State", "Rate Limit Remaining", "Rate Limit Resets At",
            "Expires At", "Leased By", "Last Health Check", "Consecutive Failures",
            "Imported At", "Imported By", "Error Message",
        ]

        xlsx_bytes = self._build_xlsx(headers, rows)
        today = fields.Date.today().isoformat()
        filename = f"aurora_token_pool_{today}.xlsx"

        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(xlsx_bytes),
            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "new",
        }

    @staticmethod
    def _build_xlsx(headers, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Tokens"
        ws.append(headers)
        for row in rows:
            ws.append([str(v) if v is not None else "" for v in row])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
