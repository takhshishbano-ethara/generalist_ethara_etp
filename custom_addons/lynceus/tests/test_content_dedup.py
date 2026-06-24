from psycopg2.errors import UniqueViolation

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..services import deduplicator


@tagged("post_install", "-at_install", "lynceus")
class TestContentDedup(TransactionCase):

    def test_content_hash_deterministic(self):
        a = deduplicator.content_hash("Phone photo of a desk.")
        b = deduplicator.content_hash("  phone   photo   of a desk  ")
        self.assertEqual(a, b)

    def test_history_blocks_duplicate_content(self):
        Prompt = self.env["lynceus.prompt"]
        Prompt.create({"content": "A unique prompt about a vet table."})
        self.assertTrue(deduplicator.is_duplicate(self.env, "A unique prompt about a vet table."))
        self.assertTrue(deduplicator.is_duplicate(self.env, "a unique prompt about a VET table"))

    def test_sql_unique_constraint(self):
        Prompt = self.env["lynceus.prompt"]
        Prompt.create({"content": "Unique once."})
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.cr.savepoint():
            Prompt.create({"content": "Unique once."})

    def test_rejection_recorded_in_history(self):
        deduplicator.record_rejection(self.env, "rejected blob 1")
        history = self.env["lynceus.history"].search([
            ("content_hash", "=", deduplicator.content_hash("rejected blob 1")),
        ])
        self.assertTrue(history)
        self.assertTrue(history.rejected_as_duplicate)
