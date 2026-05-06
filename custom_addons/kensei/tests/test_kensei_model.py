# -*- coding: utf-8 -*-
"""Comprehensive tests for Kensei, KenseiTurn, KenseiTaxonomy models
and utility functions in models/kensei.py.
"""
import json
from unittest.mock import patch, MagicMock

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import KenseiTestCase


# ═══════════════════════════════════════════════════════════════════════
# 1. Kensei record creation
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiCreation(KenseiTestCase):
    """Tests for Kensei record creation, ensure_sandboxes, and field defaults."""

    def test_create_auto_creates_sandboxes(self):
        """create() calls ensure_sandboxes — active model types exist."""
        task = self._create_task(task_id="CREATE-001")
        types = task.sandbox_ids.mapped("model_type")
        for mt in ("claude", "glm"):
            self.assertIn(mt, types)
        self.assertEqual(len(task.sandbox_ids), 2)

    def test_create_without_persona_raises(self):
        """persona_id is required — creating without it raises."""
        with self.assertRaises(Exception):
            self.Talos.create({"task_id": "FAIL-001", "task_status": "NotSubmitted"})

    def test_batch_create(self):
        """model_create_multi handles batch creation properly."""
        records = self.Talos.create([
            {"persona_id": self.persona.id, "task_id": "BATCH-A"},
            {"persona_id": self.persona.id, "task_id": "BATCH-B"},
        ])
        self.assertEqual(len(records), 2)
        for rec in records:
            self.assertEqual(len(rec.sandbox_ids), 6)

    def test_ensure_sandboxes_idempotent(self):
        """Calling ensure_sandboxes twice does not duplicate sandboxes."""
        task = self._create_task(task_id="IDEMPOTENT-001")
        count_before = len(task.sandbox_ids)
        task.ensure_sandboxes()
        self.assertEqual(len(task.sandbox_ids), count_before)

    def test_field_defaults(self):
        """Default values for selection/integer fields on a new record."""
        task = self._create_task(task_id="DEFAULTS-001")
        self.assertEqual(task.qc_status, "pending")
        self.assertEqual(task.golden_status, "idle")
        self.assertEqual(task.task_description_status, "idle")
        self.assertEqual(task.auto_process_status, "none")
        self.assertEqual(task.claude_input_tokens, 0)
        self.assertEqual(task.golden_input_tokens, 0)

    def test_employee_default(self):
        """employee_id defaults to the current user's employee."""
        task = self._create_task()
        self.assertEqual(task.employee_id, self.employee)


# ═══════════════════════════════════════════════════════════════════════
# 2. Computed fields
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiComputedFields(KenseiTestCase):
    """Tests for computed/related fields on the Kensei model."""

    def test_sandbox_ids_mapping_claude(self):
        """claude_sandbox_id resolves correctly."""
        self.assertTrue(self.task.claude_sandbox_id)
        self.assertEqual(self.task.claude_sandbox_id.model_type, "claude")

    def test_sandbox_ids_mapping_glm(self):
        """glm_sandbox_id resolves correctly."""
        self.assertTrue(self.task.glm_sandbox_id)
        self.assertEqual(self.task.glm_sandbox_id.model_type, "glm")

    def test_sandbox_ids_mapping_oneP_absent(self):
        """oneP_sandbox_id is False when no '1p' sandbox exists."""
        self.assertFalse(self.task.oneP_sandbox_id)

    def test_related_status_fields(self):
        """claude_status / glm_status are related to sandbox docker_status."""
        self.assertEqual(self.task.claude_status, self.task.claude_sandbox_id.docker_status)

    def test_related_session_status(self):
        """claude_session_status propagates from sandbox."""
        self.assertEqual(
            self.task.claude_session_status,
            self.task.claude_sandbox_id.session_status,
        )

    def test_is_kensei_admin_not_admin(self):
        """is_kensei_admin is False for non-admin users."""
        # Default test user typically is not in quality_lead group
        with patch.object(type(self.env.user), "has_group", return_value=False):
            self.task.invalidate_recordset(["is_kensei_admin"])
            self.task._compute_is_kensei_admin()
            self.assertFalse(self.task.is_kensei_admin)

    def test_is_kensei_admin_admin(self):
        """is_kensei_admin is True for quality lead users."""
        with patch.object(type(self.env.user), "has_group", return_value=True):
            self.task.invalidate_recordset(["is_kensei_admin"])
            self.task._compute_is_kensei_admin()
            self.assertTrue(self.task.is_kensei_admin)


# ═══════════════════════════════════════════════════════════════════════
# 3. Trajectory building and export
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiTrajectory(KenseiTestCase):
    """Tests for build_trajectory_json, fallback, events parsing,
    delete entry, and export_and_clear."""

    def test_build_trajectory_json_empty_turns(self):
        """build_trajectory_json works with no turns — produces empty messages."""
        task = self._create_task(task_id="TRAJ-EMPTY")
        traj = task.build_trajectory_json()
        self.assertIn("meta_info", traj)
        self.assertIn("messages", traj)
        self.assertEqual(len(traj["messages"]), 0)

    def test_build_trajectory_json_meta_info(self):
        """meta_info includes task_type, persona name, model, difficulty."""
        self.task.write({"task_type": "research_and_analysis", "difficulty": "single_app"})
        turn = self._create_turn(
            sandbox=self.claude_sandbox,
            turn_number=1,
            prompt="Hello",
            response="Hi",
        )
        traj = self.task.build_trajectory_json()
        meta = traj["meta_info"]
        self.assertEqual(meta["task_type"], "research_and_analysis")
        self.assertEqual(meta["difficulty"], "single_app")
        self.assertEqual(meta["persona"], self.persona.name)

    def test_build_trajectory_fallback_with_prompt_and_response(self):
        """Fallback builds user+assistant messages from turn prompt/response."""
        self._create_turn(
            sandbox=self.claude_sandbox,
            turn_number=1,
            prompt="What is 2+2?",
            response="4",
            model_name="litellm/claude-opus-4.7",
        )
        traj = self.task.build_trajectory_json()
        msgs = traj["messages"]
        roles = []
        for m in msgs:
            inner = m.get("message", m)
            if isinstance(inner, dict) and "message" in inner:
                inner = inner["message"]
            roles.append(inner.get("role", ""))
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_build_trajectory_fallback_tool_calls(self):
        """Fallback includes tool call + tool result when tool_calls JSON present."""
        self._create_turn(
            sandbox=self.claude_sandbox,
            turn_number=1,
            prompt="Search something",
            tool_calls=self._make_tool_calls_json(),
        )
        traj = self.task.build_trajectory_json()
        msgs = traj["messages"]
        # user msg + 2 tool calls + 2 tool results = 5
        self.assertEqual(len(msgs), 5)

    def test_build_trajectory_from_events(self):
        """_build_trajectory_from_events parses raw WS events."""
        events = [
            {"stream": "assistant", "data": {"text": "thinking..."}, "ts": "t1", "runId": "run-1"},
            {"stream": "tool", "data": {"phase": "start", "toolCallId": "tc1", "name": "bash", "args": {}}, "ts": "t2"},
            {"stream": "tool", "data": {"phase": "end", "toolCallId": "tc1", "result": "done"}, "ts": "t3"},
            {"stream": "lifecycle", "data": {"phase": "end"}, "ts": "t4"},
        ]
        messages, counter, parent_id = self.Talos._build_trajectory_from_events(
            events, [], 0, None, "claude-opus-4.7"
        )
        # assistant text + tool call + tool result = 3
        roles = []
        for m in messages:
            inner = m.get("message", {})
            roles.append(inner.get("role", ""))
        self.assertIn("assistant", roles)
        self.assertIn("toolResult", roles)
        self.assertEqual(len(messages), 3)

    def test_build_trajectory_from_events_trailing_text(self):
        """Trailing assistant text without lifecycle end is still emitted."""
        events = [
            {"stream": "assistant", "data": {"text": "final answer"}, "ts": "t1", "runId": "r1"},
        ]
        messages, _, _ = self.Talos._build_trajectory_from_events(
            events, [], 0, None, "model-x"
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message"]["role"], "assistant")

    def test_trajectory_from_ws_picks_longest(self):
        """_trajectory_from_ws picks the turn with the most trajectory_messages."""
        short_msgs = json.dumps([{"type": "message", "id": "1", "message": {"role": "user"}}])
        long_msgs = json.dumps([
            {"type": "message", "id": "1", "message": {"role": "user"}},
            {"type": "message", "id": "2", "message": {"role": "assistant"}},
        ])
        self._create_turn(sandbox=self.claude_sandbox, turn_number=1, trajectory_messages=short_msgs)
        self._create_turn(sandbox=self.claude_sandbox, turn_number=2, trajectory_messages=long_msgs)
        result = self.task._trajectory_from_ws()
        self.assertEqual(len(result), 2)

    def test_trajectory_from_ws_malformed_json_skipped(self):
        """Turns with malformed trajectory_messages JSON are skipped."""
        self._create_turn(sandbox=self.claude_sandbox, turn_number=1, trajectory_messages="not json{{")
        result = self.task._trajectory_from_ws()
        self.assertEqual(result, [])

    def test_delete_trajectory_entry(self):
        """action_delete_trajectory_entry removes an entry by index."""
        entries = [
            self._make_session_entry(session_id="s1"),
            self._make_session_entry(session_id="s2"),
        ]
        self.task.write({"claude_trajectory": json.dumps(entries)})
        result = self.task.action_delete_trajectory_entry("claude_trajectory", 0)
        self.assertTrue(result)
        remaining = json.loads(self.task.claude_trajectory)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["session_id"], "s2")

    def test_delete_trajectory_entry_invalid_field(self):
        """action_delete_trajectory_entry raises for invalid field names."""
        with self.assertRaises(UserError):
            self.task.action_delete_trajectory_entry("seed_prompt", 0)

    def test_delete_trajectory_entry_invalid_index(self):
        """action_delete_trajectory_entry raises for out-of-range index."""
        entries = [self._make_session_entry()]
        self.task.write({"claude_trajectory": json.dumps(entries)})
        with self.assertRaises(UserError):
            self.task.action_delete_trajectory_entry("claude_trajectory", 5)

    def test_delete_trajectory_entry_corrupted_json(self):
        """action_delete_trajectory_entry raises for corrupted JSON."""
        self.task.write({"claude_trajectory": "not valid json{{"})
        with self.assertRaises(UserError):
            self.task.action_delete_trajectory_entry("claude_trajectory", 0)

    def test_delete_trajectory_entry_empty_clears_field(self):
        """Deleting the last entry sets the field to empty string."""
        entries = [self._make_session_entry()]
        self.task.write({"claude_trajectory": json.dumps(entries)})
        self.task.action_delete_trajectory_entry("claude_trajectory", 0)
        self.assertEqual(self.task.claude_trajectory, "")

    def test_export_and_clear_turns(self):
        """_export_and_clear_turns creates attachment and clears turns."""
        self._create_turn(
            sandbox=self.claude_sandbox,
            turn_number=1,
            prompt="Hello",
            response="World",
        )
        attachment = self.task._export_and_clear_turns()
        self.assertTrue(attachment.exists())
        self.assertEqual(attachment.res_model, "kensei.kensei")
        self.assertEqual(attachment.res_id, self.task.id)
        self.assertIn("session-", attachment.name)
        # Turns should be cleared
        self.assertEqual(len(self.task._get_all_turns()), 0)

    def test_export_and_clear_no_turns(self):
        """_export_and_clear_turns returns empty recordset when no turns."""
        task = self._create_task(task_id="NOTURNS-001")
        attachment = task._export_and_clear_turns()
        self.assertFalse(attachment.exists())


# ═══════════════════════════════════════════════════════════════════════
# 4. Actions
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiActions(KenseiTestCase):
    """Tests for action methods on the Kensei model."""

    def test_action_view_turns(self):
        """action_view_turns returns a window action with correct domain."""
        result = self.task.action_view_turns()
        self.assertEqual(result["type"], "ir.actions.act_window")
        self.assertEqual(result["res_model"], "kensei.turn")
        domain = result["domain"]
        # domain should filter by sandbox ids
        self.assertEqual(domain[0][0], "sandbox_id")
        self.assertEqual(domain[0][1], "in")
        self.assertEqual(domain[0][2], self.task.sandbox_ids.ids)

    def test_action_export_session(self):
        """action_export_session returns a URL action."""
        result = self.task.action_export_session()
        self.assertEqual(result["type"], "ir.actions.act_url")
        self.assertIn(str(self.task.id), result["url"])

    def test_action_clear_turns(self):
        """action_clear_turns removes all turns across sandboxes."""
        self._create_turn(sandbox=self.claude_sandbox, turn_number=1, prompt="A")
        self._create_turn(sandbox=self.glm_sandbox, turn_number=1, prompt="B")
        self.task.action_clear_turns()
        self.assertEqual(len(self.task._get_all_turns()), 0)

    def test_action_generate_golden_no_claude_trajectory(self):
        """action_generate_golden_trajectory succeeds with only GLM trajectory."""
        from odoo.addons.kensei.models.kensei import _GOLDEN_GENERATING, _GOLDEN_LOCK
        task = self._create_task(task_id="GOLDEN-NOCLAUD")
        task.write({"glm_trajectory": "some data"})
        try:
            task.action_generate_golden_trajectory()
            task.invalidate_recordset(["golden_status"])
            self.assertEqual(task.golden_status, "generating")
        finally:
            with _GOLDEN_LOCK:
                _GOLDEN_GENERATING.discard(task.id)

    def test_action_generate_golden_no_glm_trajectory(self):
        """action_generate_golden_trajectory succeeds with only Claude trajectory."""
        from odoo.addons.kensei.models.kensei import _GOLDEN_GENERATING, _GOLDEN_LOCK
        task = self._create_task(task_id="GOLDEN-NOGLM")
        task.write({"claude_trajectory": "some data"})
        try:
            task.action_generate_golden_trajectory()
            task.invalidate_recordset(["golden_status"])
            self.assertEqual(task.golden_status, "generating")
        finally:
            with _GOLDEN_LOCK:
                _GOLDEN_GENERATING.discard(task.id)

    def test_action_generate_golden_no_trajectories_at_all(self):
        """action_generate_golden_trajectory raises when both trajectories missing."""
        task = self._create_task(task_id="GOLDEN-NOTRAJ")
        with self.assertRaises(UserError):
            task.action_generate_golden_trajectory()

    def test_action_generate_task_description_no_trajectory(self):
        """action_generate_task_description raises without any trajectory."""
        task = self._create_task(task_id="TASKDESC-NOTRAJ")
        with self.assertRaises(UserError):
            task.action_generate_task_description()

    def test_deployment_mode_default(self):
        """_deployment_mode returns 'local' when not configured."""
        mode = self.task._deployment_mode()
        self.assertEqual(mode, "local")

    def test_deployment_mode_k8s(self):
        """_deployment_mode returns 'k8s' when configured."""
        self._set_param("kensei.deployment_mode", "k8s")
        mode = self.task._deployment_mode()
        self.assertEqual(mode, "k8s")


# ═══════════════════════════════════════════════════════════════════════
# 5. Auto-process (publish, claim, mark_done)
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiAutoProcess(KenseiTestCase):
    """Tests for auto-process lifecycle methods."""

    def test_claim_task_not_found(self):
        """auto_process_claim_task skips when task doesn't exist."""
        result = self.Talos.auto_process_claim_task(999999)
        self.assertTrue(result.get("skip"))
        self.assertEqual(result["reason"], "not_found")

    def test_claim_task_wrong_status(self):
        """auto_process_claim_task skips when status is not 'queued'."""
        self.task.write({"auto_process_status": "none"})
        result = self.Talos.auto_process_claim_task(self.task.id)
        self.assertTrue(result.get("skip"))
        self.assertIn("status_", result["reason"])

    def test_claim_task_success(self):
        """auto_process_claim_task succeeds for a queued task."""
        self.task.write({"auto_process_status": "queued", "initial_prompt": "Do something"})
        result = self.Talos.auto_process_claim_task(self.task.id)
        self.assertFalse(result.get("skip", False))
        self.assertEqual(result["task_id"], self.task.id)
        self.assertIn("sandbox_id", result)
        # Status should now be 'processing'
        self.task.invalidate_recordset(["auto_process_status"])
        self.assertEqual(self.task.auto_process_status, "processing")

    def test_claim_task_with_existing_turns_skips(self):
        """auto_process_claim_task skips when sandbox already has turns."""
        self.task.write({"auto_process_status": "queued", "initial_prompt": "Go"})
        self._create_turn(sandbox=self.claude_sandbox, turn_number=1, prompt="existing")
        result = self.Talos.auto_process_claim_task(self.task.id)
        self.assertTrue(result.get("skip"))
        self.assertEqual(result["reason"], "has_turns")

    def test_mark_done_success(self):
        """auto_process_mark_done writes 'done' status."""
        self.task.write({"auto_process_status": "processing"})
        result = self.Talos.auto_process_mark_done(self.task.id, status="done")
        self.assertTrue(result)
        self.task.invalidate_recordset(["auto_process_status"])
        self.assertEqual(self.task.auto_process_status, "done")

    def test_mark_done_with_error(self):
        """auto_process_mark_done writes error text (truncated to 2000)."""
        long_error = "x" * 3000
        self.Talos.auto_process_mark_done(self.task.id, status="failed", error=long_error)
        self.task.invalidate_recordset(["auto_process_error", "auto_process_status"])
        self.assertEqual(self.task.auto_process_status, "failed")
        self.assertEqual(len(self.task.auto_process_error), 2000)

    def test_mark_done_nonexistent_task(self):
        """auto_process_mark_done returns False for missing task."""
        result = self.Talos.auto_process_mark_done(999999)
        self.assertFalse(result)

    def test_publish_auto_process_filters_ineligible(self):
        """action_publish_auto_process filters tasks without initial_prompt."""
        task = self._create_task(task_id="AUTOPUB-NOIP", auto_process_status="none")
        # No initial_prompt → should be filtered out
        with patch(
            "odoo.addons.kensei.services.rabbitmq_service.batch_publish_auto_process_tasks"
        ) as mock_publish:
            result = task.action_publish_auto_process()
            mock_publish.assert_not_called()
            self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════
# 6. KenseiTurn model
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiTurn(KenseiTestCase):
    """Tests for KenseiTurn model: defaults, ordering, computed, cascade."""

    def test_turn_defaults(self):
        """New turn has expected default values."""
        turn = self._create_turn(turn_number=1)
        self.assertEqual(turn.bedrock_input_tokens, 0)
        self.assertEqual(turn.claude_input_tokens, 0)
        self.assertFalse(turn.is_hint_turn)
        self.assertFalse(turn.is_auto_hint)
        self.assertEqual(turn.auto_hint_iteration, 0)

    def test_turn_ordering(self):
        """Turns are ordered by turn_number asc, id asc."""
        t3 = self._create_turn(turn_number=3)
        t1 = self._create_turn(turn_number=1)
        t2 = self._create_turn(turn_number=2)
        turns = self.Turn.search([("id", "in", [t1.id, t2.id, t3.id])])
        self.assertEqual(turns[0].turn_number, 1)
        self.assertEqual(turns[1].turn_number, 2)
        self.assertEqual(turns[2].turn_number, 3)

    def test_tool_names_computed(self):
        """tool_names is computed from tool_calls JSON."""
        turn = self._create_turn(
            turn_number=1,
            tool_calls=self._make_tool_calls_json(),
        )
        self.assertIn("web_search", turn.tool_names)
        self.assertIn("edit", turn.tool_names)

    def test_tool_names_empty_when_no_calls(self):
        """tool_names is False when tool_calls is empty."""
        turn = self._create_turn(turn_number=1)
        self.assertFalse(turn.tool_names)

    def test_tool_names_malformed_json(self):
        """tool_names handles malformed JSON gracefully."""
        turn = self._create_turn(turn_number=1, tool_calls="not json{{")
        self.assertFalse(turn.tool_names)

    def test_tool_names_deduplication(self):
        """tool_names does not include duplicate tool names."""
        calls = json.dumps([
            {"name": "bash", "toolCallId": "a"},
            {"name": "bash", "toolCallId": "b"},
            {"name": "edit", "toolCallId": "c"},
        ])
        turn = self._create_turn(turn_number=1, tool_calls=calls)
        # "bash" should appear only once
        self.assertEqual(turn.tool_names.count("bash"), 1)
        self.assertIn("edit", turn.tool_names)

    def test_cascade_delete_sandbox(self):
        """Deleting a sandbox cascades to its turns."""
        task = self._create_task(task_id="CASCADE-001")
        sandbox = task.claude_sandbox_id
        turn = self._create_turn(sandbox=sandbox, turn_number=1)
        turn_id = turn.id
        sandbox.unlink()
        self.assertFalse(self.Turn.browse(turn_id).exists())

    def test_related_kensei_id(self):
        """kensei_id is related to sandbox_id.kensei_id."""
        turn = self._create_turn(sandbox=self.claude_sandbox, turn_number=1)
        self.assertEqual(turn.kensei_id, self.task)

    def test_related_employee_id(self):
        """employee_id is related through kensei_id."""
        turn = self._create_turn(sandbox=self.claude_sandbox, turn_number=1)
        self.assertEqual(turn.employee_id, self.task.employee_id)


# ═══════════════════════════════════════════════════════════════════════
# 7. KenseiTaxonomy model
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiTaxonomy(KenseiTestCase):
    """Tests for KenseiTaxonomy model."""

    def test_create_taxonomy(self):
        """Can create a taxonomy record."""
        tax = self.Taxonomy.create({"name": "test-taxonomy-unique-123"})
        self.assertTrue(tax.exists())
        self.assertEqual(tax.name, "test-taxonomy-unique-123")

    def test_name_required(self):
        """Creating taxonomy without name raises."""
        with self.assertRaises(Exception):
            self.Taxonomy.create({})


# ═══════════════════════════════════════════════════════════════════════
# 8. Utility functions
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestKenseiUtilities(KenseiTestCase):
    """Tests for module-level utility functions in models/kensei.py."""

    def test_load_dotenv_returns_dict(self):
        """_load_dotenv returns a dict containing os.environ keys."""
        from odoo.addons.kensei.models.kensei import _load_dotenv
        result = _load_dotenv()
        self.assertIsInstance(result, dict)
        # Should at least include PATH from os.environ
        self.assertIn("PATH", result)

    def test_load_dotenv_parses_env_file(self):
        """_load_dotenv parses .env file when present."""
        import tempfile
        import os
        from odoo.addons.kensei.models.kensei import _load_dotenv

        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w") as f:
                f.write("# comment\n")
                f.write("TEST_KENSEI_VAR=hello123\n")
                f.write("\n")
                f.write("ANOTHER_VAR=world\n")
                f.write("MALFORMED_LINE\n")

            with patch("odoo.addons.kensei.models.kensei.odoo_config") as mock_config:
                mock_config.rcfile = os.path.join(tmpdir, "odoo.conf")
                result = _load_dotenv()
                self.assertEqual(result.get("TEST_KENSEI_VAR"), "hello123")
                self.assertEqual(result.get("ANOTHER_VAR"), "world")

    def test_is_degenerate_output_empty(self):
        """_is_degenerate_output returns True for empty/short text."""
        from odoo.addons.kensei.models.kensei import _is_degenerate_output
        self.assertTrue(_is_degenerate_output(None))
        self.assertTrue(_is_degenerate_output(""))
        self.assertTrue(_is_degenerate_output("short"))

    def test_is_degenerate_output_repeated_chars(self):
        """_is_degenerate_output detects repeated characters."""
        from odoo.addons.kensei.models.kensei import _is_degenerate_output
        text = "normal prefix " + "a" * 20 + " suffix"
        self.assertTrue(_is_degenerate_output(text))

    def test_is_degenerate_output_low_unique_chars(self):
        """_is_degenerate_output detects low unique character count."""
        from odoo.addons.kensei.models.kensei import _is_degenerate_output
        text = "aababababababababababababababababab"
        self.assertTrue(_is_degenerate_output(text))

    def test_is_degenerate_output_normal_text(self):
        """_is_degenerate_output returns False for normal text."""
        from odoo.addons.kensei.models.kensei import _is_degenerate_output
        text = "This is a perfectly normal response with enough variety."
        self.assertFalse(_is_degenerate_output(text))

    def test_wrap_trajectory_message_user_passthrough(self):
        """User messages are returned as-is without wrapper."""
        from odoo.addons.kensei.models.kensei import _wrap_trajectory_message
        msg = {"message": {"role": "user", "content": [{"type": "text", "text": "Hi"}]}}
        result = _wrap_trajectory_message(msg, is_accepted=1, hints="some hint")
        # User messages are NOT wrapped
        self.assertNotIn("is_accepted", result)
        self.assertEqual(result, msg)

    def test_wrap_trajectory_message_assistant_wrapped(self):
        """Assistant messages get is_accepted/hints wrapper."""
        from odoo.addons.kensei.models.kensei import _wrap_trajectory_message
        msg = {"message": {"role": "assistant", "content": []}}
        result = _wrap_trajectory_message(msg, is_accepted=1, hints="fix this")
        self.assertEqual(result["is_accepted"], 1)
        self.assertEqual(result["hints"], "fix this")
        self.assertEqual(result["message"], msg)

    def test_wrap_trajectory_message_tool_result_wrapped(self):
        """toolResult messages get is_accepted/hints wrapper."""
        from odoo.addons.kensei.models.kensei import _wrap_trajectory_message
        msg = {"message": {"role": "toolResult", "content": []}}
        result = _wrap_trajectory_message(msg, is_accepted=0, hints=None)
        self.assertEqual(result["is_accepted"], 0)
        self.assertIsNone(result["hints"])

    def test_wrap_trajectory_message_auto_hint_fields(self):
        """Auto-hint fields are included when is_auto_hint=True."""
        from odoo.addons.kensei.models.kensei import _wrap_trajectory_message
        msg = {"message": {"role": "assistant", "content": []}}
        result = _wrap_trajectory_message(msg, is_auto_hint=True, auto_hint_iteration=3)
        self.assertTrue(result.get("is_auto_hint"))
        self.assertEqual(result.get("auto_hint_iteration"), 3)

    def test_wrap_trajectory_message_no_auto_hint_fields_when_false(self):
        """Auto-hint fields are NOT included when is_auto_hint=False."""
        from odoo.addons.kensei.models.kensei import _wrap_trajectory_message
        msg = {"message": {"role": "assistant", "content": []}}
        result = _wrap_trajectory_message(msg, is_auto_hint=False)
        self.assertNotIn("is_auto_hint", result)

    def test_wrap_messages_with_turn_feedback_empty_turns(self):
        """With no turns, all messages get default wrappers."""
        from odoo.addons.kensei.models.kensei import _wrap_messages_with_turn_feedback
        messages = [
            {"message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}},
        ]
        result = _wrap_messages_with_turn_feedback(messages, [])
        self.assertEqual(len(result), 2)
        # User message unchanged, assistant gets default wrapper
        self.assertEqual(result[0], messages[0])
        self.assertEqual(result[1]["is_accepted"], 0)

    def test_format_tool_result_none(self):
        """_format_tool_result returns empty string for None."""
        from odoo.addons.kensei.models.kensei import _format_tool_result
        self.assertEqual(_format_tool_result(None), "")

    def test_format_tool_result_string(self):
        """_format_tool_result returns string as-is."""
        from odoo.addons.kensei.models.kensei import _format_tool_result
        self.assertEqual(_format_tool_result("hello"), "hello")

    def test_format_tool_result_dict(self):
        """_format_tool_result serializes dict to JSON."""
        from odoo.addons.kensei.models.kensei import _format_tool_result
        result = _format_tool_result({"key": "value"})
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_format_tool_result_non_serializable(self):
        """_format_tool_result falls back to str() for non-serializable objects."""
        from odoo.addons.kensei.models.kensei import _format_tool_result
        result = _format_tool_result(object())
        self.assertIsInstance(result, str)

    def test_generate_task_description_sync_missing_creds(self):
        """generate_task_description_sync returns empty on missing creds."""
        from odoo.addons.kensei.models.kensei import generate_task_description_sync
        with patch("odoo.addons.kensei.models.kensei._load_dotenv", return_value={}):
            desc, usage = generate_task_description_sync(
                self.env, "test seed", [{"role": "user", "text": "hi"}]
            )
            self.assertEqual(desc, "")
            self.assertEqual(usage, {})

    def test_generate_task_description_sync_degenerate_discarded(self):
        """generate_task_description_sync discards degenerate output."""
        from odoo.addons.kensei.models.kensei import generate_task_description_sync
        self._set_param("kensei.bedrock_inference_arn", "arn:test")
        dotenv = {"AWS_BEARER_TOKEN_BEDROCK": "fake-key"}
        with patch("odoo.addons.kensei.models.kensei._load_dotenv", return_value=dotenv):
            with patch("odoo.addons.kensei.models.kensei._get_taskdesc_prompt", return_value="prompt"):
                with patch(
                    "odoo.addons.kensei.controllers.llm_assisst_qc._call_bedrock_converse",
                    return_value=("aaaa", {"input_tokens": 10, "output_tokens": 5}),
                ):
                    desc, usage = generate_task_description_sync(
                        self.env, "seed", "messages"
                    )
                    self.assertEqual(desc, "")
                    self.assertIn("input_tokens", usage)


# ═══════════════════════════════════════════════════════════════════════
# 9. _search_is_kensei_admin
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestAdminSearch(KenseiTestCase):

    def test_search_admin_eq_true_admin(self):
        with patch.object(type(self.env.user), "has_group", return_value=True):
            domain = self.task._search_is_kensei_admin("=", True)
            self.assertEqual(domain, [])

    def test_search_admin_eq_true_non_admin(self):
        with patch.object(type(self.env.user), "has_group", return_value=False):
            domain = self.task._search_is_kensei_admin("=", True)
            self.assertEqual(domain, [("id", "=", False)])

    def test_search_admin_unsupported_operator(self):
        with self.assertRaises(ValueError):
            self.task._search_is_kensei_admin(">", True)

    def test_search_admin_neq_false_admin(self):
        with patch.object(type(self.env.user), "has_group", return_value=True):
            domain = self.task._search_is_kensei_admin("!=", False)
            self.assertEqual(domain, [])

    def test_search_admin_eq_false_admin(self):
        with patch.object(type(self.env.user), "has_group", return_value=True):
            domain = self.task._search_is_kensei_admin("=", False)
            self.assertEqual(domain, [("id", "=", False)])


# ═══════════════════════════════════════════════════════════════════════
# 10. Generate actions (golden + task description)
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestGenerateActions(KenseiTestCase):

    def test_generate_golden_no_persona(self):
        task = self._create_task(task_id="GG-NOPERSONA")
        task.write({
            "claude_trajectory": "data",
            "glm_trajectory": "data",
            "persona_id": False,
        })
        with self.assertRaises(UserError):
            task.action_generate_golden_trajectory()

    def test_generate_golden_already_generating(self):
        from odoo.addons.kensei.models.kensei import _GOLDEN_GENERATING, _GOLDEN_LOCK
        task = self._create_task(task_id="GG-DUPE")
        task.write({
            "claude_trajectory": "data",
            "glm_trajectory": "data",
        })
        with _GOLDEN_LOCK:
            _GOLDEN_GENERATING.add(task.id)
        try:
            with self.assertRaises(UserError):
                task.action_generate_golden_trajectory()
        finally:
            with _GOLDEN_LOCK:
                _GOLDEN_GENERATING.discard(task.id)

    def test_generate_golden_sets_status(self):
        task = self._create_task(task_id="GG-STATUS")
        task.write({
            "claude_trajectory": "data",
            "glm_trajectory": "data",
        })
        from odoo.addons.kensei.models.kensei import _GOLDEN_GENERATING, _GOLDEN_LOCK
        try:
            task.action_generate_golden_trajectory()
            task.invalidate_recordset(["golden_status"])
            self.assertEqual(task.golden_status, "generating")
        finally:
            with _GOLDEN_LOCK:
                _GOLDEN_GENERATING.discard(task.id)

    def test_generate_taskdesc_already_generating(self):
        from odoo.addons.kensei.models.kensei import _TASKDESC_GENERATING, _TASKDESC_LOCK
        task = self._create_task(task_id="TD-DUPE")
        task.write({"claude_trajectory": "data"})
        with _TASKDESC_LOCK:
            _TASKDESC_GENERATING.add(task.id)
        try:
            with self.assertRaises(UserError):
                task.action_generate_task_description()
        finally:
            with _TASKDESC_LOCK:
                _TASKDESC_GENERATING.discard(task.id)

    def test_generate_taskdesc_sets_status(self):
        from odoo.addons.kensei.models.kensei import _TASKDESC_GENERATING, _TASKDESC_LOCK
        task = self._create_task(task_id="TD-STATUS")
        task.write({"claude_trajectory": "data"})
        try:
            task.action_generate_task_description()
            task.invalidate_recordset(["task_description_status"])
            self.assertEqual(task.task_description_status, "generating")
        finally:
            with _TASKDESC_LOCK:
                _TASKDESC_GENERATING.discard(task.id)


# ═══════════════════════════════════════════════════════════════════════
# 11. action_delete_trajectory_entry — extended coverage
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestDeleteTrajectoryEntry(KenseiTestCase):

    def test_delete_entry_not_list(self):
        self.task.write({"claude_trajectory": json.dumps({"key": "value"})})
        with self.assertRaises(UserError):
            self.task.action_delete_trajectory_entry("claude_trajectory", 0)

    def test_delete_entry_negative_index(self):
        entries = [self._make_session_entry()]
        self.task.write({"claude_trajectory": json.dumps(entries)})
        with self.assertRaises(UserError):
            self.task.action_delete_trajectory_entry("claude_trajectory", -1)

    def test_delete_entry_out_of_bounds(self):
        entries = [self._make_session_entry()]
        self.task.write({"claude_trajectory": json.dumps(entries)})
        with self.assertRaises(UserError):
            self.task.action_delete_trajectory_entry("claude_trajectory", 1)

    def test_delete_entry_success(self):
        entries = [
            self._make_session_entry(session_id="s1"),
            self._make_session_entry(session_id="s2"),
            self._make_session_entry(session_id="s3"),
        ]
        self.task.write({"claude_trajectory": json.dumps(entries)})
        result = self.task.action_delete_trajectory_entry("claude_trajectory", 1)
        self.assertTrue(result)
        remaining = json.loads(self.task.claude_trajectory)
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0]["session_id"], "s1")
        self.assertEqual(remaining[1]["session_id"], "s3")

    def test_delete_entry_all_valid_fields(self):
        valid_fields = [
            "claude_trajectory", "glm_trajectory",
            "onePA_trajectory", "onePB_trajectory",
            "onePC_trajectory", "onePD_trajectory",
            "golden_trajectory",
        ]
        entries = [self._make_session_entry(), self._make_session_entry(session_id="s2")]
        for field_name in valid_fields:
            self.task.write({field_name: json.dumps(entries)})
            result = self.task.action_delete_trajectory_entry(field_name, 0)
            self.assertTrue(result)
            remaining = json.loads(self.task[field_name])
            self.assertEqual(len(remaining), 1)

    def test_delete_entry_empty_field(self):
        self.task.write({"claude_trajectory": ""})
        result = self.task.action_delete_trajectory_entry("claude_trajectory", 0)
        self.assertFalse(result)


# ═══════════════════════════════════════════════════════════════════════
# 12. Auto-process (publish + claim) — extended coverage
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestAutoProcessExtended(KenseiTestCase):

    def test_publish_eligible_tasks(self):
        t1 = self._create_task(
            task_id="PUB-A",
            auto_process_status="none",
            initial_prompt="Do task A",
        )
        t2 = self._create_task(
            task_id="PUB-B",
            auto_process_status="failed",
            initial_prompt="Do task B",
        )
        records = t1 | t2
        with patch(
            "odoo.addons.kensei.services.rabbitmq_service.batch_publish_auto_process_tasks"
        ) as mock_pub:
            records.action_publish_auto_process()
            mock_pub.assert_called_once()
            published_ids = mock_pub.call_args[0][0]
            self.assertIn(t1.id, published_ids)
            self.assertIn(t2.id, published_ids)
        t1.invalidate_recordset(["auto_process_status"])
        t2.invalidate_recordset(["auto_process_status"])
        self.assertEqual(t1.auto_process_status, "queued")
        self.assertEqual(t2.auto_process_status, "queued")

    def test_publish_already_processing_filtered(self):
        t_proc = self._create_task(
            task_id="PUB-PROC",
            auto_process_status="processing",
            initial_prompt="Do it",
        )
        t_done = self._create_task(
            task_id="PUB-DONE",
            auto_process_status="done",
            initial_prompt="Do it",
        )
        t_queued = self._create_task(
            task_id="PUB-Q",
            auto_process_status="queued",
            initial_prompt="Do it",
        )
        records = t_proc | t_done | t_queued
        with patch(
            "odoo.addons.kensei.services.rabbitmq_service.batch_publish_auto_process_tasks"
        ) as mock_pub:
            result = records.action_publish_auto_process()
            mock_pub.assert_not_called()
            self.assertIsNone(result)

    def test_claim_task_success_returns_sandbox_info(self):
        task = self._create_task(
            task_id="CLAIM-OK",
            auto_process_status="queued",
            initial_prompt="Go",
        )
        claude_sb = task.claude_sandbox_id
        claude_sb.turn_ids.unlink()
        result = self.Talos.auto_process_claim_task(task.id)
        self.assertFalse(result.get("skip", False))
        self.assertEqual(result["task_id"], task.id)
        self.assertEqual(result["sandbox_id"], claude_sb.id)
        self.assertIn("docker_status", result)
        self.assertIn("initial_prompt", result)

    def test_claim_task_already_claimed(self):
        task = self._create_task(
            task_id="CLAIM-RACE",
            auto_process_status="queued",
            initial_prompt="Go",
        )
        claude_sb = task.claude_sandbox_id
        claude_sb.turn_ids.unlink()
        # First claim succeeds
        r1 = self.Talos.auto_process_claim_task(task.id)
        self.assertFalse(r1.get("skip", False))
        # Second claim: status is now 'processing', not 'queued'
        r2 = self.Talos.auto_process_claim_task(task.id)
        self.assertTrue(r2.get("skip"))
        self.assertIn("status_", r2["reason"])


# ═══════════════════════════════════════════════════════════════════════
# 13. _get_all_turns
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestGetAllTurns(KenseiTestCase):

    def test_get_all_turns_aggregates(self):
        self._create_turn(sandbox=self.claude_sandbox, turn_number=2, prompt="C2")
        self._create_turn(sandbox=self.claude_sandbox, turn_number=1, prompt="C1")
        self._create_turn(sandbox=self.glm_sandbox, turn_number=3, prompt="G3")
        turns = self.task._get_all_turns()
        numbers = turns.mapped("turn_number")
        self.assertEqual(numbers, [1, 2, 3])

    def test_get_all_turns_empty(self):
        task = self._create_task(task_id="EMPTY-TURNS")
        turns = task._get_all_turns()
        self.assertEqual(len(turns), 0)
