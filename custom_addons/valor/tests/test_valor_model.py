from .common import ValorTestCase


class TestValor(ValorTestCase):

    def test_generate_task_id_uses_domain_prefix(self):
        l0 = self.DomainLevel.create({"name": "Safety"})
        task = self.Valor._generate_task_id(l0.id)
        self.assertIn("eval_saf_", task)

    def test_generate_task_id_uses_unk_when_no_level(self):
        task = self.Valor._generate_task_id(False)
        self.assertTrue(task.startswith("eval_unk_"))

    def test_generate_task_id_short_level_name(self):
        l0 = self.DomainLevel.create({"name": "AI"})
        task = self.Valor._generate_task_id(l0.id)
        self.assertIn("eval_ai", task)

    def test_create_auto_generates_task_id_if_missing(self):
        l0 = self.DomainLevel.create({"name": "Quality"})
        valor = self.Valor.create({"l0": l0.id})
        self.assertTrue(valor.task_id)
        self.assertIn("eval_qua_", valor.task_id)

    def test_write_does_not_allow_clearing_task_id(self):
        valor = self.Valor.create({"task_id": "eval_custom_1234"})
        valor.write({"task_id": False})
        self.assertEqual(valor.task_id, "eval_custom_1234")

    def test_action_add_turn_creates_first_and_second_turn(self):
        valor = self.Valor.create({"task_id": "eval_sequence"})

        valor.action_add_turn()
        self.assertEqual(len(valor.turn_ids), 1)
        self.assertEqual(valor.turn_ids.sequence, 1)

        valor.action_add_turn()
        self.assertEqual(len(valor.turn_ids), 2)
        sequences = sorted(valor.turn_ids.mapped("sequence"))
        self.assertEqual(sequences, [1, 2])

    def test_action_add_turn_uses_max_sequence(self):
        """When existing turns have non-contiguous sequences, next is max+1."""
        valor = self.Valor.create({"task_id": "eval_seq_gap"})
        self.ValorTurn.create(
            {
                "valor_id": valor.id,
                "sequence": 1,
            }
        )
        self.ValorTurn.create(
            {
                "valor_id": valor.id,
                "sequence": 3,
            }
        )
        valor.action_add_turn()
        sequences = sorted(valor.turn_ids.mapped("sequence"))
        self.assertEqual(sequences, [1, 3, 4])

    def test_create_keeps_provided_task_id(self):
        """If task_id is provided on create, it should not be overwritten."""
        valor = self.Valor.create({"task_id": "eval_custom_keep"})
        self.assertEqual(valor.task_id, "eval_custom_keep")

    def test_write_ignores_empty_string_task_id(self):
        """Writing empty string to task_id should be ignored (keeps old value)."""
        valor = self.Valor.create({"task_id": "eval_keep_on_empty"})
        valor.write({"task_id": ""})
        self.assertEqual(valor.task_id, "eval_keep_on_empty")



