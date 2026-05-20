"""Tests for the PRD-generation pipeline (``_run_prd_generation_bg``).

Locks in single-shot behaviour after the 3x score-improvement loop was
removed: one PRD generation and one QC pass per run, regardless of the
now-removed ``vegeta.max_llm_attempts`` config param.
"""
from contextlib import ExitStack
from unittest.mock import patch

from odoo.tests import tagged

from .common import VegetaTestCase


def _score_report(total=80):
    return {
        "total_score": total,
        "grade": "B",
        "section_scores": {},
        "reject_triggers": [],
        "warnings": [],
    }


@tagged("post_install", "-at_install", "vegeta")
class TestPrdGenerationSingleShot(VegetaTestCase):

    def setUp(self):
        super().setUp()
        self._set_param("vegeta.bedrock_inference_arn", "arn:test")
        self.job = self._create_job(
            user_id=self.tasker.id,
            state="generating",
            prd_prompt="extracted website data",
        )

    def _run(self):
        with ExitStack() as stack:
            stack.enter_context(self._patch_registry_cursor())
            gen = stack.enter_context(patch(
                "odoo.addons.vegeta.services.bedrock_service.generate_prd",
                return_value="# Generated PRD\nBody.",
            ))
            stack.enter_context(patch(
                "odoo.addons.vegeta.services.scoring_service.score_prd",
                return_value=_score_report(),
            ))
            stack.enter_context(patch(
                "odoo.addons.vegeta.services.s3_service.upload_prd_to_s3",
                return_value="https://cdn.example.com/prd.md",
            ))
            qc = stack.enter_context(patch(
                "odoo.addons.vegeta.services.qc_service.run_qc",
                return_value={"verdict": "shippable", "report": "QC OK"},
            ))
            self.Job._run_prd_generation_bg(self.env.cr.dbname, self.job.id)
        return gen, qc

    def test_generate_prd_called_exactly_once(self):
        gen, _qc = self._run()
        self.assertEqual(gen.call_count, 1)

    def test_qc_called_exactly_once(self):
        _gen, qc = self._run()
        self.assertEqual(qc.call_count, 1)

    def test_job_completes_with_single_attempt(self):
        self._run()
        self.job.invalidate_recordset()
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.job.llm_attempts, 1)

    def test_llm_trace_records_one_attempt(self):
        self._run()
        self.job.invalidate_recordset()
        attempts = (self.job.llm_trace_json or {}).get("attempts", [])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["attempt"], 1)

    def test_removed_max_attempts_param_does_not_loop(self):
        self._set_param("vegeta.max_llm_attempts", "3")
        gen, qc = self._run()
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(qc.call_count, 1)
