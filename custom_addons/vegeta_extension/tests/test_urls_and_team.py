"""Shape + logic tests for the URLs Added endpoint builders and the Team-tab
per-member stats hook.

These lock the contracts the Flutter project-detail screen relies on:
  * URLs Added — _serialize_url emits the six pen columns' keys, and
    via_batch maps to the "Source" label (Bulk CSV / Single); _build_urls_domain
    restricts to jobs that have a URL and honours the source filter.
  * Team — vegeta.job.get_member_task_stats(user_id) returns a per-user
    {done, avg_seconds}, which the shared /v2/project_team_member_list endpoint
    surfaces as total_done_task / avg_time.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.vegeta_extension.controllers.urls_added import (
    SOURCE_BULK,
    SOURCE_SINGLE,
    URL_COLUMNS,
    _build_urls_domain,
    _serialize_url,
)

URL_ROW_KEYS = {
    "id",
    "seq",
    "url",
    "category",
    "added_by",
    "tasker_name",
    "source",
    "created_date",
}


@tagged("post_install", "-at_install", "vegeta_extension")
class TestVegetaUrlsAndTeam(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["vegeta.job"]
        cls.user = cls.env["res.users"].create({
            "name": "Vegeta Urls Tester",
            "login": "vegeta_urls_tester",
            "email": "vegeta_urls_tester@example.com",
        })

    def _make_job(self, **vals):
        defaults = {"url": "https://example.com", "user_id": self.user.id}
        defaults.update(vals)
        return self.Job.create(defaults)

    def _set_state(self, job, state):
        """Set state via SQL to bypass the action-driven pipeline."""
        self.env.cr.execute(
            "UPDATE vegeta_job SET state=%s WHERE id=%s", [state, job.id]
        )
        job.invalidate_recordset()

    # ── URLs Added contract ──

    def test_url_columns_match_pen(self):
        keys = [c["key"] for c in URL_COLUMNS]
        self.assertEqual(
            keys,
            ["url", "category", "added_by", "tasker_name", "source", "created_date"],
        )
        labels = [c["label"] for c in URL_COLUMNS]
        self.assertEqual(
            labels,
            ["Website URL", "Category", "Added by", "Assigned Tasker",
             "Source", "Date added"],
        )

    def test_serialize_url_shape(self):
        cat = self.env["vegeta.category"].create({
            "name": "Normal Website", "technical_key": "normal_website",
        })
        job = self._make_job(category_id=cat.id, via_batch=True)
        row = _serialize_url(job)
        self.assertEqual(set(row), URL_ROW_KEYS)
        self.assertEqual(row["url"], "https://example.com")
        self.assertEqual(row["category"], "Normal Website")
        self.assertEqual(row["tasker_name"], self.user.name)
        # via_batch True ⇒ Bulk CSV.
        self.assertEqual(row["source"], SOURCE_BULK)

    def test_serialize_url_source_single(self):
        job = self._make_job(via_batch=False)
        self.assertEqual(_serialize_url(job)["source"], SOURCE_SINGLE)

    def test_build_urls_domain_requires_url(self):
        domain, error = _build_urls_domain(self.env, {})
        self.assertIsNone(error)
        self.assertIn(("url", "!=", False), domain)

    def test_build_urls_domain_source_filter(self):
        bulk, _ = _build_urls_domain(self.env, {"source": "bulk"})
        self.assertIn(("via_batch", "=", True), bulk)
        single, _ = _build_urls_domain(self.env, {"source": "single"})
        self.assertIn(("via_batch", "=", False), single)

    def test_build_urls_domain_search(self):
        domain, _ = _build_urls_domain(self.env, {"search": "stripe"})
        self.assertIn(("url", "ilike", "stripe"), domain)

    # ── Team per-member stats hook ──

    def test_get_member_task_stats_counts_done(self):
        self._set_state(self._make_job(), "done")
        self._set_state(self._make_job(), "submitted")
        self._set_state(self._make_job(), "draft")  # not counted
        stats = self.env["vegeta.job"].get_member_task_stats(self.user.id)
        self.assertEqual(stats["done"], 2)
        self.assertIn("avg_seconds", stats)

    def test_get_member_task_stats_no_user(self):
        stats = self.env["vegeta.job"].get_member_task_stats(False)
        self.assertEqual(stats, {"done": 0, "avg_seconds": 0})
