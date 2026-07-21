# -*- coding: utf-8 -*-
"""Tests for the harness-aligned generation robustness layer (Option A) and the
authoring-harness bank import (Option B).

OPTION A - services/vertex._selfheal_generation wraps a raw SOP generation with:
  * top-up  : a short/truncated batch is completed by requesting only the
              shortfall, targeting uncovered required_elements (no duplicates),
  * backfill: missing answer keys are regenerated from the finalized questions,
  * critique: a strict second pass corrects wrong/ambiguous answer keys.
All three are config-gated (etp_assessment_pro.gen_selfheal / gen_critique) and
best-effort - a failure never sinks the base batch.

OPTION B - models.bank_import.import_bank_harness maps an authoring-harness run
folder (questions.json + solutions.json / output.json, the Opus seed-prompt
schema) into review drafts on a generator.

Both are exercised by patching services.vertex._call_vertex with a stateful
side_effect that returns a different payload per pipeline stage, keyed off the
directive text - the same patch-the-brain pattern the other gen tests use.
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etp_assessment_pro.services import vertex


def _mcq(name, correct="A"):
    return {"name": name, "prompt": "Pick one for %s" % name,
            "question_type": "mcq", "difficulty": "easy",
            "options": ["A", "B", "C"], "correct_answer": correct}


def _sol(ref, ans="A", rationale="because"):
    return {"question_ref": ref, "answers": ans, "rationale": rationale}


@tagged("-at_install", "post_install")
class TestGenerationSelfHeal(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]
        self.ICP = self.env["ir.config_parameter"].sudo()

    def _sop(self):
        return self.Prompt.create({
            "name": "SOP self-heal",
            "source_text": "Author objective questions from this SOP."})

    def test_topup_completes_a_short_batch(self):
        """A base run returns 1 item but count=3; top-up supplies the shortfall
        without the critique changing the tally."""
        self.ICP.set_param("etp_assessment_pro.gen_critique", "0")
        prompt = self._sop()

        calls = {"n": 0}

        def brain(env, system_prompt, **kw):
            parts = kw.get("user_parts") or []
            text = " ".join(p.get("text", "") for p in parts
                            if isinstance(p, dict))
            calls["n"] += 1
            if "already authored" in text:            # top-up request
                return json.dumps({
                    "questions": [_mcq("Q2"), _mcq("Q3")],
                    "solutions": [_sol("Q2"), _sol("Q3")]})
            return json.dumps({                        # base short batch
                "questions": [_mcq("Q1")],
                "solutions": [_sol("Q1")]})

        with patch.object(vertex, "_call_vertex", side_effect=brain):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, count=3)
        self.assertEqual(len(draft_ids), 3,
                         "top-up must complete the batch to the requested count")
        self.assertGreaterEqual(calls["n"], 2, "a top-up call must have fired")

    def test_topup_never_exceeds_requested_count(self):
        """A top-up that over-delivers past the shortfall is trimmed back to the
        requested count (matches the harness's questions[:n] cap)."""
        self.ICP.set_param("etp_assessment_pro.gen_critique", "0")
        prompt = self._sop()

        def brain(env, system_prompt, **kw):
            parts = kw.get("user_parts") or []
            text = " ".join(p.get("text", "") for p in parts
                            if isinstance(p, dict))
            if "already authored" in text:            # over-delivering top-up
                return json.dumps({
                    "questions": [_mcq("Q2"), _mcq("Q3"), _mcq("Q4"), _mcq("Q5")],
                    "solutions": [_sol("Q2"), _sol("Q3"), _sol("Q4"), _sol("Q5")]})
            return json.dumps({"questions": [_mcq("Q1")],
                               "solutions": [_sol("Q1")]})

        with patch.object(vertex, "_call_vertex", side_effect=brain):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, count=3)
        self.assertEqual(len(draft_ids), 3,
                         "batch must be capped at the requested count, "
                         "not the over-delivered total")

    def test_topup_disabled_by_config(self):
        self.ICP.set_param("etp_assessment_pro.gen_selfheal", "0")
        prompt = self._sop()
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps({"questions": [_mcq("Q1")]})):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, count=5)
        self.assertEqual(len(draft_ids), 1,
                         "self-heal off: no top-up, only the base item persists")

    def test_backfill_supplies_missing_answer_keys(self):
        """Base batch has questions but NO solutions; backfill regenerates them so
        the persisted draft carries a solution_json."""
        self.ICP.set_param("etp_assessment_pro.gen_critique", "0")
        prompt = self._sop()

        def brain(env, system_prompt, **kw):
            parts = kw.get("user_parts") or []
            text = " ".join(p.get("text", "") for p in parts
                            if isinstance(p, dict))
            if "FINALIZED questions" in text:          # backfill request
                return json.dumps({"solutions": [_sol("Q1", "B")]})
            return json.dumps({"questions": [_mcq("Q1")]})  # no solutions

        with patch.object(vertex, "_call_vertex", side_effect=brain):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, count=1)
        draft = self.Draft.browse(draft_ids)
        self.assertEqual(len(draft), 1)
        self.assertTrue(draft.solution_json,
                        "backfill must persist a regenerated answer key")

    def test_critique_corrects_answer_key(self):
        """The critique pass returns a corrected solutions array; the correction
        (answer B, not the original A) is what gets persisted."""
        prompt = self._sop()

        def brain(env, system_prompt, **kw):
            parts = kw.get("user_parts") or []
            text = " ".join(p.get("text", "") for p in parts
                            if isinstance(p, dict))
            if "STRICT assessment reviewer" in text:   # critique request
                return json.dumps({
                    "solutions": [_sol("Q1", "B", "corrected")],
                    "issues": [{"item": 1, "field": "answers",
                                "problem": "was wrong"}]})
            return json.dumps({                        # base with a key
                "questions": [_mcq("Q1", "A")],
                "solutions": [_sol("Q1", "A", "original")]})

        with patch.object(vertex, "_call_vertex", side_effect=brain):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, count=1)
        draft = self.Draft.browse(draft_ids)
        self.assertEqual(len(draft), 1)
        self.assertEqual(json.loads(draft.solution_json), "B",
                         "critique correction must overwrite the answer key")

    def test_selfheal_failure_keeps_base_batch(self):
        """If a self-heal stage raises, the base batch still persists (best-effort
        layer, never sinks generation)."""
        prompt = self._sop()

        def brain(env, system_prompt, **kw):
            parts = kw.get("user_parts") or []
            text = " ".join(p.get("text", "") for p in parts
                            if isinstance(p, dict))
            if "already authored" in text or "FINALIZED" in text \
                    or "STRICT assessment reviewer" in text:
                raise RuntimeError("boom in self-heal")
            return json.dumps({"questions": [_mcq("Q1")],
                               "solutions": [_sol("Q1")]})

        with patch.object(vertex, "_call_vertex", side_effect=brain):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, count=4)
        self.assertGreaterEqual(len(draft_ids), 1,
                                "a self-heal failure must not lose the base batch")


@tagged("-at_install", "post_install")
class TestHarnessBankImport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Import = self.env["etp.assessment.pro.bank.import"]
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _run_payload(self):
        """A minimal authoring-harness output.json: questions (native fields
        shape) + solutions keyed by id."""
        return {
            "questions": [
                {"id": "q01", "instruction": "Which is prime?",
                 "covers_elements": ["e1"],
                 "fields": {"question_type": "mcq", "difficulty": "easy",
                            "options": ["2", "4"], "correct_answer": "2"},
                 "assets": {}},
                {"id": "q02", "instruction": "Explain idempotency.",
                 "fields": {"question_type": "subjective_rubric",
                            "difficulty": "hard",
                            "rubric": [{"label": "R", "pass_condition": "x"}]},
                 "assets": {"single": {"type": "image",
                                       "file": "assets/q02_single.png"}}},
            ],
            "solutions": {
                "q01": {"answers": "2", "rationale": "2 is prime"},
                "q02": {"answers": "A REST call is idempotent when...",
                        "rationale": "definition"},
            },
        }

    def test_import_harness_run_creates_drafts(self):
        res = self.Import.import_bank_harness(
            self._run_payload(), generator_name="Harness Test")
        self.assertEqual(res["questions_created"], 2)
        gen = self.Prompt.browse(res["generator_id"])
        self.assertEqual(gen.name, "Harness Test")
        drafts = self.Draft.search([("prompt_id", "=", gen.id)])
        self.assertEqual(len(drafts), 2)
        mcq = drafts.filtered(lambda d: d.question_type == "mcq")
        self.assertTrue(mcq.solution_json, "answer key must carry over")
        self.assertEqual(json.loads(mcq.options_json), ["2", "4"])

    def test_import_harness_local_asset_warns(self):
        """A local run-folder asset file (not http) is reported in warnings, not
        silently dropped."""
        res = self.Import.import_bank_harness(
            self._run_payload(), generator_name="Harness Assets")
        self.assertTrue(any("q02" in w and "local asset" in w
                            for w in res["warnings"]),
                        "local asset files must be surfaced for manual upload")

    def test_import_harness_bare_questions_array(self):
        """A bare questions.json array (no wrapping object) also imports."""
        payload = self._run_payload()["questions"]
        res = self.Import.import_bank_harness(payload, generator_name="Bare")
        self.assertEqual(res["questions_created"], 2)

    def test_import_harness_string_payload(self):
        res = self.Import.import_bank_harness(
            json.dumps(self._run_payload()), generator_name="From String")
        self.assertEqual(res["questions_created"], 2)

    def test_import_harness_empty_raises(self):
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.Import.import_bank_harness({"questions": []})
