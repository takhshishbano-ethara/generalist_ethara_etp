"""Crowley v1.5 duplicate-prompt prevention tests.

Covers _normalize_prompt_text, _check_duplicate_prompts, the onchange surface,
and the allow_duplicate manager override across cross-job and cross-user paths.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "crowley")
class TestDuplicatePrompt(TransactionCase):

    def _make_job(self, **vals):
        defaults = {
            "prompt": "A cat surfing in slow motion",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        }
        defaults.update(vals)
        return self.env["crowley.generation"].create(defaults)

    def _spawn_attempt(self, job, *, state, attempt_number=1, prompt=None, original_prompt=None):
        """Create an attempt and force its state via SQL (bypassing the state machine guard)."""
        vals = {
            "job_id": job.id,
            "attempt_number": attempt_number,
            "prompt": prompt if prompt is not None else job.prompt,
            "duration": job.duration,
            "resolution": job.resolution,
            "aspect_ratio": job.aspect_ratio,
            "state": "draft",
        }
        if original_prompt is not None:
            vals["original_prompt"] = original_prompt
        attempt = self.env["crowley.attempt"].create(vals)
        if state != "draft":
            self.env.cr.execute(
                "UPDATE crowley_attempt SET state = %s WHERE id = %s",
                (state, attempt.id),
            )
            attempt.invalidate_recordset()
        return attempt

    # ------------------------------------------------------------------
    # Normalization unit tests
    # ------------------------------------------------------------------
    def test_normalize_whitespace_and_case(self):
        Attempt = self.env["crowley.attempt"]
        self.assertEqual(
            Attempt._normalize_prompt_text("  Hello   World  "),
            "hello world",
        )
        self.assertEqual(
            Attempt._normalize_prompt_text("Hello\n\tWorld"),
            "hello world",
        )
        self.assertEqual(
            Attempt._normalize_prompt_text("MIXED   Case\tInput"),
            "mixed case input",
        )

    def test_normalize_empty_returns_false(self):
        Attempt = self.env["crowley.attempt"]
        self.assertFalse(Attempt._normalize_prompt_text(""))
        self.assertFalse(Attempt._normalize_prompt_text("   "))
        self.assertFalse(Attempt._normalize_prompt_text(None))
        self.assertFalse(Attempt._normalize_prompt_text("\t\n"))
        self.assertFalse(Attempt._normalize_prompt_text(False))

    # ------------------------------------------------------------------
    # Core duplicate detection
    # ------------------------------------------------------------------
    def test_prompt_duplicate_blocks(self):
        job_a = self._make_job(prompt="Star Wars yellow crawl over starfield")
        self._spawn_attempt(job_a, state="done")
        job_b = self._make_job(prompt="Star Wars yellow crawl over starfield")
        with self.assertRaises(UserError) as ctx:
            job_b._check_duplicate_prompts()
        self.assertIn("Duplicate prompt detected", str(ctx.exception))
        self.assertIn(job_a.name, str(ctx.exception))

    def test_original_prompt_duplicate_blocks(self):
        job_a = self._make_job(
            prompt="Enriched version A",
            original_prompt="A penguin tap dancing on ice",
        )
        self._spawn_attempt(
            job_a, state="done",
            original_prompt="A penguin tap dancing on ice",
        )
        job_b = self._make_job(
            prompt="Different enriched version B",
            original_prompt="A penguin tap dancing on ice",
        )
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    def test_cross_user_blocks(self):
        user_group = self.env.ref("crowley.group_crowley_user")
        alice = self.env["res.users"].create({
            "name": "Alice Crowley",
            "login": "alice_dup_test",
            "group_ids": [(4, user_group.id)],
        })
        bob = self.env["res.users"].create({
            "name": "Bob Crowley",
            "login": "bob_dup_test",
            "group_ids": [(4, user_group.id)],
        })
        job_a = self._make_job(prompt="zebra under aurora borealis", user_id=alice.id)
        self._spawn_attempt(job_a, state="done")
        job_b = self.env["crowley.generation"].with_user(bob).create({
            "prompt": "zebra under aurora borealis",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        })
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    def test_whitespace_difference_matches(self):
        job_a = self._make_job(prompt="tap dancing penguin on ice")
        self._spawn_attempt(job_a, state="done")
        job_b = self._make_job(prompt="  tap   dancing\tpenguin    on   ice  ")
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    def test_case_difference_matches(self):
        job_a = self._make_job(prompt="A Cat Dancing")
        self._spawn_attempt(job_a, state="done")
        job_b = self._make_job(prompt="a cat dancing")
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    def test_new_prompt_passes(self):
        job_a = self._make_job(prompt="ocean sunset over jagged cliffs")
        self._spawn_attempt(job_a, state="done")
        job_b = self._make_job(prompt="winter forest with falling snow at twilight")
        job_b._check_duplicate_prompts()

    # ------------------------------------------------------------------
    # In-flight states (race condition) — must also block.
    # If user A's video is still being generated when user B submits the
    # same prompt, B must be blocked to prevent two-videos-one-prompt.
    # ------------------------------------------------------------------
    def test_blocks_when_prior_is_queued(self):
        job_a = self._make_job(prompt="meteor shower over open ocean at midnight")
        self._spawn_attempt(job_a, state="queued")
        job_b = self._make_job(prompt="meteor shower over open ocean at midnight")
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    def test_blocks_when_prior_is_submitting(self):
        job_a = self._make_job(prompt="hot air balloons rising at sunrise over hills")
        self._spawn_attempt(job_a, state="submitting")
        job_b = self._make_job(prompt="hot air balloons rising at sunrise over hills")
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    def test_blocks_when_prior_is_processing(self):
        job_a = self._make_job(prompt="koi pond reflecting cherry blossoms in spring")
        self._spawn_attempt(job_a, state="processing")
        job_b = self._make_job(prompt="koi pond reflecting cherry blossoms in spring")
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    def test_blocks_when_prior_is_downloading(self):
        job_a = self._make_job(prompt="northern lights dancing over a frozen lake")
        self._spawn_attempt(job_a, state="downloading")
        job_b = self._make_job(prompt="northern lights dancing over a frozen lake")
        with self.assertRaises(UserError):
            job_b._check_duplicate_prompts()

    # ------------------------------------------------------------------
    # State filter — failed/cancelled attempts must NOT pollute the dup space
    # ------------------------------------------------------------------
    def test_failed_attempt_does_not_block(self):
        job_a = self._make_job(prompt="abandoned warehouse fire scene")
        self._spawn_attempt(job_a, state="failed")
        job_b = self._make_job(prompt="abandoned warehouse fire scene")
        job_b._check_duplicate_prompts()

    def test_cancelled_attempt_does_not_block(self):
        job_a = self._make_job(prompt="rainy night neon city street")
        self._spawn_attempt(job_a, state="cancelled")
        job_b = self._make_job(prompt="rainy night neon city street")
        job_b._check_duplicate_prompts()

    # ------------------------------------------------------------------
    # Manager override
    # ------------------------------------------------------------------
    def test_allow_duplicate_bypasses_check(self):
        job_a = self._make_job(prompt="dragon flying over mountain castle")
        self._spawn_attempt(job_a, state="done")
        job_b = self._make_job(
            prompt="dragon flying over mountain castle",
            allow_duplicate=True,
        )
        job_b._check_duplicate_prompts()

    # ------------------------------------------------------------------
    # onchange surface — soft warning, not exception
    # ------------------------------------------------------------------
    def test_onchange_returns_warning_dict(self):
        job_a = self._make_job(prompt="lighthouse at dusk with crashing waves")
        self._spawn_attempt(job_a, state="done")
        job_b = self._make_job(prompt="placeholder before the user edits")
        job_b.prompt = "lighthouse at dusk with crashing waves"
        result = job_b._onchange_prompts_dup_warning()
        self.assertIsNotNone(result)
        self.assertIn("warning", result)
        self.assertEqual(result["warning"]["title"], "Duplicate Prompt")
        self.assertIn("Duplicate prompt detected", result["warning"]["message"])

    def test_onchange_on_unique_prompt_returns_none(self):
        job = self._make_job(prompt="totally unique brand-new prompt zzzqqxxyy")
        result = job._onchange_prompts_dup_warning()
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Field-level ACL — allow_duplicate has groups="crowley.group_crowley_manager"
    # so non-managers cannot read it directly. _check_duplicate_prompts and
    # _onchange_prompts_dup_warning must use self.sudo().allow_duplicate to
    # bypass this for internal flow control, without exposing the field
    # (view + ORM keep it invisible to non-managers).
    # ------------------------------------------------------------------
    def test_non_manager_onchange_no_access_error(self):
        user_group = self.env.ref("crowley.group_crowley_user")
        bob = self.env["res.users"].create({
            "name": "Bob NonManager",
            "login": "bob_nonmgr_acl_test",
            "group_ids": [(4, user_group.id)],
        })
        job_a = self._make_job(prompt="solar flare erupting on the sun")
        self._spawn_attempt(job_a, state="done")
        job_b = self.env["crowley.generation"].with_user(bob).create({
            "prompt": "placeholder before user edits",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        })
        job_b.prompt = "solar flare erupting on the sun"
        result = job_b._onchange_prompts_dup_warning()
        self.assertIsNotNone(result, "Onchange should return warning dict on dup")
        self.assertIn("warning", result)
        self.assertEqual(result["warning"]["title"], "Duplicate Prompt")

    def test_non_manager_check_duplicate_no_access_error(self):
        user_group = self.env.ref("crowley.group_crowley_user")
        carol = self.env["res.users"].create({
            "name": "Carol NonManager",
            "login": "carol_nonmgr_acl_test",
            "group_ids": [(4, user_group.id)],
        })
        job_a = self._make_job(prompt="lava flow over volcanic cliffs at dawn")
        self._spawn_attempt(job_a, state="done")
        job_b = self.env["crowley.generation"].with_user(carol).create({
            "prompt": "lava flow over volcanic cliffs at dawn",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        })
        with self.assertRaises(UserError) as ctx:
            job_b._check_duplicate_prompts()
        self.assertIn("Duplicate prompt detected", str(ctx.exception))

    # ------------------------------------------------------------------
    # End-to-end: _validate_can_submit chains _check_duplicate_prompts
    # ------------------------------------------------------------------
    def test_validate_can_submit_runs_dup_check(self):
        job_a = self._make_job(prompt="aurora borealis time-lapse footage")
        self._spawn_attempt(job_a, state="done")
        job_b = self._make_job(
            prompt="aurora borealis time-lapse footage",
            category="nature_weather",
        )
        self.env["ir.config_parameter"].sudo().set_param("crowley.s3_connector_id", "1")
        with patch(
            "odoo.addons.crowley.models.crowley_generation.credential_manager.get_openrouter_api_key",
            return_value="sk-test-key-xyz",
        ):
            with self.assertRaises(UserError) as ctx:
                job_b._validate_can_submit()
        self.assertIn("Duplicate prompt detected", str(ctx.exception))
