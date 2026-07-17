# -*- coding: utf-8 -*-
"""End-to-end HTTP coverage for the candidate PORTAL layer (gap H.7 #1).

Establishes a real authenticated portal session with
``self.authenticate(login, password)`` and drives the actual single-sitting
routes in ``controllers/portal.py`` with ``url_open``, asserting BOTH the HTTP
status/redirect AND the resulting DB state (response rows + lines, evaluator
``state``, ``violation_count``, image bytes).

Facts pinned against the real source (re-confirmed by reading portal.py):
  * ``/pro_assessment/<token>`` redirects a *public* (unauthenticated) visitor
    to /web/login (portal.py) — an authenticated portal session is required;
    the token only scopes the evaluator.
  * ``.../begin`` / ``.../submit`` / ``.../finish`` / ``.../violation`` are
    csrf=True POST routes; ``url_open`` below auto-attaches the session csrf
    token (scraped from the exam page) so we don't thread it per call site.
  * ``_record_response`` form fields: ``question_id`` (int), ``justification``
    (required for subjective_*/image_prompt/image_label),
    ``dimension_<DIMENSION_ID>`` = the master option id, comma-separated for msq
    (required for mcq/msq/image_ab). Upsert = search-then-create-or-overwrite.
  * Candidate-portal flow makes NO Vertex calls — subjective answers are only
    ENQUEUED (``llm_state == 'pending'``) and scored later by cron.

The candidate's linked portal user is created with a KNOWN password so
``self.authenticate`` can log in as the real candidate.
"""
import base64
import json
import re
from uuid import uuid4

from odoo.tests.common import HttpCase, tagged

from odoo.addons.etp_assessment_pro.tests.test_phase23_lifecycle import _Base


def _valid_png_1px():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (200, 60, 60)).save(buf, format="PNG")
    return buf.getvalue()


_PNG_1PX = _valid_png_1px()


@tagged("-at_install", "post_install")
class TestPortalHttp(HttpCase, _Base):
    """Drive the live candidate portal routes over real HTTP."""

    _CSRF_RE = re.compile(
        r'name="csrf_token"[^>]*value="([^"]+)"'
        r'|value="([^"]+)"[^>]*name="csrf_token"')

    def url_open(self, url, data=None, **kw):
        tok = getattr(self, "_csrf_tok", "")
        if data is not None:
            if not tok:
                base = re.sub(r'/(begin|submit|finish|violation|review)$', '',
                              (url or "").split("?")[0])
                self.url_open(base)
                tok = getattr(self, "_csrf_tok", "")
            if isinstance(data, dict) and tok:
                data = dict(data, csrf_token=tok)
            return super().url_open(url, data=data, **kw)
        resp = super().url_open(url, **kw)
        try:
            m = self._CSRF_RE.search(resp.text or "")
            if m:
                self._csrf_tok = m.group(1) or m.group(2)
        except Exception:
            pass
        return resp

    def _portal_candidate(self, name):
        slug = name.lower().replace(" ", "_")
        login = "%s_%s@x.com" % (slug, uuid4().hex[:8])
        pwd = "portalpass1"
        portal = self.env.ref("base.group_portal")
        user = self.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": name, "login": login, "email": login,
                "password": pwd,
                "group_ids": [(6, 0, [portal.id])]})
        applicant = self.env["hr.applicant"].create({
            "partner_name": name, "email_from": login,
            "partner_id": user.partner_id.id,
            "candidate_user_id": user.id})
        return applicant, login, pwd

    def _launched(self, name="HttpCand", extra_questions=True):
        cat = self._make_category("HttpCat_%s" % name)
        q_mcq, dim_mcq, master_mcq = self._make_mcq(
            "HTTP_MCQ", correct_idx=0, category=cat,
            opt_names=("Red", "Green", "Blue"))
        payload = {"mcq": (q_mcq, dim_mcq, master_mcq)}
        if extra_questions:
            q_msq, dim_msq, master_msq = self._make_msq(
                "HTTP_MSQ", correct_idxs=(0, 1), category=cat,
                opt_names=("A", "B", "C", "D"))
            q_subj = self._make_subjective(
                "HTTP_SUBJ", category=cat, qtype="subjective_rubric")
            payload["msq"] = (q_msq, dim_msq, master_msq)
            payload["subj"] = q_subj

        applicant, login, pwd = self._portal_candidate(name)
        a = self.Assessment.create({
            "name": "HttpAssess", "generator_id": cat.id, "question_limit": 0,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [applicant.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        return ev, payload, login, pwd, applicant

    def _launched_single(self, name="SingleCand"):
        cat = self._make_category("SingleCat_%s" % name)
        q_mcq, dim_mcq, master_mcq = self._make_mcq(
            "SINGLE_MCQ", correct_idx=0, category=cat,
            opt_names=("Red", "Green", "Blue"))
        applicant, login, pwd = self._portal_candidate(name)
        a = self.Assessment.create({
            "name": "SingleAssess", "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [applicant.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        return ev, (q_mcq, dim_mcq, master_mcq), login, pwd, applicant

    # ---- 1. AUTH GATE ----------------------------------------------------

    def test_unauthenticated_landing_redirects_to_login(self):
        ev, _q, _login, _pwd, _app = self._launched_single()
        token = ev.access_token
        resp = self.url_open("/pro_assessment/%s" % token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/web/login", resp.url)

    # ---- 2. HAPPY PATH (authenticated candidate) -------------------------

    def test_happy_path_mcq_msq_subjective(self):
        ev, payload, login, pwd, _app = self._launched()
        q_mcq, dim_mcq, master_mcq = payload["mcq"]
        q_msq, dim_msq, master_msq = payload["msq"]
        q_subj = payload["subj"]
        token = ev.access_token
        self.authenticate(login, pwd)

        resp = self.url_open("/pro_assessment/%s" % token)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ev.started_at)

        resp = self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        self.assertIn(resp.status_code, (200, 303))
        ev.invalidate_recordset()
        self.assertEqual(ev.state, "in_progress")
        self.assertTrue(ev.started_at)

        resp = self.url_open("/pro_assessment/%s?q=1" % token)
        self.assertEqual(resp.status_code, 200)

        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_mcq.id),
                  "dimension_%d" % dim_mcq.id: str(master_mcq[0].id)})
        self.assertIn(resp.status_code, (200, 303))
        r_mcq = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q_mcq.id)])
        self.assertEqual(len(r_mcq), 1)
        self.assertEqual(r_mcq.state, "submitted")
        self.assertEqual(r_mcq.score, r_mcq.max_score)
        self.assertEqual(r_mcq.max_score, 1)
        self.assertEqual(
            set(r_mcq.line_ids.mapped("selected_option_id.id")),
            {master_mcq[0].id})

        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_msq.id),
                  "dimension_%d" % dim_msq.id:
                      "%d,%d" % (master_msq[0].id, master_msq[1].id)})
        self.assertIn(resp.status_code, (200, 303))
        r_msq = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q_msq.id)])
        self.assertEqual(len(r_msq), 1)
        self.assertEqual(r_msq.state, "submitted")
        self.assertEqual(
            set(r_msq.line_ids.mapped("selected_option_id.id")),
            {master_msq[0].id, master_msq[1].id})
        self.assertEqual(r_msq.score, r_msq.max_score)

        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_subj.id),
                  "justification": "My reasoned answer about the topic."})
        self.assertIn(resp.status_code, (200, 303))
        r_subj = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q_subj.id)])
        self.assertEqual(len(r_subj), 1)
        self.assertEqual(r_subj.state, "submitted")
        self.assertEqual(
            r_subj.justification, "My reasoned answer about the topic.")
        self.assertTrue(r_subj.needs_llm)
        self.assertEqual(r_subj.llm_state, "pending")

        resp = self.url_open("/pro_assessment/%s/review" % token)
        self.assertEqual(resp.status_code, 200)

        resp = self.url_open("/pro_assessment/%s/finish" % token, data={"_": "1"})
        self.assertIn(resp.status_code, (200, 303))
        ev.invalidate_recordset()
        self.assertEqual(ev.state, "submitted")
        self.assertTrue(ev.is_locked)

    def test_single_happy_path(self):
        ev, (q_mcq, dim_mcq, master_mcq), login, pwd, _app = \
            self._launched_single()
        token = ev.access_token
        self.authenticate(login, pwd)

        resp = self.url_open("/pro_assessment/%s" % token)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ev.started_at)

        resp = self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        self.assertIn(resp.status_code, (200, 303))
        ev.invalidate_recordset()
        self.assertTrue(ev.started_at)
        self.assertEqual(ev.state, "in_progress")

        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_mcq.id),
                  "dimension_%d" % dim_mcq.id: str(master_mcq[0].id)})
        self.assertIn(resp.status_code, (200, 303))
        r = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q_mcq.id)])
        self.assertEqual(len(r), 1)
        self.assertEqual(r.state, "submitted")
        self.assertEqual(r.score, 1)

        resp = self.url_open("/pro_assessment/%s/review" % token)
        self.assertEqual(resp.status_code, 200)

        resp = self.url_open("/pro_assessment/%s/finish" % token, data={"_": "1"})
        self.assertIn(resp.status_code, (200, 303))
        ev.invalidate_recordset()
        self.assertEqual(ev.state, "submitted")
        self.assertTrue(ev.is_locked)

    # ---- 3. EDIT-ON-BACK (overwrite, not duplicate) ----------------------

    def test_resubmit_overwrites_not_duplicates(self):
        ev, payload, login, pwd, _app = self._launched()
        q_mcq, dim_mcq, master_mcq = payload["mcq"]
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})

        self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_mcq.id),
                  "dimension_%d" % dim_mcq.id: str(master_mcq[0].id)})
        self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_mcq.id),
                  "dimension_%d" % dim_mcq.id: str(master_mcq[1].id)})

        rows = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q_mcq.id)])
        self.assertEqual(
            len(rows), 1,
            "re-submitting the same question must overwrite, not duplicate")
        self.assertEqual(
            set(rows.line_ids.mapped("selected_option_id.id")),
            {master_mcq[1].id})
        self.assertEqual(rows.score, 0)

    # ---- 4. VALIDATION (bad submits rejected, no row / no change) --------

    def test_submit_missing_option_rejected(self):
        ev, payload, login, pwd, _app = self._launched()
        q_mcq, dim_mcq, master_mcq = payload["mcq"]
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})

        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_mcq.id)})
        self.assertIn(resp.status_code, (200, 303))
        self.assertEqual(
            self.Response.search_count([
                ("assessment_evaluator_id", "=", ev.id),
                ("question_id", "=", q_mcq.id)]),
            0,
            "an MCQ submit with no selected option must not create a row")

    def test_submit_missing_justification_rejected(self):
        ev, payload, login, pwd, _app = self._launched()
        q_subj = payload["subj"]
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})

        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_subj.id), "justification": "   "})
        self.assertIn(resp.status_code, (200, 303))
        self.assertEqual(
            self.Response.search_count([
                ("assessment_evaluator_id", "=", ev.id),
                ("question_id", "=", q_subj.id)]),
            0,
            "a subjective submit with blank justification must not create a row")

    # ---- 5. INVALID TOKEN + CANDIDATE ISOLATION --------------------------

    def test_invalid_token_renders_invalid_page(self):
        resp = self.url_open("/pro_assessment/%s" % uuid4().hex)
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(resp.text.lower(), r"invalid|expired|not.*valid|login")

    def test_candidate_isolation_other_candidate_blocked(self):
        ev, payload, _login_a, _pwd_a, _app_a = self._launched(name="CandA")
        q_mcq, dim_mcq, master_mcq = payload["mcq"]
        token = ev.access_token
        ev_b, _payload_b, login_b, pwd_b, _app_b = self._launched(name="CandB")
        self.authenticate(login_b, pwd_b)
        self.url_open("/pro_assessment/%s" % ev_b.access_token)

        resp = self.url_open("/pro_assessment/%s" % token)
        self.assertEqual(resp.status_code, 200)
        ev.invalidate_recordset()
        self.assertEqual(ev.state, "pending")

        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        ev.invalidate_recordset()
        self.assertNotEqual(ev.state, "in_progress")

        self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_mcq.id),
                  "dimension_%d" % dim_mcq.id: str(master_mcq[0].id)})
        self.assertEqual(
            self.Response.search_count([
                ("assessment_evaluator_id", "=", ev.id)]),
            0,
            "another candidate must not be able to write responses via the token")

    def test_msq_native_repeated_checkbox_submit(self):
        ev, payload, login, pwd, _app = self._launched(name="MsqNative")
        q_msq, dim_msq, master_msq = payload["msq"]
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_msq.id),
                  "dimension_%d" % dim_msq.id: [str(master_msq[0].id),
                                                str(master_msq[1].id)]})
        r = self.Response.search([("assessment_evaluator_id", "=", ev.id),
                                  ("question_id", "=", q_msq.id)])
        self.assertEqual(len(r), 1)
        self.assertEqual(
            set(r.line_ids.mapped("selected_option_id.id")),
            {master_msq[0].id, master_msq[1].id},
            "both repeated checkbox values must be recorded via getlist")
        self.assertEqual(r.score, r.max_score)

    # ---- 6. VIOLATION ----------------------------------------------------

    def test_violation_increments_count(self):
        ev, _payload, login, pwd, _app = self._launched()
        ev.assessment_id.violation_action = "log_only"
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        ev.invalidate_recordset()
        self.assertEqual(ev.violation_count, 0)

        resp = self.url_open(
            "/pro_assessment/%s/violation" % token,
            data={"violation_reason": "tab switch"})
        self.assertIn(resp.status_code, (200, 303))
        ev.invalidate_recordset()
        self.assertEqual(ev.violation_count, 1)
        self.assertTrue(ev.is_violated)

        self.url_open(
            "/pro_assessment/%s/violation" % token,
            data={"violation_reason": "copy paste"})
        ev.invalidate_recordset()
        self.assertEqual(ev.violation_count, 2)

    def test_auto_submit_on_first_violation_when_no_cap(self):
        ev, _payload, login, pwd, _app = self._launched()
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        self.url_open(
            "/pro_assessment/%s/violation" % token,
            data={"violation_reason": "tab switch"})
        ev.invalidate_recordset()
        self.assertEqual(ev.violation_count, 1)
        self.assertTrue(ev.is_locked)
        self.assertEqual(ev.state, "submitted")

    # ---- 7. qimage route -------------------------------------------------

    def _launched_image_label(self, name="LabelCand", with_detections=True):
        cat = self._make_category("LabelCat_%s" % name)
        q_img = self.Question.create({
            "name": "LABEL_Q",
            "question_type": "image_label",
            "prompt": "Label each numbered box",
            "difficulty": "medium",
            "generator_id": cat.id,
        })
        img_vals = {
            "question_id": q_img.id,
            "label": "Single",
            "slot": "single",
            "image": base64.b64encode(_PNG_1PX).decode("ascii"),
        }
        if with_detections:
            img_vals.update({
                "annotated_image": base64.b64encode(_PNG_1PX).decode("ascii"),
                "detections_json": json.dumps([
                    {"number": 1, "label": "car", "description": "a red car",
                     "box_px": [0, 0, 1, 1]},
                    {"number": 2, "label": "tree", "description": "a tree",
                     "box_px": [0, 0, 1, 1]},
                ]),
            })
        image = self.env["etp.assessment.pro.question.image"].create(img_vals)
        applicant, login, pwd = self._portal_candidate(name)
        a = self.Assessment.create({
            "name": "LabelAssess", "generator_id": cat.id, "question_limit": 0,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [applicant.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        return ev, q_img, image, login, pwd

    def test_qimage_returns_bytes_for_real_image(self):
        ev, _q_img, image, login, pwd = self._launched_image_label(
            name="ImgCand")
        token = ev.access_token
        self.authenticate(login, pwd)

        resp = self.url_open("/pro_assessment/qimage/%s/%d" % (token, image.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content, "image route must return non-empty bytes")
        self.assertTrue(resp.headers.get("Content-Type", "").startswith("image"))

        bogus_id = image.id + 999999
        resp = self.url_open(
            "/pro_assessment/qimage/%s/%d" % (token, bogus_id))
        self.assertEqual(resp.status_code, 404)

    # ---- 8. image_label per-box labelling --------------------------------

    def test_image_label_renders_annotated_and_numbered_inputs(self):
        ev, _q_img, image, login, pwd = self._launched_image_label(
            name="LabelRender")
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        resp = self.url_open("/pro_assessment/%s?q=1" % token)
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn(
            "/pro_assessment/qimage/%s/%d?annotated=1" % (token, image.id),
            html, "annotated overlay image must be served to the candidate")
        self.assertIn('name="label_1"', html)
        self.assertIn('name="label_2"', html)
        self.assertIn("Label for box 1", html)
        self.assertIn("Label for box 2", html)
        self.assertNotIn("a red car", html)
        self.assertNotIn("detections_json", html)

    def test_image_label_records_json_dict_answer(self):
        ev, q_img, _image, login, pwd = self._launched_image_label(
            name="LabelRecord")
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        resp = self.url_open(
            "/pro_assessment/%s/submit" % token,
            data={"question_id": str(q_img.id),
                  "label_1": "car", "label_2": "tree"})
        self.assertIn(resp.status_code, (200, 303))
        r = self.Response.search([("assessment_evaluator_id", "=", ev.id),
                                  ("question_id", "=", q_img.id)])
        self.assertEqual(len(r), 1)
        self.assertEqual(r.state, "submitted")
        self.assertEqual(
            json.loads(r.justification), {"1": "car", "2": "tree"},
            "per-box labels must persist as a JSON dict for phase-6 scoring")

    def test_image_label_fallback_single_textarea(self):
        ev, _q_img, image, login, pwd = self._launched_image_label(
            name="LabelFallback", with_detections=False)
        token = ev.access_token
        self.authenticate(login, pwd)
        self.url_open("/pro_assessment/%s/begin" % token, data={"_": "1"})
        resp = self.url_open("/pro_assessment/%s?q=1" % token)
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertNotIn('name="label_1"', html)
        self.assertNotIn("?annotated=1", html)
        self.assertIn('name="justification"', html)
        self.assertIn(
            "/pro_assessment/qimage/%s/%d" % (token, image.id), html,
            "fallback must still show the plain source image")

    # ---- sanity: token order is what we think it is ----------------------

    def test_question_order_matches_assigned_set(self):
        ev, payload, _login, _pwd, _app = self._launched()
        order = set(json.loads(ev.question_order or "[]"))
        expected = {payload["mcq"][0].id, payload["msq"][0].id,
                    payload["subj"].id}
        self.assertEqual(order, expected)
