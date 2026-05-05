# -*- coding: utf-8 -*-
import json
from unittest.mock import MagicMock, patch, call

from odoo.tests import tagged

from .common import KenseiTestCase

_MOD_KENSEI = "odoo.addons.kensei.models.kensei"
_MOD_SANDBOX = "odoo.addons.kensei.models.kensei_sandbox"


def _fake_registry_cursor(env_stub):
    cr_mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cr_mock)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, cr_mock


def _build_env(task_exists=True, task_vals=None, sandbox_exists=True, sandbox_vals=None):
    task_vals = task_vals or {}
    sandbox_vals = sandbox_vals or {}

    task_mock = MagicMock()
    task_mock.exists.return_value = task_exists
    for k, v in task_vals.items():
        setattr(task_mock, k, v)
    for field in (
        "claude_trajectory", "glm_trajectory", "oneP_trajectory",
        "seed_prompt", "golden_input_tokens", "golden_output_tokens",
        "taskdesc_input_tokens", "taskdesc_output_tokens",
        "golden_status", "golden_error", "golden_started_at",
        "task_description", "task_description_status", "task_description_error",
    ):
        if not hasattr(task_mock, field) or field not in task_vals:
            setattr(task_mock, field, task_vals.get(field, ""))

    if "persona_id" not in task_vals:
        persona_mock = MagicMock()
        persona_mock.soul_md = "soul"
        persona_mock.memory_md = "memory"
        persona_mock.agents_md = "agents"
        task_mock.persona_id = persona_mock

    sandbox_mock = MagicMock()
    sandbox_mock.exists.return_value = sandbox_exists
    sandbox_mock.model_type = sandbox_vals.get("model_type", "claude")
    sandbox_mock.docker_status = sandbox_vals.get("docker_status", "running")
    sandbox_mock.docker_error = sandbox_vals.get("docker_error", "")
    partner_employee = MagicMock()
    sandbox_mock.employee_id.user_id.partner_id = partner_employee

    bus_mock = MagicMock()

    partner_mock = MagicMock()
    partner_mock.exists.return_value = True

    icp_mock = MagicMock()

    def _icp_get(key):
        mapping = {
            "kensei.bedrock_inference_arn": "arn:aws:bedrock:us-east-1:123:inference/abc",
            "kensei.bedrock_region": "us-east-1",
        }
        return mapping.get(key, "")
    icp_mock.get_param = _icp_get

    icp_sudo = MagicMock()
    icp_sudo.get_param = _icp_get

    icp_model = MagicMock()
    icp_model.sudo.return_value = icp_sudo

    env = MagicMock()

    def _env_getitem(model_name):
        if model_name == "kensei.kensei":
            kensei_model = MagicMock()
            kensei_model.browse.return_value = task_mock
            return kensei_model
        if model_name == "kensei.sandbox":
            sb_model = MagicMock()
            sb_model.browse.return_value = sandbox_mock
            return sb_model
        if model_name == "bus.bus":
            return bus_mock
        if model_name == "res.partner":
            partner_model = MagicMock()
            partner_model.browse.return_value = partner_mock
            return partner_model
        if model_name == "ir.config_parameter":
            return icp_model
        return MagicMock()

    env.__getitem__ = _env_getitem
    return env, task_mock, sandbox_mock, bus_mock, partner_mock


def _patch_registry_and_env(module_path, env, extra_patches=None):
    patches = {}

    cr_mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cr_mock)
    cm.__exit__ = MagicMock(return_value=False)

    reg_instance = MagicMock()
    reg_instance.cursor.return_value = cm

    patches["Registry"] = patch(module_path + ".Registry", return_value=reg_instance)
    patches["api_env"] = patch(
        module_path + ".api.Environment", return_value=env
    )

    if extra_patches:
        patches.update(extra_patches)
    return patches


@tagged("post_install", "-at_install")
class TestRunGoldenGenerationBackground(KenseiTestCase):

    def _call_target(self, db_name="testdb", task_id=1, partner_id=10):
        from odoo.addons.kensei.models.kensei import _run_golden_generation_background
        _run_golden_generation_background(db_name, task_id, partner_id)

    @patch(_MOD_KENSEI + "._get_golden_prompt", return_value="golden system prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    @patch(_MOD_KENSEI + ".get_module_path", return_value="/fake/kensei")
    @patch(_MOD_KENSEI + ".os.path.isfile", return_value=False)
    def test_golden_gen_success(self, _isfile, _modpath, _dotenv, _prompt):
        env, task_mock, _, bus_mock, partner_mock = _build_env(
            task_vals={
                "claude_trajectory": "traj-claude",
                "glm_trajectory": "traj-glm",
                "golden_input_tokens": 100,
                "golden_output_tokens": 50,
            }
        )
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        bedrock_patch = patch(
            _MOD_KENSEI + "._call_bedrock_converse",
            return_value=("golden output text", {"input_tokens": 200, "output_tokens": 300}),
        )
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], bedrock_patch, lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                return_value=("golden output text", {"input_tokens": 200, "output_tokens": 300}),
            ):
                self._call_target()

        task_mock.write.assert_called()
        write_calls = task_mock.write.call_args_list
        success_write = None
        for c in write_calls:
            args = c[0][0] if c[0] else c[1]
            if isinstance(args, dict) and args.get("golden_status") == "done":
                success_write = args
                break
        self.assertIsNotNone(success_write, "Expected a write with golden_status='done'")
        self.assertEqual(success_write["golden_trajectory"], "golden output text")
        self.assertEqual(success_write["golden_status"], "done")

    def test_golden_gen_task_not_exists(self):
        env, task_mock, _, _, _ = _build_env(task_exists=False)
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target()
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            self.assertNotIn("golden_trajectory", args)

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": ""})
    def test_golden_gen_no_api_key(self, _dotenv):
        env, task_mock, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_set = set()
        lock_set.add(1)
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=lock_set)
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(task_id=1)
        error_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("golden_status") == "error":
                error_write = args
                break
        self.assertIsNotNone(error_write, "Expected golden_status='error' write")
        self.assertIn("not set", error_write.get("golden_error", ""))

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    def test_golden_gen_no_inference_arn(self, _dotenv):
        env, task_mock, _, _, _ = _build_env()
        icp_sudo = MagicMock()
        icp_sudo.get_param = lambda k: ""
        icp_model = MagicMock()
        icp_model.sudo.return_value = icp_sudo

        orig_getitem = env.__getitem__

        def _patched_getitem(name):
            if name == "ir.config_parameter":
                return icp_model
            return orig_getitem(name)

        env.__getitem__ = _patched_getitem

        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target()

        error_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("golden_status") == "error":
                error_write = args
                break
        self.assertIsNotNone(error_write, "Expected golden_status='error' write")
        self.assertIn("ARN", error_write.get("golden_error", ""))

    @patch(_MOD_KENSEI + "._get_golden_prompt", return_value="prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    @patch(_MOD_KENSEI + ".get_module_path", return_value="/fake/kensei")
    @patch(_MOD_KENSEI + ".os.path.isfile", return_value=False)
    def test_golden_gen_bedrock_failure(self, _isfile, _modpath, _dotenv, _prompt):
        env, task_mock, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        bedrock_err = RuntimeError("Bedrock timeout")
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                side_effect=bedrock_err,
            ):
                self._call_target()

        error_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("golden_status") == "error":
                error_write = args
                break
        self.assertIsNotNone(error_write, "Expected golden_status='error'")
        self.assertIn("Bedrock timeout", error_write.get("golden_error", ""))

    @patch(_MOD_KENSEI + "._get_golden_prompt", return_value="prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    @patch(_MOD_KENSEI + ".get_module_path", return_value="/fake/kensei")
    @patch(_MOD_KENSEI + ".os.path.isfile", return_value=False)
    def test_golden_gen_sends_bus_notification(self, _isfile, _modpath, _dotenv, _prompt):
        env, task_mock, _, bus_mock, partner_mock = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                return_value=("result", {"input_tokens": 10, "output_tokens": 20}),
            ):
                self._call_target(partner_id=99)

        bus_mock._sendone.assert_called()
        send_call = bus_mock._sendone.call_args
        self.assertEqual(send_call[0][1], "kensei/golden_ready")
        self.assertEqual(send_call[0][2]["status"], "done")

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": ""})
    def test_golden_gen_cleans_up_lock(self, _dotenv):
        env, _, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_set = set()
        lock_set.add(42)
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=lock_set)
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(task_id=42)
        self.assertNotIn(42, lock_set)

    @patch(_MOD_KENSEI + "._get_golden_prompt", return_value="prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    @patch(_MOD_KENSEI + ".get_module_path", return_value="/fake/kensei")
    @patch(_MOD_KENSEI + ".os.path.isfile", return_value=False)
    def test_golden_gen_tokens_accumulated(self, _isfile, _modpath, _dotenv, _prompt):
        env, task_mock, _, _, _ = _build_env(
            task_vals={"golden_input_tokens": 50, "golden_output_tokens": 25}
        )
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                return_value=("text", {"input_tokens": 100, "output_tokens": 200}),
            ):
                self._call_target()

        success_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("golden_status") == "done":
                success_write = args
                break
        self.assertIsNotNone(success_write)
        self.assertEqual(success_write["golden_input_tokens"], 150)
        self.assertEqual(success_write["golden_output_tokens"], 225)

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": ""})
    def test_golden_gen_error_sends_bus_notification(self, _dotenv):
        env, task_mock, _, bus_mock, partner_mock = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._GOLDEN_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(partner_id=99)

        bus_mock._sendone.assert_called()
        send_call = bus_mock._sendone.call_args
        self.assertEqual(send_call[0][1], "kensei/golden_ready")
        self.assertEqual(send_call[0][2]["status"], "error")


@tagged("post_install", "-at_install")
class TestRunTaskDescriptionBackground(KenseiTestCase):

    def _call_target(self, db_name="testdb", task_id=1, partner_id=10):
        from odoo.addons.kensei.models.kensei import _run_task_description_background
        _run_task_description_background(db_name, task_id, partner_id)

    @patch(_MOD_KENSEI + "._get_taskdesc_prompt", return_value="taskdesc system prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    def test_taskdesc_bg_success(self, _dotenv, _prompt):
        env, task_mock, _, bus_mock, _ = _build_env(
            task_vals={
                "claude_trajectory": "traj-c",
                "glm_trajectory": "traj-g",
                "oneP_trajectory": "traj-1p",
                "seed_prompt": "seed",
                "taskdesc_input_tokens": 10,
                "taskdesc_output_tokens": 5,
            }
        )
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                return_value=("Generated task description", {"input_tokens": 50, "output_tokens": 100}),
            ):
                self._call_target()

        success_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("task_description_status") == "done":
                success_write = args
                break
        self.assertIsNotNone(success_write, "Expected task_description_status='done'")
        self.assertEqual(success_write["task_description"], "Generated task description")

    def test_taskdesc_bg_task_not_exists(self):
        env, task_mock, _, _, _ = _build_env(task_exists=False)
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target()
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            self.assertNotIn("task_description", args)

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": ""})
    def test_taskdesc_bg_no_credentials_api_key(self, _dotenv):
        env, task_mock, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target()

        error_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("task_description_status") == "error":
                error_write = args
                break
        self.assertIsNotNone(error_write, "Expected task_description_status='error'")
        self.assertIn("not set", error_write.get("task_description_error", ""))

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    def test_taskdesc_bg_no_credentials_arn(self, _dotenv):
        env, task_mock, _, _, _ = _build_env()
        icp_sudo = MagicMock()
        icp_sudo.get_param = lambda k: ""
        icp_model = MagicMock()
        icp_model.sudo.return_value = icp_sudo

        orig_getitem = env.__getitem__

        def _patched(name):
            if name == "ir.config_parameter":
                return icp_model
            return orig_getitem(name)

        env.__getitem__ = _patched

        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target()

        error_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("task_description_status") == "error":
                error_write = args
                break
        self.assertIsNotNone(error_write)
        self.assertIn("ARN", error_write.get("task_description_error", ""))

    @patch(_MOD_KENSEI + "._get_taskdesc_prompt", return_value="prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    def test_taskdesc_bg_failure(self, _dotenv, _prompt):
        env, task_mock, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                side_effect=RuntimeError("GLM service down"),
            ):
                self._call_target()

        error_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("task_description_status") == "error":
                error_write = args
                break
        self.assertIsNotNone(error_write)
        self.assertIn("GLM service down", error_write.get("task_description_error", ""))

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": ""})
    def test_taskdesc_bg_cleans_up_lock(self, _dotenv):
        env, _, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_set = set()
        lock_set.add(7)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=lock_set)
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(task_id=7)
        self.assertNotIn(7, lock_set)

    @patch(_MOD_KENSEI + "._get_taskdesc_prompt", return_value="prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    def test_taskdesc_bg_sends_bus_notification(self, _dotenv, _prompt):
        env, task_mock, _, bus_mock, partner_mock = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                return_value=("desc text", {"input_tokens": 5, "output_tokens": 10}),
            ):
                self._call_target(partner_id=55)

        bus_mock._sendone.assert_called()
        send_call = bus_mock._sendone.call_args
        self.assertEqual(send_call[0][1], "kensei/taskdesc_ready")
        self.assertEqual(send_call[0][2]["status"], "done")

    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": ""})
    def test_taskdesc_bg_error_sends_bus_notification(self, _dotenv):
        env, task_mock, _, bus_mock, partner_mock = _build_env()
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(partner_id=55)

        bus_mock._sendone.assert_called()
        send_call = bus_mock._sendone.call_args
        self.assertEqual(send_call[0][1], "kensei/taskdesc_ready")
        self.assertEqual(send_call[0][2]["status"], "error")

    @patch(_MOD_KENSEI + "._get_taskdesc_prompt", return_value="prompt")
    @patch(_MOD_KENSEI + "._load_dotenv", return_value={"AWS_BEARER_TOKEN_BEDROCK": "key123"})
    def test_taskdesc_bg_tokens_accumulated(self, _dotenv, _prompt):
        env, task_mock, _, _, _ = _build_env(
            task_vals={"taskdesc_input_tokens": 20, "taskdesc_output_tokens": 10}
        )
        patches = _patch_registry_and_env(_MOD_KENSEI, env)
        lock_patch = patch(_MOD_KENSEI + "._TASKDESC_GENERATING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            with patch(
                "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                return_value=("desc", {"input_tokens": 30, "output_tokens": 40}),
            ):
                self._call_target()

        success_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("task_description_status") == "done":
                success_write = args
                break
        self.assertIsNotNone(success_write)
        self.assertEqual(success_write["taskdesc_input_tokens"], 50) 
        self.assertEqual(success_write["taskdesc_output_tokens"], 50)


@tagged("post_install", "-at_install")
class TestRunSandboxStartBackground(KenseiTestCase):

    def _call_target(self, db_name="testdb", sandbox_id=1, mode="local", partner_id=10):
        from odoo.addons.kensei.models.kensei_sandbox import _run_sandbox_start_background
        _run_sandbox_start_background(db_name, sandbox_id, mode, partner_id)

    def test_sandbox_start_bg_success_local(self):
        env, _, sandbox_mock, bus_mock, partner_mock = _build_env(
            sandbox_vals={"docker_status": "running"}
        )
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(mode="local")

        sandbox_mock._start_local_bg.assert_called_once()
        sandbox_mock._start_k8s_bg.assert_not_called()

    def test_sandbox_start_bg_success_k8s(self):
        env, _, sandbox_mock, bus_mock, partner_mock = _build_env(
            sandbox_vals={"docker_status": "running"}
        )
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(mode="k8s")

        sandbox_mock._start_k8s_bg.assert_called_once()
        sandbox_mock._start_local_bg.assert_not_called()

    def test_sandbox_start_bg_sandbox_not_exists(self):
        env, _, sandbox_mock, _, _ = _build_env(sandbox_exists=False)
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target()

        sandbox_mock._start_local_bg.assert_not_called()
        sandbox_mock._start_k8s_bg.assert_not_called()

    def test_sandbox_start_bg_error_writes_status(self):
        env, _, sandbox_mock, _, _ = _build_env(
            sandbox_vals={"docker_status": "starting"}
        )
        sandbox_mock._start_local_bg.side_effect = RuntimeError("Docker crashed")
        sandbox_mock.docker_status = "starting"

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(mode="local")

        write_called = False
        for c in sandbox_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and args.get("docker_status") == "error":
                write_called = True
                self.assertIn("Docker crashed", args.get("docker_error", ""))
                break
        self.assertTrue(write_called, "Expected docker_status='error' write")

    def test_sandbox_start_bg_sends_notification(self):
        env, _, sandbox_mock, bus_mock, partner_mock = _build_env(
            sandbox_vals={"docker_status": "running"}
        )
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(partner_id=77)

        bus_mock._sendone.assert_called()
        send_call = bus_mock._sendone.call_args
        self.assertEqual(send_call[0][1], "kensei/sandbox_ready")
        self.assertEqual(send_call[0][2]["sandbox_id"], 1)
        self.assertEqual(send_call[0][2]["docker_status"], "running")

    def test_sandbox_start_bg_cleans_up_lock(self):
        env, _, sandbox_mock, _, _ = _build_env(sandbox_exists=False)
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_set = set()
        lock_set.add(99)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=lock_set)
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(sandbox_id=99)
        self.assertNotIn(99, lock_set)

    def test_sandbox_start_bg_cleans_up_lock_on_exception(self):
        env, _, sandbox_mock, _, _ = _build_env()
        sandbox_mock._start_local_bg.side_effect = RuntimeError("crash")
        sandbox_mock.docker_status = "error"

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_set = set()
        lock_set.add(88)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=lock_set)
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(sandbox_id=88, mode="local")
        self.assertNotIn(88, lock_set)

    def test_sandbox_start_bg_fallback_employee_partner(self):
        env, _, sandbox_mock, bus_mock, _ = _build_env(
            sandbox_vals={"docker_status": "running"}
        )
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(partner_id=0)

        bus_mock._sendone.assert_called()

    def test_sandbox_start_bg_captures_model_type(self):
        env, _, sandbox_mock, bus_mock, partner_mock = _build_env(
            sandbox_vals={"docker_status": "running", "model_type": "glm"}
        )
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        lock_patch = patch(_MOD_SANDBOX + "._SANDBOX_STARTING", new=set())
        with patches["Registry"], patches["api_env"], lock_patch:
            self._call_target(partner_id=10)

        send_call = bus_mock._sendone.call_args
        self.assertEqual(send_call[0][2]["model_type"], "glm")


@tagged("post_install", "-at_install")
class TestInjectTaskDescriptionBg(KenseiTestCase):

    def _call_target(self, db_name="testdb", task_id=1, field_name="claude_trajectory",
                     seed_prompt="seed", messages=None, entry_index=-1):
        from odoo.addons.kensei.models.kensei_sandbox import _inject_task_description_bg
        _inject_task_description_bg(
            db_name, task_id, field_name, seed_prompt,
            messages or [{"role": "user", "content": "hi"}],
            entry_index,
        )

    def _make_traj_data(self, task_desc_status="generating", has_trajectory=True):
        entry = {
            "session_id": "sess-001",
            "timestamp": "2026-01-01",
            "task_description_status": task_desc_status,
        }
        if has_trajectory:
            entry["trajectory"] = {
                "meta_info": {"task_type": "home_and_organization"},
                "messages": [],
            }
        return [entry]

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_success(self, mock_gen):
        mock_gen.return_value = ("A generated description", {"input_tokens": 10, "output_tokens": 20})

        traj_data = self._make_traj_data()
        env, task_mock, _, _, _ = _build_env(
            task_vals={
                "claude_trajectory": json.dumps(traj_data),
                "taskdesc_input_tokens": 0,
                "taskdesc_output_tokens": 0,
            }
        )
        task_mock.__getitem__ = lambda self_mock, key: getattr(self_mock, key)

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        write_called = False
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and "claude_trajectory" in args:
                written_data = json.loads(args["claude_trajectory"])
                self.assertIsInstance(written_data, list)
                meta = written_data[-1]["trajectory"]["meta_info"]
                self.assertEqual(meta["task_description"], "A generated description")
                self.assertEqual(written_data[-1]["task_description_status"], "done")
                write_called = True
                break
        self.assertTrue(write_called, "Expected write with injected description")

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    @patch(_MOD_SANDBOX + "._mark_task_description_status")
    def test_inject_taskdesc_empty_desc(self, mock_mark, mock_gen):
        mock_gen.return_value = ("", {"input_tokens": 0, "output_tokens": 0})

        env, task_mock, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        mock_mark.assert_called_once_with("testdb", 1, "claude_trajectory", "done", -1)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_dict_data(self, mock_gen):
        mock_gen.return_value = ("Dict description", {"input_tokens": 5, "output_tokens": 10})

        dict_data = {
            "meta_info": {"task_type": "research_and_analysis"},
            "messages": [],
        }
        env, task_mock, _, _, _ = _build_env(
            task_vals={
                "claude_trajectory": json.dumps(dict_data),
                "taskdesc_input_tokens": 0,
                "taskdesc_output_tokens": 0,
            }
        )
        task_mock.__getitem__ = lambda self_mock, key: getattr(self_mock, key)

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        write_called = False
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and "claude_trajectory" in args:
                written_data = json.loads(args["claude_trajectory"])
                self.assertIsInstance(written_data, dict)
                self.assertEqual(
                    written_data["meta_info"]["task_description"],
                    "Dict description",
                )
                self.assertEqual(
                    written_data["meta_info"]["task_completion_status"],
                    "success",
                )
                write_called = True
                break
        self.assertTrue(write_called)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_aborted_skipped(self, mock_gen):
        mock_gen.return_value = ("Some desc", {"input_tokens": 5, "output_tokens": 5})

        traj_data = self._make_traj_data(task_desc_status="aborted")
        env, task_mock, _, _, _ = _build_env(
            task_vals={
                "claude_trajectory": json.dumps(traj_data),
                "taskdesc_input_tokens": 0,
                "taskdesc_output_tokens": 0,
            }
        )
        task_mock.__getitem__ = lambda self_mock, key: getattr(self_mock, key)

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and "claude_trajectory" in args:
                written_data = json.loads(args["claude_trajectory"])
                if isinstance(written_data, list):
                    meta = written_data[-1].get("trajectory", {}).get("meta_info", {})
                    self.assertNotIn("task_description", meta)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_accumulates_tokens(self, mock_gen):
        mock_gen.return_value = ("desc", {"input_tokens": 100, "output_tokens": 200})

        traj_data = self._make_traj_data()
        env, task_mock, _, _, _ = _build_env(
            task_vals={
                "claude_trajectory": json.dumps(traj_data),
                "taskdesc_input_tokens": 50,
                "taskdesc_output_tokens": 25,
            }
        )
        task_mock.__getitem__ = lambda self_mock, key: getattr(self_mock, key)

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        token_write = None
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and "taskdesc_input_tokens" in args:
                token_write = args
                break
        self.assertIsNotNone(token_write)
        self.assertEqual(token_write["taskdesc_input_tokens"], 150) 
        self.assertEqual(token_write["taskdesc_output_tokens"], 225)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync", side_effect=RuntimeError("LLM error"))
    @patch(_MOD_SANDBOX + "._mark_task_description_status")
    def test_inject_taskdesc_exception_marks_done(self, mock_mark, mock_gen):
        env, task_mock, _, _, _ = _build_env()
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        mock_mark.assert_called_once_with("testdb", 1, "claude_trajectory", "done", -1)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_task_not_exists(self, mock_gen):
        mock_gen.return_value = ("desc", {"input_tokens": 10, "output_tokens": 20})

        env, task_mock, _, _, _ = _build_env(task_exists=False)
        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            self.assertNotIn("claude_trajectory", args)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_empty_field(self, mock_gen):
        mock_gen.return_value = ("desc", {"input_tokens": 10, "output_tokens": 20})

        env, task_mock, _, _, _ = _build_env(
            task_vals={
                "claude_trajectory": "",
                "taskdesc_input_tokens": 0,
                "taskdesc_output_tokens": 0,
            }
        )
        task_mock.__getitem__ = lambda self_mock, key: getattr(self_mock, key)

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target()

        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            self.assertNotIn("claude_trajectory", args)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_specific_entry_index(self, mock_gen):
        mock_gen.return_value = ("Desc for entry 0", {"input_tokens": 5, "output_tokens": 5})

        traj_data = [
            {
                "session_id": "sess-001",
                "task_description_status": "generating",
                "trajectory": {"meta_info": {}, "messages": []},
            },
            {
                "session_id": "sess-002",
                "task_description_status": "done",
                "trajectory": {"meta_info": {"task_description": "existing"}, "messages": []},
            },
        ]
        env, task_mock, _, _, _ = _build_env(
            task_vals={
                "claude_trajectory": json.dumps(traj_data),
                "taskdesc_input_tokens": 0,
                "taskdesc_output_tokens": 0,
            }
        )
        task_mock.__getitem__ = lambda self_mock, key: getattr(self_mock, key)

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target(entry_index=0)

        write_called = False
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and "claude_trajectory" in args:
                written_data = json.loads(args["claude_trajectory"])
                self.assertEqual(
                    written_data[0]["trajectory"]["meta_info"]["task_description"],
                    "Desc for entry 0",
                )
                self.assertEqual(written_data[0]["task_description_status"], "done")
                self.assertEqual(
                    written_data[1]["trajectory"]["meta_info"]["task_description"],
                    "existing",
                )
                write_called = True
                break
        self.assertTrue(write_called)

    @patch(_MOD_SANDBOX + ".generate_task_description_sync")
    def test_inject_taskdesc_uses_field_name(self, mock_gen):
        mock_gen.return_value = ("Desc for glm", {"input_tokens": 5, "output_tokens": 5})

        traj_data = self._make_traj_data()
        env, task_mock, _, _, _ = _build_env(
            task_vals={
                "glm_trajectory": json.dumps(traj_data),
                "taskdesc_input_tokens": 0,
                "taskdesc_output_tokens": 0,
            }
        )
        task_mock.__getitem__ = lambda self_mock, key: getattr(self_mock, key)

        patches = _patch_registry_and_env(_MOD_SANDBOX, env)
        with patches["Registry"], patches["api_env"]:
            self._call_target(field_name="glm_trajectory")

        write_called = False
        for c in task_mock.write.call_args_list:
            args = c[0][0] if c[0] else {}
            if isinstance(args, dict) and "glm_trajectory" in args:
                write_called = True
                break
        self.assertTrue(write_called, "Expected write to glm_trajectory field")
