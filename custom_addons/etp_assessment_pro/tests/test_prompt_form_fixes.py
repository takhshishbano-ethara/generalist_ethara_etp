# -*- coding: utf-8 -*-
import base64
from uuid import uuid4

from odoo.tests.common import TransactionCase, tagged


@tagged("-at_install", "post_install")
class TestFreshInstallAccess(TransactionCase):
    def test_admin_has_manager_group(self):
        admin = self.env.ref("base.user_admin")
        mgr = self.env.ref("etp_assessment_pro.group_assessment_manager")
        self.assertIn(mgr, admin.group_ids,
                      "admin must hold the ETP Manager group after install")

    def test_manager_group_has_members(self):
        mgr = self.env.ref("etp_assessment_pro.group_assessment_manager")
        self.assertTrue(mgr.user_ids,
                        "the Manager group must not be empty on install")

    def test_manager_implies_evaluator(self):
        mgr = self.env.ref("etp_assessment_pro.group_assessment_manager")
        ev = self.env.ref("etp_assessment_pro.group_assessment_evaluator")
        self.assertIn(ev, mgr.implied_ids)


@tagged("-at_install", "post_install")
class TestSimilarPromptsNewIdGuard(TransactionCase):
    def _tag(self):
        return self.env["etp.assessment.pro.tag"].create(
            {"name": "t-%s" % uuid4().hex[:8]})

    def test_newid_record_with_tags_does_not_crash(self):
        tag = self._tag()
        rec = self.env["etp.assessment.pro.prompt"].new({
            "name": "unsaved", "tag_ids": [(6, 0, tag.ids)]})
        rec._compute_similar_count()
        self.assertEqual(rec.similar_count, 0)
        self.assertEqual(rec._similar_prompts(), [])

    def test_editing_saved_record_binds_origin_id(self):
        tag = self._tag()
        Prompt = self.env["etp.assessment.pro.prompt"]
        p1 = Prompt.create({"name": "p1", "tag_ids": [(6, 0, tag.ids)]})
        p2 = Prompt.create({"name": "p2", "tag_ids": [(6, 0, tag.ids)]})
        virtual = Prompt.new({"tag_ids": [(6, 0, tag.ids)]}, origin=p1)
        virtual._compute_similar_count()
        self.assertIn(p2, [s["prompt"] for s in p1._similar_prompts()])


@tagged("-at_install", "post_install")
class TestPromptResourceViews(TransactionCase):
    def _b64(self, text):
        return base64.b64encode(text.encode()).decode()

    def test_reference_category_exists(self):
        sel = dict(self.env["etp.assessment.pro.prompt.resource"]
                   ._fields["category"].selection)
        self.assertIn("reference", sel)

    def test_sop_resources_visible_before_save(self):
        p = self.env["etp.assessment.pro.prompt"].new({"name": "unsaved"})
        p._add_resource(self._b64("sop text"), "live.md", "sop")
        self.assertEqual(p.sop_resource_ids.mapped("name"), ["live.md"],
                         "the uploaded SOP must show before the record is saved")
        self.assertEqual(p.sop_resource_ids.mapped("status"), ["pending"])

    def test_sop_resources_exclude_non_sop(self):
        p = self.env["etp.assessment.pro.prompt"].new({"name": "unsaved"})
        p._add_resource(self._b64("ref"), "ref.md", "reference")
        self.assertFalse(p.sop_resource_ids,
                         "a reference file must not appear as the SOP")

    def test_filtered_sop_and_reference_views(self):
        p = self.env["etp.assessment.pro.prompt"].create({
            "name": "res",
            "resource_ids": [
                (0, 0, {"name": "sop.txt", "file": self._b64("hi"),
                        "category": "sop"}),
                (0, 0, {"name": "ref.txt", "file": self._b64("yo"),
                        "category": "reference"}),
            ]})
        self.assertEqual(p.sop_resource_ids.mapped("name"), ["sop.txt"])
        self.assertEqual(p.reference_resource_ids.mapped("name"), ["ref.txt"])
        p.allowed_question_type_ids = self.env[
            "etp.assessment.pro.question.type"].search([])
        p._raise_count_to_type_floor()
        self.assertEqual(p.sop_question_count, 0,
                         "Auto (0) must stay Auto — the floor only binds a fixed count")
        self.assertEqual(len(p.resource_ids), 2)

    def test_resource_status_badge(self):
        R = self.env["etp.assessment.pro.prompt.resource"]
        self.assertEqual(
            R.new({"name": "a", "file": self._b64("hi")}).status, "pending")
        self.assertEqual(
            R.new({"name": "b", "file": self._b64("hi"),
                   "extracted_text": "text"}).status, "ready")
        self.assertEqual(
            R.new({"name": "c", "file": self._b64("hi"),
                   "extraction_error": "boom"}).status, "failed")


@tagged("-at_install", "post_install")
class TestQuestionCountModeAndAllSelected(TransactionCase):
    def test_mode_reflects_count(self):
        P = self.env["etp.assessment.pro.prompt"]
        p = P.new({"sop_question_count": 0})
        self.assertEqual(p.question_count_mode, "auto")
        p.sop_question_count = 4
        self.assertEqual(p.question_count_mode, "fixed")

    def test_mode_auto_zeroes_count_onchange(self):
        p = self.env["etp.assessment.pro.prompt"].new({"sop_question_count": 4})
        p.question_count_mode = "auto"
        p._onchange_question_count_mode()
        self.assertEqual(p.sop_question_count, 0)

    def test_mode_auto_zeroes_count_on_write(self):
        p = self.env["etp.assessment.pro.prompt"].create(
            {"name": "m", "sop_question_count": 4})
        p.write({"question_count_mode": "auto"})
        self.assertEqual(p.sop_question_count, 0)

    def test_mode_fixed_seeds_count_to_type_floor(self):
        qtypes = self.env["etp.assessment.pro.question.type"].search([], limit=3)
        p = self.env["etp.assessment.pro.prompt"].new({
            "sop_question_count": 0,
            "allowed_question_type_ids": [(6, 0, qtypes.ids)]})
        p.question_count_mode = "fixed"
        p._onchange_question_count_mode()
        self.assertEqual(p.sop_question_count, 3)

    def test_types_all_selected_flag(self):
        P = self.env["etp.assessment.pro.prompt"]
        all_types = self.env["etp.assessment.pro.question.type"].search([])
        self.assertTrue(
            P.new({"allowed_question_type_ids": [(6, 0, all_types.ids)]})
            .types_all_selected)
        self.assertFalse(
            P.new({"allowed_question_type_ids": [(6, 0, all_types[:2].ids)]})
            .types_all_selected)
