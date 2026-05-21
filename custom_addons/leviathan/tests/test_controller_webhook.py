import json
import os
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from ..controllers.main import LeviathanController
from .common import LeviathanTestCase


def _make_request_mock(env, body=b"", token=None):
    mock_req = MagicMock()
    mock_req.env = env
    mock_req.httprequest.data = body
    mock_req.httprequest.headers.get = MagicMock(return_value=token)
    return mock_req


@tagged("post_install", "-at_install", "leviathan")
class TestWebhookAuth(LeviathanTestCase):

    def test_missing_env_token_returns_401(self):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.httprequest.headers.get.return_value = "any"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LEVIATHAN_WEBHOOK_TOKEN", None)
            with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                result = ctrl.webhook_extraction_complete()
        self.assertEqual(result.status_code, 401)

    def test_wrong_token_returns_401(self):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.httprequest.headers.get.return_value = "wrong"
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "secret"}):
            with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                result = ctrl.webhook_extraction_complete()
        self.assertEqual(result.status_code, 401)


@tagged("post_install", "-at_install", "leviathan")
class TestWebhookPayloadValidation(LeviathanTestCase):

    def _call(self, body):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.data = body
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "secret"}):
            with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                return ctrl.webhook_extraction_complete()

    def test_invalid_json_returns_400(self):
        result = self._call(b"not json")
        self.assertEqual(result.status_code, 400)

    def test_missing_job_id_returns_400(self):
        result = self._call(b'{"success": true}')
        self.assertEqual(result.status_code, 400)

    def test_unknown_job_returns_404(self):
        result = self._call(b'{"job_id": 999999999, "success": true}')
        self.assertEqual(result.status_code, 404)


@tagged("post_install", "-at_install", "leviathan")
class TestWebhookIdempotency(LeviathanTestCase):

    def _call(self, body):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.data = body
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "secret"}):
            with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                return ctrl.webhook_extraction_complete()

    def test_started_ping_updates_heartbeat(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        body = json.dumps({"job_id": job.id, "status": "started"}).encode()
        result = self._call(body)
        self.assertEqual(result.status_code, 200)
        job.invalidate_recordset()
        self.assertTrue(job.last_heartbeat)

    def test_callback_on_non_extracting_state_ignored(self):
        job = self._create_job(user_id=self.tasker.id, state="done")
        body = json.dumps({"job_id": job.id, "success": True}).encode()
        result = self._call(body)
        self.assertEqual(result.status_code, 200)
        parsed = json.loads(result.response[0])
        self.assertTrue(parsed.get("ignored"))

    def test_cancel_requested_short_circuits(self):
        job = self._create_job(
            user_id=self.tasker.id, state="extracting", cancel_requested=True,
        )
        body = json.dumps({"job_id": job.id, "success": True}).encode()
        result = self._call(body)
        self.assertEqual(result.status_code, 200)
        parsed = json.loads(result.response[0])
        self.assertTrue(parsed.get("ignored"))


@tagged("post_install", "-at_install", "leviathan")
class TestWebhookSuccessPath(LeviathanTestCase):

    def _call(self, body):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.data = body
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "secret"}):
            with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                with self._patch_submit_bg():
                    return ctrl.webhook_extraction_complete()

    def test_success_writes_fields_and_advances_state(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        body = json.dumps({
            "job_id": job.id,
            "success": True,
            "prd_prompt": "extracted data",
            "site_discovery": {
                "title": "Example",
                "tech_stack": {"react": {}},
                "pages": ["/", "/about"],
            },
            "screenshot_keys": ["a/1.png"],
            "asset_keys": ["a/logo.svg"],
        }).encode()
        result = self._call(body)
        self.assertEqual(result.status_code, 200)
        job.invalidate_recordset()
        self.assertEqual(job.state, "generating")
        self.assertEqual(job.prd_prompt, "extracted data")
        self.assertEqual(job.site_name, "Example")
        self.assertEqual(job.page_count, 2)
        self.assertEqual(job.screenshot_keys, ["a/1.png"])
        self.assertEqual(job.asset_keys, ["a/logo.svg"])
        self.assertTrue(job.lambda_callback_json)

    def test_partial_extraction_surfaces_warnings(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        body = json.dumps({
            "job_id": job.id,
            "success": True,
            "prd_prompt": "extracted",
            "partial": True,
            "warnings": ["Some pages skipped due to deadline"],
        }).encode()
        result = self._call(body)
        self.assertEqual(result.status_code, 200)
        job.invalidate_recordset()
        self.assertEqual(job.state, "generating")
        self.assertIn("deadline", job.extraction_warnings)
        self.assertFalse(job.error_message)

    def test_failure_marks_job_failed(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        body = json.dumps({
            "job_id": job.id,
            "success": False,
            "error": "Playwright crashed",
        }).encode()
        result = self._call(body)
        self.assertEqual(result.status_code, 200)
        job.invalidate_recordset()
        self.assertEqual(job.state, "failed")
        self.assertIn("Playwright", job.error_message)


@tagged("post_install", "-at_install", "leviathan")
class TestGetJobStatus(LeviathanTestCase):

    def test_unknown_returns_404(self):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
            result = ctrl.get_job_status(job_id=99999999)
        self.assertEqual(result.status_code, 404)

    def test_success_returns_serialised_job(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done",
            score=85.0, grade="B", qc_verdict="shippable",
        )
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
            result = ctrl.get_job_status(job_id=job.id)
        self.assertEqual(result.status_code, 200)
        body = json.loads(result.response[0])
        self.assertEqual(body["result"]["id"], job.id)
        self.assertEqual(body["result"]["state"], "done")
        self.assertEqual(body["result"]["grade"], "B")
        self.assertEqual(body["result"]["qc_verdict"], "shippable")


@tagged("post_install", "-at_install", "leviathan")
class TestWebhookHardening(LeviathanTestCase):
    """Webhook hardening added in 19.0.3.0.0:
    - Content-Length cap returns 413 before parsing/processing
    - LEVIATHAN_WEBHOOK_TOKEN accepts comma-separated list for zero-downtime rotation
    """

    def test_oversized_content_length_returns_413(self):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        # Declared length > cap, body irrelevant — must reject before parsing
        mock_req.httprequest.content_length = 50 * 1024 * 1024  # 50 MB declared
        mock_req.httprequest.data = b'{"job_id": 1}'
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "secret"}):
            with patch(
                "odoo.addons.leviathan.controllers.main._get_webhook_max_bytes",
                return_value=10 * 1024 * 1024,
            ):
                with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                    result = ctrl.webhook_extraction_complete()
        self.assertEqual(result.status_code, 413)

    def test_oversized_body_returns_413_even_if_content_length_lies(self):
        ctrl = LeviathanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        # Liar: tiny content_length but actual body is large
        mock_req.httprequest.content_length = 100
        mock_req.httprequest.data = b"x" * (20 * 1024 * 1024)  # 20 MB
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "secret"}):
            with patch(
                "odoo.addons.leviathan.controllers.main._get_webhook_max_bytes",
                return_value=10 * 1024 * 1024,
            ):
                with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                    result = ctrl.webhook_extraction_complete()
        self.assertEqual(result.status_code, 413)

    def test_token_rotation_accepts_any_in_comma_list(self):
        from odoo.addons.leviathan.controllers.main import _verify_webhook_token

        mock_req = MagicMock()
        # All three tokens in the env list must pass during rotation window
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "old,current,new"}):
            for tok in ("old", "current", "new"):
                mock_req.httprequest.headers.get = MagicMock(return_value=tok)
                with patch(
                    "odoo.addons.leviathan.controllers.main.request", mock_req,
                ):
                    self.assertTrue(
                        _verify_webhook_token(), f"token '{tok}' must be accepted",
                    )

    def test_token_rotation_rejects_unknown(self):
        from odoo.addons.leviathan.controllers.main import _verify_webhook_token

        mock_req = MagicMock()
        mock_req.httprequest.headers.get = MagicMock(return_value="stranger")
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "old,current,new"}):
            with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                self.assertFalse(_verify_webhook_token())

    def test_single_token_still_works(self):
        # Backwards compat: comma-list of 1 (i.e. no comma) is the old behaviour
        from odoo.addons.leviathan.controllers.main import _verify_webhook_token

        mock_req = MagicMock()
        mock_req.httprequest.headers.get = MagicMock(return_value="only")
        with patch.dict(os.environ, {"LEVIATHAN_WEBHOOK_TOKEN": "only"}):
            with patch("odoo.addons.leviathan.controllers.main.request", mock_req):
                self.assertTrue(_verify_webhook_token())
