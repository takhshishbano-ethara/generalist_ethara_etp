"""End-to-end REST API tests for ``/api/v1/iris/...`` (HttpCase).

Token auth: an ``api.access_token`` row is created directly via ORM (the
gateway's HTTP issuance route is incompatible with the test transaction —
see ``_get_token``), then every Iris call sends the ``access_token`` header
through the real ``@validate_token`` decorator.

LLM execution note: HTTP triggers only *enqueue* work (the route returns
``llm_status='queued'``); the actual completion runs through the queue cron.
The cron is invoked here directly in the test thread with ``mock_llm``
active — the Odoo test HTTP server is in-process, and the HTTP routes
themselves never call the LLM, so no cross-thread patching is required.
"""

import base64
import json
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from .common import (
    API_KEY_PARAM,
    RESUME_TEXT,
    VALID_HOLD_RECORD,
    VALID_SCORECARD_STRONG_HIRE,
    VALID_SHIP_RECORD,
    make_pdf_bytes,
    mock_llm,
    patch_encryption_env,
)
from odoo.addons.api_auth_gateway.models import access_token as access_token_model
from odoo.addons.iris.models import credential_manager

PASSWORD = "IrisApiTest#2026!"
JSON_HEADERS = {"Content-Type": "application/json"}


@tagged("post_install", "-at_install", "iris")
class TestIrisApi(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env_patcher, _key = patch_encryption_env()
        cls.addClassCleanup(env_patcher.stop)

        Users = cls.env["res.users"].with_context(no_reset_password=True)
        base_group = cls.env.ref("base.group_user")
        cls.user_plain = Users.create({
            "name": "API Plain",
            "login": "iris_api_plain",
            "email": "iris_api_plain@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [base_group.id])],
        })
        cls.user_iris = Users.create({
            "name": "API Iris User",
            "login": "iris_api_user",
            "email": "iris_api_user@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [
                base_group.id,
                cls.env.ref("iris.group_iris_user").id,
            ])],
        })
        cls.user_manager = Users.create({
            "name": "API Iris Manager",
            "login": "iris_api_manager",
            "email": "iris_api_manager@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [
                base_group.id,
                cls.env.ref("iris.group_iris_manager").id,
            ])],
        })

        credential_manager.set_encrypted_param(
            cls.env, API_KEY_PARAM, "sk-iris-api-test",
        )
        connectors = cls.env["s3.connector"].sudo().search([])
        if connectors:
            connectors.unlink()
        cls.env.flush_all()

    def setUp(self):
        super().setUp()
        self.token_user = self._get_token("iris_api_user")
        self.token_manager = self._get_token("iris_api_manager")
        self.token_plain = self._get_token("iris_api_plain")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_token(self, login):
        # Create the api.access_token row directly via ORM. The gateway's
        # POST /api/v1/auth_token cannot be exercised from HttpCase: it is
        # auth="none" (→ readonly=True default in Odoo 19) yet writes
        # res_users_log during session.authenticate, and its token helper
        # calls cr.commit() — both forbidden inside the test transaction.
        # Production traffic still uses the HTTP route; @validate_token
        # (what iris routes actually depend on) only reads this table.
        user = self.env["res.users"].sudo().search([("login", "=", login)], limit=1)
        self.assertTrue(user, f"no such test user: {login}")
        token = access_token_model.nonce()
        self.env["api.access_token"].sudo().create({
            "user_id": user.id,
            "access_token": token,
            "refresh_token": access_token_model.nonce(),
            "expiry": fields.Datetime.now() + timedelta(seconds=3600),
        })
        self.env.flush_all()
        return token

    def _request(self, method, url, token=None, payload=None):
        headers = dict(JSON_HEADERS)
        if token:
            # Dash form: the HTTP server drops underscore header names (same
            # as nginx default in prod); werkzeug normalizes access-token so
            # the gateway's headers.get("access_token") still matches.
            headers["access-token"] = token
        data = json.dumps(payload) if payload is not None else None
        self.env.flush_all()
        resp = self.url_open(url, data=data, headers=headers, method=method)
        self.env.invalidate_all()
        return resp

    def _make_candidate(self, name="Jane Doe", **overrides):
        vals = {
            "name": name,
            "target_role": "Senior ML Engineer",
            "resume_text": RESUME_TEXT,
        }
        vals.update(overrides)
        candidate = self.env["iris.candidate"].create(vals)
        self.env.flush_all()
        return candidate

    def _run_llm_queue(self):
        self.env["iris.candidate"]._cron_process_llm_queue()
        self.env.flush_all()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def test_missing_token_is_401(self):
        resp = self.url_open("/api/v1/iris/candidates")
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_is_401(self):
        resp = self._request("GET", "/api/v1/iris/candidates", token="bogus")
        self.assertEqual(resp.status_code, 401)

    def test_token_without_iris_group_is_403(self):
        resp = self._request(
            "GET", "/api/v1/iris/candidates", token=self.token_plain,
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    # ------------------------------------------------------------------
    # Candidate CRUD
    # ------------------------------------------------------------------
    def test_candidate_crud_happy_path(self):
        # POST create
        resp = self._request(
            "POST", "/api/v1/iris/candidates", token=self.token_user,
            payload={
                "name": "Api Crud",
                # v1.1: target_role must resolve to a known role profile
                # (unknown strings 400 by design — never silently coerced).
                "target_role": "Head of Engineering",
                "email": "crud@example.com",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        candidate = resp.json()["candidate"]
        cid = candidate["id"]
        self.assertEqual(candidate["state"], "draft")
        self.assertTrue(candidate["reference"].startswith("IRC"))

        # GET detail
        resp = self._request(
            "GET", f"/api/v1/iris/candidates/{cid}", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["candidate"]["email"], "crud@example.com")

        # PUT update
        resp = self._request(
            "PUT", f"/api/v1/iris/candidates/{cid}", token=self.token_user,
            payload={"phone": "+1 555 042", "name": "Api Crud Renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["candidate"]
        self.assertEqual(body["name"], "Api Crud Renamed")
        self.assertEqual(body["phone"], "+1 555 042")

        # GET list contains the record
        resp = self._request(
            "GET", "/api/v1/iris/candidates?search=Crud%20Renamed",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200)
        ids = [c["id"] for c in resp.json()["candidates"]]
        self.assertIn(cid, ids)

    def test_candidate_detail_404(self):
        resp = self._request(
            "GET", "/api/v1/iris/candidates/99999999", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_validation_errors(self):
        resp = self._request(
            "POST", "/api/v1/iris/candidates", token=self.token_user,
            payload={"name": "No Role"},
        )
        self.assertEqual(resp.status_code, 400)

        resp = self._request(
            "GET", "/api/v1/iris/candidates?state=bogus", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 400)

    def test_pagination_limit_clamped_to_200(self):
        self._make_candidate(name="Clamp One")
        self._make_candidate(name="Clamp Two")
        resp = self._request(
            "GET", "/api/v1/iris/candidates?limit=999&page=1",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200)
        pagination = resp.json()["pagination"]
        self.assertEqual(pagination["limit"], 200)
        self.assertGreaterEqual(pagination["total_records"], 2)

    def test_delete_is_manager_only(self):
        candidate = self._make_candidate(name="Del Me")
        url = f"/api/v1/iris/candidates/{candidate.id}"

        resp = self._request("DELETE", url, token=self.token_user)
        self.assertEqual(resp.status_code, 403)

        resp = self._request("DELETE", url, token=self.token_manager)
        self.assertEqual(resp.status_code, 200)

        resp = self._request("GET", url, token=self.token_manager)
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # Resume upload + extraction
    # ------------------------------------------------------------------
    def test_resume_upload_populates_resume_text(self):
        candidate = self._make_candidate(name="Pdf Person", resume_text=False)
        pdf_b64 = base64.b64encode(
            make_pdf_bytes("Pdf Person — Staff ML Engineer at Initech"),
        ).decode("ascii")

        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{candidate.id}/resume",
            token=self.token_user,
            payload={"resume_base64": pdf_b64, "resume_filename": "cv.pdf"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["candidate"]["has_resume"])

        candidate.invalidate_recordset()
        self.assertIn("Staff ML Engineer at Initech", candidate.resume_text)
        self.assertEqual(candidate.resume_filename, "cv.pdf")
        self.assertTrue(candidate.resume_uploaded_at)

    def test_resume_upload_rejects_non_pdf(self):
        candidate = self._make_candidate(name="Bad Pdf", resume_text=False)
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{candidate.id}/resume",
            token=self.token_user,
            payload={
                "resume_base64": base64.b64encode(b"plain text").decode(),
                "resume_filename": "cv.pdf",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_resume_download_404_without_s3(self):
        candidate = self._make_candidate(name="No S3")
        resp = self._request(
            "GET", f"/api/v1/iris/candidates/{candidate.id}/resume",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # Pipeline triggers
    # ------------------------------------------------------------------
    def test_screen_without_resume_is_400(self):
        candidate = self._make_candidate(name="No Resume", resume_text=False)
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{candidate.id}/screen",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 400)

    def test_screen_then_status_polling_with_mocked_llm(self):
        candidate = self._make_candidate(name="Poll Me")
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{candidate.id}/screen",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["llm_status"], "queued")
        self.assertTrue(body["screening_id"])

        # Client polls while the job is queued.
        resp = self._request(
            "GET", f"/api/v1/iris/candidates/{candidate.id}/status",
            token=self.token_user,
        )
        self.assertEqual(resp.json()["state"], "screening")
        self.assertEqual(resp.json()["llm_status"]["screening"], "queued")

        # The queue cron completes the job (LLM mocked in-process).
        with mock_llm(VALID_SHIP_RECORD):
            self._run_llm_queue()

        resp = self._request(
            "GET", f"/api/v1/iris/candidates/{candidate.id}/status",
            token=self.token_user,
        )
        body = resp.json()
        self.assertEqual(body["state"], "shipped")
        self.assertEqual(body["current_verdict"], "ship")
        self.assertEqual(body["llm_status"]["screening"], "done")

    def test_evidence_and_rescreen_flow(self):
        candidate = self._make_candidate(name="Hold Api")
        candidate.action_screen()
        with mock_llm(VALID_HOLD_RECORD):
            self._run_llm_queue()
        self.assertEqual(candidate.state, "hold")

        # Re-screen without evidence → 400 (UserError mapped).
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{candidate.id}/rescreen",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 400)

        # Evidence + rescreen_now spawns the chained re-screen.
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{candidate.id}/evidence",
            token=self.token_user,
            payload={
                "evidence": "Reference call verified the 40M/day claim.",
                "rescreen_now": True,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["candidate_state"], "screening")
        self.assertEqual(body["llm_status"], "queued")

        rescreen = self.env["iris.screening"].browse(body["screening_id"])
        self.assertTrue(rescreen.is_rescreen)
        self.assertTrue(rescreen.parent_screening_id)

    def test_manual_verdict_is_manager_only(self):
        candidate = self._make_candidate(name="Review Me")
        candidate.write({"state": "needs_review"})
        screening = self.env["iris.screening"].create({
            "candidate_id": candidate.id,
        })
        screening.write({"llm_status": "needs_review"})
        url = f"/api/v1/iris/candidates/{candidate.id}/verdict"

        resp = self._request(
            "POST", url, token=self.token_user, payload={"verdict": "ship"},
        )
        self.assertEqual(resp.status_code, 403)

        resp = self._request(
            "POST", url, token=self.token_manager, payload={"verdict": "bogus"},
        )
        self.assertEqual(resp.status_code, 400)

        resp = self._request(
            "POST", url, token=self.token_manager, payload={"verdict": "ship"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["candidate"]["state"], "shipped")

    # ------------------------------------------------------------------
    # Full pipeline over the API
    # ------------------------------------------------------------------
    def test_full_pipeline_over_api(self):
        candidate = self._make_candidate(name="Flow Complete")
        cid = candidate.id

        # 1) Screen → SHIP.
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{cid}/screen",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        with mock_llm(VALID_SHIP_RECORD):
            self._run_llm_queue()

        # Screening record is readable with full markdown + metadata.
        screening_id = self._request(
            "GET", f"/api/v1/iris/candidates/{cid}/status",
            token=self.token_user,
        ).json()["artifact_ids"]["screening_id"]
        resp = self._request(
            "GET", f"/api/v1/iris/screenings/{screening_id}",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200)
        screening_body = resp.json()["screening"]
        self.assertEqual(screening_body["verdict"], "ship")
        self.assertIn("Forensic Ladder", screening_body["markdown_record"])
        self.assertEqual(screening_body["llm_prompt_tokens"], 100)

        # 2) Interview guide (only from shipped).
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{cid}/interviews",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        interview_id = resp.json()["interview_id"]
        with mock_llm("# Interview Guide\n\nSteering Ladder questions..."):
            self._run_llm_queue()

        resp = self._request(
            "GET", f"/api/v1/iris/interviews/{interview_id}",
            token=self.token_user,
        )
        self.assertIn("Steering Ladder", resp.json()["interview"]["guide_markdown"])

        # A second guide for a non-shipped candidate is rejected.
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{cid}/interviews",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 400)

        # 3) Scorecard before notes → 400.
        resp = self._request(
            "POST", f"/api/v1/iris/interviews/{interview_id}/scorecard",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 400)

        # 4) Notes PUT → interviewed.
        resp = self._request(
            "PUT", f"/api/v1/iris/interviews/{interview_id}/notes",
            token=self.token_user,
            payload={
                "notes": "Q1 5 caught R1; Q2 4; zero red flags.",
                "interviewer_name": "External Panelist",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["candidate_state"], "interviewed")

        # 5) Scorecard → scored.
        resp = self._request(
            "POST", f"/api/v1/iris/interviews/{interview_id}/scorecard",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        scorecard_id = resp.json()["scorecard_id"]
        with mock_llm(VALID_SCORECARD_STRONG_HIRE):
            self._run_llm_queue()

        resp = self._request(
            "GET", f"/api/v1/iris/scorecards/{scorecard_id}",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200)
        scorecard_body = resp.json()["scorecard"]
        self.assertEqual(scorecard_body["recommendation"], "strong_hire")
        self.assertIn("**Recommendation:**", scorecard_body["scorecard_markdown"])

        # 6) Final decision.
        resp = self._request(
            "POST", f"/api/v1/iris/candidates/{cid}/decision",
            token=self.token_user, payload={"decision": "hired"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["candidate"]["state"], "hired")

    # ------------------------------------------------------------------
    # Artifact list endpoints + manager-only deletes
    # ------------------------------------------------------------------
    def test_artifact_lists_and_manager_deletes(self):
        candidate = self._make_candidate(name="Artifacts Inc")
        candidate.action_screen()
        with mock_llm(VALID_SHIP_RECORD):
            self._run_llm_queue()
        screening = candidate.screening_ids

        resp = self._request(
            "GET", f"/api/v1/iris/screenings?candidate_id={candidate.id}",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [s["id"] for s in resp.json()["screenings"]], [screening.id],
        )

        resp = self._request(
            "DELETE", f"/api/v1/iris/screenings/{screening.id}",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 403)

        resp = self._request(
            "DELETE", f"/api/v1/iris/screenings/{screening.id}",
            token=self.token_manager,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(screening.exists())
