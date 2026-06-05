# -*- coding: utf-8 -*-
"""TransactionCase tests for the RabbitMQ pipeline integration on t2av.generation.

Covers:
- pipeline_status field schema (default, selection values, timestamps)
- action_batch_publish_pipeline validation gates
- action_retry_pipeline validation + retry counter increment
- run_pipeline_sync ACL guard (group_t2av_consumer required)
- run_pipeline_sync idempotent no-op when pipeline_status not in (queued, failed)
- run_pipeline_sync raises UserError 'Permanent failure: ...' for missing prompt/category
- _cron_watchdog_pipeline resets stale running records
- End-to-end pipeline orchestration with mocked sub-steps
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _job_defaults():
    return {
        "prompt": "A cat surfing in the ocean",
        "category": "human_activities",
        "duration": "5",
        "resolution": "720p",
        "aspect_ratio": "16:9",
    }


class _NoCommitMixin:
    """Production code calls cr.commit() before publishing to RabbitMQ so the
    consumer process sees the pipeline_status='queued' write under PG MVCC.
    Odoo's TransactionCase forbids cr.commit/rollback inside tests, so we
    swap commit for a per-env flush + skip the real commit. The flush must
    span ALL environments sharing this cursor (production code runs under
    with_user(bot) so its writes live in the bot env, not the test env)."""

    def setUp(self):
        super().setUp()
        env = self.env
        cr = env.cr
        original_commit = cr.commit

        def flush_no_commit(*a, **kw):
            env.flush_all()

        cr.commit = flush_no_commit
        self.addCleanup(setattr, cr, "commit", original_commit)


@tagged("post_install", "-at_install", "t2av", "t2av_pipeline")
class TestPipelineSchema(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["t2av.generation"]

    def _make_job(self, **overrides):
        vals = _job_defaults()
        vals.update(overrides)
        return self.Job.create(vals)

    def test_pipeline_status_default_is_not_published(self):
        job = self._make_job()
        self.assertEqual(job.pipeline_status, "not_published")

    def test_pipeline_status_selection_values(self):
        sel = dict(self.Job._fields["pipeline_status"].selection)
        self.assertEqual(
            set(sel.keys()),
            {"not_published", "queued", "running", "done", "failed"},
        )

    def test_pipeline_retry_count_starts_zero(self):
        job = self._make_job()
        self.assertEqual(job.pipeline_retry_count, 0)

    def test_pipeline_timestamps_unset(self):
        job = self._make_job()
        self.assertFalse(job.pipeline_published_at)
        self.assertFalse(job.pipeline_started_at)
        self.assertFalse(job.pipeline_finished_at)
        self.assertFalse(job.pipeline_error_text)


@tagged("post_install", "-at_install", "t2av", "t2av_pipeline")
class TestBatchPublishValidation(_NoCommitMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["t2av.generation"]

    def _make_job(self, **overrides):
        vals = _job_defaults()
        vals.update(overrides)
        return self.Job.create(vals)

    def test_empty_active_ids_raises(self):
        with self.assertRaises(UserError):
            self.Job.with_context(active_ids=[]).action_batch_publish_pipeline()

    def test_no_prompt_raises(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET prompt='' WHERE id=%s", (job.id,)
        )
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job.with_context(active_ids=job.ids).action_batch_publish_pipeline()

    def test_no_category_raises(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET category=NULL WHERE id=%s", (job.id,)
        )
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job.with_context(active_ids=job.ids).action_batch_publish_pipeline()

    def test_non_draft_state_raises(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET state='processing' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job.with_context(active_ids=job.ids).action_batch_publish_pipeline()

    def test_non_publishable_pipeline_status_raises(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='running' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job.with_context(active_ids=job.ids).action_batch_publish_pipeline()

    def test_success_path_updates_status_and_publishes(self):
        job = self._make_job()
        with patch(
            "odoo.addons.t2av.services.rabbitmq_service.batch_publish_pipeline_tasks",
            return_value=1,
        ) as fake_publish:
            result = job.with_context(active_ids=job.ids).action_batch_publish_pipeline()
        fake_publish.assert_called_once()
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "queued")
        self.assertTrue(job.pipeline_published_at)
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("type"), "ir.actions.client")

    def test_failed_records_publishable_again(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='failed' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        with patch(
            "odoo.addons.t2av.services.rabbitmq_service.batch_publish_pipeline_tasks",
            return_value=1,
        ):
            job.with_context(active_ids=job.ids).action_batch_publish_pipeline()
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "queued")


@tagged("post_install", "-at_install", "t2av", "t2av_pipeline")
class TestRetryPipeline(_NoCommitMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["t2av.generation"]

    def _make_job(self, **overrides):
        vals = _job_defaults()
        vals.update(overrides)
        return self.Job.create(vals)

    def test_requires_failed_or_not_published(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='running' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job.action_retry_pipeline()

    def test_requires_state_draft(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='failed', state='processing' "
            "WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job.action_retry_pipeline()

    def test_increments_retry_count_and_publishes(self):
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='failed' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        initial_count = job.pipeline_retry_count
        with patch(
            "odoo.addons.t2av.services.rabbitmq_service.publish_pipeline_task",
            return_value=None,
        ) as fake_publish:
            job.action_retry_pipeline()
        fake_publish.assert_called_once()
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "queued")
        self.assertEqual(job.pipeline_retry_count, initial_count + 1)


@tagged("post_install", "-at_install", "t2av", "t2av_pipeline")
class TestRunPipelineSync(_NoCommitMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["t2av.generation"]
        cls.consumer_group = cls.env.ref("t2av.group_t2av_consumer")

    def _make_job(self, **overrides):
        vals = _job_defaults()
        vals.update(overrides)
        return self.Job.create(vals)

    def _consumer_user(self):
        user = self.env["res.users"].search(
            [("login", "=", "t2av_test_consumer_bot")], limit=1,
        )
        if not user:
            user = self.env["res.users"].create({
                "name": "T2AV Test Consumer Bot",
                "login": "t2av_test_consumer_bot",
                "password": "test_password",
                "group_ids": [(6, 0, [self.consumer_group.id])],
            })
        return user

    def _plain_user(self):
        return self.env["res.users"].create({
            "name": "Plain User",
            "login": "t2av_test_plain_user_%d" % self.env.cr.now().microsecond,
            "password": "pwd",
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })

    def test_non_consumer_user_blocked(self):
        plain_user = self._plain_user()
        job = self._make_job()
        with self.assertRaises(AccessError):
            self.Job.with_user(plain_user).run_pipeline_sync(job.id)

    def test_nonexistent_record_raises_permanent(self):
        bot = self._consumer_user()
        with self.assertRaises(UserError) as ctx:
            self.Job.with_user(bot).run_pipeline_sync(999999999)
        msg = str(ctx.exception).lower()
        self.assertIn("permanent failure", msg)
        self.assertIn("record does not exist", msg)

    def test_already_running_is_no_op(self):
        bot = self._consumer_user()
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='running' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        result = self.Job.with_user(bot).run_pipeline_sync(job.id)
        self.assertEqual(result, True)
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "running")

    def test_already_done_is_no_op(self):
        bot = self._consumer_user()
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='done' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        result = self.Job.with_user(bot).run_pipeline_sync(job.id)
        self.assertEqual(result, True)
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "done")

    def test_already_not_published_is_no_op(self):
        bot = self._consumer_user()
        job = self._make_job()
        result = self.Job.with_user(bot).run_pipeline_sync(job.id)
        self.assertEqual(result, True)
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "not_published")

    def test_missing_prompt_raises_permanent(self):
        # TransactionCase.assertRaises wraps the with-block in a cr.savepoint()
        # that rolls back on the expected exception, undoing the pipeline_status
        # write we're trying to verify. Use plain try/except so the savepoint
        # is not created and the failure-state write survives.
        consumer_group = self.env.ref("t2av.group_t2av_consumer")
        self.env.user.write({"group_ids": [(4, consumer_group.id)]})
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='queued', prompt='' "
            "WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        raised = None
        try:
            self.Job.run_pipeline_sync(job.id)
        except UserError as exc:
            raised = exc
        self.assertIsNotNone(raised, "expected UserError was not raised")
        self.assertIn("permanent failure", str(raised).lower())
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT pipeline_status, pipeline_error_text "
            "FROM t2av_generation WHERE id=%s",
            (job.id,),
        )
        status, error_text = self.env.cr.fetchone()
        self.assertEqual(status, "failed")
        self.assertIn("prompt", (error_text or "").lower())

    def test_missing_category_raises_permanent(self):
        consumer_group = self.env.ref("t2av.group_t2av_consumer")
        self.env.user.write({"group_ids": [(4, consumer_group.id)]})
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='queued', category=NULL "
            "WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        raised = None
        try:
            self.Job.run_pipeline_sync(job.id)
        except UserError as exc:
            raised = exc
        self.assertIsNotNone(raised, "expected UserError was not raised")
        self.assertIn("permanent failure", str(raised).lower())
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT pipeline_status FROM t2av_generation WHERE id=%s",
            (job.id,),
        )
        (status,) = self.env.cr.fetchone()
        self.assertEqual(status, "failed")


@tagged("post_install", "-at_install", "t2av", "t2av_pipeline")
class TestWatchdogPipeline(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["t2av.generation"]

    def _make_job(self, **overrides):
        vals = _job_defaults()
        vals.update(overrides)
        return self.Job.create(vals)

    def test_stale_running_record_reset_to_failed(self):
        job = self._make_job()
        self.env["ir.config_parameter"].sudo().set_param(
            "t2av.pipeline.watchdog_stale_seconds", "1",
        )
        stale = datetime.utcnow() - timedelta(seconds=60)
        self.env.cr.execute(
            "UPDATE t2av_generation "
            "SET pipeline_status='running', pipeline_started_at=%s "
            "WHERE id=%s",
            (stale, job.id),
        )
        job.invalidate_recordset()
        self.Job._cron_watchdog_pipeline()
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "failed")
        self.assertIn("watchdog", (job.pipeline_error_text or "").lower())
        self.assertTrue(job.pipeline_finished_at)

    def test_recent_running_record_not_touched(self):
        job = self._make_job()
        self.env["ir.config_parameter"].sudo().set_param(
            "t2av.pipeline.watchdog_stale_seconds", "900",
        )
        self.env.cr.execute(
            "UPDATE t2av_generation "
            "SET pipeline_status='running', pipeline_started_at=NOW() "
            "WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        self.Job._cron_watchdog_pipeline()
        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "running")
        self.assertFalse(job.pipeline_error_text)

    def test_non_running_records_not_touched(self):
        job_queued = self._make_job()
        job_done = self._make_job()
        very_old = datetime.utcnow() - timedelta(days=1)
        self.env.cr.execute(
            "UPDATE t2av_generation "
            "SET pipeline_status='queued', pipeline_started_at=%s WHERE id=%s",
            (very_old, job_queued.id),
        )
        self.env.cr.execute(
            "UPDATE t2av_generation "
            "SET pipeline_status='done', pipeline_started_at=%s WHERE id=%s",
            (very_old, job_done.id),
        )
        self.Job._cron_watchdog_pipeline()
        job_queued.invalidate_recordset()
        job_done.invalidate_recordset()
        self.assertEqual(job_queued.pipeline_status, "queued")
        self.assertEqual(job_done.pipeline_status, "done")


@tagged("post_install", "-at_install", "t2av", "t2av_pipeline")
class TestPipelineE2EWithMocks(_NoCommitMixin, TransactionCase):
    """End-to-end orchestration test with all I/O boundaries stubbed via SQL."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["t2av.generation"]
        cls.Attempt = cls.env["t2av.attempt"]
        cls.consumer_group = cls.env.ref("t2av.group_t2av_consumer")

    def _make_job(self, **overrides):
        vals = _job_defaults()
        vals.update(overrides)
        return self.Job.create(vals)

    def _consumer_user(self):
        user = self.env["res.users"].search(
            [("login", "=", "t2av_test_consumer_bot")], limit=1,
        )
        if not user:
            user = self.env["res.users"].create({
                "name": "T2AV Test Consumer Bot",
                "login": "t2av_test_consumer_bot",
                "password": "test_password",
                "group_ids": [(6, 0, [self.consumer_group.id])],
            })
        return user

    def test_pipeline_orchestration_done(self):
        bot = self._consumer_user()
        job = self._make_job()
        self.env.cr.execute(
            "UPDATE t2av_generation SET pipeline_status='queued' WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()

        env = self.env

        def fake_run_enrichment(self_gen, record):
            env.cr.execute(
                "UPDATE t2av_generation "
                "SET enriched_prompt=%s, golden_prompt=%s, golden_source='llm' "
                "WHERE id=%s",
                ("Enriched: " + record.prompt, "Golden: " + record.prompt, record.id),
            )
            record.invalidate_recordset()

        def fake_poll_until_terminal(self_gen, attempt):
            env.cr.execute(
                "UPDATE t2av_attempt SET state='done' WHERE id=%s",
                (attempt.id,),
            )
            attempt.invalidate_recordset()

        def fake_run_submit(self_attempt):
            env.cr.execute(
                "UPDATE t2av_attempt SET state='processing' WHERE id=%s",
                (self_attempt.id,),
            )
            self_attempt.invalidate_recordset()

        Job = type(self.Job)
        Attempt = type(self.Attempt)
        with patch.object(
            Job, "_pipeline_run_enrichment",
            side_effect=fake_run_enrichment, autospec=True,
        ), patch.object(
            Job, "_pipeline_poll_until_terminal",
            side_effect=fake_poll_until_terminal, autospec=True,
        ), patch.object(
            Attempt, "_run_submit",
            side_effect=fake_run_submit, autospec=True,
        ):
            self.Job.with_user(bot).run_pipeline_sync(job.id)

        job.invalidate_recordset()
        self.assertEqual(job.pipeline_status, "done")
        self.assertTrue(job.pipeline_started_at)
        self.assertTrue(job.pipeline_finished_at)
        self.assertEqual(len(job.attempt_ids), 1)


@tagged("post_install", "-at_install", "t2av", "t2av_pipeline")
class TestConsumerBotManagerAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["t2av.generation"]
        cls.Enrichment = cls.env["t2av.enrichment"]

    def test_bot_user_implies_manager_group(self):
        bot = self.env.ref("t2av.user_t2av_consumer_bot")
        self.assertTrue(
            bot.has_group("t2av.group_t2av_manager"),
            "group_t2av_consumer.implied_ids must include group_t2av_manager "
            "(security/t2av_consumer_security.xml + "
            "migrations/19.0.1.18.6/post-migration.py).",
        )

    def test_bot_can_write_manager_gated_model_id_on_enrichment(self):
        bot = self.env.ref("t2av.user_t2av_consumer_bot")
        job = self.Job.sudo().create(_job_defaults())
        enrichment = self.Enrichment.sudo().create({
            "job_id": job.id,
            "attempt_number": 1,
            "state": "queued",
        })
        enrichment.with_user(bot).write({"model_id": "anthropic.claude-test-v1"})
        enrichment.invalidate_recordset()
        self.assertEqual(enrichment.sudo().model_id, "anthropic.claude-test-v1")
