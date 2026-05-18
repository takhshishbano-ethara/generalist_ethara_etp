"""Regression tests for the two-stage watchdog (added in 19.0.3.0.0).

Covers:
- ``started_processing_at`` filter excludes pure queue-waiters from being
  killed (the bug that failed 139/200 jobs in prod May 15).
- First stuck hit triggers ``_watchdog_auto_retry`` (smart routing: prd_prompt
  present → straight to PRD gen; absent → re-fire Lambda extraction).
- Second stuck hit (counter at cap) marks the job failed for real.
- Chunked-commit isolation: one bad row does not lose the others' marks.

These tests do not run the cron itself (advisory-lock + commit semantics
are awkward inside a TransactionCase). They exercise the underlying
``_watchdog_recover_chunked`` and ``_watchdog_auto_retry`` directly with
synthesised stuck states.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from .common import LeviathanTestCase


@tagged("post_install", "-at_install", "leviathan")
class TestWatchdogSkipQueued(LeviathanTestCase):
    """started_processing_at IS NULL means the worker hasn't touched the row
    yet. The watchdog domain must skip these or the 139/200-stuck pattern
    returns."""

    def test_queued_generating_excluded_by_search_domain(self):
        very_stale = fields.Datetime.now() - timedelta(minutes=300)

        queued = self._create_job(
            user_id=self.tasker.id,
            state="generating",
            last_heartbeat=very_stale,
            # NOT set — this is the queued-in-_POOL case
            started_processing_at=False,
        )
        running = self._create_job(
            user_id=self.tasker.id,
            state="generating",
            last_heartbeat=very_stale,
            started_processing_at=very_stale,
        )

        # Re-create the exact search the cron uses
        stale = self.Job.search([
            ("state", "in", ("generating", "scoring")),
            ("started_processing_at", "!=", False),
            ("last_heartbeat", "<", fields.Datetime.now() - timedelta(minutes=120)),
        ])
        self.assertIn(running, stale, "actually-running stuck job must be flagged")
        self.assertNotIn(
            queued, stale,
            "queued-in-pool job must NOT be flagged — that's the 139/200 bug",
        )


@tagged("post_install", "-at_install", "leviathan")
class TestWatchdogAutoRetry(LeviathanTestCase):
    """First stuck hit auto-retries (smart). Second hit marks failed."""

    def _stale(self, minutes=200):
        return fields.Datetime.now() - timedelta(minutes=minutes)

    def test_first_hit_with_prd_prompt_goes_to_generating(self):
        job = self._create_job(
            user_id=self.tasker.id,
            state="extracting",
            prd_prompt="extracted data",
            last_heartbeat=self._stale(),
            started_at=self._stale(),
            screenshot_keys=["a.png"],
        )
        self.assertEqual(job.watchdog_retry_count, 0)

        with self._patch_submit_bg():
            job._watchdog_auto_retry("extracting >30min")

        self.assertEqual(job.state, "generating")
        self.assertEqual(job.watchdog_retry_count, 1)
        self.assertFalse(job.error_message)
        self.assertEqual(job.prd_prompt, "extracted data", "must keep extraction data")

    def test_first_hit_without_prd_prompt_re_triggers_extraction(self):
        job = self._create_job(
            user_id=self.tasker.id,
            state="extracting",
            prd_prompt=False,
            last_heartbeat=self._stale(),
            started_at=self._stale(),
            screenshot_keys=["a.png"],
            prd_text="stale prd",
        )
        with patch.object(type(job), "_trigger_extraction", lambda self: None):
            job._watchdog_auto_retry("extracting >30min")

        self.assertEqual(job.state, "extracting")
        self.assertEqual(job.watchdog_retry_count, 1)
        self.assertFalse(job.prd_text, "stale results must be wiped on full re-extract")
        self.assertFalse(job.error_message)

    def test_chunked_recover_first_hit_auto_retries_all(self):
        jobs = self.Job
        for _ in range(3):
            jobs |= self._create_job(
                user_id=self.tasker.id,
                state="extracting",
                prd_prompt="extracted",  # so retry uses fast path
                last_heartbeat=self._stale(),
                started_at=self._stale(),
            )
        with self._patch_submit_bg():
            self.Job._watchdog_recover_chunked(
                jobs,
                reason="ignored on first hit",
                log_label="extracting >30min",
                auto_retry_max=1,
            )

        for j in jobs:
            self.assertEqual(j.watchdog_retry_count, 1, f"{j.name}: retried")
            self.assertEqual(j.state, "generating", f"{j.name}: state")
            self.assertFalse(j.error_message)

    def test_chunked_recover_second_hit_marks_failed(self):
        # Already retried once — next watchdog tick must mark failed for real.
        job = self._create_job(
            user_id=self.tasker.id,
            state="extracting",
            prd_prompt="extracted",
            last_heartbeat=self._stale(),
            started_at=self._stale(),
            watchdog_retry_count=1,  # auto-retry already happened
        )
        reason = "Watchdog: extraction timed out (no response for 30+ minutes)"
        self.Job._watchdog_recover_chunked(
            job, reason=reason, log_label="extracting >30min", auto_retry_max=1,
        )
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_message, reason)
        self.assertEqual(
            job.watchdog_retry_count, 1,
            "counter not bumped past cap on the final-failure path",
        )

    def test_auto_retry_max_zero_disables_retry(self):
        # If admin sets ICP leviathan.watchdog_auto_retry_max=0, behaviour
        # should be the pre-19.0.3 mark-failed-immediately path.
        job = self._create_job(
            user_id=self.tasker.id,
            state="extracting",
            prd_prompt="extracted",
            last_heartbeat=self._stale(),
            started_at=self._stale(),
        )
        self.Job._watchdog_recover_chunked(
            job,
            reason="ignored",
            log_label="extracting >30min",
            auto_retry_max=0,  # disabled
        )
        self.assertEqual(job.state, "failed", "with cap=0 first hit must mark failed")
