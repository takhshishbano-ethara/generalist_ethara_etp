# -*- coding: utf-8 -*-
"""Cross-cutting integration tests for the Talos module.

These tests exercise real ORM operations (TransactionCase) to verify
that models, sandboxes, turns, and trajectories interact correctly
end-to-end.
"""
import json
from unittest.mock import patch

from odoo.tests import tagged

from .common import TalosTestCase
from ..models.talos_sandbox import TRAJECTORY_FIELD_MAP


# ── Full Lifecycle ──────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestFullLifecycle(TalosTestCase):
    """Verify task → sandbox → turn → trajectory pipeline."""

    def test_turn_creation_and_trajectory_build(self):
        """Create task → sandbox → turn with trajectory_messages → build_trajectory_json."""
        task = self._create_task(
            seed_prompt="Trajectory build test",
            task_type="research_and_analysis",
            system_prompt="You are a test assistant.",
        )
        sandbox = task.claude_sandbox_id

        messages = [
            {
                "type": "message", "id": "m1", "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            },
            {
                "type": "message", "id": "m2", "parentId": "m1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]},
            },
        ]
        self._create_turn(
            sandbox=sandbox,
            turn_number=1,
            prompt="Hello",
            response="Hi!",
            trajectory_messages=json.dumps(messages),
        )

        result = sandbox.build_trajectory_json()
        meta = result["meta_info"]
        self.assertEqual(meta["task_type"], "research_and_analysis")
        self.assertEqual(meta["persona"], task.persona_id.name)
        self.assertIn("model", meta)
        self.assertGreaterEqual(len(result["messages"]), 2)

    def test_multi_turn_trajectory_picks_longest(self):
        """_trajectory_from_ws picks the turn with the most messages."""
        task = self._create_task(seed_prompt="Multi-turn test")
        sandbox = task.claude_sandbox_id

        short_msgs = [
            {"type": "message", "id": "s1", "parentId": None,
             "timestamp": "2026-01-01T00:00:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Q1"}]}},
            {"type": "message", "id": "s2", "parentId": "s1",
             "timestamp": "2026-01-01T00:00:01Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "A1"}]}},
        ]
        long_msgs = [
            {"type": "message", "id": "l%d" % i, "parentId": ("l%d" % (i - 1)) if i else None,
             "timestamp": "2026-01-01T00:00:%02dZ" % i,
             "message": {"role": "user" if i % 2 == 0 else "assistant",
                         "content": [{"type": "text", "text": "msg-%d" % i}]}}
            for i in range(5)
        ]

        self._create_turn(
            sandbox=sandbox, turn_number=1,
            prompt="Q1", response="A1",
            trajectory_messages=json.dumps(short_msgs),
        )
        self._create_turn(
            sandbox=sandbox, turn_number=2,
            prompt="Q2", response="A2",
            trajectory_messages=json.dumps(long_msgs),
        )

        result = sandbox._trajectory_from_ws()
        self.assertEqual(len(result), 5, "Should pick the longer trajectory (5 messages)")

    def test_export_clears_turns(self):
        """Extends test_sandbox_data::test_export_traj_clears_turns —
        additionally verifies TRAJECTORY_FIELD_MAP routing and that the
        target task field is populated with a valid JSON list."""
        task = self._create_task(seed_prompt="Export test")
        sandbox = task.claude_sandbox_id

        self._create_turn(sandbox=sandbox, turn_number=1, prompt="Q", response="A")
        self.assertTrue(sandbox.turn_ids, "Turns should exist before export")

        jsonl_entries = self._make_jsonl_entries()
        with patch.object(type(sandbox), "_read_session_jsonl", return_value=jsonl_entries), \
             patch.object(type(sandbox), "_query_litellm_spend", return_value=(0, 0)):
            sandbox._export_trajectory_to_task()

        sandbox.invalidate_recordset()
        self.assertFalse(sandbox.turn_ids, "Turns should be unlinked after export")

        field_name = TRAJECTORY_FIELD_MAP["claude"]
        raw = task[field_name]
        self.assertTrue(raw, "Trajectory field should be populated")
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)


# ── Cascading Deletes ───────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestCascadingDeletes(TalosTestCase):
    """Verify ondelete='cascade' behaviour between models."""

    def test_task_deletion_cascades_to_sandboxes(self):
        """Deleting a task removes all its sandboxes."""
        task = self._create_task(seed_prompt="Cascade task delete")
        sandbox_ids = task.sandbox_ids.ids
        self.assertTrue(sandbox_ids, "Task should have sandboxes")

        task.unlink()

        remaining = self.Sandbox.search([("id", "in", sandbox_ids)])
        self.assertFalse(remaining, "All sandboxes should be deleted with the task")


# ── Token Aggregation ───────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestTokenAggregation(TalosTestCase):
    """Verify per-model token fields stay independent."""

    def test_token_fields_independent_per_model_type(self):
        """Claude and GLM token counts are written to separate task fields."""
        task = self._create_task(seed_prompt="Token independence test")
        claude_sb = task.claude_sandbox_id
        glm_sb = task.glm_sandbox_id

        self._create_turn(
            sandbox=claude_sb, turn_number=1,
            prompt="Q", response="A",
            claude_input_tokens=100, claude_output_tokens=50,
        )
        self._create_turn(
            sandbox=glm_sb, turn_number=1,
            prompt="Q", response="A",
            model_name="litellm/kimi-k2.5",
            glm_input_tokens=200, glm_output_tokens=75,
        )

        with patch.object(type(claude_sb), "_read_session_jsonl", return_value=[]), \
             patch.object(type(claude_sb), "_query_litellm_spend", return_value=(0, 0)):
            claude_sb._export_trajectory_to_task()

        with patch.object(type(glm_sb), "_read_session_jsonl", return_value=[]), \
             patch.object(type(glm_sb), "_query_litellm_spend", return_value=(0, 0)):
            glm_sb._export_trajectory_to_task()

        task.invalidate_recordset()
        self.assertEqual(task.claude_input_tokens, 100)
        self.assertEqual(task.claude_output_tokens, 50)
        self.assertEqual(task.glm_input_tokens, 200)
        self.assertEqual(task.glm_output_tokens, 75)

    def test_trajectory_field_map_all_model_types(self):
        """TRAJECTORY_FIELD_MAP covers every model type with correct field names."""
        expected = {
            "claude": "claude_trajectory",
            "glm": "glm_trajectory",
            "1pa": "onePA_trajectory",
            "1pb": "onePB_trajectory",
            "1pc": "onePC_trajectory",
            "1pd": "onePD_trajectory",
        }
        self.assertEqual(TRAJECTORY_FIELD_MAP, expected)


# ── Persona Propagation ─────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestPersonaPropagation(TalosTestCase):
    """Verify persona data flows through task → sandbox chain."""

    def test_persona_fields_available_for_sandbox(self):
        """Sandbox can reach persona fields through talos_id.persona_id."""
        task = self._create_task(seed_prompt="Persona propagation test")
        sandbox = task.claude_sandbox_id

        persona = sandbox.talos_id.persona_id
        self.assertTrue(persona, "Sandbox should reach persona via talos_id")
        self.assertEqual(persona.soul_md, self.persona.soul_md)
        self.assertEqual(persona.memory_md, self.persona.memory_md)
        self.assertEqual(persona.agents_md, self.persona.agents_md)

    def test_task_without_persona_still_builds_trajectory(self):
        """build_trajectory_json works when persona fields are minimal."""
        minimal_persona = self.Persona.create({
            "name": "empty-persona",
            "soul_md": "",
            "memory_md": "",
            "agents_md": "",
        })
        task = self._create_task(
            persona_id=minimal_persona.id,
            seed_prompt="No-persona test",
        )
        sandbox = task.claude_sandbox_id

        self._create_turn(
            sandbox=sandbox, turn_number=1,
            prompt="Hello", response="World",
        )

        result = sandbox.build_trajectory_json()
        meta = result["meta_info"]
        self.assertEqual(meta["persona"], "empty-persona")
        self.assertIn("messages", result)
