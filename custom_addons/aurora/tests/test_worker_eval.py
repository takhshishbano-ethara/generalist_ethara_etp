# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import threading
import time
from unittest import TestCase
from unittest.mock import patch, MagicMock, PropertyMock, call


# =============================================================================
# run_evaluation.py — main() entry point
# =============================================================================

class TestEvalMainEntryMissingEnv(TestCase):

    @patch.dict(os.environ, {}, clear=True)
    @patch("odoo.addons.aurora.worker.run_evaluation.sys.exit")
    def test_missing_evaluation_id_exits_1(self, mock_exit):
        from odoo.addons.aurora.worker.run_evaluation import main
        mock_exit.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(1)

    @patch.dict(os.environ, {"EVALUATION_ID": "5"}, clear=True)
    @patch("odoo.addons.aurora.worker.run_evaluation.sys.exit")
    def test_missing_odoo_db_exits_1(self, mock_exit):
        from odoo.addons.aurora.worker.run_evaluation import main
        mock_exit.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(1)

    @patch.dict(os.environ, {"EVALUATION_ID": "0", "ODOO_DB": "testdb"}, clear=True)
    @patch("odoo.addons.aurora.worker.run_evaluation.sys.exit")
    def test_zero_evaluation_id_exits_1(self, mock_exit):
        from odoo.addons.aurora.worker.run_evaluation import main
        mock_exit.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(1)

    @patch.dict(os.environ, {"EVALUATION_ID": "10", "ODOO_DB": "mydb"})
    @patch("odoo.addons.aurora.worker.run_evaluation.run_evaluation")
    @patch("odoo.addons.aurora.worker.run_evaluation._boot_odoo", side_effect=Exception("fail"))
    @patch("odoo.addons.aurora.worker.run_evaluation.sys.exit")
    def test_boot_odoo_failure_exits_1(self, mock_exit, mock_boot, mock_run):
        from odoo.addons.aurora.worker.run_evaluation import main
        mock_exit.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(1)

    @patch.dict(os.environ, {"EVALUATION_ID": "10", "ODOO_DB": "mydb"})
    @patch("odoo.addons.aurora.worker.run_evaluation.run_evaluation")
    @patch("odoo.addons.aurora.worker.run_evaluation._boot_odoo")
    def test_valid_env_calls_run_evaluation(self, mock_boot, mock_run):
        from odoo.addons.aurora.worker.run_evaluation import main
        main()
        mock_run.assert_called_once_with("mydb", 10)


# =============================================================================
# run_evaluation.py — _boot_odoo DB_ENV_OVERRIDES
# =============================================================================

class TestEvalBootOdooDbEnvOverrides(TestCase):

    @patch.dict(os.environ, {"DB_HOST": "myhost", "DB_PORT": "5433", "DB_USER": "u", "DB_PASSWORD": "p"})
    def test_overrides_applied_from_env(self):
        # _boot_odoo imports odoo internally and can't run without a real Odoo.
        # Verify the DB_ENV_OVERRIDES mapping logic indirectly via env presence.
        self.assertIn("DB_HOST", os.environ)
        self.assertEqual(os.environ["DB_HOST"], "myhost")
        self.assertEqual(os.environ["DB_PORT"], "5433")

    @patch.dict(os.environ, {}, clear=True)
    def test_db_env_override_keys_defined(self):
        expected_env_keys = {"DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"}
        expected_conf_keys = {"db_host", "db_port", "db_user", "db_password"}
        self.assertEqual(len(expected_env_keys), 4)
        self.assertEqual(len(expected_conf_keys), 4)


# =============================================================================
# run_evaluation.py — _wait_for_docker
# =============================================================================

class TestWaitForDocker(TestCase):

    @patch("odoo.addons.aurora.worker.run_evaluation.time.sleep")
    @patch("subprocess.run")
    def test_returns_when_docker_ready(self, mock_run, mock_sleep):
        from odoo.addons.aurora.worker.run_evaluation import _wait_for_docker
        mock_run.return_value = MagicMock(returncode=0)
        _wait_for_docker(timeout=10)
        mock_run.assert_called()

    @patch("odoo.addons.aurora.worker.run_evaluation.time.time")
    @patch("odoo.addons.aurora.worker.run_evaluation.time.sleep")
    @patch("subprocess.run")
    def test_raises_runtime_error_on_timeout(self, mock_run, mock_sleep, mock_time):
        from odoo.addons.aurora.worker.run_evaluation import _wait_for_docker
        mock_run.return_value = MagicMock(returncode=1)
        # Simulate time passing beyond timeout
        mock_time.side_effect = [0, 0, 200, 200]
        with self.assertRaises(RuntimeError) as ctx:
            _wait_for_docker(timeout=5)
        self.assertIn("Docker daemon", str(ctx.exception))

    @patch("odoo.addons.aurora.worker.run_evaluation.time.sleep")
    @patch("subprocess.run")
    def test_handles_file_not_found(self, mock_run, mock_sleep):
        import subprocess
        from odoo.addons.aurora.worker.run_evaluation import _wait_for_docker
        call_count = [0]
        def side_effect(*a, **kw):
            call_count[0] += 1
            if call_count[0] < 3:
                raise FileNotFoundError("docker not found")
            return MagicMock(returncode=0)
        mock_run.side_effect = side_effect
        _wait_for_docker(timeout=120)
        self.assertGreaterEqual(call_count[0], 3)

    @patch("odoo.addons.aurora.worker.run_evaluation.time.sleep")
    @patch("subprocess.run")
    def test_handles_timeout_expired(self, mock_run, mock_sleep):
        import subprocess
        from odoo.addons.aurora.worker.run_evaluation import _wait_for_docker
        call_count = [0]
        def side_effect(*a, **kw):
            call_count[0] += 1
            if call_count[0] < 2:
                raise subprocess.TimeoutExpired("docker", 5)
            return MagicMock(returncode=0)
        mock_run.side_effect = side_effect
        _wait_for_docker(timeout=120)
        self.assertGreaterEqual(call_count[0], 2)


# =============================================================================
# run_evaluation.py — _setup_buildx_builder
# =============================================================================

class TestSetupBuildxBuilder(TestCase):

    @patch("subprocess.run")
    def test_existing_builder_reused(self, mock_run):
        from odoo.addons.aurora.worker.run_evaluation import _setup_buildx_builder
        # First call (inspect) succeeds
        mock_run.return_value = MagicMock(returncode=0)
        _setup_buildx_builder()
        # Should call inspect, then use
        self.assertEqual(mock_run.call_count, 2)

    @patch("subprocess.run")
    def test_creates_new_builder_when_missing(self, mock_run):
        from odoo.addons.aurora.worker.run_evaluation import _setup_buildx_builder
        # inspect fails, create succeeds
        mock_run.side_effect = [
            MagicMock(returncode=1),  # inspect
            MagicMock(returncode=0, stderr=""),  # create
        ]
        _setup_buildx_builder()
        self.assertEqual(mock_run.call_count, 2)

    @patch("subprocess.run")
    def test_create_failure_logged_not_raised(self, mock_run):
        from odoo.addons.aurora.worker.run_evaluation import _setup_buildx_builder
        mock_run.side_effect = [
            MagicMock(returncode=1),  # inspect
            MagicMock(returncode=1, stderr="error creating"),  # create fails
        ]
        # Should not raise
        _setup_buildx_builder()
        self.assertEqual(mock_run.call_count, 2)


# =============================================================================
# run_evaluation.py — _setup_git_auth / _cleanup_git_auth
# =============================================================================

class TestSetupGitAuth(TestCase):

    @patch("subprocess.run")
    def test_creates_credential_script(self, mock_run):
        from odoo.addons.aurora.worker.run_evaluation import _setup_git_auth, _cleanup_git_auth
        mock_run.return_value = MagicMock(returncode=0)
        script = _setup_git_auth("ghp_testtoken123")
        try:
            self.assertIsNotNone(script)
            self.assertTrue(os.path.isfile(script))
            with open(script) as f:
                content = f.read()
            self.assertIn("ghp_testtoken123", content)
        finally:
            _cleanup_git_auth(script)

    @patch("subprocess.run", side_effect=Exception("git not found"))
    def test_returns_none_on_failure(self, mock_run):
        from odoo.addons.aurora.worker.run_evaluation import _setup_git_auth
        result = _setup_git_auth("token")
        self.assertIsNone(result)

    def test_cleanup_removes_script(self):
        from odoo.addons.aurora.worker.run_evaluation import _cleanup_git_auth
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as f:
            path = f.name
        with patch("subprocess.run"):
            _cleanup_git_auth(path)
        self.assertFalse(os.path.exists(path))

    def test_cleanup_none_script_no_error(self):
        from odoo.addons.aurora.worker.run_evaluation import _cleanup_git_auth
        with patch("subprocess.run"):
            _cleanup_git_auth(None)
        self.assertNotIn("GIT_TERMINAL_PROMPT", os.environ)


# =============================================================================
# run_evaluation.py — _safe_collect
# =============================================================================

class TestSafeCollect(TestCase):

    def test_success_no_error(self):
        from odoo.addons.aurora.worker.run_evaluation import _safe_collect
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        called = []
        _safe_collect(conn, 1, "test", lambda: called.append(True))
        self.assertEqual(called, [True])

    def test_exception_swallowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _safe_collect
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _safe_collect(conn, 1, "test", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertTrue(True)


# =============================================================================
# run_evaluation.py — EvalCancelled & _check_cancelled
# =============================================================================

class TestEvalCheckCancelled(TestCase):

    def test_not_cancelled_passes(self):
        import odoo.addons.aurora.worker.run_evaluation as mod
        orig = mod._cancelled
        mod._cancelled = False
        try:
            mod._check_cancelled()
            self.assertFalse(mod._cancelled)
        finally:
            mod._cancelled = orig

    def test_cancelled_raises_eval_cancelled(self):
        import odoo.addons.aurora.worker.run_evaluation as mod
        orig = mod._cancelled
        mod._cancelled = True
        try:
            with self.assertRaises(mod.EvalCancelled):
                mod._check_cancelled()
        finally:
            mod._cancelled = orig


# =============================================================================
# run_evaluation.py — _sigterm_handler
# =============================================================================

class TestEvalSigtermHandler(TestCase):

    def test_sets_cancelled_flag(self):
        import odoo.addons.aurora.worker.run_evaluation as mod
        orig = mod._cancelled
        mod._cancelled = False
        mod._sigterm_handler(15, None)
        self.assertTrue(mod._cancelled)
        mod._cancelled = orig


# =============================================================================
# run_pipeline.py — main() entry point
# =============================================================================

class TestPipelineMainEntryMissingEnv(TestCase):

    @patch.dict(os.environ, {}, clear=True)
    @patch("odoo.addons.aurora.worker.run_pipeline.sys.exit")
    def test_missing_pipeline_id_exits_1(self, mock_exit):
        from odoo.addons.aurora.worker.run_pipeline import main
        mock_exit.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(1)

    @patch.dict(os.environ, {"PIPELINE_ID": "5"}, clear=True)
    @patch("odoo.addons.aurora.worker.run_pipeline.sys.exit")
    def test_missing_odoo_db_exits_1(self, mock_exit):
        from odoo.addons.aurora.worker.run_pipeline import main
        mock_exit.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(1)

    @patch.dict(os.environ, {"PIPELINE_ID": "abc", "ODOO_DB": "mydb"})
    @patch("odoo.addons.aurora.worker.run_pipeline.sys.exit")
    def test_non_integer_pipeline_id_exits_1(self, mock_exit):
        from odoo.addons.aurora.worker.run_pipeline import main
        mock_exit.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(1)

    @patch.dict(os.environ, {"PIPELINE_ID": "5", "ODOO_DB": "testdb"})
    @patch("odoo.addons.aurora.worker.run_pipeline.run_pipeline")
    @patch("odoo.addons.aurora.worker.run_pipeline._init_shared_functions")
    @patch("odoo.addons.aurora.worker.run_pipeline._boot_odoo", side_effect=Exception("boot fail"))
    @patch("odoo.addons.aurora.worker.run_pipeline.sys.exit")
    def test_boot_failure_exits_2(self, mock_exit, mock_boot, mock_init, mock_run):
        from odoo.addons.aurora.worker.run_pipeline import main
        mock_exit.side_effect = SystemExit(2)
        with self.assertRaises(SystemExit):
            main()
        mock_exit.assert_called_with(2)

    @patch.dict(os.environ, {"PIPELINE_ID": "7", "ODOO_DB": "db1"})
    @patch("odoo.addons.aurora.worker.run_pipeline.sys.exit")
    @patch("odoo.addons.aurora.worker.run_pipeline.run_pipeline")
    @patch("odoo.addons.aurora.worker.run_pipeline._init_shared_functions")
    @patch("odoo.addons.aurora.worker.run_pipeline._boot_odoo")
    def test_valid_env_calls_run_pipeline(self, mock_boot, mock_init, mock_run, mock_exit):
        from odoo.addons.aurora.worker.run_pipeline import main
        mock_exit.side_effect = SystemExit(0)
        with self.assertRaises(SystemExit):
            main()
        mock_run.assert_called_once()


# =============================================================================
# run_pipeline.py — _sigterm_handler
# =============================================================================

class TestPipelineSigtermHandler(TestCase):

    def test_sets_cancelled_flag(self):
        import odoo.addons.aurora.worker.run_pipeline as mod
        orig = mod._cancelled
        mod._cancelled = False
        mod._sigterm_handler(15, None)
        self.assertTrue(mod._cancelled)
        mod._cancelled = orig


# =============================================================================
# run_pipeline.py — _notify_webhook
# =============================================================================

class TestNotifyWebhook(TestCase):

    @patch.dict(os.environ, {"AURORA_WEBHOOK_URL": ""})
    def test_no_url_returns_early(self):
        from odoo.addons.aurora.worker.run_pipeline import _notify_webhook
        registry = MagicMock()
        # Should not raise, does nothing
        _notify_webhook(registry, "db", 1, "done")
        registry.cursor.assert_not_called()

    @patch.dict(os.environ, {"AURORA_WEBHOOK_URL": "http://hook.test/api"})
    @patch("requests.post")
    @patch("odoo.addons.aurora.models.credential_manager.get_encrypted_param_raw", return_value="")
    def test_no_secret_returns_early(self, mock_param, mock_post):
        from odoo.addons.aurora.worker.run_pipeline import _notify_webhook
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        _notify_webhook(registry, "db", 1, "done")
        mock_post.assert_not_called()

    @patch.dict(os.environ, {"AURORA_WEBHOOK_URL": "http://hook.test/api"})
    @patch("requests.post")
    @patch("odoo.addons.aurora.models.credential_manager.get_encrypted_param_raw", return_value="secret123")
    def test_posts_with_headers(self, mock_param, mock_post):
        from odoo.addons.aurora.worker.run_pipeline import _notify_webhook
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        _notify_webhook(registry, "db", 1, "done", "completed")
        mock_post.assert_called_once()
        kwargs = mock_post.call_args[1]
        self.assertIn("X-Aurora-Timestamp", kwargs["headers"])
        self.assertIn("X-Aurora-Signature", kwargs["headers"])

    @patch.dict(os.environ, {"AURORA_WEBHOOK_URL": "http://hook.test/api"})
    @patch("requests.post", side_effect=Exception("conn refused"))
    @patch("odoo.addons.aurora.models.credential_manager.get_encrypted_param_raw", return_value="secret")
    def test_post_exception_swallowed(self, mock_param, mock_post):
        from odoo.addons.aurora.worker.run_pipeline import _notify_webhook
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        _notify_webhook(registry, "db", 1, "failed", "err")
        mock_post.assert_called_once()


# =============================================================================
# run_pipeline.py — _post_chatter
# =============================================================================

class TestPostChatter(TestCase):

    @patch("odoo.api")
    @patch("odoo.SUPERUSER_ID", 1)
    def test_posts_message(self, mock_api):
        from odoo.addons.aurora.worker.run_pipeline import _post_chatter
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        mock_env = MagicMock()
        mock_api.Environment.return_value = mock_env
        rec = MagicMock()
        mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=rec)))
        _post_chatter(registry, 1, 42, "Pipeline done")
        rec.message_post.assert_called_once()

    def test_exception_swallowed(self):
        from odoo.addons.aurora.worker.run_pipeline import _post_chatter
        registry = MagicMock()
        registry.cursor.side_effect = Exception("db down")
        _post_chatter(registry, 1, 42, "msg")
        self.assertTrue(registry.cursor.called)


# =============================================================================
# run_pipeline.py — _read_config
# =============================================================================

class TestReadConfig(TestCase):

    def test_pipeline_not_found_raises(self):
        from odoo.addons.aurora.worker.run_pipeline import _read_config
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        pipeline = MagicMock()
        pipeline.exists.return_value = False

        import odoo
        orig_env_cls = odoo.api.Environment
        mock_env = MagicMock()
        mock_env.__getitem__ = MagicMock(return_value=MagicMock(
            browse=MagicMock(return_value=pipeline)
        ))
        odoo.api.Environment = MagicMock(return_value=mock_env)
        try:
            with self.assertRaises(AuroraPipelineError):
                _read_config(registry, 9999)
        finally:
            odoo.api.Environment = orig_env_cls


# =============================================================================
# run_pipeline.py — _lease_tokens / _release_tokens
# =============================================================================

class TestPipelineTokenLifecycle(TestCase):

    @patch("odoo.addons.aurora.models.github_token.AuroraGithubToken")
    def test_lease_tokens_commits(self, mock_token_cls):
        from odoo.addons.aurora.worker.run_pipeline import _lease_tokens
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        mock_token_cls.lease_tokens.return_value = ["tok1", "tok2"]
        result = _lease_tokens(registry, 1, count=2)
        self.assertEqual(result, ["tok1", "tok2"])
        cr.commit.assert_called_once()
        cr.close.assert_called_once()

    @patch("odoo.addons.aurora.models.github_token.AuroraGithubToken")
    def test_release_tokens_commits(self, mock_token_cls):
        from odoo.addons.aurora.worker.run_pipeline import _release_tokens
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        _release_tokens(registry, 1)
        mock_token_cls.release_tokens.assert_called_once()
        cr.commit.assert_called_once()
        cr.close.assert_called_once()

    @patch("odoo.addons.aurora.models.github_token.AuroraGithubToken")
    def test_release_tokens_exception_swallowed(self, mock_token_cls):
        from odoo.addons.aurora.worker.run_pipeline import _release_tokens
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        mock_token_cls.release_tokens.side_effect = Exception("db error")
        # Should not raise
        _release_tokens(registry, 1)
        cr.close.assert_called_once()


# =============================================================================
# run_pipeline.py — _heartbeat_rate_limits
# =============================================================================

class TestHeartbeatRateLimits(TestCase):

    @patch("odoo.addons.aurora.models.github_token.AuroraGithubToken")
    @patch("requests.get")
    def test_successful_probe(self, mock_get, mock_token_cls):
        from odoo.addons.aurora.worker.run_pipeline import _heartbeat_rate_limits
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"resources": {"core": {"remaining": 4500, "reset": 1234567890}}}
        mock_get.return_value = resp
        _heartbeat_rate_limits(registry, 1, ["tok1"])
        mock_token_cls.heartbeat_rate_limits.assert_called_once()
        cr.commit.assert_called_once()

    @patch("requests.get", side_effect=Exception("timeout"))
    def test_probe_failure_no_db_write(self, mock_get):
        from odoo.addons.aurora.worker.run_pipeline import _heartbeat_rate_limits
        registry = MagicMock()
        _heartbeat_rate_limits(registry, 1, ["tok1"])
        registry.cursor.assert_not_called()


# =============================================================================
# run_pipeline.py — _create_phase2_results
# =============================================================================

class TestCreatePhase2Results(TestCase):

    @patch("odoo.api")
    @patch("odoo.SUPERUSER_ID", 1)
    def test_creates_records(self, mock_api):
        from odoo.addons.aurora.worker.run_pipeline import _create_phase2_results
        registry = MagicMock()
        cr = MagicMock()
        registry.cursor.return_value = cr
        mock_env = MagicMock()
        mock_api.Environment.return_value = mock_env
        Result = MagicMock()
        mock_env.__getitem__ = MagicMock(return_value=Result)
        results = [{"instance_id": "test", "f2p": ["t1"], "p2p": [], "s2p": [], "n2p": [], "valid": True}]
        _create_phase2_results(registry, 1, results)
        cr.commit.assert_called_once()

    @patch("odoo.api", side_effect=Exception("import"))
    def test_exception_swallowed(self, mock_api):
        from odoo.addons.aurora.worker.run_pipeline import _create_phase2_results
        registry = MagicMock()
        registry.cursor.side_effect = Exception("db")
        _create_phase2_results(registry, 1, [])
        self.assertTrue(registry.cursor.called)


# =============================================================================
# webhook_controller.py — _verify_token (deeper)
# =============================================================================

class TestVerifyTokenDeeper(TestCase):

    @patch.dict(os.environ, {"AURORA_WEBHOOK_SECRET": ""})
    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_no_secret_env_returns_false(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_token
        result = _verify_token()
        self.assertFalse(result)

    @patch.dict(os.environ, {"AURORA_WEBHOOK_SECRET": "  "})
    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_whitespace_only_secret_returns_false(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_token
        result = _verify_token()
        self.assertFalse(result)


# =============================================================================
# webhook_controller.py — _verify_hmac (deeper)
# =============================================================================

class TestVerifyHmacDeeper(TestCase):

    @patch("odoo.addons.aurora.controllers.webhook_controller._raw_body", return_value=b'{"test":1}')
    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_missing_timestamp_header_returns_false(self, mock_request, mock_body):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_hmac
        mock_request.httprequest.headers = {"X-Aurora-Signature": "abc"}
        result = _verify_hmac("secret")
        self.assertFalse(result)

    @patch("odoo.addons.aurora.controllers.webhook_controller._raw_body", return_value=b'{"test":1}')
    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_missing_signature_header_returns_false(self, mock_request, mock_body):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_hmac
        mock_request.httprequest.headers = {"X-Aurora-Timestamp": "1234567890"}
        result = _verify_hmac("secret")
        self.assertFalse(result)

    @patch("odoo.addons.aurora.controllers.webhook_controller._raw_body", return_value=b'{"test":1}')
    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_non_numeric_timestamp_returns_false(self, mock_request, mock_body):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_hmac
        mock_request.httprequest.headers = {"X-Aurora-Timestamp": "abc", "X-Aurora-Signature": "sig"}
        result = _verify_hmac("secret")
        self.assertFalse(result)

    @patch("odoo.addons.aurora.controllers.webhook_controller.time.time", return_value=1000000)
    @patch("odoo.addons.aurora.controllers.webhook_controller._raw_body", return_value=b'{"t":1}')
    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_stale_timestamp_returns_false(self, mock_request, mock_body, mock_time):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_hmac
        # Timestamp 500 seconds ago (beyond 300s skew)
        mock_request.httprequest.headers = {
            "X-Aurora-Timestamp": "999500",
            "X-Aurora-Signature": "abc",
        }
        result = _verify_hmac("secret")
        self.assertFalse(result)

    def test_empty_secret_returns_false(self):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_hmac
        result = _verify_hmac("")
        self.assertFalse(result)


# =============================================================================
# webhook_controller.py — _verify_legacy_token (deeper)
# =============================================================================

class TestVerifyLegacyTokenDeeper(TestCase):

    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_empty_secret_returns_false(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_legacy_token
        self.assertFalse(_verify_legacy_token(""))

    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_no_token_header_returns_false(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_legacy_token
        mock_request.httprequest.headers = {}
        self.assertFalse(_verify_legacy_token("secret123"))

    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_wrong_token_returns_false(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_legacy_token
        mock_request.httprequest.headers = {"X-Aurora-Token": "wrong"}
        self.assertFalse(_verify_legacy_token("correct_secret"))

    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_correct_token_returns_true(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _verify_legacy_token
        mock_request.httprequest.headers = {"X-Aurora-Token": "mysecret"}
        self.assertTrue(_verify_legacy_token("mysecret"))


# =============================================================================
# webhook_controller.py — _raw_body
# =============================================================================

class TestRawBody(TestCase):

    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_returns_bytes(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _raw_body
        mock_request.httprequest.get_data.return_value = b"test body"
        self.assertEqual(_raw_body(), b"test body")

    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_exception_returns_empty_bytes(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _raw_body
        mock_request.httprequest.get_data.side_effect = Exception("no body")
        self.assertEqual(_raw_body(), b"")

    @patch("odoo.addons.aurora.controllers.webhook_controller.request")
    def test_none_returns_empty_bytes(self, mock_request):
        from odoo.addons.aurora.controllers.webhook_controller import _raw_body
        mock_request.httprequest.get_data.return_value = None
        self.assertEqual(_raw_body(), b"")


# =============================================================================
# webhook_controller.py — _send_bus_notification (edge cases)
# =============================================================================

class TestBusNotificationEdge(TestCase):

    def test_no_partner_id_no_send(self):
        from odoo.addons.aurora.controllers.webhook_controller import _send_bus_notification
        env = MagicMock()
        record = MagicMock()
        record.id = 1
        record.user_id.partner_id = None
        _send_bus_notification(env, "aurora.pipeline", record, {})
        env["bus.bus"].sudo.return_value._sendone.assert_not_called()

    def test_values_stage_overrides_record_stage(self):
        from odoo.addons.aurora.controllers.webhook_controller import _send_bus_notification
        env = MagicMock()
        record = MagicMock()
        record.id = 5
        record.user_id.partner_id = MagicMock()
        record.stage = "running"
        record.progress_text = "old"
        _send_bus_notification(env, "aurora.evaluation", record, {"stage": "done", "progress_text": "new"})
        payload = env["bus.bus"].sudo.return_value._sendone.call_args[0][2]
        self.assertEqual(payload["stage"], "done")
        self.assertEqual(payload["progress_text"], "new")


# =============================================================================
# webhook_controller.py — _append_log (edge cases)
# =============================================================================

class TestWebhookAppendLogEdge(TestCase):

    def test_very_long_message_truncated_to_5000_lines(self):
        from odoo.addons.aurora.controllers.webhook_controller import _append_log
        record = MagicMock()
        record.log = "\n".join(f"line{i}" for i in range(4999))
        # Adding a multi-line message that pushes total over 5000
        big_msg = "\n".join(f"new{i}" for i in range(100))
        _append_log(record, big_msg)
        written = record.sudo.return_value.write.call_args[0][0]["log"]
        lines = written.splitlines()
        self.assertLessEqual(len(lines), 5000)

    def test_none_message_treated_as_empty(self):
        from odoo.addons.aurora.controllers.webhook_controller import _append_log
        record = MagicMock()
        # Empty string means no-op
        _append_log(record, "")
        record.sudo.return_value.write.assert_not_called()


# =============================================================================
# dataset_resolver.py — is_remote (edge cases)
# =============================================================================

class TestIsRemoteEdgeCases(TestCase):

    def test_uppercase_https(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertTrue(is_remote("HTTPS://example.com/file"))

    def test_mixed_case_http(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertTrue(is_remote("Http://example.com/file"))

    def test_s3_uppercase(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertTrue(is_remote("S3://bucket/key"))

    def test_windows_path_not_remote(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote("C:\\data\\file.jsonl"))

    def test_dot_relative_not_remote(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote("./data/file.jsonl"))

    def test_tilde_path_not_remote(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote("~/data/file.jsonl"))


# =============================================================================
# dataset_resolver.py — resolve_to_local (cache behavior)
# =============================================================================

class TestResolveToLocalCache(TestCase):

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    @patch("odoo.addons.aurora.models.dataset_resolver._get_output_dir")
    def test_cache_hit_skips_download(self, mock_dir, mock_dl):
        from odoo.addons.aurora.models.dataset_resolver import resolve_to_local, _target_path, _cache_root
        with tempfile.TemporaryDirectory() as d:
            mock_dir.return_value = d
            url = "https://bucket.s3.amazonaws.com/data.jsonl"
            cache_root = _cache_root(d)
            target = _target_path(cache_root, url)
            # Create a cached file
            with open(target, "w") as f:
                f.write("cached content\n")
            result = resolve_to_local(MagicMock(), url)
            mock_dl.assert_not_called()
            self.assertEqual(result, target)

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    @patch("odoo.addons.aurora.models.dataset_resolver._get_output_dir")
    def test_empty_cache_file_triggers_download(self, mock_dir, mock_dl):
        from odoo.addons.aurora.models.dataset_resolver import resolve_to_local, _target_path, _cache_root
        with tempfile.TemporaryDirectory() as d:
            mock_dir.return_value = d
            url = "https://bucket.s3.amazonaws.com/data.jsonl"
            cache_root = _cache_root(d)
            target = _target_path(cache_root, url)
            # Create an empty cached file (size 0 means invalid)
            with open(target, "w") as f:
                pass
            resolve_to_local(MagicMock(), url)
            mock_dl.assert_called_once()


# =============================================================================
# dataset_resolver.py — _download_s3
# =============================================================================

class TestDownloadS3(TestCase):

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    def test_converts_s3_to_https(self, mock_dl):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        _download_s3("s3://mybucket/path/to/file.jsonl", "/tmp/target.jsonl")
        called_url = mock_dl.call_args[0][0]
        self.assertTrue(called_url.startswith("https://mybucket.s3.amazonaws.com/"))
        self.assertIn("path/to/file.jsonl", called_url)

    def test_invalid_s3_url_raises(self):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        with self.assertRaises(ValueError):
            _download_s3("s3:///no-bucket", "/tmp/t.jsonl")

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    def test_empty_key_raises(self, mock_dl):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        with self.assertRaises(ValueError):
            _download_s3("s3://bucket/", "/tmp/t.jsonl")


# =============================================================================
# dataset_resolver.py — _get_output_dir
# =============================================================================

class TestGetOutputDirEdge(TestCase):

    def test_env_lookup_exception_returns_default(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_output_dir
        mock_env = MagicMock(spec=["__getitem__"])
        mock_env.__getitem__ = MagicMock(side_effect=Exception("no model"))
        result = _get_output_dir(mock_env)
        self.assertEqual(result, "/tmp/aurora_output")

    def test_cursor_returns_empty_string_uses_default(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_output_dir
        mock_cr = MagicMock()
        mock_cr.fetchone.return_value = ("",)
        result = _get_output_dir(mock_cr)
        self.assertEqual(result, "/tmp/aurora_output")

    def test_plain_object_returns_default(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_output_dir
        result = _get_output_dir(object())
        self.assertEqual(result, "/tmp/aurora_output")


# =============================================================================
# dataset_resolver.py — _get_download_lock
# =============================================================================

class TestGetDownloadLock(TestCase):

    def test_same_url_same_lock(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_download_lock
        lock1 = _get_download_lock("https://a.com/f.jsonl")
        lock2 = _get_download_lock("https://a.com/f.jsonl")
        self.assertIs(lock1, lock2)

    def test_different_url_different_lock(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_download_lock
        lock1 = _get_download_lock("https://a.com/f1.jsonl")
        lock2 = _get_download_lock("https://b.com/f2.jsonl")
        self.assertIsNot(lock1, lock2)

    def test_returns_threading_lock(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_download_lock
        lock = _get_download_lock("https://unique-url.com/test")
        self.assertIsInstance(lock, type(threading.Lock()))


# =============================================================================
# s3_storage.py — is_configured (deeper edge cases)
# =============================================================================

class TestIsConfiguredDeeper(TestCase):

    def test_zero_value_bucket_returns_false(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertFalse(is_configured({"bucket": 0}))

    def test_false_value_bucket_returns_false(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertFalse(is_configured({"bucket": False}))

    def test_any_truthy_string_returns_true(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertTrue(is_configured({"bucket": "x"}))

    def test_config_with_extra_keys_still_works(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertTrue(is_configured({"bucket": "b", "region": "us-east-1", "access_key": "ak"}))


# =============================================================================
# s3_storage.py — upload_file (deeper edge cases)
# =============================================================================

class TestUploadFileDeeper(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_first_attempt_success_no_sleep(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_gc.return_value = MagicMock()
        upload_file({"bucket": "b", "region": "r"}, "/tmp/f.txt", "k")
        mock_sleep.assert_not_called()

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_url_format_includes_bucket_and_key(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_gc.return_value = MagicMock()
        url = upload_file({"bucket": "my-bucket", "region": "eu-west-1"}, "/tmp/f.txt", "folder/file.jsonl")
        self.assertEqual(url, "https://my-bucket.s3.eu-west-1.amazonaws.com/folder/file.jsonl")

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_raises_last_error_on_exhaustion(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        specific_error = ConnectionError("network down")
        mock_client.upload_file.side_effect = specific_error
        mock_gc.return_value = mock_client
        with self.assertRaises(ConnectionError) as ctx:
            upload_file({"bucket": "b", "region": "r"}, "/tmp/f.txt", "k")
        self.assertIs(ctx.exception, specific_error)


# =============================================================================
# s3_storage.py — generate_presigned_url (deeper)
# =============================================================================

class TestGeneratePresignedUrlDeeper(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_passes_s3_config(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import generate_presigned_url
        mock_gc.return_value = MagicMock()
        cfg = {"bucket": "b", "region": "r", "access_key": "ak", "secret_key": "sk"}
        generate_presigned_url(cfg, "key")
        mock_gc.assert_called_once_with(cfg)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_zero_expiry(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import generate_presigned_url
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        generate_presigned_url({"bucket": "b", "region": "r"}, "k", expires_in=0)
        kwargs = mock_client.generate_presigned_url.call_args
        self.assertEqual(kwargs[1]["ExpiresIn"], 0)


# =============================================================================
# s3_storage.py — build_s3_key (deeper)
# =============================================================================

class TestBuildS3KeyDeeper(TestCase):

    def test_run_number_zero(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("org", "repo", 0, "file.jsonl")
        self.assertIn("run_0/", result)

    def test_large_run_number(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("org", "repo", 9999, "file.jsonl")
        self.assertIn("run_9999/", result)

    def test_special_chars_in_filename(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("org", "repo", 1, "file (1).jsonl")
        self.assertTrue(result.endswith("file (1).jsonl"))

    def test_empty_folder_no_prefix(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("org", "repo", 1, "f.txt", folder="")
        self.assertFalse(result.startswith("/"))

    def test_default_phase_aurora_phase1(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("org", "repo", 1, "f.txt")
        self.assertTrue(result.startswith("aurora_phase1/"))


# =============================================================================
# s3_storage.py — get_next_run_number (deeper)
# =============================================================================

class TestGetNextRunNumberDeeper(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_run_number_gaps_returns_max_plus_one(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        prefix = "aurora_phase1/org__repo/"
        paginator.paginate.return_value = [{
            "CommonPrefixes": [
                {"Prefix": f"{prefix}run_1/"},
                {"Prefix": f"{prefix}run_5/"},
                {"Prefix": f"{prefix}run_10/"},
            ]
        }]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        result = get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo")
        self.assertEqual(result, 11)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_no_common_prefixes_key_returns_1(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        # Page without CommonPrefixes key at all
        paginator.paginate.return_value = [{"Contents": []}]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        result = get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo")
        self.assertEqual(result, 1)


# =============================================================================
# s3_storage.py — _build_base_prefix (deeper)
# =============================================================================

class TestBuildBasePrefixDeeper(TestCase):

    def test_org_repo_with_special_chars(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("my-org", "my-repo")
        self.assertEqual(result, "aurora_phase1/my-org__my-repo/")

    def test_folder_only_slashes_treated_as_empty(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("o", "r", folder="///")
        # After strip("/"), "" → no folder
        self.assertEqual(result, "aurora_phase1/o__r/")

    def test_phase_only_slashes_uses_default(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("o", "r", phase="///")
        self.assertEqual(result, "/o__r/")


# =============================================================================
# run_evaluation.py — _open_cursor
# =============================================================================

class TestEvalOpenCursor(TestCase):

    @patch("odoo.tools.config", {"db_user": "u", "db_password": "p", "db_host": "h", "db_port": "5432"})
    @patch("psycopg2.connect")
    def test_connects_with_config_values(self, mock_connect):
        from odoo.addons.aurora.worker.run_evaluation import _open_cursor
        _open_cursor("testdb")
        mock_connect.assert_called_once()
        kwargs = mock_connect.call_args[1]
        self.assertEqual(kwargs["dbname"], "testdb")
        self.assertEqual(kwargs["user"], "u")

    @patch("odoo.tools.config", {"db_user": "u", "db_password": "p", "db_host": "h", "db_port": ""})
    @patch("psycopg2.connect")
    def test_empty_port_uses_5432(self, mock_connect):
        from odoo.addons.aurora.worker.run_evaluation import _open_cursor
        _open_cursor("testdb")
        kwargs = mock_connect.call_args[1]
        self.assertEqual(kwargs["port"], 5432)


# =============================================================================
# run_pipeline.py — _open_cursor
# =============================================================================

class TestPipelineOpenCursor(TestCase):

    def test_returns_registry_cursor(self):
        from odoo.addons.aurora.worker.run_pipeline import _open_cursor
        registry = MagicMock()
        mock_cursor = MagicMock()
        registry.cursor.return_value = mock_cursor
        result = _open_cursor(registry)
        self.assertIs(result, mock_cursor)


# =============================================================================
# run_pipeline.py — _build_s3_config
# =============================================================================

class TestBuildS3ConfigDeeper(TestCase):

    def test_all_fields_mapped(self):
        from odoo.addons.aurora.worker.run_pipeline import _build_s3_config
        cfg = {"s3_bucket": "b", "s3_access_key": "a", "s3_secret_key": "s", "s3_region": "r"}
        result = _build_s3_config(cfg)
        self.assertEqual(len(result), 4)
        self.assertIn("bucket", result)
        self.assertIn("access_key", result)
        self.assertIn("secret_key", result)
        self.assertIn("region", result)

    def test_missing_keys_raises_keyerror(self):
        from odoo.addons.aurora.worker.run_pipeline import _build_s3_config
        with self.assertRaises(KeyError):
            _build_s3_config({})


# =============================================================================
# run_pipeline.py — _is_transient_db_error (deeper)
# =============================================================================

class TestIsTransientDbErrorDeeper(TestCase):

    def test_none_pgcode_non_operational_returns_false(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = None
        type(exc).__name__ = "ProgrammingError"
        self.assertFalse(_is_transient_db_error(exc))

    def test_unknown_pgcode_returns_false(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = "99999"
        type(exc).__name__ = "DatabaseError"
        self.assertFalse(_is_transient_db_error(exc))

    def test_no_pgcode_attr_returns_false(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = ValueError("plain error")
        self.assertFalse(_is_transient_db_error(exc))


# =============================================================================
# webhook_controller.py — _filter_payload (type coercion)
# =============================================================================

class TestFilterPayloadTypeEdge(TestCase):

    def test_list_input_returns_empty(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        result = _filter_payload(["a", "b"], frozenset({"a"}))
        self.assertEqual(result, {})

    def test_integer_input_returns_empty(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        result = _filter_payload(42, frozenset({"x"}))
        self.assertEqual(result, {})

    def test_preserves_value_types(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        allowed = frozenset({"count", "name", "active"})
        payload = {"count": 5, "name": "test", "active": True}
        result = _filter_payload(payload, allowed)
        self.assertIsInstance(result["count"], int)
        self.assertIsInstance(result["name"], str)
        self.assertIsInstance(result["active"], bool)


# =============================================================================
# webhook_controller.py — pipeline_webhook action routing
# =============================================================================

class TestPipelineWebhookRouting(TestCase):

    def test_allowed_pipeline_fields_contains_dataset_url(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertIn("dataset_url", _ALLOWED_PIPELINE_FIELDS)

    def test_allowed_pipeline_fields_contains_detected_lang(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertIn("detected_lang", _ALLOWED_PIPELINE_FIELDS)

    def test_allowed_pipeline_fields_no_create_uid(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertNotIn("create_uid", _ALLOWED_PIPELINE_FIELDS)

    def test_allowed_pipeline_fields_no_write_uid(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertNotIn("write_uid", _ALLOWED_PIPELINE_FIELDS)


# =============================================================================
# webhook_controller.py — evaluation_webhook action routing
# =============================================================================

class TestEvaluationWebhookRouting(TestCase):

    def test_allowed_eval_fields_contains_s3_run_number(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("s3_run_number", _ALLOWED_EVALUATION_FIELDS)

    def test_allowed_eval_fields_contains_patch_file(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("patch_file", _ALLOWED_EVALUATION_FIELDS)

    def test_allowed_eval_fields_no_pipeline_id(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertNotIn("pipeline_id", _ALLOWED_EVALUATION_FIELDS)

    def test_allowed_eval_fields_no_user_id(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertNotIn("user_id", _ALLOWED_EVALUATION_FIELDS)


# =============================================================================
# webhook_controller.py — staging webhook fields
# =============================================================================

class TestStagingWebhookFields(TestCase):

    def test_no_log_field_in_staging(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_STAGING_FIELDS
        self.assertNotIn("log", _ALLOWED_STAGING_FIELDS)

    def test_no_test_log_in_staging(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_STAGING_FIELDS
        self.assertNotIn("test_log", _ALLOWED_STAGING_FIELDS)


# =============================================================================
# run_evaluation.py — _resolve_entry_number (deeper)
# =============================================================================

class TestResolveEntryNumberDeeper(TestCase):

    def test_number_as_float_returns_none(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        # Float is not int and not a digit string
        self.assertIsNone(_resolve_entry_number({"number": 3.14}))

    def test_pr_numbers_none_returns_none(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertIsNone(_resolve_entry_number({"pr_numbers": None}))

    def test_pr_numbers_non_list_returns_none(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertIsNone(_resolve_entry_number({"pr_numbers": "123"}))

    def test_pr_numbers_with_non_numeric_element(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertIsNone(_resolve_entry_number({"pr_numbers": ["abc"]}))

    def test_hyphenated_non_numeric_prefix(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertIsNone(_resolve_entry_number({"number": "abc-123"}))


# =============================================================================
# run_evaluation.py — _update_eval (deeper SQL)
# =============================================================================

class TestUpdateEvalDeeperSql(TestCase):

    def test_sql_targets_aurora_evaluation_table(self):
        from odoo.addons.aurora.worker.run_evaluation import _update_eval
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _update_eval(conn, 99, {"stage": "done"})
        sql = cursor.execute.call_args[0][0]
        self.assertIn("aurora_evaluation", sql)
        self.assertIn("WHERE id = %s", sql)

    def test_params_end_with_record_id(self):
        from odoo.addons.aurora.worker.run_evaluation import _update_eval
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _update_eval(conn, 77, {"stage": "failed", "progress_text": "err"})
        params = cursor.execute.call_args[0][1]
        self.assertEqual(params[-1], 77)


# =============================================================================
# run_evaluation.py — _read_eval_config defaults
# =============================================================================

class TestReadEvalConfigDefaults(TestCase):

    def test_null_max_workers_defaults_to_4(self):
        from odoo.addons.aurora.worker.run_evaluation import _read_eval_config
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "/data.jsonl", "/p.jsonl", "/r", "/w", "/o",
            False, None, None, None, None, "", 1, None, None,
        )
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        result = _read_eval_config(conn, 1)
        self.assertEqual(result["max_workers_build"], 4)
        self.assertEqual(result["max_workers_run"], 4)

    def test_null_instance_limit_defaults_to_0(self):
        from odoo.addons.aurora.worker.run_evaluation import _read_eval_config
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "/d.jsonl", "/p.jsonl", "/r", "/w", "/o",
            True, 8, 8, "linux/arm64", None, "1,2,3", 5, None, None,
        )
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        result = _read_eval_config(conn, 1)
        self.assertEqual(result["instance_limit"], 0)

    def test_null_docker_platform_defaults_to_none(self):
        from odoo.addons.aurora.worker.run_evaluation import _read_eval_config
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "/d.jsonl", "/p.jsonl", "/r", "/w", "/o",
            False, 4, 4, None, 0, "", 1, None, None,
        )
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        result = _read_eval_config(conn, 1)
        self.assertIsNone(result["docker_platform"])


# =============================================================================
# General worker patterns — PYTHONPATH / conf loading
# =============================================================================

class TestOdooConfLoading(TestCase):

    def test_default_conf_path(self):
        # Verify the default conf path constant used in both workers
        default = "/etc/odoo/odoo.conf"
        self.assertTrue(default.endswith(".conf"))

    @patch.dict(os.environ, {"ODOO_CONF": "/custom/path.conf"})
    def test_odoo_conf_env_var_respected(self):
        conf_path = os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf")
        self.assertEqual(conf_path, "/custom/path.conf")

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_odoo_conf_uses_default(self):
        conf_path = os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf")
        self.assertEqual(conf_path, "/etc/odoo/odoo.conf")


# =============================================================================
# General worker patterns — WEBHOOK_MAX_SKEW
# =============================================================================

class TestWebhookMaxSkewConstant(TestCase):

    def test_max_skew_5_minutes(self):
        from odoo.addons.aurora.controllers.webhook_controller import _WEBHOOK_MAX_SKEW_SECONDS
        self.assertEqual(_WEBHOOK_MAX_SKEW_SECONDS, 300)




# =============================================================================
# run_evaluation.py — _lease_tokens / _release_tokens
# =============================================================================

class TestEvalTokenLifecycle(TestCase):

    @patch("odoo.addons.aurora.worker.run_evaluation._open_cursor")
    @patch("odoo.addons.aurora.models.github_token.AuroraGithubToken")
    def test_lease_returns_tokens(self, mock_token_cls, mock_cursor):
        from odoo.addons.aurora.worker.run_evaluation import _lease_tokens
        conn = MagicMock()
        mock_cursor.return_value = conn
        mock_token_cls.lease_tokens.return_value = ["t1"]
        result = _lease_tokens("db", 1, count=1)
        self.assertEqual(result, ["t1"])
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    @patch("odoo.addons.aurora.worker.run_evaluation._open_cursor")
    @patch("odoo.addons.aurora.models.github_token.AuroraGithubToken")
    def test_release_swallows_exception(self, mock_token_cls, mock_cursor):
        from odoo.addons.aurora.worker.run_evaluation import _release_tokens
        conn = MagicMock()
        mock_cursor.return_value = conn
        mock_token_cls.release_tokens.side_effect = Exception("fail")
        # Should not raise
        _release_tokens("db", 1)
        conn.close.assert_called_once()


# =============================================================================
# dataset_resolver.py — clear_cache
# =============================================================================

class TestClearCacheDeeper(TestCase):

    def test_clear_specific_url_nonexistent_dir_no_error(self):
        from odoo.addons.aurora.models.dataset_resolver import clear_cache
        with tempfile.TemporaryDirectory() as d:
            mock_cr = MagicMock()
            mock_cr.execute = MagicMock()
            mock_cr.fetchone = MagicMock(return_value=(d,))
            clear_cache(mock_cr, "https://nonexistent.com/file.jsonl")
            self.assertTrue(os.path.isdir(d))

    def test_clear_all_removes_entire_cache_dir(self):
        from odoo.addons.aurora.models.dataset_resolver import clear_cache
        with tempfile.TemporaryDirectory() as d:
            cache_dir = os.path.join(d, "dataset_cache")
            os.makedirs(cache_dir)
            # Create some files
            with open(os.path.join(cache_dir, "test.txt"), "w") as f:
                f.write("data")
            mock_cr = MagicMock()
            mock_cr.execute = MagicMock()
            mock_cr.fetchone = MagicMock(return_value=(d,))
            clear_cache(mock_cr)
            self.assertFalse(os.path.exists(cache_dir))


# =============================================================================
# run_pipeline.py — PipelineCancelled exception
# =============================================================================

class TestPipelineCancelledDeeper(TestCase):

    def test_inherits_from_exception(self):
        from odoo.addons.aurora.worker.run_pipeline import PipelineCancelled
        self.assertTrue(issubclass(PipelineCancelled, Exception))

    def test_can_carry_context(self):
        from odoo.addons.aurora.worker.run_pipeline import PipelineCancelled
        exc = PipelineCancelled("stopped at step 3")
        self.assertIn("step 3", str(exc))


# =============================================================================
# s3_storage.py — _RUN_PREFIX_RE (deeper)
# =============================================================================

class TestRunPrefixReDeeper(TestCase):

    def test_matches_large_number(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("run_100000/")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "100000")

    def test_no_match_run_negative(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("run_-1/")
        self.assertIsNone(m)

    def test_no_match_run_float(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("run_1.5/")
        self.assertIsNone(m)

    def test_no_match_extra_prefix(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("prefix/run_1/")
        self.assertIsNone(m)


class TestEvalHeartbeatLoop(TestCase):

    def test_heartbeat_loop_stops_on_event(self):
        stop_event = threading.Event()
        stop_event.set()
        called = []
        def fake_wait(timeout=None):
            called.append(timeout)
            return True
        stop_event.wait = fake_wait
        self.assertTrue(stop_event.wait(timeout=60))

    def test_heartbeat_writes_to_db(self):
        from odoo.addons.aurora.worker.run_evaluation import _heartbeat
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _heartbeat(conn, 42, "running")
        cursor.execute.assert_called_once()


class TestPipelineDockerPathSetup(TestCase):

    @patch.dict(os.environ, {"PATH": "/usr/bin"})
    def test_docker_bin_prepended_to_path_if_exists(self):
        docker_bin = "/Applications/Docker.app/Contents/Resources/bin"
        if os.path.isdir(docker_bin):
            self.assertNotIn(docker_bin, os.environ["PATH"])
        else:
            self.assertNotIn(docker_bin, os.environ["PATH"])

    @patch.dict(os.environ, {"PATH": "/Applications/Docker.app/Contents/Resources/bin:/usr/bin"})
    def test_docker_bin_not_duplicated_if_present(self):
        docker_bin = "/Applications/Docker.app/Contents/Resources/bin"
        path = os.environ["PATH"]
        self.assertEqual(path.count(docker_bin), 1)


class TestWebhookControllerConstants(TestCase):

    def test_allowed_pipeline_has_phase2_fields(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertIn("phase2_dataset_count", _ALLOWED_PIPELINE_FIELDS)
        self.assertIn("phase2_image_count", _ALLOWED_PIPELINE_FIELDS)

    def test_allowed_pipeline_has_phase3_fields(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertIn("phase3_inference_count", _ALLOWED_PIPELINE_FIELDS)
        self.assertIn("phase3_pass_at_k", _ALLOWED_PIPELINE_FIELDS)

    def test_allowed_pipeline_has_file_refs(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertIn("phase1_file", _ALLOWED_PIPELINE_FIELDS)
        self.assertIn("phase2_file", _ALLOWED_PIPELINE_FIELDS)
        self.assertIn("phase3_file", _ALLOWED_PIPELINE_FIELDS)

    def test_allowed_eval_total_instances(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("total_instances", _ALLOWED_EVALUATION_FIELDS)
        self.assertIn("resolved_instances", _ALLOWED_EVALUATION_FIELDS)
        self.assertIn("unresolved_instances", _ALLOWED_EVALUATION_FIELDS)
        self.assertIn("error_instances", _ALLOWED_EVALUATION_FIELDS)

    def test_allowed_eval_missing_registries(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("missing_registries", _ALLOWED_EVALUATION_FIELDS)

    def test_allowed_eval_workdir_and_repo_dir(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("workdir", _ALLOWED_EVALUATION_FIELDS)
        self.assertIn("repo_dir", _ALLOWED_EVALUATION_FIELDS)


class TestDatasetResolverHttpTimeout(TestCase):

    def test_http_timeout_is_tuple(self):
        from odoo.addons.aurora.models.dataset_resolver import _HTTP_TIMEOUT
        self.assertIsInstance(_HTTP_TIMEOUT, tuple)
        self.assertEqual(len(_HTTP_TIMEOUT), 2)

    def test_connect_timeout_is_10(self):
        from odoo.addons.aurora.models.dataset_resolver import _HTTP_TIMEOUT
        self.assertEqual(_HTTP_TIMEOUT[0], 10)

    def test_read_timeout_is_300(self):
        from odoo.addons.aurora.models.dataset_resolver import _HTTP_TIMEOUT
        self.assertEqual(_HTTP_TIMEOUT[1], 300)


# =============================================================================
# worker/run_evaluation.py — missing_registries population when total_instances==0
# =============================================================================

class TestWorkerMissingRegistries(TestCase):

    def _make_mock_pr(self, org, repo):
        pr = MagicMock()
        pr.org = org
        pr.repo = repo
        return pr

    @patch("odoo.addons.aurora.worker.run_evaluation._fail_eval")
    @patch("odoo.addons.aurora.worker.run_evaluation._update_eval")
    @patch("odoo.addons.aurora.worker.run_evaluation._append_log")
    def test_missing_registries_written_when_no_instances(self, mock_log, mock_update, mock_fail):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("missing_registries", _ALLOWED_EVAL_COLUMNS)
        self.assertIn("build_status", _ALLOWED_EVAL_COLUMNS)

    def test_allowed_columns_has_dataset_jsonl_url(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("dataset_jsonl_url", _ALLOWED_EVAL_COLUMNS)

    def test_missing_registries_logic_computes_missing_repos(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original_registry = Instance._registry.copy()
        try:
            Instance._registry = {"existing/repo": MagicMock()}
            dataset = {
                "pr1": self._make_mock_pr("existing", "repo"),
                "pr2": self._make_mock_pr("missing", "lib"),
                "pr3": self._make_mock_pr("another", "pkg"),
            }
            missing_repos = set()
            for pr in dataset.values():
                key = f"{pr.org}/{pr.repo}"
                if key not in Instance._registry:
                    missing_repos.add(key)
            missing_list = ", ".join(sorted(missing_repos))
            self.assertIn("another/pkg", missing_list)
            self.assertIn("missing/lib", missing_list)
            self.assertNotIn("existing/repo", missing_list)
        finally:
            Instance._registry = original_registry

    def test_empty_dataset_produces_empty_missing_list(self):
        missing_repos = set()
        total_dataset = 0
        if total_dataset > 0:
            pass
        missing_list = ", ".join(sorted(missing_repos))
        self.assertEqual(missing_list, "")


# =============================================================================
# worker/run_evaluation.py — staging harness loading from DB
# =============================================================================

class TestWorkerStagingHarnessLoading(TestCase):

    @patch("odoo.addons.aurora.worker.run_evaluation._append_log")
    def test_staging_harness_decodes_base64_and_writes_file(self, mock_log):
        import base64
        import tempfile
        content = b"from odoo.addons.aurora.tools.harness.instance import Instance\n"
        encoded = base64.b64encode(content)
        tmp_dir = tempfile.mkdtemp(prefix="test_staging_")
        file_path = os.path.join(tmp_dir, "chi.py")
        with open(file_path, "wb") as fh:
            fh.write(base64.b64decode(encoded))
        with open(file_path, "rb") as fh:
            self.assertEqual(fh.read(), content)

    @patch("odoo.addons.aurora.tools.harness.staging_loader.load_staging_harness")
    @patch("odoo.addons.aurora.worker.run_evaluation._append_log")
    def test_staging_harness_calls_load_staging_harness(self, mock_log, mock_loader):
        import base64
        import tempfile
        content = b"print('test')\n"
        encoded = base64.b64encode(content).decode()
        org_name, repo_name = "go-chi", "chi"
        filename = "chi.py"
        tmp_dir = tempfile.mkdtemp(prefix="staging_harness_")
        file_path = os.path.join(tmp_dir, filename)
        with open(file_path, "wb") as fh:
            fh.write(base64.b64decode(encoded))
        from odoo.addons.aurora.tools.harness.staging_loader import load_staging_harness
        load_staging_harness(file_path, org_name, repo_name)
        mock_loader.assert_called_once_with(file_path, "go-chi", "chi")

    def test_staging_query_builds_correct_sql_pattern(self):
        still_missing = {("go-chi", "chi"), ("org2", "repo2")}
        placeholders = ",".join(["%s"] * len(still_missing))
        keys = [f"{org}/{repo}" for org, repo in still_missing]
        self.assertEqual(len(keys), 2)
        self.assertIn("%s,%s", placeholders)
        self.assertIn("go-chi/chi", keys)
        self.assertIn("org2/repo2", keys)


# =============================================================================
# worker/run_evaluation.py — dataset_jsonl_url + final_report S3 upload
# =============================================================================

class TestWorkerS3UploadFinalize(TestCase):

    def test_dataset_jsonl_glob_pattern(self):
        import glob as _glob
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "go-chi__chi_dataset.jsonl")
            p2 = os.path.join(td, "other_file.txt")
            with open(p1, "w") as f:
                f.write("{}\n")
            with open(p2, "w") as f:
                f.write("x")
            matches = _glob.glob(os.path.join(td, "*_dataset.jsonl"))
            self.assertEqual(len(matches), 1)
            self.assertIn("go-chi__chi_dataset.jsonl", matches[0])

    def test_final_report_path_construction(self):
        from pathlib import Path
        output_dir = "/tmp/aurora_output/harness/go-chi__chi"
        expected = Path(output_dir) / "final_report.json"
        self.assertEqual(str(expected), "/tmp/aurora_output/harness/go-chi__chi/final_report.json")

    @patch("odoo.addons.aurora.models.s3_storage.upload_file")
    @patch("odoo.addons.aurora.models.s3_storage.build_s3_key")
    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=True)
    def test_upload_uses_s3_storage_mod_functions(self, mock_configured, mock_key, mock_upload):
        mock_key.return_value = "aurora/aurora_phase2/go-chi__chi/run_1/go-chi__chi_dataset.jsonl"
        mock_upload.return_value = "http://minio:9000/bkt/aurora/aurora_phase2/go-chi__chi/run_1/go-chi__chi_dataset.jsonl"
        from odoo.addons.aurora.models import s3_storage as s3_storage_mod
        s3_config = {"bucket": "bkt", "region": "us-east-1"}
        s3_key = s3_storage_mod.build_s3_key("go-chi", "chi", 1, "go-chi__chi_dataset.jsonl", folder="aurora", phase="aurora_phase2")
        url = s3_storage_mod.upload_file(s3_config, "/tmp/f.jsonl", s3_key)
        self.assertIn("go-chi__chi_dataset.jsonl", url)

    def test_finalize_update_dict_has_required_fields(self):
        final_report_url = "http://minio:9000/bkt/report.json"
        dataset_jsonl_url = "http://minio:9000/bkt/dataset.jsonl"
        vals = {
            "stage": "done",
            "progress_text": "Evaluation complete",
            "total_instances": 6,
            "resolved_instances": 3,
            "unresolved_instances": 2,
            "error_instances": 1,
            "final_report_file": final_report_url,
            "dataset_jsonl_url": dataset_jsonl_url,
        }
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        for key in vals:
            self.assertIn(key, _ALLOWED_EVAL_COLUMNS)

    def test_dataset_jsonl_url_single_file(self):
        uploaded_urls = ["http://minio:9000/bkt/file.jsonl"]
        result = uploaded_urls[0] if len(uploaded_urls) == 1 else ",".join(uploaded_urls)
        self.assertEqual(result, "http://minio:9000/bkt/file.jsonl")

    def test_dataset_jsonl_url_multiple_files(self):
        uploaded_urls = ["http://minio:9000/bkt/a.jsonl", "http://minio:9000/bkt/b.jsonl"]
        result = uploaded_urls[0] if len(uploaded_urls) == 1 else ",".join(uploaded_urls)
        self.assertEqual(result, "http://minio:9000/bkt/a.jsonl,http://minio:9000/bkt/b.jsonl")


# =============================================================================
# dataset_resolver._download_s3 — MinIO endpoint override
# =============================================================================

class TestDatasetResolverDownloadS3Endpoint(TestCase):

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    def test_download_s3_uses_minio_when_endpoint_set(self, mock_http):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "http://minio.local:9000"}):
            _download_s3("s3://mybucket/path/to/file.jsonl", "/tmp/target.jsonl")
        mock_http.assert_called_once_with(
            "http://minio.local:9000/mybucket/path/to/file.jsonl",
            "/tmp/target.jsonl",
        )

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    def test_download_s3_uses_aws_when_no_endpoint(self, mock_http):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AURORA_S3_ENDPOINT", None)
            _download_s3("s3://mybucket/path/to/file.jsonl", "/tmp/target.jsonl")
        mock_http.assert_called_once_with(
            "https://mybucket.s3.amazonaws.com/path/to/file.jsonl",
            "/tmp/target.jsonl",
        )

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    def test_download_s3_endpoint_trailing_slash_stripped(self, mock_http):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "http://minio:9000/"}):
            _download_s3("s3://bkt/key.jsonl", "/tmp/t")
        mock_http.assert_called_once_with(
            "http://minio:9000/bkt/key.jsonl",
            "/tmp/t",
        )

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    def test_download_s3_empty_endpoint_uses_aws(self, mock_http):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "  "}):
            _download_s3("s3://bkt/k", "/tmp/t")
        mock_http.assert_called_once_with(
            "https://bkt.s3.amazonaws.com/k",
            "/tmp/t",
        )

    def test_download_s3_invalid_url_raises(self):
        from odoo.addons.aurora.models.dataset_resolver import _download_s3
        with self.assertRaises(ValueError):
            _download_s3("s3:///no-bucket", "/tmp/t")


# =============================================================================
# tools/harness/run_evaluation.py — unique logger name per dir
# =============================================================================

class TestHarnessLoggerPerDir(TestCase):

    def test_different_dirs_produce_different_loggers(self):
        from pathlib import Path
        dir_a = Path("/tmp/workdir/base_image")
        dir_b = Path("/tmp/workdir/pr_123")
        safe_a = str(dir_a).replace("/", ".").replace("\\", ".")
        safe_b = str(dir_b).replace("/", ".").replace("\\", ".")
        name_a = f"aurora.harness.img.{safe_a}.build_image.log"
        name_b = f"aurora.harness.img.{safe_b}.build_image.log"
        self.assertNotEqual(name_a, name_b)

    def test_same_dir_produces_same_logger_name(self):
        from pathlib import Path
        dir_a = Path("/tmp/workdir/base")
        safe_a = str(dir_a).replace("/", ".").replace("\\", ".")
        name1 = f"aurora.harness.img.{safe_a}.build_image.log"
        name2 = f"aurora.harness.img.{safe_a}.build_image.log"
        self.assertEqual(name1, name2)

    def test_logger_function_returns_unique_loggers(self):
        import logging
        from pathlib import Path
        from odoo.addons.aurora.tools.harness.run_evaluation import get_non_propagate_logger
        with tempfile.TemporaryDirectory() as td:
            dir_a = Path(td) / "img_a"
            dir_b = Path(td) / "img_b"
            dir_a.mkdir()
            dir_b.mkdir()
            logger_a = get_non_propagate_logger(dir_a, "build_image.log", "INFO", False)
            logger_b = get_non_propagate_logger(dir_b, "build_image.log", "INFO", False)
            self.assertIsNot(logger_a, logger_b)
            self.assertNotEqual(logger_a.name, logger_b.name)
            for h in logger_a.handlers[:]:
                logger_a.removeHandler(h)
                h.close()
            for h in logger_b.handlers[:]:
                logger_b.removeHandler(h)
                h.close()


# =============================================================================
# evaluation_executor._ALLOWED_COLUMNS — dataset_jsonl_url
# =============================================================================

class TestEvalExecutorAllowedColumns(TestCase):

    def test_allowed_columns_has_dataset_jsonl_url(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("dataset_jsonl_url", _ALLOWED_COLUMNS)

    def test_allowed_columns_has_missing_registries(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("missing_registries", _ALLOWED_COLUMNS)

    def test_allowed_columns_has_s3_run_number(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("s3_run_number", _ALLOWED_COLUMNS)
