# -*- coding: utf-8 -*-
"""Phase 1 of video_prompt: upload path + portal <video> streaming + scoring
reuse, all offline. video_prompt is the video twin of image_prompt: a reference
clip + an output clip, the candidate writes ONE transformation prompt, graded
against ideal_prompt through the EXISTING image_prompt key/rubric (no new math).

Covered here:
  * the upload handler stores each clip as a reference/output question.video row
    (video_url when S3 ingests it, on-record Binary as the dev fallback);
  * the /pro_assessment/qvideo/<token>/<id> route 302-redirects to video_url and
    streams the Binary fallback when there is no url, under the exam-token guard;
  * scoring a video_prompt response reuses the image_prompt item (ideal_prompt +
    rubric) and produces a score;
  * _compute_scoring_kind / needs_llm treat video_prompt as subjective;
  * the exam page context exposes the videos for a video_prompt question.
"""
import base64
import json
import re
from unittest.mock import patch
from uuid import uuid4

from odoo.tests.common import HttpCase, tagged

from odoo.addons.etp_assessment_pro.services import vertex, scoring
from odoo.addons.etp_assessment_pro.tests.test_scoring_v6 import _ScoringBase


_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_FAKE_MP4_B64 = base64.b64encode(_FAKE_MP4).decode("ascii")


def _video_key(question_type="video_prompt"):
    return json.dumps({
        "ideal_prompt": "Slow the clip to half speed and grade to a warm teal.",
        "mandatory_elements": ["half speed", "teal grade"],
        "penalty_rules": ["no added text overlay"],
        "scoring_guide": "Award for naming the speed change and the colour grade.",
    })


class TestVideoPromptPhase1Scoring(_ScoringBase):
    def _video_question(self):
        return self.Question.create({
            "name": "Transform the clip",
            "prompt": "Write the prompt that turns the reference into the output.",
            "question_type": "video_prompt",
            "subjective_rubric_json": _video_key(),
        })

    def test_build_item_reuses_image_prompt_key_and_rubric(self):
        q = self._video_question()
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q,
                          justification="Halve the speed, warm teal grade.")
        item = scoring._build_item(resp)
        self.assertEqual(item["question_type"], "video_prompt")
        self.assertEqual(
            item["ideal_prompt"],
            "Slow the clip to half speed and grade to a warm teal.")
        self.assertEqual(item["candidate_text"],
                         "Halve the speed, warm teal grade.")
        self.assertIn("checklist", item["rubric"])
        self.assertTrue(any("half speed" in c
                            for c in item["rubric"]["checklist"]))

    def test_video_prompt_needs_llm_is_subjective(self):
        q = self._video_question()
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q,
                          justification="Halve the speed, warm teal grade.")
        self.assertTrue(resp.needs_llm)
        self.assertFalse(resp.has_objective)

    def test_video_prompt_blank_justification_not_needs_llm(self):
        q = self._video_question()
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="")
        self.assertFalse(resp.needs_llm)

    def test_video_prompt_scored_against_ideal_prompt(self):
        q = self._video_question()
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q,
                          justification="Halve the speed, warm teal grade.")
        self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.88, "passed": True, "rubric_source": "supplied",
            "rubric": {"checklist": ["half speed", "teal grade"],
                       "constraints": [], "pass_condition": "captures both"},
            "gate": "none", "reference_answer": "Slow to half speed...",
            "reasoning": "both mandatory elements named.",
            "verdict_consistency": "match", "feedback": "Accurate.", "flags": [],
        }])
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 88.0)
        self.assertTrue(resp.llm_passed)
        self.assertEqual(resp.llm_max_score, 1)


class TestVideoPromptPhase1Upload(_ScoringBase):
    def test_upload_two_clips_creates_reference_and_output_with_url(self):
        q = self.Question.create({
            "name": "Upload pair", "prompt": "Prompt it.",
            "question_type": "video_prompt"})
        urls = iter(["https://cdn.example.com/ref.mp4",
                     "https://cdn.example.com/out.mp4"])

        def _fake_ingest(env, url=None, data=None, key_hint="qimg",
                         content_type=None):
            self.assertEqual(content_type, "video/mp4")
            return next(urls), False

        target = "odoo.addons.etp_assessment_pro.services.image_ingest.ingest"
        with patch(target, side_effect=_fake_ingest):
            q.write({"upload_video": _FAKE_MP4_B64,
                     "upload_video_filename": "ref.mp4",
                     "upload_video_slot": "reference"})
            q.action_apply_uploaded_video()
            q.write({"upload_video": _FAKE_MP4_B64,
                     "upload_video_filename": "out.mp4",
                     "upload_video_slot": "output"})
            q.action_apply_uploaded_video()
        self.assertEqual(len(q.video_ids), 2)
        by_slot = {v.slot: v for v in q.video_ids}
        self.assertEqual(set(by_slot), {"reference", "output"})
        self.assertEqual(by_slot["reference"].video_url,
                         "https://cdn.example.com/ref.mp4")
        self.assertEqual(by_slot["output"].video_url,
                         "https://cdn.example.com/out.mp4")
        self.assertFalse(q.upload_video)

    def test_upload_binary_fallback_when_s3_unconfigured(self):
        q = self.Question.create({
            "name": "Upload dev", "prompt": "Prompt it.",
            "question_type": "video_prompt"})
        q.write({"upload_video": _FAKE_MP4_B64,
                 "upload_video_filename": "ref.mp4",
                 "upload_video_slot": "reference"})
        q.action_apply_uploaded_video()
        self.assertEqual(len(q.video_ids), 1)
        vid = q.video_ids
        self.assertFalse(vid.video_url)
        self.assertTrue(vid.video)
        self.assertEqual(vid.slot, "reference")

    def test_reupload_same_slot_replaces(self):
        q = self.Question.create({
            "name": "Replace", "prompt": "Prompt it.",
            "question_type": "video_prompt"})
        q.write({"upload_video": _FAKE_MP4_B64,
                 "upload_video_filename": "a.mp4",
                 "upload_video_slot": "reference"})
        q.action_apply_uploaded_video()
        first_id = q.video_ids.id
        q.write({"upload_video": _FAKE_MP4_B64,
                 "upload_video_filename": "b.mp4",
                 "upload_video_slot": "reference"})
        q.action_apply_uploaded_video()
        self.assertEqual(len(q.video_ids), 1)
        self.assertNotEqual(q.video_ids.id, first_id)

    def test_has_required_videos_gates_empty_video_prompt(self):
        q = self.Question.create({
            "name": "No clip", "prompt": "Prompt it.",
            "question_type": "video_prompt"})
        self.assertFalse(q._has_required_videos())
        self.env["etp.assessment.pro.question.video"].create({
            "question_id": q.id, "slot": "reference",
            "video_url": "https://cdn.example.com/ref.mp4"})
        q.invalidate_recordset()
        self.assertTrue(q._has_required_videos())


@tagged("-at_install", "post_install")
class TestVideoPromptPhase1Http(HttpCase):
    _CSRF_RE = re.compile(
        r'name="csrf_token"[^>]*value="([^"]+)"'
        r'|value="([^"]+)"[^>]*name="csrf_token"')

    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.Video = self.env["etp.assessment.pro.question.video"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Response = self.env["etp.assessment.pro.response"]

    def url_open(self, url, data=None, **kw):
        tok = getattr(self, "_csrf_tok", "")
        if data is not None:
            if not tok:
                base = re.sub(r'/(begin|submit|finish|violation|review)$', '',
                              (url or "").split("?")[0])
                self.url_open(base)
                tok = getattr(self, "_csrf_tok", "")
            if isinstance(data, dict) and tok:
                data = dict(data, csrf_token=tok)
            return super().url_open(url, data=data, **kw)
        resp = super().url_open(url, **kw)
        try:
            m = self._CSRF_RE.search(resp.text or "")
            if m:
                self._csrf_tok = m.group(1) or m.group(2)
        except Exception:
            pass
        return resp

    def _portal_candidate(self, name):
        slug = name.lower().replace(" ", "_")
        login = "%s_%s@x.com" % (slug, uuid4().hex[:8])
        pwd = "portalpass1"
        portal = self.env.ref("base.group_portal")
        user = self.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": name, "login": login, "email": login, "password": pwd,
                "group_ids": [(6, 0, [portal.id])]})
        applicant = self.env["hr.applicant"].create({
            "partner_name": name, "email_from": login,
            "partner_id": user.partner_id.id, "candidate_user_id": user.id})
        return applicant, login, pwd

    def _launched_video(self, name="VidCand", with_url=True):
        cat = self.env["etp.assessment.pro.prompt"].create(
            {"name": "VidCat_%s" % name})
        q = self.Question.create({
            "name": "VID_Q", "question_type": "video_prompt",
            "prompt": "Write the transformation prompt.",
            "difficulty": "medium", "generator_id": cat.id,
            "subjective_rubric_json": _video_key()})
        if with_url:
            ref_vals = {"question_id": q.id, "label": "Reference",
                        "slot": "reference",
                        "video_url": "https://cdn.example.com/ref.mp4"}
            out_vals = {"question_id": q.id, "label": "Output", "slot": "output",
                        "sequence": 20,
                        "video_url": "https://cdn.example.com/out.mp4"}
        else:
            ref_vals = {"question_id": q.id, "label": "Reference",
                        "slot": "reference",
                        "video": _FAKE_MP4_B64, "video_filename": "ref.mp4"}
            out_vals = {"question_id": q.id, "label": "Output", "slot": "output",
                        "sequence": 20,
                        "video": _FAKE_MP4_B64, "video_filename": "out.mp4"}
        ref = self.Video.create(ref_vals)
        out = self.Video.create(out_vals)
        applicant, login, pwd = self._portal_candidate(name)
        a = self.Assessment.create({
            "name": "VidAssess", "generator_id": cat.id, "question_limit": 0,
            "duration_minutes": 30, "evaluator_ids": [(6, 0, [applicant.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        return ev, q, ref, out, login, pwd

    def test_qvideo_redirects_to_url(self):
        ev, _q, ref, _out, login, pwd = self._launched_video(name="VidUrl")
        token = ev.access_token
        self.authenticate(login, pwd)
        resp = self.url_open(
            "/pro_assessment/qvideo/%s/%d" % (token, ref.id),
            allow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"),
                         "https://cdn.example.com/ref.mp4")

    def test_qvideo_streams_binary_fallback(self):
        ev, _q, ref, _out, login, pwd = self._launched_video(
            name="VidBin", with_url=False)
        token = ev.access_token
        self.authenticate(login, pwd)
        resp = self.url_open("/pro_assessment/qvideo/%s/%d" % (token, ref.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content, "binary fallback must return bytes")
        self.assertTrue(
            resp.headers.get("Content-Type", "").startswith("video"))

    def test_qvideo_bogus_id_is_404(self):
        ev, _q, ref, _out, login, pwd = self._launched_video(name="VidBogus")
        token = ev.access_token
        self.authenticate(login, pwd)
        resp = self.url_open(
            "/pro_assessment/qvideo/%s/%d" % (token, ref.id + 999999))
        self.assertEqual(resp.status_code, 404)

    def test_qvideo_video_of_unassigned_question_is_404(self):
        ev, _q, _ref, _out, login, pwd = self._launched_video(name="VidGuard")
        token = ev.access_token
        other_cat = self.env["etp.assessment.pro.prompt"].create(
            {"name": "OtherCat"})
        other_q = self.Question.create({
            "name": "OTHER", "question_type": "video_prompt",
            "prompt": "x", "generator_id": other_cat.id})
        other_vid = self.Video.create({
            "question_id": other_q.id, "slot": "reference",
            "video_url": "https://cdn.example.com/other.mp4"})
        self.authenticate(login, pwd)
        resp = self.url_open(
            "/pro_assessment/qvideo/%s/%d" % (token, other_vid.id),
            allow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_exam_page_context_exposes_videos(self):
        ev, _q, ref, out, login, pwd = self._launched_video(name="VidRender")
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        resp = self.url_open("/pro_assessment/%s?q=1" % token)
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn(
            "/pro_assessment/qvideo/%s/%d" % (token, ref.id), html,
            "the reference clip must be streamed via the token video route")
        self.assertIn("/pro_assessment/qvideo/%s/%d" % (token, out.id), html)
        self.assertIn("<video", html)
        self.assertIn('name="justification"', html)
        self.assertIn("Your Prompt", html)

    def test_video_prompt_records_prompt_answer(self):
        ev, q, _ref, _out, login, pwd = self._launched_video(name="VidAnswer")
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q.id),
                  "justification": "Halve the speed and grade to warm teal."})
        self.assertIn(resp.status_code, (200, 303))
        r = self.Response.search([("assessment_evaluator_id", "=", ev.id),
                                  ("question_id", "=", q.id)])
        self.assertEqual(len(r), 1)
        self.assertEqual(r.state, "submitted")
        self.assertTrue(r.needs_llm)
        self.assertEqual(r.llm_state, "pending")
