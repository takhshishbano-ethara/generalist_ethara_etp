"""Dual second-screener sign-off on BLOCK (v1.1, P0-1).

Contract under test (governance design, Item 1):

* every human-visible BLOCK (LLM-parsed or manager manual verdict) lands
  the candidate in ``pending_block`` — never directly in ``blocked``;
* the screening keeps ``verdict='block'`` immediately, with the proposer
  recorded (LLM path → the user who requested the run; manual path → the
  acting manager) and ``block_signoff_state='pending'``;
* co-sign requires ANY iris user DIFFERENT from the proposer and a
  ``block_kind`` (credibility/competence) → ``blocked`` + deadline cleared;
* sign-off rejection requires a reason (the proposer MAY reject their own)
  → ``needs_review`` with the screening's ``llm_status`` set to
  ``needs_review`` (load-bearing for ``_apply_manual_verdict``);
* the daily auto-block cron is structurally EXEMPT (domain ``state='hold'``)
  and never touches ``pending_block`` rows;
* ``hold_deadline`` is kept while pending (never-extended invariant) and
  cleared only by the confirmed co-sign (or a SHIP).
"""

import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged

from .common import (
    API_KEY_PARAM,
    RESUME_TEXT,
    VALID_BLOCK_RECORD,
    IrisCase,
    mock_llm,
    patch_encryption_env,
)
from odoo.addons.api_auth_gateway.models import access_token as access_token_model
from odoo.addons.iris.models import credential_manager

EVIDENCE = "Reference call with the former manager confirms the claim."


@tagged("post_install", "-at_install", "iris")
class TestBlockSignoff(IrisCase):
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _pending_block_candidate(self, proposer=None, **overrides):
        """Screen a fresh candidate into ``pending_block`` via the LLM path."""
        candidate = self._make_candidate(**overrides)
        proposer = proposer or self.user_iris
        candidate.with_user(proposer).action_screen()
        with mock_llm(self.VALID_BLOCK_RECORD):
            self._run_llm_queue()
        self.assertEqual(candidate.state, "pending_block")
        return candidate

    def _signoff_activities(self, candidate):
        return self.env["mail.activity"].search([
            ("res_model", "=", "iris.candidate"),
            ("res_id", "=", candidate.id),
            ("activity_type_id", "=", self.env.ref(
                "iris.mail_activity_type_block_signoff",
            ).id),
        ])

    def _latest_screening(self, candidate):
        return candidate.screening_ids.sorted("id")[-1]

    # ------------------------------------------------------------------
    # 1. LLM BLOCK proposes — never blocks directly
    # ------------------------------------------------------------------
    def test_llm_block_enters_pending_with_requester_as_proposer(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        screening = self._latest_screening(candidate)

        self.assertEqual(candidate.state, "pending_block")
        self.assertEqual(screening.verdict, "block")
        self.assertFalse(screening.verdict_manual)
        self.assertEqual(screening.llm_status, "done")
        self.assertEqual(screening.block_signoff_state, "pending")
        # The proposer is the user who clicked Screen — NOT the cron user.
        self.assertEqual(screening.block_proposed_by_id, self.user_iris)
        self.assertTrue(screening.block_proposed_at)
        self.assertFalse(screening.block_signed_off_by_id)
        # Fresh BLOCK: no hold episode, deadline untouched (unset).
        self.assertFalse(candidate.hold_deadline)
        # Exactly one sign-off activity was scheduled, to a non-proposer.
        activities = self._signoff_activities(candidate)
        self.assertEqual(len(activities), 1)
        self.assertNotEqual(activities.user_id, self.user_iris)
        bodies = "\n".join(self._chatter_bodies(candidate))
        self.assertIn("second screener", bodies)

    def test_hold_deadline_survives_rescreen_block_until_cosign(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_HOLD_RECORD)
        deadline = candidate.hold_deadline
        self.assertTrue(deadline)

        hold = candidate._get_current_hold_screening()
        hold.sudo().write({"verification_evidence": EVIDENCE})
        candidate.action_rescreen()
        with mock_llm(self.VALID_BLOCK_RECORD):
            self._run_llm_queue()

        # pending_block keeps the (never-extended) deadline ...
        self.assertEqual(candidate.state, "pending_block")
        self.assertEqual(candidate.hold_deadline, deadline)
        # ... and only the confirmed co-sign clears it.
        candidate.with_user(self.user_second)._block_signoff("credibility")
        self.assertEqual(candidate.state, "blocked")
        self.assertFalse(candidate.hold_deadline)

    # ------------------------------------------------------------------
    # 2. Co-sign by a different user
    # ------------------------------------------------------------------
    def test_cosign_by_different_user_blocks_and_records_fields(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        screening = self._latest_screening(candidate)

        candidate.with_user(self.user_second)._block_signoff("credibility")

        self.assertEqual(candidate.state, "blocked")
        self.assertFalse(candidate.hold_deadline)
        self.assertEqual(screening.block_signoff_state, "approved")
        self.assertEqual(screening.block_signed_off_by_id, self.user_second)
        self.assertTrue(screening.block_signed_off_at)
        self.assertEqual(screening.block_kind, "credibility")
        # The sign-off activity was completed.
        self.assertFalse(self._signoff_activities(candidate))
        bodies = "\n".join(self._chatter_bodies(candidate))
        self.assertIn("BLOCK confirmed", bodies)

    # ------------------------------------------------------------------
    # 3-5. Co-sign guards
    # ------------------------------------------------------------------
    def test_proposer_cannot_cosign_own_block(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        with self.assertRaises(UserError):
            candidate.with_user(self.user_iris)._block_signoff("credibility")
        self.assertEqual(candidate.state, "pending_block")
        screening = self._latest_screening(candidate)
        self.assertEqual(screening.block_signoff_state, "pending")

    def test_cosign_requires_pending_block_state(self):
        candidate = self._make_candidate()
        with self.assertRaises(UserError):
            candidate._block_signoff("credibility")
        with self.assertRaises(UserError):
            candidate.action_open_block_signoff_wizard()
        with self.assertRaises(UserError):
            candidate.action_open_block_reject_wizard()
        # Once blocked, a second co-sign is equally refused.
        blocked = self._pending_block_candidate(proposer=self.user_iris)
        blocked.with_user(self.user_second)._block_signoff("competence")
        with self.assertRaises(UserError):
            blocked.with_user(self.user_manager)._block_signoff("competence")

    def test_cosign_requires_a_valid_block_kind(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        with self.assertRaises(UserError):
            candidate.with_user(self.user_second)._block_signoff(False)
        with self.assertRaises(UserError):
            candidate.with_user(self.user_second)._block_signoff("vibes")
        self.assertEqual(candidate.state, "pending_block")

    # ------------------------------------------------------------------
    # 6. Sign-off rejection
    # ------------------------------------------------------------------
    def test_reject_requires_a_reason(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        with self.assertRaises(UserError):
            candidate.with_user(self.user_second)._block_signoff_reject("")
        with self.assertRaises(UserError):
            candidate.with_user(self.user_second)._block_signoff_reject("   ")
        self.assertEqual(candidate.state, "pending_block")

    def test_reject_routes_to_needs_review_with_llm_status(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        screening = self._latest_screening(candidate)

        candidate.with_user(self.user_second)._block_signoff_reject(
            "The B2 date subtraction does not hold up.",
        )

        self.assertEqual(candidate.state, "needs_review")
        self.assertEqual(screening.block_signoff_state, "rejected")
        self.assertEqual(
            screening.block_signoff_rejection_reason,
            "The B2 date subtraction does not hold up.",
        )
        # LOAD-BEARING: _apply_manual_verdict finds the screening by this.
        self.assertEqual(screening.llm_status, "needs_review")
        # Verdict stays on record (audit); the open activity is unlinked.
        self.assertEqual(screening.verdict, "block")
        self.assertFalse(self._signoff_activities(candidate))

    def test_proposer_may_reject_own_pending_block(self):
        # Fail-safe direction: rejection routes to MORE scrutiny.
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        candidate.with_user(self.user_iris)._block_signoff_reject(
            "On reflection the evidence is thinner than I wrote.",
        )
        self.assertEqual(candidate.state, "needs_review")

    # ------------------------------------------------------------------
    # 7. After rejection: manual verdicts re-chain
    # ------------------------------------------------------------------
    def test_after_reject_manual_ship_works(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        candidate.with_user(self.user_second)._block_signoff_reject("re-read it")

        candidate.with_user(self.user_manager).action_manual_verdict_ship()

        screening = self._latest_screening(candidate)
        self.assertEqual(candidate.state, "shipped")
        self.assertEqual(screening.verdict, "ship")
        self.assertTrue(screening.verdict_manual)
        self.assertEqual(screening.llm_status, "done")

    def test_after_reject_manual_block_rechains_with_manager_as_proposer(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        candidate.with_user(self.user_second)._block_signoff_reject("re-read it")

        candidate.with_user(self.user_manager).action_manual_verdict_block()

        screening = self._latest_screening(candidate)
        self.assertEqual(candidate.state, "pending_block")
        self.assertEqual(screening.block_signoff_state, "pending")
        self.assertTrue(screening.verdict_manual)
        # The manager who set the manual verdict is the NEW proposer.
        self.assertEqual(screening.block_proposed_by_id, self.user_manager)
        # The original requester is now a valid co-signer.
        candidate.with_user(self.user_iris)._block_signoff("competence")
        self.assertEqual(candidate.state, "blocked")
        self.assertEqual(screening.block_kind, "competence")

    # ------------------------------------------------------------------
    # 8-9. Cron exemption (structural)
    # ------------------------------------------------------------------
    def test_cron_auto_block_bypasses_signoff(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_HOLD_RECORD)
        candidate.sudo().write({
            "hold_deadline": fields.Date.context_today(candidate)
            - timedelta(days=3),
        })

        self.env["iris.candidate"]._cron_auto_block_expired_holds()

        self.assertEqual(candidate.state, "blocked")
        screening = self._latest_screening(candidate)
        self.assertTrue(screening.auto_blocked)
        # No sign-off chain on the cron path — fields stay at defaults.
        self.assertEqual(screening.block_signoff_state, "none")
        self.assertFalse(screening.block_proposed_by_id)
        self.assertFalse(candidate.hold_deadline)

    def test_cron_ignores_pending_block_with_past_deadline(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_HOLD_RECORD)
        hold = candidate._get_current_hold_screening()
        hold.sudo().write({"verification_evidence": EVIDENCE})
        candidate.action_rescreen()
        with mock_llm(self.VALID_BLOCK_RECORD):
            self._run_llm_queue()
        self.assertEqual(candidate.state, "pending_block")
        candidate.sudo().write({
            "hold_deadline": fields.Date.context_today(candidate)
            - timedelta(days=3),
        })

        self.env["iris.candidate"]._cron_auto_block_expired_holds()

        # Structurally exempt: the cron's domain is state='hold' only.
        self.assertEqual(candidate.state, "pending_block")
        screening = self._latest_screening(candidate)
        self.assertEqual(screening.block_signoff_state, "pending")
        self.assertFalse(screening.auto_blocked)

    # ------------------------------------------------------------------
    # 10. No new screening while pending
    # ------------------------------------------------------------------
    def test_no_screen_or_rescreen_during_pending_block(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        with self.assertRaises(UserError):
            candidate.action_screen()
        with self.assertRaises(UserError):
            candidate.action_rescreen()
        self.assertEqual(len(candidate.screening_ids), 1)
        self.assertEqual(candidate.state, "pending_block")

    # ------------------------------------------------------------------
    # 11. Ship / hold leave the sign-off fields alone
    # ------------------------------------------------------------------
    def test_ship_and_hold_keep_signoff_defaults(self):
        shipped = self._make_candidate(name="Ship Case")
        ship_screening = self._screen(shipped, self.VALID_SHIP_RECORD)
        self.assertEqual(shipped.state, "shipped")
        self.assertEqual(ship_screening.block_signoff_state, "none")
        self.assertFalse(ship_screening.block_proposed_by_id)
        self.assertFalse(ship_screening.block_kind)

        held = self._make_candidate(name="Hold Case")
        hold_screening = self._screen(held, self.VALID_HOLD_RECORD)
        self.assertEqual(held.state, "hold")
        self.assertEqual(hold_screening.block_signoff_state, "none")
        self.assertFalse(hold_screening.block_proposed_by_id)
        self.assertFalse(self._signoff_activities(held))

    # ------------------------------------------------------------------
    # Grandfathering: pre-v1.1 blocked rows are untouched
    # ------------------------------------------------------------------
    def test_grandfathered_blocked_candidate_stays_blocked(self):
        candidate = self._make_candidate(name="Legacy Block")
        candidate.sudo().write({"state": "blocked"})
        self.env["iris.candidate"]._cron_auto_block_expired_holds()
        self.assertEqual(candidate.state, "blocked")
        with self.assertRaises(UserError):
            candidate.with_user(self.user_second)._block_signoff("credibility")

    # ------------------------------------------------------------------
    # Wizards (thin wrappers over the candidate methods)
    # ------------------------------------------------------------------
    def test_open_wizard_actions_shape(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        action = candidate.action_open_block_signoff_wizard()
        self.assertEqual(action["res_model"], "iris.block.signoff.wizard")
        self.assertEqual(action["target"], "new")
        self.assertEqual(
            action["context"]["default_candidate_id"], candidate.id,
        )
        reject_action = candidate.action_open_block_reject_wizard()
        self.assertEqual(reject_action["res_model"], "iris.block.reject.wizard")

    def test_cosign_wizard_confirms_and_posts_note(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        wizard = self.env["iris.block.signoff.wizard"].with_user(
            self.user_second,
        ).create({
            "candidate_id": candidate.id,
            "block_kind": "competence",
            "note": "Checked the role-fit gap myself.",
        })
        result = wizard.action_confirm()
        self.assertEqual(result["type"], "ir.actions.act_window_close")
        self.assertEqual(candidate.state, "blocked")
        screening = self._latest_screening(candidate)
        self.assertEqual(screening.block_kind, "competence")
        bodies = "\n".join(self._chatter_bodies(candidate))
        self.assertIn("Checked the role-fit gap myself.", bodies)

    def test_cosign_wizard_enforces_proposer_guard(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        wizard = self.env["iris.block.signoff.wizard"].with_user(
            self.user_iris,
        ).create({
            "candidate_id": candidate.id,
            "block_kind": "credibility",
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_reject_wizard_confirms_with_reason(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        wizard = self.env["iris.block.reject.wizard"].with_user(
            self.user_second,
        ).create({
            "candidate_id": candidate.id,
            "reason": "Both quotes read as parsing noise.",
        })
        wizard.action_confirm()
        self.assertEqual(candidate.state, "needs_review")
        screening = self._latest_screening(candidate)
        self.assertEqual(
            screening.block_signoff_rejection_reason,
            "Both quotes read as parsing noise.",
        )

    def test_reject_wizard_refuses_whitespace_reason(self):
        candidate = self._pending_block_candidate(proposer=self.user_iris)
        wizard = self.env["iris.block.reject.wizard"].with_user(
            self.user_second,
        ).create({
            "candidate_id": candidate.id,
            "reason": "   ",
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()
        self.assertEqual(candidate.state, "pending_block")


@tagged("post_install", "-at_install", "iris")
class TestBlockSignoffApi(HttpCase):
    """HTTP routes for the dual sign-off (12: API surface).

    Token plumbing mirrors ``test_api_controllers.TestIrisApi``: the
    ``api.access_token`` row is created via ORM (the gateway's HTTP
    issuance route cannot run inside the test transaction); screenings are
    driven to ``pending_block`` in the test thread with ``mock_llm`` + the
    queue cron, then the routes are exercised over real HTTP.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env_patcher, _key = patch_encryption_env()
        cls.addClassCleanup(env_patcher.stop)

        Users = cls.env["res.users"].with_context(no_reset_password=True)
        base_group = cls.env.ref("base.group_user")
        iris_group = cls.env.ref("iris.group_iris_user")
        cls.user_proposer = Users.create({
            "name": "BS Proposer",
            "login": "iris_bs_proposer",
            "email": "iris_bs_proposer@example.com",
            "group_ids": [(6, 0, [base_group.id, iris_group.id])],
        })
        cls.user_second = Users.create({
            "name": "BS Second",
            "login": "iris_bs_second",
            "email": "iris_bs_second@example.com",
            "group_ids": [(6, 0, [base_group.id, iris_group.id])],
        })
        cls.user_plain = Users.create({
            "name": "BS Plain",
            "login": "iris_bs_plain",
            "email": "iris_bs_plain@example.com",
            "group_ids": [(6, 0, [base_group.id])],
        })

        credential_manager.set_encrypted_param(
            cls.env, API_KEY_PARAM, "sk-iris-bs-test",
        )
        connectors = cls.env["s3.connector"].sudo().search([])
        if connectors:
            connectors.unlink()
        cls.env.flush_all()

    def setUp(self):
        super().setUp()
        self.token_proposer = self._get_token("iris_bs_proposer")
        self.token_second = self._get_token("iris_bs_second")
        self.token_plain = self._get_token("iris_bs_plain")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_token(self, login):
        user = self.env["res.users"].sudo().search(
            [("login", "=", login)], limit=1,
        )
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
        headers = {"Content-Type": "application/json"}
        if token:
            headers["access-token"] = token
        data = json.dumps(payload) if payload is not None else None
        self.env.flush_all()
        resp = self.url_open(url, data=data, headers=headers, method=method)
        self.env.invalidate_all()
        return resp

    def _make_candidate(self, name="Block Api"):
        candidate = self.env["iris.candidate"].create({
            "name": name,
            "resume_text": RESUME_TEXT,
        })
        self.env.flush_all()
        return candidate

    def _pending_block_candidate(self, proposer=None, name="Block Api"):
        candidate = self._make_candidate(name=name)
        proposer = proposer or self.user_proposer
        candidate.with_user(proposer).action_screen()
        with mock_llm(VALID_BLOCK_RECORD):
            self.env["iris.candidate"]._cron_process_llm_queue()
        self.env.flush_all()
        self.assertEqual(candidate.state, "pending_block")
        return candidate

    # ------------------------------------------------------------------
    # POST /candidates/<id>/block-signoff
    # ------------------------------------------------------------------
    def test_block_signoff_happy_path(self):
        candidate = self._pending_block_candidate(proposer=self.user_proposer)
        resp = self._request(
            "POST",
            f"/api/v1/iris/candidates/{candidate.id}/block-signoff",
            token=self.token_second,
            payload={"block_kind": "credibility", "note": "double-checked"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["candidate"]["state"], "blocked")
        self.assertEqual(body["screening"]["block_signoff_state"], "approved")
        self.assertEqual(body["screening"]["block_kind"], "credibility")
        self.assertEqual(
            body["screening"]["block_signed_off_by"]["id"],
            self.user_second.id,
        )
        self.assertEqual(candidate.state, "blocked")
        self.assertFalse(candidate.hold_deadline)

    def test_block_signoff_same_user_is_400(self):
        candidate = self._pending_block_candidate(proposer=self.user_second)
        resp = self._request(
            "POST",
            f"/api/v1/iris/candidates/{candidate.id}/block-signoff",
            token=self.token_second,
            payload={"block_kind": "credibility"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(candidate.state, "pending_block")

    def test_block_signoff_invalid_or_missing_kind_is_400(self):
        candidate = self._pending_block_candidate(proposer=self.user_proposer)
        url = f"/api/v1/iris/candidates/{candidate.id}/block-signoff"
        resp = self._request(
            "POST", url, token=self.token_second,
            payload={"block_kind": "vibes"},
        )
        self.assertEqual(resp.status_code, 400)
        resp = self._request("POST", url, token=self.token_second, payload={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(candidate.state, "pending_block")

    def test_block_signoff_wrong_state_is_400(self):
        candidate = self._make_candidate(name="Still Draft")
        resp = self._request(
            "POST",
            f"/api/v1/iris/candidates/{candidate.id}/block-signoff",
            token=self.token_second,
            payload={"block_kind": "credibility"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_block_signoff_requires_iris_group(self):
        candidate = self._pending_block_candidate(proposer=self.user_proposer)
        resp = self._request(
            "POST",
            f"/api/v1/iris/candidates/{candidate.id}/block-signoff",
            token=self.token_plain,
            payload={"block_kind": "credibility"},
        )
        self.assertEqual(resp.status_code, 403)

    # ------------------------------------------------------------------
    # POST /candidates/<id>/block-signoff/reject
    # ------------------------------------------------------------------
    def test_block_reject_happy_path(self):
        candidate = self._pending_block_candidate(proposer=self.user_proposer)
        resp = self._request(
            "POST",
            f"/api/v1/iris/candidates/{candidate.id}/block-signoff/reject",
            token=self.token_second,
            payload={"reason": "Needs a second read of the B2 evidence."},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["candidate"]["state"], "needs_review")
        self.assertEqual(body["screening"]["block_signoff_state"], "rejected")
        self.assertEqual(
            body["screening"]["block_signoff_rejection_reason"],
            "Needs a second read of the B2 evidence.",
        )
        screening = candidate.screening_ids.sorted("id")[-1]
        self.assertEqual(screening.llm_status, "needs_review")

    def test_block_reject_blank_reason_is_400(self):
        candidate = self._pending_block_candidate(proposer=self.user_proposer)
        url = f"/api/v1/iris/candidates/{candidate.id}/block-signoff/reject"
        resp = self._request(
            "POST", url, token=self.token_second, payload={"reason": "   "},
        )
        self.assertEqual(resp.status_code, 400)
        resp = self._request("POST", url, token=self.token_second, payload={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(candidate.state, "pending_block")

    def test_block_reject_wrong_state_is_400(self):
        candidate = self._make_candidate(name="Draft Reject")
        resp = self._request(
            "POST",
            f"/api/v1/iris/candidates/{candidate.id}/block-signoff/reject",
            token=self.token_second,
            payload={"reason": "irrelevant"},
        )
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------------
    # pending_block is a first-class API state
    # ------------------------------------------------------------------
    def test_candidates_list_accepts_pending_block_filter(self):
        candidate = self._pending_block_candidate(proposer=self.user_proposer)
        resp = self._request(
            "GET",
            "/api/v1/iris/candidates?state=pending_block",
            token=self.token_second,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["candidates"]
        self.assertIn(candidate.id, [c["id"] for c in body])
        self.assertTrue(all(c["state"] == "pending_block" for c in body))

    def test_status_endpoint_reports_signoff_state(self):
        candidate = self._pending_block_candidate(proposer=self.user_proposer)
        resp = self._request(
            "GET",
            f"/api/v1/iris/candidates/{candidate.id}/status",
            token=self.token_second,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["state"], "pending_block")
        self.assertEqual(body["block_signoff_state"], "pending")
