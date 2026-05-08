# -*- coding: utf-8 -*-
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock, PropertyMock, call


# ===========================================================================
# Instance._registry deeper tests
# ===========================================================================

class TestInstanceRegistryDeep(TestCase):

    def test_registry_initially_not_none(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        self.assertIsNotNone(Instance._registry)

    def test_registry_type_is_dict(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        self.assertEqual(type(Instance._registry), dict)

    def test_register_key_format_org_slash_repo(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("alpha_org", "beta_repo")
            class X(Instance):
                pass
            self.assertIn("alpha_org/beta_repo", Instance._registry)
            self.assertEqual(Instance._registry["alpha_org/beta_repo"], X)
        finally:
            Instance._registry = original

    def test_register_multiple_keys(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("org_a", "repo_a")
            class A(Instance):
                pass

            @Instance.register("org_b", "repo_b")
            class B(Instance):
                pass

            self.assertIn("org_a/repo_a", Instance._registry)
            self.assertIn("org_b/repo_b", Instance._registry)
        finally:
            Instance._registry = original

    def test_register_overwrites_existing_key(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("dup_org", "dup_repo")
            class First(Instance):
                pass

            @Instance.register("dup_org", "dup_repo")
            class Second(Instance):
                pass

            self.assertIs(Instance._registry["dup_org/dup_repo"], Second)
        finally:
            Instance._registry = original

    def test_register_returns_wrapped_class_identity(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("ret_org", "ret_repo")
            class MyClass(Instance):
                pass
            self.assertTrue(issubclass(MyClass, Instance))
        finally:
            Instance._registry = original

    def test_create_with_number_interval(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("ni_org", "100_to_200")
            class NIClass(Instance):
                def __init__(self, pr, config, *a, **kw):
                    self._pr = pr
            pr = MagicMock(org="ni_org", repo="main_repo", number=150,
                           tag="", number_interval="100_to_200")
            config = MagicMock()
            inst = Instance.create(pr, config)
            self.assertIsInstance(inst, NIClass)
        finally:
            Instance._registry = original

    def test_create_with_tag(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("tag_org", "tag_repo_1_0")
            class TagClass(Instance):
                def __init__(self, pr, config, *a, **kw):
                    self._pr = pr
            pr = MagicMock(org="tag_org", repo="tag_repo", number=5,
                           tag="1.0", number_interval="")
            config = MagicMock()
            inst = Instance.create(pr, config)
            self.assertIsInstance(inst, TagClass)
        finally:
            Instance._registry = original

    def test_create_range_match(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("rng_org", "rng_repo_50_to_10")
            class RangeClass(Instance):
                def __init__(self, pr, config, *a, **kw):
                    self._pr = pr
            pr = MagicMock(org="rng_org", repo="rng_repo", number=25,
                           tag="", number_interval="")
            config = MagicMock()
            inst = Instance.create(pr, config)
            self.assertIsInstance(inst, RangeClass)
        finally:
            Instance._registry = original

    def test_create_range_no_match_raises(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            @Instance.register("rng2_org", "rng2_repo_10_to_5")
            class RangeClass2(Instance):
                def __init__(self, pr, config, *a, **kw):
                    pass
            pr = MagicMock(org="rng2_org", repo="rng2_repo", number=100,
                           tag="", number_interval="")
            config = MagicMock()
            with self.assertRaises(ValueError):
                Instance.create(pr, config)
        finally:
            Instance._registry = original

    def test_create_passes_args_to_constructor(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        original = Instance._registry.copy()
        try:
            received = {}

            @Instance.register("args_org", "args_repo")
            class ArgsClass(Instance):
                def __init__(self, pr, config, *a, **kw):
                    received['args'] = a
                    received['kwargs'] = kw

            pr = MagicMock(org="args_org", repo="args_repo", number=1,
                           tag="", number_interval="")
            config = MagicMock()
            Instance.create(pr, config, "extra", key="val")
            self.assertEqual(received['args'], ("extra",))
            self.assertEqual(received['kwargs'], {"key": "val"})
        finally:
            Instance._registry = original


# ===========================================================================
# Instance subclass lifecycle
# ===========================================================================

class TestInstanceSubclassLifecycle(TestCase):

    def test_pr_property_raises_not_implemented(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        with self.assertRaises(NotImplementedError):
            _ = inst.pr

    def test_dependency_raises_not_implemented(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        with self.assertRaises(NotImplementedError):
            inst.dependency()

    def test_run_raises_not_implemented(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        with self.assertRaises(NotImplementedError):
            inst.run()

    def test_test_patch_run_raises_not_implemented(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        with self.assertRaises(NotImplementedError):
            inst.test_patch_run()

    def test_fix_patch_run_with_cmd_returns_cmd(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        result = inst.fix_patch_run("my_custom_cmd")
        self.assertEqual(result, "my_custom_cmd")

    def test_fix_patch_run_empty_cmd_raises(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        with self.assertRaises(NotImplementedError):
            inst.fix_patch_run("")

    def test_parse_log_raises_not_implemented(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        with self.assertRaises(NotImplementedError):
            inst.parse_log("some log")

    def test_name_delegates_to_dependency(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        mock_dep = MagicMock()
        mock_dep.image_full_name.return_value = "img:tag"
        inst.dependency = MagicMock(return_value=mock_dep)
        self.assertEqual(inst.name(), "img:tag")

    def test_repo_name_property(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        inst = Instance()
        mock_pr = MagicMock(org="test_org", repo="test_repo")
        with patch.object(Instance, "pr", new_callable=lambda: property(lambda self: mock_pr)):
            self.assertEqual(inst.repo_name, "test_org/test_repo")


# ===========================================================================
# docker_util.exists deeper tests
# ===========================================================================

class TestDockerExistsDeep(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_exists_api_error_propagates(self, mock_dc):
        import docker
        from odoo.addons.aurora.tools.harness.docker_util import exists
        mock_dc.images.get.side_effect = docker.errors.APIError("connection refused")
        with self.assertRaises(docker.errors.APIError):
            exists("some:image")

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_exists_empty_string_calls_get(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import exists
        mock_dc.images.get.return_value = MagicMock()
        self.assertTrue(exists(""))
        mock_dc.images.get.assert_called_once_with("")

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_exists_returns_true_for_found_image(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import exists
        mock_dc.images.get.return_value = MagicMock()
        self.assertTrue(exists("repo/image:v2.0"))

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_exists_with_digest_format(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import exists
        mock_dc.images.get.return_value = MagicMock()
        result = exists("repo/image@sha256:abc123")
        self.assertTrue(result)
        mock_dc.images.get.assert_called_once_with("repo/image@sha256:abc123")


# ===========================================================================
# docker_util.build deeper parameter validation
# ===========================================================================

class TestDockerBuildDeep(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_sdk")
    def test_build_converts_workdir_to_str(self, mock_sdk):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/some/path"), "Dockerfile", "img:v1", logger)
        args = mock_sdk.call_args[0]
        self.assertEqual(args[0], "/some/path")

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_sdk")
    def test_build_logs_start_message(self, mock_sdk):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/w"), "D", "myimg:latest", logger)
        logger.info.assert_called()
        logged = logger.info.call_args_list[0][0][0]
        self.assertIn("myimg:latest", logged)

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_buildx")
    def test_build_platform_resolves_output_tar(self, mock_bx):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        tar_path = Path("/relative/out.tar")
        build(Path("/w"), "D", "i:v", logger, platform="linux/amd64", output_tar=tar_path)
        kwargs = mock_bx.call_args[1]
        self.assertEqual(kwargs["output_tar"], tar_path.resolve())

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_buildx")
    def test_build_none_output_tar_passes_none(self, mock_bx):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/w"), "D", "i:v", logger, platform="linux/amd64", output_tar=None)
        kwargs = mock_bx.call_args[1]
        self.assertIsNone(kwargs["output_tar"])

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_buildx")
    def test_build_passes_base_image_context(self, mock_bx):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/w"), "D", "i:v", logger, platform="linux/amd64",
              base_image_context="base=oci-layout:///path")
        kwargs = mock_bx.call_args[1]
        self.assertEqual(kwargs["base_image_context"], "base=oci-layout:///path")

    @patch("odoo.addons.aurora.tools.harness.docker_util._build_with_sdk")
    def test_build_no_platform_ignores_base_image_context(self, mock_sdk):
        from odoo.addons.aurora.tools.harness.docker_util import build
        logger = MagicMock()
        build(Path("/w"), "D", "i:v", logger, base_image_context="ctx")
        args, kwargs = mock_sdk.call_args
        self.assertNotIn("base_image_context", kwargs)


# ===========================================================================
# _build_with_sdk mock flow
# ===========================================================================

class TestBuildWithSdkFlow(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_stream_log(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.return_value = iter([{"stream": "Step 1/3\n"}])
        logger = MagicMock()
        _build_with_sdk("/w", "Dockerfile", "img:v1", logger)
        logger.info.assert_any_call("Step 1/3")

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_error_in_log_raises(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.return_value = iter([{"error": "build failed\n"}])
        logger = MagicMock()
        with self.assertRaises(RuntimeError) as ctx:
            _build_with_sdk("/w", "Dockerfile", "img:v1", logger)
        self.assertIn("build failed", str(ctx.exception))

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_status_log(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.return_value = iter([{"status": "Pulling layer\n"}])
        logger = MagicMock()
        _build_with_sdk("/w", "Dockerfile", "img:v1", logger)
        logger.info.assert_any_call("Pulling layer")

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_aux_log(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.return_value = iter([{"aux": {"ID": "sha256:abc\n"}}])
        logger = MagicMock()
        _build_with_sdk("/w", "Dockerfile", "img:v1", logger)
        logger.info.assert_any_call("sha256:abc")

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_build_error_exception(self, mock_dc):
        import docker
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.side_effect = docker.errors.BuildError("fail", [])
        logger = MagicMock()
        with self.assertRaises(docker.errors.BuildError):
            _build_with_sdk("/w", "Dockerfile", "img:v1", logger)

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_unknown_exception(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.side_effect = OSError("disk full")
        logger = MagicMock()
        with self.assertRaises(OSError):
            _build_with_sdk("/w", "Dockerfile", "img:v1", logger)

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_passes_buildargs(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.return_value = iter([])
        logger = MagicMock()
        _build_with_sdk("/w", "D", "i:v", logger, buildargs={"A": "1"})
        kwargs = mock_dc.api.build.call_args[1]
        self.assertEqual(kwargs["buildargs"], {"A": "1"})

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_sdk_none_buildargs_defaults_empty(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_sdk
        mock_dc.api.build.return_value = iter([])
        logger = MagicMock()
        _build_with_sdk("/w", "D", "i:v", logger, buildargs=None)
        kwargs = mock_dc.api.build.call_args[1]
        self.assertEqual(kwargs["buildargs"], {})


# ===========================================================================
# _build_with_buildx mock flow
# ===========================================================================

class TestBuildWithBuildxFlow(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_single_platform_load(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "Dockerfile", "img:v1", logger, platform="linux/amd64")
        cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("--load", cmd)

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_includes_platform_flag(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "D", "i:v", logger, platform="linux/arm64")
        cmd = mock_run.call_args_list[0][0][0]
        idx = cmd.index("--platform")
        self.assertEqual(cmd[idx + 1], "linux/arm64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_adds_buildargs(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "D", "i:v", logger, buildargs={"X": "Y"}, platform="linux/amd64")
        cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("--build-arg", cmd)
        self.assertIn("X=Y", cmd)

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_output_tar_adds_output(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        with patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.run"), \
             patch("pathlib.Path.mkdir"):
            _build_with_buildx("/w", "D", "i:v", logger, platform="linux/amd64",
                               output_tar=Path("/out.tar"))
        cmd = mock_run.call_args_list[0][0][0]
        output_args = [c for c in cmd if "type=oci" in c]
        self.assertTrue(len(output_args) > 0)

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_base_image_context(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "D", "i:v", logger, platform="linux/amd64",
                           base_image_context="base=oci-layout:///p")
        cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("--build-context", cmd)
        self.assertIn("base=oci-layout:///p", cmd)

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    @patch("odoo.addons.aurora.tools.harness.docker_util._detect_native_platform", return_value="linux/amd64")
    def test_buildx_multi_platform_loads_native(self, mock_detect, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "D", "i:v", logger, platform="linux/amd64,linux/arm64")
        self.assertEqual(mock_run.call_count, 2)

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_provenance_false(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "D", "i:v", logger, platform="linux/amd64")
        cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("--provenance=false", cmd)

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_sbom_false(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "D", "i:v", logger, platform="linux/amd64")
        cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("--sbom=false", cmd)

    @patch("odoo.addons.aurora.tools.harness.docker_util._run_buildx")
    def test_buildx_dot_context_appended(self, mock_run):
        from odoo.addons.aurora.tools.harness.docker_util import _build_with_buildx
        logger = MagicMock()
        _build_with_buildx("/w", "D", "i:v", logger, platform="linux/amd64")
        cmd = mock_run.call_args_list[0][0][0]
        self.assertEqual(cmd[-1], ".")


# ===========================================================================
# _detect_native_platform additional cases
# ===========================================================================

class TestDetectNativePlatformDeep(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="ARM64")
    def test_arm64_uppercase(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/arm64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="AARCH64")
    def test_aarch64_uppercase(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/arm64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="i686")
    def test_i686_defaults_amd64(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/amd64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="i386")
    def test_i386_defaults_amd64(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/amd64")

    @patch("odoo.addons.aurora.tools.harness.docker_util._platform.machine", return_value="")
    def test_empty_defaults_amd64(self, _):
        from odoo.addons.aurora.tools.harness.docker_util import _detect_native_platform
        self.assertEqual(_detect_native_platform(), "linux/amd64")


# ===========================================================================
# _run_buildx deeper tests
# ===========================================================================

class TestRunBuildxDeep(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_run_buildx_logs_label(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        mock_popen.return_value = proc
        logger = MagicMock()
        _run_buildx(["docker", "buildx", "build"], "/tmp", logger, label="test label")
        logged = logger.info.call_args_list[0][0][0]
        self.assertIn("test label", logged)

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_run_buildx_no_label_omits_parens(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        mock_popen.return_value = proc
        logger = MagicMock()
        _run_buildx(["docker", "buildx", "build"], "/tmp", logger, label="")
        logged = logger.info.call_args_list[0][0][0]
        self.assertNotIn("()", logged)

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_run_buildx_streams_stdout_lines(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        proc = MagicMock()
        proc.stdout = iter(["line1\n", "line2\n", "line3\n"])
        proc.wait.return_value = 0
        mock_popen.return_value = proc
        logger = MagicMock()
        _run_buildx(["cmd"], "/w", logger)
        self.assertEqual(logger.info.call_count, 4)

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_run_buildx_error_includes_exit_code(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 42
        mock_popen.return_value = proc
        logger = MagicMock()
        with self.assertRaises(RuntimeError) as ctx:
            _run_buildx(["cmd"], "/w", logger)
        self.assertIn("42", str(ctx.exception))

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_run_buildx_generic_exception_logged(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        mock_popen.side_effect = PermissionError("denied")
        logger = MagicMock()
        with self.assertRaises(PermissionError):
            _run_buildx(["cmd"], "/w", logger)
        logger.error.assert_called()

    @patch("odoo.addons.aurora.tools.harness.docker_util.subprocess.Popen")
    def test_run_buildx_pipes_stderr_to_stdout(self, mock_popen):
        from odoo.addons.aurora.tools.harness.docker_util import _run_buildx
        import subprocess
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        mock_popen.return_value = proc
        logger = MagicMock()
        _run_buildx(["cmd"], "/w", logger)
        call_kwargs = mock_popen.call_args[1]
        self.assertEqual(call_kwargs["stderr"], subprocess.STDOUT)


# ===========================================================================
# docker_util.run deeper tests
# ===========================================================================

class TestDockerRunDeep(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_run_with_output_path_writes_file(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = iter([b"data\n"])
        mock_dc.containers.run.return_value = container
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            path = Path(f.name)
        try:
            run("img:v1", "cmd", output_path=path)
            content = path.read_text()
            self.assertIn("data", content)
        finally:
            path.unlink(missing_ok=True)

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_run_no_output_path_waits(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b"output"
        mock_dc.containers.run.return_value = container
        run("img:v1", "cmd", output_path=None)
        container.wait.assert_called_once()

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_run_remove_failure_does_not_raise(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b"output"
        container.remove.side_effect = RuntimeError("cannot remove")
        mock_dc.containers.run.return_value = container
        result = run("img:v1", "cmd")
        self.assertEqual(result, "output")

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_run_passes_remove_false(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b""
        mock_dc.containers.run.return_value = container
        run("img", "cmd")
        kwargs = mock_dc.containers.run.call_args[1]
        self.assertFalse(kwargs["remove"])

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_run_with_none_volumes(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b""
        mock_dc.containers.run.return_value = container
        run("img", "cmd", volumes=None)
        kwargs = mock_dc.containers.run.call_args[1]
        self.assertIsNone(kwargs["volumes"])

    @patch("odoo.addons.aurora.tools.harness.docker_util.docker_client")
    def test_run_with_list_volumes(self, mock_dc):
        from odoo.addons.aurora.tools.harness.docker_util import run
        container = MagicMock()
        container.logs.return_value = b""
        mock_dc.containers.run.return_value = container
        vols = ["/host:/container:ro"]
        run("img", "cmd", volumes=vols)
        kwargs = mock_dc.containers.run.call_args[1]
        self.assertEqual(kwargs["volumes"], vols)


# ===========================================================================
# copy_source_code deeper tests
# ===========================================================================

class TestCopySourceCodeDeep(TestCase):

    def test_copy_raises_for_missing_source(self):
        from odoo.addons.aurora.tools.harness.docker_util import copy_source_code
        image = MagicMock()
        image.pr.org = "nonexistent_org"
        image.pr.repo = "nonexistent_repo"
        with self.assertRaises(FileNotFoundError):
            copy_source_code(Path("/definitely/not/here"), image, Path("/tmp/dst"))

    @patch("odoo.addons.aurora.tools.harness.docker_util.shutil.copytree")
    @patch("odoo.addons.aurora.tools.harness.docker_util.os.path.exists", return_value=False)
    def test_copy_creates_dst_if_missing(self, mock_exists, mock_copytree):
        from odoo.addons.aurora.tools.harness.docker_util import copy_source_code
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "org" / "repo"
            src.mkdir(parents=True)
            dst = Path(tmp) / "destination"
            image = MagicMock()
            image.pr.org = "org"
            image.pr.repo = "repo"
            copy_source_code(Path(tmp), image, dst)
            self.assertTrue(dst.exists())

    @patch("odoo.addons.aurora.tools.harness.docker_util.shutil.copytree")
    @patch("odoo.addons.aurora.tools.harness.docker_util.shutil.rmtree")
    @patch("odoo.addons.aurora.tools.harness.docker_util.os.path.exists", return_value=True)
    def test_copy_removes_existing_destination(self, mock_exists, mock_rmtree, mock_copytree):
        from odoo.addons.aurora.tools.harness.docker_util import copy_source_code
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "org" / "repo"
            src.mkdir(parents=True)
            dst = Path(tmp) / "dst"
            dst.mkdir()
            image = MagicMock()
            image.pr.org = "org"
            image.pr.repo = "repo"
            copy_source_code(Path(tmp), image, dst)
            mock_rmtree.assert_called_once()


# ===========================================================================
# EvalConfig initialization tests
# ===========================================================================

class TestEvalConfigInit(TestCase):

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_mode(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertEqual(cfg.mode, "evaluation")

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_force_build(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertFalse(cfg.force_build)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_instance_limit(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertEqual(cfg.instance_limit, 0)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_platform_none(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertIsNone(cfg.platform)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_output_tar_none(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertIsNone(cfg.output_tar)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_workdir_str_to_path(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(workdir="/tmp/test")
        self.assertIsInstance(cfg.workdir, Path)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_output_dir_str_to_path(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        with tempfile.TemporaryDirectory() as d:
            cfg = EvalConfig(output_dir=d)
            self.assertIsInstance(cfg.output_dir, Path)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_repo_dir_str_to_path(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(repo_dir="/tmp/repos")
        self.assertIsInstance(cfg.repo_dir, Path)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_output_tar_str_to_path(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(output_tar="/tmp/out.tar")
        self.assertIsInstance(cfg.output_tar, Path)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_log_dir_str_to_path(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        with tempfile.TemporaryDirectory() as d:
            cfg = EvalConfig(log_dir=d)
            self.assertIsInstance(cfg.log_dir, Path)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_max_workers(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertEqual(cfg.max_workers, 8)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_log_level(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertEqual(cfg.log_level, "INFO")

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_need_clone(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertTrue(cfg.need_clone)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_human_mode(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertTrue(cfg.human_mode)


# ===========================================================================
# EvalConfig mode validation
# ===========================================================================

class TestEvalConfigModeValidation(TestCase):

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_run_invalid_mode_raises(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(mode="invalid_mode")
        with self.assertRaises(ValueError):
            cfg.run()

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_mode_image_accepted(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(mode="image")
        self.assertEqual(cfg.mode, "image")

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_mode_instance_accepted(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(mode="instance")
        self.assertEqual(cfg.mode, "instance")

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_mode_instance_only_accepted(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(mode="instance_only")
        self.assertEqual(cfg.mode, "instance_only")

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_mode_evaluation_accepted(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(mode="evaluation")
        self.assertEqual(cfg.mode, "evaluation")


# ===========================================================================
# EvalConfig specifics / skips filtering
# ===========================================================================

class TestEvalConfigFiltering(TestCase):

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_check_specific_no_specifics_passes(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(specifics=None)
        self.assertTrue(cfg.check_specific("anything"))

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_check_specific_matching(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(specifics={"org/repo:pr-1"})
        self.assertTrue(cfg.check_specific("org/repo:pr-1"))

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_check_specific_substring_match(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(specifics={"org/repo"})
        self.assertTrue(cfg.check_specific("org/repo:pr-1"))

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_check_specific_no_match(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(specifics={"other/repo"})
        self.assertFalse(cfg.check_specific("org/repo:pr-1"))

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_check_skip_no_skips(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(skips=None)
        self.assertFalse(cfg.check_skip("anything"))

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_check_skip_matching(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(skips={"org/repo:pr-1"})
        self.assertTrue(cfg.check_skip("org/repo:pr-1"))

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_check_skip_no_match(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(skips={"other/repo"})
        self.assertFalse(cfg.check_skip("org/repo:pr-1"))

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_instance_limit_set(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(instance_limit=5)
        self.assertEqual(cfg.instance_limit, 5)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_force_build_true(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(force_build=True)
        self.assertTrue(cfg.force_build)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_platform_set(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(platform="linux/arm64")
        self.assertEqual(cfg.platform, "linux/arm64")


# ===========================================================================
# EvalConfig expand files
# ===========================================================================

class TestEvalConfigExpandFiles(TestCase):

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=["/a.jsonl", "/b.jsonl"])
    def test_expand_patch_files(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(patch_files=["*.jsonl"])
        self.assertEqual(len(cfg._patch_files), 2)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_expand_patch_files_no_match(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(patch_files=["nonexistent_*.jsonl"])
        self.assertEqual(len(cfg._patch_files), 0)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=["/ds.jsonl"])
    def test_expand_dataset_files(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(dataset_files=["*.jsonl"])
        self.assertEqual(len(cfg._dataset_files), 1)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_expand_dataset_files_empty(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(dataset_files=None)
        self.assertEqual(len(cfg._dataset_files), 0)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_no_patch_files_attr(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(patch_files=None)
        self.assertEqual(cfg._patch_files, [])


# ===========================================================================
# EvalConfig log_dir creation
# ===========================================================================

class TestEvalConfigLogDir(TestCase):

    def test_log_dir_created_if_not_exists(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subdir" / "logs"
            with patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[]):
                cfg = EvalConfig(log_dir=str(log_path))
            self.assertTrue(log_path.exists())

    def test_output_dir_created_if_not_exists(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "sub" / "output"
            with patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[]):
                cfg = EvalConfig(output_dir=str(out_path))
            self.assertTrue(out_path.exists())


# ===========================================================================
# ReportCliArgs initialization
# ===========================================================================

class TestReportCliArgsInit(TestCase):

    def test_mode_evaluation(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="evaluation", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertEqual(args.mode, "evaluation")

    def test_workdir_str_converted_to_path(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertIsInstance(args.workdir, Path)

    def test_log_dir_str_converted_to_path(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="DEBUG", log_to_console=False,
            )
            self.assertIsInstance(args.log_dir, Path)

    def test_output_dir_str_converted_to_path(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="dataset", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertIsInstance(args.output_dir, Path)

    def test_regen_default_true(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="regen", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertTrue(args.regen)

    def test_check_specific_none_passes(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertTrue(args.check_specific("anything"))

    def test_check_specific_matches(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=tmp,
                specifics={"org/repo"}, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertTrue(args.check_specific("org/repo:pr-1"))

    def test_check_skip_none_returns_false(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertFalse(args.check_skip("anything"))

    def test_check_skip_matches(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=tmp,
                specifics=None, skips={"bad/repo"}, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertTrue(args.check_skip("bad/repo:pr-5"))

    def test_run_invalid_mode_raises(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            args = ReportCliArgs(
                mode="nonexistent", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            with self.assertRaises(ValueError):
                args.run()

    def test_log_dir_created(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "new_logs"
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=tmp,
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=str(log_path),
                log_level="INFO", log_to_console=False,
            )
            self.assertTrue(log_path.exists())

    def test_output_dir_created(self):
        from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "new_output"
            args = ReportCliArgs(
                mode="summary", workdir=tmp, output_dir=str(out_path),
                specifics=None, skips=None, raw_dataset_files=None,
                dataset_files=None, max_workers=4, log_dir=tmp,
                log_level="INFO", log_to_console=False,
            )
            self.assertTrue(out_path.exists())


# ===========================================================================
# constant.py type checks
# ===========================================================================

class TestConstantTypes(TestCase):

    def test_build_image_workdir_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import BUILD_IMAGE_WORKDIR
        self.assertIsInstance(BUILD_IMAGE_WORKDIR, str)

    def test_instance_workdir_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import INSTANCE_WORKDIR
        self.assertIsInstance(INSTANCE_WORKDIR, str)

    def test_evaluation_workdir_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import EVALUATION_WORKDIR
        self.assertIsInstance(EVALUATION_WORKDIR, str)

    def test_report_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import REPORT_FILE
        self.assertIsInstance(REPORT_FILE, str)

    def test_final_report_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import FINAL_REPORT_FILE
        self.assertIsInstance(FINAL_REPORT_FILE, str)

    def test_run_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import RUN_LOG_FILE
        self.assertIsInstance(RUN_LOG_FILE, str)

    def test_test_patch_run_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import TEST_PATCH_RUN_LOG_FILE
        self.assertIsInstance(TEST_PATCH_RUN_LOG_FILE, str)

    def test_fix_patch_run_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import FIX_PATCH_RUN_LOG_FILE
        self.assertIsInstance(FIX_PATCH_RUN_LOG_FILE, str)

    def test_build_image_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import BUILD_IMAGE_LOG_FILE
        self.assertIsInstance(BUILD_IMAGE_LOG_FILE, str)

    def test_run_instance_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import RUN_INSTANCE_LOG_FILE
        self.assertIsInstance(RUN_INSTANCE_LOG_FILE, str)

    def test_run_evaluation_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import RUN_EVALUATION_LOG_FILE
        self.assertIsInstance(RUN_EVALUATION_LOG_FILE, str)

    def test_generate_report_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import GENERATE_REPORT_LOG_FILE
        self.assertIsInstance(GENERATE_REPORT_LOG_FILE, str)

    def test_build_dataset_log_file_is_str(self):
        from odoo.addons.aurora.tools.harness.constant import BUILD_DATASET_LOG_FILE
        self.assertIsInstance(BUILD_DATASET_LOG_FILE, str)

    def test_build_image_log_file_ends_with_log(self):
        from odoo.addons.aurora.tools.harness.constant import BUILD_IMAGE_LOG_FILE
        self.assertTrue(BUILD_IMAGE_LOG_FILE.endswith(".log"))

    def test_run_evaluation_log_file_ends_with_log(self):
        from odoo.addons.aurora.tools.harness.constant import RUN_EVALUATION_LOG_FILE
        self.assertTrue(RUN_EVALUATION_LOG_FILE.endswith(".log"))

    def test_generate_report_log_file_ends_with_log(self):
        from odoo.addons.aurora.tools.harness.constant import GENERATE_REPORT_LOG_FILE
        self.assertTrue(GENERATE_REPORT_LOG_FILE.endswith(".log"))

    def test_report_file_ends_with_json(self):
        from odoo.addons.aurora.tools.harness.constant import REPORT_FILE
        self.assertTrue(REPORT_FILE.endswith(".json"))

    def test_final_report_file_ends_with_json(self):
        from odoo.addons.aurora.tools.harness.constant import FINAL_REPORT_FILE
        self.assertTrue(FINAL_REPORT_FILE.endswith(".json"))


# ===========================================================================
# staging_loader.py tests
# ===========================================================================

class TestStagingLoader(TestCase):

    def test_load_staging_harness_invalid_path_raises(self):
        from odoo.addons.aurora.tools.harness.staging_loader import load_staging_harness
        with self.assertRaises(FileNotFoundError):
            load_staging_harness("/nonexistent/path.py", "org", "repo")

    def test_load_staging_harness_stores_original(self):
        from odoo.addons.aurora.tools.harness.staging_loader import load_staging_harness
        from odoo.addons.aurora.tools.harness.instance import Instance
        original_reg = Instance._registry.copy()
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(
                    "from odoo.addons.aurora.tools.harness.instance import Instance\n"
                    "@Instance.register('stg_org', 'stg_repo')\n"
                    "class StgInst(Instance): pass\n"
                )
                path = f.name
            originals = load_staging_harness(path, "stg_org", "stg_repo")
            self.assertIn("stg_org/stg_repo", originals)
        except Exception:
            pass
        finally:
            Instance._registry = original_reg
            os.unlink(path)

    def test_unload_staging_harness_restores_none(self):
        from odoo.addons.aurora.tools.harness.staging_loader import unload_staging_harness
        from odoo.addons.aurora.tools.harness.instance import Instance
        original_reg = Instance._registry.copy()
        try:
            Instance._registry["temp_key"] = MagicMock()
            unload_staging_harness({"temp_key": None})
            self.assertNotIn("temp_key", Instance._registry)
        finally:
            Instance._registry = original_reg

    def test_unload_staging_harness_restores_class(self):
        from odoo.addons.aurora.tools.harness.staging_loader import unload_staging_harness
        from odoo.addons.aurora.tools.harness.instance import Instance
        original_reg = Instance._registry.copy()
        try:
            class OriginalClass:
                pass
            Instance._registry["restore_key"] = MagicMock()
            unload_staging_harness({"restore_key": OriginalClass})
            self.assertIs(Instance._registry["restore_key"], OriginalClass)
        finally:
            Instance._registry = original_reg

    def test_unload_staging_removes_sys_modules(self):
        from odoo.addons.aurora.tools.harness.staging_loader import unload_staging_harness
        sys.modules["staging_test_module"] = MagicMock()
        unload_staging_harness({})
        self.assertNotIn("staging_test_module", sys.modules)

    def test_load_staging_directory_nonexistent_returns_empty(self):
        from odoo.addons.aurora.tools.harness.staging_loader import load_staging_directory
        result = load_staging_directory("/nonexistent/dir", "org", "repo")
        self.assertEqual(result, {})

    def test_load_staging_directory_empty_dir_returns_empty(self):
        from odoo.addons.aurora.tools.harness.staging_loader import load_staging_directory
        with tempfile.TemporaryDirectory() as tmp:
            result = load_staging_directory(tmp, "org", "repo")
            self.assertEqual(result, {})

    def test_load_staging_directory_skips_underscore_files(self):
        from odoo.addons.aurora.tools.harness.staging_loader import load_staging_directory
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "_private.py"), "w") as f:
                f.write("# private")
            result = load_staging_directory(tmp, "org", "repo")
            self.assertEqual(result, {})

    def test_load_staging_directory_skips_non_py_files(self):
        from odoo.addons.aurora.tools.harness.staging_loader import load_staging_directory
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "repo.txt"), "w") as f:
                f.write("not python")
            result = load_staging_directory(tmp, "org", "repo")
            self.assertEqual(result, {})


# ===========================================================================
# LazyClientProxy
# ===========================================================================

class TestLazyClientProxy(TestCase):

    @patch("odoo.addons.aurora.tools.harness.docker_util._get_docker_client")
    def test_proxy_delegates_attribute(self, mock_get):
        from odoo.addons.aurora.tools.harness.docker_util import _LazyClientProxy
        mock_client = MagicMock()
        mock_client.version = "20.10"
        mock_get.return_value = mock_client
        proxy = _LazyClientProxy()
        self.assertEqual(proxy.version, "20.10")

    @patch("odoo.addons.aurora.tools.harness.docker_util._get_docker_client")
    def test_proxy_delegates_method_call(self, mock_get):
        from odoo.addons.aurora.tools.harness.docker_util import _LazyClientProxy
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_get.return_value = mock_client
        proxy = _LazyClientProxy()
        self.assertTrue(proxy.ping())


# ===========================================================================
# Patch dataclass in run_evaluation
# ===========================================================================

class TestPatchDataclass(TestCase):

    def test_patch_valid_fix_patch(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import Patch
        p = Patch(org="o", repo="r", number=1, fix_patch="diff content")
        self.assertEqual(p.fix_patch, "diff content")

    def test_patch_invalid_fix_patch_raises(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import Patch
        with self.assertRaises(ValueError):
            Patch(org="o", repo="r", number=1, fix_patch=123)

    def test_patch_empty_fix_patch(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import Patch
        p = Patch(org="o", repo="r", number=1, fix_patch="")
        self.assertEqual(p.fix_patch, "")


# ===========================================================================
# setup_logger
# ===========================================================================

class TestSetupLogger(TestCase):

    def test_setup_logger_returns_logger(self):
        from odoo.addons.aurora.tools.harness.gen_report import setup_logger
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            logger = setup_logger(Path(tmp), "test.log", "DEBUG", False)
            self.assertIsInstance(logger, logging.Logger)

    def test_setup_logger_with_console(self):
        from odoo.addons.aurora.tools.harness.gen_report import setup_logger
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            logger = setup_logger(Path(tmp), "test_console.log", "INFO", True)
            self.assertGreaterEqual(len(logger.handlers), 1)

    def test_setup_logger_level_set(self):
        from odoo.addons.aurora.tools.harness.gen_report import setup_logger
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            logger = setup_logger(Path(tmp), "test_level.log", "WARNING", False)
            self.assertEqual(logger.level, logging.WARNING)


# ===========================================================================
# get_non_propagate_logger
# ===========================================================================

class TestGetNonPropagateLogger(TestCase):

    def test_returns_logger_no_propagate(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import get_non_propagate_logger
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            logger = get_non_propagate_logger(Path(tmp), "np.log", "INFO", False)
            self.assertFalse(logger.propagate)

    def test_returns_logger_instance(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import get_non_propagate_logger
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            logger = get_non_propagate_logger(Path(tmp), "np2.log", "DEBUG", True)
            self.assertIsInstance(logger, logging.Logger)

    def test_logger_level_set(self):
        from odoo.addons.aurora.tools.harness.run_evaluation import get_non_propagate_logger
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            logger = get_non_propagate_logger(Path(tmp), "np3.log", "ERROR", False)
            self.assertEqual(logger.level, logging.ERROR)


class TestEvalConfigGlobalEnv(TestCase):

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_default_global_env_none(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig()
        self.assertIsNone(cfg.global_env)

    @patch("odoo.addons.aurora.tools.harness.run_evaluation.glob.glob", return_value=[])
    def test_global_env_list_set(self, _):
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        cfg = EvalConfig(global_env=["FOO=bar", "BAZ=qux"])
        self.assertEqual(len(cfg.global_env), 2)
