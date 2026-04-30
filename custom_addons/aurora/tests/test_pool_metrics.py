# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields as odoo_fields
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAuroraPoolMetrics(TransactionCase):

    def _create_metrics(self, **kwargs):
        vals = {
            "total_tokens": 100,
            "active_count": 80,
            "exhausted_count": 10,
            "expired_count": 5,
            "quarantined_count": 3,
            "leased_count": 2,
            "total_remaining": 40000,
            "avg_remaining": 500.0,
            "pool_utilization": 2.5,
        }
        vals.update(kwargs)
        return self.env["aurora.pool.metrics"].create(vals)

    def test_create_basic(self):
        rec = self._create_metrics()
        self.assertTrue(rec.id)

    def test_create_default_timestamp(self):
        rec = self._create_metrics()
        self.assertTrue(rec.timestamp)

    def test_create_all_fields(self):
        rec = self._create_metrics()
        self.assertEqual(rec.total_tokens, 100)
        self.assertEqual(rec.active_count, 80)
        self.assertEqual(rec.exhausted_count, 10)
        self.assertEqual(rec.expired_count, 5)
        self.assertEqual(rec.quarantined_count, 3)
        self.assertEqual(rec.leased_count, 2)
        self.assertEqual(rec.total_remaining, 40000)
        self.assertAlmostEqual(rec.avg_remaining, 500.0, places=1)
        self.assertAlmostEqual(rec.pool_utilization, 2.5, places=2)

    def test_create_zero_values(self):
        rec = self._create_metrics(
            total_tokens=0, active_count=0, exhausted_count=0,
            expired_count=0, quarantined_count=0, leased_count=0,
            total_remaining=0, avg_remaining=0.0, pool_utilization=0.0,
        )
        self.assertEqual(rec.total_tokens, 0)
        self.assertEqual(rec.active_count, 0)

    def test_ordering_desc(self):
        rec1 = self._create_metrics()
        rec2 = self._create_metrics()
        results = self.env["aurora.pool.metrics"].search([
            ("id", "in", [rec1.id, rec2.id])
        ])
        self.assertEqual(results[0].id, rec2.id)

    def test_write_field(self):
        rec = self._create_metrics()
        rec.write({"total_tokens": 200})
        self.assertEqual(rec.total_tokens, 200)

    def test_unlink(self):
        rec = self._create_metrics()
        rec_id = rec.id
        rec.unlink()
        self.assertFalse(self.env["aurora.pool.metrics"].browse(rec_id).exists())

    def test_batch_create(self):
        recs = self.env["aurora.pool.metrics"].create([
            {"total_tokens": 1},
            {"total_tokens": 2},
            {"total_tokens": 3},
        ])
        self.assertEqual(len(recs), 3)

    def test_search_filter(self):
        self._create_metrics(active_count=50)
        self._create_metrics(active_count=150)
        results = self.env["aurora.pool.metrics"].search([("active_count", ">", 100)])
        self.assertTrue(all(r.active_count > 100 for r in results))

    def test_float_precision_avg(self):
        rec = self._create_metrics(avg_remaining=123.4)
        self.assertAlmostEqual(rec.avg_remaining, 123.4, places=1)

    def test_float_precision_utilization(self):
        rec = self._create_metrics(pool_utilization=99.99)
        self.assertAlmostEqual(rec.pool_utilization, 99.99, places=2)

    def test_model_name(self):
        self.assertEqual(
            self.env["aurora.pool.metrics"]._name,
            "aurora.pool.metrics"
        )

    def test_model_order(self):
        self.assertEqual(
            self.env["aurora.pool.metrics"]._order,
            "timestamp desc"
        )

    def test_large_values(self):
        rec = self._create_metrics(total_remaining=999999999)
        self.assertEqual(rec.total_remaining, 999999999)

    def test_multiple_records_ordered(self):
        import time
        r1 = self._create_metrics(total_tokens=1)
        time.sleep(0.01)
        r2 = self._create_metrics(total_tokens=2)
        time.sleep(0.01)
        r3 = self._create_metrics(total_tokens=3)
        all_recs = self.env["aurora.pool.metrics"].search([
            ("id", "in", [r1.id, r2.id, r3.id])
        ])
        self.assertEqual(all_recs[0].id, r3.id)
