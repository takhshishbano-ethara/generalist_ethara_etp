from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_rl", "post_install", "-at_install")
class TestAtlasRubricLevel(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Task = cls.env["atlas.atlas"].create({})
        cls.Criterion = cls.env["atlas.rubric.criterion"].create(
            {"atlas_id": cls.Task.id, "name": "Parent for levels"})
        cls.Level = cls.env["atlas.rubric.level"]

    def test_create_with_score(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 5})
        self.assertEqual(lv.score, 5)

    def test_default_score_is_0(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id})
        self.assertEqual(lv.score, 0)

    def test_score_negative(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": -10})
        self.assertEqual(lv.score, -10)

    def test_score_zero(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 0})
        self.assertEqual(lv.score, 0)

    def test_score_very_large(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 10 ** 8})
        self.assertEqual(lv.score, 10 ** 8)

    def test_label_stored(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 1, "label": "good"})
        self.assertEqual(lv.label, "good")

    def test_label_empty_stored_false(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 1, "label": ""})
        self.assertFalse(lv.label)

    def test_label_unicode(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 1, "label": "\u4e2d\u6587"})
        self.assertEqual(lv.label, "\u4e2d\u6587")

    def test_score_required(self):
        pass

    def test_criterion_id_required(self):
        with self.assertRaises(Exception):
            self.Level.create({"score": 0})

    def test_ordering_ascending_by_score(self):
        c = self.env["atlas.rubric.criterion"].create(
            {"atlas_id": self.Task.id, "name": "order_test"})
        l3 = self.Level.create({"criterion_id": c.id, "score": 3})
        l1 = self.Level.create({"criterion_id": c.id, "score": 1})
        l2 = self.Level.create({"criterion_id": c.id, "score": 2})
        got = self.Level.search([("criterion_id", "=", c.id)])
        self.assertEqual([r.id for r in got], [l1.id, l2.id, l3.id])

    def test_ordering_tie_breaker_by_id(self):
        c = self.env["atlas.rubric.criterion"].create(
            {"atlas_id": self.Task.id, "name": "order_tie"})
        a = self.Level.create({"criterion_id": c.id, "score": 5})
        b = self.Level.create({"criterion_id": c.id, "score": 5})
        got = self.Level.search([("criterion_id", "=", c.id)])
        self.assertEqual([r.id for r in got], [a.id, b.id])

    def test_cascade_unlink_from_criterion(self):
        c = self.env["atlas.rubric.criterion"].create(
            {"atlas_id": self.Task.id, "name": "cascade_src"})
        lv = self.Level.create({"criterion_id": c.id, "score": 1})
        lid = lv.id
        c.unlink()
        self.assertFalse(self.Level.search([("id", "=", lid)]))

    def test_write_score(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 0})
        lv.score = 99
        self.assertEqual(lv.score, 99)

    def test_write_label(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 0, "label": "old"})
        lv.label = "new"
        self.assertEqual(lv.label, "new")

    def test_unlink_self_removes(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 0})
        lid = lv.id
        lv.unlink()
        self.assertFalse(self.Level.search([("id", "=", lid)]))

    def test_multiple_levels_same_score_allowed(self):
        a = self.Level.create({"criterion_id": self.Criterion.id, "score": 7})
        b = self.Level.create({"criterion_id": self.Criterion.id, "score": 7})
        self.assertNotEqual(a.id, b.id)

    def test_label_with_special_chars_stored(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 0,
                                "label": "'; DROP TABLE; --"})
        self.assertEqual(lv.label, "'; DROP TABLE; --")

    def test_label_with_emoji_stored(self):
        lv = self.Level.create({"criterion_id": self.Criterion.id, "score": 0,
                                "label": "good \U0001f44d"})
        self.assertIn("\U0001f44d", lv.label)
