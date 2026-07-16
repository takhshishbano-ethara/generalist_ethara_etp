# -*- coding: utf-8 -*-
"""Regression tests for two production bugs in the SOP question-generation flow.

BUG A — image_ab drafts were dropped as malformed because the generation
directive never told the model the image_ab OUTPUT CONTRACT (a flaw_plan with
construction_keys). The fix wires the flaw_plan contract into BOTH the
forced-type directive (_forced_type_directive for a run forced to image_ab) and
the generic multi-type directive (_image_contracts_note), so image_ab items now
carry a flaw_plan, survive _validate_question_item, and materialize a
flaw_plan_json (Phase-3 flaw-injection is finally active).

BUG B — _cron_generate_from_sop lost the final state commit to a serialization
race (SQLSTATE 40001) with the tag-extraction cron writing the same prompt row,
which rolled the whole transaction back and mis-reported the run as 'failed'. The
fix commits the drafts BEFORE the contended state write and retries that write on
40001, so the run ends 'done' with the drafts intact.
"""
import json
from unittest.mock import patch

from psycopg2 import errors as pg_errors

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError

from odoo.addons.etp_assessment_pro.services import vertex
from odoo.addons.etp_assessment_pro.constants import validate_flaw_plan


def _flaw_plan():
    """A valid flaw plan (flawed side b): no verdict names the flawed side and OC
    names the clean side, so validate_flaw_plan accepts it."""
    return {
        "flawed_side": "b",
        "clean_prompt": "A clean photorealistic red mug on a plain table.",
        "flawed_prompt": "The same mug but with an extra floating handle.",
        "injected_flaws": ["extra floating handle", "wrong handle count"],
        "construction_keys": {"IF": "Response A", "VQ": "Both Good",
                              "LAI": "Both Good", "OC": "Response A"},
    }


def _image_ab_item():
    return {
        "name": "AB flaw", "prompt": "Which image better follows the brief?",
        "question_type": "image_ab", "difficulty": "medium",
        "image_specs": {"flaw_plan": _flaw_plan()},
    }


@tagged("-at_install", "post_install")
class TestBugAImageAbDirectiveWiring(TransactionCase):
    """The image_ab flaw_plan contract is wired into generation and produces a
    valid, non-dropped draft carrying flaw_plan_json."""

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _sop_prompt(self):
        return self.Prompt.create({
            "name": "SOP directive bug",
            "source_text": "Author image A/B comparison questions."})

    def test_image_ab_item_yields_valid_draft_with_flaw_plan(self):
        prompt = self._sop_prompt()
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps([_image_ab_item()])):
            draft_ids = vertex.generate_questions_from_sop(self.env, prompt)
        self.assertEqual(len(draft_ids), 1,
                         "the image_ab item must NOT be dropped as malformed")
        draft = self.Draft.browse(draft_ids)
        self.assertEqual(draft.question_type, "image_ab")
        self.assertTrue(draft.flaw_plan_json,
                        "Phase-3 flaw injection must persist a flaw_plan_json")
        stored = json.loads(draft.flaw_plan_json)
        self.assertEqual(validate_flaw_plan(stored), [])
        self.assertEqual(set(stored["construction_keys"]),
                         {"IF", "VQ", "LAI", "OC"})

    def test_generic_directive_carries_flaw_plan_contract(self):
        prompt = self._sop_prompt()
        captured = {}

        def fake_call(env, system_prompt, **kw):
            captured["user_parts"] = kw.get("user_parts") or []
            return json.dumps([_image_ab_item()])

        with patch.object(vertex, "_call_vertex", side_effect=fake_call):
            vertex.generate_questions_from_sop(self.env, prompt)
        directive = " ".join(
            p.get("text", "") for p in captured["user_parts"]
            if isinstance(p, dict))
        for token in ("flaw_plan", "construction_keys", "injected_flaws"):
            self.assertIn(token, directive,
                          "generic SOP directive must document the image_ab "
                          "flaw_plan contract")

    def test_forced_image_ab_directive_carries_flaw_plan_contract(self):
        directive = vertex._forced_type_directive(
            "image_ab", 5, ab_dims=vertex._ab_fallback_dims())
        for token in ("image_ab", "flaw_plan", "construction_keys",
                      "injected_flaws", "IF", "VQ", "LAI", "OC"):
            self.assertIn(token, directive)

    def test_image_contracts_note_covers_all_image_types(self):
        note = vertex._image_contracts_note(vertex._ab_fallback_dims())
        self.assertIn("flaw_plan", note)
        self.assertIn("image_prompt", note)
        self.assertIn("image_label", note)


@tagged("-at_install", "post_install")
class TestBugBSopGenSerializationResilience(TransactionCase):
    """The SOP-gen cron survives a serialization race on its final state write:
    the drafts stay committed and the run ends 'done', not 'failed'."""

    def test_cron_survives_serialization_failure_on_state_commit(self):
        Prompt = self.env["etp.assessment.pro.prompt"]
        Draft = self.env["etp.assessment.pro.prompt.question"]
        prompt = Prompt.create({
            "name": "SOP race",
            "source_text": "Author some questions.",
            "sop_gen_state": "queued"})

        def fake_gen(env, prompt_rec, count=0, allowed_types=()):
            ids = []
            for i in range(2):
                ids.append(env["etp.assessment.pro.prompt.question"].sudo().create({
                    "prompt_id": prompt_rec.id,
                    "name": "Draft %d" % i,
                    "question_prompt": "Question %d text" % i,
                    "question_type": "mcq",
                    "options_json": json.dumps(["Alpha", "Beta"]),
                    "correct_answer_json": json.dumps("Alpha"),
                }).id)
            return ids

        PromptCls = type(prompt)
        orig_write = PromptCls.write
        state = {"raised": 0}

        def flaky_write(self, vals):
            # Simulate the concurrent tag-cron losing us the state-write race
            # exactly once, only on the terminal 'done' write (the reported line).
            if vals.get("sop_gen_state") == "done" and state["raised"] == 0:
                state["raised"] = 1
                raise pg_errors.SerializationFailure(
                    "could not serialize access due to concurrent update")
            return orig_write(self, vals)

        # A TransactionCase forbids real commit/rollback on its cursor, so the
        # cron's own commit/rollback are stubbed with flush/clear to keep the test
        # within one transaction while still exercising the finalize retry path.
        cr = self.env.cr

        def safe_commit():
            cr.flush()

        def safe_rollback():
            cr.clear()

        with patch.object(vertex, "generate_questions_from_sop",
                          side_effect=fake_gen), \
                patch.object(PromptCls, "write", flaky_write), \
                patch.object(cr, "commit", safe_commit), \
                patch.object(cr, "rollback", safe_rollback):
            Prompt._cron_generate_from_sop()

        prompt.invalidate_recordset()
        self.assertEqual(state["raised"], 1,
                         "the serialization race must actually have been hit")
        self.assertEqual(prompt.sop_gen_state, "done",
                         "the transient race must self-heal to 'done'")
        self.assertFalse(prompt.sop_gen_error)
        drafts = Draft.search([("prompt_id", "=", prompt.id)])
        self.assertEqual(len(drafts), 2,
                         "drafts committed before the race must survive it")


@tagged("-at_install", "post_install")
class TestTagExtractSerializationResilience(TransactionCase):
    """The tag-extraction cron survives a serialization race on its final state
    write: the tags stay committed and the run ends 'done', not 'failed'."""

    def test_cron_survives_serialization_failure_on_state_commit(self):
        Prompt = self.env["etp.assessment.pro.prompt"]
        prompt = Prompt.create({
            "name": "Tag race",
            "source_text": "Author some questions.",
            "tag_extract_state": "queued"})

        def fake_extract(env, prompt_rec):
            return (["domain:logistics", "task:routing"], '{"tags": []}')

        PromptCls = type(prompt)
        orig_write = PromptCls.write
        state = {"raised": 0}

        def flaky_write(self, vals):
            if vals.get("tag_extract_state") == "done" and state["raised"] == 0:
                state["raised"] = 1
                raise pg_errors.SerializationFailure(
                    "could not serialize access due to concurrent update")
            return orig_write(self, vals)

        cr = self.env.cr

        def safe_commit():
            cr.flush()

        def safe_rollback():
            cr.clear()

        with patch.object(vertex, "extract_tags_from_sop",
                          side_effect=fake_extract), \
                patch.object(PromptCls, "write", flaky_write), \
                patch.object(cr, "commit", safe_commit), \
                patch.object(cr, "rollback", safe_rollback):
            Prompt._cron_extract_tags()

        prompt.invalidate_recordset()
        self.assertEqual(state["raised"], 1,
                         "the serialization race must actually have been hit")
        self.assertEqual(prompt.tag_extract_state, "done",
                         "the transient race must self-heal to 'done', not fail")
        self.assertNotEqual(prompt.tag_extract_state, "failed")
        self.assertFalse(prompt.tag_extract_error)
        self.assertTrue(prompt.tag_ids,
                        "tags committed before the race must survive it")


@tagged("-at_install", "post_install")
class TestTagExtractManualOnly(TransactionCase):
    """Tag extraction is MANUAL-ONLY: the Extract Tags button runs the Vertex
    extract inline and stores the tags (never merely queues), a failure records a
    terminal 'failed' state (never stuck in 'queued'), and the scheduled tag-cron
    ir.cron record no longer exists."""

    def test_button_extracts_inline_and_stores_tags(self):
        Prompt = self.env["etp.assessment.pro.prompt"]
        prompt = Prompt.create({
            "name": "Manual tags", "source_text": "Author some questions."})

        def fake_extract(env, prompt_rec):
            return (["domain:logistics", "task:routing"], '{"tags": []}')

        with patch.object(vertex, "extract_tags_from_sop",
                          side_effect=fake_extract):
            prompt.action_extract_tags()
        prompt.invalidate_recordset()
        self.assertEqual(prompt.tag_extract_state, "done")
        self.assertNotEqual(prompt.tag_extract_state, "queued")
        self.assertEqual(sorted(prompt.tag_ids.mapped("name")),
                         ["domain:logistics", "task:routing"])

    def test_button_records_failure_not_queued(self):
        Prompt = self.env["etp.assessment.pro.prompt"]
        prompt = Prompt.create({
            "name": "Manual tags fail", "source_text": "x"})

        def boom(env, prompt_rec):
            raise RuntimeError("vertex exploded")

        with patch.object(vertex, "extract_tags_from_sop", side_effect=boom):
            with self.assertRaises(UserError):
                prompt.action_extract_tags()
        self.assertNotEqual(prompt.tag_extract_state, "queued")
        self.assertFalse(prompt.tag_ids)

    def test_tag_cron_record_removed(self):
        self.assertFalse(
            self.env.ref("etp_assessment_pro.ir_cron_extract_tags",
                         raise_if_not_found=False),
            "the scheduled tag-extraction cron must no longer exist")


class TestTruncatedJsonSalvage(TransactionCase):
    """_extract_json_array recovers the complete leading items of a JSON array
    truncated at the output-token ceiling, instead of hard-failing the run."""

    def test_salvage_recovers_complete_objects_from_truncated_array(self):
        truncated = (
            '[\n {"name": "Q1", "question_type": "image_ab"},\n'
            ' {"name": "Q2", "question_type": "image_ab"},\n'
            ' {"name": "Q3", "prompt": "Evaluate the two ')
        items = vertex._salvage_json_objects(truncated)
        self.assertEqual([i["name"] for i in items], ["Q1", "Q2"])

    def test_extract_json_array_returns_salvaged_on_truncation(self):
        truncated = (
            '[{"name": "Q1", "question_type": "image_ab"}, '
            '{"name": "Q2", "prompt": "Rate them on Instruction Foll')
        self.assertEqual(
            [i["name"] for i in vertex._extract_json_array(truncated)], ["Q1"])

    def test_extract_json_array_still_parses_clean_array(self):
        clean = '[{"name": "Q1"}, {"name": "Q2"}]'
        self.assertEqual(
            [i["name"] for i in vertex._extract_json_array(clean)], ["Q1", "Q2"])

    def test_salvage_returns_empty_when_first_object_incomplete(self):
        self.assertEqual(vertex._salvage_json_objects('[{"name": "Q1'), [])

    def test_extract_json_array_raises_when_unsalvageable(self):
        with self.assertRaises(ValueError):
            vertex._extract_json_array("total garbage no json here")
