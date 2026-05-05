# -*- coding: utf-8 -*-
"""Shared test fixtures for the Kensei test suite.

All Kensei test classes should inherit from ``KenseiTestCase`` to get
a consistent set of pre-created records (persona, task, sandboxes,
employee) and reusable helpers.
"""
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class KenseiTestCase(TransactionCase):
    """Base test case with shared fixtures for all Kensei tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # ── Model shortcuts ─────────────────────────────────────────
        cls.Talos = cls.env["kensei.kensei"]
        cls.Sandbox = cls.env["kensei.sandbox"]
        cls.Turn = cls.env["kensei.turn"]
        cls.Persona = cls.env["kensei.persona"]
        cls.Domain = cls.env["kensei.domain"]
        cls.Taxonomy = cls.env["kensei.taxonomy"]
        cls.ICP = cls.env["ir.config_parameter"].sudo()

        # ── Persona ─────────────────────────────────────────────────
        cls.persona = cls.Persona.create({
            "name": "test-persona",
            "soul_md": "# Soul\nTest persona soul.",
            "memory_md": "# Memory\nTest persona memory.",
            "agents_md": "# Agents\nTest persona agents.",
        })

        # ── Employee (required for record rules) ────────────────────
        cls.test_user = cls.env.user
        if not cls.test_user.employee_id:
            cls.employee = cls.env["hr.employee"].create({
                "name": cls.test_user.name,
                "user_id": cls.test_user.id,
            })
        else:
            cls.employee = cls.test_user.employee_id

        # ── Task ────────────────────────────────────────────────────
        cls.task = cls.Talos.create({
            "task_id": "TEST-BASE-001",
            "persona_id": cls.persona.id,
            "task_status": "NotSubmitted",
            "seed_prompt": "Test seed prompt for unit tests.",
        })

        # ── Sandboxes (auto-created by ensure_sandboxes in create) ─
        cls.claude_sandbox = cls.task.claude_sandbox_id
        cls.glm_sandbox = cls.task.glm_sandbox_id

    # ── Helpers ─────────────────────────────────────────────────────

    @classmethod
    def _create_task(cls, **overrides):
        """Create a kensei.kensei record with sensible defaults."""
        vals = {
            "persona_id": cls.persona.id,
            "task_status": "NotSubmitted",
        }
        vals.update(overrides)
        return cls.Talos.create(vals)

    @classmethod
    def _create_sandbox(cls, task=None, model_type="claude", **overrides):
        """Create a kensei.sandbox record."""
        task = task or cls.task
        vals = {
            "kensei_id": task.id,
            "model_type": model_type,
        }
        vals.update(overrides)
        return cls.Sandbox.create(vals)

    @classmethod
    def _create_turn(cls, sandbox=None, turn_number=1, **overrides):
        """Create a kensei.turn record."""
        sandbox = sandbox or cls.claude_sandbox
        vals = {
            "sandbox_id": sandbox.id,
            "turn_number": turn_number,
            "turn_status": "Completed",
            "model_name": "litellm/claude-opus-4.7",
        }
        vals.update(overrides)
        return cls.Turn.create(vals)

    @classmethod
    def _set_param(cls, key, value):
        """Shortcut for ir.config_parameter.set_param."""
        cls.ICP.set_param(key, value)

    @staticmethod
    def _make_trajectory_json(
        task_type="home_and_organization",
        task_description="Test task for unit tests",
        system_prompt="You are a test assistant.",
        messages=None,
    ):
        """Build a minimal valid trajectory JSON string."""
        if messages is None:
            messages = [
                {
                    "type": "message",
                    "id": "msg-001",
                    "parentId": None,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                },
                {
                    "type": "message",
                    "id": "msg-002",
                    "parentId": "msg-001",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hi there!"}],
                    },
                },
            ]
        trajectory = {
            "meta_info": {
                "task_type": task_type,
                "task_description": task_description,
                "task_completion_status": "success",
                "system_prompt": system_prompt,
                "platform": "macOS",
            },
            "messages": messages,
        }
        return json.dumps(trajectory, ensure_ascii=False)

    @staticmethod
    def _make_session_entry(trajectory_json_str=None, session_id="test-session-001"):
        """Build a session entry as stored in trajectory fields."""
        if trajectory_json_str is None:
            traj = json.loads(KenseiTestCase._make_trajectory_json())
        else:
            traj = json.loads(trajectory_json_str)
        return {
            "session_id": session_id,
            "timestamp": "2026-01-01 00:00:00",
            "trajectory": traj,
        }

    @staticmethod
    def _make_jsonl_entries():
        """Build minimal JSONL entries as returned by _read_session_jsonl."""
        return [
            {
                "type": "message",
                "id": "j-001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello from JSONL"}],
                },
            },
            {
                "type": "message",
                "id": "j-002",
                "parentId": "j-001",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi from JSONL"}],
                },
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        ]

    @staticmethod
    def _make_tool_calls_json():
        """Build a JSON string of tool calls."""
        return json.dumps([
            {
                "toolCallId": "tc-001",
                "name": "web_search",
                "args": {"query": "test"},
                "result": "search results",
                "isError": False,
            },
            {
                "toolCallId": "tc-002",
                "name": "edit",
                "args": {"file": "test.py"},
                "result": None,
                "isError": False,
            },
        ])
