# -*- coding: utf-8 -*-
"""Phase 3 of video_prompt: ASYNC Veo video generation, all offline (every Veo
op submit/fetch is mocked — no real Vertex call). video_prompt is the video twin
of image_prompt; Phase 3 turns the authored clip briefs into generated clips via
Veo long-running operations, staged on the draft and materialized to
question.video on approve. It MUST stay config-gated so the Phase-1 upload path
works when Veo is unavailable.

Covered here:
  * submit_video_op / fetch_video_op hit the right predictLongRunning /
    fetchPredictOperation endpoints with the right body and raise on 429;
  * the poll cron submits one op per brief and flips the draft to 'generating';
  * both ops done -> two question.video rows on approve, video_state='rendered',
    all-or-nothing (one done + one pending stays 'generating');
  * a 429 on submit/fetch re-queues without spending an attempt or failing;
  * an op that fails past the cap fails the draft (never silently rendered);
  * the CONFIG GATE: no creds -> no op submitted, stays 'pending', upload works;
  * the cron is idempotent: running it twice never double-submits or double-fills.
"""
import base64
import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import vertex
from odoo.addons.etp_assessment_pro.models.prompt import _VIDEO_OP_MAX_ATTEMPTS


_IDEAL = "Slow the reference clip to half speed and grade to a warm teal."
_FAKE_MP4_B64 = base64.b64encode(
    b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 48).decode("ascii")


class _VideoPhase3Base(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _draft(self, slots=("reference", "output"), state="pending"):
        prompt = self.Prompt.create({"name": "Vid gen"})
        briefs = [{"slot": s, "label": s.title(),
                   "prompt": "A five second clip: %s" % s} for s in slots]
        return self.Draft.create({
            "prompt_id": prompt.id,
            "name": "Transform the clip",
            "question_prompt": "Write the transformation prompt.",
            "question_type": "video_prompt",
            "video_brief_json": json.dumps(briefs),
            "rubric_json": json.dumps({"ideal_prompt": _IDEAL}),
            "video_state": state,
        })

    def _ops(self, *slots):
        return {s: {"op_name": "op/%s" % s, "state": "submitted",
                    "attempts": 0, "label": s.title()} for s in slots}

    @contextmanager
    def _no_real_commit(self):
        cr = self.env.cr
        with patch.object(cr, "commit", cr.flush), \
                patch.object(cr, "rollback", cr.clear):
            yield


class TestVideoPromptPhase3Submit(_VideoPhase3Base):
    def test_submit_creates_two_ops_and_generating(self):
        draft = self._draft()
        names = iter(["op/ref", "op/out"])
        with self._no_real_commit(), \
                patch.object(vertex, "video_generation_available",
                             return_value=True), \
                patch.object(vertex, "submit_video_op",
                             side_effect=lambda env, brief, **kw: next(names)) \
                as sub, \
                patch.object(vertex, "fetch_video_op",
                             return_value={"done": False, "video_b64": None,
                                           "gcs_uri": None, "error": None}):
            self.Draft._cron_poll_video_ops()
        draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "generating")
        self.assertEqual(sub.call_count, 2)
        ops = json.loads(draft.video_op_json)
        self.assertEqual(set(ops), {"reference", "output"})
        self.assertEqual(ops["reference"]["op_name"], "op/ref")
        self.assertEqual(ops["output"]["op_name"], "op/out")

    def test_config_gate_no_creds_no_submit_stays_pending(self):
        draft = self._draft()
        with self._no_real_commit():
            self.Draft._cron_poll_video_ops()
        draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "pending")
        self.assertFalse(draft.video_op_json)
        self.assertFalse(draft.video_error)
        draft.action_approve()
        q = draft.approved_question_id
        self.assertEqual(len(q.video_ids), 0)
        self.assertFalse(q._has_required_videos())

    def test_quota_on_submit_stays_pending_no_failure(self):
        draft = self._draft()
        with self._no_real_commit(), \
                patch.object(vertex, "video_generation_available",
                             return_value=True), \
                patch.object(vertex, "submit_video_op",
                             side_effect=vertex.VertexQuotaError("429")):
            self.Draft._cron_poll_video_ops()
        draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "pending")
        self.assertFalse(draft.video_error)
        self.assertEqual(json.loads(draft.video_op_json or "{}"), {})


class TestVideoPromptPhase3Poll(_VideoPhase3Base):
    def test_poll_both_done_renders_and_materializes_with_url(self):
        draft = self._draft()
        names = iter(["op/ref", "op/out"])
        urls = iter(["https://cdn.example.com/ref.mp4",
                     "https://cdn.example.com/out.mp4"])

        def _fake_ingest(env, url=None, data=None, key_hint="qimg",
                         content_type=None):
            self.assertEqual(content_type, "video/mp4")
            return next(urls), False

        ingest_target = \
            "odoo.addons.etp_assessment_pro.services.image_ingest.ingest"
        with self._no_real_commit(), \
                patch.object(vertex, "video_generation_available",
                             return_value=True), \
                patch.object(vertex, "submit_video_op",
                             side_effect=lambda env, brief, **kw: next(names)), \
                patch.object(vertex, "fetch_video_op",
                             return_value={"done": True,
                                           "video_b64": _FAKE_MP4_B64,
                                           "gcs_uri": None, "error": None}), \
                patch(ingest_target, side_effect=_fake_ingest):
            self.Draft._cron_poll_video_ops()
        draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "rendered")
        files = json.loads(draft.video_files_json)
        self.assertEqual({f["slot"] for f in files}, {"reference", "output"})
        self.assertTrue(all(f["url"] for f in files))
        draft.action_approve()
        q = draft.approved_question_id
        self.assertEqual(len(q.video_ids), 2)
        by_slot = {v.slot: v for v in q.video_ids}
        self.assertEqual(by_slot["reference"].video_url,
                         "https://cdn.example.com/ref.mp4")
        self.assertEqual(by_slot["output"].video_url,
                         "https://cdn.example.com/out.mp4")
        self.assertTrue(q._has_required_videos())

    def test_all_or_nothing_one_done_one_pending_stays_generating(self):
        draft = self._draft(state="generating")
        draft.write({"video_op_json": json.dumps(
            self._ops("reference", "output"))})

        def _fake_fetch(env, op_name, *, model, location):
            if op_name == "op/reference":
                return {"done": True, "video_b64": _FAKE_MP4_B64,
                        "gcs_uri": None, "error": None}
            return {"done": False, "video_b64": None, "gcs_uri": None,
                    "error": None}

        with patch.object(vertex, "fetch_video_op", side_effect=_fake_fetch):
            draft._poll_video_ops()
        draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "generating")
        ops = json.loads(draft.video_op_json)
        self.assertEqual(ops["reference"]["state"], "done")
        self.assertNotEqual(ops["output"]["state"], "done")
        files = json.loads(draft.video_files_json)
        self.assertEqual([f["slot"] for f in files], ["reference"])

    def test_quota_on_fetch_no_attempt_spent_stays_generating(self):
        draft = self._draft(state="generating")
        draft.write({"video_op_json": json.dumps(
            self._ops("reference", "output"))})
        with patch.object(vertex, "fetch_video_op",
                          side_effect=vertex.VertexQuotaError("429")):
            draft._poll_video_ops()
        draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "generating")
        ops = json.loads(draft.video_op_json)
        self.assertEqual(ops["reference"]["attempts"], 0)
        self.assertEqual(ops["output"]["attempts"], 0)

    def test_op_failure_past_cap_fails_draft_not_rendered(self):
        draft = self._draft(slots=("single",), state="generating")
        draft.write({"video_op_json": json.dumps(self._ops("single"))})
        fail = {"done": True, "video_b64": None, "gcs_uri": None,
                "error": "safety block"}
        with patch.object(vertex, "fetch_video_op", return_value=fail):
            for _ in range(_VIDEO_OP_MAX_ATTEMPTS):
                draft._poll_video_ops()
                draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "failed")
        self.assertIn("safety", (draft.video_error or "").lower())

    def test_running_cron_twice_is_idempotent(self):
        draft = self._draft()
        names = iter(["op/ref", "op/out"])
        with self._no_real_commit(), \
                patch.object(vertex, "video_generation_available",
                             return_value=True), \
                patch.object(vertex, "submit_video_op",
                             side_effect=lambda env, brief, **kw: next(names)) \
                as sub, \
                patch.object(vertex, "fetch_video_op",
                             return_value={"done": True,
                                           "video_b64": _FAKE_MP4_B64,
                                           "gcs_uri": None, "error": None}):
            self.Draft._cron_poll_video_ops()
            self.Draft._cron_poll_video_ops()
        draft.invalidate_recordset()
        self.assertEqual(draft.video_state, "rendered")
        self.assertEqual(sub.call_count, 2)
        files = json.loads(draft.video_files_json)
        self.assertEqual(len(files), 2)


class TestVeoRequestShape(TransactionCase):
    class _Resp:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.content = b"x"
            self.text = json.dumps(self._payload)

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, resp):
            self._resp = resp
            self.calls = []

        def post(self, url, json=None, headers=None):
            self.calls.append({"url": url, "json": json, "headers": headers})
            return self._resp

    def test_submit_posts_predict_long_running_with_body(self):
        resp = self._Resp(payload={
            "name": "projects/p/locations/us-central1/operations/xyz"})
        client = self._Client(resp)
        with patch.object(vertex, "_minted_bearer", return_value="tok"), \
                patch.object(vertex, "_httpx", return_value=client), \
                patch.object(vertex, "_vertex_creds",
                             return_value=("proj-x", "global", "m", "")):
            op = vertex.submit_video_op(
                self.env, {"prompt": "a clip", "slot": "single"})
        self.assertEqual(
            op, "projects/p/locations/us-central1/operations/xyz")
        call = client.calls[0]
        self.assertIn("us-central1-aiplatform.googleapis.com", call["url"])
        self.assertIn("projects/proj-x/locations/us-central1", call["url"])
        self.assertTrue(call["url"].endswith(":predictLongRunning"))
        body = call["json"]
        self.assertEqual(body["instances"][0]["prompt"], "a clip")
        self.assertTrue(body["parameters"]["generateAudio"])
        self.assertEqual(body["parameters"]["sampleCount"], 1)
        self.assertEqual(body["parameters"]["aspectRatio"], "16:9")
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok")

    def test_submit_raises_quota_on_429(self):
        client = self._Client(self._Resp(status_code=429))
        with patch.object(vertex, "_minted_bearer", return_value="tok"), \
                patch.object(vertex, "_httpx", return_value=client), \
                patch.object(vertex, "_vertex_creds",
                             return_value=("p", "global", "m", "")):
            with self.assertRaises(vertex.VertexQuotaError):
                vertex.submit_video_op(self.env, {"prompt": "x"})

    def test_fetch_parses_done_with_inline_bytes(self):
        resp = self._Resp(payload={
            "done": True,
            "response": {"videos": [{"bytesBase64Encoded": "QUJD"}]}})
        client = self._Client(resp)
        with patch.object(vertex, "_minted_bearer", return_value="tok"), \
                patch.object(vertex, "_httpx", return_value=client), \
                patch.object(vertex, "_vertex_creds",
                             return_value=("p", "global", "m", "")):
            res = vertex.fetch_video_op(
                self.env, "op/xyz", model="veo-3.1-generate-001",
                location="us-central1")
        self.assertTrue(res["done"])
        self.assertEqual(res["video_b64"], "QUJD")
        self.assertTrue(client.calls[0]["url"].endswith(":fetchPredictOperation"))
        self.assertEqual(client.calls[0]["json"], {"operationName": "op/xyz"})

    def test_fetch_parses_gcs_only_generated_samples(self):
        resp = self._Resp(payload={
            "done": True,
            "response": {"generatedSamples": [
                {"video": {"uri": "gs://bucket/out.mp4"}}]}})
        client = self._Client(resp)
        with patch.object(vertex, "_minted_bearer", return_value="tok"), \
                patch.object(vertex, "_httpx", return_value=client), \
                patch.object(vertex, "_vertex_creds",
                             return_value=("p", "global", "m", "")):
            res = vertex.fetch_video_op(
                self.env, "op/xyz", model="veo-3.1-generate-001",
                location="us-central1")
        self.assertTrue(res["done"])
        self.assertIsNone(res["video_b64"])
        self.assertEqual(res["gcs_uri"], "gs://bucket/out.mp4")


class TestVeoUsageCostAndDashboard(TransactionCase):
    """A Veo submit must log a usage row priced PER SECOND, and that spend must
    surface in the LLM Budget dashboard (Videos card + Total Cost)."""

    def _submit(self, model, duration_s):
        resp = TestVeoRequestShape._Resp(payload={"name": "op/xyz"})
        client = TestVeoRequestShape._Client(resp)
        with patch.object(vertex, "_minted_bearer", return_value="tok"), \
                patch.object(vertex, "_httpx", return_value=client), \
                patch.object(vertex, "_vertex_creds",
                             return_value=("proj-x", "global", "m", "")):
            vertex.submit_video_op(
                self.env, {"prompt": "a clip"},
                model=model, duration_s=duration_s)

    def test_submit_logs_per_second_cost_and_shows_in_dashboard(self):
        Usage = self.env["etp.assessment.pro.llm.usage"]
        before = Usage.search_count([("operation", "=", "submit_video_op")])

        model = "veo-3.1-generate-001"
        duration_s = 6
        self._submit(model, duration_s)

        rows = Usage.search(
            [("operation", "=", "submit_video_op")], order="create_date desc")
        self.assertEqual(len(rows), before + 1)
        row = rows[0]
        self.assertEqual(row.model, model)
        self.assertEqual(row.video_seconds, float(duration_s))

        expected_cost = duration_s * vertex._VIDEO_PRICING[model]
        self.assertGreater(row.cost_usd, 0.0)
        self.assertAlmostEqual(row.cost_usd, expected_cost, places=5)
        self.assertEqual(row.tokens_in, 0)
        self.assertEqual(row.total_tokens, 0)

        dash = self.env["etp.assessment.pro.llm.dashboard"].create({})
        self.assertGreaterEqual(dash.total_videos, 1)
        self.assertGreaterEqual(dash.total_cost, expected_cost - 0.0001)
        self.assertIn("Submit Video (Veo)", dash.chart_cost_by_operation_html)
        self.assertIn("Submit Video (Veo)", dash.chart_cost_dist_html)
