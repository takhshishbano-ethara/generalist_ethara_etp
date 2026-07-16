# -*- coding: utf-8 -*-
"""Phase 2 of video_prompt: the GENERATION CONTRACT. SOP question-generation can
AUTHOR video_prompt drafts (two clip briefs + an ideal_prompt transformation key),
mirroring image_prompt's reference+output form. Assets stay upload-filled (Phase 3
adds Veo). Nothing renders here; Phase 2 only produces the DRAFT.

Covered here:
  * _build_image_draft_fields authors a video_prompt draft: video_brief_json with
    reference+output briefs (and single for a lone clip) + rubric_json ideal_prompt,
    and never image_brief_json;
  * _validate_question_item accepts a well-formed video_prompt and rejects one with
    no ideal_prompt or no videos;
  * generate_questions_from_sop forced to video_prompt persists video_prompt drafts
    in the pending state;
  * the forced directive + generic contracts note carry the two-clip transformation
    contract.
"""
import base64
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import vertex


_IDEAL = ("Slow the reference clip to half speed, split it into two scenes, and "
          "grade the whole thing to a warm teal; keep the ambient audio, add no "
          "dialogue.")


def _video_specs(single=False):
    if single:
        videos = [{"slot": "single", "label": "Clip",
                   "prompt": "A five second clip of a paper plane gliding across "
                             "a sunlit desk, soft ambient room tone, no speech."}]
    else:
        videos = [
            {"slot": "reference", "label": "Reference",
             "prompt": "A five second clip of a paper plane gliding across a "
                       "sunlit desk at normal speed, ambient room tone, no cuts."},
            {"slot": "output", "label": "Output",
             "prompt": "The same paper plane clip re-timed to ten seconds, split "
                       "into two scenes, graded warm teal, ambient tone kept, "
                       "no dialogue."},
        ]
    return {
        "videos": videos,
        "answer_key": {
            "ideal_prompt": _IDEAL,
            "mandatory_elements": ["half speed", "warm teal grade",
                                   "two scenes"],
            "penalty_rules": ["no added dialogue"],
            "scoring_guide": "Reward naming the re-time, the split, and the grade.",
        },
    }


def _video_item(single=False):
    return {
        "name": "Transform the clip",
        "question_type": "video_prompt",
        "prompt": "Write the prompt that turns the reference clip into the output "
                  "clip.",
        "difficulty": "medium",
        "image_specs": _video_specs(single=single),
    }


class _Base(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]


class TestVideoPromptPhase2Build(_Base):
    def test_build_two_clip_video_prompt_draft(self):
        vals = vertex._build_image_draft_fields(
            self.env, "video_prompt", _video_item())
        briefs = json.loads(vals["video_brief_json"])
        self.assertEqual([b["slot"] for b in briefs], ["reference", "output"])
        self.assertTrue(all(b["prompt"] for b in briefs))
        self.assertNotIn("image_brief_json", vals)
        key = json.loads(vals["rubric_json"])
        self.assertEqual(key["ideal_prompt"], _IDEAL)

    def test_build_single_clip_video_prompt_draft(self):
        vals = vertex._build_image_draft_fields(
            self.env, "video_prompt", _video_item(single=True))
        briefs = json.loads(vals["video_brief_json"])
        self.assertEqual([b["slot"] for b in briefs], ["single"])
        self.assertIn("ideal_prompt", json.loads(vals["rubric_json"]))
        self.assertNotIn("image_brief_json", vals)

    def test_build_reads_images_key_as_video_fallback(self):
        item = _video_item()
        item["image_specs"]["images"] = item["image_specs"].pop("videos")
        vals = vertex._build_image_draft_fields(
            self.env, "video_prompt", item)
        briefs = json.loads(vals["video_brief_json"])
        self.assertEqual([b["slot"] for b in briefs], ["reference", "output"])


class TestVideoPromptPhase2Validate(_Base):
    def test_valid_two_clip_and_single_clip(self):
        self.assertEqual(
            vertex._validate_question_item(_video_item(), "video_prompt"), [])
        self.assertEqual(
            vertex._validate_question_item(
                _video_item(single=True), "video_prompt"), [])

    def test_rejects_video_prompt_without_ideal_prompt(self):
        item = _video_item()
        item["image_specs"]["answer_key"].pop("ideal_prompt")
        errs = vertex._validate_question_item(item, "video_prompt")
        self.assertTrue(any("ideal_prompt" in e for e in errs))

    def test_rejects_video_prompt_without_videos(self):
        item = _video_item()
        item["image_specs"].pop("videos")
        errs = vertex._validate_question_item(item, "video_prompt")
        self.assertTrue(any("videos" in e for e in errs))


class TestVideoPromptPhase2SopGen(_Base):
    def _sop_prompt(self):
        prompt = self.Prompt.create({"name": "SOP Video"})
        self.env["etp.assessment.pro.prompt.resource"].create({
            "prompt_id": prompt.id, "name": "sop.pdf",
            "file": base64.b64encode(b"%PDF-1.4 fake"), "category": "sop"})
        return prompt

    def test_generate_from_sop_forced_video_prompt_persists_drafts(self):
        prompt = self._sop_prompt()
        payload = json.dumps([_video_item(), _video_item(single=True)])
        with patch.object(vertex, "_call_vertex", return_value=payload):
            draft_ids = vertex.generate_questions_from_sop(
                self.env, prompt, allowed_types=("video_prompt",))
        self.assertEqual(len(draft_ids), 2)
        drafts = self.Draft.browse(draft_ids)
        for d in drafts:
            self.assertEqual(d.question_type, "video_prompt")
            self.assertTrue(d.video_brief_json)
            self.assertFalse(d.image_brief_json)
            self.assertEqual(d.image_state, "pending")
            self.assertTrue(json.loads(d.rubric_json)["ideal_prompt"])


class TestVideoPromptPhase2Directive(_Base):
    def test_forced_directive_carries_two_clip_transformation_contract(self):
        directive = vertex._forced_type_directive("video_prompt", 3)
        self.assertIn("video_prompt", directive)
        self.assertIn("reference", directive)
        self.assertIn("output", directive)
        self.assertIn("ideal_prompt", directive)
        self.assertIn("TRANSFORMATION", directive)

    def test_contracts_note_includes_video_prompt(self):
        note = vertex._image_contracts_note()
        self.assertIn("video_prompt", note)
        self.assertIn("reference->output", note)

    def test_type_contract_names_transformation_checklist_elements(self):
        contract = vertex._image_type_contract("video_prompt")
        for token in ("STYLE", "SCENE", "AUDIO", "LENGTH", "DIALOGUE"):
            self.assertIn(token, contract)
