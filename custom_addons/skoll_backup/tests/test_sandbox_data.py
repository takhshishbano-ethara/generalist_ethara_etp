# -*- coding: utf-8 -*-
import io
import json
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import SkollTestCase


# ---------------------------------------------------------------------------
# _read_jsonl_local
# ---------------------------------------------------------------------------

@tagged("post_install", "-at_install")
class TestReadJsonlLocal(SkollTestCase):
    """Tests for SkollSandbox._read_jsonl_local."""

    def _sandbox(self, **kw):
        """Return the pre-created claude sandbox, optionally patched."""
        sb = self.claude_sandbox
        if kw:
            sb.write(kw)
        return sb

    def test_read_jsonl_local_no_workdir(self):
        """docker_workdir=None → returns []."""
        sb = self._sandbox(docker_workdir=False)
        self.assertEqual(sb._read_jsonl_local(), [])

    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.isdir")
    def test_read_jsonl_local_no_sessions_dir(self, mock_isdir):
        """Sessions dir doesn't exist → returns []."""
        sb = self._sandbox(docker_workdir="/tmp/fake")
        mock_isdir.side_effect = lambda p: p == "/tmp/fake"
        self.assertEqual(sb._read_jsonl_local(), [])

    @patch("odoo.addons.skoll.models.skoll_sandbox.os.listdir", return_value=[])
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.isdir", return_value=True)
    def test_read_jsonl_local_no_jsonl_files(self, _isdir, _listdir):
        """Empty sessions dir → returns []."""
        sb = self._sandbox(docker_workdir="/tmp/fake")
        self.assertEqual(sb._read_jsonl_local(), [])

    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.getmtime", return_value=1.0)
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.listdir",
           return_value=["a.jsonl", "b.jsonl"])
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.isdir", return_value=True)
    def test_read_jsonl_local_multiple_files(self, _isdir, _listdir, _getmtime):
        """Reads and merges from multiple .jsonl files."""
        sb = self._sandbox(docker_workdir="/tmp/fake")
        line1 = json.dumps({"type": "message", "id": "1"})
        line2 = json.dumps({"type": "message", "id": "2"})
        file_contents = {
            "a.jsonl": line1 + "\n",
            "b.jsonl": line2 + "\n",
        }

        def _open_side(path, *a, **kw):
            for key, content in file_contents.items():
                if path.endswith(key):
                    return io.StringIO(content)
            return io.StringIO("")

        with patch("builtins.open", side_effect=_open_side):
            entries = sb._read_jsonl_local()
        self.assertEqual(len(entries), 2)
        ids = {e["id"] for e in entries}
        self.assertEqual(ids, {"1", "2"})

    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.getmtime", return_value=1.0)
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.listdir",
           return_value=["x.jsonl"])
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.isdir", return_value=True)
    def test_read_jsonl_local_malformed_json_skipped(self, _isdir, _listdir, _getmtime):
        """Invalid JSON lines are skipped without error."""
        sb = self._sandbox(docker_workdir="/tmp/fake")
        content = "NOT VALID JSON\n" + json.dumps({"id": "ok"}) + "\n"
        with patch("builtins.open", return_value=io.StringIO(content)):
            entries = sb._read_jsonl_local()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "ok")

    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.getmtime", return_value=1.0)
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.listdir",
           return_value=["x.jsonl"])
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.isdir", return_value=True)
    def test_read_jsonl_local_empty_lines_skipped(self, _isdir, _listdir, _getmtime):
        """Empty lines don't cause errors."""
        sb = self._sandbox(docker_workdir="/tmp/fake")
        content = "\n\n" + json.dumps({"id": "ok"}) + "\n\n"
        with patch("builtins.open", return_value=io.StringIO(content)):
            entries = sb._read_jsonl_local()
        self.assertEqual(len(entries), 1)

    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.getmtime", return_value=1.0)
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.listdir",
           return_value=["x.jsonl"])
    @patch("odoo.addons.skoll.models.skoll_sandbox.os.path.isdir", return_value=True)
    def test_read_jsonl_local_uses_persona_name(self, mock_isdir, _listdir, _getmtime):
        """Path includes the persona name, not hardcoded."""
        sb = self._sandbox(docker_workdir="/tmp/fake")
        content = json.dumps({"id": "ok"}) + "\n"
        with patch("builtins.open", return_value=io.StringIO(content)):
            sb._read_jsonl_local()
        persona_name = sb.skoll_id.persona_id.name
        calls = [str(c) for c in mock_isdir.call_args_list]
        sessions_call = [c for c in calls if persona_name in c]
        self.assertTrue(
            len(sessions_call) > 0,
            "Expected sessions_dir call containing persona name '%s', got: %s"
            % (persona_name, calls),
        )


# ---------------------------------------------------------------------------
# _read_jsonl_k8s
# ---------------------------------------------------------------------------

@tagged("post_install", "-at_install")
class TestReadJsonlK8s(SkollTestCase):
    """Tests for SkollSandbox._read_jsonl_k8s."""

    def _sandbox(self, **kw):
        sb = self.claude_sandbox
        if kw:
            sb.write(kw)
        return sb

    def test_read_jsonl_k8s_no_k8s(self):
        """K8S_AVAILABLE=False → returns []."""
        sb = self._sandbox()
        with patch(
            "odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_jsonl_k8s",
            wraps=sb._read_jsonl_k8s,
        ):
            with patch.dict("sys.modules", {"kubernetes": None}):
                result = sb._read_jsonl_k8s()
        self.assertEqual(result, [])

    @patch("odoo.addons.skoll.models.skoll_sandbox.subprocess.run")
    def test_read_jsonl_k8s_no_pod(self, _mock_run):
        """Label selector finds nothing → returns []."""
        sb = self._sandbox()
        mock_k8s_client = MagicMock()
        mock_k8s_config = MagicMock()
        mock_core_v1 = MagicMock()
        mock_k8s_client.CoreV1Api.return_value = mock_core_v1
        mock_pods = MagicMock()
        mock_pods.items = []
        mock_core_v1.list_namespaced_pod.return_value = mock_pods

        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s_config,
        }):
            result = sb._read_jsonl_k8s()
        self.assertEqual(result, [])

    @patch("odoo.addons.skoll.models.skoll_sandbox.subprocess.run")
    def test_read_jsonl_k8s_success(self, mock_run):
        """Parses kubectl stdout into entries."""
        sb = self._sandbox()
        entry1 = {"type": "message", "id": "k-1"}
        entry2 = {"type": "message", "id": "k-2"}
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            stderr="",
        )

        mock_k8s_client = MagicMock()
        mock_k8s_config = MagicMock()
        mock_core_v1 = MagicMock()
        mock_k8s_client.CoreV1Api.return_value = mock_core_v1
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.metadata.name = "test-pod-abc"
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_core_v1.list_namespaced_pod.return_value = mock_pods

        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s_config,
        }):
            result = sb._read_jsonl_k8s()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "k-1")
        self.assertEqual(result[1]["id"], "k-2")

    @patch("odoo.addons.skoll.models.skoll_sandbox.subprocess.run")
    def test_read_jsonl_k8s_kubectl_nonzero_exit(self, mock_run):
        """kubectl non-zero exit → returns []."""
        sb = self._sandbox()
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error from kubectl",
        )

        mock_k8s_client = MagicMock()
        mock_k8s_config = MagicMock()
        mock_core_v1 = MagicMock()
        mock_k8s_client.CoreV1Api.return_value = mock_core_v1
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.metadata.name = "test-pod-abc"
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_core_v1.list_namespaced_pod.return_value = mock_pods

        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s_config,
        }):
            result = sb._read_jsonl_k8s()
        self.assertEqual(result, [])

    @patch("odoo.addons.skoll.models.skoll_sandbox.subprocess.run")
    def test_read_jsonl_k8s_namespace_from_config(self, mock_run):
        """Uses namespace from config param."""
        sb = self._sandbox()
        self._set_param("skoll.k8s_namespace", "custom-ns")
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"id": "1"}) + "\n",
            stderr="",
        )

        mock_k8s_client = MagicMock()
        mock_k8s_config = MagicMock()
        mock_core_v1 = MagicMock()
        mock_k8s_client.CoreV1Api.return_value = mock_core_v1
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.metadata.name = "test-pod-abc"
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_core_v1.list_namespaced_pod.return_value = mock_pods

        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s_config,
        }):
            sb._read_jsonl_k8s()

        call_args = mock_core_v1.list_namespaced_pod.call_args
        self.assertEqual(call_args[1].get("namespace", call_args[0][0] if call_args[0] else None), "custom-ns")


# ---------------------------------------------------------------------------
# _query_litellm_spend
# ---------------------------------------------------------------------------

@tagged("post_install", "-at_install")
class TestQueryLitellmSpend(SkollTestCase):
    """Tests for SkollSandbox._query_litellm_spend."""

    def _sandbox(self, model_type="claude", **kw):
        sb = self.claude_sandbox if model_type == "claude" else self.glm_sandbox
        if kw:
            sb.write(kw)
        return sb

    def test_query_spend_no_port(self):
        """No litellm port in local mode → returns (0, 0)."""
        self._set_param("skoll.deployment_mode", "local")
        sb = self._sandbox(docker_litellm_port=0)
        self.assertEqual(sb._query_litellm_spend(), (0, 0))

    @patch("odoo.addons.skoll.models.skoll_sandbox._load_dotenv", return_value={})
    def test_query_spend_no_litellm_key(self, _dotenv):
        """No LITELLM_MASTER_KEY → returns (0, 0)."""
        self._set_param("skoll.deployment_mode", "local")
        sb = self._sandbox(docker_litellm_port=14001)
        self.assertEqual(sb._query_litellm_spend(), (0, 0))

    @patch("odoo.addons.skoll.models.skoll_sandbox._load_dotenv",
           return_value={"LITELLM_MASTER_KEY": "sk-test"})
    def test_query_spend_no_create_date(self, _dotenv):
        """No create_date → returns (0, 0)."""
        self._set_param("skoll.deployment_mode", "local")
        sb = self._sandbox(docker_litellm_port=14001)
        with patch.object(type(sb), "create_date", new_callable=lambda: property(lambda self: None)):
            result = sb._query_litellm_spend()
        self.assertEqual(result, (0, 0))

    @patch("odoo.addons.skoll.models.skoll_sandbox.urllib.request.urlopen")
    @patch("odoo.addons.skoll.models.skoll_sandbox._load_dotenv",
           return_value={"LITELLM_MASTER_KEY": "sk-test"})
    def test_query_spend_http_error(self, _dotenv, mock_urlopen):
        """Returns (0, 0) on HTTP error."""
        import urllib.error
        self._set_param("skoll.deployment_mode", "local")
        sb = self._sandbox(docker_litellm_port=14001)
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://localhost:14001/spend/logs",
            code=500,
            msg="Internal Server Error",
            hdrs=MagicMock(),
            fp=io.BytesIO(b""),
        )
        self.assertEqual(sb._query_litellm_spend(), (0, 0))

    @patch("odoo.addons.skoll.models.skoll_sandbox.urllib.request.urlopen")
    @patch("odoo.addons.skoll.models.skoll_sandbox._load_dotenv",
           return_value={"LITELLM_MASTER_KEY": "sk-test"})
    def test_query_spend_success(self, _dotenv, mock_urlopen):
        """Sums prompt_tokens and completion_tokens from logs."""
        self._set_param("skoll.deployment_mode", "local")
        sb = self._sandbox(docker_litellm_port=14001)
        response_data = {
            "data": [
                {"prompt_tokens": 100, "completion_tokens": 50},
                {"prompt_tokens": 200, "completion_tokens": 75},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = sb._query_litellm_spend()
        self.assertEqual(result, (300, 125))

    @patch("odoo.addons.skoll.models.skoll_sandbox.urllib.request.urlopen")
    @patch("odoo.addons.skoll.models.skoll_sandbox._load_dotenv",
           return_value={"LITELLM_MASTER_KEY": "sk-test"})
    def test_query_spend_k8s_mode_url(self, _dotenv, mock_urlopen):
        """Uses https://<ws_host>/litellm/<id> URL in k8s mode."""
        self._set_param("skoll.deployment_mode", "k8s")
        self._set_param("skoll.ws_router_host", "ws.example.com")
        sb = self._sandbox(docker_litellm_port=14001)
        response_data = {"data": [{"prompt_tokens": 10, "completion_tokens": 5}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        sb._query_litellm_spend()
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.assertIn("https://ws.example.com/litellm/", url)
        self.assertIn(str(sb.id), url)

    @patch("odoo.addons.skoll.models.skoll_sandbox.urllib.request.urlopen")
    @patch("odoo.addons.skoll.models.skoll_sandbox._load_dotenv", return_value={})
    def test_query_spend_k8s_fallback_key(self, _dotenv, mock_urlopen):
        """Falls back to sk-skoll-<token[:16]> when no master key."""
        self._set_param("skoll.deployment_mode", "k8s")
        self._set_param("skoll.ws_router_host", "ws.example.com")
        token = "abcdef1234567890extra"
        sb = self._sandbox(docker_gateway_token=token)
        response_data = {"data": [{"prompt_tokens": 10, "completion_tokens": 5}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        sb._query_litellm_spend()
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        auth_header = req.get_header("Authorization")
        self.assertIn("sk-skoll-" + token[:16], auth_header)

    @patch("odoo.addons.skoll.models.skoll_sandbox.urllib.request.urlopen")
    @patch("odoo.addons.skoll.models.skoll_sandbox._load_dotenv",
           return_value={"LITELLM_MASTER_KEY": "sk-test"})
    def test_query_spend_response_as_list(self, _dotenv, mock_urlopen):
        """Handles response where data is list directly (not wrapped in dict)."""
        self._set_param("skoll.deployment_mode", "local")
        sb = self._sandbox(docker_litellm_port=14001)
        response_data = [
            {"prompt_tokens": 50, "completion_tokens": 25},
            {"prompt_tokens": 150, "completion_tokens": 75},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = sb._query_litellm_spend()
        self.assertEqual(result, (200, 100))


# ---------------------------------------------------------------------------
# _export_trajectory_to_task
# ---------------------------------------------------------------------------

@tagged("post_install", "-at_install")
class TestExportTrajectoryToTask(SkollTestCase):
    """Tests for SkollSandbox._export_trajectory_to_task."""

    def _sandbox(self, model_type="claude", **kw):
        sb = self.claude_sandbox if model_type == "claude" else self.glm_sandbox
        if kw:
            sb.write(kw)
        return sb

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(0, 0))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl",
           return_value=[])
    def test_export_traj_legacy_dict_converted(self, _mock_jsonl, _mock_spend):
        """Existing non-list trajectory wrapped as legacy entry."""
        sb = self._sandbox()
        task = sb.skoll_id
        old_traj = {"meta_info": {"task_type": "test"}, "messages": []}
        task.write({"claude_trajectory": json.dumps(old_traj)})

        turn = self._create_turn(sandbox=sb, turn_number=1)
        _mock_jsonl.return_value = []
        sb._export_trajectory_to_task()

        raw = task.claude_trajectory
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 2)
        legacy_entry = data[0]
        self.assertEqual(legacy_entry["session_id"], "legacy")

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(0, 0))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl",
           return_value=[])
    def test_export_traj_no_trajectory_no_turns(self, _mock_jsonl, _mock_spend):
        """No JSONL and no turns → no write to trajectory field."""
        sb = self._sandbox()
        task = sb.skoll_id
        task.write({"claude_trajectory": ""})
        sb._export_trajectory_to_task()
        self.assertFalse(task.claude_trajectory)

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(500, 200))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl")
    def test_export_traj_token_map_claude(self, mock_jsonl, _mock_spend):
        """Claude tokens go to correct fields."""
        sb = self._sandbox(model_type="claude")
        task = sb.skoll_id
        task.write({"claude_input_tokens": 0, "claude_output_tokens": 0})
        mock_jsonl.return_value = self._make_jsonl_entries()
        sb._export_trajectory_to_task()
        self.assertEqual(task.claude_input_tokens, 500)
        self.assertEqual(task.claude_output_tokens, 200)

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(300, 150))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl")
    def test_export_traj_token_map_glm(self, mock_jsonl, _mock_spend):
        """GLM tokens go to correct fields."""
        sb = self._sandbox(model_type="glm")
        sb = self.glm_sandbox
        task = sb.skoll_id
        task.write({"glm_input_tokens": 0, "glm_output_tokens": 0})
        mock_jsonl.return_value = self._make_jsonl_entries()
        sb._export_trajectory_to_task()
        self.assertEqual(task.glm_input_tokens, 300)
        self.assertEqual(task.glm_output_tokens, 150)

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(1000, 500))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl")
    def test_export_traj_litellm_spend_primary(self, mock_jsonl, _mock_spend):
        """Uses LiteLLM spend when available (primary source)."""
        sb = self._sandbox(model_type="claude")
        task = sb.skoll_id
        task.write({"claude_input_tokens": 0, "claude_output_tokens": 0})
        entries = self._make_jsonl_entries()
        mock_jsonl.return_value = entries
        sb._export_trajectory_to_task()
        self.assertEqual(task.claude_input_tokens, 1000)
        self.assertEqual(task.claude_output_tokens, 500)

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(0, 0))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl")
    def test_export_traj_jsonl_token_fallback(self, mock_jsonl, _mock_spend):
        """Falls back to JSONL tokens when LiteLLM returns 0."""
        sb = self._sandbox(model_type="claude")
        task = sb.skoll_id
        task.write({"claude_input_tokens": 0, "claude_output_tokens": 0})
        entries = [
            {
                "type": "message",
                "id": "j-001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello"}],
                },
            },
            {
                "type": "message",
                "id": "j-002",
                "parentId": "j-001",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi"}],
                },
                "usage": {"input_tokens": 200, "output_tokens": 80},
            },
        ]
        mock_jsonl.return_value = entries
        sb._export_trajectory_to_task()
        self.assertEqual(task.claude_input_tokens, 200)
        self.assertEqual(task.claude_output_tokens, 80)

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(0, 0))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl",
           return_value=[])
    def test_export_traj_clears_turns(self, _mock_jsonl, _mock_spend):
        """Turns unlinked after export."""
        sb = self._sandbox()
        t1 = self._create_turn(sandbox=sb, turn_number=1)
        t2 = self._create_turn(sandbox=sb, turn_number=2)
        self.assertEqual(len(sb.turn_ids), 2)
        sb._export_trajectory_to_task()
        self.assertEqual(len(sb.turn_ids), 0)
        self.assertFalse(self.Turn.browse(t1.id).exists())
        self.assertFalse(self.Turn.browse(t2.id).exists())

    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._query_litellm_spend",
           return_value=(0, 0))
    @patch("odoo.addons.skoll.models.skoll_sandbox.SkollSandbox._read_session_jsonl")
    def test_export_traj_with_thinking_blocks(self, mock_jsonl, _mock_spend):
        """Thinking blocks preserved through export pipeline."""
        sb = self._sandbox(model_type="claude")
        task = sb.skoll_id
        entries = [
            {
                "type": "message",
                "id": "j-001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Think about this"}],
                },
            },
            {
                "type": "message",
                "id": "j-002",
                "parentId": "j-001",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Let me reason about this...",
                            "thinkingSignature": "sig123",
                        },
                        {"type": "text", "text": "Here's my answer"},
                    ],
                },
            },
        ]
        mock_jsonl.return_value = entries
        sb._export_trajectory_to_task()

        raw = task.claude_trajectory
        self.assertTrue(raw)
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        traj = data[-1]["trajectory"]
        messages = traj["messages"]
        assistant_msgs = [
            m for m in messages
            if m.get("message", {}).get("role") == "assistant"
        ]
        self.assertTrue(len(assistant_msgs) > 0)
        content = assistant_msgs[0]["message"]["content"]
        thinking_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "thinking"]
        self.assertTrue(
            len(thinking_blocks) > 0,
            "Expected thinking blocks in exported trajectory, got: %s" % content,
        )
