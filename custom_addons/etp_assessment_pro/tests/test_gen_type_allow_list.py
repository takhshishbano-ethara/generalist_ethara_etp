# -*- coding: utf-8 -*-
"""Tests for the generation question-type ALLOW-LIST (multi-select force type).

The generator's single-select force_question_type became a multi-select allow-list
(allowed_question_type_ids, a Many2many into the seeded etp.assessment.pro.question.
type vocabulary). These cover: back-compat (a 1-element list reproduces the legacy
forced directive), the mixed text+image directive, the image-majority nudge being
gated off for an allow-list, the OVERRIDE->FILTER applier change, the fail-closed
gate, and the reader + vocabulary model.
"""
import json
from unittest.mock import patch, Mock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError

from odoo.addons.etp_assessment_pro.services import vertex
from odoo.addons.etp_assessment_pro.constants import QUESTION_TYPE_SELECTION

_LOGGER = "odoo.addons.etp_assessment_pro.services.vertex"


def _mcq_item():
    return {"name": "MCQ", "prompt": "Under a threats-only policy, what applies?",
            "question_type": "mcq", "difficulty": "easy",
            "options": ["Allow", "Remove", "Escalate"],
            "correct_answer": "Allow"}


def _image_ab_item():
    return {"name": "AB", "prompt": "Which image better follows the brief?",
            "question_type": "image_ab", "difficulty": "medium",
            "image_specs": {"flaw_plan": {
                "flawed_side": "b",
                "clean_prompt": "A clean red mug on a plain table.",
                "flawed_prompt": "The same mug with an extra floating handle.",
                "injected_flaws": ["extra floating handle", "wrong handle count"],
                "construction_keys": {"IF": "Response A", "VQ": "Both Good",
                                      "LAI": "Both Good", "OC": "Response A"}}}}


@tagged("-at_install", "post_install")
class TestGenTypeDirective(TransactionCase):
    """The directive builders — back-compat, mixed contracts, nudge gating."""

    def test_single_type_list_matches_legacy_forced_directive(self):
        # The strongest back-compat guarantee: a 1-element allow-list is
        # byte-identical to the old forced-type directive, for image AND text.
        ab = vertex._ab_fallback_dims()
        self.assertEqual(
            vertex._allowed_types_directive(("video_prompt",), 3, ab_dims=ab),
            vertex._forced_type_directive("video_prompt", 3, ab_dims=ab))
        self.assertEqual(
            vertex._allowed_types_directive(("mcq",), 5),
            vertex._forced_type_directive("mcq", 5))

    def test_mixed_directive_carries_both_contracts_and_excludes_others(self):
        ab = vertex._ab_fallback_dims()
        d = vertex._allowed_types_directive(("mcq", "image_ab"), 4, ab_dims=ab)
        self.assertIn("options (>=3 strings)", d)   # mcq text contract
        self.assertIn("flaw_plan", d)               # image_ab image contract
        self.assertIn("one of", d)                  # multi-type preamble
        # image_prompt / image_label are NOT allowed -> their contract is absent.
        # Assert the "For <type> items:" PREFIX, not the bare token (which leaks
        # from image_ab's own contract text and would make the test lie).
        self.assertNotIn("For image_prompt items:", d)
        self.assertNotIn("For image_label items:", d)

    def test_majority_nudge_off_for_allow_list_on_for_generic(self):
        ab = vertex._ab_fallback_dims()
        multi = vertex._allowed_types_directive(("mcq", "image_ab"), 4, ab_dims=ab)
        self.assertNotIn("MAJORITY", multi,
                         "the image-majority nudge must NOT override an allow-list")
        # The generic (no allow-list) path is now TASK-FIRST with NO fixed ratio:
        # it must not force an image-comparison majority and must ask for BOTH a
        # real TASK and assessment questions, driven by the SOP's tasks.
        generic = self._capture_directive(allowed_types=())
        self.assertNotIn("MUST be the MAJORITY", generic,
                         "the generic path must no longer force an image-type majority")
        self.assertNotIn("25%", generic,
                         "the fixed 25% task ratio must be gone")
        self.assertIn("TASK-FIRST", generic,
                      "the generic path must use the task-first directive")
        self.assertIn("Task: ", generic,
                      "the generic path must ask for 'Task:' real-task items")
        self.assertIn("ASSESSMENT QUESTIONS", generic,
                      "the generic path must also ask for assessment questions")

    def test_image_contracts_note_types_none_is_generic_hardcoded_set(self):
        ab = vertex._ab_fallback_dims()
        # types=None must reproduce the historical all-image-types note exactly.
        self.assertEqual(
            vertex._image_contracts_note(ab, types=None),
            vertex._image_contracts_note(ab))
        note = vertex._image_contracts_note(ab, types=("image_ab",))
        self.assertIn("For image_ab items:", note)
        self.assertNotIn("For image_label items:", note)

    def _capture_directive(self, allowed_types):
        prompt = self.env["etp.assessment.pro.prompt"].create({
            "name": "cap", "source_text": "Author questions."})
        captured = {}

        def fake_call(env, system_prompt, **kw):
            parts = kw.get("user_parts") or []
            captured["text"] = " ".join(p.get("text", "") for p in parts)
            return json.dumps([])

        with patch.object(vertex, "_call_vertex", side_effect=fake_call):
            vertex.generate_questions_from_sop(
                self.env, prompt, allowed_types=allowed_types)
        return captured["text"]


@tagged("-at_install", "post_install")
class TestGenTypeFilterAndGate(TransactionCase):
    """The applier is a FILTER (not an override); the gate fails closed."""

    def _prompt(self):
        return self.env["etp.assessment.pro.prompt"].create({
            "name": "filter", "source_text": "Author questions."})

    def test_out_of_list_item_dropped_and_logged(self):
        prompt = self._prompt()
        Draft = self.env["etp.assessment.pro.prompt.question"]
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps([_mcq_item(), _image_ab_item()])):
            with self.assertLogs(_LOGGER, level="WARNING") as cm:
                draft_ids = vertex.generate_questions_from_sop(
                    self.env, prompt,
                    allowed_types=("mcq", "subjective_rubric"))
        types = set(Draft.browse(draft_ids).mapped("question_type"))
        self.assertNotIn("image_ab", types,
                         "an out-of-list image_ab item must be dropped, not stamped")
        self.assertTrue(any("out-of-scope" in m for m in cm.output),
                        "the drop must be logged as out-of-scope")

    def test_generic_path_keeps_all_valid_types(self):
        prompt = self._prompt()
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps([_mcq_item(), _image_ab_item()])):
            draft_ids = vertex.generate_questions_from_sop(self.env, prompt)
        types = set(self.env["etp.assessment.pro.prompt.question"]
                    .browse(draft_ids).mapped("question_type"))
        self.assertIn("image_ab", types,
                      "with no allow-list the generic path keeps mixed types")

    def test_unknown_type_fails_closed_before_llm_call(self):
        prompt = self._prompt()
        mock = Mock()
        with patch.object(vertex, "_call_vertex", mock):
            with self.assertRaises(ValueError):
                vertex.generate_questions_from_sop(
                    self.env, prompt, allowed_types=("nope",))
        mock.assert_not_called()

    def test_non_string_item_question_type_does_not_crash_batch(self):
        # BUG 1 regression: a model item with a non-string question_type (e.g. a
        # list) must NOT raise TypeError (unhashable) and abort the whole batch —
        # it is dropped, the batch's other valid items still persist.
        prompt = self._prompt()
        bad = {"name": "bad", "prompt": "x", "question_type": ["mcq"]}
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps([bad, _mcq_item()])):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, allowed_types=("mcq", "subjective_rubric"))
        types = set(self.env["etp.assessment.pro.prompt.question"]
                    .browse(draft_ids).mapped("question_type"))
        self.assertIn("mcq", types,
                      "batch must complete and keep the valid mcq item")

    def test_non_string_allow_list_entry_fails_closed_with_valueerror(self):
        # BUG 2 regression: a non-string allow-list entry must raise the intended
        # ValueError (surfaced to sop_gen_error), not a TypeError from sorted/join.
        prompt = self._prompt()
        mock = Mock()
        with patch.object(vertex, "_call_vertex", mock):
            with self.assertRaises(ValueError):
                vertex.generate_questions_from_sop(
                    self.env, prompt, allowed_types=(None,))
        mock.assert_not_called()


@tagged("-at_install", "post_install")
class TestAllowListReaderAndModel(TransactionCase):
    """_allowed_question_types() ordering/fallback and the vocabulary model."""

    def _qtype(self, code):
        rec = self.env["etp.assessment.pro.question.type"].with_context(
            active_test=False).search([("code", "=", code)], limit=1)
        self.assertTrue(rec, "vocabulary must be seeded with %r" % code)
        return rec

    def test_reader_order_dedupe_and_legacy_fallback(self):
        Prompt = self.env["etp.assessment.pro.prompt"]
        # Link in the "wrong" order (image_ab before mcq); the reader must still
        # emit them in vocabulary (sequence) order, i.e. mcq before image_ab.
        p = Prompt.create({
            "name": "reader",
            "allowed_question_type_ids": [
                (6, 0, [self._qtype("image_ab").id, self._qtype("mcq").id])]})
        self.assertEqual(p._allowed_question_types(), ("mcq", "image_ab"),
                         "reader follows the vocabulary sequence order")
        empty = Prompt.create({"name": "empty"})
        self.assertEqual(empty._allowed_question_types(), ())
        empty.force_question_type = "subjective_rubric"
        self.assertEqual(empty._allowed_question_types(), ("subjective_rubric",),
                         "no links -> fall back to the legacy scalar")

    def test_vocabulary_seeded_from_constants(self):
        QType = self.env["etp.assessment.pro.question.type"].with_context(
            active_test=False)
        rows = {t.code: t.name for t in QType.search([])}
        expected = dict(QUESTION_TYPE_SELECTION)  # constants = single source
        self.assertEqual(set(rows), set(expected),
                         "every taxonomy code has exactly one vocabulary row")
        for code, label in expected.items():
            self.assertEqual(rows[code], label,
                             "vocabulary label mirrors the constant")

    def test_vocabulary_code_is_unique(self):
        # The init() unique index on `code` makes a second row structurally
        # impossible — a duplicate mirror of the closed enum is a bug.
        from psycopg2 import IntegrityError
        QType = self.env["etp.assessment.pro.question.type"]
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                QType.create({"code": "mcq", "name": "Dup MCQ"})


@tagged("-at_install", "post_install")
class TestCountGuardrails(TransactionCase):
    """Questions-to-Generate is kept >= the number of selected types (auto-raise
    onchange + hard constraint), plus the one-click select-all toggle."""

    def _qtypes(self, codes):
        return self.env["etp.assessment.pro.question.type"].with_context(
            active_test=False).search([("code", "in", list(codes))])

    def test_negative_count_rejected(self):
        with self.assertRaises(ValidationError):
            self.env["etp.assessment.pro.prompt"].create({
                "name": "neg", "sop_question_count": -1})

    def test_count_below_type_floor_rejected(self):
        types = self._qtypes(["mcq", "msq", "subjective_rubric"])
        with self.assertRaises(ValidationError):
            self.env["etp.assessment.pro.prompt"].create({
                "name": "low", "sop_question_count": 2,
                "allowed_question_type_ids": [(6, 0, types.ids)]})

    def test_count_zero_with_types_is_exempt(self):
        # 0 means "let the model decide the count" — never blocked by the floor.
        types = self._qtypes(["mcq", "msq", "subjective_rubric"])
        rec = self.env["etp.assessment.pro.prompt"].create({
            "name": "zero", "sop_question_count": 0,
            "allowed_question_type_ids": [(6, 0, types.ids)]})
        self.assertTrue(rec.exists())

    def test_count_at_or_above_floor_ok(self):
        types = self._qtypes(["mcq", "msq", "subjective_rubric"])
        rec = self.env["etp.assessment.pro.prompt"].create({
            "name": "ok", "sop_question_count": 3,
            "allowed_question_type_ids": [(6, 0, types.ids)]})
        self.assertEqual(rec.sop_question_count, 3)

    def test_floor_auto_raises_count(self):
        rec = self.env["etp.assessment.pro.prompt"].new({"sop_question_count": 2})
        rec.allowed_question_type_ids = self._qtypes(
            ["mcq", "msq", "subjective_rubric", "image_ab", "image_prompt"])
        rec._raise_count_to_type_floor()
        self.assertEqual(rec.sop_question_count, 5,
                         "count auto-raises to the number of selected types")

    def test_floor_only_raises_never_lowers(self):
        rec = self.env["etp.assessment.pro.prompt"].new({"sop_question_count": 9})
        rec.allowed_question_type_ids = self._qtypes(["mcq", "msq"])
        rec._raise_count_to_type_floor()
        self.assertEqual(rec.sop_question_count, 9,
                         "a count already above the floor is left untouched")

    def test_floor_exempts_zero(self):
        rec = self.env["etp.assessment.pro.prompt"].new({"sop_question_count": 0})
        rec.allowed_question_type_ids = self._qtypes(["mcq", "msq", "image_ab"])
        rec._raise_count_to_type_floor()
        self.assertEqual(rec.sop_question_count, 0, "0 (auto) is never raised")

    def test_select_all_loads_every_type_and_resets(self):
        total = self.env["etp.assessment.pro.question.type"].search_count([])
        rec = self.env["etp.assessment.pro.prompt"].new({
            "sop_question_count": 2, "select_all_types": True})
        rec._onchange_select_all_types()
        self.assertEqual(len(rec.allowed_question_type_ids), total,
                         "one click loads every question type")
        self.assertFalse(rec.select_all_types,
                         "the toggle resets so it behaves as a momentary button")
        self.assertEqual(rec.sop_question_count, total,
                         "count is raised to cover every selected type")


@tagged("-at_install", "post_install")
class TestJsonSalvage(TransactionCase):
    """The merged _salvage_json_objects recovers the leading COMPLETE items from a
    JSON array truncated by the output-token ceiling (mid-array), instead of
    losing the whole batch."""

    def test_recovers_leading_complete_objects(self):
        truncated = '[{"a": 1}, {"b": 2}, {"c": '
        self.assertEqual(vertex._salvage_json_objects(truncated),
                         [{"a": 1}, {"b": 2}])

    def test_no_opening_bracket_returns_empty(self):
        self.assertEqual(vertex._salvage_json_objects("not json at all"), [])

    def test_extract_json_array_falls_back_to_salvage(self):
        truncated = ('[{"name": "Q1", "prompt": "p1"}, '
                     '{"name": "Q2", "prompt": "p2"}, {"nam')
        out = vertex._extract_json_array(truncated)
        self.assertEqual(len(out), 2, "the two complete items survive")
        self.assertEqual(out[0]["name"], "Q1")
