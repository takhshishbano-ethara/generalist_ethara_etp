import json
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from ..controllers.main import GohanController
from .common import GohanTestCase


def _make_request_mock(env, body=b"", token=None):
    mock_req = MagicMock()
    mock_req.env = env
    mock_req.httprequest.data = body
    mock_req.httprequest.headers.get = MagicMock(return_value=token)
    return mock_req


@tagged("post_install", "-at_install", "gohan")
class TestWebhookAuth(GohanTestCase):

    def test_missing_icp_token_returns_401(self):
        self.ICP.set_param("gohan.webhook_token", "")
        ctrl = GohanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.headers.get.return_value = "any"
        with patch("odoo.addons.gohan.controllers.main.request", mock_req):
            result = ctrl.webhook_extraction_complete()
        self.assertEqual(result.status_code, 401)

    def test_wrong_token_returns_401(self):
        self.ICP.set_param("gohan.webhook_token", "secret")
        ctrl = GohanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.headers.get.return_value = "wrong"
        with patch("odoo.addons.gohan.controllers.main.request", mock_req):
            result = ctrl.webhook_extraction_complete()
        self.assertEqual(result.status_code, 401)


@tagged("post_install", "-at_install", "gohan")
class TestWebhookPayloadValidation(GohanTestCase):

    def _call(self, body):
        self.ICP.set_param("gohan.webhook_token", "secret")
        ctrl = GohanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.data = body
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch("odoo.addons.gohan.controllers.main.request", mock_req):
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


@tagged("post_install", "-at_install", "gohan")
class TestWebhookIdempotency(GohanTestCase):

    def _call(self, body):
        self.ICP.set_param("gohan.webhook_token", "secret")
        ctrl = GohanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.data = body
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch("odoo.addons.gohan.controllers.main.request", mock_req):
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


@tagged("post_install", "-at_install", "gohan")
class TestWebhookSuccessPath(GohanTestCase):

    def _call(self, body):
        self.ICP.set_param("gohan.webhook_token", "secret")
        ctrl = GohanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        mock_req.httprequest.data = body
        mock_req.httprequest.headers.get.return_value = "secret"
        with patch("odoo.addons.gohan.controllers.main.request", mock_req):
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


@tagged("post_install", "-at_install", "gohan")
class TestGetJobStatus(GohanTestCase):

    def test_unknown_returns_404(self):
        ctrl = GohanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        with patch("odoo.addons.gohan.controllers.main.request", mock_req):
            result = ctrl.get_job_status(job_id=99999999)
        self.assertEqual(result.status_code, 404)

    def test_success_returns_serialised_job(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done",
            score=85.0, grade="B", qc_verdict="shippable",
        )
        ctrl = GohanController()
        mock_req = MagicMock()
        mock_req.env = self.env
        with patch("odoo.addons.gohan.controllers.main.request", mock_req):
            result = ctrl.get_job_status(job_id=job.id)
        self.assertEqual(result.status_code, 200)
        body = json.loads(result.response[0])
        self.assertEqual(body["result"]["id"], job.id)
        self.assertEqual(body["result"]["state"], "done")
        self.assertEqual(body["result"]["grade"], "B")
        self.assertEqual(body["result"]["qc_verdict"], "shippable")
