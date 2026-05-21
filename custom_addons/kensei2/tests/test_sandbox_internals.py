# -*- coding: utf-8 -*-
import json
import subprocess
import threading
from unittest.mock import MagicMock, call, mock_open, patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import Kensei2TestCase


@tagged("post_install", "-at_install")
class TestCheckLocalStatus(Kensei2TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sandbox = cls.claude_sandbox

    def setUp(self):
        super().setUp()
        self.sandbox.write({
            "docker_compose_project": "kensei2-test-proj",
            "docker_status": "running",
            "docker_error": False,
        })

    def _mock_compose_ps(self, stdout="", returncode=0, side_effect=None):
        if side_effect:
            return patch(
                "odoo.addons.kensei2.models.kensei2_sandbox.subprocess.run",
                side_effect=side_effect,
            )
        mock_result = MagicMock()
        mock_result.stdout = stdout
        mock_result.returncode = returncode
        return patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.subprocess.run",
            return_value=mock_result,
        )

    def test_skip_when_sandbox_starting(self):
        from odoo.addons.kensei2.models.kensei2_sandbox import (
            _SANDBOX_LOCK,
            _SANDBOX_STARTING,
        )

        with _SANDBOX_LOCK:
            _SANDBOX_STARTING.add(self.sandbox.id)
        try:
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox.subprocess.run"
            ) as mock_run:
                self.sandbox._check_local_status()
                mock_run.assert_not_called()
        finally:
            with _SANDBOX_LOCK:
                _SANDBOX_STARTING.discard(self.sandbox.id)

    def test_no_compose_project(self):
        self.sandbox.write({
            "docker_compose_project": False,
            "docker_status": "running",
        })
        self.sandbox._check_local_status()
        self.assertEqual(self.sandbox.docker_status, "stopped")

    def test_container_exited(self):
        ps_json = json.dumps({"State": "exited", "Health": ""})
        with self._mock_compose_ps(stdout=ps_json):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch("os.path.isdir", return_value=True):
                    self.sandbox.write({"docker_status": "running"})
                    self.sandbox._check_local_status()
                    self.assertEqual(self.sandbox.docker_status, "error")

    def test_unhealthy_during_startup(self):
        ps_json = json.dumps({"State": "running", "Health": "unhealthy"})
        with self._mock_compose_ps(stdout=ps_json):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch("os.path.isdir", return_value=True):
                    self.sandbox.write({"docker_status": "starting"})
                    self.sandbox._check_local_status()
                    self.assertEqual(self.sandbox.docker_status, "starting")

    def test_unhealthy_after_running(self):
        ps_json = json.dumps({"State": "running", "Health": "unhealthy"})
        with self._mock_compose_ps(stdout=ps_json):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch("os.path.isdir", return_value=True):
                    self.sandbox.write({"docker_status": "running"})
                    self.sandbox._check_local_status()
                    self.assertEqual(self.sandbox.docker_status, "error")

    def test_healthy_running(self):
        ps_json = json.dumps({"State": "running", "Health": "healthy"})
        with self._mock_compose_ps(stdout=ps_json):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch("os.path.isdir", return_value=True):
                    self.sandbox.write({"docker_status": "starting"})
                    self.sandbox._check_local_status()
                    self.assertEqual(self.sandbox.docker_status, "running")

    def test_health_starting(self):
        ps_json = json.dumps({"State": "running", "Health": "starting"})
        with self._mock_compose_ps(stdout=ps_json):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch("os.path.isdir", return_value=True):
                    self.sandbox.write({"docker_status": "starting"})
                    self.sandbox._check_local_status()
                    self.assertEqual(self.sandbox.docker_status, "starting")

    def test_no_output(self):
        with self._mock_compose_ps(stdout=""):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch("os.path.isdir", return_value=True):
                    self.sandbox.write({"docker_status": "starting"})
                    self.sandbox._check_local_status()
                    self.assertEqual(self.sandbox.docker_status, "starting")

    def test_timeout_exception(self):
        with self._mock_compose_ps(
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10)
        ):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch("os.path.isdir", return_value=True):
                    self.sandbox.write({"docker_status": "starting"})
                    self.sandbox._check_local_status()
                    self.assertEqual(self.sandbox.docker_status, "starting")


@tagged("post_install", "-at_install")
class TestWaitForHealth(Kensei2TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sandbox = cls.claude_sandbox
        cls.sandbox.write({"docker_port": 21999})

    def _compose_bin(self):
        return ["docker", "compose"]

    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.sleep")
    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.monotonic")
    def test_success(self, mock_mono, mock_sleep):
        mock_mono.side_effect = [0, 1]

        mock_run_result = MagicMock()
        mock_run_result.stdout = json.dumps({"State": "running", "Health": "healthy"})
        mock_run_result.returncode = 0

        with patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.subprocess.run",
            return_value=mock_run_result,
        ):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox.urllib.request.urlopen"
            ) as mock_urlopen:
                mock_urlopen.return_value = MagicMock()
                result = self.sandbox._wait_for_health(
                    self._compose_bin(), "test-proj", "/tmp/workdir"
                )
        self.assertTrue(result)

    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.sleep")
    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.monotonic")
    def test_timeout(self, mock_mono, mock_sleep):
        mock_mono.side_effect = [0, 999999]

        result = self.sandbox._wait_for_health(
            self._compose_bin(), "test-proj", "/tmp/workdir"
        )
        self.assertFalse(result)

    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.sleep")
    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.monotonic")
    def test_container_exited(self, mock_mono, mock_sleep):
        mock_mono.side_effect = [0, 1]

        mock_run_result = MagicMock()
        mock_run_result.stdout = json.dumps({"State": "exited", "Health": ""})
        mock_run_result.returncode = 0

        with patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.subprocess.run",
            return_value=mock_run_result,
        ):
            result = self.sandbox._wait_for_health(
                self._compose_bin(), "test-proj", "/tmp/workdir"
            )
        self.assertFalse(result)

    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.sleep")
    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.monotonic")
    def test_connection_error_retries(self, mock_mono, mock_sleep):
        mock_mono.side_effect = [0, 1, 2]

        mock_run_result = MagicMock()
        mock_run_result.stdout = json.dumps({"State": "running", "Health": "starting"})
        mock_run_result.returncode = 0

        with patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.subprocess.run",
            return_value=mock_run_result,
        ):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox.urllib.request.urlopen"
            ) as mock_urlopen:
                mock_urlopen.side_effect = [
                    ConnectionError("refused"),
                    MagicMock(),
                ]
                result = self.sandbox._wait_for_health(
                    self._compose_bin(), "test-proj", "/tmp/workdir"
                )
        self.assertTrue(result)

    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.sleep")
    @patch("odoo.addons.kensei2.models.kensei2_sandbox.time.monotonic")
    def test_json_array_format(self, mock_mono, mock_sleep):
        mock_mono.side_effect = [0, 1]

        mock_run_result = MagicMock()
        mock_run_result.stdout = json.dumps(
            [{"State": "running", "Health": "healthy"}]
        )
        mock_run_result.returncode = 0

        with patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.subprocess.run",
            return_value=mock_run_result,
        ):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox.urllib.request.urlopen"
            ) as mock_urlopen:
                mock_urlopen.return_value = MagicMock()
                result = self.sandbox._wait_for_health(
                    self._compose_bin(), "test-proj", "/tmp/workdir"
                )
        self.assertTrue(result)


@tagged("post_install", "-at_install")
class TestBuildComposeEnv(Kensei2TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sandbox = cls.claude_sandbox

    def _patch_dotenv(self, env_dict=None):
        return patch(
            "odoo.addons.kensei2.models.kensei2_sandbox._load_dotenv",
            return_value=env_dict or {},
        )

    def test_sets_persona(self):
        with self._patch_dotenv():
            env = self.sandbox._build_compose_env("tok-123")
        self.assertEqual(env["PERSONA"], self.persona.name)

    def test_generates_litellm_key(self):
        with self._patch_dotenv({}):
            env = self.sandbox._build_compose_env("tok-123")
        self.assertTrue(env["LITELLM_MASTER_KEY"].startswith("sk-kensei2-"))

    def test_sets_gog_keyring(self):
        self.task.write({"password": "secret-pw"})
        with self._patch_dotenv():
            env = self.sandbox._build_compose_env("tok-123")
        self.assertEqual(env["GOG_KEYRING_PASSWORD"], "secret-pw")
        self.task.write({"password": False})

    def test_sets_gog_account(self):
        self.task.write({"email": "test@example.com"})
        with self._patch_dotenv():
            env = self.sandbox._build_compose_env("tok-123")
        self.assertEqual(env["GOG_ACCOUNT"], "test@example.com")
        self.task.write({"email": False})


@tagged("post_install", "-at_install")
class TestPrepareWorkdir(Kensei2TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sandbox = cls.claude_sandbox

    def setUp(self):
        super().setUp()
        # Clear mock attributes so no stale references leak between tests
        # if _base_patches ExitStack fails partway through __enter__.
        self._mock_load_dotenv = None
        self._mock_module_dir = None
        self._mock_isdir = None
        self._mock_isfile = None
        self._mock_exists = None
        self._mock_makedirs = None
        self._mock_rmtree = None
        self._mock_copy2 = None
        self._mock_open = None
        self._mock_json_dump = None
        self._written_files = {}

    def _base_patches(self, source_dir="/fake/sandbox"):
        from contextlib import ExitStack

        stack = ExitStack()
        self._mock_load_dotenv = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox._load_dotenv",
            return_value={},
        ))
        self._mock_module_dir = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox._module_sandbox_dir",
            return_value=source_dir,
        ))
        self._mock_isdir = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.os.path.isdir",
            return_value=True,
        ))
        self._mock_isfile = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.os.path.isfile",
            return_value=False,
        ))
        self._mock_exists = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.os.path.exists",
            return_value=False,
        ))
        self._mock_makedirs = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.os.makedirs",
        ))
        self._mock_rmtree = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.shutil.rmtree",
        ))
        self._mock_copy2 = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.shutil.copy2",
        ))
        self._written_files = {}

        def fake_open(path, mode="r"):
            if "w" in mode:
                m = mock_open()()
                self._written_files[path] = m
                return m
            raise FileNotFoundError(path)

        self._mock_open = stack.enter_context(patch(
            "builtins.open", side_effect=fake_open,
        ))
        self._mock_json_dump = stack.enter_context(patch(
            "odoo.addons.kensei2.models.kensei2_sandbox.json.dump",
        ))
        return stack

    def test_no_sandbox_dir_raises(self):
        with patch(
            "odoo.addons.kensei2.models.kensei2_sandbox._load_dotenv", return_value={}
        ):
            with patch(
                "odoo.addons.kensei2.models.kensei2_sandbox._module_sandbox_dir",
                return_value=None,
            ):
                with self.assertRaises(UserError):
                    self.sandbox._prepare_workdir(
                        self.persona, "tok", 21000, 16000, 17432
                    )

    def test_creates_directory_structure(self):
        with self._base_patches():
            self.sandbox._prepare_workdir(
                self.persona, "tok", 21000, 16000, 17432
            )
        makedirs_paths = [c.args[0] for c in self._mock_makedirs.call_args_list]
        self.assertTrue(len(makedirs_paths) >= 4)

    def test_writes_persona_files(self):
        with self._base_patches():
            self.sandbox._prepare_workdir(
                self.persona, "tok", 21000, 16000, 17432
            )
        written_paths = list(self._written_files.keys())
        soul_paths = [p for p in written_paths if "SOUL.md" in p]
        memory_paths = [p for p in written_paths if "MEMORY.md" in p]
        agents_paths = [p for p in written_paths if "AGENTS.md" in p]
        self.assertTrue(len(soul_paths) >= 1, "SOUL.md not written")
        self.assertTrue(len(memory_paths) >= 1, "MEMORY.md not written")
        self.assertTrue(len(agents_paths) >= 1, "AGENTS.md not written")

    def test_uses_persona_compose_yaml(self):
        self.persona.write({"docker_compose_yaml": "version: '3'\nservices: {}"})
        try:
            with self._base_patches():
                self.sandbox._prepare_workdir(
                    self.persona, "tok", 21000, 16000, 17432
                )
            compose_paths = [
                p for p in self._written_files if "docker-compose.yml" in p
            ]
            self.assertTrue(len(compose_paths) >= 1)
        finally:
            self.persona.write({"docker_compose_yaml": False})

    def test_copies_bundled_compose(self):
        self.persona.write({"docker_compose_yaml": False})
        with self._base_patches() as stack:
            self._mock_isfile.return_value = True
            self.sandbox._prepare_workdir(
                self.persona, "tok", 21000, 16000, 17432
            )
        self.assertTrue(self._mock_copy2.called)

    def test_writes_openclaw_config(self):
        with self._base_patches():
            self.sandbox._prepare_workdir(
                self.persona, "tok-abc", 21000, 16000, 17432
            )
        dump_calls = self._mock_json_dump.call_args_list
        self.assertTrue(len(dump_calls) >= 1, "json.dump not called")
        config = dump_calls[0].args[0]
        self.assertIn("gateway", config)
        self.assertEqual(config["gateway"]["auth"]["token"], "tok-abc")

    def test_writes_litellm_config(self):
        with self._base_patches():
            self.sandbox._prepare_workdir(
                self.persona, "tok", 21000, 16000, 17432
            )
        litellm_paths = [p for p in self._written_files if "litellm-config.yaml" in p]
        self.assertTrue(len(litellm_paths) >= 1)

    def test_writes_nginx_conf(self):
        with self._base_patches():
            self.sandbox._prepare_workdir(
                self.persona, "my-gw-token", 21000, 16000, 17432
            )
        nginx_paths = [p for p in self._written_files if "nginx.conf" in p]
        self.assertTrue(len(nginx_paths) >= 1)
        mock_fh = self._written_files[nginx_paths[0]]
        write_calls = mock_fh.write.call_args_list
        written_content = "".join(c.args[0] for c in write_calls)
        self.assertIn("my-gw-token", written_content)

    def test_writes_override(self):
        with self._base_patches():
            self.sandbox._prepare_workdir(
                self.persona, "tok", 21000, 16000, 17432
            )
        override_paths = [
            p for p in self._written_files if "docker-compose.override.yml" in p
        ]
        self.assertTrue(len(override_paths) >= 1)
        mock_fh = self._written_files[override_paths[0]]
        write_calls = mock_fh.write.call_args_list
        written_content = "".join(c.args[0] for c in write_calls)
        self.assertIn("21000", written_content)
        self.assertIn("16000", written_content)

    def test_custom_port_in_origins(self):
        with self._base_patches():
            self.sandbox._prepare_workdir(
                self.persona, "tok", 20000, 16000, 17432
            )
        dump_calls = self._mock_json_dump.call_args_list
        config = dump_calls[0].args[0]
        origins = config["gateway"]["controlUi"]["allowedOrigins"]
        self.assertIn("http://localhost:20000", origins)

    def test_gog_auth_config(self):
        gog_data = json.dumps({
            "installed": {"client_id": "test", "client_secret": "secret"},
        })
        self.task.write({"gog_auth": gog_data})
        try:
            with self._base_patches():
                self.sandbox._prepare_workdir(
                    self.persona, "tok", 21000, 16000, 17432
                )
            dump_calls = self._mock_json_dump.call_args_list
            self.assertTrue(len(dump_calls) >= 2)
        finally:
            self.task.write({"gog_auth": False})

    def test_gog_token_files(self):
        token_data = json.dumps({
            "tokens": {"token.json": '{"access_token": "abc"}'},
        })
        self.task.write({"gog_auth_token": token_data})
        try:
            with self._base_patches():
                self.sandbox._prepare_workdir(
                    self.persona, "tok", 21000, 16000, 17432
                )
            token_paths = [p for p in self._written_files if "token.json" in p]
            self.assertTrue(len(token_paths) >= 1)
        finally:
            self.task.write({"gog_auth_token": False})

    def test_default_gog_config(self):
        self.task.write({"gog_auth": False, "gog_auth_token": False})
        with self._base_patches() as stack:
            self._mock_isfile.return_value = False
            self.sandbox._prepare_workdir(
                self.persona, "tok", 21000, 16000, 17432
            )
        dump_calls = self._mock_json_dump.call_args_list
        config_calls = [
            c for c in dump_calls
            if isinstance(c.args[0], dict) and "keyring_backend" in c.args[0]
        ]
        self.assertTrue(
            len(config_calls) >= 1,
            "Default gog config.json not written",
        )

    def test_cleans_existing(self):
        with self._base_patches():
            self._mock_exists.return_value = True
            self.sandbox._prepare_workdir(
                self.persona, "tok", 21000, 16000, 17432
            )
        self.assertTrue(self._mock_rmtree.called)
