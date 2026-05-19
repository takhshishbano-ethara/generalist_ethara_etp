"""Shared fixtures for the Gohan test suite.

All Odoo-aware test cases inherit from :class:`GohanTestCase` to get a
consistent set of pre-created records (category, job, ICP setter) and helpers
for mocking external services (boto3, the background thread pool).
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "gohan")
class GohanTestCase(TransactionCase):
    """Base test case with shared fixtures for all Gohan tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.Job = cls.env["gohan.job"]
        cls.Category = cls.env["gohan.category"]
        cls.ICP = cls.env["ir.config_parameter"].sudo()

        cls.category = cls.Category.create({
            "name": "Test CRM",
            "code": "test_crm",
        })
        cls.category_ct = cls.Category.create({
            "name": "ERP",
            "code": "erp",
        })

        cls.tasker = cls.env.user
        cls.other_user = cls.env["res.users"].create({
            "name": "Other Tasker",
            "login": "gohan_other_tasker",
            "email": "gohan_other_tasker@example.com",
        })

        # Drain any cron- or migration-created records so search-based action
        # tests work against a known empty queue.
        cls.env["gohan.job"].search([]).unlink()

    # ── factories ───────────────────────────────────────────────────

    @classmethod
    def _create_job(cls, **overrides):
        vals = {
            "url": "https://example.com",
            "category_id": cls.category.id,
        }
        vals.update(overrides)
        return cls.Job.create(vals)

    @classmethod
    def _set_param(cls, key, value):
        cls.ICP.set_param(key, value)

    # ── mock helpers ────────────────────────────────────────────────

    @staticmethod
    @contextmanager
    def _patch_submit_bg():
        """Neutralise the background thread pool so postcommit callbacks are
        captured but never executed. Yields the mock so the test can assert
        on what was queued."""
        with patch(
            "odoo.addons.gohan.models.gohan_job._submit_bg",
        ) as mock_submit:
            mock_submit.return_value = MagicMock()
            yield mock_submit

    @staticmethod
    @contextmanager
    def _patch_postcommit():
        """Stub ``cr.postcommit.add`` so deferred callbacks are captured."""
        captured = []

        def _add(fn):
            captured.append(fn)

        with patch("odoo.sql_db.Cursor.postcommit", create=True) as _postcommit:
            _postcommit.add = _add
            yield captured

    @staticmethod
    def _png_bytes(width=10, height=10, color=(255, 0, 0)):
        """Build a minimal PNG byte string for image-resize tests."""
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        img = Image.new("RGB", (width, height), color)
        img.save(buf, format="PNG")
        return buf.getvalue()
