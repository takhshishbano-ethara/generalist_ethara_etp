import os
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from ..controllers.cron_1min import VegetaCron1Min
from ..controllers.cron_5min import VegetaCron5Min
from .common import VegetaTestCase


def _mock_req(token="", env=None):
    req = MagicMock()
    req.httprequest.headers.get = MagicMock(return_value=token)
    if env is not None:
        req.env = env
    return req


@tagged("post_install", "-at_install", "vegeta")
class TestCron1MinAuth(VegetaTestCase):

    def test_missing_token_dispatch_returns_401(self):
        ctrl = VegetaCron1Min()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VEGETA_WEBHOOK_TOKEN", None)
            with patch("odoo.addons.vegeta.controllers.cron_1min.request", _mock_req("")):
                result = ctrl.trigger_dispatch()
        self.assertEqual(result.status_code, 401)

    def test_wrong_token_dispatch_returns_401(self):
        ctrl = VegetaCron1Min()
        with patch.dict(os.environ, {"VEGETA_WEBHOOK_TOKEN": "secret"}):
            with patch("odoo.addons.vegeta.controllers.cron_1min.request", _mock_req("wrong")):
                result = ctrl.trigger_dispatch()
        self.assertEqual(result.status_code, 401)

    def test_missing_token_reconcile_returns_401(self):
        ctrl = VegetaCron1Min()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VEGETA_WEBHOOK_TOKEN", None)
            with patch("odoo.addons.vegeta.controllers.cron_1min.request", _mock_req("")):
                result = ctrl.trigger_reconcile()
        self.assertEqual(result.status_code, 401)

    def test_wrong_token_reconcile_returns_401(self):
        ctrl = VegetaCron1Min()
        with patch.dict(os.environ, {"VEGETA_WEBHOOK_TOKEN": "secret"}):
            with patch("odoo.addons.vegeta.controllers.cron_1min.request", _mock_req("wrong")):
                result = ctrl.trigger_reconcile()
        self.assertEqual(result.status_code, 401)


@tagged("post_install", "-at_install", "vegeta")
class TestCron1MinDispatch(VegetaTestCase):

    def _call(self):
        self._set_param("vegeta.webhook_token", "secret")
        ctrl = VegetaCron1Min()
        req = _mock_req("secret", env=self.env)
        with patch("odoo.addons.vegeta.controllers.cron_1min.request", req):
            with patch.object(
                type(self.env["vegeta.job"]), "_cron_dispatch_prd_jobs", return_value=None
            ) as mock_cron:
                result = ctrl.trigger_dispatch()
        return result, mock_cron

    def test_dispatch_returns_200(self):
        result, _ = self._call()
        self.assertEqual(result.status_code, 200)

    def test_dispatch_calls_cron_method(self):
        _, mock_cron = self._call()
        mock_cron.assert_called_once()

    def test_dispatch_exception_returns_500(self):
        self._set_param("vegeta.webhook_token", "secret")
        ctrl = VegetaCron1Min()
        req = _mock_req("secret", env=self.env)
        with patch("odoo.addons.vegeta.controllers.cron_1min.request", req):
            with patch.object(
                type(self.env["vegeta.job"]),
                "_cron_dispatch_prd_jobs",
                side_effect=RuntimeError("boom"),
            ):
                result = ctrl.trigger_dispatch()
        self.assertEqual(result.status_code, 500)


@tagged("post_install", "-at_install", "vegeta")
class TestCron1MinReconcile(VegetaTestCase):

    def _call(self):
        self._set_param("vegeta.webhook_token", "secret")
        ctrl = VegetaCron1Min()
        req = _mock_req("secret", env=self.env)
        with patch("odoo.addons.vegeta.controllers.cron_1min.request", req):
            with patch.object(
                type(self.env["vegeta.job"]), "_cron_reconcile_prd_jobs", return_value=None
            ) as mock_cron:
                result = ctrl.trigger_reconcile()
        return result, mock_cron

    def test_reconcile_returns_200(self):
        result, _ = self._call()
        self.assertEqual(result.status_code, 200)

    def test_reconcile_calls_cron_method(self):
        _, mock_cron = self._call()
        mock_cron.assert_called_once()

    def test_reconcile_exception_returns_500(self):
        self._set_param("vegeta.webhook_token", "secret")
        ctrl = VegetaCron1Min()
        req = _mock_req("secret", env=self.env)
        with patch("odoo.addons.vegeta.controllers.cron_1min.request", req):
            with patch.object(
                type(self.env["vegeta.job"]),
                "_cron_reconcile_prd_jobs",
                side_effect=RuntimeError("boom"),
            ):
                result = ctrl.trigger_reconcile()
        self.assertEqual(result.status_code, 500)


@tagged("post_install", "-at_install", "vegeta")
class TestCron5MinAuth(VegetaTestCase):

    def test_missing_token_watchdog_returns_401(self):
        ctrl = VegetaCron5Min()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VEGETA_WEBHOOK_TOKEN", None)
            with patch("odoo.addons.vegeta.controllers.cron_5min.request", _mock_req("")):
                result = ctrl.trigger_watchdog()
        self.assertEqual(result.status_code, 401)

    def test_wrong_token_watchdog_returns_401(self):
        ctrl = VegetaCron5Min()
        with patch.dict(os.environ, {"VEGETA_WEBHOOK_TOKEN": "secret"}):
            with patch("odoo.addons.vegeta.controllers.cron_5min.request", _mock_req("wrong")):
                result = ctrl.trigger_watchdog()
        self.assertEqual(result.status_code, 401)


@tagged("post_install", "-at_install", "vegeta")
class TestCron5MinWatchdog(VegetaTestCase):

    def _call(self):
        self._set_param("vegeta.webhook_token", "secret")
        ctrl = VegetaCron5Min()
        req = _mock_req("secret", env=self.env)
        with patch("odoo.addons.vegeta.controllers.cron_5min.request", req):
            with patch.object(
                type(self.env["vegeta.job"]), "_cron_watchdog_stuck_jobs", return_value=None
            ) as mock_cron:
                result = ctrl.trigger_watchdog()
        return result, mock_cron

    def test_watchdog_returns_200(self):
        result, _ = self._call()
        self.assertEqual(result.status_code, 200)

    def test_watchdog_calls_cron_method(self):
        _, mock_cron = self._call()
        mock_cron.assert_called_once()

    def test_watchdog_exception_returns_500(self):
        self._set_param("vegeta.webhook_token", "secret")
        ctrl = VegetaCron5Min()
        req = _mock_req("secret", env=self.env)
        with patch("odoo.addons.vegeta.controllers.cron_5min.request", req):
            with patch.object(
                type(self.env["vegeta.job"]),
                "_cron_watchdog_stuck_jobs",
                side_effect=RuntimeError("boom"),
            ):
                result = ctrl.trigger_watchdog()
        self.assertEqual(result.status_code, 500)
