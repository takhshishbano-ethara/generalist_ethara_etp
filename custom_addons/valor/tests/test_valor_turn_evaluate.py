from unittest.mock import patch

from .common import ValorTestCase


class TestValorTurnAutoEvaluation(ValorTestCase):

    def test_run_auto_evaluation_skips_when_responses_missing(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": False,
                "client_response_b": False,
            }
        )
        turn._run_auto_evaluation()
        self.assertFalse(turn.valor_id.is_eval_done)

    def test_run_auto_evaluation_writes_scores_and_marks_eval_done(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "Resp A",
                "client_response_b": "Resp B",
            }
        )

        fake_eval_result = [{"dummy": "value"}]
        fake_data = {
            "truthfulness_a": "5",
            "truthfulness_b": "4",
            "ab_preference": "1",
            "ab_comment": "Looks good",
        }

        with patch(
            "odoo.addons.valor.models.valor_turn.run_evaluation_kimi", return_value=fake_eval_result
        ), patch(
            "odoo.addons.valor.models.models.get_eval_data", return_value=fake_data
        ):
            turn._run_auto_evaluation()

        self.assertEqual(turn.truthfulness_a, "5")
        self.assertEqual(turn.store_truthfulness_a, "5")
        self.assertEqual(turn.store_truthfulness_b, "4")
        self.assertEqual(turn.ab_preference, "1")
        self.assertEqual(turn.store_ab_preference, "1")
        self.assertEqual(turn.ab_comment, "Looks good")
        self.assertTrue(turn.valor_id.is_eval_done)

    def test_run_auto_evaluation_handles_empty_eval_result(self):
        """If Kimi returns empty result, nothing should be written."""
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "A",
                "client_response_b": "B",
            }
        )
        with patch("odoo.addons.valor.models.valor_turn.run_evaluation_kimi", return_value=[]):
            turn._run_auto_evaluation()
        self.assertFalse(turn.valor_id.is_eval_done)

    def test_run_auto_evaluation_handles_get_eval_data_none(self):
        """If get_eval_data returns None, skip writing."""
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "A",
                "client_response_b": "B",
            }
        )
        with patch(
            "odoo.addons.valor.models.valor_turn.run_evaluation_kimi", return_value=[{"dummy": "x"}]
        ), patch("odoo.addons.valor.models.models.get_eval_data", return_value=None):
            turn._run_auto_evaluation()
        self.assertFalse(turn.valor_id.is_eval_done)

    def test_run_auto_evaluation_partial_data_only_updates_available_fields(self):
        """When eval data has only some dims, only those should be written."""
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "client_response_a": "A",
                "client_response_b": "B",
            }
        )
        fake_eval_result = [{"dummy": "value"}]
        fake_data = {
            "truthfulness_a": "5",
            # truthfulness_b missing on purpose
        }
        with patch(
            "odoo.addons.valor.models.valor_turn.run_evaluation_kimi", return_value=fake_eval_result
        ), patch("odoo.addons.valor.models.models.get_eval_data", return_value=fake_data):
            turn._run_auto_evaluation()
        self.assertEqual(turn.truthfulness_a, "5")
        self.assertFalse(bool(turn.truthfulness_b))


class TestValorTurnEvaluate(ValorTestCase):

    def test_action_evaluate_requires_all_dimensions(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
            }
        )
        with self.assertRaisesRegex(Exception, "Please fill all the dimensions"):
            turn.action_evaluate()

    def test_action_evaluate_requires_prior_prompts(self):
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
                "truthfulness_a": "1",
                "truthfulness_b": "1",
                "instruction_following_a": "1",
                "instruction_following_b": "1",
                "writing_quality_a": "1",
                "writing_quality_b": "1",
                "verbosity_a": "1",
                "verbosity_b": "1",
                "prompt_correctness_a": "1",
                "prompt_correctness_b": "1",
                "overall_quality_a": "1",
                "overall_quality_b": "1",
                "ab_preference": "1",
                "ab_comment": "ok",
            }
        )
        with self.assertRaisesRegex(Exception, "Prompt is missing for Turn 1"):
            t2.action_evaluate()

    def test_action_evaluate_runs_eval_and_qc(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "Prompt 1",
                "truthfulness_a": "1",
                "truthfulness_b": "1",
                "instruction_following_a": "1",
                "instruction_following_b": "1",
                "writing_quality_a": "1",
                "writing_quality_b": "1",
                "verbosity_a": "1",
                "verbosity_b": "1",
                "prompt_correctness_a": "1",
                "prompt_correctness_b": "1",
                "overall_quality_a": "1",
                "overall_quality_b": "1",
                "ab_preference": "1",
                "ab_comment": "ok",
            }
        )

        with patch("odoo.addons.valor.models.valor_turn.ValorTurn._run_eval_and_qc") as mocked_run:
            turn.action_evaluate()
            mocked_run.assert_called_once()

    def test_action_evaluate_allows_multi_turn_when_valid(self):
        """Ensure action_evaluate passes when prior turns have prompts and all dims are set."""
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
                "truthfulness_a": "1",
                "truthfulness_b": "1",
                "instruction_following_a": "1",
                "instruction_following_b": "1",
                "writing_quality_a": "1",
                "writing_quality_b": "1",
                "verbosity_a": "1",
                "verbosity_b": "1",
                "prompt_correctness_a": "1",
                "prompt_correctness_b": "1",
                "overall_quality_a": "1",
                "overall_quality_b": "1",
                "ab_preference": "1",
                "ab_comment": "ok",
            }
        )
        # Should not raise
        with patch("odoo.addons.valor.models.valor_turn.ValorTurn._run_eval_and_qc") as mocked_run:
            turn2.action_evaluate()
            mocked_run.assert_called_once()

