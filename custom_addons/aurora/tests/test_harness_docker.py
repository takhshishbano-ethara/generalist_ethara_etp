# -*- coding: utf-8 -*-
import os
import platform
import subprocess
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock, PropertyMock, call


class TestDockerUtilExists(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_image_found_returns_true(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import exists
        mock_dc.images.get.return_value = MagicMock()
        self.assertTrue(exists("myimage:latest"))

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_image_not_found_returns_false(self, mock_dc):
        import docker
        from odoo.addons.aurora.tools.harness.docker_util import exists
        mock_dc.images.get.side_effect = docker.errors.ImageNotFound("nope")
        self.assertFalse(exists("missing:tag"))

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_calls_get_with_name(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import exists
        exists("test/image:v1")
        mock_dc.images.get.assert_called_once_with("test/image:v1")


class TestDockerUtilBuild(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_sdk")
    def test_no_platform_uses_sdk(self, mock_sdk):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/work"), "Dockerfile", "img:v1", logger)
        mock_sdk.assert_called_once()

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_buildx")
    def test_with_platform_uses_buildx(self, mock_bx):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/work"), "Dockerfile", "img:v1", logger, platform="linux/amd64")
        mock_bx.assert_called_once()

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_buildx")
    def test_passes_buildargs(self, mock_bx):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/w"), "D", "i:v", logger, buildargs={"KEY": "VAL"}, platform="linux/arm64")
        kwargs = mock_bx.call_args[1]
        self.assertEqual(kwargs["buildargs"], {"KEY": "VAL"})

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_buildx")
    def test_passes_output_tar(self, mock_bx):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/w"), "D", "i:v", logger, platform="linux/amd64", output_tar=Path("/out.tar"))
        kwargs = mock_bx.call_args[1]
        self.assertEqual(kwargs["output_tar"], Path("/out.tar").resolve())

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_sdk")
    def test_no_platform_no_output_tar(self, mock_sdk):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/w"), "D", "i:v", logger, output_tar=Path("/x.tar"))
        mock_sdk.assert_called_once()


class TestDetectNativePlatform(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="arm64")
    def test_arm64(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/arm64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="aarch64")
    def test_aarch64(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/arm64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="x86_64")
    def test_x86_64(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/amd64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="AMD64")
    def test_amd64_upper(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/amd64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="unknown")
    def test_unknown_defaults_amd64(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/amd64")


class TestRunBuildx(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_success(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        proc = MagicMock()
        proc.stdout = iter(["line1\n", "line2\n"])
        proc.wait.return_value = 0
        mock_popen.return_value = proc
        logger = MagicMock()
        _run_buildx(["docker", "buildx", "build"], "/tmp", logger)
        self.assertTrue(logger.info.called)

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_nonzero_exit_raises(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 1
        mock_popen.return_value = proc
        logger = MagicMock()
        with self.assertRaises(RuntimeError):
            _run_buildx(["docker", "buildx", "build"], "/tmp", logger)

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_file_not_found_raises(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        mock_popen.side_effect = FileNotFoundError()
        logger = MagicMock()
        with self.assertRaises(RuntimeError):
            _run_buildx(["docker", "buildx", "build"], "/tmp", logger)

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_uses_cwd(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        mock_popen.return_value = proc
        logger = MagicMock()
        _run_buildx(["cmd"], "/my/dir", logger)
        self.assertEqual(mock_popen.call_args[1]["cwd"], "/my/dir")


class TestDockerUtilRun(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_removes_container_on_success(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b"output"
        container.wait.return_value = {"StatusCode": 0}
        mock_dc.containers.run.return_value = container
        run("img:v1", "cmd")
        container.remove.assert_called_once_with(force=True)

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_returns_output(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b"hello world"
        container.wait.return_value = {"StatusCode": 0}
        mock_dc.containers.run.return_value = container
        result = run("img", "cmd")
        self.assertEqual(result, "hello world")

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_detach_true(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b""
        container.wait.return_value = {"StatusCode": 0}
        mock_dc.containers.run.return_value = container
        run("img", "cmd")
        kwargs = mock_dc.containers.run.call_args[1]
        self.assertTrue(kwargs["detach"])

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_passes_environment(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b""
        container.wait.return_value = {"StatusCode": 0}
        mock_dc.containers.run.return_value = container
        run("img", "cmd", global_env=["VAR=val"])
        kwargs = mock_dc.containers.run.call_args[1]
        self.assertEqual(kwargs["environment"], ["VAR=val"])

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_passes_volumes(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b""
        container.wait.return_value = {"StatusCode": 0}
        mock_dc.containers.run.return_value = container
        vols = {"/host": "/container"}
        run("img", "cmd", volumes=vols)
        kwargs = mock_dc.containers.run.call_args[1]
        self.assertEqual(kwargs["volumes"], vols)


class TestCopySourceCode(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.shutil.copytree")
    @patch("odoo.addons.aurora.tools.harness.docker_util.os.path.exists", return_value=False)
    def test_copies_to_dst(self, mock_exists, mock_copy):
        from odoo.addons.aurora.tools.harness.docker_util import copy_source_code
        import tempfile
        with tempfile.TemporaryDirectory() as src_dir:
            org_dir = Path(src_dir) / "myorg" / "myrepo"
            org_dir.mkdir(parents=True)
            image = MagicMock()
            image.pr.org = "myorg"
            image.pr.repo = "myrepo"
            dst = Path(src_dir) / "dst"
            dst.mkdir()
            copy_source_code(Path(src_dir), image, dst)
            mock_copy.assert_called_once()

    def test_missing_source_raises(self):
        from odoo.addons.aurora.tools.harness.docker_util import copy_source_code
        image = MagicMock()
        image.pr.org = "no"
        image.pr.repo = "exist"
        with self.assertRaises(FileNotFoundError):
            copy_source_code(Path("/nonexistent"), image, Path("/tmp/dst"))


class TestInstanceRegistry(TestCase):

    def test_registry_is_dict(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        self.assertIsInstance(Instance._registry, dict)

    def test_register_adds_to_registry(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("test_org_unique", "test_repo_unique")
            class TestInstance(Instance):
                pass
            self.assertIn("test_org_unique/test_repo_unique", Instance._registry)
        finally:
            Instance._registry = original

    def test_register_returns_class(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("org_ret", "repo_ret")
            class MyInst(Instance):
                pass
            self.assertIs(Instance._registry["org_ret/repo_ret"], MyInst)
        finally:
            Instance._registry = original

    def test_create_raises_for_unregistered(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        pr = MagicMock(org="unknown_org_xyz", repo="unknown_repo_xyz", number=1, tag="", number_interval="")
        config = MagicMock()
        with self.assertRaises(ValueError):
            Instance.create(pr, config)

    def test_repo_name_format(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        mock_pr = MagicMock(org="myorg", repo="myrepo")
        with patch.object(Instance, "pr", new_callable=lambda: property(lambda self: mock_pr)):
            self.assertEqual(inst.repo_name, "myorg/myrepo")


class TestPipelineConstants(TestCase):

    def test_namespace(self):
        from odoo.addons.aurora.models.pipeline import NAMESPACE
        self.assertEqual(NAMESPACE, "aurora")

    def test_service_account(self):
        from odoo.addons.aurora.models.pipeline import SERVICE_ACCOUNT
        self.assertEqual(SERVICE_ACCOUNT, "aurora-worker")

    def test_s3_bucket(self):
        from odoo.addons.aurora.models.pipeline import S3_BUCKET
        self.assertEqual(S3_BUCKET, "production-grtlabs-tag")

    def test_s3_region(self):
        from odoo.addons.aurora.models.pipeline import S3_REGION
        self.assertEqual(S3_REGION, "us-east-1")

    def test_docker_image(self):
        from odoo.addons.aurora.models.pipeline import DOCKER_IMAGE
        self.assertIn("aurora-worker", DOCKER_IMAGE)

    def test_cpu_request(self):
        from odoo.addons.aurora.models.pipeline import CPU_REQUEST
        self.assertEqual(CPU_REQUEST, "1")

    def test_memory_request(self):
        from odoo.addons.aurora.models.pipeline import MEMORY_REQUEST
        self.assertEqual(MEMORY_REQUEST, "2Gi")

    def test_memory_limit(self):
        from odoo.addons.aurora.models.pipeline import MEMORY_LIMIT
        self.assertEqual(MEMORY_LIMIT, "4Gi")

    def test_deadline_seconds(self):
        from odoo.addons.aurora.models.pipeline import DEADLINE_SECONDS
        self.assertEqual(DEADLINE_SECONDS, 14400)

    def test_kueue_queue(self):
        from odoo.addons.aurora.models.pipeline import KUEUE_QUEUE
        self.assertEqual(KUEUE_QUEUE, "aurora-pipelines")

    def test_node_selector(self):
        from odoo.addons.aurora.models.pipeline import NODE_SELECTOR
        self.assertEqual(NODE_SELECTOR, {"ethara.ai/node-pool": "general-purpose"})

    def test_s3_aurora_prefix(self):
        from odoo.addons.aurora.models.pipeline import S3_AURORA_PREFIX
        self.assertEqual(S3_AURORA_PREFIX, "aurora")


class TestSafeGithubNameRegex(TestCase):

    def test_alphanumeric(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("myorg123"))

    def test_with_dot(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("my.org"))

    def test_with_hyphen(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("my-repo"))

    def test_with_underscore(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("my_repo"))

    def test_slash_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("org/repo"))

    def test_space_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("my repo"))

    def test_empty_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match(""))

    def test_at_sign_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("user@org"))

    def test_semicolon_rejected(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNone(_SAFE_GITHUB_NAME.match("org;drop"))

    def test_single_char(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        self.assertIsNotNone(_SAFE_GITHUB_NAME.match("a"))


class TestValidateFilePath(TestCase):

    def test_valid_path(self):
        import tempfile
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "file.txt")
            open(f, "w").close()
            result = _validate_file_path(f, d)
            self.assertEqual(result, os.path.realpath(f))

    def test_traversal_raises(self):
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            _validate_file_path("/etc/passwd", "/tmp/aurora_output")

    def test_base_path_itself_ok(self):
        import tempfile
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        with tempfile.TemporaryDirectory() as d:
            result = _validate_file_path(d, d)
            self.assertEqual(result, os.path.realpath(d))

    def test_parent_traversal_rejected(self):
        import tempfile
        from odoo.addons.aurora.models.pipeline import _validate_file_path
        from odoo.exceptions import UserError
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(UserError):
                _validate_file_path(os.path.join(d, "..", "etc", "passwd"), d)


class TestStepSelection(TestCase):

    def test_has_draft(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        self.assertIn("draft", keys)

    def test_has_done(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        self.assertIn("done", keys)

    def test_has_failed(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        self.assertIn("failed", keys)

    def test_has_all_6_steps(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        for step in ["fetch_prs", "filter_prs", "discover_tags", "group_prs", "fetch_issues", "build_dataset"]:
            self.assertIn(step, keys)

    def test_has_phase2_stages(self):
        from odoo.addons.aurora.models.pipeline import STEP_SELECTION
        keys = [s[0] for s in STEP_SELECTION]
        self.assertIn("phase2_build", keys)
        self.assertIn("phase2_test", keys)
        self.assertIn("phase2_report", keys)


class TestTerminalStates(TestCase):

    def test_contains_done(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertIn("done", TERMINAL_STATES)

    def test_contains_failed(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertIn("failed", TERMINAL_STATES)

    def test_exactly_two(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertEqual(len(TERMINAL_STATES), 2)

    def test_draft_not_terminal(self):
        from odoo.addons.aurora.models.pipeline import TERMINAL_STATES
        self.assertNotIn("draft", TERMINAL_STATES)


class TestGetEnv(TestCase):

    @patch.dict(os.environ, {"TEST_KEY": " value "})
    def test_strips_value(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        self.assertEqual(_get_env("TEST_KEY"), "value")

    def test_missing_key_returns_default(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        self.assertEqual(_get_env("DEFINITELY_NOT_SET_XYZ123", "fallback"), "fallback")

    @patch.dict(os.environ, {"EMPTY_KEY": ""})
    def test_empty_returns_empty(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        self.assertEqual(_get_env("EMPTY_KEY"), "")

    def test_default_is_empty_string(self):
        from odoo.addons.aurora.models.pipeline import _get_env
        self.assertEqual(_get_env("NOT_EXISTS_ABC"), "")


class TestAutomationStatus(TestCase):

    def test_has_idle(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("idle", keys)

    def test_has_running(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("running", keys)

    def test_has_done(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("done", keys)

    def test_has_failed(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        keys = [s[0] for s in AUTOMATION_STATUS]
        self.assertIn("failed", keys)

    def test_exactly_four(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        self.assertEqual(len(AUTOMATION_STATUS), 4)
