"""Shared fixtures for the Vegeta test suite.

All Odoo-aware test cases inherit from :class:`VegetaTestCase` to get a
consistent set of pre-created records (category, job, ICP setter) and helpers
for mocking external services (boto3, the background thread pool).
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "vegeta")
class VegetaTestCase(TransactionCase):
    """Base test case with shared fixtures for all Vegeta tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.Job = cls.env["vegeta.job"]
        cls.Category = cls.env["vegeta.category"]
        cls.ICP = cls.env["ir.config_parameter"].sudo()

        cls.category = cls.Category.create({
            "name": "Test Normal Website",
            "technical_key": "test_normal_website",
        })
        cls.category_ct = cls.Category.create({
            "name": "Cool Transition",
            "technical_key": "cool_transition",
        })

        cls.tasker = cls.env.user
        cls.other_user = cls.env["res.users"].create({
            "name": "Other Tasker",
            "login": "vegeta_other_tasker",
            "email": "vegeta_other_tasker@example.com",
        })

        # Drain any cron- or migration-created records so search-based action
        # tests work against a known empty queue.
        cls.env["vegeta.job"].search([]).unlink()

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
            "odoo.addons.vegeta.models.vegeta_job._submit_bg",
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

    @contextmanager
    def _patch_registry_cursor(self):
        """Route ``Registry(db).cursor()`` to the live test cursor.

        Background PRD/QC methods open their own ``Registry(db).cursor()``
        connections, which cannot see records created inside a rolled-back
        TransactionCase. This redirects them to the test cursor; ``commit``
        is downgraded to a flush and ``rollback`` to a no-op, both of which
        the test framework otherwise forbids inside a test.
        """
        test_cr = self.env.cr

        @contextmanager
        def _cursor():
            yield test_cr

        registry = MagicMock()
        registry.cursor = _cursor

        with patch(
            "odoo.addons.vegeta.models.vegeta_job.Registry",
            return_value=registry,
        ), patch.object(test_cr, "commit", self.env.flush_all), \
                patch.object(test_cr, "rollback", lambda: None):
            yield

    @staticmethod
    def _png_bytes(width=10, height=10, color=(255, 0, 0)):
        """Build a minimal PNG byte string for image-resize tests."""
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        img = Image.new("RGB", (width, height), color)
        img.save(buf, format="PNG")
        return buf.getvalue()
