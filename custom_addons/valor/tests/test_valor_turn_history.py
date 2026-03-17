from .common import ValorTestCase


class TestValorTurnHistory(ValorTestCase):

    def test_get_preferred_response_uses_ab_preference_for_a(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_response_a": "Response A",
                "client_response_b": "Response B",
                "ab_preference": "-1",
            }
        )
        preferred = turn._get_preferred_response()
        self.assertEqual(preferred, "Response A")

    def test_get_preferred_response_uses_ab_preference_for_b(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_response_a": "Response A",
                "client_response_b": "Response B",
                "ab_preference": "2",
            }
        )
        preferred = turn._get_preferred_response()
        self.assertEqual(preferred, "Response B")

    def test_get_preferred_response_returns_empty_when_no_preference(self):
        """If ab_preference is not set, preferred response should be empty string."""
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_response_a": "Response A",
                "client_response_b": "Response B",
                "ab_preference": False,
            }
        )
        self.assertEqual(turn._get_preferred_response(), "")

    def test_build_dialog_history_uses_prior_turns_and_preferences(self):
        t1 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "Resp1A",
                "client_response_b": "Resp1B",
                "ab_preference": "-1",
            }
        )
        t2 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "Prompt 2",
                "client_response_a": "Resp2A",
                "client_response_b": "Resp2B",
                "ab_preference": "1",
            }
        )
        t3 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 3,
                "client_prompt": "Prompt 3",
                "client_response_a": "Resp3A",
                "client_response_b": "Resp3B",
                "ab_preference": "1",
            }
        )

        history = t3._build_dialog_history()
        self.assertEqual(
            history,
            [
                ("Prompt 1", "Resp1A"),
                ("Prompt 2", "Resp2B"),
            ],
        )

    def test_build_dialog_history_no_prior_turns(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
            }
        )
        self.assertEqual(turn._build_dialog_history(), [])

    def test_build_dialog_history_uses_preferred_response_when_no_ab_preference(self):
        """If ab_preference is empty, preferred response should be empty string."""
        t1 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "Resp1A",
                "client_response_b": "Resp1B",
                "ab_preference": False,
            }
        )
        t2 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "Prompt 2",
            }
        )
        history = t2._build_dialog_history()
        self.assertEqual(history, [("Prompt 1", "")])

    def test_build_evaluation_inputs_includes_turns_up_to_current_sequence(self):
        self.valor.task_id = "task_eval_1"
        self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "Resp1A",
                "client_response_b": "Resp1B",
            }
        )
        t2 = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "Prompt 2",
                "client_response_a": "Resp2A",
                "client_response_b": "Resp2B",
            }
        )

        inputs = t2._build_evaluation_inputs()
        self.assertEqual(
            inputs,
            [
                {
                    "task_id": "task_eval_1",
                    "prompt": "Prompt 1",
                    "response_a": "Resp1A",
                    "response_b": "Resp1B",
                },
                {
                    "task_id": "task_eval_1",
                    "prompt": "Prompt 2",
                    "response_a": "Resp2A",
                    "response_b": "Resp2B",
                },
            ],
        )

    def test_build_evaluation_inputs_ignores_future_turns(self):
        """_build_evaluation_inputs should not include turns with higher sequence."""
        self.valor.task_id = "task_eval_2"
        self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "P1",
                "client_response_a": "A1",
                "client_response_b": "B1",
            }
        )
        current = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 2,
                "client_prompt": "P2",
                "client_response_a": "A2",
                "client_response_b": "B2",
            }
        )
        # Future turn that should not be included
        self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 3,
                "client_prompt": "P3",
                "client_response_a": "A3",
                "client_response_b": "B3",
            }
        )

        inputs = current._build_evaluation_inputs()
        self.assertEqual(len(inputs), 2)
        self.assertEqual(inputs[-1]["prompt"], "P2")

    def test_get_preferred_response_returns_empty_when_no_responses(self):
        """_get_preferred_response should return empty string when both responses are missing."""
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_response_a": False,
                "client_response_b": False,
                "ab_preference": "1",
            }
        )
        self.assertEqual(turn._get_preferred_response(), "")


