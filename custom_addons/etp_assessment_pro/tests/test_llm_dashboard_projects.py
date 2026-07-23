# -*- coding: utf-8 -*-
"""Per-project cost breakdown on the LLM Budget dashboard.

Locks the attribution contract of the new "Cost by Project" section:

* Authoring spend (rows carrying ``prompt_id``) is summed per generator.
* Evaluation spend (``score_subjective`` rows carrying ``evaluator_id``) is
  folded into the generator resolved via evaluator -> assessment -> generator.
* Everything not resolvable to a generator lands in an "Unattributed" bucket,
  and the per-project + unattributed buckets always reconcile back to the
  ledger grand totals.
* The dashboard model computes the section HTML without error.
"""
import base64
import contextlib
import io
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import vertex, scoring
from odoo.addons.etp_assessment_pro.tests import vertex_fixtures as vf


def _png_bytes(w=120, h=90, color=(255, 255, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    status_code = 200
    content = b"{}"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def post(self, *args, **kwargs):
        return _FakeResp(self._payload)


@contextlib.contextmanager
def _fake_vertex(payload):
    """Run the REAL vertex request/log path against a canned Gemini response so
    _log_usage actually writes a ledger row (patching _call_vertex would skip
    logging). Stubs credential resolution so no live project is needed."""
    with patch.object(vertex, "_gemini_request",
                      return_value=("http://vertex.test", {})), \
            patch.object(vertex, "_httpx",
                         return_value=_FakeClient(payload)):
        yield


def _text_reply(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}


def _image_reply(b64):
    return {"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": b64}}]}}],
            "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 0}}


class TestLlmDashboardProjects(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.Usage = self.env["etp.assessment.pro.llm.usage"]
        self.Dashboard = self.env["etp.assessment.pro.llm.dashboard"]
        # These tests assert on the WHOLE ledger (empty-placeholder,
        # per-project reconciliation to grand totals), so they must own the
        # ledger they read. A dev DB that has had real generation run against
        # it carries committed usage rows (e.g. a "Test local" generator) that
        # would leak into the aggregation and break the fixed expectations.
        # Clearing here gives each test a deterministic clean slate; the
        # TransactionCase rolls it back, so committed dev-DB rows are untouched.
        self.Usage.search([]).unlink()

    def _usage(self, operation, cost, tokens_in=0, prompt=None, evaluator=None):
        return self.Usage.create({
            "operation": operation,
            "model": "gemini-3.1-pro-preview",
            "cost_usd": cost,
            "tokens_in": tokens_in,
            "prompt_id": prompt.id if prompt else False,
            "evaluator_id": evaluator.id if evaluator else False,
        })

    def _evaluator_for(self, generator):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        assessment = self.Assessment.create({
            "name": "A", "generator_id": generator.id if generator else False})
        return self.Evaluator.create({
            "assessment_id": assessment.id, "applicant_id": applicant.id})

    def test_per_project_attribution_and_reconciliation(self):
        p1 = self.Prompt.create({"name": "Project One"})
        p2 = self.Prompt.create({"name": "Project Two"})

        self._usage("generate_questions", 1.0, tokens_in=100, prompt=p1)
        self._usage("generate_image", 0.5, tokens_in=50, prompt=p1)
        self._usage("extract_tags", 0.25, tokens_in=20, prompt=p2)

        ev1 = self._evaluator_for(p1)
        self._usage("score_subjective", 2.0, tokens_in=200, evaluator=ev1)

        ev_orphan = self._evaluator_for(None)
        self._usage("score_subjective", 0.9, tokens_in=30, evaluator=ev_orphan)

        self._usage("other", 0.75, tokens_in=10)

        rows, unattributed = self.Dashboard._project_cost_rows(self.Usage)

        by_gen = {d["gen"].id: d for d in rows}
        self.assertAlmostEqual(by_gen[p1.id]["auth"], 1.5)
        self.assertAlmostEqual(by_gen[p1.id]["eval"], 2.0)
        self.assertEqual(by_gen[p1.id]["tokens"], 350)
        self.assertEqual(by_gen[p1.id]["requests"], 3)
        self.assertAlmostEqual(by_gen[p2.id]["auth"], 0.25)
        self.assertAlmostEqual(by_gen[p2.id]["eval"], 0.0)

        self.assertAlmostEqual(unattributed["cost"], 0.9 + 0.75)
        self.assertEqual(unattributed["tokens"], 30 + 10)
        self.assertEqual(unattributed["requests"], 2)

        total_cost = sum(d["auth"] + d["eval"] for d in rows) + unattributed["cost"]
        self.assertAlmostEqual(total_cost, 5.4)
        total_req = sum(d["requests"] for d in rows) + unattributed["requests"]
        self.assertEqual(total_req, self.Usage.search_count([]))

        self.assertEqual(rows[0]["gen"].id, p1.id)

    def test_dashboard_html_computes_and_names_project(self):
        p = self.Prompt.create({"name": "Named Project"})
        self._usage("generate_questions", 1.0, prompt=p)

        dash = self.Dashboard.create({})
        html = dash.chart_cost_by_project_html or ""
        self.assertIn("etp-proj-wrap", html)
        self.assertIn("Named Project", html)

    def test_empty_ledger_renders_placeholder(self):
        dash = self.Dashboard.create({})
        self.assertIn("etp-chart-empty", dash.chart_cost_by_project_html or "")


class TestUsageAttributionStamping(TransactionCase):
    """Every image/scoring Vertex call now stamps its owning project onto the
    logged usage row, so the dashboard attributes it instead of dropping it into
    Unattributed. These drive the REAL vertex path (fake httpx, no live calls)
    and assert the written ledger row carries the right link."""

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Question = self.env["etp.assessment.pro.question"]
        self.QImage = self.env["etp.assessment.pro.question.image"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Usage = self.env["etp.assessment.pro.llm.usage"]

    def test_image_render_row_carries_prompt_id(self):
        gen = self.Prompt.create({"name": "Render Gen"})
        draft = self.Draft.create({
            "prompt_id": gen.id,
            "name": "Img Q",
            "question_prompt": "Show a blue mug",
            "question_type": "image_prompt",
            "image_state": "pending",
            "image_brief_json": json.dumps(
                [{"slot": "single", "label": "Image", "prompt": "a blue mug"}]),
        })
        reply = _image_reply(base64.b64encode(_png_bytes()).decode())
        with _fake_vertex(reply):
            self.assertTrue(draft._render_all_images())
        row = self.Usage.search(
            [("operation", "=", "generate_image")], limit=1, order="id desc")
        self.assertTrue(row)
        self.assertEqual(row.prompt_id, gen)

    def test_detect_row_carries_prompt_id(self):
        gen = self.Prompt.create({"name": "Detect Gen"})
        q = self.Question.create({
            "name": "Label", "prompt": "Label the image.",
            "question_type": "image_label", "generator_id": gen.id})
        img = self.QImage.create({
            "question_id": q.id, "label": "Image", "slot": "single",
            "image": base64.b64encode(_png_bytes())})
        boxes = [{"box_2d": [100, 100, 400, 400], "label": "car",
                  "description": "a car"}]
        with _fake_vertex(_text_reply(json.dumps(boxes))):
            self.assertTrue(img._detect_and_annotate())
        row = self.Usage.search(
            [("operation", "=", "detect_image_elements")], limit=1,
            order="id desc")
        self.assertTrue(row)
        self.assertEqual(row.prompt_id, gen)

    def test_detect_row_unattributed_when_no_generator(self):
        q = self.Question.create({
            "name": "Manual Label", "prompt": "Label it.",
            "question_type": "image_label"})
        img = self.QImage.create({
            "question_id": q.id, "label": "Image", "slot": "single",
            "image": base64.b64encode(_png_bytes())})
        boxes = [{"box_2d": [10, 10, 40, 40], "label": "dog",
                  "description": "a dog"}]
        with _fake_vertex(_text_reply(json.dumps(boxes))):
            self.assertTrue(img._detect_and_annotate())
        row = self.Usage.search(
            [("operation", "=", "detect_image_elements")], limit=1,
            order="id desc")
        self.assertTrue(row)
        self.assertFalse(row.prompt_id)

    def test_scoring_row_carries_evaluator_id(self):
        gen = self.Prompt.create({"name": "Score Gen"})
        assessment = self.Assessment.create({
            "name": "A", "generator_id": gen.id})
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id, "applicant_id": applicant.id})
        q = self.Question.create({
            "name": "Justify", "prompt": "Justify your verdict.",
            "question_type": "subjective_rubric"})
        resp = self.Response.create({
            "assessment_id": assessment.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id,
            "question_id": q.id,
            "justification": "A is sharper, evidence: crisp edges.",
        })
        with _fake_vertex(_text_reply(vf.score_payload([resp]))):
            scoring._score_submission(self.env, resp)
        row = self.Usage.search(
            [("operation", "=", "score_subjective")], limit=1, order="id desc")
        self.assertTrue(row)
        self.assertEqual(row.evaluator_id, ev)
        self.assertFalse(row.prompt_id)
