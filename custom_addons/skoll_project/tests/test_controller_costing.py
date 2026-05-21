# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests import tagged

from .common import SkollTestCase


@tagged("post_install", "-at_install")
class TestCostingData(SkollTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.task.write({
            "bedrock_input_tokens": 100,
            "bedrock_output_tokens": 50,
            "claude_input_tokens": 200,
            "claude_output_tokens": 100,
            "glm_input_tokens": 300,
            "glm_output_tokens": 150,
            "traj_qc_input_tokens": 40,
            "traj_qc_output_tokens": 20,
            "taskdesc_input_tokens": 10,
            "taskdesc_output_tokens": 5,
            "golden_input_tokens": 500,
            "golden_output_tokens": 250,
        })

    def _call_costing(self, period="week"):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()
        with patch.object(type(controller), "__init__", lambda s: None):
            controller = SkollCostingController.__new__(SkollCostingController)
        return controller.costing_data(period=period)

    def test_costing_week_returns_rows_and_totals(self):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()

        with patch("odoo.addons.skoll.controllers.costing.request") as mock_request:
            mock_request.env = self.env
            result = controller.costing_data(period="week")

        self.assertIn("rows", result)
        self.assertIn("totals", result)
        self.assertIn("period", result)
        self.assertEqual(result["period"], "week")

    def test_costing_month_period(self):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()

        with patch("odoo.addons.skoll.controllers.costing.request") as mock_request:
            mock_request.env = self.env
            result = controller.costing_data(period="month")

        self.assertEqual(result["period"], "month")
        self.assertIsNotNone(result["start_date"])

    def test_costing_all_time(self):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()

        with patch("odoo.addons.skoll.controllers.costing.request") as mock_request:
            mock_request.env = self.env
            result = controller.costing_data(period="all")

        self.assertIsNone(result["start_date"])

    def test_costing_row_has_all_token_fields(self):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()

        with patch("odoo.addons.skoll.controllers.costing.request") as mock_request:
            mock_request.env = self.env
            result = controller.costing_data(period="all")

        if result["rows"]:
            row = result["rows"][0]
            for key in ("bedrock_input", "bedrock_output", "bedrock_total",
                        "claude_input", "claude_output", "claude_total",
                        "glm_input", "glm_output", "glm_total",
                        "grand_total"):
                self.assertIn(key, row, f"Missing key: {key}")

    def test_costing_totals_aggregate_correctly(self):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()

        with patch("odoo.addons.skoll.controllers.costing.request") as mock_request:
            mock_request.env = self.env
            result = controller.costing_data(period="all")

        totals = result["totals"]
        self.assertGreaterEqual(totals["bedrock_input"], 100)
        self.assertGreaterEqual(totals["claude_input"], 200)
        self.assertGreater(totals["grand_total"], 0)

    def test_costing_empty_no_tasks(self):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()

        with patch("odoo.addons.skoll.controllers.costing.request") as mock_request:
            mock_request.env = self.env
            mock_request.env["skoll.skoll"].sudo = lambda: self.env["skoll.skoll"].browse([])
            result = controller.costing_data(period="all")

        self.assertIsInstance(result["rows"], list)
        self.assertIsInstance(result["totals"], dict)

    def test_costing_week_start_date_is_monday(self):
        from ..controllers.costing import SkollCostingController
        controller = SkollCostingController()

        with patch("odoo.addons.skoll.controllers.costing.request") as mock_request:
            mock_request.env = self.env
            result = controller.costing_data(period="week")

        if result["start_date"]:
            from datetime import date
            start = date.fromisoformat(result["start_date"])
            self.assertEqual(start.weekday(), 0)
