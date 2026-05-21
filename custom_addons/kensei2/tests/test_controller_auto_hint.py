# -*- coding: utf-8 -*-
import json
import uuid
from unittest.mock import patch, MagicMock

from odoo.tests import tagged

from .common import Kensei2TestCase

_HINT_MOD = "odoo.addons.kensei2.controllers.auto_hint"


@tagged("post_install", "-at_install")
class TestAutoHintEval(Kensei2TestCase):

    def _make_completed_turn(self, sandbox):
        return self._create_turn(
            sandbox=sandbox,
            turn_number=1,
            turn_status="Completed",
            response="Agent did the task.",
            prompt="Please do X.",
        )

    def test_auto_hint_submits_background(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        turn = self._make_completed_turn(self.claude_sandbox)
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.env.cr = self.env.cr
            mock_req.env.cr.dbname = self.env.cr.dbname
            mock_req.env.user = self.env.user
            with patch(_HINT_MOD + "._AUTO_HINT_POOL") as mock_pool:
                mock_pool.submit.return_value = MagicMock()
                result = ctrl.auto_hint_eval(
                    turn_id=turn.id, sandbox_id=self.claude_sandbox.id,
                )
        self.assertEqual(result["status"], "pending")

    def test_auto_hint_missing_params(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.auto_hint_eval(turn_id=0, sandbox_id=0)
        self.assertIn("error", result)

    def test_auto_hint_turn_not_completed(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        turn = self._create_turn(
            sandbox=self.claude_sandbox, turn_status="InProgress",
        )
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.auto_hint_eval(
                turn_id=turn.id, sandbox_id=self.claude_sandbox.id,
            )
        self.assertIn("error", result)
        self.assertIn("not completed", result["error"])

    def test_auto_hint_turn_no_response(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        turn = self._create_turn(
            sandbox=self.claude_sandbox, turn_status="Completed", response="",
        )
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.auto_hint_eval(
                turn_id=turn.id, sandbox_id=self.claude_sandbox.id,
            )
        self.assertIn("error", result)

    def test_auto_hint_sandbox_not_found(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        turn = self._make_completed_turn(self.claude_sandbox)
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.auto_hint_eval(turn_id=turn.id, sandbox_id=99999999)
        self.assertIn("error", result)

    def test_auto_hint_max_retries(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        self.claude_sandbox.write({"auto_hint_iteration": 5})
        turn = self._make_completed_turn(self.claude_sandbox)
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.auto_hint_eval(
                turn_id=turn.id, sandbox_id=self.claude_sandbox.id,
            )
        self.assertEqual(result.get("status"), "max_retries")

    def test_auto_hint_increments_iteration(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        self.claude_sandbox.write({"auto_hint_iteration": 0})
        turn = self._make_completed_turn(self.claude_sandbox)
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.env.cr = self.env.cr
            mock_req.env.cr.dbname = self.env.cr.dbname
            mock_req.env.user = self.env.user
            with patch(_HINT_MOD + "._AUTO_HINT_POOL") as mock_pool:
                mock_pool.submit.return_value = MagicMock()
                ctrl.auto_hint_eval(
                    turn_id=turn.id, sandbox_id=self.claude_sandbox.id,
                )
        self.claude_sandbox.invalidate_recordset()
        self.assertEqual(self.claude_sandbox.auto_hint_iteration, 1)

    def test_auto_hint_generates_group_id(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        self.claude_sandbox.write({"auto_hint_iteration": 0, "auto_hint_group_id": False})
        turn = self._make_completed_turn(self.claude_sandbox)
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.env.cr = self.env.cr
            mock_req.env.cr.dbname = self.env.cr.dbname
            mock_req.env.user = self.env.user
            with patch(_HINT_MOD + "._AUTO_HINT_POOL") as mock_pool:
                mock_pool.submit.return_value = MagicMock()
                ctrl.auto_hint_eval(
                    turn_id=turn.id, sandbox_id=self.claude_sandbox.id,
                )
        self.claude_sandbox.invalidate_recordset()
        self.assertTrue(self.claude_sandbox.auto_hint_group_id)
        self.assertEqual(len(self.claude_sandbox.auto_hint_group_id), 32)

    def test_auto_hint_preserves_group_id(self):
        from odoo.addons.kensei2.controllers.auto_hint import AutoHintController

        existing_group = uuid.uuid4().hex
        self.claude_sandbox.write({
            "auto_hint_iteration": 2,
            "auto_hint_group_id": existing_group,
        })
        turn = self._make_completed_turn(self.claude_sandbox)
        ctrl = AutoHintController()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            mock_req.env.cr = self.env.cr
            mock_req.env.cr.dbname = self.env.cr.dbname
            mock_req.env.user = self.env.user
            with patch(_HINT_MOD + "._AUTO_HINT_POOL") as mock_pool:
                mock_pool.submit.return_value = MagicMock()
                ctrl.auto_hint_eval(
                    turn_id=turn.id, sandbox_id=self.claude_sandbox.id,
                )
        self.claude_sandbox.invalidate_recordset()
        self.assertEqual(self.claude_sandbox.auto_hint_group_id, existing_group)


@tagged("post_install", "-at_install")
class TestAutoHintHelpers(Kensei2TestCase):

    def test_format_conversation_user_messages(self):
        from odoo.addons.kensei2.controllers.auto_hint import _format_conversation

        msgs = [{"message": {"role": "user", "content": [{"type": "text", "text": "Hi"}]}}]
        out = _format_conversation(msgs)
        self.assertIn("[User]", out)
        self.assertIn("Hi", out)

    def test_format_conversation_assistant_messages(self):
        from odoo.addons.kensei2.controllers.auto_hint import _format_conversation

        msgs = [{"message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}}]
        out = _format_conversation(msgs)
        self.assertIn("[Assistant]", out)

    def test_format_conversation_tool_results(self):
        from odoo.addons.kensei2.controllers.auto_hint import _format_conversation

        msgs = [{"message": {"role": "toolResult", "toolName": "search", "content": [{"type": "text", "text": "Result"}]}}]
        out = _format_conversation(msgs)
        self.assertIn("[Tool Result: search]", out)

    def test_format_conversation_empty(self):
        from odoo.addons.kensei2.controllers.auto_hint import _format_conversation

        out = _format_conversation([])
        self.assertEqual(out, "")

    def test_format_conversation_truncates_long_tool_results(self):
        from odoo.addons.kensei2.controllers.auto_hint import _format_conversation

        long_text = "x" * 1000
        msgs = [{"message": {"role": "toolResult", "content": [{"type": "text", "text": long_text}]}}]
        out = _format_conversation(msgs)
        self.assertIn("...", out)
        self.assertTrue(len(out) < 1000)


@tagged("post_install", "-at_install")
class TestAutoHintAccumulateTokens(Kensei2TestCase):

    def test_accumulate_qwen_tokens(self):
        from odoo.addons.kensei2.controllers.auto_hint import _accumulate_qwen_tokens

        self.task.write({"kimi_eval_input_tokens": 100, "kimi_eval_output_tokens": 50})

        _accumulate_qwen_tokens(
            self.env, self.task.id,
            {"input_tokens": 25, "output_tokens": 10},
        )
        self.task.invalidate_recordset()
        self.assertEqual(self.task.kimi_eval_input_tokens, 125)
        self.assertEqual(self.task.kimi_eval_output_tokens, 60)
