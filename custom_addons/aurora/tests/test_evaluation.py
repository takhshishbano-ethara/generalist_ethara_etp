# -*- coding: utf-8 -*-
import base64
import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch, MagicMock

from odoo import fields as odoo_fields
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError

@tagged("post_install", "-at_install")
class TestAuroraEvaluation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("aurora.output_dir", "/tmp/aurora_test")
        cls.pipeline = cls.env["aurora.pipeline"].create({
            "github_org": "testorg",
            "github_repo": "testrepo",
        })
        cls.pipeline.write({"stage": "done", "step6_file": "/tmp/test_dataset.jsonl"})

    def _create_eval(self, **kwargs):
        vals = {}
        vals.update(kwargs)
        return self.env["aurora.evaluation"].create(vals)

    # ═══════════════════════════════════════════════════════════════════════════
    # Record creation
    # ═══════════════════════════════════════════════════════════════════════════

    def test_create_assigns_sequence(self):
        """New evaluation gets EVAL-XXXXX reference."""
        rec = self._create_eval()
        self.assertTrue(rec.name.startswith("EVAL-") or rec.name != "New")
        self.assertNotEqual(rec.name, "New")

    def test_create_preserves_explicit_name(self):
        """Explicit name is preserved."""
        rec = self._create_eval(name="CUSTOM-001")
        self.assertEqual(rec.name, "CUSTOM-001")

    def test_create_multi_unique_names(self):
        """Batch create assigns unique sequences."""
        recs = self.env["aurora.evaluation"].create([{}, {}, {}])
        self.assertEqual(len(recs), 3)
        names = [r.name for r in recs]
        self.assertEqual(len(set(names)), 3)

    def test_create_multi_mixed_names(self):
        """Batch create: explicit name preserved, 'New' gets sequence."""
        recs = self.env["aurora.evaluation"].create([
            {"name": "CUSTOM-X"},
            {},
        ])
        self.assertEqual(recs[0].name, "CUSTOM-X")
        self.assertNotEqual(recs[1].name, "New")

    def test_create_default_stage(self):
        """Stage defaults to draft."""
        rec = self._create_eval()
        self.assertEqual(rec.stage, "draft")

    def test_create_default_statuses(self):
        """build/run/report status default to idle."""
        rec = self._create_eval()
        self.assertEqual(rec.build_status, "idle")
        self.assertEqual(rec.run_status, "idle")
        self.assertEqual(rec.report_status, "idle")

    def test_create_default_counters(self):
        """Counter fields default to 0."""
        rec = self._create_eval()
        self.assertEqual(rec.total_instances, 0)
        self.assertEqual(rec.resolved_instances, 0)
        self.assertEqual(rec.unresolved_instances, 0)
        self.assertEqual(rec.error_instances, 0)

    def test_create_default_active(self):
        """active defaults to True."""
        rec = self._create_eval()
        self.assertTrue(rec.active)

    def test_create_default_user_id(self):
        """user_id defaults to current user."""
        rec = self._create_eval()
        self.assertEqual(rec.user_id, self.env.user)

    def test_create_default_workers(self):
        """Worker fields default to 4."""
        rec = self._create_eval()
        self.assertEqual(rec.max_workers_build, 4)
        self.assertEqual(rec.max_workers_run, 4)

    def test_create_default_instance_limit(self):
        """instance_limit defaults to 0."""
        rec = self._create_eval()
        self.assertEqual(rec.instance_limit, 0)

    def test_create_default_force_build(self):
        """force_build defaults to False."""
        rec = self._create_eval()
        self.assertFalse(rec.force_build)

    def test_create_empty_string_fields(self):
        """String fields are empty by default."""
        rec = self._create_eval()
        self.assertFalse(rec.dataset_file)
        self.assertFalse(rec.patch_file)
        self.assertFalse(rec.repo_dir)
        self.assertFalse(rec.workdir)
        self.assertFalse(rec.output_dir)
        self.assertFalse(rec.docker_platform)
        self.assertFalse(rec.specific_prs)
        self.assertFalse(rec.log)
        self.assertFalse(rec.final_report_file)
        self.assertFalse(rec.missing_registries)

    def test_run_only_from_draft(self):
        """Can only start from draft stage."""
        for stage in ["building_images", "running_instances", "generating_reports", "done", "failed"]:
            rec = self._create_eval()
            rec.write({"stage": stage})
            with self.assertRaises(UserError):
                rec.action_run_evaluation()

    def test_run_no_dataset_raises(self):
        """No dataset_file raises UserError."""
        rec = self._create_eval()
        with self.assertRaises(UserError):
            rec.action_run_evaluation()

    @patch("os.path.isfile", return_value=False)
    def test_run_dataset_not_found_raises(self, mock_isfile):
        """Dataset file not on filesystem raises UserError."""
        rec = self._create_eval(dataset_file="/tmp/nonexistent.jsonl")
        with self.assertRaises(UserError):
            rec.action_run_evaluation()

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_success(self, mock_isfile, mock_makedirs, mock_submit):
        """Successful run sets stage and returns action."""
        mock_submit.return_value = True
        rec = self._create_eval(
            dataset_file="/tmp/test.jsonl",
            pipeline_id=self.pipeline.id,
            patch_file="/tmp/patches.jsonl",
        )
        result = rec.action_run_evaluation()
        self.assertEqual(result["type"], "ir.actions.act_window")
        self.assertEqual(result["res_model"], "aurora.evaluation")
        self.assertEqual(rec.stage, "building_images")

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_auto_fills_dataset_from_pipeline(self, mock_isfile, mock_makedirs, mock_submit):
        """Auto-fills dataset_file from pipeline if not set."""
        rec = self._create_eval(pipeline_id=self.pipeline.id, patch_file="/tmp/p.jsonl")
        rec.action_run_evaluation()
        self.assertEqual(rec.dataset_file, "/tmp/test_dataset.jsonl")

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_sets_output_dir_from_pipeline(self, mock_isfile, mock_makedirs, mock_submit):
        """Sets output_dir from pipeline org/repo."""
        rec = self._create_eval(
            pipeline_id=self.pipeline.id,
            dataset_file="/tmp/ds.jsonl",
            patch_file="/tmp/p.jsonl",
        )
        rec.action_run_evaluation()
        self.assertIn("testorg__testrepo", rec.output_dir)

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_sets_workdir(self, mock_isfile, mock_makedirs, mock_submit):
        """Sets workdir as subdir of output_dir."""
        rec = self._create_eval(
            dataset_file="/tmp/ds.jsonl",
            pipeline_id=self.pipeline.id,
            patch_file="/tmp/p.jsonl",
        )
        rec.action_run_evaluation()
        self.assertTrue(rec.workdir)
        self.assertIn("workdir", rec.workdir)

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_sets_repo_dir(self, mock_isfile, mock_makedirs, mock_submit):
        """Sets repo_dir from default base."""
        rec = self._create_eval(
            dataset_file="/tmp/ds.jsonl",
            pipeline_id=self.pipeline.id,
            patch_file="/tmp/p.jsonl",
        )
        rec.action_run_evaluation()
        self.assertTrue(rec.repo_dir)
        self.assertIn("repos", rec.repo_dir)

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_creates_directories(self, mock_isfile, mock_makedirs, mock_submit):
        """Creates workdir, output_dir, repo_dir directories."""
        rec = self._create_eval(
            dataset_file="/tmp/ds.jsonl",
            pipeline_id=self.pipeline.id,
            patch_file="/tmp/p.jsonl",
        )
        rec.action_run_evaluation()
        self.assertTrue(mock_makedirs.call_count >= 3)

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_without_pipeline(self, mock_isfile, mock_makedirs, mock_submit):
        """Run without pipeline_id uses name for output_dir."""
        rec = self._create_eval(
            dataset_file="/tmp/ds.jsonl",
            patch_file="/tmp/p.jsonl",
        )
        rec.action_run_evaluation()
        self.assertIn(rec.name, rec.output_dir)

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_run_preserves_existing_output_dir(self, mock_isfile, mock_makedirs, mock_submit):
        """Existing output_dir is not overwritten."""
        rec = self._create_eval(
            dataset_file="/tmp/ds.jsonl",
            output_dir="/custom/output",
            patch_file="/tmp/p.jsonl",
        )
        rec.action_run_evaluation()
        self.assertEqual(rec.output_dir, "/custom/output")

    # ═══════════════════════════════════════════════════════════════════════════
    # action_cancel
    # ═══════════════════════════════════════════════════════════════════════════

    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_cancel_running(self, mock_cancel):
        """Cancel sets stage to failed."""
        rec = self._create_eval()
        rec.write({"stage": "building_images"})
        rec.action_cancel()
        mock_cancel.assert_called_once_with(rec.id)
        self.assertEqual(rec.stage, "failed")

    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_cancel_from_all_running_stages(self, mock_cancel):
        """Cancel works from all non-terminal stages."""
        for stage in ["building_images", "running_instances", "generating_reports"]:
            rec = self._create_eval()
            rec.write({"stage": stage})
            rec.action_cancel()
            self.assertEqual(rec.stage, "failed")

    def test_cancel_terminal_raises(self):
        """Cannot cancel terminal states."""
        for stage in ["done", "failed"]:
            rec = self._create_eval()
            rec.write({"stage": stage})
            with self.assertRaises(UserError):
                rec.action_cancel()

    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_cancel_posts_message(self, mock_cancel):
        """Cancel posts chatter message."""
        rec = self._create_eval()
        rec.write({"stage": "building_images"})
        msg_count = len(rec.message_ids)
        rec.action_cancel()
        self.assertGreater(len(rec.message_ids), msg_count)

    # ═══════════════════════════════════════════════════════════════════════════
    # action_reset_to_draft
    # ═══════════════════════════════════════════════════════════════════════════

    def test_reset_to_draft(self):
        """Reset clears all statuses and counters."""
        rec = self._create_eval()
        rec.write({
            "stage": "failed",
            "build_status": "done",
            "run_status": "failed",
            "report_status": "running",
            "log": "some log",
            "total_instances": 100,
            "resolved_instances": 50,
            "unresolved_instances": 30,
            "error_instances": 20,
            "final_report_file": "/tmp/report.json",
            "patch_file": "/tmp/patches.jsonl",
            "missing_registries": "org/repo",
        })
        rec.action_reset_to_draft()
        self.assertEqual(rec.stage, "draft")
        self.assertEqual(rec.build_status, "idle")
        self.assertEqual(rec.run_status, "idle")
        self.assertEqual(rec.report_status, "idle")
        self.assertFalse(rec.log)
        self.assertEqual(rec.total_instances, 0)
        self.assertEqual(rec.resolved_instances, 0)
        self.assertEqual(rec.unresolved_instances, 0)
        self.assertEqual(rec.error_instances, 0)
        self.assertFalse(rec.final_report_file)
        self.assertFalse(rec.patch_file)
        self.assertFalse(rec.missing_registries)

    def test_reset_only_from_terminal(self):
        """Cannot reset non-terminal states."""
        for stage in ["building_images", "running_instances", "generating_reports", "draft"]:
            rec = self._create_eval()
            rec.write({"stage": stage})
            with self.assertRaises(UserError):
                rec.action_reset_to_draft()

    def test_reset_from_done(self):
        """Can reset from done."""
        rec = self._create_eval()
        rec.write({"stage": "done"})
        rec.action_reset_to_draft()
        self.assertEqual(rec.stage, "draft")

    def test_reset_from_failed(self):
        """Can reset from failed."""
        rec = self._create_eval()
        rec.write({"stage": "failed"})
        rec.action_reset_to_draft()
        self.assertEqual(rec.stage, "draft")

    # ═══════════════════════════════════════════════════════════════════════════
    # action_regenerate_report
    # ═══════════════════════════════════════════════════════════════════════════

    def test_regenerate_only_from_terminal(self):
        """Can only regenerate from terminal states."""
        for stage in ["building_images", "running_instances", "generating_reports", "draft"]:
            rec = self._create_eval()
            rec.write({"stage": stage})
            with self.assertRaises(UserError):
                rec.action_regenerate_report()

    def test_regenerate_requires_output_dir(self):
        """Requires output_dir."""
        rec = self._create_eval(dataset_file="/tmp/ds.jsonl")
        rec.write({"stage": "done"})
        with self.assertRaises(UserError):
            rec.action_regenerate_report()

    def test_regenerate_requires_dataset_file(self):
        """Requires dataset_file."""
        rec = self._create_eval(output_dir="/tmp/out")
        rec.write({"stage": "done"})
        with self.assertRaises(UserError):
            rec.action_regenerate_report()

    def test_regenerate_sets_report_running(self):
        """Sets report_status to running."""
        rec = self._create_eval(output_dir="/tmp/out", dataset_file="/tmp/ds.jsonl")
        rec.write({"stage": "done"})
        result = rec.action_regenerate_report()
        self.assertEqual(rec.report_status, "running")
        self.assertEqual(result["type"], "ir.actions.act_window")

    # ═══════════════════════════════════════════════════════════════════════════
    # _cron_watchdog_stalled_eval
    # ═══════════════════════════════════════════════════════════════════════════

    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_watchdog_marks_stalled(self, mock_cancel):
        """Watchdog marks stalled evals as failed."""
        rec = self._create_eval()
        stale_time = odoo_fields.Datetime.now() - timedelta(minutes=20)
        rec.write({"stage": "building_images", "last_heartbeat": stale_time})
        self.env["aurora.evaluation"]._cron_watchdog_stalled_eval()
        rec.invalidate_recordset()
        self.assertEqual(rec.stage, "failed")
        mock_cancel.assert_called_once_with(rec.id)

    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_watchdog_ignores_fresh(self, mock_cancel):
        """Watchdog ignores fresh heartbeats."""
        rec = self._create_eval()
        rec.write({"stage": "building_images", "last_heartbeat": odoo_fields.Datetime.now()})
        self.env["aurora.evaluation"]._cron_watchdog_stalled_eval()
        rec.invalidate_recordset()
        self.assertEqual(rec.stage, "building_images")
        mock_cancel.assert_not_called()

    def test_watchdog_ignores_terminal(self):
        """Watchdog ignores terminal states."""
        stale_time = odoo_fields.Datetime.now() - timedelta(minutes=20)
        for stage in ["done", "failed"]:
            rec = self._create_eval()
            rec.write({"stage": stage, "last_heartbeat": stale_time})
            self.env["aurora.evaluation"]._cron_watchdog_stalled_eval()
            rec.invalidate_recordset()
            self.assertEqual(rec.stage, stage)

    def test_watchdog_ignores_draft(self):
        """Watchdog ignores draft."""
        rec = self._create_eval()
        stale_time = odoo_fields.Datetime.now() - timedelta(minutes=20)
        rec.write({"stage": "draft", "last_heartbeat": stale_time})
        self.env["aurora.evaluation"]._cron_watchdog_stalled_eval()
        rec.invalidate_recordset()
        self.assertEqual(rec.stage, "draft")

    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_watchdog_multiple_stalled(self, mock_cancel):
        """Watchdog handles multiple stalled evals."""
        stale_time = odoo_fields.Datetime.now() - timedelta(minutes=20)
        rec1 = self._create_eval()
        rec1.write({"stage": "building_images", "last_heartbeat": stale_time})
        rec2 = self._create_eval()
        rec2.write({"stage": "running_instances", "last_heartbeat": stale_time})
        self.env["aurora.evaluation"]._cron_watchdog_stalled_eval()
        rec1.invalidate_recordset()
        rec2.invalidate_recordset()
        self.assertEqual(rec1.stage, "failed")
        self.assertEqual(rec2.stage, "failed")
        self.assertEqual(mock_cancel.call_count, 2)

    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_watchdog_posts_message(self, mock_cancel):
        """Watchdog posts chatter message."""
        rec = self._create_eval()
        stale_time = odoo_fields.Datetime.now() - timedelta(minutes=20)
        rec.write({"stage": "building_images", "last_heartbeat": stale_time})
        msg_count = len(rec.message_ids)
        self.env["aurora.evaluation"]._cron_watchdog_stalled_eval()
        rec.invalidate_recordset()
        self.assertGreater(len(rec.message_ids), msg_count)

    # ═══════════════════════════════════════════════════════════════════════════
    # is_admin computed field
    # ═══════════════════════════════════════════════════════════════════════════

    def test_is_admin_for_admin_group(self):
        """Admin group member gets True."""
        admin_group = self.env.ref("aurora.group_aurora_admin")
        self.env.user.write({"group_ids": [(4, admin_group.id)]})
        rec = self._create_eval()
        rec.invalidate_recordset()
        self.assertTrue(rec.is_admin)

    def test_is_admin_for_regular_user(self):
        """Non-admin gets False."""
        admin_group = self.env.ref("aurora.group_aurora_admin")
        self.env.user.write({"group_ids": [(3, admin_group.id)]})
        rec = self._create_eval()
        rec.invalidate_recordset()
        self.assertFalse(rec.is_admin)

    # ═══════════════════════════════════════════════════════════════════════════
    # Constants
    # ═══════════════════════════════════════════════════════════════════════════

    def test_eval_stage_selection_has_all_stages(self):
        """EVAL_STAGE_SELECTION contains all expected stages."""
        from ..models.evaluation import EVAL_STAGE_SELECTION
        keys = {k for k, _ in EVAL_STAGE_SELECTION}
        expected = {"draft", "building_images", "running_instances", "generating_reports", "done", "failed"}
        self.assertEqual(keys, expected)

    def test_eval_terminal_states_subset(self):
        """EVAL_TERMINAL_STATES is subset of stage selection keys."""
        from ..models.evaluation import EVAL_STAGE_SELECTION, EVAL_TERMINAL_STATES
        keys = {k for k, _ in EVAL_STAGE_SELECTION}
        self.assertTrue(EVAL_TERMINAL_STATES.issubset(keys))

    def test_eval_status_has_all_states(self):
        """EVAL_STATUS contains idle, running, done, failed."""
        from ..models.evaluation import EVAL_STATUS
        keys = {k for k, _ in EVAL_STATUS}
        self.assertEqual(keys, {"idle", "running", "done", "failed"})

    def test_eval_terminal_states_exact(self):
        """EVAL_TERMINAL_STATES is exactly done and failed."""
        from ..models.evaluation import EVAL_TERMINAL_STATES
        self.assertEqual(EVAL_TERMINAL_STATES, {"done", "failed"})

    def test_eval_stage_selection_count(self):
        """EVAL_STAGE_SELECTION has 6 stages."""
        from ..models.evaluation import EVAL_STAGE_SELECTION
        self.assertEqual(len(EVAL_STAGE_SELECTION), 6)

    def test_eval_status_count(self):
        """EVAL_STATUS has 4 states."""
        from ..models.evaluation import EVAL_STATUS
        self.assertEqual(len(EVAL_STATUS), 4)

    # ═══════════════════════════════════════════════════════════════════════════
    # _onchange_pipeline_id
    # ═══════════════════════════════════════════════════════════════════════════

    def test_onchange_pipeline_sets_dataset_file(self):
        """Sets dataset_file from pipeline step6_file."""
        rec = self._create_eval()
        rec.pipeline_id = self.pipeline
        rec._onchange_pipeline_id()
        self.assertEqual(rec.dataset_file, "/tmp/test_dataset.jsonl")

    def test_onchange_pipeline_sets_output_dir(self):
        """Sets output_dir from pipeline org/repo."""
        rec = self._create_eval()
        rec.pipeline_id = self.pipeline
        rec._onchange_pipeline_id()
        self.assertIn("testorg__testrepo", rec.output_dir)

    def test_onchange_no_pipeline(self):
        """No pipeline_id: no changes."""
        rec = self._create_eval()
        rec.pipeline_id = False
        rec._onchange_pipeline_id()
        self.assertFalse(rec.dataset_file)

    def test_onchange_pipeline_no_step6(self):
        """Pipeline without step6_file: dataset_file not set."""
        pl = self.env["aurora.pipeline"].create({
            "github_org": "org2", "github_repo": "repo2",
        })
        rec = self._create_eval()
        rec.pipeline_id = pl
        rec._onchange_pipeline_id()
        self.assertFalse(rec.dataset_file)

    # ═══════════════════════════════════════════════════════════════════════════
    # Parametric stage tests
    # ═══════════════════════════════════════════════════════════════════════════

    def test_all_running_stages_watchdog_marks_failed(self):
        """Each running stage is caught by watchdog."""
        stale_time = odoo_fields.Datetime.now() - timedelta(minutes=20)
        for stage in ["building_images", "running_instances", "generating_reports"]:
            rec = self._create_eval()
            rec.write({"stage": stage, "last_heartbeat": stale_time})
            with patch("odoo.addons.aurora.models.evaluation_executor.request_cancel"):
                self.env["aurora.evaluation"]._cron_watchdog_stalled_eval()
            rec.invalidate_recordset()
            self.assertEqual(rec.stage, "failed", f"Watchdog should catch stage {stage}")

    def test_write_stage_transitions(self):
        """Can write all valid stage values."""
        from ..models.evaluation import EVAL_STAGE_SELECTION
        rec = self._create_eval()
        for stage_key, _ in EVAL_STAGE_SELECTION:
            rec.write({"stage": stage_key})
            self.assertEqual(rec.stage, stage_key)

    def test_write_status_transitions(self):
        """Can write all valid status values."""
        from ..models.evaluation import EVAL_STATUS
        rec = self._create_eval()
        for status_key, _ in EVAL_STATUS:
            rec.write({"build_status": status_key})
            self.assertEqual(rec.build_status, status_key)
            rec.write({"run_status": status_key})
            self.assertEqual(rec.run_status, status_key)
            rec.write({"report_status": status_key})
            self.assertEqual(rec.report_status, status_key)

    # ═══════════════════════════════════════════════════════════════════════════
    # action_reset_to_draft — new behaviors
    # ═══════════════════════════════════════════════════════════════════════════

    def test_reset_unlinks_instances(self):
        rec = self._create_eval()
        rec.write({"stage": "done"})
        inst = self.env["aurora.evaluation.instance"].create({
            "evaluation_id": rec.id,
            "instance_id": "test-instance-1",
        })
        self.assertTrue(rec.instance_ids)
        rec.action_reset_to_draft()
        self.assertFalse(rec.instance_ids)
        self.assertFalse(inst.exists())

    def test_reset_restores_dataset_file_from_pipeline(self):
        rec = self._create_eval()
        rec.write({"pipeline_id": self.pipeline.id})
        rec.write({
            "stage": "failed",
            "dataset_file": "/tmp/aurora_output/dataset_cache/stale_local_path.jsonl",
        })
        rec.action_reset_to_draft()
        self.assertEqual(rec.dataset_file, "/tmp/test_dataset.jsonl")

    def test_reset_clears_dataset_file_without_pipeline(self):
        rec = self._create_eval()
        rec.write({
            "stage": "failed",
            "dataset_file": "/tmp/stale.jsonl",
        })
        rec.action_reset_to_draft()
        self.assertFalse(rec.dataset_file)

    def test_reset_clears_dataset_jsonl_url(self):
        rec = self._create_eval()
        rec.write({
            "stage": "done",
            "dataset_jsonl_url": "http://minio:9000/bkt/dataset.jsonl",
        })
        rec.action_reset_to_draft()
        self.assertFalse(rec.dataset_jsonl_url)

    # ═══════════════════════════════════════════════════════════════════════════
    # _delete_evaluation_job
    # ═══════════════════════════════════════════════════════════════════════════

    @patch("odoo.addons.aurora.models.evaluation.K8S_AVAILABLE", False)
    def test_delete_job_noop_when_k8s_unavailable(self):
        rec = self._create_eval()
        rec._delete_evaluation_job()

    @patch("odoo.addons.aurora.models.evaluation._load_k8s_config")
    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    @patch("odoo.addons.aurora.models.evaluation.K8S_AVAILABLE", True)
    def test_delete_job_uses_label_selector(self, mock_k8s, mock_load):
        mock_batch = MagicMock()
        mock_k8s.BatchV1Api.return_value = mock_batch
        mock_k8s.V1DeleteOptions.return_value = MagicMock()
        rec = self._create_eval()
        rec._delete_evaluation_job()
        mock_batch.delete_collection_namespaced_job.assert_called_once()
        call_kwargs = mock_batch.delete_collection_namespaced_job.call_args
        self.assertIn(f"evaluation-id={rec.id}", call_kwargs.kwargs.get("label_selector", call_kwargs[1].get("label_selector", "")))

    @patch("odoo.addons.aurora.models.evaluation._load_k8s_config")
    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    @patch("odoo.addons.aurora.models.evaluation.K8S_AVAILABLE", True)
    def test_delete_job_swallows_exception(self, mock_k8s, mock_load):
        mock_batch = MagicMock()
        mock_batch.delete_collection_namespaced_job.side_effect = Exception("API error")
        mock_k8s.BatchV1Api.return_value = mock_batch
        rec = self._create_eval()
        rec._delete_evaluation_job()

    @patch("odoo.addons.aurora.models.evaluation.K8S_AVAILABLE", True)
    @patch("odoo.addons.aurora.models.evaluation._load_k8s_config")
    @patch("odoo.addons.aurora.models.evaluation.k8s_client")
    @patch("odoo.addons.aurora.models.evaluation_executor.request_cancel")
    def test_cancel_calls_delete_job(self, mock_cancel, mock_k8s, mock_load):
        mock_batch = MagicMock()
        mock_k8s.BatchV1Api.return_value = mock_batch
        mock_k8s.V1DeleteOptions.return_value = MagicMock()
        rec = self._create_eval()
        rec.write({"stage": "building_images"})
        rec.action_cancel()
        mock_batch.delete_collection_namespaced_job.assert_called_once()

    # ═══════════════════════════════════════════════════════════════════════════
    # action_regenerate_report — S3 upload
    # ═══════════════════════════════════════════════════════════════════════════

    @patch("odoo.addons.aurora.models.s3_storage.upload_file", return_value="http://minio:9000/bkt/report.json")
    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=True)
    @patch("odoo.addons.aurora.models.s3_storage.build_s3_key", return_value="aurora/phase2/org__repo/run_1/final_report.json")
    @patch("odoo.addons.aurora.models.artifact_collector.load_s3_config", return_value={"bucket": "bkt", "region": "us-east-1", "folder": "aurora"})
    def test_regen_uploads_to_s3_when_configured(self, mock_s3cfg, mock_key, mock_is_cfg, mock_upload):
        rec = self._create_eval(
            output_dir="/tmp/out",
            dataset_file="/tmp/ds.jsonl",
        )
        rec.write({"pipeline_id": self.pipeline.id, "stage": "done", "s3_run_number": 1})
        with tempfile.TemporaryDirectory() as td:
            rec.write({"output_dir": td})
            report_path = os.path.join(td, "final_report.json")
            with open(report_path, "w") as f:
                json.dump({"total_instances": 2, "resolved_instances": 1, "unresolved_instances": 1, "error_instances": 0}, f)
            with patch("odoo.addons.aurora.models.dataset_resolver.is_remote", return_value=False):
                rec.action_regenerate_report()

    # ═══════════════════════════════════════════════════════════════════════════
    # dataset_jsonl_url field
    # ═══════════════════════════════════════════════════════════════════════════

    def test_dataset_jsonl_url_field_exists(self):
        rec = self._create_eval()
        self.assertFalse(rec.dataset_jsonl_url)

    def test_dataset_jsonl_url_writable(self):
        rec = self._create_eval()
        rec.write({"dataset_jsonl_url": "http://minio:9000/bkt/dataset.jsonl"})
        self.assertEqual(rec.dataset_jsonl_url, "http://minio:9000/bkt/dataset.jsonl")



@tagged("post_install", "-at_install")
class TestAuroraEvaluationCustomImport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("aurora.output_dir", "/tmp/aurora_test")

    def _make_jsonl_b64(self, entries=None):
        if entries is None:
            entries = [{"org": "myorg", "repo": "myrepo", "number": 1}]
        content = "\n".join(json.dumps(e) for e in entries)
        return base64.b64encode(content.encode()).decode()

    def _create_custom_eval(self, **kwargs):
        vals = {
            "dataset_source": "custom",
            "custom_org": "myorg",
            "custom_repo": "myrepo",
            "custom_jsonl_file": self._make_jsonl_b64(),
            "custom_jsonl_filename": "dataset.jsonl",
            "docker_platform": "linux/amd64,linux/arm64",
        }
        vals.update(kwargs)
        return self.env["aurora.evaluation"].create(vals)

    def test_custom_no_file_raises(self):
        rec = self.env["aurora.evaluation"].create({
            "dataset_source": "custom",
            "custom_org": "myorg",
            "custom_repo": "myrepo",
            "docker_platform": "linux/amd64,linux/arm64",
        })
        with self.assertRaises(UserError):
            rec.action_run_evaluation()

    def test_custom_no_org_raises(self):
        rec = self.env["aurora.evaluation"].create({
            "dataset_source": "custom",
            "custom_repo": "myrepo",
            "custom_jsonl_file": self._make_jsonl_b64(),
            "custom_jsonl_filename": "dataset.jsonl",
            "docker_platform": "linux/amd64,linux/arm64",
        })
        with self.assertRaises(UserError):
            rec.action_run_evaluation()

    def test_custom_no_repo_raises(self):
        rec = self.env["aurora.evaluation"].create({
            "dataset_source": "custom",
            "custom_org": "myorg",
            "custom_jsonl_file": self._make_jsonl_b64(),
            "custom_jsonl_filename": "dataset.jsonl",
            "docker_platform": "linux/amd64,linux/arm64",
        })
        with self.assertRaises(UserError):
            rec.action_run_evaluation()

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    def test_custom_run_sets_stage_to_building_images(self, _submit):
        rec = self._create_custom_eval()
        rec.action_run_evaluation()
        self.assertEqual(rec.stage, "building_images")

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    def test_custom_run_sets_dataset_file(self, _submit):
        rec = self._create_custom_eval()
        rec.action_run_evaluation()
        self.assertTrue(rec.dataset_file)
        self.assertIn(f"custom_{rec.id}_", rec.dataset_file)

    @patch("odoo.addons.aurora.models.evaluation_executor.submit_evaluation_async")
    def test_custom_run_s3_not_uploaded_from_backend(self, _submit):
        rec = self._create_custom_eval()
        rec.action_run_evaluation()
        self.assertEqual(rec.s3_run_number, 0)
        self.assertFalse(rec.dataset_jsonl_url)

    def test_reset_clears_custom_fields(self):
        rec = self._create_custom_eval()
        rec.write({"stage": "failed"})
        rec.action_reset_to_draft()
        self.assertFalse(rec.custom_jsonl_file)
        self.assertFalse(rec.custom_jsonl_filename)
        self.assertEqual(rec.stage, "draft")

    def test_s3_base_uri_uses_custom_org_repo(self):
        rec = self._create_custom_eval()
        rec.write({"s3_run_number": 7})
        self.assertIn("myorg__myrepo", rec.s3_base_uri)
        self.assertIn("run_7", rec.s3_base_uri)
        self.assertIn("aurora_phase2", rec.s3_base_uri)

    def test_s3_base_uri_empty_without_run_number(self):
        rec = self._create_custom_eval()
        self.assertFalse(rec.s3_base_uri)
