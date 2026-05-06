import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestTrajectoryDispatchPreconditions(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.llm_config_template", json.dumps({
            "model": "bedrock/test-model",
            "api_key": "test-key",
        }))
        ICP.set_param("jaeger.ecr_prefix", "test.ecr.io/prefix")

        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/trajorg/trajrepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
            "dataset_status": "done",
            "final_dataset_jsonl_path": "/tmp/test_final.jsonl",
            "final_dataset_count": 5,
            "trajectory_status": "pending",
        })

    def test_dispatch_requires_stage6(self):
        self.repo.write({"current_stage": "stage5"})
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        self.repo.write({"current_stage": "stage6"})

    def test_dispatch_blocks_if_already_dispatched(self):
        self.repo.write({"trajectory_status": "dispatched"})
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_blocks_if_running(self):
        self.repo.write({"trajectory_status": "running"})
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_blocks_if_evaluating(self):
        self.repo.write({"trajectory_status": "evaluating"})
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_requires_final_dataset(self):
        self.repo.write({"final_dataset_jsonl_path": False, "final_dataset_count": 0})
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        self.repo.write({"final_dataset_jsonl_path": "/tmp/f.jsonl", "final_dataset_count": 5})

    def test_dispatch_requires_llm_config_template(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.llm_config_template", "")
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        ICP.set_param("jaeger.llm_config_template", json.dumps({"model": "test"}))

    def test_dispatch_requires_ecr_prefix(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.ecr_prefix", "")
        self.repo.write({"ecr_prefix": False})
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        ICP.set_param("jaeger.ecr_prefix", "test.ecr.io/prefix")

    def test_dispatch_uses_repo_ecr_prefix_over_icp(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.ecr_prefix", "icp-prefix")
        self.repo.write({"ecr_prefix": "repo-prefix"})
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.assertEqual(self.repo.trajectory_status, "dispatched")
        self.repo.write({"trajectory_status": "pending", "ecr_prefix": False})

    def test_dispatch_allows_pending_status(self):
        self.repo.write({"trajectory_status": "pending"})
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.assertEqual(self.repo.trajectory_status, "dispatched")
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_allows_failed_retry(self):
        self.repo.write({"trajectory_status": "failed", "error_message": "Old error"})
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.assertEqual(self.repo.trajectory_status, "dispatched")
        self.assertFalse(self.repo.error_message)
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_clears_error_message(self):
        self.repo.write({"trajectory_status": "failed", "error_message": "prev error"})
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.assertFalse(self.repo.error_message)
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_with_zero_valid_instances(self):
        self.repo.write({"final_dataset_jsonl_path": False, "final_dataset_count": 0})
        with self.assertRaises(UserError):
            self.repo.action_dispatch_trajectories()
        self.repo.write({"final_dataset_jsonl_path": "/tmp/f.jsonl", "final_dataset_count": 5})


class TestTrajectoryDispatchExecution(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.llm_config_template", json.dumps({
            "model": "bedrock/test", "api_key": "k",
        }))
        ICP.set_param("jaeger.ecr_prefix", "ecr.io/test")

        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/disporg/disprepo",
            "language": "javascript",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
            "final_dataset_jsonl_path": "/tmp/dataset.jsonl",
            "final_dataset_count": 3,
            "trajectory_status": "pending",
            "k_runs": 4,
            "temperature": 0.8,
        })

    def test_dispatch_writes_status_dispatched(self):
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.assertEqual(self.repo.trajectory_status, "dispatched")
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_generates_unique_job_id(self):
        import re
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.assertTrue(re.match(
            r"jaeger-traj-\d+-[a-f0-9]{8}",
            self.repo.eks_job_id,
        ))
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_stores_llm_config_json(self):
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        config = json.loads(self.repo.llm_config_json)
        self.assertEqual(config["k_runs"], 4)
        self.assertAlmostEqual(config["temperature"], 0.8)
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_registers_postcommit_hook(self):
        mock_postcommit = MagicMock()
        with patch.object(self.env.cr, "postcommit", mock_postcommit):
            self.repo.action_dispatch_trajectories()
        mock_postcommit.add.assert_called_once()
        self.repo.write({"trajectory_status": "pending"})

    def test_dispatch_appends_log(self):
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.repo.invalidate_recordset(["log_output"])
        self.assertIn("Trajectory job dispatched", self.repo.log_output or "")
        self.repo.write({"trajectory_status": "pending"})


class TestTrajectoryConfigResolution(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.llm_config_template", json.dumps({
            "model": "bedrock/default", "base_url": "https://api.example.com",
        }))
        ICP.set_param("jaeger.default_model", "gpt-4")
        ICP.set_param("jaeger.default_k", "8")

        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/cfgorg/cfgrepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
            "k_runs": 4,
            "temperature": 0.0,
        })

    def test_per_repo_k_runs_overrides_icp(self):
        config = self.repo._resolve_trajectory_config()
        self.assertEqual(config["k_runs"], 4)

    def test_icp_default_used_when_repo_zero(self):
        self.repo.write({"k_runs": 0})
        config = self.repo._resolve_trajectory_config()
        self.assertEqual(config["k_runs"], 8)
        self.repo.write({"k_runs": 4})

    def test_temperature_default_1_0(self):
        self.repo.write({"temperature": 0.0})
        config = self.repo._resolve_trajectory_config()
        self.assertAlmostEqual(config["temperature"], 1.0)

    def test_llm_template_merged_with_repo_config(self):
        self.repo.write({"model_canonical_name": "claude-sonnet"})
        config = self.repo._resolve_trajectory_config()
        self.assertEqual(config["model_name"], "claude-sonnet")
        self.assertIn("base_url", config)
        self.repo.write({"model_canonical_name": False})

    def test_invalid_template_json_fallback(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.llm_config_template", "not json{")
        config = self.repo._resolve_trajectory_config()
        self.assertIn("k_runs", config)
        ICP.set_param("jaeger.llm_config_template", json.dumps({"model": "test"}))

    def test_empty_template_returns_defaults(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.llm_config_template", "")
        config = self.repo._resolve_trajectory_config()
        self.assertIn("k_runs", config)
        ICP.set_param("jaeger.llm_config_template", json.dumps({"model": "test"}))

    def test_repo_model_overrides_icp_default(self):
        self.repo.write({"model_canonical_name": "my-model"})
        config = self.repo._resolve_trajectory_config()
        self.assertEqual(config["model_name"], "my-model")
        self.repo.write({"model_canonical_name": False})


class TestTrajectoryWebhookProgress(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/wporg/wprepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
            "trajectory_status": "dispatched",
        })

    def test_progress_sets_running_status(self):
        self.repo._handle_trajectory_progress({"step": "Running inference..."})
        self.assertEqual(self.repo.trajectory_status, "running")

    def test_progress_appends_log(self):
        self.repo._handle_trajectory_progress({"step": "Step 3/15"})
        self.repo.invalidate_recordset(["log_output"])
        self.assertIn("Step 3/15", self.repo.log_output or "")

    def test_progress_from_dispatched_transitions_to_running(self):
        self.repo.write({"trajectory_status": "dispatched"})
        self.repo._handle_trajectory_progress({"step": "Starting..."})
        self.assertEqual(self.repo.trajectory_status, "running")


class TestTrajectoryWebhookDone(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/doneorg/donerepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
            "trajectory_status": "running",
            "k_runs": 8,
        })
        cls.inst = cls.env["jaeger.instance"].create({
            "name": "doneorg__donerepo-1",
            "repository_id": cls.repo.id,
            "org": "doneorg",
            "repo": "donerepo",
            "pr_number": 1,
            "is_valid": True,
        })

    def test_done_sets_status_done(self):
        self.repo._handle_trajectory_done({
            "summary": {"pass_at_k": 0.5},
            "per_run_results": [],
        })
        self.assertEqual(self.repo.trajectory_status, "done")

    def test_done_writes_pass_at_k(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_done({
            "summary": {"pass_at_k": 0.375},
            "per_run_results": [],
        })
        self.assertAlmostEqual(self.repo.pass_at_k, 0.375)

    def test_done_writes_cost_metrics(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_done({
            "summary": {
                "pass_at_k": 0.5,
                "timing_metrics": {"total": {
                    "accumulated_cost_usd": 18.5,
                    "api_calls": 500,
                    "prompt_tokens": 1000000,
                    "completion_tokens": 50000,
                }},
            },
            "per_run_results": [],
        })
        self.assertAlmostEqual(self.repo.total_api_cost, 18.5)
        self.assertEqual(self.repo.total_api_calls, 500)

    def test_done_creates_trajectory_run_records(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_done({
            "summary": {"pass_at_k": 0.5},
            "per_run_results": [
                {"run_number": 1, "resolved": True, "api_cost": 2.0},
                {"run_number": 2, "resolved": False, "api_cost": 1.5},
                {"run_number": 3, "resolved": True, "api_cost": 2.5},
            ],
        })
        runs = self.env["jaeger.trajectory.run"].search([
            ("repository_id", "=", self.repo.id),
        ])
        self.assertGreaterEqual(len(runs), 3)

    def test_done_run_records_have_correct_fields(self):
        self.repo.write({"trajectory_status": "running"})
        Run = self.env["jaeger.trajectory.run"]
        Run.search([("repository_id", "=", self.repo.id)]).unlink()
        self.repo._handle_trajectory_done({
            "summary": {"pass_at_k": 1.0},
            "per_run_results": [
                {"run_number": 1, "resolved": True, "api_cost": 2.5, "api_calls": 45},
            ],
        })
        run = Run.search([
            ("repository_id", "=", self.repo.id),
            ("run_number", "=", 1),
        ], limit=1)
        self.assertTrue(run.resolved)
        self.assertAlmostEqual(run.api_cost, 2.5)

    def test_done_stores_summary_json(self):
        self.repo.write({"trajectory_status": "running"})
        summary = {"pass_at_k": 0.5, "total_instances": 10}
        self.repo._handle_trajectory_done({
            "summary": summary,
            "per_run_results": [],
        })
        stored = json.loads(self.repo.pass_at_k_summary_json)
        self.assertEqual(stored["total_instances"], 10)

    def test_done_with_empty_per_run_results(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_done({
            "summary": {"pass_at_k": 0.0},
            "per_run_results": [],
        })
        self.assertEqual(self.repo.trajectory_status, "done")

    def test_done_with_zero_pass_at_k(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_done({
            "summary": {"pass_at_k": 0.0},
            "per_run_results": [],
        })
        self.assertAlmostEqual(self.repo.pass_at_k, 0.0)
        self.assertEqual(self.repo.trajectory_status, "done")

    def test_done_with_missing_summary_fields(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_done({
            "summary": {},
            "per_run_results": [],
        })
        self.assertAlmostEqual(self.repo.total_api_cost, 0.0)


class TestTrajectoryWebhookFailed(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/failorg/failrepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
            "trajectory_status": "running",
        })

    def test_failed_sets_status_failed(self):
        self.repo._handle_trajectory_failed({"error": "Something broke"})
        self.assertEqual(self.repo.trajectory_status, "failed")

    def test_failed_writes_error_message(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_failed({"error": "OOM killed"})
        self.assertIn("OOM killed", self.repo.error_message)

    def test_failed_truncates_long_error(self):
        self.repo.write({"trajectory_status": "running"})
        long_error = "x" * 3000
        self.repo._handle_trajectory_failed({"error": long_error})
        self.assertLessEqual(len(self.repo.error_message), 2000)

    def test_failed_appends_log(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_failed({"error": "crash"})
        self.repo.invalidate_recordset(["log_output"])
        self.assertIn("FAILED", self.repo.log_output or "")

    def test_failed_does_not_advance_stage(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_failed({"error": "fail"})
        self.assertEqual(self.repo.current_stage, "stage6")

    def test_failed_allows_retry_dispatch(self):
        self.repo.write({"trajectory_status": "running"})
        self.repo._handle_trajectory_failed({"error": "fail"})
        self.assertEqual(self.repo.trajectory_status, "failed")
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.llm_config_template", json.dumps({"model": "t"}))
        ICP.set_param("jaeger.ecr_prefix", "test.ecr")
        self.repo.write({"final_dataset_jsonl_path": "/tmp/x.jsonl", "final_dataset_count": 1})
        with patch.object(self.env.cr, "postcommit", MagicMock()):
            self.repo.action_dispatch_trajectories()
        self.assertEqual(self.repo.trajectory_status, "dispatched")
        self.repo.write({"trajectory_status": "pending"})


class TestTrajectoryStageGates(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/gateorg/gaterepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage5",
        })

    def test_stage5_gate_requires_dataset_done(self):
        self.repo.write({"current_stage": "stage5", "dataset_status": "pending"})
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("Dataset finalization not complete", msg)

    def test_stage5_gate_requires_nonzero_count(self):
        self.repo.write({
            "current_stage": "stage5",
            "dataset_status": "done",
            "final_dataset_count": 0,
        })
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("No valid instances", msg)

    def test_stage5_gate_passes(self):
        self.repo.write({
            "current_stage": "stage5",
            "dataset_status": "done",
            "final_dataset_count": 5,
        })
        ok, _ = self.repo._check_current_gate()
        self.assertTrue(ok)

    def test_stage6_gate_requires_trajectory_done(self):
        self.repo.write({"current_stage": "stage6", "trajectory_status": "running"})
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("Trajectory generation not complete", msg)

    def test_stage6_gate_requires_summary(self):
        self.repo.write({
            "current_stage": "stage6",
            "trajectory_status": "done",
            "pass_at_k_summary_json": False,
        })
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("No pass@k summary", msg)

    def test_stage6_gate_passes(self):
        self.repo.write({
            "current_stage": "stage6",
            "trajectory_status": "done",
            "pass_at_k_summary_json": json.dumps({"pass_at_k": 0.5}),
        })
        ok, _ = self.repo._check_current_gate()
        self.assertTrue(ok)

    def test_terminal_state_blocks_gate(self):
        self.repo.write({
            "current_stage": "stage6",
            "terminal_state": "no_valid_instances",
        })
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("Terminal state", msg)
        self.repo.write({"terminal_state": "none"})
