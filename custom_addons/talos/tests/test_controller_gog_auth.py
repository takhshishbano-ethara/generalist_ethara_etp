# -*- coding: utf-8 -*-
import json
import subprocess
from unittest.mock import patch, MagicMock

from odoo.tests import tagged

from .common import TalosTestCase

_GOG_MOD = "odoo.addons.talos.controllers.gog_auth"


@tagged("post_install", "-at_install")
class TestGogStartAuth(TalosTestCase):

    def _ctrl(self):
        from odoo.addons.talos.controllers.gog_auth import TalosGogAuthController
        return TalosGogAuthController()

    def _task_with_gog(self):
        task = self._create_task(
            task_id="GOG-START-%s" % self.env["ir.sequence"].next_by_code("talos.talos") or "001",
            email="user@example.com",
            password="keyring-pw",
            gog_auth=json.dumps({"installed": {"client_id": "abc"}}),
        )
        return task

    @patch(_GOG_MOD + "._local_exec")
    def test_start_auth_success(self, mock_exec):
        mock_exec.side_effect = [
            ("setup ok", "", 0),
            (json.dumps({"auth_url": "https://accounts.google.com/o/auth?x=1"}), "", 0),
        ]
        task = self._task_with_gog()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("auth_url", result)
        self.assertTrue(result["auth_url"].startswith("https://"))

    def test_start_auth_missing_task_id(self):
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=0)
        self.assertIn("error", result)

    @patch(_GOG_MOD + "._local_exec")
    def test_start_auth_no_email(self, mock_exec):
        task = self._create_task(
            task_id="GOG-NOEMAIL-001",
            gog_auth=json.dumps({"installed": {"client_id": "abc"}}),
        )
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)
        self.assertIn("email", result["error"].lower())

    def test_start_auth_no_gog_auth(self):
        task = self._create_task(task_id="GOG-NOAUTH-001", email="a@b.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)

    def test_start_auth_invalid_json_gog_auth(self):
        task = self._create_task(
            task_id="GOG-BADJSON-001",
            email="a@b.com",
            gog_auth="NOT VALID JSON{{{",
        )
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)
        self.assertIn("invalid JSON", result["error"])

    @patch(_GOG_MOD + "._local_exec")
    def test_start_auth_gog_cli_failure(self, mock_exec):
        mock_exec.side_effect = [
            ("", "", 0),
            ("", "auth failed", 1),
        ]
        task = self._task_with_gog()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)

    @patch(_GOG_MOD + "._local_exec")
    def test_start_auth_timeout(self, mock_exec):
        mock_exec.side_effect = [
            ("", "", 0),
            subprocess.TimeoutExpired(cmd="gog", timeout=45),
        ]
        task = self._task_with_gog()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])

    @patch(_GOG_MOD + "._local_exec")
    def test_start_auth_unparseable_output(self, mock_exec):
        mock_exec.side_effect = [
            ("", "", 0),
            ("not json at all", "", 0),
        ]
        task = self._task_with_gog()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)


@tagged("post_install", "-at_install")
class TestExtractClientSecret(TalosTestCase):

    def _extract(self, raw):
        from odoo.addons.talos.controllers.gog_auth import TalosGogAuthController
        return TalosGogAuthController._extract_client_secret(raw)

    def test_extract_installed_key(self):
        raw = json.dumps({"installed": {"client_id": "abc", "client_secret": "xyz"}})
        secret, err = self._extract(raw)
        self.assertIsNone(err)
        self.assertIn("installed", secret)

    def test_extract_web_key(self):
        raw = json.dumps({"web": {"client_id": "abc"}})
        secret, err = self._extract(raw)
        self.assertIsNone(err)
        self.assertIn("web", secret)

    def test_extract_client_secret_key(self):
        raw = json.dumps({"client_secret": {"client_id": "nested"}})
        secret, err = self._extract(raw)
        self.assertIsNone(err)
        parsed = json.loads(secret)
        self.assertEqual(parsed["client_id"], "nested")

    def test_extract_empty(self):
        secret, err = self._extract(None)
        self.assertIsNone(secret)
        self.assertIn("error", err)

    def test_extract_invalid_json(self):
        secret, err = self._extract("{not valid")
        self.assertIsNone(secret)
        self.assertIn("error", err)
        self.assertIn("invalid JSON", err["error"])

    def test_extract_no_recognized_key(self):
        raw = json.dumps({"unrelated": "data"})
        secret, err = self._extract(raw)
        self.assertIsNone(secret)
        self.assertIn("error", err)


@tagged("post_install", "-at_install")
class TestGogExchangeToken(TalosTestCase):

    def _ctrl(self):
        from odoo.addons.talos.controllers.gog_auth import TalosGogAuthController
        return TalosGogAuthController()

    @patch(_GOG_MOD + "._local_exec")
    def test_exchange_success(self, mock_exec):
        mock_exec.side_effect = [
            ("exchange ok", "", 0),
            ("---FILE:token.json\ndG9rZW4=\n---ENDFILE", "", 0),
        ]
        task = self._create_task(
            task_id="GOG-EXCH-001",
            email="user@test.com",
            gog_auth=json.dumps({"installed": {"client_id": "abc"}}),
        )
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(
                task_id=task.id,
                redirect_url="http://localhost?code=abc",
            )
        self.assertTrue(result.get("success"))
        task.invalidate_recordset()
        self.assertTrue(task.gog_auth_token)

    @patch(_GOG_MOD + "._local_exec")
    def test_exchange_missing_redirect_url(self, mock_exec):
        task = self._create_task(task_id="GOG-EXCH-002", email="u@t.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(task_id=task.id, redirect_url="")
        self.assertIn("error", result)

    @patch(_GOG_MOD + "._local_exec")
    def test_exchange_gog_failure(self, mock_exec):
        mock_exec.return_value = ("", "exchange error", 1)
        task = self._create_task(task_id="GOG-EXCH-003", email="u@t.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(
                task_id=task.id,
                redirect_url="http://localhost?code=x",
            )
        self.assertIn("error", result)

    @patch(_GOG_MOD + "._local_exec")
    def test_exchange_timeout(self, mock_exec):
        mock_exec.side_effect = subprocess.TimeoutExpired(cmd="gog", timeout=45)
        task = self._create_task(task_id="GOG-EXCH-004", email="u@t.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(
                task_id=task.id,
                redirect_url="http://localhost?code=x",
            )
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])


@tagged("post_install", "-at_install")
class TestGogStatus(TalosTestCase):

    def test_status_authenticated_from_db(self):
        from odoo.addons.talos.controllers.gog_auth import TalosGogAuthController

        self.task.sudo().write({
            "gog_auth_token": json.dumps({"tokens": {"token.json": "content"}}),
        })
        ctrl = TalosGogAuthController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=self.task.id)
        self.assertTrue(result.get("authenticated"))
