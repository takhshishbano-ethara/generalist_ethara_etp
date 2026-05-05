# -*- coding: utf-8 -*-
import json
import subprocess
from unittest.mock import patch, MagicMock

from odoo.tests import tagged

from .common import KenseiTestCase

_GOG_MOD = "odoo.addons.kensei.controllers.gog_auth"


@tagged("post_install", "-at_install")
class TestGogStartAuth(KenseiTestCase):

    def _ctrl(self):
        from odoo.addons.kensei.controllers.gog_auth import KenseiGogAuthController
        return KenseiGogAuthController()

    def _task_with_gog(self):
        task = self._create_task(
            task_id="GOG-START-%s" % self.env["ir.sequence"].next_by_code("kensei.kensei") or "001",
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
class TestExtractClientSecret(KenseiTestCase):

    def _extract(self, raw):
        from odoo.addons.kensei.controllers.gog_auth import KenseiGogAuthController
        return KenseiGogAuthController._extract_client_secret(raw)

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
class TestGogExchangeToken(KenseiTestCase):

    def _ctrl(self):
        from odoo.addons.kensei.controllers.gog_auth import KenseiGogAuthController
        return KenseiGogAuthController()

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
class TestGogStatus(KenseiTestCase):

    def test_status_authenticated_from_db(self):
        from odoo.addons.kensei.controllers.gog_auth import KenseiGogAuthController

        self.task.sudo().write({
            "gog_auth_token": json.dumps({"tokens": {"token.json": "content"}}),
        })
        ctrl = KenseiGogAuthController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=self.task.id)
        self.assertTrue(result.get("authenticated"))


# ═══════════════════════════════════════════════════════════════════════
#  Error-path & edge-case tests (appended)
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestGogAuthErrorPaths(KenseiTestCase):

    def _ctrl(self):
        from odoo.addons.kensei.controllers.gog_auth import KenseiGogAuthController
        return KenseiGogAuthController()

    def _task_with_gog(self):
        task = self._create_task(
            task_id="GOG-ERR-%s" % (self.env["ir.sequence"].next_by_code("kensei.kensei") or "001"),
            email="user@example.com",
            password="keyring-pw",
            gog_auth=json.dumps({"installed": {"client_id": "abc"}}),
        )
        return task

    # ── start_auth ──────────────────────────────────────────────────

    def test_start_auth_nonexistent_task(self):
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=999999)
        self.assertIn("error", result)
        self.assertIn("Task not found", result["error"])

    @patch(_GOG_MOD + "._local_exec")
    def test_start_auth_setup_timeout(self, mock_exec):
        mock_exec.side_effect = subprocess.TimeoutExpired(cmd="gog", timeout=45)
        task = self._task_with_gog()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"].lower())

    @patch(_GOG_MOD + "._local_exec")
    def test_start_auth_no_auth_url_in_response(self, mock_exec):
        mock_exec.side_effect = [
            ("setup ok", "", 0),
            (json.dumps({"status": "ok"}), "", 0),
        ]
        task = self._task_with_gog()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.start_auth(task_id=task.id)
        self.assertIn("error", result)
        self.assertIn("auth_url", result["error"].lower())

    # ── exchange_token ──────────────────────────────────────────────

    def test_exchange_nonexistent_task(self):
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(task_id=999999, redirect_url="http://localhost?code=x")
        self.assertIn("error", result)
        self.assertIn("Task not found", result["error"])

    @patch(_GOG_MOD + "._local_exec")
    def test_exchange_missing_email(self, mock_exec):
        task = self._create_task(task_id="GOG-NOEMAIL-EXCH-001")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(task_id=task.id, redirect_url="http://localhost?code=x")
        self.assertIn("error", result)
        self.assertIn("email", result["error"].lower())

    @patch(_GOG_MOD + "._local_exec")
    def test_exchange_generic_exception(self, mock_exec):
        mock_exec.side_effect = RuntimeError("unexpected failure")
        task = self._create_task(task_id="GOG-EXCEPT-001", email="u@t.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(
                task_id=task.id, redirect_url="http://localhost?code=x",
            )
        self.assertIn("error", result)

    @patch(_GOG_MOD + "._local_exec")
    def test_exchange_multiple_config_files(self, mock_exec):
        file_output = (
            "---FILE:token.json\ndG9rZW4=\n---ENDFILE\n"
            "---FILE:credentials.json\nY3JlZA==\n---ENDFILE"
        )
        mock_exec.side_effect = [
            ("ok", "", 0),
            (file_output, "", 0),
        ]
        task = self._create_task(
            task_id="GOG-MULTI-001",
            email="user@test.com",
            gog_auth=json.dumps({"installed": {"client_id": "abc"}}),
        )
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.exchange_token(
                task_id=task.id, redirect_url="http://localhost?code=abc",
            )
        self.assertTrue(result.get("success"))
        task.invalidate_recordset()
        saved = json.loads(task.gog_auth_token)
        self.assertIn("tokens", saved)
        self.assertIn("token.json", saved["tokens"])
        self.assertIn("credentials.json", saved["tokens"])

    # ── status ──────────────────────────────────────────────────────

    @patch(_GOG_MOD + "._local_exec")
    def test_status_not_authenticated_empty_token(self, mock_exec):
        mock_exec.return_value = ("some output", "", 0)
        task = self._create_task(task_id="GOG-STAT-EMPTY-001", email="nobody@x.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=task.id)
        self.assertFalse(result.get("authenticated"))

    def test_status_invalid_json_in_token(self):
        task = self._create_task(task_id="GOG-STAT-BADJSON-001", email="a@b.com")
        task.sudo().write({"gog_auth_token": "NOT-VALID-JSON{{{"})
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch(_GOG_MOD + "._local_exec") as mock_exec:
                mock_exec.return_value = ("", "", 0)
                result = ctrl.gog_status(task_id=task.id)
        self.assertFalse(result.get("authenticated"))

    @patch(_GOG_MOD + "._local_exec")
    def test_status_gog_cli_shows_email(self, mock_exec):
        mock_exec.return_value = ("Accounts:\n  user@test.com (gmail,drive)\n", "", 0)
        task = self._create_task(task_id="GOG-STAT-EMAIL-001", email="user@test.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=task.id)
        self.assertTrue(result.get("authenticated"))

    @patch(_GOG_MOD + "._local_exec")
    def test_status_gog_cli_no_email(self, mock_exec):
        mock_exec.return_value = ("No accounts configured\n", "", 0)
        task = self._create_task(task_id="GOG-STAT-NOEML-001", email="missing@x.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=task.id)
        self.assertFalse(result.get("authenticated"))

    @patch(_GOG_MOD + "._local_exec")
    def test_status_gog_cli_exception(self, mock_exec):
        mock_exec.side_effect = RuntimeError("cli crashed")
        task = self._create_task(task_id="GOG-STAT-EXCEPT-001", email="a@b.com")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=task.id)
        self.assertFalse(result.get("authenticated"))

    def test_status_nonexistent_task(self):
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=999999)
        self.assertIn("error", result)
        self.assertIn("Task not found", result["error"])

    def test_status_missing_task_id(self):
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.gog_status(task_id=0)
        self.assertIn("error", result)
        self.assertIn("required", result["error"])


@tagged("post_install", "-at_install")
class TestExtractClientSecretErrorPaths(KenseiTestCase):

    def _extract(self, raw):
        from odoo.addons.kensei.controllers.gog_auth import KenseiGogAuthController
        return KenseiGogAuthController._extract_client_secret(raw)

    def test_extract_client_secret_non_dict(self):
        raw = json.dumps([1, 2, 3])
        secret, err = self._extract(raw)
        self.assertIsNone(secret)
        self.assertIn("error", err)
        self.assertIn("JSON object", err["error"])
