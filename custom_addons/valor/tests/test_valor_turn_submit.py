from unittest.mock import patch

from .common import ValorTestCase


class TestValorTurnSubmit(ValorTestCase):

    def test_action_next_turn_requires_current_prompt(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": False,
            }
        )
        with self.assertRaisesRegex(Exception, "Current prompt is required before adding the next turn"):
            turn.action_next_turn()

    def test_action_next_turn_creates_next_turn_with_suggestion(self):
        from odoo.addons.valor.models import valor_turn as valor_turn_mod

        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "Resp1A",
                "client_response_b": "Resp1B",
                "ab_preference": "1",
            }
        )

        with patch.object(
            valor_turn_mod, "generate_follow_up_prompt_kimi", return_value={"follow_up_prompt": "Suggested next prompt"}
        ):
            turn.action_next_turn()

        next_turn = self.ValorTurn.search(
            [("valor_id", "=", self.valor.id), ("sequence", "=", 2)], limit=1
        )
        self.assertTrue(next_turn)
        self.assertEqual(next_turn.store_client_prompt, "Suggested next prompt")

    def test_action_next_turn_updates_existing_next_turn(self):
        from odoo.addons.valor.models import valor_turn as valor_turn_mod

        turn1 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "Resp1A",
                "client_response_b": "Resp1B",
                "ab_preference": "1",
            }
        )
        next_turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
            }
        )

        with patch.object(
            valor_turn_mod, "generate_follow_up_prompt_kimi", return_value={"follow_up_prompt": "Updated prompt"}
        ):
            turn1.action_next_turn()

        next_turn = self.ValorTurn.browse(next_turn.id)
        self.assertEqual(next_turn.store_client_prompt, "Updated prompt")

    def test_action_submit_prompt_turn1_requires_prompt_or_image(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": False,
                "image": False,
            }
        )
        with self.assertRaisesRegex(Exception, "Prompt or image required"):
            turn.action_submit_prompt()

    def test_action_submit_prompt_later_turn_requires_all_prior_prompts(self):
        self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": False,
            }
        )
        t2 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "Prompt 2",
            }
        )

        with self.assertRaisesRegex(Exception, "All 2 Prompts Required"):
            t2.action_submit_prompt()

    def test_action_submit_prompt_later_turn_with_all_prompts_and_dialog_id_succeeds(self):
        """Later turn should pass validation when all prompts filled and dialog_id present."""
        self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
            }
        )
        turn2 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "Prompt 2",
            }
        )
        self.valor.dialog_id = "dlg123"
        with patch("odoo.addons.valor.models.valor_turn.ValorTurn._generate_responses") as gen_mock, patch(
            "odoo.addons.valor.models.valor_turn.check_follow_up_relevance_kimi",
            return_value={"is_relevant": True},
        ):
            turn2.action_submit_prompt()
        gen_mock.assert_called_once()

    def test_action_submit_prompt_later_turn_requires_dialog_session(self):
        # Prior turn with prompt so prior-prompts check passes.
        self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
            }
        )
        turn2 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "Prompt 2",
            }
        )

        # Force router config no-op so dialog_id stays empty and we hit the dialog_id validation.
        with patch("odoo.addons.valor.models.valor_turn.ValorTurn._ensure_router_config", return_value=None):
            with self.assertRaisesRegex(
                Exception, "Dialog session is missing. Please submit Turn 1 first to start a session."
            ):
                turn2.action_submit_prompt()

    def test_action_submit_prompt_turn1_with_prompt_only_succeeds(self):
        """Turn 1 with only prompt should pass validation and reach _generate_responses."""
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt only",
                "image": False,
            }
        )
        with patch("odoo.addons.valor.models.valor_turn.ValorTurn._generate_responses") as gen_mock:
            # Also avoid external router config/http
            with patch("odoo.addons.valor.models.valor_turn.ValorTurn._ensure_router_config", return_value=None):
                turn.action_submit_prompt()
        gen_mock.assert_called_once()

    def test_action_submit_prompt_turn1_strips_whitespace_in_prompt(self):
        """Leading/trailing whitespace in prompt should not break submission."""
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "  Prompt only  ",
            }
        )
        with patch("odoo.addons.valor.models.valor_turn.ValorTurn._generate_responses") as gen_mock:
            with patch("odoo.addons.valor.models.valor_turn.ValorTurn._ensure_router_config", return_value=None):
                turn.action_submit_prompt()
        gen_mock.assert_called_once()

    def test_action_submit_prompt_turn1_with_image_only_succeeds(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": False,
                "image": b"fake",
            }
        )
        with patch("odoo.addons.valor.models.valor_turn.ValorTurn._generate_responses") as gen_mock:
            with patch(
                "odoo.addons.valor.models.models.Valor._ensure_image_handle_for_turn_record",
                return_value=None,
            ), patch(
                "odoo.addons.valor.models.valor_turn.ValorTurn._ensure_router_config",
                return_value=None,
            ):
                turn.action_submit_prompt()
        gen_mock.assert_called_once()

    def test_action_submit_prompt_later_turn_rewrites_irrelevant_followup(self):
        """Later turns should raise if follow-up is not relevant."""
        from odoo.addons.valor.models import valor_turn as valor_turn_mod

        # Prior prompt present
        self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
            }
        )
        turn2 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "Off-topic",
            }
        )
        # Make sure dialog_id exists so we hit relevance check
        self.valor.dialog_id = "dlg123"

        with patch.object(
            valor_turn_mod, "check_follow_up_relevance_kimi", return_value={"is_relevant": False}
        ):
            with self.assertRaisesRegex(Exception, "Please rewrite the prompt"):
                turn2.action_submit_prompt()

    def test_action_next_turn_creates_empty_suggestion_when_api_returns_empty(self):
        """If generate_follow_up_prompt_kimi returns no text, suggestion should be empty string."""
        from odoo.addons.valor.models import valor_turn as valor_turn_mod

        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "A1",
                "client_response_b": "B1",
                "ab_preference": "1",
            }
        )

        with patch.object(valor_turn_mod, "generate_follow_up_prompt_kimi", return_value={}):
            turn.action_next_turn()

        next_turn = self.ValorTurn.search(
            [("valor_id", "=", self.valor.id), ("sequence", "=", 2)], limit=1
        )
        self.assertTrue(next_turn)
        self.assertEqual(next_turn.store_client_prompt, "")


