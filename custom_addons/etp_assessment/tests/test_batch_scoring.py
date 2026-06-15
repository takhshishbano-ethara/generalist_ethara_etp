# -*- coding: utf-8 -*-
"""Unit tests for batched per-evaluator subjective scoring.

Coverage:
  - score_evaluator returns {} when no responses need scoring
  - score_evaluator bundles N responses into ONE _call_vertex call
  - score_evaluator fills synthetic feedback for IDs omitted by the LLM
  - score_evaluator tolerates the legacy {"passed": bool} reply shape
  - evaluator.action_llm_score writes scores from the batched result
  - evaluator.action_llm_score falls back to per-response enqueue on
    batch failure

Run from the project root:
    src/odoo-bin -c src/odoo.conf -d ethara_dev \\
        --test-tags=etp_assessment_batch_scoring \\
        --stop-after-init -i etp_assessment
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("etp_assessment_batch_scoring", "post_install", "-at_install")
class TestBatchScoring(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Category = cls.env["etp.assessment.category"]
        Dimension = cls.env["etp.assessment.dimension"]
        Option = cls.env["etp.assessment.dimension.option"]
        Question = cls.env["etp.assessment.question"]
        Employee = cls.env["hr.employee"]
        Assessment = cls.env["etp.assessment"]

        cls.category = Category.create({"name": "Test Category"})
        cls.dimension = Dimension.create({"name": "Did it work?"})
        Option.create([
            {"name": "Yes", "dimension_id": cls.dimension.id, "sequence": 10},
            {"name": "No", "dimension_id": cls.dimension.id, "sequence": 20},
        ])
        cls.questions = Question.create([{
            "name": f"Question {i}",
            "question_type": "text",
            "prompt": f"Prompt for question {i}",
            "category_id": cls.category.id,
        } for i in range(3)])

        cls.employee = Employee.create({
            "name": "Test Candidate",
            "work_email": "test@example.com",
        })
        cls.assessment = Assessment.create({
            "name": "Batch Scoring Test",
            "category_id": cls.category.id,
            "question_limit": 3,
            "evaluator_ids": [(4, cls.employee.id)],
        })

    def _make_evaluator_with_responses(self, n_responses=3,
                                       justification="Real candidate answer"):
        Evaluator = self.env["etp.assessment.evaluator"]
        Response = self.env["etp.assessment.response"]
        ev = Evaluator.create({
            "assessment_id": self.assessment.id,
            "employee_id": self.employee.id,
            "access_token": "test-token-batch",
            "state": "submitted",
        })
        for q in self.questions[:n_responses]:
            Response.create({
                "assessment_id": self.assessment.id,
                "assessment_evaluator_id": ev.id,
                "evaluator_id": self.employee.id,
                "question_id": q.id,
                "justification": justification,
                "state": "submitted",
                "llm_state": "pending",
            })
        return ev

    def test_empty_evaluator_returns_empty_dict(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        ev = self.env["etp.assessment.evaluator"].create({
            "assessment_id": self.assessment.id,
            "employee_id": self.employee.id,
            "access_token": "test-empty",
            "state": "submitted",
        })
        self.assertEqual(vertex_scoring.score_evaluator(self.env, ev), {})

    def test_batched_one_call_for_n_responses(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        ev = self._make_evaluator_with_responses(n_responses=3)
        response_ids = [r.id for r in ev.response_ids if r.needs_llm]
        fake_reply = json.dumps([
            {"id": rid, "score": 0.9, "feedback": f"Good answer #{rid}"}
            for rid in response_ids
        ])
        with patch.object(
            vertex_scoring, "_call_vertex", return_value=fake_reply
        ) as mock_vertex:
            results = vertex_scoring.score_evaluator(self.env, ev)
            self.assertEqual(
                mock_vertex.call_count, 1,
                "Expected exactly ONE Vertex call for N responses (batched)",
            )
        self.assertEqual(len(results), 3)
        for rid in response_ids:
            self.assertIn(rid, results)
            self.assertAlmostEqual(results[rid]["score01"], 0.9, places=2)

    def test_partial_response_gets_synthetic_feedback(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        ev = self._make_evaluator_with_responses(n_responses=3)
        response_ids = sorted(r.id for r in ev.response_ids if r.needs_llm)
        omitted_id = response_ids[-1]
        fake_reply = json.dumps([
            {"id": rid, "score": 1.0, "feedback": "ok"}
            for rid in response_ids if rid != omitted_id
        ])
        with patch.object(
            vertex_scoring, "_call_vertex", return_value=fake_reply
        ):
            results = vertex_scoring.score_evaluator(self.env, ev)
        self.assertIn(omitted_id, results)
        self.assertEqual(results[omitted_id]["score01"], 0.0)
        self.assertIn("LLM did not return", results[omitted_id]["feedback"])

    def test_legacy_passed_field_mapped_to_binary(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        ev = self._make_evaluator_with_responses(n_responses=2)
        response_ids = sorted(r.id for r in ev.response_ids if r.needs_llm)
        fake_reply = json.dumps([
            {"id": response_ids[0], "passed": True, "feedback": "y"},
            {"id": response_ids[1], "passed": False, "feedback": "n"},
        ])
        with patch.object(
            vertex_scoring, "_call_vertex", return_value=fake_reply
        ):
            results = vertex_scoring.score_evaluator(self.env, ev)
        self.assertEqual(results[response_ids[0]]["score01"], 1.0)
        self.assertEqual(results[response_ids[1]]["score01"], 0.0)

    def test_action_llm_score_writes_through_batched_path(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        ev = self._make_evaluator_with_responses(n_responses=3)
        response_ids = [r.id for r in ev.response_ids if r.needs_llm]
        fake_reply = json.dumps([
            {"id": rid, "score": 0.9, "feedback": f"feedback {rid}"}
            for rid in response_ids
        ])
        with patch.object(
            vertex_scoring, "_call_vertex", return_value=fake_reply
        ) as mock_vertex:
            ev.action_llm_score()
            self.assertEqual(mock_vertex.call_count, 1)
        for resp in ev.response_ids:
            if not resp.needs_llm:
                continue
            self.assertEqual(resp.llm_state, "scored")
            self.assertAlmostEqual(resp.llm_raw_score, 0.9, places=2)
            self.assertTrue(resp.llm_passed)
            self.assertGreaterEqual(resp.llm_attempts, 1)

    def test_action_llm_score_falls_back_on_batch_failure(self):
        from odoo.addons.etp_assessment.services import vertex_scoring
        ev = self._make_evaluator_with_responses(n_responses=2)
        with patch.object(
            vertex_scoring, "_call_vertex",
            side_effect=RuntimeError("simulated Vertex 500"),
        ), patch(
            "odoo.addons.etp_assessment.models.assessment."
            "EtpAssessmentResponse._enqueue_subjective_scoring"
        ) as mock_enqueue:
            ev.action_llm_score()
            mock_enqueue.assert_called_once()
