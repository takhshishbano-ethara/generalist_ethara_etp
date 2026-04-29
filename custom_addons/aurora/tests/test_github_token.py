# -*- coding: utf-8 -*-
import base64
import hashlib
import io
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from odoo import fields as odoo_fields
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged("post_install", "-at_install")
class TestAuroraGithubToken(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("aurora.output_dir", "/tmp/aurora_test")

    def _create_token(self, **kwargs):
        vals = {
            "name": "Test Token",
            "token": "ghp_faketoken123",
            "token_hash": hashlib.sha256(b"ghp_faketoken123").hexdigest(),
            "state": "active",
        }
        vals.update(kwargs)
        return self.env["aurora.github.token"].sudo().create(vals)

    # ═══════════════════════════════════════════════════════════════════════════
    # Constants
    # ═══════════════════════════════════════════════════════════════════════════

    def test_token_states_count(self):
        from ..models.github_token import TOKEN_STATES
        self.assertEqual(len(TOKEN_STATES), 6)

    def test_token_states_keys(self):
        from ..models.github_token import TOKEN_STATES
        keys = {k for k, _ in TOKEN_STATES}
        self.assertEqual(keys, {"draft", "active", "exhausted", "expired", "revoked", "quarantined"})

    def test_valid_token_prefixes(self):
        from ..models.github_token import _VALID_TOKEN_PREFIXES
        self.assertIn("ghp_", _VALID_TOKEN_PREFIXES)
        self.assertIn("gho_", _VALID_TOKEN_PREFIXES)
        self.assertIn("github_pat_", _VALID_TOKEN_PREFIXES)

    def test_valid_token_prefixes_count(self):
        from ..models.github_token import _VALID_TOKEN_PREFIXES
        self.assertEqual(len(_VALID_TOKEN_PREFIXES), 3)

    def test_allowed_update_columns_is_frozenset(self):
        from ..models.github_token import _ALLOWED_UPDATE_COLUMNS
        self.assertIsInstance(_ALLOWED_UPDATE_COLUMNS, frozenset)

    def test_allowed_update_columns_contains_state(self):
        from ..models.github_token import _ALLOWED_UPDATE_COLUMNS
        self.assertIn("state", _ALLOWED_UPDATE_COLUMNS)

    def test_allowed_update_columns_contains_rate_fields(self):
        from ..models.github_token import _ALLOWED_UPDATE_COLUMNS
        self.assertIn("rate_limit_remaining", _ALLOWED_UPDATE_COLUMNS)
        self.assertIn("rate_limit_reset", _ALLOWED_UPDATE_COLUMNS)

    def test_lease_batch_size(self):
        from ..models.github_token import _LEASE_BATCH_SIZE
        self.assertEqual(_LEASE_BATCH_SIZE, 3)

    def test_min_remaining_for_lease(self):
        from ..models.github_token import _MIN_REMAINING_FOR_LEASE
        self.assertEqual(_MIN_REMAINING_FOR_LEASE, 100)

    def test_quarantine_threshold(self):
        from ..models.github_token import _QUARANTINE_THRESHOLD
        self.assertEqual(_QUARANTINE_THRESHOLD, 6)

    def test_quarantine_expiry_hours(self):
        from ..models.github_token import _QUARANTINE_EXPIRY_HOURS
        self.assertEqual(_QUARANTINE_EXPIRY_HOURS, 24)

    def test_metrics_retention_days(self):
        from ..models.github_token import _METRICS_RETENTION_DAYS
        self.assertEqual(_METRICS_RETENTION_DAYS, 7)

    def test_health_check_workers(self):
        from ..models.github_token import _HEALTH_CHECK_WORKERS
        self.assertGreater(_HEALTH_CHECK_WORKERS, 0)

    # ═══════════════════════════════════════════════════════════════════════════
    # Token creation
    # ═══════════════════════════════════════════════════════════════════════════

    def test_create_token(self):
        tok = self._create_token(token_hash="unique_hash_1")
        self.assertEqual(tok.name, "Test Token")
        self.assertEqual(tok.state, "active")

    def test_create_default_state_draft(self):
        tok = self.env["aurora.github.token"].sudo().create({
            "name": "Draft Token",
            "token": "ghp_abc",
            "token_hash": "unique_hash_draft",
        })
        self.assertEqual(tok.state, "draft")

    def test_create_default_counters(self):
        tok = self._create_token(token_hash="unique_hash_2")
        self.assertEqual(tok.rate_limit_remaining, 0)
        self.assertEqual(tok.consecutive_failure_count, 0)

    def test_create_imported_at_set(self):
        tok = self._create_token(token_hash="unique_hash_3")
        self.assertTrue(tok.imported_at)

    def test_create_imported_by_set(self):
        tok = self._create_token(token_hash="unique_hash_4")
        self.assertTrue(tok.imported_by)

    # ═══════════════════════════════════════════════════════════════════════════
    # Encryption helpers
    # ═══════════════════════════════════════════════════════════════════════════

    def test_hash_token_deterministic(self):
        from ..models.github_token import AuroraGithubToken
        h1 = AuroraGithubToken._hash_token("ghp_test123")
        h2 = AuroraGithubToken._hash_token("ghp_test123")
        self.assertEqual(h1, h2)

    def test_hash_token_different_inputs(self):
        from ..models.github_token import AuroraGithubToken
        h1 = AuroraGithubToken._hash_token("ghp_aaa")
        h2 = AuroraGithubToken._hash_token("ghp_bbb")
        self.assertNotEqual(h1, h2)

    def test_hash_token_is_sha256(self):
        from ..models.github_token import AuroraGithubToken
        raw = "ghp_test123"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        self.assertEqual(AuroraGithubToken._hash_token(raw), expected)

    def test_hash_token_length(self):
        from ..models.github_token import AuroraGithubToken
        h = AuroraGithubToken._hash_token("ghp_x")
        self.assertEqual(len(h), 64)

    def test_encrypt_decrypt_round_trip(self):
        from cryptography.fernet import Fernet
        tok = self._create_token(token_hash="unique_hash_enc_1")
        key = Fernet.generate_key().decode()
        with patch.dict("os.environ", {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            encrypted = tok._encrypt_token("ghp_real_token")
            decrypted = tok._decrypt_token(encrypted)
        self.assertEqual(decrypted, "ghp_real_token")

    def test_decrypt_token_raw_empty(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        self.assertEqual(AuroraGithubToken._decrypt_token_raw(cr, ""), "")

    def test_decrypt_token_raw_no_prefix(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        self.assertEqual(AuroraGithubToken._decrypt_token_raw(cr, "ghp_plain"), "ghp_plain")

    def test_decrypt_token_raw_invalid(self):
        from ..models.github_token import AuroraGithubToken
        from ..models.credential_manager import _ENCRYPTED_PREFIX
        from cryptography.fernet import Fernet
        cr = MagicMock()
        key = Fernet.generate_key().decode()
        with patch.dict("os.environ", {"AURORA_ENCRYPTION_KEY": key}):
            cr.fetchone.return_value = None
            result = AuroraGithubToken._decrypt_token_raw(cr, _ENCRYPTED_PREFIX + "garbage")
        self.assertEqual(result, "")

    # ═══════════════════════════════════════════════════════════════════════════
    # Leasing
    # ═══════════════════════════════════════════════════════════════════════════

    def test_lease_tokens_returns_list(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        cr.fetchall.return_value = []
        result = AuroraGithubToken.lease_tokens(cr, 1, count=3)
        self.assertEqual(result, [])

    def test_lease_tokens_empty_pool(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        cr.fetchall.return_value = []
        result = AuroraGithubToken.lease_tokens(cr, 1)
        self.assertEqual(result, [])
        self.assertEqual(cr.execute.call_count, 1)

    def test_release_tokens_clears_lease(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        AuroraGithubToken.release_tokens(cr, 42)
        cr.execute.assert_called_once()
        sql = cr.execute.call_args[0][0]
        self.assertIn("leased_by_run_id = NULL", sql)

    def test_release_tokens_with_summaries(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        summaries = {"hash1": {"remaining": 4000, "reset": 1700000000}}
        AuroraGithubToken.release_tokens(cr, 42, summaries)
        self.assertTrue(cr.execute.call_count >= 2)

    def test_heartbeat_rate_limits_updates(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        summaries = {"hash_a": {"remaining": 3000, "reset": 1700000000}}
        AuroraGithubToken.heartbeat_rate_limits(cr, 1, summaries)
        cr.execute.assert_called()
        cr.commit.assert_called()

    def test_heartbeat_rate_limits_empty(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        AuroraGithubToken.heartbeat_rate_limits(cr, 1, {})
        cr.commit.assert_not_called()

    def test_heartbeat_rate_limits_no_release(self):
        from ..models.github_token import AuroraGithubToken
        cr = MagicMock()
        summaries = {"h": {"remaining": 100, "reset": None}}
        AuroraGithubToken.heartbeat_rate_limits(cr, 1, summaries)
        for call_args in cr.execute.call_args_list:
            sql = call_args[0][0]
            self.assertNotIn("leased_by_run_id = NULL", sql)

    # ═══════════════════════════════════════════════════════════════════════════
    # Cron: Health Check
    # ═══════════════════════════════════════════════════════════════════════════

    @patch("odoo.addons.aurora.models.github_token._health_check_lock")
    def test_health_check_skips_if_locked(self, mock_lock):
        mock_lock.acquire.return_value = False
        self.env["aurora.github.token"]._cron_token_health_check()
        mock_lock.release.assert_not_called()

    @patch("odoo.addons.aurora.models.github_token._health_check_lock")
    def test_health_check_releases_lock(self, mock_lock):
        mock_lock.acquire.return_value = True
        with patch.object(
            type(self.env["aurora.github.token"]), "_run_health_check", return_value=None
        ):
            self.env["aurora.github.token"]._cron_token_health_check()
        mock_lock.release.assert_called_once()

    # ═══════════════════════════════════════════════════════════════════════════
    # Cron: Expiry Alert
    # ═══════════════════════════════════════════════════════════════════════════

    def test_expiry_alert_no_expiring(self):
        self.env["aurora.github.token"]._cron_token_expiry_alert()

    def test_expiry_alert_with_expiring_tokens(self):
        tok = self._create_token(
            token_hash="unique_expiry_1",
            expires_at=odoo_fields.Date.today() + timedelta(days=3),
        )
        self.env["aurora.github.token"]._cron_token_expiry_alert()

    # ═══════════════════════════════════════════════════════════════════════════
    # Cron: Orphan Reclaim
    # ═══════════════════════════════════════════════════════════════════════════

    def test_orphan_reclaim_no_orphans(self):
        self.env["aurora.github.token"]._cron_token_orphan_reclaim()

    def test_orphan_reclaim_finished_pipeline(self):
        pipeline = self.env["aurora.pipeline"].create({
            "github_org": "org", "github_repo": "repo",
        })
        pipeline.write({"stage": "done"})
        tok = self._create_token(
            token_hash="unique_orphan_1",
            leased_by_run_id=pipeline.id,
        )
        self.env["aurora.github.token"]._cron_token_orphan_reclaim()
        tok.invalidate_recordset()
        self.assertFalse(tok.leased_by_run_id)

    # ═══════════════════════════════════════════════════════════════════════════
    # Cron: Pool Metrics
    # ═══════════════════════════════════════════════════════════════════════════

    def test_pool_metrics_creates_record(self):
        before = self.env["aurora.pool.metrics"].search_count([])
        self.env["aurora.github.token"]._cron_pool_metrics()
        after = self.env["aurora.pool.metrics"].search_count([])
        self.assertEqual(after, before + 1)

    def test_pool_metrics_counts(self):
        self._create_token(token_hash="metrics_1", state="active")
        self._create_token(token_hash="metrics_2", state="exhausted")
        self.env["aurora.github.token"]._cron_pool_metrics()
        latest = self.env["aurora.pool.metrics"].search([], limit=1, order="id desc")
        self.assertGreater(latest.total_tokens, 0)

    def test_pool_metrics_cleans_old(self):
        old = self.env["aurora.pool.metrics"].create({
            "timestamp": odoo_fields.Datetime.now() - timedelta(days=10),
            "total_tokens": 1,
        })
        self.env["aurora.github.token"]._cron_pool_metrics()
        self.assertFalse(old.exists())

    # ═══════════════════════════════════════════════════════════════════════════
    # Export
    # ═══════════════════════════════════════════════════════════════════════════

    def test_build_xlsx_returns_bytes(self):
        from ..models.github_token import AuroraGithubToken
        result = AuroraGithubToken._build_xlsx(["Col1", "Col2"], [("a", "b")])
        self.assertIsInstance(result, bytes)
        self.assertTrue(len(result) > 0)

    def test_build_xlsx_valid_zip(self):
        from ..models.github_token import AuroraGithubToken
        result = AuroraGithubToken._build_xlsx(["H1"], [("v1",)])
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
            self.assertIn("xl/worksheets/sheet1.xml", names)
            self.assertIn("xl/sharedStrings.xml", names)

    def test_build_xlsx_empty_rows(self):
        from ..models.github_token import AuroraGithubToken
        result = AuroraGithubToken._build_xlsx(["H1"], [])
        self.assertIsInstance(result, bytes)

    def test_build_xlsx_many_rows(self):
        from ..models.github_token import AuroraGithubToken
        rows = [(f"val_{i}",) for i in range(100)]
        result = AuroraGithubToken._build_xlsx(["Col"], rows)
        self.assertTrue(len(result) > 0)

    def test_build_xlsx_special_chars(self):
        from ..models.github_token import AuroraGithubToken
        result = AuroraGithubToken._build_xlsx(["H"], [("a&b<c>d",)])
        self.assertIsInstance(result, bytes)

    def test_export_tokens_creates_attachment(self):
        self._create_token(token_hash="export_1")
        result = self.env["aurora.github.token"].sudo().action_export_tokens()
        self.assertEqual(result["type"], "ir.actions.act_url")
        self.assertIn("download=true", result["url"])

    # ═══════════════════════════════════════════════════════════════════════════
    # SQL constraint
    # ═══════════════════════════════════════════════════════════════════════════

    def test_duplicate_hash_rejected(self):
        self._create_token(token_hash="dup_hash_test")
        with self.assertRaises(Exception):
            self._create_token(token_hash="dup_hash_test")

    # ═══════════════════════════════════════════════════════════════════════════
    # State transitions
    # ═══════════════════════════════════════════════════════════════════════════

    def test_write_all_states(self):
        from ..models.github_token import TOKEN_STATES
        tok = self._create_token(token_hash="state_test_1")
        for state_key, _ in TOKEN_STATES:
            tok.write({"state": state_key})
            self.assertEqual(tok.state, state_key)

    def test_token_fields_exist(self):
        tok = self._create_token(token_hash="field_test_1")
        self.assertTrue(hasattr(tok, "name"))
        self.assertTrue(hasattr(tok, "token"))
        self.assertTrue(hasattr(tok, "token_hash"))
        self.assertTrue(hasattr(tok, "state"))
        self.assertTrue(hasattr(tok, "rate_limit_remaining"))
        self.assertTrue(hasattr(tok, "rate_limit_reset"))
        self.assertTrue(hasattr(tok, "expires_at"))
        self.assertTrue(hasattr(tok, "leased_by_run_id"))
        self.assertTrue(hasattr(tok, "leased_at"))
        self.assertTrue(hasattr(tok, "last_health_check"))
        self.assertTrue(hasattr(tok, "last_heartbeat"))
        self.assertTrue(hasattr(tok, "consecutive_failure_count"))
        self.assertTrue(hasattr(tok, "error_message"))
