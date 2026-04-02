# -*- coding: utf-8 -*-
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPipelineExecutor(TransactionCase):
    def test_get_tools_path_default(self):
        """Default tools path is module's own tools/ directory."""
        from ..models import pipeline_executor

        path = pipeline_executor.get_tools_path(self.env)
        self.assertTrue(path.endswith("/tools"))
        self.assertIn("commit0_pipeline", path)

    def test_get_tools_path_from_config(self):
        """Tools path can be overridden via config parameter."""
        self.env["ir.config_parameter"].sudo().set_param(
            "commit0_pipeline.tools_path", "/custom/path"
        )
        from ..models import pipeline_executor

        path = pipeline_executor.get_tools_path(self.env)
        self.assertEqual(path, "/custom/path")

    def test_submit_returns_true(self):
        """submit_pipeline_async returns True when semaphore available."""
        from ..models import pipeline_executor

        with patch.object(pipeline_executor, "_executor") as mock_exec:
            result = pipeline_executor.submit_pipeline_async("testdb", 1, 1)
            self.assertTrue(result)
            mock_exec.submit.assert_called_once()

    def test_submit_returns_false_when_full(self):
        """submit_pipeline_async returns False when semaphore is full."""
        from ..models import pipeline_executor

        # Exhaust the semaphore
        original_sem = pipeline_executor._semaphore
        pipeline_executor._semaphore = MagicMock()
        pipeline_executor._semaphore.acquire.return_value = False
        try:
            result = pipeline_executor.submit_pipeline_async("testdb", 1, 1)
            self.assertFalse(result)
        finally:
            pipeline_executor._semaphore = original_sem
