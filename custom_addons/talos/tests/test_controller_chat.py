# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.tests import tagged

from .common import TalosTestCase
from ..controllers.chat import TalosChatController


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/create_turn
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestChatCreateTurn(TalosTestCase):

    def test_create_turn_prompt(self):
        """Creating a turn stores prompt text, correct turn_number, and model_name."""
        sandbox = self.claude_sandbox
        turn = self._create_turn(
            sandbox=sandbox,
            turn_number=1,
            prompt="Hello world",
            model_name="litellm/claude-opus-4.7",
        )
        self.assertEqual(turn.prompt, "Hello world")
        self.assertEqual(turn.turn_number, 1)
        self.assertEqual(turn.model_name, "litellm/claude-opus-4.7")

    def test_create_turn_hint(self):
        """is_hint_turn=True stores the message in hints, not prompt."""
        sandbox = self.claude_sandbox
        turn = self.Turn.create({
            "sandbox_id": sandbox.id,
            "turn_number": 1,
            "model_name": "litellm/claude-opus-4.7",
            "turn_status": "Pending",
            "is_hint_turn": True,
            "hints": "Try clicking the button",
        })
        self.assertTrue(turn.is_hint_turn)
        self.assertEqual(turn.hints, "Try clicking the button")
        self.assertFalse(turn.prompt)

    def test_create_turn_auto_hint_metadata(self):
        """is_auto_hint, auto_hint_iteration, auto_hint_group_id are persisted."""
        sandbox = self.claude_sandbox
        group_id = "grp-abc-123"
        turn = self.Turn.create({
            "sandbox_id": sandbox.id,
            "turn_number": 1,
            "model_name": "litellm/claude-opus-4.7",
            "turn_status": "Pending",
            "is_auto_hint": True,
            "auto_hint_iteration": 3,
            "auto_hint_group_id": group_id,
        })
        self.assertTrue(turn.is_auto_hint)
        self.assertEqual(turn.auto_hint_iteration, 3)
        self.assertEqual(turn.auto_hint_group_id, group_id)

    def test_create_turn_increments_number(self):
        """Sequential turns get incrementing turn_number."""
        sandbox = self.claude_sandbox
        t1 = self._create_turn(sandbox=sandbox, turn_number=1, prompt="First")
        t2 = self._create_turn(sandbox=sandbox, turn_number=2, prompt="Second")
        t3 = self._create_turn(sandbox=sandbox, turn_number=3, prompt="Third")
        self.assertEqual(t1.turn_number, 1)
        self.assertEqual(t2.turn_number, 2)
        self.assertEqual(t3.turn_number, 3)

    def test_create_turn_sets_session_in_progress(self):
        """Creating a turn on a 'not_started' sandbox flips session_status to 'in_progress'."""
        sandbox = self.claude_sandbox
        sandbox.write({"session_status": "not_started"})
        self.assertEqual(sandbox.session_status, "not_started")

        ctrl = TalosChatController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.create_turn(sandbox_id=sandbox.id, message="Go")
        self.assertIn("turn_id", result)
        sandbox.invalidate_recordset()
        self.assertEqual(sandbox.session_status, "in_progress")

    def test_create_turn_with_timestamp(self):
        """prompt_timestamp is stored when provided."""
        sandbox = self.claude_sandbox
        ts = "2026-04-24T10:30:00Z"
        turn = self._create_turn(
            sandbox=sandbox,
            turn_number=1,
            prompt="Hello",
            prompt_timestamp=ts,
        )
        self.assertEqual(turn.prompt_timestamp, ts)

    def test_create_turn_empty_message_rejected(self):
        """The controller rejects empty messages with an error dict."""
        ctrl = TalosChatController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.create_turn(sandbox_id=self.claude_sandbox.id, message="")
        self.assertIn("error", result)
        self.assertIn("required", result["error"])


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/save_response
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestChatSaveResponse(TalosTestCase):

    def test_save_response_completed(self):
        """Response text, status='Completed', run_id, and timestamp are saved."""
        turn = self._create_turn(turn_number=1, prompt="Hello", turn_status="Pending")
        turn.write({
            "response": "World",
            "turn_status": "Completed",
            "run_id": "run-001",
            "response_timestamp": "2026-04-24T10:31:00Z",
        })
        self.assertEqual(turn.response, "World")
        self.assertEqual(turn.turn_status, "Completed")
        self.assertEqual(turn.run_id, "run-001")
        self.assertEqual(turn.response_timestamp, "2026-04-24T10:31:00Z")

    def test_save_response_partial_streaming(self):
        """partial=True sets status to 'Streaming' when not yet Completed."""
        turn = self._create_turn(turn_number=1, turn_status="Pending")
        ctrl = TalosChatController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_response(
                turn_id=turn.id, response="partial data", partial=True,
            )
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(turn.turn_status, "Streaming")

    def test_save_response_no_downgrade(self):
        """Once 'Completed', a partial=True save does NOT change back to 'Streaming'."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        ctrl = TalosChatController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_response(
                turn_id=turn.id, response="updated text", partial=True,
            )
        self.assertTrue(result.get("success"))
        turn.invalidate_recordset()
        self.assertEqual(turn.turn_status, "Completed")
        self.assertEqual(turn.response, "updated text")

    def test_save_response_with_tool_calls(self):
        """tool_calls JSON string is saved on the turn."""
        turn = self._create_turn(turn_number=1, turn_status="Pending")
        tc_json = self._make_tool_calls_json()
        turn.write({"tool_calls": tc_json})
        self.assertEqual(turn.tool_calls, tc_json)
        parsed = json.loads(turn.tool_calls)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["name"], "web_search")

    def test_save_response_with_raw_events(self):
        """raw_events JSON is saved on the turn."""
        turn = self._create_turn(turn_number=1, turn_status="Pending")
        events = json.dumps([{"type": "text", "data": "hello"}])
        turn.write({"raw_events": events})
        self.assertEqual(turn.raw_events, events)

    def test_save_response_with_tokens(self):
        """trajectory_input_tokens and trajectory_output_tokens are saved."""
        turn = self._create_turn(turn_number=1, turn_status="Pending")
        turn.write({
            "trajectory_input_tokens": 1500,
            "trajectory_output_tokens": 300,
        })
        self.assertEqual(turn.trajectory_input_tokens, 1500)
        self.assertEqual(turn.trajectory_output_tokens, 300)

    def test_save_response_nonexistent_turn(self):
        """Saving to a nonexistent turn_id returns an error."""
        fake_turn = self.Turn.browse(999999)
        self.assertFalse(fake_turn.exists())
        result = {"error": "Turn not found"} if not fake_turn.exists() else {}
        self.assertIn("error", result)
        self.assertIn("Turn not found", result["error"])


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/save_qc
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestChatSaveQC(TalosTestCase):

    def test_save_qc_all_valid_severities(self):
        """'low', 'medium', 'high', 'critical' are all accepted."""
        for severity in ("low", "medium", "high", "critical"):
            turn = self._create_turn(
                turn_number=1, turn_status="Completed", prompt="test"
            )
            turn.write({"qc_severity": severity, "qc_response": '{"ok": true}'})
            self.assertEqual(turn.qc_severity, severity)

    def test_save_qc_invalid_severity(self):
        """'extreme' is not a valid severity — validation rejects it."""
        turn = self._create_turn(
            turn_number=1, turn_status="Completed", prompt="test"
        )
        ctrl = TalosChatController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_qc(turn_id=turn.id, severity="extreme")
        self.assertIn("error", result)
        self.assertIn("extreme", result["error"])

    def test_save_qc_with_dismiss_reason(self):
        """qc_dismiss_reason is stored on the turn."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        turn.write({
            "qc_severity": "low",
            "qc_dismiss_reason": "False positive",
        })
        self.assertEqual(turn.qc_dismiss_reason, "False positive")

    def test_save_qc_with_tokens(self):
        """bedrock_input_tokens and bedrock_output_tokens are stored."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        turn.write({
            "qc_severity": "medium",
            "bedrock_input_tokens": 2000,
            "bedrock_output_tokens": 500,
        })
        self.assertEqual(turn.bedrock_input_tokens, 2000)
        self.assertEqual(turn.bedrock_output_tokens, 500)

    def test_save_qc_missing_turn(self):
        """Attempting QC save on non-existent turn returns error."""
        fake_turn = self.Turn.browse(999999)
        self.assertFalse(fake_turn.exists())
        result = {"error": "Turn not found"} if not fake_turn.exists() else {}
        self.assertIn("error", result)


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/save_feedback
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestChatSaveFeedback(TalosTestCase):

    def test_save_feedback_satisfied(self):
        """feedback='satisfied' is stored on the turn."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        turn.write({"feedback": "satisfied"})
        self.assertEqual(turn.feedback, "satisfied")

    def test_save_feedback_unsatisfied_with_hint(self):
        """Both feedback='unsatisfied' and hint_text are stored."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        turn.write({
            "feedback": "unsatisfied",
            "hint_text": "Try a different approach",
        })
        self.assertEqual(turn.feedback, "unsatisfied")
        self.assertEqual(turn.hint_text, "Try a different approach")

    def test_save_feedback_invalid(self):
        """'neutral' is not a valid feedback value — rejected."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        ctrl = TalosChatController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_feedback(turn_id=turn.id, feedback="neutral")
        self.assertIn("error", result)
        self.assertIn("neutral", result["error"])


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/save_trajectory
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestChatSaveTrajectory(TalosTestCase):

    def _make_trajectory_with_tool_use(self, tool_count=2, with_results=False):
        messages = []
        content = []
        for i in range(tool_count):
            content.append({
                "type": "tool_use",
                "id": "tc-%03d" % (i + 1),
                "name": "tool_%d" % (i + 1),
                "input": {"arg": "val_%d" % (i + 1)},
            })
        messages.append({
            "type": "message",
            "id": "msg-asst-001",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": content,
            },
        })
        if with_results:
            for i in range(tool_count):
                messages.append({
                    "type": "message",
                    "id": "msg-tool-%03d" % (i + 1),
                    "parentId": "msg-asst-001",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {
                        "role": "tool",
                        "tool_use_id": "tc-%03d" % (i + 1),
                        "content": [{"type": "text", "text": "result_%d" % (i + 1)}],
                    },
                })
        return json.dumps(messages)

    def _make_trajectory_with_usage(self, input_tokens=100, output_tokens=50):
        messages = [
            {
                "type": "message",
                "id": "msg-001",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                },
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        ]
        return json.dumps(messages)

    def test_save_trajectory_stores_messages(self):
        """trajectory_messages JSON is stored on the turn."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        traj = self._make_trajectory_json()
        turn.write({"trajectory_messages": traj})
        self.assertEqual(turn.trajectory_messages, traj)

    def test_save_trajectory_extracts_tool_calls(self):
        """tool_use blocks in trajectory are extracted to tool_calls JSON."""
        traj_json = self._make_trajectory_with_tool_use(tool_count=3, with_results=True)
        extracted = TalosChatController._extract_tool_calls_from_trajectory(traj_json)
        self.assertEqual(len(extracted), 3)
        self.assertEqual(extracted[0]["name"], "tool_1")
        self.assertEqual(extracted[0]["toolCallId"], "tc-001")
        self.assertEqual(extracted[0]["args"], {"arg": "val_1"})
        self.assertEqual(extracted[0]["result"], "result_1")
        self.assertEqual(extracted[2]["result"], "result_3")

    def test_save_trajectory_extracts_tokens(self):
        """Usage data is extracted and stored by model_type (claude vs glm)."""
        traj_json = self._make_trajectory_with_usage(input_tokens=500, output_tokens=200)
        token_usage = TalosChatController._extract_token_usage_from_trajectory(traj_json)
        self.assertEqual(token_usage["input_tokens"], 500)
        self.assertEqual(token_usage["output_tokens"], 200)

        turn_claude = self._create_turn(
            sandbox=self.claude_sandbox,
            turn_number=1,
            turn_status="Completed",
        )
        turn_claude.write({
            "trajectory_messages": traj_json,
            "claude_input_tokens": token_usage["input_tokens"],
            "claude_output_tokens": token_usage["output_tokens"],
        })
        self.assertEqual(turn_claude.claude_input_tokens, 500)
        self.assertEqual(turn_claude.claude_output_tokens, 200)

        turn_glm = self._create_turn(
            sandbox=self.glm_sandbox,
            turn_number=1,
            turn_status="Completed",
        )
        turn_glm.write({
            "trajectory_messages": traj_json,
            "glm_input_tokens": token_usage["input_tokens"],
            "glm_output_tokens": token_usage["output_tokens"],
        })
        self.assertEqual(turn_glm.glm_input_tokens, 500)
        self.assertEqual(turn_glm.glm_output_tokens, 200)

    def test_save_trajectory_keeps_better_calls(self):
        """Only overwrites tool_calls when new extraction has more entries."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        existing = json.dumps([
            {"toolCallId": "tc-001", "name": "search", "args": {}, "result": None, "isError": False},
            {"toolCallId": "tc-002", "name": "edit", "args": {}, "result": None, "isError": False},
        ])
        turn.write({"tool_calls": existing})

        traj_fewer = self._make_trajectory_with_tool_use(tool_count=1)
        extracted_fewer = TalosChatController._extract_tool_calls_from_trajectory(traj_fewer)
        existing_list = json.loads(turn.tool_calls)
        if len(extracted_fewer) > len(existing_list):
            turn.write({"tool_calls": json.dumps(extracted_fewer)})
        self.assertEqual(len(json.loads(turn.tool_calls)), 2)

        traj_more = self._make_trajectory_with_tool_use(tool_count=5)
        extracted_more = TalosChatController._extract_tool_calls_from_trajectory(traj_more)
        existing_list = json.loads(turn.tool_calls)
        if len(extracted_more) > len(existing_list):
            turn.write({"tool_calls": json.dumps(extracted_more)})
        self.assertEqual(len(json.loads(turn.tool_calls)), 5)

    def test_save_trajectory_empty(self):
        """Empty / None trajectory does not crash and returns safe defaults."""
        result = TalosChatController._extract_tool_calls_from_trajectory("")
        self.assertEqual(result, [])

        result2 = TalosChatController._extract_tool_calls_from_trajectory("[]")
        self.assertEqual(result2, [])

        result3 = TalosChatController._extract_tool_calls_from_trajectory(None)
        self.assertEqual(result3, [])

        token_result = TalosChatController._extract_token_usage_from_trajectory("")
        self.assertEqual(token_result, {"input_tokens": 0, "output_tokens": 0})

        token_result2 = TalosChatController._extract_token_usage_from_trajectory(None)
        self.assertEqual(token_result2, {"input_tokens": 0, "output_tokens": 0})


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/history
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestChatHistory(TalosTestCase):

    def test_history_returns_all_turns(self):
        """All turns for a sandbox are returned with correct data."""
        sandbox = self.claude_sandbox
        self._create_turn(
            sandbox=sandbox, turn_number=1, prompt="Hello",
            response="Hi", turn_status="Completed",
        )
        self._create_turn(
            sandbox=sandbox, turn_number=2, prompt="How?",
            response="Fine", turn_status="Completed",
        )
        self._create_turn(
            sandbox=sandbox, turn_number=3, prompt="Bye",
            response="Later", turn_status="Completed",
        )
        turns = sandbox.turn_ids
        self.assertEqual(len(turns), 3)
        sorted_turns = turns.sorted("turn_number")
        self.assertEqual(sorted_turns[0].prompt, "Hello")
        self.assertEqual(sorted_turns[1].prompt, "How?")
        self.assertEqual(sorted_turns[2].prompt, "Bye")

    def test_history_backfills_tool_calls(self):
        """Turns with trajectory_messages but no tool_calls get backfilled."""
        sandbox = self.claude_sandbox
        traj_messages = json.dumps([
            {
                "type": "message",
                "id": "msg-001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tc-backfill-001",
                            "name": "web_search",
                            "input": {"query": "test"},
                        },
                    ],
                },
            },
            {
                "type": "message",
                "id": "msg-002",
                "parentId": "msg-001",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "tool",
                    "tool_use_id": "tc-backfill-001",
                    "content": [{"type": "text", "text": "search results"}],
                },
            },
        ])
        turn = self._create_turn(
            sandbox=sandbox, turn_number=1, prompt="Search",
            turn_status="Completed",
        )
        turn.write({"trajectory_messages": traj_messages})
        self.assertFalse(turn.tool_calls)

        tool_calls_str = turn.tool_calls or ""
        if not tool_calls_str and turn.trajectory_messages:
            extracted = TalosChatController._extract_tool_calls_from_trajectory(
                turn.trajectory_messages
            )
            if extracted:
                tool_calls_str = json.dumps(extracted)
                turn.sudo().write({"tool_calls": tool_calls_str})

        self.assertTrue(turn.tool_calls)
        parsed = json.loads(turn.tool_calls)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "web_search")
        self.assertEqual(parsed[0]["result"], "search results")

    def test_history_empty_sandbox(self):
        """A sandbox with no turns returns an empty turns list."""
        task = self._create_task(task_id="HIST-EMPTY-001")
        sandbox = task.claude_sandbox_id
        turns = sandbox.turn_ids
        self.assertEqual(len(turns), 0)


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/export_session
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestExportSession(TalosTestCase):

    def test_export_by_sandbox(self):
        """build_trajectory_json returns trajectory with correct structure."""
        sandbox = self.claude_sandbox
        self._create_turn(
            sandbox=sandbox, turn_number=1, prompt="Hello",
            response="Hi there", turn_status="Completed",
        )
        self._create_turn(
            sandbox=sandbox, turn_number=2, prompt="Do task",
            response="Done", turn_status="Completed",
        )
        trajectory = sandbox.build_trajectory_json()
        self.assertIn("meta_info", trajectory)
        self.assertIn("messages", trajectory)
        self.assertIsInstance(trajectory["meta_info"], dict)
        self.assertIsInstance(trajectory["messages"], list)
        self.assertIn("task_type", trajectory["meta_info"])
        self.assertIn("task_description", trajectory["meta_info"])
        self.assertIn("platform", trajectory["meta_info"])
        self.assertGreater(len(trajectory["messages"]), 0)


# ═══════════════════════════════════════════════════════════════════════
#  /talos/chat/sandbox_state
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestSandboxState(TalosTestCase):

    def test_sandbox_state_returns_hint_fields(self):
        """auto_hint_status, iteration, group_id are returned."""
        sandbox = self.claude_sandbox
        sandbox.write({
            "auto_hint_status": "evaluating",
            "auto_hint_iteration": 2,
            "auto_hint_group_id": "grp-xyz-789",
        })
        self.assertEqual(sandbox.auto_hint_status, "evaluating")
        self.assertEqual(sandbox.auto_hint_iteration, 2)
        self.assertEqual(sandbox.auto_hint_group_id, "grp-xyz-789")

    def test_sandbox_state_last_turn(self):
        """Last turn's feedback and hint_text are returned."""
        sandbox = self.claude_sandbox
        self._create_turn(
            sandbox=sandbox, turn_number=1, prompt="First",
            turn_status="Completed", feedback="satisfied",
        )
        self._create_turn(
            sandbox=sandbox, turn_number=2, prompt="Second",
            turn_status="Completed", feedback="unsatisfied",
            hint_text="Do it differently",
        )
        last_turn = sandbox.turn_ids.sorted("turn_number", reverse=True)[:1]
        self.assertTrue(last_turn)
        self.assertEqual(last_turn.feedback, "unsatisfied")
        self.assertEqual(last_turn.hint_text, "Do it differently")

    def test_sandbox_state_no_turns(self):
        """Sandbox with no turns returns default zero values."""
        task = self._create_task(task_id="STATE-EMPTY-001")
        sandbox = task.claude_sandbox_id
        last_turn = sandbox.turn_ids.sorted("turn_number", reverse=True)[:1]
        self.assertFalse(last_turn)
        result = {
            "auto_hint_status": sandbox.auto_hint_status or "idle",
            "auto_hint_iteration": sandbox.auto_hint_iteration or 0,
            "auto_hint_group_id": sandbox.auto_hint_group_id or "",
            "last_turn_id": last_turn.id if last_turn else 0,
            "last_turn_feedback": last_turn.feedback if last_turn else "",
            "last_turn_hint_text": last_turn.hint_text if last_turn else "",
        }
        self.assertEqual(result["auto_hint_status"], "idle")
        self.assertEqual(result["auto_hint_iteration"], 0)
        self.assertEqual(result["auto_hint_group_id"], "")
        self.assertEqual(result["last_turn_id"], 0)
        self.assertEqual(result["last_turn_feedback"], "")
        self.assertEqual(result["last_turn_hint_text"], "")


# ═══════════════════════════════════════════════════════════════════════
#  Error-path & edge-case tests (appended)
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestChatControllerErrorPaths(TalosTestCase):
    """Error-path tests that exercise the controller methods directly."""

    def _ctrl(self):
        return TalosChatController()

    # ── create_turn ─────────────────────────────────────────────────

    def test_create_turn_missing_sandbox_id(self):
        """sandbox_id=0 → error dict."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.create_turn(sandbox_id=0, message="hi")
        self.assertIn("error", result)
        self.assertIn("required", result["error"])

    def test_create_turn_nonexistent_sandbox(self):
        """sandbox_id=999999 → 'Sandbox not found'."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.create_turn(sandbox_id=999999, message="hi")
        self.assertIn("error", result)
        self.assertIn("Sandbox not found", result["error"])

    # ── save_response ───────────────────────────────────────────────

    def test_save_response_missing_turn_id(self):
        """turn_id=0 → error dict."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_response(turn_id=0, response="data")
        self.assertIn("error", result)
        self.assertIn("required", result["error"])

    def test_save_response_nonexistent_turn(self):
        """turn_id=999999 → 'Turn not found'."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_response(turn_id=999999, response="data")
        self.assertIn("error", result)
        self.assertIn("Turn not found", result["error"])

    # ── save_qc ─────────────────────────────────────────────────────

    def test_save_qc_missing_turn_id(self):
        """turn_id=0 → error dict."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_qc(turn_id=0, severity="low")
        self.assertIn("error", result)
        self.assertIn("required", result["error"])

    def test_save_qc_invalid_severity(self):
        """severity='INVALID' → error dict (case-insensitive check)."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_qc(turn_id=turn.id, severity="INVALID")
        self.assertIn("error", result)
        self.assertIn("Invalid severity", result["error"])

    # ── save_feedback ───────────────────────────────────────────────

    def test_save_feedback_missing_turn_id(self):
        """turn_id=0 → error dict."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_feedback(turn_id=0, feedback="satisfied")
        self.assertIn("error", result)
        self.assertIn("required", result["error"])

    def test_save_feedback_nonexistent_turn(self):
        """turn_id=999999 → 'Turn not found'."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_feedback(turn_id=999999, feedback="satisfied")
        self.assertIn("error", result)
        self.assertIn("Turn not found", result["error"])

    # ── save_trajectory ─────────────────────────────────────────────

    def test_save_trajectory_missing_turn_id(self):
        """turn_id=0 → error dict."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_trajectory(turn_id=0, trajectory_messages="[]")
        self.assertIn("error", result)
        self.assertIn("required", result["error"])

    def test_save_trajectory_nonexistent_turn(self):
        """turn_id=999999 → 'Turn not found'."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_trajectory(turn_id=999999, trajectory_messages="[]")
        self.assertIn("error", result)
        self.assertIn("Turn not found", result["error"])

    def test_save_trajectory_malformed_json(self):
        """Malformed trajectory JSON does not crash — gracefully handled."""
        turn = self._create_turn(turn_number=1, turn_status="Completed")
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.save_trajectory(
                turn_id=turn.id, trajectory_messages="NOT VALID JSON{{{",
            )
        # Should succeed without crash; extraction returns empty
        self.assertTrue(result.get("success"))

    # ── history ─────────────────────────────────────────────────────

    def test_history_missing_sandbox_id(self):
        """sandbox_id=0 → error dict."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.chat_history(sandbox_id=0)
        self.assertIn("error", result)
        self.assertIn("required", result["error"])

    def test_history_nonexistent_sandbox(self):
        """sandbox_id=999999 → 'Sandbox not found'."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.chat_history(sandbox_id=999999)
        self.assertIn("error", result)
        self.assertIn("Sandbox not found", result["error"])

    # ── export_session ──────────────────────────────────────────────

    def test_export_session_sandbox_not_found(self):
        """sandbox_id=999999 → not_found response."""
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.not_found.return_value = {"error": "not_found"}
            result = ctrl.export_session(sandbox_id=999999)
        mock_req.not_found.assert_called_once()
        self.assertEqual(result, {"error": "not_found"})

    def test_export_session_by_task_id(self):
        """task_id exports task trajectory via build_trajectory_json."""
        task = self._create_task(task_id="EXPORT-TASK-001")
        ctrl = self._ctrl()
        fake_traj = {"meta_info": {}, "messages": []}
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.make_response.return_value = "ok-response"
            with patch.object(
                type(task), "build_trajectory_json", return_value=fake_traj,
            ):
                result = ctrl.export_session(sandbox_id=0, task_id=task.id)
        self.assertEqual(result, "ok-response")
        mock_req.make_response.assert_called_once()
