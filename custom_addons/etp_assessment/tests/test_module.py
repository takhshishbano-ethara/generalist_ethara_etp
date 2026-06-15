# -*- coding: utf-8 -*-
"""End-to-end module tests covering the parts test_batch_scoring.py doesn't.

Five test classes:
  TestAuthRouter       — _gemini_request picks the right endpoint per credential
  TestImageHardening   — _guess_mime + _fetch_image_b64 reject non-images
  TestEmailFallback    — no-email candidate gets a cancelled mail.mail with link
  TestEnqueueScoring   — _enqueue_subjective_scoring groups by evaluator
  TestQueueStats       — queue_stats never raises (returns error dict on failure)

Run with:
    src/odoo-bin -c src/odoo.conf -d <DB> \\
        --test-tags=etp_assessment_module --stop-after-init -u etp_assessment
"""
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase, tagged


@tagged("etp_assessment_module", "post_install", "-at_install")
class TestAuthRouter(TransactionCase):
    """Verify the 3-mode auth router in vertex_questions._gemini_request."""

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        # Clear every credential source so the test starts from a known
        # un-configured state. Without this, the live System Parameters
        # (e.g. a service account JSON) leak in and _gemini_request would
        # still find a credential, defeating the routing-isolation tests.
        for key in (
            "vertex_api_key", "vertex_access_token", "vertex_project_id",
            "vertex_location", "vertex_service_account_json",
            "vertex_service_account_filename", "vertex_minted_token",
            "vertex_minted_token_expires",
        ):
            self.ICP.set_param(f"etp_assessment.{key}", "")

    def test_aq_api_key_routes_to_aiplatform_express(self):
        from odoo.addons.etp_assessment.services.vertex_questions import (
            _gemini_request,
        )
        self.ICP.set_param("etp_assessment.vertex_api_key", "AQ.fake-test-key")
        url, headers = _gemini_request(self.env, "gemini-2.5-flash", "countTokens")
        self.assertIn("aiplatform.googleapis.com", url)
        self.assertIn("publishers/google/models/gemini-2.5-flash:countTokens", url)
        self.assertNotIn("projects/", url, "AQ. should be global publisher path")
        self.assertEqual(headers.get("x-goog-api-key"), "AQ.fake-test-key")
        self.assertNotIn("Authorization", headers)

    def test_aiza_api_key_routes_to_generativelanguage(self):
        from odoo.addons.etp_assessment.services.vertex_questions import (
            _gemini_request,
        )
        self.ICP.set_param("etp_assessment.vertex_api_key", "AIzaFAKE-key")
        url, headers = _gemini_request(self.env, "gemini-2.5-flash", "countTokens")
        self.assertIn("generativelanguage.googleapis.com", url)
        self.assertIn("v1beta/models/gemini-2.5-flash:countTokens", url)
        self.assertEqual(headers.get("x-goog-api-key"), "AIzaFAKE-key")

    def test_access_token_routes_to_project_scoped_aiplatform(self):
        from odoo.addons.etp_assessment.services.vertex_questions import (
            _gemini_request,
        )
        self.ICP.set_param("etp_assessment.vertex_access_token", "ya29.test-bearer")
        self.ICP.set_param("etp_assessment.vertex_project_id", "test-project")
        self.ICP.set_param("etp_assessment.vertex_location", "us-central1")
        url, headers = _gemini_request(self.env, "gemini-2.5-pro", "generateContent")
        self.assertIn("us-central1-aiplatform.googleapis.com", url)
        self.assertIn("projects/test-project/locations/us-central1", url)
        self.assertEqual(headers.get("Authorization"), "Bearer ya29.test-bearer")
        self.assertNotIn("x-goog-api-key", headers)

    def test_no_credentials_raises_actionable_error(self):
        from odoo.addons.etp_assessment.services.vertex_questions import (
            _gemini_request,
        )
        with self.assertRaisesRegex(ValueError, "Vertex/Gemini not configured"):
            _gemini_request(self.env, "gemini-2.5-flash", "countTokens")

    def test_placeholder_values_treated_as_unset(self):
        from odoo.addons.etp_assessment.services.vertex_questions import (
            _gemini_request,
        )
        self.ICP.set_param("etp_assessment.vertex_api_key",
                           "PLACEHOLDER_VERTEX_API_KEY")
        with self.assertRaisesRegex(ValueError, "Vertex/Gemini not configured"):
            _gemini_request(self.env, "gemini-2.5-flash", "countTokens")


@tagged("etp_assessment_module", "post_install", "-at_install")
class TestImageHardening(TransactionCase):
    """Verify _guess_mime + _fetch_image_b64 defend against bad inputs."""

    def test_guess_mime_recognizes_real_formats(self):
        from odoo.addons.etp_assessment.services.vertex_scoring import _guess_mime
        self.assertEqual(_guess_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(_guess_mime(b"\xff\xd8\xff\xe0..."), "image/jpeg")
        self.assertEqual(_guess_mime(b"GIF89a..."), "image/gif")
        self.assertEqual(
            _guess_mime(b"RIFF\x00\x00\x00\x00WEBPmore"), "image/webp")

    def test_guess_mime_returns_none_for_html(self):
        from odoo.addons.etp_assessment.services.vertex_scoring import _guess_mime
        html = b"<!DOCTYPE html><html><body>Error 404</body></html>"
        self.assertIsNone(_guess_mime(html),
                          "HTML body must NOT be classified as image/png")

    def test_guess_mime_returns_none_for_empty(self):
        from odoo.addons.etp_assessment.services.vertex_scoring import _guess_mime
        self.assertIsNone(_guess_mime(b""))
        self.assertIsNone(_guess_mime(None))

    def test_fetch_image_b64_rejects_html_content_type(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<!DOCTYPE html>..."
        mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = (
                mock_resp
            )
            b64, mime = vertex_scoring._fetch_image_b64(
                "https://example.com/not-an-image")
        self.assertIsNone(b64)
        self.assertIsNone(mime)

    def test_fetch_image_b64_rejects_image_content_type_with_bad_bytes(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"this is not really an image"
        mock_resp.headers = {"content-type": "image/png"}
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = (
                mock_resp
            )
            b64, mime = vertex_scoring._fetch_image_b64(
                "https://example.com/lies.png")
        self.assertIsNone(b64)
        self.assertIsNone(mime)

    def test_fetch_image_b64_accepts_real_png(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        import base64
        real_png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
            b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx"
            b"\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa6\x9c\x99/\x00\x00"
            b"\x00\x00IEND\xaeB`\x82"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = real_png_bytes
        mock_resp.headers = {"content-type": "image/png"}
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = (
                mock_resp
            )
            b64, mime = vertex_scoring._fetch_image_b64(
                "https://example.com/real.png")
        self.assertEqual(mime, "image/png")
        self.assertEqual(base64.b64decode(b64), real_png_bytes)


@tagged("etp_assessment_module", "post_install", "-at_install")
class TestEmailFallback(TransactionCase):
    """Verify the cancelled-mail-with-link path when candidate has no email."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        category = cls.env["etp.assessment.category"].create({"name": "EmailCat"})
        cls.question = cls.env["etp.assessment.question"].create({
            "name": "Q1", "question_type": "text",
            "prompt": "p", "category_id": category.id,
        })
        cls.employee_no_email = cls.env["hr.employee"].create({
            "name": "Candidate Without Email",
        })
        cls.assessment = cls.env["etp.assessment"].create({
            "name": "Email Test",
            "category_id": category.id,
            "question_limit": 1,
            "evaluator_ids": [(4, cls.employee_no_email.id)],
        })

    def test_no_email_candidate_creates_cancelled_mail_with_link(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "http://test.local")
        self.assessment.action_start()
        ev = self.assessment.assessment_evaluator_ids[:1]
        self.assertTrue(ev, "evaluator should be created even without email")
        msgs = self.env["mail.mail"].with_context(active_test=False).search([
            ("state", "=", "cancel"),
            ("failure_reason", "ilike", f"/assessment/{ev.access_token}"),
        ])
        self.assertEqual(len(msgs), 1)
        self.assertIn(ev.access_token, msgs.failure_reason)
        self.assertIn("Candidate Without Email", msgs.failure_reason)

    def test_email_candidate_does_not_get_cancelled_row(self):
        self.employee_no_email.work_email = "ok@local.test"
        self.assessment.action_start()
        cancelled = self.env["mail.mail"].with_context(active_test=False).search([
            ("state", "=", "cancel"),
            ("failure_reason", "ilike", "Candidate Without Email"),
        ])
        self.assertFalse(cancelled,
                         "candidate with email should follow normal send path")


@tagged("etp_assessment_module", "post_install", "-at_install")
class TestEnqueueScoring(TransactionCase):
    """_enqueue_subjective_scoring groups responses by evaluator and publishes
    one queue task per evaluator (not per response).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = cls.env["etp.assessment.category"].create(
            {"name": "EnqCat"})
        cls.questions = cls.env["etp.assessment.question"].create([
            {"name": f"Q{i}", "question_type": "text",
             "prompt": f"p{i}", "category_id": cls.category.id}
            for i in range(3)
        ])
        cls.employee = cls.env["hr.employee"].create({
            "name": "EnqCandidate", "work_email": "enq@test.local"})
        cls.assessment = cls.env["etp.assessment"].create({
            "name": "Enq", "category_id": cls.category.id,
            "evaluator_ids": [(4, cls.employee.id)],
        })

    def _make_evaluator_with_responses(self, n=3,
                                       justification="real answer"):
        ev = self.env["etp.assessment.evaluator"].create({
            "assessment_id": self.assessment.id,
            "employee_id": self.employee.id,
            "access_token": "tok-enq",
            "state": "submitted",
        })
        Response = self.env["etp.assessment.response"]
        responses = self.env["etp.assessment.response"]
        for q in self.questions[:n]:
            responses |= Response.create({
                "assessment_id": self.assessment.id,
                "assessment_evaluator_id": ev.id,
                "evaluator_id": self.employee.id,
                "question_id": q.id,
                "justification": justification,
                "state": "submitted",
                "llm_state": "pending",
            })
        return ev, responses

    def test_n_responses_publish_one_evaluator_message(self):
        ev, resps = self._make_evaluator_with_responses(n=3)
        from odoo.addons.etp_assessment.services import rabbitmq_service
        with patch.object(
            rabbitmq_service, "publish_evaluator_score_task"
        ) as mock_pub:
            resps._enqueue_subjective_scoring()
            self.assertEqual(
                mock_pub.call_count, 1,
                "Expected exactly ONE publish for ONE candidate's 3 responses",
            )
            mock_pub.assert_called_with(ev.id)

    def test_broker_failure_falls_back_to_pending(self):
        ev, resps = self._make_evaluator_with_responses(n=2)
        from odoo.addons.etp_assessment.services import rabbitmq_service
        with patch.object(
            rabbitmq_service, "publish_evaluator_score_task",
            side_effect=RuntimeError("simulated broker down"),
        ):
            resps._enqueue_subjective_scoring()
        for r in resps:
            self.assertEqual(r.llm_state, "pending",
                             "broker-down should drop to pending for cron drain")

    def test_response_without_justification_marked_not_needed(self):
        ev = self.env["etp.assessment.evaluator"].create({
            "assessment_id": self.assessment.id,
            "employee_id": self.employee.id,
            "access_token": "tok-empty",
            "state": "submitted",
        })
        r = self.env["etp.assessment.response"].create({
            "assessment_id": self.assessment.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": self.employee.id,
            "question_id": self.questions[0].id,
            "justification": "",
            "state": "submitted",
            "llm_state": "pending",
        })
        r._enqueue_subjective_scoring()
        self.assertEqual(r.llm_state, "not_needed")


@tagged("etp_assessment_module", "post_install", "-at_install")
class TestQueueStats(TransactionCase):
    """queue_stats must never raise — broker down returns error dict."""

    def test_queue_stats_returns_error_dict_when_broker_unreachable(self):
        from odoo.addons.etp_assessment.services import rabbitmq_service
        with patch.object(
            rabbitmq_service, "_get_channel",
            side_effect=ConnectionError("simulated broker down"),
        ):
            result = rabbitmq_service.queue_stats()
        self.assertIn("error", result)
        self.assertIn("simulated broker down", result["error"])
