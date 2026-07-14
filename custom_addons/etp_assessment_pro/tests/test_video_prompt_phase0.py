from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.constants import (
    MEDIUM_CODES, QUESTION_TYPE_CODES, VIDEO_QUESTION_TYPES,
)


class TestVideoPromptPhase0(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.Video = self.env["etp.assessment.pro.question.video"]

    def test_video_prompt_is_a_valid_question_type(self):
        self.assertIn("video_prompt", QUESTION_TYPE_CODES)
        self.assertIn("video_prompt", VIDEO_QUESTION_TYPES)

    def test_medium_selection_has_video(self):
        self.assertIn("video", MEDIUM_CODES)

    def test_video_record_creates_and_links_to_question(self):
        q = self.Question.create({
            "name": "Transform the clip",
            "prompt": "Write the prompt that turns the reference into the output.",
            "question_type": "video_prompt"})
        ref = self.Video.create({
            "question_id": q.id, "label": "Reference", "slot": "reference",
            "video_url": "https://cdn.example.com/ref.mp4"})
        out = self.Video.create({
            "question_id": q.id, "label": "Output", "slot": "output",
            "video_url": "https://cdn.example.com/out.mp4", "sequence": 20})
        self.assertEqual(q.video_ids, ref | out)
        self.assertEqual(q.video_ids[0].slot, "reference")
        self.assertEqual(ref.display_name, "Reference")

    def test_slot_defaults_to_single(self):
        q = self.Question.create({
            "name": "Lone clip", "prompt": "Describe.",
            "question_type": "video_prompt"})
        vid = self.Video.create({"question_id": q.id})
        self.assertEqual(vid.slot, "single")
        self.assertFalse(q._has_required_images() is False)
