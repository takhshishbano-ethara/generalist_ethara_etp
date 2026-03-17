from odoo.tests.common import TransactionCase


class ValorTestCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Valor = cls.env["valor"]
        cls.ValorTurn = cls.env["valor.turn"]
        cls.DomainLevel = cls.env["domain.level"]
        cls.valor = cls.Valor.create({"task_id": "test_task"})

