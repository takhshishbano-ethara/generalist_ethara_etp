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


@tagged("-at_install", "post_install")
class TestCsvImportRowCap(TransactionCase):
    """L-9: the CSV import path is bounded so a huge upload cannot create an
    unbounded number of draft records in one request."""

    def setUp(self):
        super().setUp()
        self.Wizard = self.env["etp.assessment.pro.bank.import.wizard"]

    def _csv(self, n_rows):
        header = "title,question_type,prompt,options,correct_answer\n"
        row = "Q%d,mcq,Pick,A|B|C,A\n"
        return (header + "".join(row % i for i in range(n_rows))).encode("utf-8")

    def test_row_cap_enforced(self):
        from odoo.exceptions import UserError
        wiz = self.Wizard.new({})
        over = self.Wizard._MAX_CSV_ROWS + 5
        with self.assertRaises(UserError):
            wiz._parse_csv(self._csv(over))

    def test_under_cap_parses(self):
        wiz = self.Wizard.new({})
        rows = wiz._parse_csv(self._csv(10))
        self.assertEqual(len(rows), 10)

    def test_oversize_bytes_rejected(self):
        from odoo.exceptions import UserError
        wiz = self.Wizard.new({})
        big = b"title,prompt\n" + b"x" * (wiz._MAX_CSV_BYTES + 1)
        with self.assertRaises(UserError):
            wiz._parse_csv(big)


@tagged("-at_install", "post_install")
class TestCandidateRosterCsvCap(TransactionCase):
    """L-9 (second path): the candidate-roster CSV import
    (action_import_candidates_csv) is bounded so a huge upload cannot create an
    unbounded number of hr.applicant records in one request."""

    def setUp(self):
        super().setUp()
        self.Assessment = self.env["etp.assessment.pro"]

    def _assessment(self, csv_bytes):
        import base64
        return self.Assessment.create({
            "name": "Roster cap test",
            "candidate_csv_file": base64.b64encode(csv_bytes),
        })

    def _roster_csv(self, n_rows):
        header = "name,email\n"
        row = "Cand %d,cand%d@example.com\n"
        return (header + "".join(row % (i, i) for i in range(n_rows))).encode()

    def test_roster_row_cap_enforced(self):
        from odoo.addons.etp_assessment_pro.models import assessment as amod
        from odoo.exceptions import UserError
        a = self._assessment(self._roster_csv(amod._MAX_CANDIDATE_CSV_ROWS + 5))
        with self.assertRaises(UserError):
            a.action_import_candidates_csv()

    def test_roster_oversize_bytes_rejected(self):
        from odoo.addons.etp_assessment_pro.models import assessment as amod
        from odoo.exceptions import UserError
        big = b"name,email\n" + b"x" * (amod._MAX_CANDIDATE_CSV_BYTES + 1)
        a = self._assessment(big)
        with self.assertRaises(UserError):
            a.action_import_candidates_csv()


@tagged("-at_install", "post_install")
class TestSecretStoreEncryption(TransactionCase):
    """M-8: secrets are encrypted at rest, round-trip correctly, and legacy
    plaintext stays readable (backward compatible)."""

    def test_encrypt_decrypt_roundtrip(self):
        from odoo.addons.etp_assessment_pro.services import secret_store
        plain = '{"private_key":"-----BEGIN PRIVATE KEY-----abc"}'
        enc = secret_store.encrypt(self.env, plain)
        self.assertTrue(secret_store.is_encrypted(enc))
        self.assertNotIn("private_key", enc)
        self.assertEqual(secret_store.decrypt(self.env, enc), plain)

    def test_legacy_plaintext_passes_through(self):
        from odoo.addons.etp_assessment_pro.services import secret_store
        # A value written before encryption existed has no marker: read as-is.
        self.assertEqual(
            secret_store.decrypt(self.env, "legacy-plain-secret"),
            "legacy-plain-secret")

    def test_empty_stays_empty(self):
        from odoo.addons.etp_assessment_pro.services import secret_store
        self.assertEqual(secret_store.encrypt(self.env, ""), "")
        self.assertEqual(secret_store.decrypt(self.env, ""), "")

    def test_get_set_secret_via_config(self):
        from odoo.addons.etp_assessment_pro.services import secret_store
        key = "etp_assessment_pro.s3_secret_key"
        secret_store.set_secret(self.env, key, "AKIA-secret-value")
        raw = self.env["ir.config_parameter"].sudo().get_param(key)
        self.assertTrue(secret_store.is_encrypted(raw))
        self.assertEqual(
            secret_store.get_secret(self.env, key), "AKIA-secret-value")


@tagged("-at_install", "post_install")
class TestCoerce100Boundary(TransactionCase):
    """M-12: a 0-1 judge score that overshoots 1.0 by a rounding hair is a PERFECT
    answer, not a ~0.1% one."""

    def test_overshoot_one_is_perfect(self):
        from odoo.addons.etp_assessment_pro.services.scoring import _coerce_100
        self.assertEqual(_coerce_100(1.001), 100.0)
        self.assertEqual(_coerce_100(1.0), 100.0)
        self.assertEqual(_coerce_100(0.5), 50.0)

    def test_genuine_100_scale_untouched(self):
        from odoo.addons.etp_assessment_pro.services.scoring import _coerce_100
        self.assertEqual(_coerce_100(87.0), 87.0)
        self.assertEqual(_coerce_100(1.5), 1.5)  # >1.01 -> genuine 0-100 value
        self.assertEqual(_coerce_100(150), 100.0)  # clamped


@tagged("-at_install", "post_install")
class TestScoringTrustBoundary(TransactionCase):
    """P2: a grader must not be able to hand the platform its own composition
    output. composed_raw_100 short-circuits the _recompute_v10 trust gate in
    _store_scored, so an LLM-supplied value would be honored verbatim. It (and
    the other platform-internal composition keys) must be stripped at parse."""

    def test_parse_results_strips_platform_composition_keys(self):
        from odoo.addons.etp_assessment_pro.services.scoring import _parse_results
        # A hostile/hallucinated grader response that tries to force a perfect
        # composed score plus fake audit sub-objects.
        payload = json.dumps({"results": [{
            "item_id": "1", "score": 5,
            "composed_raw_100": 100.0,
            "ab_scores": {"verdict_score": 1.0},
            "label_scores": {"coverage": 1.0},
            "recompute_note": "totally legit",
        }]})
        out = _parse_results(payload)
        self.assertEqual(len(out), 1)
        it = out[0]
        # The judge's real fields survive...
        self.assertEqual(it.get("item_id"), "1")
        self.assertEqual(it.get("score"), 5)
        # ...but every platform-internal composition key is gone, so the trust
        # recompute in _store_scored cannot be bypassed.
        for key in ("composed_raw_100", "ab_scores", "label_scores",
                    "recompute_note"):
            self.assertNotIn(
                key, it,
                "%s must be stripped from untrusted grader output" % key)

    def test_parse_results_keeps_legit_judge_fields(self):
        from odoo.addons.etp_assessment_pro.services.scoring import _parse_results
        # Fields the judge legitimately emits (ceiling triggers, verdicts) must
        # NOT be stripped - only the composition OUTPUTS are.
        payload = json.dumps([{
            "item_id": "9", "score": 72,
            "verdict_consistency": "consistent",
            "fabrication_count": 0, "flags": ["media_unseen"],
        }])
        it = _parse_results(payload)[0]
        self.assertEqual(it.get("verdict_consistency"), "consistent")
        self.assertEqual(it.get("fabrication_count"), 0)
        self.assertEqual(it.get("flags"), ["media_unseen"])


@tagged("-at_install", "post_install")
class TestExtractJsonArraySingleObject(TransactionCase):
    """P2: a lone JSON object response must not silently yield zero items. The
    primary parse branch returned the dict unguarded; callers iterate it with
    `[it for it in ... if isinstance(it, dict)]`, which iterates the dict's KEYS
    and filters everything out. A single object must become a one-item list."""

    def test_bare_object_becomes_single_item_list(self):
        from odoo.addons.etp_assessment_pro.services.vertex import (
            _extract_json_array)
        out = _extract_json_array('{"question_type": "mcq", "prompt": "Q?"}')
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].get("question_type"), "mcq")

    def test_wrapped_list_still_unwraps(self):
        from odoo.addons.etp_assessment_pro.services.vertex import (
            _extract_json_array)
        out = _extract_json_array('{"questions": [{"a": 1}, {"a": 2}]}')
        self.assertEqual([d.get("a") for d in out], [1, 2])

    def test_plain_array_untouched(self):
        from odoo.addons.etp_assessment_pro.services.vertex import (
            _extract_json_array)
        out = _extract_json_array('[{"a": 1}, {"a": 2}]')
        self.assertEqual([d.get("a") for d in out], [1, 2])
