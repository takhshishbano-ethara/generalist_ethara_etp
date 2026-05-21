# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.tests import tagged

from .common import SkollTestCase


@tagged("post_install", "-at_install")
class TestSkollSandboxCreate(SkollTestCase):
    """Basic creation, defaults, constraints, and ordering."""

    def test_default_docker_status(self):
        self.assertEqual(self.claude_sandbox.docker_status, "stopped")

    def test_default_session_status(self):
        self.assertEqual(self.claude_sandbox.session_status, "not_started")

    def test_default_auto_hint_status(self):
        self.assertEqual(self.claude_sandbox.auto_hint_status, "idle")

    def test_unique_constraint_task_model(self):
        """Cannot create two sandboxes with the same (skoll_id, model_type)."""
        with self.assertRaises(Exception):
            self.Sandbox.create({
                "skoll_id": self.task.id,
                "model_type": "claude",
            })

    def test_cascade_delete_from_task(self):
        """Deleting the task removes its sandboxes."""
        task2 = self._create_task(task_id="CASCADE-001")
        sb_id = task2.claude_sandbox_id.id
        self.assertTrue(self.Sandbox.browse(sb_id).exists())
        task2.unlink()
        self.assertFalse(self.Sandbox.browse(sb_id).exists())

    def test_ordering_by_model_type(self):
        """Sandboxes are ordered by model_type (selection key)."""
        task2 = self._create_task(task_id="ORDER-001")
        sandboxes = self.Sandbox.search([("skoll_id", "=", task2.id)])
        types = sandboxes.mapped("model_type")
        self.assertEqual(types, sorted(types))


@tagged("post_install", "-at_install")
class TestSandboxComputedFields(SkollTestCase):
    """Tests for dashboard_url, ws_url, and _get_gateway_ws_url."""

    def _make_running(self, sandbox, port=19042, token="abc123"):
        sandbox.write({
            "docker_status": "running",
            "docker_port": port,
            "docker_gateway_token": token,
        })

    def test_dashboard_url_stopped(self):
        self.assertFalse(self.claude_sandbox.docker_dashboard_url)

    def test_dashboard_url_running_local(self):
        self._set_param("skoll.deployment_mode", "local")
        self._make_running(self.claude_sandbox)
        url = self.claude_sandbox.docker_dashboard_url
        self.assertIn("localhost:19042", url)
        self.assertIn("#token=abc123", url)

    def test_dashboard_url_running_k8s_with_ws_host(self):
        self._set_param("skoll.deployment_mode", "k8s")
        self._set_param("skoll.ws_router_host", "ws.example.com")
        self._make_running(self.claude_sandbox)
        url = self.claude_sandbox.docker_dashboard_url
        self.assertIn("https://ws.example.com/sandbox/", url)
        self.assertIn("#token=abc123", url)

    def test_dashboard_url_running_k8s_no_ws_host(self):
        self._set_param("skoll.deployment_mode", "k8s")
        self._set_param("skoll.ws_router_host", "")
        self._make_running(self.claude_sandbox)
        url = self.claude_sandbox.docker_dashboard_url
        self.assertIn("skoll-sandbox-", url)
        self.assertIn(".skoll.svc.cluster.local:18789", url)

    def test_ws_url_stopped(self):
        self.assertFalse(self.claude_sandbox.docker_ws_url)

    def test_ws_url_running_local(self):
        self._set_param("skoll.deployment_mode", "local")
        self._make_running(self.claude_sandbox)
        self.assertEqual(
            self.claude_sandbox.docker_ws_url, "ws://localhost:19042"
        )

    def test_ws_url_running_k8s(self):
        self._set_param("skoll.deployment_mode", "k8s")
        self._set_param("skoll.ws_router_host", "ws.example.com")
        self._make_running(self.claude_sandbox)
        url = self.claude_sandbox.docker_ws_url
        self.assertTrue(url.startswith("wss://ws.example.com/sandbox/"))

    def test_get_gateway_ws_url_local(self):
        self._set_param("skoll.deployment_mode", "local")
        self._make_running(self.claude_sandbox)
        url = self.claude_sandbox._get_gateway_ws_url()
        self.assertEqual(url, "ws://localhost:19042")

    def test_get_gateway_ws_url_k8s(self):
        self._set_param("skoll.deployment_mode", "k8s")
        self._make_running(self.claude_sandbox)
        url = self.claude_sandbox._get_gateway_ws_url()
        self.assertIn("skoll-sandbox-", url)
        self.assertIn(".skoll.svc.cluster.local:18789", url)

    def test_get_gateway_ws_url_no_port(self):
        self._set_param("skoll.deployment_mode", "local")
        self.claude_sandbox.write({"docker_status": "running", "docker_port": 0})
        self.assertFalse(self.claude_sandbox._get_gateway_ws_url())


@tagged("post_install", "-at_install")
class TestSandboxPortAllocation(SkollTestCase):
    """Tests for _allocate_ports."""

    def test_returns_3_tuple(self):
        result = self.claude_sandbox._allocate_ports()
        self.assertEqual(len(result), 3)

    def test_uses_id_mod_5000(self):
        sb = self.claude_sandbox
        offset = sb.id % 5000
        gw, ll, db = sb._allocate_ports()
        self.assertEqual(gw, 19000 + offset)
        self.assertEqual(ll, 14000 + offset)
        self.assertEqual(db, 15432 + offset)

    def test_correct_base_ranges(self):
        from odoo.addons.skoll.models.skoll_sandbox import (
            GATEWAY_PORT_BASE,
            LITELLM_PORT_BASE,
            DB_PORT_BASE,
        )
        self.assertEqual(GATEWAY_PORT_BASE, 19000)
        self.assertEqual(LITELLM_PORT_BASE, 14000)
        self.assertEqual(DB_PORT_BASE, 15432)


@tagged("post_install", "-at_install")
class TestSandboxJSONL(SkollTestCase):
    """Tests for JSONL sanitization, trajectory building, and token extraction."""

    def test_sanitize_strips_internal_msg_fields(self):
        msg = {
            "role": "assistant",
            "sender": "system",
            "thinkingSignature": "abc",
            "api": "openai",
            "provider": "litellm",
            "model": "claude-4.7",
            "usage": {"input_tokens": 10},
            "content": [{"type": "text", "text": "hello"}],
        }
        result = self.Sandbox._sanitize_jsonl_message(msg)
        for field in ("sender", "thinkingSignature", "api", "provider", "model", "usage"):
            self.assertNotIn(field, result)
        self.assertEqual(result["role"], "assistant")
        self.assertEqual(result["content"], [{"type": "text", "text": "hello"}])

    def test_sanitize_preserves_content_text(self):
        msg = {
            "role": "user",
            "content": [{"type": "text", "text": "keep me"}],
        }
        result = self.Sandbox._sanitize_jsonl_message(msg)
        self.assertEqual(result["content"][0]["text"], "keep me")

    def test_sanitize_cleans_pipe_toolcallid(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_result", "toolCallId": "tc-001|extra-data"},
            ],
        }
        result = self.Sandbox._sanitize_jsonl_message(msg)
        self.assertEqual(result["content"][0]["toolCallId"], "tc-001")

    def test_sanitize_cleans_pipe_tool_use_id(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu-001|suffix"},
            ],
        }
        result = self.Sandbox._sanitize_jsonl_message(msg)
        self.assertEqual(result["content"][0]["id"], "tu-001")

    def test_sanitize_strips_block_internal_fields(self):
        msg = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "hello",
                    "api": "openai",
                    "provider": "bedrock",
                    "model": "claude",
                    "usage": {"tokens": 10},
                },
            ],
        }
        result = self.Sandbox._sanitize_jsonl_message(msg)
        block = result["content"][0]
        for field in ("api", "provider", "model", "usage"):
            self.assertNotIn(field, block)
        self.assertEqual(block["text"], "hello")

    def test_build_trajectory_from_jsonl_valid_entries(self):
        entries = self._make_jsonl_entries()
        sb = self.claude_sandbox
        trajectory = sb._build_trajectory_from_jsonl(entries)
        self.assertIn("meta_info", trajectory)
        self.assertIn("messages", trajectory)
        self.assertEqual(len(trajectory["messages"]), 2)

    def test_build_trajectory_from_jsonl_empty(self):
        sb = self.claude_sandbox
        trajectory = sb._build_trajectory_from_jsonl([])
        self.assertEqual(trajectory["messages"], [])

    def test_build_trajectory_from_jsonl_skips_system_before_user(self):
        entries = [
            {
                "type": "message",
                "id": "sys-1",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "system", "content": "init"},
            },
            {
                "type": "message",
                "id": "usr-1",
                "parentId": "sys-1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
        ]
        trajectory = self.claude_sandbox._build_trajectory_from_jsonl(entries)
        roles = []
        for m in trajectory["messages"]:
            inner = m.get("message", m)
            if isinstance(inner, dict) and "message" in inner:
                inner = inner["message"]
            roles.append(inner.get("role", ""))
        # system before user should be skipped
        self.assertNotIn("system", roles[:1])

    def test_extract_tokens_from_jsonl_sums_correctly(self):
        entries = [
            {"usage": {"input_tokens": 100, "output_tokens": 50}},
            {"usage": {"input_tokens": 200, "output_tokens": 100}},
        ]
        total_in, total_out = self.Sandbox._extract_tokens_from_jsonl(entries)
        self.assertEqual(total_in, 300)
        self.assertEqual(total_out, 150)

    def test_extract_tokens_handles_missing_usage(self):
        entries = [
            {"type": "message", "message": {"role": "user"}},
            {},
        ]
        total_in, total_out = self.Sandbox._extract_tokens_from_jsonl(entries)
        self.assertEqual(total_in, 0)
        self.assertEqual(total_out, 0)

    def test_extract_tokens_from_message_usage(self):
        """Tokens inside message.usage should also be counted."""
        entries = [
            {
                "message": {
                    "role": "assistant",
                    "usage": {"inputTokens": 50, "outputTokens": 25},
                },
            },
        ]
        total_in, total_out = self.Sandbox._extract_tokens_from_jsonl(entries)
        self.assertEqual(total_in, 50)
        self.assertEqual(total_out, 25)

    def test_read_session_jsonl_local_mode(self):
        """Mock _read_jsonl_local for local mode."""
        self._set_param("skoll.deployment_mode", "local")
        fake_entries = [{"type": "message", "id": "1"}]
        with patch.object(
            type(self.claude_sandbox), "_read_jsonl_local", return_value=fake_entries
        ):
            result = self.claude_sandbox._read_session_jsonl()
        self.assertEqual(result, fake_entries)

    def test_read_session_jsonl_k8s_mode(self):
        """Mock _read_jsonl_k8s for k8s mode."""
        self._set_param("skoll.deployment_mode", "k8s")
        fake_entries = [{"type": "message", "id": "k8s-1"}]
        with patch.object(
            type(self.claude_sandbox), "_read_jsonl_k8s", return_value=fake_entries
        ):
            result = self.claude_sandbox._read_session_jsonl()
        self.assertEqual(result, fake_entries)


@tagged("post_install", "-at_install")
class TestSandboxTrajectoryBuilding(SkollTestCase):
    """Tests for build_trajectory_json, _trajectory_from_turns, _export_trajectory_to_task."""

    def test_build_trajectory_json_prefers_ws(self):
        """build_trajectory_json should use _trajectory_from_ws when available."""
        ws_msgs = [
            {
                "type": "message",
                "id": "ws-1",
                "message": {"role": "user", "content": [{"type": "text", "text": "ws"}]},
            },
        ]
        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            trajectory_messages=json.dumps(ws_msgs),
            prompt="hello",
            response="world",
        )
        result = self.claude_sandbox.build_trajectory_json()
        self.assertIn("meta_info", result)
        self.assertTrue(len(result["messages"]) > 0)

    def test_build_trajectory_json_falls_back_to_turns(self):
        """Without WS or events data, falls back to _trajectory_from_turns."""
        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            prompt="hello",
            response="world",
        )
        result = self.claude_sandbox.build_trajectory_json()
        self.assertIn("messages", result)
        self.assertTrue(len(result["messages"]) > 0)

    def test_trajectory_from_turns_correct_envelope(self):
        """_trajectory_from_turns builds user + assistant messages with correct structure."""
        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            prompt="test prompt",
            response="test response",
            run_id="run-001",
        )
        messages = self.claude_sandbox._trajectory_from_turns()
        self.assertTrue(len(messages) >= 2)
        first_msg = messages[0]
        self.assertEqual(first_msg["message"]["role"], "user")
        last_msg = messages[-1]
        # _wrap_trajectory_message wraps assistant as {"is_accepted": ..., "message": {original}}
        wrapped = last_msg.get("message", last_msg)
        if isinstance(wrapped, dict) and "message" in wrapped:
            role = wrapped["message"].get("role", "")
        else:
            role = wrapped.get("role", "")
        self.assertEqual(role, "assistant")

    def test_trajectory_from_turns_empty(self):
        """No turns means empty messages list."""
        messages = self.glm_sandbox._trajectory_from_turns()
        self.assertEqual(messages, [])

    def test_export_trajectory_to_task_writes_correct_field(self):
        """_export_trajectory_to_task writes trajectory to the correct field on the task."""
        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            prompt="export test",
            response="export response",
        )
        with patch.object(
            type(self.claude_sandbox), "_read_session_jsonl", return_value=[]
        ), patch.object(
            type(self.claude_sandbox), "_query_litellm_spend", return_value=(0, 0)
        ):
            self.claude_sandbox._export_trajectory_to_task()

        raw = self.task.claude_trajectory
        self.assertTrue(raw)
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertIn("trajectory", data[0])
        self.assertIn("session_id", data[0])

    def test_export_trajectory_aggregates_tokens(self):
        """_export_trajectory_to_task aggregates bedrock tokens to the task."""
        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            prompt="token test",
            response="token response",
            bedrock_input_tokens=100,
            bedrock_output_tokens=50,
        )
        initial_in = self.task.bedrock_input_tokens or 0
        initial_out = self.task.bedrock_output_tokens or 0

        with patch.object(
            type(self.claude_sandbox), "_read_session_jsonl", return_value=[]
        ), patch.object(
            type(self.claude_sandbox), "_query_litellm_spend", return_value=(0, 0)
        ):
            self.claude_sandbox._export_trajectory_to_task()

        self.task.invalidate_recordset()
        self.assertEqual(self.task.bedrock_input_tokens, initial_in + 100)
        self.assertEqual(self.task.bedrock_output_tokens, initial_out + 50)

    def test_export_trajectory_with_jsonl(self):
        """When JSONL entries are available, trajectory is built from JSONL."""
        entries = self._make_jsonl_entries()
        with patch.object(
            type(self.claude_sandbox), "_read_session_jsonl", return_value=entries
        ), patch.object(
            type(self.claude_sandbox), "_query_litellm_spend", return_value=(0, 0)
        ):
            self.claude_sandbox._export_trajectory_to_task()

        raw = self.task.claude_trajectory
        self.assertTrue(raw)
        data = json.loads(raw)
        self.assertIsInstance(data, list)

    def test_export_appends_to_existing_trajectory(self):
        """Multiple exports append session entries, not overwrite."""
        existing = json.dumps([self._make_session_entry(session_id="old-session")])
        self.task.write({"claude_trajectory": existing})

        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            prompt="append test",
            response="append response",
        )
        with patch.object(
            type(self.claude_sandbox), "_read_session_jsonl", return_value=[]
        ), patch.object(
            type(self.claude_sandbox), "_query_litellm_spend", return_value=(0, 0)
        ):
            self.claude_sandbox._export_trajectory_to_task()

        self.task.invalidate_recordset()
        data = json.loads(self.task.claude_trajectory)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["session_id"], "old-session")


@tagged("post_install", "-at_install")
class TestSandboxAutoProcess(SkollTestCase):
    """Tests for auto_process_* XML-RPC methods."""

    def test_auto_process_get_ws_info_running(self):
        self._set_param("skoll.deployment_mode", "local")
        self.claude_sandbox.write({
            "docker_status": "running",
            "docker_port": 19100,
            "docker_gateway_token": "tok-123",
        })
        result = self.Sandbox.auto_process_get_ws_info(self.claude_sandbox.id)
        self.assertNotIn("error", result)
        self.assertEqual(result["ws_url"], "ws://localhost:19100")
        self.assertEqual(result["gateway_token"], "tok-123")

    def test_auto_process_get_ws_info_stopped(self):
        result = self.Sandbox.auto_process_get_ws_info(self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("not running", result["error"])

    def test_auto_process_get_ws_info_nonexistent(self):
        result = self.Sandbox.auto_process_get_ws_info(999999)
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    def test_create_turn(self):
        result = self.Sandbox.auto_process_create_turn(
            self.claude_sandbox.id, "hello world"
        )
        self.assertIn("turn_id", result)
        turn = self.Turn.browse(result["turn_id"])
        self.assertTrue(turn.exists())
        self.assertEqual(turn.prompt, "hello world")
        self.assertEqual(turn.turn_status, "Pending")

    def test_create_turn_hint(self):
        result = self.Sandbox.auto_process_create_turn(
            self.claude_sandbox.id, "hint text", is_hint=True
        )
        turn = self.Turn.browse(result["turn_id"])
        self.assertTrue(turn.is_hint_turn)
        self.assertEqual(turn.hints, "hint text")

    def test_create_turn_updates_session_status(self):
        self.assertEqual(self.claude_sandbox.session_status, "not_started")
        self.Sandbox.auto_process_create_turn(self.claude_sandbox.id, "first msg")
        self.claude_sandbox.invalidate_recordset()
        self.assertEqual(self.claude_sandbox.session_status, "in_progress")

    def test_create_turn_nonexistent_sandbox(self):
        result = self.Sandbox.auto_process_create_turn(999999, "nope")
        self.assertIn("error", result)

    def test_save_response(self):
        turn = self._create_turn(sandbox=self.claude_sandbox, turn_status="Pending")
        result = self.Sandbox.auto_process_save_response(
            turn.id, "response text", tool_calls_json='[{"name":"test"}]'
        )
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(turn.response, "response text")
        self.assertEqual(turn.turn_status, "Completed")
        self.assertEqual(turn.tool_calls, '[{"name":"test"}]')

    def test_save_response_partial(self):
        turn = self._create_turn(sandbox=self.claude_sandbox, turn_status="Pending")
        result = self.Sandbox.auto_process_save_response(
            turn.id, "partial", partial=True
        )
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(turn.turn_status, "Streaming")

    def test_save_response_nonexistent_turn(self):
        result = self.Sandbox.auto_process_save_response(999999, "nope")
        self.assertIn("error", result)

    def test_save_trajectory(self):
        turn = self._create_turn(sandbox=self.claude_sandbox)
        traj = [{"type": "message", "id": "1"}]
        result = self.Sandbox.auto_process_save_trajectory(
            self.claude_sandbox.id, turn.id, traj
        )
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(json.loads(turn.trajectory_messages), traj)

    def test_save_trajectory_string_input(self):
        turn = self._create_turn(sandbox=self.claude_sandbox)
        traj_str = json.dumps([{"type": "message", "id": "s1"}])
        result = self.Sandbox.auto_process_save_trajectory(
            self.claude_sandbox.id, turn.id, traj_str
        )
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(turn.trajectory_messages, traj_str)

    def test_save_feedback_satisfied(self):
        turn = self._create_turn(sandbox=self.claude_sandbox)
        result = self.Sandbox.auto_process_save_feedback(turn.id, "satisfied")
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(turn.feedback, "satisfied")

    def test_save_feedback_unsatisfied_with_hint(self):
        turn = self._create_turn(sandbox=self.claude_sandbox)
        result = self.Sandbox.auto_process_save_feedback(
            turn.id, "unsatisfied", hint_text="try again"
        )
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(turn.feedback, "unsatisfied")
        self.assertEqual(turn.hint_text, "try again")

    def test_save_feedback_invalid(self):
        turn = self._create_turn(sandbox=self.claude_sandbox)
        result = self.Sandbox.auto_process_save_feedback(turn.id, "invalid_value")
        self.assertIn("error", result)

    def test_save_feedback_nonexistent_turn(self):
        result = self.Sandbox.auto_process_save_feedback(999999, "satisfied")
        self.assertIn("error", result)

    def test_reset_hint_status(self):
        self.claude_sandbox.write({
            "auto_hint_status": "evaluating",
            "auto_hint_iteration": 3,
            "auto_hint_group_id": "grp-123",
        })
        result = self.Sandbox.auto_process_reset_hint_status(self.claude_sandbox.id)
        self.assertTrue(result.get("success"))
        self.claude_sandbox.invalidate_recordset()
        self.assertEqual(self.claude_sandbox.auto_hint_status, "idle")
        self.assertEqual(self.claude_sandbox.auto_hint_iteration, 0)
        self.assertFalse(self.claude_sandbox.auto_hint_group_id)

    def test_reset_hint_status_nonexistent(self):
        result = self.Sandbox.auto_process_reset_hint_status(999999)
        self.assertIn("error", result)

    def test_poll_hint_status(self):
        self.claude_sandbox.write({
            "auto_hint_status": "streaming",
            "auto_hint_iteration": 2,
            "auto_hint_group_id": "poll-grp",
        })
        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            feedback="unsatisfied",
            hint_text="do better",
        )
        result = self.Sandbox.auto_process_poll_hint_status(self.claude_sandbox.id)
        self.assertEqual(result["auto_hint_status"], "streaming")
        self.assertEqual(result["auto_hint_iteration"], 2)
        self.assertEqual(result["auto_hint_group_id"], "poll-grp")
        self.assertEqual(result["last_turn_id"], turn.id)
        self.assertEqual(result["last_turn_feedback"], "unsatisfied")
        self.assertEqual(result["last_turn_hint_text"], "do better")

    def test_poll_hint_status_no_turns(self):
        result = self.Sandbox.auto_process_poll_hint_status(self.glm_sandbox.id)
        self.assertEqual(result["last_turn_id"], 0)
        self.assertEqual(result["last_turn_feedback"], "")

    def test_poll_hint_status_nonexistent(self):
        result = self.Sandbox.auto_process_poll_hint_status(999999)
        self.assertIn("error", result)
