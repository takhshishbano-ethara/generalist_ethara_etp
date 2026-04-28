from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_rc", "post_install", "-at_install")
class TestAtlasRubricCriterion(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Task = cls.env["atlas.atlas"].create({})
        cls.Criterion = cls.env["atlas.rubric.criterion"]
        cls.Level = cls.env["atlas.rubric.level"]

    def _crit(self, **kw):
        kw.setdefault("atlas_id", self.Task.id)
        kw.setdefault("name", "crit_" + str(hash(frozenset(kw.items()))))
        return self.Criterion.create(kw)

    def test_create_minimal_requires_atlas_and_name(self):
        c = self._crit()
        self.assertTrue(c.id > 0)

    def test_default_weight_is_5(self):
        c = self._crit()
        self.assertEqual(c.weight, 5)

    def test_default_sequence_is_10(self):
        c = self._crit()
        self.assertEqual(c.sequence, 10)

    def test_default_category_is_other(self):
        c = self._crit()
        self.assertEqual(c.category, "other")

    def test_default_is_negative_false(self):
        c = self._crit()
        self.assertFalse(c.is_negative)

    def test_all_categories_accepted(self):
        for cat in ["factuality_hallucination", "task_completion", "instruction_following",
                    "communication_style", "other"]:
            c = self._crit(category=cat, name="cat_%s" % cat)
            self.assertEqual(c.category, cat)

    def test_all_importances_accepted(self):
        for imp in ["critically_detrimental", "detrimental", "slightly_detrimental",
                    "slightly_important", "important", "critically_important"]:
            c = self._crit(importance=imp, name="imp_%s" % imp)
            self.assertEqual(c.importance, imp)

    def test_all_qc_statuses_accepted(self):
        for st in ["pending", "running", "done", "error"]:
            c = self._crit(qc_status=st, name="qcs_%s" % st)
            self.assertEqual(c.qc_status, st)

    def test_all_qc_severities_accepted(self):
        for sv in ["low", "medium", "high", "critical"]:
            c = self._crit(qc_severity=sv, name="qcv_%s" % sv)
            self.assertEqual(c.qc_severity, sv)

    def test_weight_negative_accepted(self):
        c = self._crit(weight=-5, name="wn")
        self.assertEqual(c.weight, -5)

    def test_weight_zero_accepted(self):
        c = self._crit(weight=0, name="wz")
        self.assertEqual(c.weight, 0)

    def test_weight_very_large(self):
        c = self._crit(weight=10 ** 9, name="wl")
        self.assertEqual(c.weight, 10 ** 9)

    def test_sequence_ordering_ascending(self):
        t = self.env["atlas.atlas"].create({})
        c1 = self.Criterion.create({"atlas_id": t.id, "name": "Z", "sequence": 30})
        c2 = self.Criterion.create({"atlas_id": t.id, "name": "A", "sequence": 10})
        c3 = self.Criterion.create({"atlas_id": t.id, "name": "M", "sequence": 20})
        got = self.Criterion.search([("atlas_id", "=", t.id)])
        self.assertEqual([r.id for r in got], [c2.id, c3.id, c1.id])

    def test_sequence_tie_breaker_by_id(self):
        t = self.env["atlas.atlas"].create({})
        c1 = self.Criterion.create({"atlas_id": t.id, "name": "first", "sequence": 10})
        c2 = self.Criterion.create({"atlas_id": t.id, "name": "second", "sequence": 10})
        got = self.Criterion.search([("atlas_id", "=", t.id)])
        self.assertEqual([r.id for r in got], [c1.id, c2.id])

    def test_cascade_unlink_from_atlas(self):
        t = self.env["atlas.atlas"].create({})
        c = self.Criterion.create({"atlas_id": t.id, "name": "will_cascade"})
        cid = c.id
        t.unlink()
        self.assertFalse(self.Criterion.search([("id", "=", cid)]))

    def test_level_ids_one2many(self):
        c = self._crit(name="lvl_parent")
        self.Level.create({"criterion_id": c.id, "score": 0, "label": "bad"})
        self.Level.create({"criterion_id": c.id, "score": 1, "label": "ok"})
        self.assertEqual(len(c.level_ids), 2)

    def test_unlink_criterion_cascades_levels(self):
        c = self._crit(name="cascade_level")
        lv = self.Level.create({"criterion_id": c.id, "score": 0})
        lid = lv.id
        c.unlink()
        self.assertFalse(self.Level.search([("id", "=", lid)]))

    def test_is_negative_true(self):
        c = self._crit(is_negative=True, name="neg")
        self.assertTrue(c.is_negative)

    def test_custom_category_stored(self):
        c = self._crit(category="other", custom_category="mine", name="cc")
        self.assertEqual(c.custom_category, "mine")

    def test_suggestion_long_text(self):
        s = "x" * 2000
        c = self._crit(suggestion=s, name="sug")
        self.assertEqual(c.suggestion, s)

    def test_name_required_raises(self):
        from psycopg2 import IntegrityError
        from odoo.tools import mute_logger
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"):
            self.Criterion.create({"atlas_id": self.Task.id})

    def test_atlas_id_required_raises(self):
        from psycopg2 import IntegrityError
        from odoo.tools import mute_logger
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"):
            self.Criterion.create({"name": "no_parent"})

    def test_write_category(self):
        c = self._crit(name="w")
        c.category = "task_completion"
        self.assertEqual(c.category, "task_completion")

    def test_write_weight(self):
        c = self._crit(name="wr")
        c.weight = 99
        self.assertEqual(c.weight, 99)

    def test_qc_feedback_text_preserved(self):
        c = self._crit(qc_feedback="line1\nline2", name="fb")
        self.assertEqual(c.qc_feedback, "line1\nline2")

    def test_category_invalid_rejected(self):
        with self.assertRaises(Exception):
            self._crit(category="bogus", name="bog")

    def test_importance_invalid_rejected(self):
        with self.assertRaises(Exception):
            self._crit(importance="bogus", name="bog_imp")

    def test_unicode_name_preserved(self):
        c = self._crit(name="\u4e2d\u6587 criterion name")
        self.assertEqual(c.name, "\u4e2d\u6587 criterion name")
