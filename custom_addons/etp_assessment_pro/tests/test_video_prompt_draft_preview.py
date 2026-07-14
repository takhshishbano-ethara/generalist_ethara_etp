# -*- coding: utf-8 -*-
"""HTTP coverage for the admin video_prompt DRAFT clip-preview route
(/etp_assessment/admin_draft_qvideo/<draft_id>/<slot>). The route lets a backend
reviewer watch a staged reference/output clip before Approve; it must presign
private-S3 clips (302), gate on the internal user's draft read access
(portal/unauthed denied), and 404 gracefully when a slot has no staged clip. S3
is fully mocked — no real presign call."""
import base64
import json
from uuid import uuid4
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

_S3 = "odoo.addons.etp_assessment_pro.services.s3_service"
_FAKE_MP4 = base64.b64encode(
    b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 48).decode("ascii")


@tagged("-at_install", "post_install")
class TestVideoDraftPreviewRoute(HttpCase):
    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]
        self.mgr_login = "vp_mgr_%s@x.com" % uuid4().hex[:8]
        self.mgr_pwd = "mgrpass1"
        self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "VP Mgr", "login": self.mgr_login, "email": self.mgr_login,
            "password": self.mgr_pwd,
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref(
                    "etp_assessment_pro.group_assessment_manager").id])]})

    def _draft(self, files, state="rendered"):
        prompt = self.Prompt.create({"name": "Vid preview"})
        return self.Draft.create({
            "prompt_id": prompt.id,
            "name": "CGI clip draft",
            "question_prompt": "Write the transformation prompt.",
            "question_type": "video_prompt",
            "video_state": state,
            "video_files_json": json.dumps(files),
        })

    def _portal_user(self):
        login = "vp_portal_%s@x.com" % uuid4().hex[:8]
        pwd = "portalpass1"
        self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "VP Portal", "login": login, "email": login,
            "password": pwd,
            "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])]})
        return login, pwd

    def test_admin_route_redirects_to_presigned_for_s3_clip(self):
        draft = self._draft([
            {"slot": "reference", "label": "Reference",
             "url": "https://bucket.s3.amazonaws.com/qvideo/ref.mp4",
             "data": False}])
        self.authenticate(self.mgr_login, self.mgr_pwd)
        with patch("%s.object_key_from_url" % _S3,
                   return_value="qvideo/ref.mp4"), \
                patch("%s.presigned_url" % _S3,
                      return_value="https://signed.example/ref.mp4?sig=abc") \
                as pres:
            resp = self.url_open(
                "/etp_assessment/admin_draft_qvideo/%d/reference" % draft.id,
                allow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"),
                         "https://signed.example/ref.mp4?sig=abc")
        pres.assert_called()

    def test_admin_route_streams_base64_fallback(self):
        draft = self._draft([
            {"slot": "output", "label": "Output", "url": False,
             "data": "data:video/mp4;base64,%s" % _FAKE_MP4}])
        self.authenticate(self.mgr_login, self.mgr_pwd)
        resp = self.url_open(
            "/etp_assessment/admin_draft_qvideo/%d/output" % draft.id,
            allow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "video/mp4")
        self.assertEqual(resp.content, base64.b64decode(_FAKE_MP4))

    def test_admin_route_not_found_when_slot_unstaged(self):
        draft = self._draft([
            {"slot": "reference", "label": "Reference",
             "url": "https://bucket.s3.amazonaws.com/qvideo/ref.mp4"}])
        self.authenticate(self.mgr_login, self.mgr_pwd)
        resp = self.url_open(
            "/etp_assessment/admin_draft_qvideo/%d/output" % draft.id,
            allow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_admin_route_not_found_when_no_clips(self):
        draft = self._draft([], state="generating")
        self.authenticate(self.mgr_login, self.mgr_pwd)
        resp = self.url_open(
            "/etp_assessment/admin_draft_qvideo/%d/reference" % draft.id,
            allow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_portal_user_denied(self):
        draft = self._draft([
            {"slot": "reference", "label": "Reference",
             "url": "https://bucket.s3.amazonaws.com/qvideo/ref.mp4"}])
        login, pwd = self._portal_user()
        self.authenticate(login, pwd)
        with patch("%s.object_key_from_url" % _S3, return_value="qvideo/ref.mp4"), \
                patch("%s.presigned_url" % _S3,
                      return_value="https://signed.example/ref.mp4?sig=abc"):
            resp = self.url_open(
                "/etp_assessment/admin_draft_qvideo/%d/reference" % draft.id,
                allow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_not_served(self):
        draft = self._draft([
            {"slot": "reference", "label": "Reference",
             "url": "https://bucket.s3.amazonaws.com/qvideo/ref.mp4"}])
        resp = self.url_open(
            "/etp_assessment/admin_draft_qvideo/%d/reference" % draft.id)
        self.assertNotIn("signed.example", resp.url)
        self.assertIn("/web/login", resp.url)

    def test_preview_field_builds_players_only_for_video_prompt(self):
        draft = self._draft([
            {"slot": "reference", "label": "Reference",
             "url": "https://bucket.s3.amazonaws.com/qvideo/ref.mp4"},
            {"slot": "output", "label": "Output", "url": False,
             "data": "data:video/mp4;base64,%s" % _FAKE_MP4}])
        self.assertTrue(draft.has_video_clips)
        self.assertIn("<video", draft.video_preview)
        self.assertIn(
            "/etp_assessment/admin_draft_qvideo/%d/reference" % draft.id,
            draft.video_preview)
        self.assertIn(
            "/etp_assessment/admin_draft_qvideo/%d/output" % draft.id,
            draft.video_preview)
        draft.question_type = "image_prompt"
        draft.invalidate_recordset()
        self.assertFalse(draft.has_video_clips)
        self.assertFalse(draft.video_preview)
