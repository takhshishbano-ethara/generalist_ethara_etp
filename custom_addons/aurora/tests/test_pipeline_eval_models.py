# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch, PropertyMock


class TestPipelineConstants(unittest.TestCase):
    """Tests 1-14: Pipeline infrastructure constants."""

    def test_namespace_value(self):
        from odoo.addons.aurora.models.pipeline import NAMESPACE
        self.assertEqual(NAMESPACE, "aurora")

    def test_service_account_value(self):
        from odoo.addons.aurora.models.pipeline import SERVICE_ACCOUNT
        self.assertEqual(SERVICE_ACCOUNT, "aurora-worker")

    def test_s3_bucket_value(self):
        from odoo.addons.aurora.models.pipeline import S3_BUCKET
        self.assertEqual(S3_BUCKET, "production-grtlabs-tag")

    def test_s3_region_value(self):
        from odoo.addons.aurora.models.pipeline import S3_REGION
        self.assertEqual(S3_REGION, "us-east-1")

    def test_docker_image_contains_aurora_worker(self):
        from odoo.addons.aurora.models.pipeline import DOCKER_IMAGE
        self.assertIn("aurora-worker", DOCKER_IMAGE)

    def test_cpu_request_value(self):
        from odoo.addons.aurora.models.pipeline import CPU_REQUEST
        self.assertEqual(CPU_REQUEST, "1")

    def test_memory_request_value(self):
        from odoo.addons.aurora.models.pipeline import MEMORY_REQUEST
        self.assertEqual(MEMORY_REQUEST, "2Gi")

    def test_memory_limit_value(self):
        from odoo.addons.aurora.models.pipeline import MEMORY_LIMIT
        self.assertEqual(MEMORY_LIMIT, "4Gi")

    def test_deadline_seconds_value(self):
        from odoo.addons.aurora.models.pipeline import DEADLINE_SECONDS
        self.assertEqual(DEADLINE_SECONDS, 14400)

    def test_kueue_queue_value(self):
        from odoo.addons.aurora.models.pipeline import KUEUE_QUEUE
        self.assertEqual(KUEUE_QUEUE, "aurora-pipelines")

    def test_node_selector_key(self):
        from odoo.addons.aurora.models.pipeline import NODE_SELECTOR
        self.assertIn("ethara.ai/node-pool", NODE_SELECTOR)

    def test_node_selector_value_general_purpose(self):
        from odoo.addons.aurora.models.pipeline import NODE_SELECTOR
        self.assertEqual(NODE_SELECTOR["ethara.ai/node-pool"], "general-purpose")

    def test_s3_aurora_prefix(self):
        from odoo.addons.aurora.models.pipeline import S3_AURORA_PREFIX
        self.assertEqual(S3_AURORA_PREFIX, "aurora")

    def test_worker_script_path(self):
        from odoo.addons.aurora.models.pipeline import WORKER_SCRIPT
        self.assertEqual(WORKER_SCRIPT, "/opt/odoo/custom_addons/aurora/worker/run_pipeline.py")


class TestPipelineGetEnv(unittest.TestCase):
    """Tests 15-22: _get_env helper function."""

    def test_get_env_returns_value(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        with patch.dict(os.environ, {"TEST_KEY_1": "hello"}):
            self.assertEqual(_get_env("TEST_KEY_1"), "hello")

    def test_get_env_returns_default_when_missing(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        result = _get_env("NONEXISTENT_KEY_XYZ_12345", "fallback")
        self.assertEqual(result, "fallback")

    def test_get_env_default_is_empty_string(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        result = _get_env("NONEXISTENT_KEY_XYZ_99999")
        self.assertEqual(result, "")

    def test_get_env_strips_whitespace(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        with patch.dict(os.environ, {"TEST_KEY_2": "  value  "}):
            self.assertEqual(_get_env("TEST_KEY_2"), "value")

    def test_get_env_strips_newline(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        with patch.dict(os.environ, {"TEST_KEY_3": "value\n"}):
            self.assertEqual(_get_env("TEST_KEY_3"), "value")

    def test_get_env_strips_tabs(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        with patch.dict(os.environ, {"TEST_KEY_4": "\tval\t"}):
            self.assertEqual(_get_env("TEST_KEY_4"), "val")

    def test_get_env_empty_value_returns_empty(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        with patch.dict(os.environ, {"TEST_KEY_5": ""}):
            self.assertEqual(_get_env("TEST_KEY_5", "default"), "")

    def test_get_env_whitespace_only_returns_empty(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        with patch.dict(os.environ, {"TEST_KEY_6": "   "}):
            self.assertEqual(_get_env("TEST_KEY_6", "default"), "")


class TestLoadK8sConfig(unittest.TestCase):
    """Tests 23-29: _load_k8s_config double-check locking."""

    def test_load_k8s_config_incluster_success(self):
        import odoo.addons.aurora.models.pipeline as pm
        pm._k8s_config_loaded = False
        with patch("odoo.addons.aurora.models.pipeline.k8s_config") as mock_cfg:
            mock_cfg.load_incluster_config.return_value = None
            pm._load_k8s_config()
            mock_cfg.load_incluster_config.assert_called_once()
        pm._k8s_config_loaded = False

    def test_load_k8s_config_falls_back_to_kubeconfig(self):
        import odoo.addons.aurora.models.pipeline as pm
        pm._k8s_config_loaded = False
        with patch("odoo.addons.aurora.models.pipeline.k8s_config") as mock_cfg:
            mock_cfg.ConfigException = Exception
            mock_cfg.load_incluster_config.side_effect = Exception("not in cluster")
            mock_cfg.load_kube_config.return_value = None
            pm._load_k8s_config()
            mock_cfg.load_kube_config.assert_called_once()
        pm._k8s_config_loaded = False

    def test_load_k8s_config_sets_loaded_flag(self):
        import odoo.addons.aurora.models.pipeline as pm
        pm._k8s_config_loaded = False
        with patch("odoo.addons.aurora.models.pipeline.k8s_config") as mock_cfg:
            mock_cfg.load_incluster_config.return_value = None
            pm._load_k8s_config()
            self.assertTrue(pm._k8s_config_loaded)
        pm._k8s_config_loaded = False

    def test_load_k8s_config_skips_if_already_loaded(self):
        import odoo.addons.aurora.models.pipeline as pm
        pm._k8s_config_loaded = True
        with patch("odoo.addons.aurora.models.pipeline.k8s_config") as mock_cfg:
            pm._load_k8s_config()
            mock_cfg.load_incluster_config.assert_not_called()
        pm._k8s_config_loaded = False

    def test_load_k8s_config_double_check_after_lock(self):
        import odoo.addons.aurora.models.pipeline as pm
        pm._k8s_config_loaded = False
        with patch("odoo.addons.aurora.models.pipeline.k8s_config") as mock_cfg:
            mock_cfg.load_incluster_config.return_value = None
            pm._load_k8s_config()
            pm._load_k8s_config()
            self.assertEqual(mock_cfg.load_incluster_config.call_count, 1)
        pm._k8s_config_loaded = False

    def test_load_k8s_config_thread_safety(self):
        import odoo.addons.aurora.models.pipeline as pm
        pm._k8s_config_loaded = False
        with patch("odoo.addons.aurora.models.pipeline.k8s_config") as mock_cfg:
            mock_cfg.load_incluster_config.return_value = None
            import threading
            threads = [threading.Thread(target=pm._load_k8s_config) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertTrue(pm._k8s_config_loaded)
        pm._k8s_config_loaded = False

    def test_load_k8s_config_lock_exists(self):
        import odoo.addons.aurora.models.pipeline as pm
        import threading
        self.assertIsInstance(pm._k8s_config_lock, type(threading.Lock()))


class TestSafeGithubNameRegex(unittest.TestCase):
    """Tests 30-47: _SAFE_GITHUB_NAME regex pattern."""

    def test_simple_name_matches(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("my-repo"))

    def test_alphanumeric_matches(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("repo123"))

    def test_dots_allowed(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("my.repo.name"))

    def test_underscores_allowed(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("my_repo_name"))

    def test_hyphens_allowed(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("my-repo-name"))

    def test_mixed_valid_chars(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("My_Repo-123.v2"))

    def test_slash_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("org/repo"))

    def test_space_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("my repo"))

    def test_empty_string_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match(""))

    def test_unicode_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("r\u00e9po"))

    def test_at_sign_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("user@repo"))

    def test_colon_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("repo:branch"))

    def test_path_traversal_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("../etc/passwd"))

    def test_single_char_matches(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("a"))

    def test_very_long_name_matches(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        long_name = "a" * 200
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match(long_name))

    def test_newline_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("repo\nname"))

    def test_hash_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("repo#1"))

    def test_uppercase_allowed(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("MyRepo"))


class TestValidateFilePath(unittest.TestCase):
    """Tests 48-55: _validate_file_path security checks."""

    def test_valid_path_within_base(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "output.txt")
            with open(test_file, "w") as f:
                f.write("test")
            result = _validate_file_path(test_file, tmpdir)
            self.assertTrue(result.startswith(os.path.realpath(tmpdir)))

    def test_traversal_raises_error(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        from odoo.exceptions import UserError
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = os.path.join(tmpdir, "..", "..", "etc", "shadow")
            with self.assertRaises(UserError):
                _validate_file_path(bad_path, tmpdir)

    def test_absolute_outside_raises_error(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        from odoo.exceptions import UserError
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(UserError):
                _validate_file_path("/etc/passwd", tmpdir)

    def test_base_path_itself_is_allowed(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _validate_file_path(tmpdir, tmpdir)
            self.assertEqual(result, os.path.realpath(tmpdir))

    def test_symlink_resolved(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        from odoo.exceptions import UserError
        with tempfile.TemporaryDirectory() as tmpdir:
            target = "/etc/hosts"
            link = os.path.join(tmpdir, "link")
            os.symlink(target, link)
            with self.assertRaises(UserError):
                _validate_file_path(link, tmpdir)

    def test_double_traversal_rejected(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        from odoo.exceptions import UserError
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "sub")
            os.makedirs(subdir)
            bad = os.path.join(subdir, "..", "..", "outside")
            with self.assertRaises(UserError):
                _validate_file_path(bad, tmpdir)

    def test_root_path_rejected(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        from odoo.exceptions import UserError
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(UserError):
                _validate_file_path("/", tmpdir)

    def test_returns_realpath(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "a", "b")
            os.makedirs(subdir)
            result = _validate_file_path(subdir, tmpdir)
            self.assertEqual(result, os.path.realpath(subdir))


class TestStepSelection(unittest.TestCase):
    """Tests 56-63: STEP_SELECTION list structure."""

    def test_step_selection_is_list(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        self.assertIsInstance(STEP_SELECTION, list)

    def test_step_selection_has_expected_count(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        self.assertEqual(len(STEP_SELECTION), 15)

    def test_step_selection_all_tuples(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        for item in STEP_SELECTION:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)

    def test_step_selection_starts_with_draft(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        self.assertEqual(STEP_SELECTION[0][0], "draft")

    def test_step_selection_ends_with_failed(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        self.assertEqual(STEP_SELECTION[-1][0], "failed")

    def test_step_selection_contains_done(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        self.assertIn("done", keys)

    def test_step_selection_contains_fetch_prs(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        self.assertIn("fetch_prs", keys)

    def test_step_selection_ordering_draft_before_done(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        self.assertLess(keys.index("draft"), keys.index("done"))


class TestTerminalStates(unittest.TestCase):
    """Tests 64-69: TERMINAL_STATES membership."""

    def test_done_is_terminal(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertIn("done", TERMINAL_STATES)

    def test_failed_is_terminal(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertIn("failed", TERMINAL_STATES)

    def test_draft_not_terminal(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertNotIn("draft", TERMINAL_STATES)

    def test_running_not_terminal(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertNotIn("running", TERMINAL_STATES)

    def test_terminal_states_is_set(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertIsInstance(TERMINAL_STATES, set)

    def test_terminal_states_count(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertEqual(len(TERMINAL_STATES), 2)


class TestAutomationStatus(unittest.TestCase):
    """Tests 70-75: AUTOMATION_STATUS list."""

    def test_automation_status_is_list(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        self.assertIsInstance(AUTOMATION_STATUS, list)

    def test_automation_status_has_idle(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("idle", keys)

    def test_automation_status_has_running(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("running", keys)

    def test_automation_status_has_done(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("done", keys)

    def test_automation_status_has_failed(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("failed", keys)

    def test_automation_status_count(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        self.assertEqual(len(AUTOMATION_STATUS), 4)


class TestBuildWorkerOdooConf(unittest.TestCase):
    """Tests 76-85: AuroraPipeline._build_worker_odoo_conf."""

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_contains_options_header(self, mock_config):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("[options]", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_contains_addons_path(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("addons_path = /opt/odoo/addons,/opt/odoo/custom_addons", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_contains_data_dir(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/custom/data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("data_dir = /custom/data", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_default_data_dir(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": None,
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("data_dir = /tmp/odoo-data", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_contains_db_host(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("db_host = False", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_contains_without_demo(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("without_demo = all", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_server_wide_modules_from_list(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": ["base", "web", "aurora"],
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("server_wide_modules = base,web,aurora", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_server_wide_modules_from_string(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("server_wide_modules = base,web", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_server_wide_modules_none_default(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": None,
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("server_wide_modules = base,web", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config")
    def test_conf_contains_admin_passwd(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj._build_worker_odoo_conf = AuroraPipeline._build_worker_odoo_conf.__get__(obj)
        result = obj._build_worker_odoo_conf()
        self.assertIn("admin_passwd = False", result)


class TestCreatePipelineSecret(unittest.TestCase):
    """Tests 86-91: AuroraPipeline._create_pipeline_secret."""

    @patch("odoo.addons.aurora.models.pipeline.odoo_config", {"db_password": "dbpass"})
    @patch("odoo.addons.aurora.models.pipeline._get_env", return_value="secret_val")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_creates_secret_successfully(self, mock_k8s, mock_env):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 42
        obj._create_pipeline_secret = AuroraPipeline._create_pipeline_secret.__get__(obj)
        core_v1 = MagicMock()
        core_v1.create_namespaced_secret.return_value = None
        result = obj._create_pipeline_secret(core_v1, {"app": "test"})
        self.assertEqual(result, "aurora-pipeline-creds-42")

    @patch("odoo.addons.aurora.models.pipeline.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.pipeline._get_env", return_value="val")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    @patch("odoo.addons.aurora.models.pipeline.K8sApiException", new_callable=lambda: type("K8sApiException", (Exception,), {"__init__": lambda self, status=None, reason=None: [setattr(self, 'status', status), setattr(self, 'reason', reason)] and None}))
    def test_handles_409_conflict(self, MockExc, mock_k8s, mock_env):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 7
        obj._create_pipeline_secret = AuroraPipeline._create_pipeline_secret.__get__(obj)
        core_v1 = MagicMock()
        exc = MockExc(status=409)
        core_v1.create_namespaced_secret.side_effect = exc
        with patch("odoo.addons.aurora.models.pipeline.K8sApiException", MockExc):
            result = obj._create_pipeline_secret(core_v1, {})
        core_v1.replace_namespaced_secret.assert_called_once()
        self.assertEqual(result, "aurora-pipeline-creds-7")

    @patch("odoo.addons.aurora.models.pipeline.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.pipeline._get_env", return_value="v")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_secret_name_includes_pipeline_id(self, mock_k8s, mock_env):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 99
        obj._create_pipeline_secret = AuroraPipeline._create_pipeline_secret.__get__(obj)
        core_v1 = MagicMock()
        result = obj._create_pipeline_secret(core_v1, {})
        self.assertIn("99", result)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.pipeline._get_env", return_value="v")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_secret_data_has_db_password(self, mock_k8s, mock_env):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 1
        obj._create_pipeline_secret = AuroraPipeline._create_pipeline_secret.__get__(obj)
        core_v1 = MagicMock()
        obj._create_pipeline_secret(core_v1, {})
        call_kwargs = mock_k8s.V1Secret.call_args
        string_data = call_kwargs[1]["string_data"] if "string_data" in (call_kwargs[1] or {}) else call_kwargs.kwargs.get("string_data")
        self.assertIn("DB_PASSWORD", string_data)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.pipeline._get_env", return_value="v")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_secret_data_has_encryption_key(self, mock_k8s, mock_env):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 1
        obj._create_pipeline_secret = AuroraPipeline._create_pipeline_secret.__get__(obj)
        core_v1 = MagicMock()
        obj._create_pipeline_secret(core_v1, {})
        call_kwargs = mock_k8s.V1Secret.call_args
        string_data = call_kwargs[1].get("string_data") or call_kwargs.kwargs.get("string_data")
        self.assertIn("AURORA_ENCRYPTION_KEY", string_data)

    @patch("odoo.addons.aurora.models.pipeline.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.pipeline._get_env", return_value="v")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_secret_data_has_s3_keys(self, mock_k8s, mock_env):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 1
        obj._create_pipeline_secret = AuroraPipeline._create_pipeline_secret.__get__(obj)
        core_v1 = MagicMock()
        obj._create_pipeline_secret(core_v1, {})
        call_kwargs = mock_k8s.V1Secret.call_args
        string_data = call_kwargs[1].get("string_data") or call_kwargs.kwargs.get("string_data")
        self.assertIn("AURORA_S3_ACCESS_KEY", string_data)
        self.assertIn("AURORA_S3_SECRET_KEY", string_data)


class TestCreateWorkerConfigmap(unittest.TestCase):
    """Tests 92-96: AuroraPipeline._create_worker_configmap."""

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_creates_configmap_successfully(self, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 10
        obj._build_worker_odoo_conf = MagicMock(return_value="[options]\n")
        obj._create_worker_configmap = AuroraPipeline._create_worker_configmap.__get__(obj)
        core_v1 = MagicMock()
        result = obj._create_worker_configmap(core_v1, {"app": "test"})
        self.assertEqual(result, "aurora-worker-config-10")

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_configmap_name_includes_id(self, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 55
        obj._build_worker_odoo_conf = MagicMock(return_value="conf")
        obj._create_worker_configmap = AuroraPipeline._create_worker_configmap.__get__(obj)
        core_v1 = MagicMock()
        result = obj._create_worker_configmap(core_v1, {})
        self.assertIn("55", result)

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_configmap_data_has_odoo_conf(self, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 1
        obj._build_worker_odoo_conf = MagicMock(return_value="[options]\nfoo=bar\n")
        obj._create_worker_configmap = AuroraPipeline._create_worker_configmap.__get__(obj)
        core_v1 = MagicMock()
        obj._create_worker_configmap(core_v1, {})
        call_kwargs = mock_k8s.V1ConfigMap.call_args
        data = call_kwargs[1].get("data") or call_kwargs.kwargs.get("data")
        self.assertIn("odoo.conf", data)

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    @patch("odoo.addons.aurora.models.pipeline.K8sApiException", new_callable=lambda: type("K8sApiException", (Exception,), {"__init__": lambda self, status=None, reason=None: [setattr(self, 'status', status), setattr(self, 'reason', reason)] and None}))
    def test_configmap_409_replaces(self, MockExc, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 3
        obj._build_worker_odoo_conf = MagicMock(return_value="conf")
        obj._create_worker_configmap = AuroraPipeline._create_worker_configmap.__get__(obj)
        core_v1 = MagicMock()
        exc = MockExc(status=409)
        core_v1.create_namespaced_config_map.side_effect = exc
        with patch("odoo.addons.aurora.models.pipeline.K8sApiException", MockExc):
            result = obj._create_worker_configmap(core_v1, {})
        core_v1.replace_namespaced_config_map.assert_called_once()
        self.assertEqual(result, "aurora-worker-config-3")

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    @patch("odoo.addons.aurora.models.pipeline.K8sApiException", new_callable=lambda: type("K8sApiException", (Exception,), {"__init__": lambda self, status=None, reason=None: [setattr(self, 'status', status), setattr(self, 'reason', reason)] and None}))
    def test_configmap_other_error_raises(self, MockExc, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 3
        obj._build_worker_odoo_conf = MagicMock(return_value="conf")
        obj._create_worker_configmap = AuroraPipeline._create_worker_configmap.__get__(obj)
        core_v1 = MagicMock()
        exc = MockExc(status=500)
        core_v1.create_namespaced_config_map.side_effect = exc
        with patch("odoo.addons.aurora.models.pipeline.K8sApiException", MockExc):
            with self.assertRaises(MockExc):
                obj._create_worker_configmap(core_v1, {})


class TestDeleteWorkerConfigmap(unittest.TestCase):
    """Tests 97-100: AuroraPipeline._delete_worker_configmap."""

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_delete_configmap_calls_api(self, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 20
        obj._delete_worker_configmap = AuroraPipeline._delete_worker_configmap.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        obj._delete_worker_configmap()
        mock_core.delete_namespaced_config_map.assert_called_once()

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_delete_configmap_handles_exception(self, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 21
        obj._delete_worker_configmap = AuroraPipeline._delete_worker_configmap.__get__(obj)
        mock_core = MagicMock()
        mock_core.delete_namespaced_config_map.side_effect = Exception("gone")
        mock_k8s.CoreV1Api.return_value = mock_core
        # Should not raise
        obj._delete_worker_configmap()
        self.assertIsNone(None)

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_delete_configmap_name_format(self, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 33
        obj._delete_worker_configmap = AuroraPipeline._delete_worker_configmap.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        obj._delete_worker_configmap()
        call_kwargs = mock_core.delete_namespaced_config_map.call_args
        self.assertIn("aurora-worker-config-33", str(call_kwargs))

    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_delete_configmap_uses_namespace(self, mock_k8s):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline, NAMESPACE
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 5
        obj._delete_worker_configmap = AuroraPipeline._delete_worker_configmap.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        obj._delete_worker_configmap()
        call_kwargs = mock_core.delete_namespaced_config_map.call_args
        self.assertIn(NAMESPACE, str(call_kwargs))


class TestDeletePipelineSecret(unittest.TestCase):
    """Tests 101-105: AuroraPipeline._delete_pipeline_secret."""

    @patch("odoo.addons.aurora.models.pipeline._load_k8s_config")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_delete_secret_calls_api(self, mock_k8s, mock_load):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 15
        obj._delete_pipeline_secret = AuroraPipeline._delete_pipeline_secret.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        obj._delete_pipeline_secret()
        mock_core.delete_namespaced_secret.assert_called_once()

    @patch("odoo.addons.aurora.models.pipeline._load_k8s_config")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    @patch("odoo.addons.aurora.models.pipeline.K8sApiException", new_callable=lambda: type("K8sApiException", (Exception,), {"__init__": lambda self, status=None, reason=None: [setattr(self, 'status', status), setattr(self, 'reason', reason)] and None}))
    def test_delete_secret_handles_404(self, MockExc, mock_k8s, mock_load):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 16
        obj._delete_pipeline_secret = AuroraPipeline._delete_pipeline_secret.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        exc = MockExc(status=404)
        mock_core.delete_namespaced_secret.side_effect = exc
        with patch("odoo.addons.aurora.models.pipeline.K8sApiException", MockExc):
            obj._delete_pipeline_secret()
        self.assertIsNone(None)

    @patch("odoo.addons.aurora.models.pipeline._load_k8s_config")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    @patch("odoo.addons.aurora.models.pipeline.K8sApiException", new_callable=lambda: type("K8sApiException", (Exception,), {"__init__": lambda self, status=None, reason=None: [setattr(self, 'status', status), setattr(self, 'reason', reason)] and None}))
    def test_delete_secret_handles_other_k8s_error(self, MockExc, mock_k8s, mock_load):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 17
        obj._delete_pipeline_secret = AuroraPipeline._delete_pipeline_secret.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        exc = MockExc(status=500)
        mock_core.delete_namespaced_secret.side_effect = exc
        with patch("odoo.addons.aurora.models.pipeline.K8sApiException", MockExc):
            obj._delete_pipeline_secret()
        self.assertTrue(True)

    @patch("odoo.addons.aurora.models.pipeline._load_k8s_config")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_delete_secret_handles_generic_exception(self, mock_k8s, mock_load):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 18
        obj._delete_pipeline_secret = AuroraPipeline._delete_pipeline_secret.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        mock_core.delete_namespaced_secret.side_effect = RuntimeError("network error")
        obj._delete_pipeline_secret()
        self.assertTrue(True)

    @patch("odoo.addons.aurora.models.pipeline._load_k8s_config")
    @patch("odoo.addons.aurora.models.pipeline.k8s_client")
    def test_delete_secret_name_format(self, mock_k8s, mock_load):
        from odoo.addons.aurora.models.pipeline import AuroraPipeline
        obj = MagicMock(spec=AuroraPipeline)
        obj.id = 77
        obj._delete_pipeline_secret = AuroraPipeline._delete_pipeline_secret.__get__(obj)
        mock_core = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_core
        obj._delete_pipeline_secret()
        call_kwargs = mock_core.delete_namespaced_secret.call_args
        self.assertIn("aurora-pipeline-creds-77", str(call_kwargs))


class TestEvalConstants(unittest.TestCase):
    """Tests 106-119: Evaluation infrastructure constants."""

    def test_eval_namespace(self):
        from odoo.addons.aurora.models.evaluation import EVAL_NAMESPACE
        self.assertEqual(EVAL_NAMESPACE, "aurora")

    def test_eval_node_selector_key(self):
        from odoo.addons.aurora.models.evaluation import EVAL_NODE_SELECTOR
        self.assertIn("ethara.ai/node-pool", EVAL_NODE_SELECTOR)

    def test_eval_node_selector_value(self):
        from odoo.addons.aurora.models.evaluation import EVAL_NODE_SELECTOR
        self.assertEqual(EVAL_NODE_SELECTOR["ethara.ai/node-pool"], "general-purpose")

    def test_eval_service_account(self):
        from odoo.addons.aurora.models.evaluation import EVAL_SERVICE_ACCOUNT
        self.assertEqual(EVAL_SERVICE_ACCOUNT, "aurora-worker")

    def test_eval_docker_image_contains_worker(self):
        from odoo.addons.aurora.models.evaluation import EVAL_DOCKER_IMAGE
        self.assertIn("aurora-worker", EVAL_DOCKER_IMAGE)

    def test_eval_dind_image(self):
        from odoo.addons.aurora.models.evaluation import EVAL_DIND_IMAGE
        self.assertEqual(EVAL_DIND_IMAGE, "docker:27-dind")

    def test_eval_binfmt_image(self):
        from odoo.addons.aurora.models.evaluation import EVAL_BINFMT_IMAGE
        self.assertEqual(EVAL_BINFMT_IMAGE, "tonistiigi/binfmt:latest")

    def test_eval_cpu_request(self):
        from odoo.addons.aurora.models.evaluation import EVAL_CPU_REQUEST
        self.assertEqual(EVAL_CPU_REQUEST, "2")

    def test_eval_memory_request(self):
        from odoo.addons.aurora.models.evaluation import EVAL_MEMORY_REQUEST
        self.assertEqual(EVAL_MEMORY_REQUEST, "4Gi")

    def test_eval_memory_limit(self):
        from odoo.addons.aurora.models.evaluation import EVAL_MEMORY_LIMIT
        self.assertEqual(EVAL_MEMORY_LIMIT, "8Gi")

    def test_eval_deadline_seconds(self):
        from odoo.addons.aurora.models.evaluation import EVAL_DEADLINE_SECONDS
        self.assertEqual(EVAL_DEADLINE_SECONDS, 28800)

    def test_eval_kueue_queue(self):
        from odoo.addons.aurora.models.evaluation import EVAL_KUEUE_QUEUE
        self.assertEqual(EVAL_KUEUE_QUEUE, "aurora-pipelines")

    def test_eval_worker_script(self):
        from odoo.addons.aurora.models.evaluation import EVAL_WORKER_SCRIPT
        self.assertEqual(EVAL_WORKER_SCRIPT, "/opt/odoo/custom_addons/aurora/worker/run_evaluation.py")

    def test_eval_odoo_conf_path(self):
        from odoo.addons.aurora.models.evaluation import EVAL_ODOO_CONF_PATH
        self.assertEqual(EVAL_ODOO_CONF_PATH, "/etc/odoo/odoo.conf")


class TestEvalStageSelection(unittest.TestCase):
    """Tests 120-126: EVAL_STAGE_SELECTION structure."""

    def test_eval_stage_is_list(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        self.assertIsInstance(EVAL_STAGE_SELECTION, list)

    def test_eval_stage_has_draft(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [s[0] for s in EVAL_STAGE_SELECTION]
        self.assertIn("draft", keys)

    def test_eval_stage_has_building_images(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [s[0] for s in EVAL_STAGE_SELECTION]
        self.assertIn("building_images", keys)

    def test_eval_stage_has_running_instances(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [s[0] for s in EVAL_STAGE_SELECTION]
        self.assertIn("running_instances", keys)

    def test_eval_stage_has_generating_reports(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [s[0] for s in EVAL_STAGE_SELECTION]
        self.assertIn("generating_reports", keys)

    def test_eval_stage_has_done(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [s[0] for s in EVAL_STAGE_SELECTION]
        self.assertIn("done", keys)

    def test_eval_stage_has_failed(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [s[0] for s in EVAL_STAGE_SELECTION]
        self.assertIn("failed", keys)


class TestEvalTerminalStates(unittest.TestCase):
    """Tests 127-132: EVAL_TERMINAL_STATES membership."""

    def test_eval_done_is_terminal(self):
        from odoo.addons.aurora.models.evaluation import EVAL_TERMINAL_STATES
        self.assertIn("done", EVAL_TERMINAL_STATES)

    def test_eval_failed_is_terminal(self):
        from odoo.addons.aurora.models.evaluation import EVAL_TERMINAL_STATES
        self.assertIn("failed", EVAL_TERMINAL_STATES)

    def test_eval_draft_not_terminal(self):
        from odoo.addons.aurora.models.evaluation import EVAL_TERMINAL_STATES
        self.assertNotIn("draft", EVAL_TERMINAL_STATES)

    def test_eval_building_not_terminal(self):
        from odoo.addons.aurora.models.evaluation import EVAL_TERMINAL_STATES
        self.assertNotIn("building_images", EVAL_TERMINAL_STATES)

    def test_eval_terminal_is_set(self):
        from odoo.addons.aurora.models.evaluation import EVAL_TERMINAL_STATES
        self.assertIsInstance(EVAL_TERMINAL_STATES, set)

    def test_eval_terminal_count(self):
        from odoo.addons.aurora.models.evaluation import EVAL_TERMINAL_STATES
        self.assertEqual(len(EVAL_TERMINAL_STATES), 2)


class TestEvalStatus(unittest.TestCase):
    """Tests 133-137: EVAL_STATUS list."""

    def test_eval_status_is_list(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STATUS
        self.assertIsInstance(EVAL_STATUS, list)

    def test_eval_status_has_idle(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STATUS
        keys = [s[0] for s in EVAL_STATUS]
        self.assertIn("idle", keys)

    def test_eval_status_has_running(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STATUS
        keys = [s[0] for s in EVAL_STATUS]
        self.assertIn("running", keys)

    def test_eval_status_has_done(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STATUS
        keys = [s[0] for s in EVAL_STATUS]
        self.assertIn("done", keys)

    def test_eval_status_has_failed(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STATUS
        keys = [s[0] for s in EVAL_STATUS]
        self.assertIn("failed", keys)


class TestResolveEntryNumber(unittest.TestCase):
    """Tests 138-145: AuroraEvaluation._resolve_entry_number."""

    def test_resolve_int_number(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({"number": 42})
        self.assertEqual(result, 42)

    def test_resolve_str_digit_number(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({"number": "123"})
        self.assertEqual(result, 123)

    def test_resolve_str_with_dash(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({"number": "456-suffix"})
        self.assertEqual(result, 456)

    def test_resolve_from_pr_numbers_list(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({"pr_numbers": [789, 101]})
        self.assertEqual(result, 789)

    def test_resolve_returns_none_no_data(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({})
        self.assertIsNone(result)

    def test_resolve_returns_none_empty_pr_numbers(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({"pr_numbers": []})
        self.assertIsNone(result)

    def test_resolve_non_digit_string_falls_to_pr_numbers(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({"number": "abc", "pr_numbers": [55]})
        self.assertEqual(result, 55)

    def test_resolve_none_number_field(self):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        result = AuroraEvaluation._resolve_entry_number({"number": None, "pr_numbers": []})
        self.assertIsNone(result)


class TestBuildEvalOdooConf(unittest.TestCase):
    """Tests 146-150: AuroraEvaluation._build_eval_odoo_conf."""

    @patch("odoo.addons.aurora.models.evaluation.odoo_config")
    def test_eval_conf_contains_options(self, mock_config):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        obj = MagicMock(spec=AuroraEvaluation)
        obj._build_eval_odoo_conf = AuroraEvaluation._build_eval_odoo_conf.__get__(obj)
        result = obj._build_eval_odoo_conf()
        self.assertIn("[options]", result)

    @patch("odoo.addons.aurora.models.evaluation.odoo_config")
    def test_eval_conf_addons_path(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj._build_eval_odoo_conf = AuroraEvaluation._build_eval_odoo_conf.__get__(obj)
        result = obj._build_eval_odoo_conf()
        self.assertIn("addons_path = /opt/odoo/addons,/opt/odoo/custom_addons", result)

    @patch("odoo.addons.aurora.models.evaluation.odoo_config")
    def test_eval_conf_data_dir_custom(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/my/data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj._build_eval_odoo_conf = AuroraEvaluation._build_eval_odoo_conf.__get__(obj)
        result = obj._build_eval_odoo_conf()
        self.assertIn("data_dir = /my/data", result)

    @patch("odoo.addons.aurora.models.evaluation.odoo_config")
    def test_eval_conf_server_wide_modules_list(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": ["base", "web", "aurora"],
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj._build_eval_odoo_conf = AuroraEvaluation._build_eval_odoo_conf.__get__(obj)
        result = obj._build_eval_odoo_conf()
        self.assertIn("server_wide_modules = base,web,aurora", result)

    @patch("odoo.addons.aurora.models.evaluation.odoo_config")
    def test_eval_conf_db_port(self, mock_config):
        mock_config.get.side_effect = lambda k, *a: {
            "data_dir": "/tmp/odoo-data",
            "server_wide_modules": "base,web",
        }.get(k, a[0] if a else None)
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj._build_eval_odoo_conf = AuroraEvaluation._build_eval_odoo_conf.__get__(obj)
        result = obj._build_eval_odoo_conf()
        self.assertIn("db_port = 5432", result)


class TestCreateEvalSecret(unittest.TestCase):
    """Tests 151-153: AuroraEvaluation._create_eval_secret."""

    @patch("odoo.addons.aurora.models.evaluation.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.evaluation._get_env", return_value="secret_val")
    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    def test_creates_eval_secret(self, mock_k8s, mock_env):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj.id = 8
        obj._create_eval_secret = AuroraEvaluation._create_eval_secret.__get__(obj)
        core_v1 = MagicMock()
        result = obj._create_eval_secret(core_v1, {"app": "eval"})
        self.assertEqual(result, "aurora-eval-creds-8")

    @patch("odoo.addons.aurora.models.evaluation.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.evaluation._get_env", return_value="v")
    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    @patch("odoo.addons.aurora.models.evaluation.K8sApiException", new_callable=lambda: type("K8sApiException", (Exception,), {"__init__": lambda self, status=None, reason=None: [setattr(self, 'status', status), setattr(self, 'reason', reason)] and None}))
    def test_eval_secret_409_replaces(self, MockExc, mock_k8s, mock_env):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj.id = 9
        obj._create_eval_secret = AuroraEvaluation._create_eval_secret.__get__(obj)
        core_v1 = MagicMock()
        exc = MockExc(status=409)
        core_v1.create_namespaced_secret.side_effect = exc
        with patch("odoo.addons.aurora.models.evaluation.K8sApiException", MockExc):
            result = obj._create_eval_secret(core_v1, {})
        core_v1.replace_namespaced_secret.assert_called_once()
        self.assertEqual(result, "aurora-eval-creds-9")

    @patch("odoo.addons.aurora.models.evaluation.odoo_config", {"db_password": "dbpass", "db_host": "localhost", "db_port": "5432", "db_user": "odoo", "data_dir": "/tmp", "server_wide_modules": "base,web"})
    @patch("odoo.addons.aurora.models.evaluation._get_env", return_value="v")
    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    def test_eval_secret_name_format(self, mock_k8s, mock_env):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj.id = 44
        obj._create_eval_secret = AuroraEvaluation._create_eval_secret.__get__(obj)
        core_v1 = MagicMock()
        result = obj._create_eval_secret(core_v1, {})
        self.assertIn("44", result)
        self.assertTrue(result.startswith("aurora-eval-creds-"))


class TestCreateEvalConfigmap(unittest.TestCase):
    """Tests 154-155: AuroraEvaluation._create_eval_configmap."""

    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    def test_creates_eval_configmap(self, mock_k8s):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj.id = 12
        obj._build_eval_odoo_conf = MagicMock(return_value="[options]\n")
        obj._create_eval_configmap = AuroraEvaluation._create_eval_configmap.__get__(obj)
        core_v1 = MagicMock()
        result = obj._create_eval_configmap(core_v1, {"app": "eval"})
        self.assertEqual(result, "aurora-eval-config-12")

    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    @patch("odoo.addons.aurora.models.evaluation.K8sApiException", new_callable=lambda: type("K8sApiException", (Exception,), {"__init__": lambda self, status=None, reason=None: [setattr(self, 'status', status), setattr(self, 'reason', reason)] and None}))
    def test_eval_configmap_409_replaces(self, MockExc, mock_k8s):
        from odoo.addons.aurora.models.evaluation import AuroraEvaluation
        obj = MagicMock(spec=AuroraEvaluation)
        obj.id = 13
        obj._build_eval_odoo_conf = MagicMock(return_value="conf")
        obj._create_eval_configmap = AuroraEvaluation._create_eval_configmap.__get__(obj)
        core_v1 = MagicMock()
        exc = MockExc(status=409)
        core_v1.create_namespaced_config_map.side_effect = exc
        with patch("odoo.addons.aurora.models.evaluation.K8sApiException", MockExc):
            result = obj._create_eval_configmap(core_v1, {})
        core_v1.replace_namespaced_config_map.assert_called_once()
        self.assertEqual(result, "aurora-eval-config-13")


if __name__ == "__main__":
    unittest.main()
