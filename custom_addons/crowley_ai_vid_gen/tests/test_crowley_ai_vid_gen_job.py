# -*- coding: utf-8 -*-
import psycopg2

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "crowley_ai_vid_gen")
class TestCrowleyAIVidGenJob(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Job = self.env["crowley.ai.vid.gen.job"]
        self.Attempt = self.env["crowley.ai.vid.gen.attempt"]
        self.ICP = self.env["ir.config_parameter"].sudo()

    def _make_draft(self, **overrides):
        vals = {
            "prompt": "A serene mountain landscape at sunset.",
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
        }
        vals.update(overrides)
        return self.Job.create(vals)

    def test_create_assigns_sequence(self):
        job = self._make_draft()
        self.assertTrue(job.name and job.name != "New",
                        "Sequence should assign a non-default name on create")
        self.assertTrue(job.name.startswith("CVG/"),
                        f"Expected CVG/ prefix, got {job.name!r}")
        self.assertEqual(job.state, "draft")
        self.assertEqual(job.user_id, self.env.user)
        self.assertEqual(job.model, "bytedance/seedance-2.0")
        self.assertTrue(job.generate_audio)
        self.assertEqual(job.attempt_count, 0)
        self.assertFalse(job.active_attempt_id)

    def test_defaults_align_with_recommended_target(self):
        Job = self.Job.with_context(default_company_id=self.env.company.id)
        defaults = Job.default_get(["duration", "resolution", "aspect_ratio", "model"])
        self.assertEqual(defaults["duration"], 15,
                         "Default duration should be 15s (maximum allowed by Seedance 2.0).")
        self.assertEqual(defaults["resolution"], "720p")
        self.assertEqual(defaults["aspect_ratio"], "16:9")
        self.assertEqual(defaults["model"], "bytedance/seedance-2.0")

    def test_system_prompt_prefix_prepended(self):
        """System prompt is silently prepended to whatever prompt the attempt sends."""
        self.ICP.set_param(
            "crowley_ai_vid_gen.system_prompt",
            "Single continuous take, no cuts.",
        )
        job = self._make_draft(prompt="Cat playing piano under neon lights")
        effective = job._build_effective_prompt_for(self.ICP, job.prompt)
        self.assertTrue(
            effective.startswith("Single continuous take, no cuts."),
            "System prompt prefix should appear at the start of the effective prompt.",
        )
        self.assertIn("Cat playing piano under neon lights", effective)

    def test_system_prompt_empty_falls_through(self):
        """An empty/whitespace system prompt yields the user prompt verbatim."""
        self.ICP.set_param("crowley_ai_vid_gen.system_prompt", "   ")
        job = self._make_draft(prompt="bare prompt")
        self.assertEqual(
            job._build_effective_prompt_for(self.ICP, job.prompt),
            "bare prompt",
        )

    def test_system_prompt_user_prompt_trimmed(self):
        """Leading/trailing whitespace on the user prompt is normalised."""
        self.ICP.set_param("crowley_ai_vid_gen.system_prompt", "PREFIX.")
        job = self._make_draft(prompt="  ringed planet  ")
        self.assertEqual(
            job._build_effective_prompt_for(self.ICP, "  ringed planet  "),
            "PREFIX.\n\nringed planet",
        )

    def test_duration_constraint_too_low(self):
        with self.assertRaises(ValidationError):
            self._make_draft(duration=3)

    def test_duration_constraint_too_high(self):
        with self.assertRaises(ValidationError):
            self._make_draft(duration=16)

    def test_duration_constraint_boundaries(self):
        self._make_draft(duration=4)
        self._make_draft(duration=15)

    def test_prompt_constraint_empty(self):
        with self.assertRaises(ValidationError):
            self._make_draft(prompt="   ")

    def test_prompt_constraint_too_long(self):
        with self.assertRaises(ValidationError):
            self._make_draft(prompt="x" * 4001)

    def test_seed_constraint_negative(self):
        with self.assertRaises(ValidationError):
            self._make_draft(seed=-1)

    def test_seed_constraint_too_large(self):
        try:
            with self.env.cr.savepoint():
                self._make_draft(seed=2**31)
        except (ValidationError, psycopg2.errors.NumericValueOutOfRange):
            return
        self.fail("Expected ValidationError or NumericValueOutOfRange for seed=2**31")

    def test_video_play_url_empty_when_not_ready(self):
        job = self._make_draft()
        self.assertFalse(job.video_play_url)

    def test_action_cancel_invalid_in_draft(self):
        job = self._make_draft()
        with self.assertRaises(UserError):
            job.action_cancel()

    def test_action_retry_invalid_in_draft(self):
        job = self._make_draft()
        with self.assertRaises(UserError):
            job.action_retry()

    def test_attachment_count_zero_initially(self):
        job = self._make_draft()
        self.assertEqual(job.attachment_count, 0)

    def test_attachment_count_with_attachments(self):
        job = self._make_draft()
        self.env["ir.attachment"].create({
            "name": "test.mp4",
            "datas": b"AAAA",
            "mimetype": "video/mp4",
            "crowley_job_id": job.id,
        })
        job.invalidate_recordset(["attachment_count"])
        self.assertEqual(job.attachment_count, 1)


@tagged("post_install", "-at_install", "crowley_ai_vid_gen")
class TestCrowleyAttempts(TransactionCase):
    """Multi-attempt refinement workflow: each job can host up to 3 attempts."""

    def setUp(self):
        super().setUp()
        self.Job = self.env["crowley.ai.vid.gen.job"]
        self.Attempt = self.env["crowley.ai.vid.gen.attempt"]

    def _make_draft(self, **overrides):
        vals = {
            "prompt": "A serene mountain landscape at sunset.",
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
        }
        vals.update(overrides)
        return self.Job.create(vals)

    def _seed_attempt(self, job, number, *, state="ready",
                       prompt=None, s3_key=None, cost_usd=0.0):
        """Create a fully-populated attempt row without going through OpenRouter."""
        return self.Attempt.create({
            "job_id": job.id,
            "attempt_number": number,
            "prompt": prompt or job.prompt,
            "seed": job.seed,
            "state": state,
            "s3_bucket": "test-bucket" if state == "ready" else False,
            "s3_key": s3_key or (
                f"crowley-seedance/2026/05/{job.id}/attempt-{number}.mp4"
                if state == "ready" else False
            ),
            "file_size": 1000 if state == "ready" else 0,
            "cost_usd": cost_usd,
            "completed_at": "2026-05-19" if state == "ready" else False,
        })

    def test_active_attempt_prefers_latest_ready(self):
        """The active attempt is the highest-numbered ready attempt."""
        job = self._make_draft()
        self._seed_attempt(job, 1, state="ready", cost_usd=1.0)
        self._seed_attempt(job, 2, state="failed")
        job.invalidate_recordset(["active_attempt_id"])
        self.assertEqual(job.active_attempt_id.attempt_number, 1,
                         "Should fall back to attempt 1 when 2 failed.")

        a3 = self._seed_attempt(job, 3, state="ready", cost_usd=2.0)
        job.invalidate_recordset(["active_attempt_id"])
        self.assertEqual(job.active_attempt_id, a3,
                         "Should prefer attempt 3 once it's ready.")

    def test_active_attempt_falls_back_when_none_ready(self):
        """Without any ready attempt the latest one (whatever state) becomes active."""
        job = self._make_draft()
        a1 = self._seed_attempt(job, 1, state="failed")
        a2 = self._seed_attempt(job, 2, state="polling")
        job.invalidate_recordset(["active_attempt_id"])
        self.assertEqual(job.active_attempt_id, a2,
                         "Latest attempt (whatever state) becomes active.")

    def test_state_proxies_active_attempt(self):
        """Job's state mirrors the active attempt's state."""
        job = self._make_draft()
        self._seed_attempt(job, 1, state="ready")
        job.invalidate_recordset()
        self.assertEqual(job.state, "ready")

    def test_cost_usd_sums_all_attempts(self):
        """cost_usd is the SUM of every attempt — the user paid for all of them."""
        job = self._make_draft()
        self._seed_attempt(job, 1, state="ready", cost_usd=1.50)
        self._seed_attempt(job, 2, state="ready", cost_usd=2.25)
        job.invalidate_recordset(["cost_usd"])
        self.assertAlmostEqual(job.cost_usd, 3.75, places=4)

    def test_action_generate_creates_attempt_one(self):
        """The first action_generate produces attempt #1 in queued state."""
        job = self._make_draft()
        job.action_generate()
        self.assertEqual(job.attempt_count, 1)
        first = job.attempt_ids
        self.assertEqual(first.attempt_number, 1)
        self.assertEqual(first.state, "queued")
        self.assertEqual(first.prompt, job.prompt)

    def test_action_generate_twice_rejected(self):
        """Once attempt #1 exists, action_generate refuses to overwrite — use refine."""
        job = self._make_draft()
        job.action_generate()
        with self.assertRaises(UserError):
            job.action_generate()

    def test_action_refine_creates_next_attempt(self):
        """action_refine creates attempt #2 with the revised prompt."""
        job = self._make_draft()
        self._seed_attempt(job, 1, state="ready")
        job.action_refine(new_prompt="Same landscape, but at dawn instead.")
        self.assertEqual(job.attempt_count, 2)
        self.assertEqual(job.prompt, "Same landscape, but at dawn instead.")
        attempt2 = job.attempt_ids.filtered(lambda a: a.attempt_number == 2)
        self.assertEqual(attempt2.prompt, "Same landscape, but at dawn instead.")
        self.assertEqual(attempt2.state, "queued")

    def test_action_refine_caps_at_three(self):
        """A fourth refine raises UserError — the spec is hard-capped at 3."""
        job = self._make_draft()
        for n in (1, 2, 3):
            self._seed_attempt(job, n, state="ready",
                              prompt=f"prompt {n}")
        with self.assertRaises(UserError):
            job.action_refine(new_prompt="never gonna happen")

    def test_action_refine_blocks_while_in_flight(self):
        """Cannot refine while a prior attempt is still being generated."""
        job = self._make_draft()
        self._seed_attempt(job, 1, state="polling")
        with self.assertRaises(UserError):
            job.action_refine(new_prompt="too soon")

    def test_action_refine_empty_prompt_rejected(self):
        job = self._make_draft()
        self._seed_attempt(job, 1, state="ready")
        with self.assertRaises(UserError):
            job.action_refine(new_prompt="   ")

    def test_seed_carried_across_attempts(self):
        """The same seed is reused across every attempt of one job."""
        job = self._make_draft(seed=12345)
        job.action_generate()
        first = job.attempt_ids
        self.assertEqual(first.seed, 12345)
        first.write({"state": "ready"})
        job.action_refine(new_prompt="revised")
        second = job.attempt_ids.filtered(lambda a: a.attempt_number == 2)
        self.assertEqual(second.seed, 12345,
                         "Seed must NOT change between attempts — only the prompt does.")

    def test_attempt_state_independent_of_siblings(self):
        """Attempts have independent state machines."""
        job = self._make_draft()
        a1 = self._seed_attempt(job, 1, state="failed")
        a2 = self._seed_attempt(job, 2, state="ready")
        self.assertEqual(a1.state, "failed")
        self.assertEqual(a2.state, "ready")

    def test_s3_keys_same_folder_across_attempts(self):
        """Spec: all attempts of one job live in the same S3 folder."""
        job = self._make_draft()
        a1 = self._seed_attempt(
            job, 1, state="ready",
            s3_key=f"crowley-seedance/2026/05/{job.id}/abc.mp4",
        )
        a2 = self._seed_attempt(
            job, 2, state="ready",
            s3_key=f"crowley-seedance/2026/05/{job.id}/def.mp4",
        )
        prefix_a1 = a1.s3_key.rsplit("/", 1)[0]
        prefix_a2 = a2.s3_key.rsplit("/", 1)[0]
        self.assertEqual(prefix_a1, prefix_a2,
                         "Both attempts must share the same S3 folder per spec.")

    def test_cron_points_at_attempt_model(self):
        """Regression guard: cron must invoke the attempt model's poller.

        v1.5→v1.6 hit production with the cron's ``model_id`` still
        pointing at the (now method-less) job model. This test fails
        loudly if a future refactor recreates that drift.
        """
        cron = self.env.ref(
            "crowley_ai_vid_gen.cron_crowley_poll_openrouter",
            raise_if_not_found=False,
        )
        self.assertTrue(cron, "Polling cron record must exist after install.")
        server_action = cron.ir_actions_server_id
        self.assertEqual(
            server_action.model_id.model,
            "crowley.ai.vid.gen.attempt",
            "Cron must dispatch _cron_poll_openrouter on the attempt model, "
            "not the job model — see Oracle review A1.",
        )
        self.assertIn("_cron_poll_openrouter", server_action.code or "")
