"""End-to-end REST API tests for batches + roles (v1.1, HttpCase).

Token auth follows the v1.0 pattern: ``api.access_token`` rows are created
directly via ORM (the gateway's HTTP issuance route is incompatible with
the test transaction), then every call sends the ``access-token`` header
through the real ``@validate_token`` decorator.

LLM execution: HTTP triggers only enqueue work; completion runs through the
queue cron invoked in the test thread with ``mock_llm`` active (the HTTP
routes themselves never call the LLM).
"""

import base64
import json
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from .common import (
    API_KEY_PARAM,
    RESUME_TEXT,
    VALID_BATCH_REPORT,
    VALID_SHIP_RECORD,
    make_pdf_bytes,
    mock_llm,
    patch_encryption_env,
)
from odoo.addons.api_auth_gateway.models import access_token as access_token_model
from odoo.addons.iris.models import credential_manager

PASSWORD = "IrisBatchApi#2026!"
JSON_HEADERS = {"Content-Type": "application/json"}
BASE = "/api/v1/iris/batches"


def _pdf_b64(text):
    return base64.b64encode(make_pdf_bytes(text)).decode("ascii")


@tagged("post_install", "-at_install", "iris")
class TestIrisBatchApi(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env_patcher, _key = patch_encryption_env()
        cls.addClassCleanup(env_patcher.stop)

        Users = cls.env["res.users"].with_context(no_reset_password=True)
        base_group = cls.env.ref("base.group_user")
        cls.user_plain = Users.create({
            "name": "Batch API Plain",
            "login": "iris_batch_api_plain",
            "email": "iris_batch_api_plain@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [base_group.id])],
        })
        cls.user_iris = Users.create({
            "name": "Batch API Iris User",
            "login": "iris_batch_api_user",
            "email": "iris_batch_api_user@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [
                base_group.id,
                cls.env.ref("iris.group_iris_user").id,
            ])],
        })
        cls.user_manager = Users.create({
            "name": "Batch API Iris Manager",
            "login": "iris_batch_api_manager",
            "email": "iris_batch_api_manager@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [
                base_group.id,
                cls.env.ref("iris.group_iris_manager").id,
            ])],
        })

        cls.role_hoe = cls.env.ref("iris.role_head_of_engineering")
        credential_manager.set_encrypted_param(
            cls.env, API_KEY_PARAM, "sk-iris-batch-api-test",
        )
        connectors = cls.env["s3.connector"].sudo().search([])
        if connectors:
            connectors.unlink()
        cls.env.flush_all()

    def setUp(self):
        super().setUp()
        self.token_user = self._get_token("iris_batch_api_user")
        self.token_manager = self._get_token("iris_batch_api_manager")
        self.token_plain = self._get_token("iris_batch_api_plain")

    # ------------------------------------------------------------------
    # Helpers (v1.0 pattern — see test_api_controllers.py)
    # ------------------------------------------------------------------
    def _get_token(self, login):
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
            headers["access-token"] = token
        data = json.dumps(payload) if payload is not None else None
        self.env.flush_all()
        resp = self.url_open(url, data=data, headers=headers, method=method)
        self.env.invalidate_all()
        return resp

    def _make_candidate(self, name="Draft Member", **overrides):
        vals = {"name": name, "resume_text": RESUME_TEXT}
        vals.update(overrides)
        candidate = self.env["iris.candidate"].create(vals)
        self.env.flush_all()
        return candidate

    def _make_batch(self):
        batch = self.env["iris.screening.batch"].create({
            "role_id": self.role_hoe.id,
        })
        self.env.flush_all()
        return batch

    def _run_llm_queue(self):
        self.env["iris.candidate"]._cron_process_llm_queue()
        self.env.flush_all()

    # ------------------------------------------------------------------
    # Bulk intake: per-index errors, all-or-nothing
    # ------------------------------------------------------------------
    def test_bulk_create_per_index_pdf_error_is_all_or_nothing(self):
        Batch = self.env["iris.screening.batch"].sudo()
        Candidate = self.env["iris.candidate"].sudo()
        batches_before = Batch.search_count([])
        candidates_before = Candidate.search_count([])

        resp = self._request("POST", BASE, token=self.token_user, payload={
            "role_code": "head_of_engineering",
            "candidates": [
                {
                    "name": "Valid Member",
                    "resume_base64": _pdf_b64("Valid Member — engineer"),
                    "resume_filename": "valid.pdf",
                },
                {
                    "name": "Bad Pdf Member",
                    "resume_base64": base64.b64encode(b"plain text").decode(),
                    "resume_filename": "bad.pdf",
                },
                {"resume_base64": _pdf_b64("No Name"), "resume_filename": "x.pdf"},
            ],
        })
        self.assertEqual(resp.status_code, 400, resp.text)
        errors = resp.json()["errors"]
        self.assertIn("candidates[1]: resume_base64 is not a PDF.", errors)
        self.assertIn("candidates[2]: name is required.", errors)

        # ALL-OR-NOTHING: the valid entry was not created either.
        self.assertEqual(Batch.search_count([]), batches_before)
        self.assertEqual(Candidate.search_count([]), candidates_before)

    def test_bulk_create_invalid_base64_per_index_error(self):
        resp = self._request("POST", BASE, token=self.token_user, payload={
            "role_code": "head_of_engineering",
            "candidates": [{
                "name": "Broken B64",
                "resume_base64": "@@not-base64@@",
                "resume_filename": "cv.pdf",
            }],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            "candidates[0]: resume_base64 is not valid base64.",
            resp.json()["errors"],
        )

    def test_screen_now_requires_resumes_and_two_members(self):
        resp = self._request("POST", BASE, token=self.token_user, payload={
            "role_code": "head_of_engineering",
            "screen_now": True,
            "candidates": [{"name": "No Resume"}, {"name": "Also None"}],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            "candidates[0]: resume_base64 is required when screen_now is true.",
            resp.json()["errors"],
        )

        resp = self._request("POST", BASE, token=self.token_user, payload={
            "role_code": "head_of_engineering",
            "screen_now": True,
            "candidates": [{
                "name": "Lonely",
                "resume_base64": _pdf_b64("Lonely — engineer"),
                "resume_filename": "lonely.pdf",
            }],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least 2", resp.text)

    def test_unknown_role_is_400_with_valid_list(self):
        resp = self._request("POST", BASE, token=self.token_user, payload={
            "role_code": "nonexistent_role",
            "candidates": [],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("head_of_engineering", resp.text)

    # ------------------------------------------------------------------
    # screen_now → status poll → done → report
    # ------------------------------------------------------------------
    def test_screen_now_status_poll_to_done_and_report(self):
        resp = self._request("POST", BASE, token=self.token_user, payload={
            "role_code": "head_of_engineering",
            "screen_now": True,
            "candidates": [
                {
                    "name": "Poll Alpha",
                    "resume_base64": _pdf_b64("Poll Alpha — platform lead"),
                    "resume_filename": "alpha.pdf",
                },
                {
                    "name": "Poll Beta",
                    "resume_base64": _pdf_b64("Poll Beta — infra manager"),
                    "resume_filename": "beta.pdf",
                },
            ],
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["batch"]
        bid = body["id"]
        self.assertEqual(body["state"], "screening")
        self.assertEqual(body["member_count"], 2)
        for member in body["members"]:
            self.assertEqual(member["screening"]["llm_status"], "queued")

        # Poll while members are still screening.
        resp = self._request("GET", f"{BASE}/{bid}/status", token=self.token_user)
        self.assertEqual(resp.status_code, 200)
        status = resp.json()
        self.assertEqual(status["state"], "screening")
        self.assertEqual(status["members"]["total"], 2)
        self.assertEqual(status["members"]["settled"], 0)
        self.assertEqual(len(status["members"]["blocking"]), 2)

        # Report is 404 until done.
        resp = self._request("GET", f"{BASE}/{bid}/report", token=self.token_user)
        self.assertEqual(resp.status_code, 404)

        # Queue cron settles both members (LLM mocked in-process) — the
        # write-hook feeder flips the batch to consistency.
        with mock_llm(VALID_SHIP_RECORD):
            self._run_llm_queue()
        resp = self._request("GET", f"{BASE}/{bid}/status", token=self.token_user)
        status = resp.json()
        self.assertEqual(status["state"], "consistency")
        self.assertEqual(status["members"]["settled"], 2)
        self.assertEqual(status["consistency_llm_status"], "queued")

        # Still no report mid-consistency.
        resp = self._request("GET", f"{BASE}/{bid}/report", token=self.token_user)
        self.assertEqual(resp.status_code, 404)

        # Second cron run executes the consistency pass.
        batch = self.env["iris.screening.batch"].browse(bid)
        members = batch.candidate_ids.sorted("id")
        report_md = VALID_BATCH_REPORT.format(
            ref1=members[0].reference, ref2=members[1].reference,
        )
        with mock_llm(report_md):
            self._run_llm_queue()

        resp = self._request("GET", f"{BASE}/{bid}/status", token=self.token_user)
        status = resp.json()
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["consistency_llm_status"], "done")
        self.assertGreater(status["total_cost_usd"], 0.0)

        resp = self._request("GET", f"{BASE}/{bid}/report", token=self.token_user)
        self.assertEqual(resp.status_code, 200, resp.text)
        report = resp.json()
        self.assertEqual(report["report_markdown"], report_md)
        self.assertEqual(
            report["machine_summary"]["schema"], "iris.batch_consistency.v1",
        )
        self.assertEqual(report["inconsistency_count"], 1)
        self.assertEqual(report["revision_advisory_count"], 1)

    # ------------------------------------------------------------------
    # Member management (draft only)
    # ------------------------------------------------------------------
    def test_member_add_remove_draft_only(self):
        batch = self._make_batch()
        alpha = self._make_candidate(name="Member Alpha")
        beta = self._make_candidate(name="Member Beta")
        url = f"{BASE}/{batch.id}/candidates"

        # Add both existing draft candidates.
        resp = self._request(
            "POST", url, token=self.token_user,
            payload={"candidate_ids": [alpha.id, beta.id]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["batch"]["member_count"], 2)

        # A non-draft candidate is rejected with a per-index error.
        shipped = self._make_candidate(name="Already Shipped")
        shipped.write({"state": "shipped"})
        self.env.flush_all()
        resp = self._request(
            "POST", url, token=self.token_user,
            payload={"candidate_ids": [shipped.id]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not in Draft", resp.text)

        # Remove one member.
        resp = self._request(
            "DELETE", f"{url}/{alpha.id}", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["batch"]["member_count"], 1)
        self.assertFalse(alpha.batch_id)

        # Removing a non-member is a 404.
        resp = self._request(
            "DELETE", f"{url}/{alpha.id}", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 404)

        # Re-add, kick off, then member management locks.
        resp = self._request(
            "POST", url, token=self.token_user,
            payload={"candidate_ids": [alpha.id]},
        )
        self.assertEqual(resp.status_code, 200)
        resp = self._request(
            "POST", f"{BASE}/{batch.id}/screen", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["batch"]["state"], "screening")

        extra = self._make_candidate(name="Too Late")
        resp = self._request(
            "POST", url, token=self.token_user,
            payload={"candidate_ids": [extra.id]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Draft", resp.text)
        resp = self._request(
            "DELETE", f"{url}/{beta.id}", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Draft", resp.text)

    # ------------------------------------------------------------------
    # Roles (read-only API)
    # ------------------------------------------------------------------
    def test_roles_list_with_creation_locked_flag(self):
        resp = self._request("GET", "/api/v1/iris/roles", token=self.token_user)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        codes = [role["code"] for role in body["roles"]]
        self.assertIn("head_of_engineering", codes)
        self.assertTrue(body["creation_locked"],
                        "v1.1 ships with role creation locked")

        # The ICP feature flag flips the envelope flag (the v1.2 unlock).
        self.env["ir.config_parameter"].sudo().set_param(
            "iris.enable_role_creation", "1",
        )
        resp = self._request("GET", "/api/v1/iris/roles", token=self.token_user)
        self.assertFalse(resp.json()["creation_locked"])
        self.env["ir.config_parameter"].sudo().search([
            ("key", "=", "iris.enable_role_creation"),
        ]).unlink()

    def test_roles_detail_hides_prompt_overrides(self):
        resp = self._request(
            "GET", f"/api/v1/iris/roles/{self.role_hoe.id}",
            token=self.token_user,
        )
        self.assertEqual(resp.status_code, 200)
        role = resp.json()["role"]
        self.assertEqual(role["code"], "head_of_engineering")
        self.assertTrue(role["competence_guidance"])
        self.assertTrue(role["default_tech_date_reference"])
        self.assertNotIn("screening_prompt", role)
        self.assertNotIn("batch_consistency_prompt", role)

        resp = self._request(
            "GET", "/api/v1/iris/roles/99999999", token=self.token_user,
        )
        self.assertEqual(resp.status_code, 404)

    def test_roles_require_iris_group(self):
        resp = self._request("GET", "/api/v1/iris/roles", token=self.token_plain)
        self.assertEqual(resp.status_code, 403)

    # ------------------------------------------------------------------
    # Candidate creation: role_id / role_code / target_role
    # ------------------------------------------------------------------
    def test_candidate_create_via_role_code(self):
        resp = self._request(
            "POST", "/api/v1/iris/candidates", token=self.token_user,
            payload={"name": "Via Code", "role_code": "head_of_engineering"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        candidate = resp.json()["candidate"]
        self.assertEqual(candidate["role"]["code"], "head_of_engineering")
        self.assertEqual(candidate["target_role"], "Head of Engineering")

    def test_candidate_create_via_role_id(self):
        resp = self._request(
            "POST", "/api/v1/iris/candidates", token=self.token_user,
            payload={"name": "Via Id", "role_id": self.role_hoe.id},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json()["candidate"]["role"]["id"], self.role_hoe.id,
        )

    def test_candidate_create_via_legacy_target_role_string(self):
        resp = self._request(
            "POST", "/api/v1/iris/candidates", token=self.token_user,
            payload={"name": "Via Name", "target_role": "head OF engineering"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        candidate = resp.json()["candidate"]
        self.assertEqual(candidate["role"]["code"], "head_of_engineering")
        self.assertEqual(candidate["target_role"], "Head of Engineering")

    def test_candidate_create_unknown_role_is_400_listing_valid_roles(self):
        for payload in (
            {"name": "Bogus Role", "target_role": "Chief Vibes Officer"},
            {"name": "Bogus Code", "role_code": "chief_vibes_officer"},
            {"name": "Bogus Id", "role_id": 99999999},
        ):
            resp = self._request(
                "POST", "/api/v1/iris/candidates", token=self.token_user,
                payload=payload,
            )
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertIn("head_of_engineering", resp.text)

        # Never silently coerced: nothing was created.
        self.assertFalse(self.env["iris.candidate"].sudo().search([
            ("name", "in", ["Bogus Role", "Bogus Code", "Bogus Id"]),
        ]))
