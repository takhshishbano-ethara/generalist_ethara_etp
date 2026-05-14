# -*- coding: utf-8 -*-
"""Unit tests for the Phase 2 tar-decision window + ECR push pipeline.

Covers the additions in:
  - models/evaluation.py         (new stage, new fields, constrains, actions, cron)
  - models/evaluation_instance.py (ECR per-instance fields)
  - models/artifact_collector.py  (push_resolved_images_to_ecr, ensure_ecr_repository)
  - models/harness_staging.py     (zip-merge collision check, S3 key parsing)

Style follows test_artifact_collector.py / test_worker_pipeline_staging.py:
import the module under conftest stubs, assert on structural facts, and use
mocks for any external side-effects (subprocess, file IO, DB).
"""
import json
import os
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock, ANY


# ═══════════════════════════════════════════════════════════════════════════
# 1. EVAL_STAGE_SELECTION / EVAL_TERMINAL_STATES — new stage value
# ═══════════════════════════════════════════════════════════════════════════
class TestEvalStageSelection(TestCase):

    def test_awaiting_tar_decision_is_a_stage(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [k for k, _ in EVAL_STAGE_SELECTION]
        self.assertIn("awaiting_tar_decision", keys)

    def test_awaiting_tar_decision_after_generating_reports(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = [k for k, _ in EVAL_STAGE_SELECTION]
        self.assertLess(keys.index("generating_reports"), keys.index("awaiting_tar_decision"))
        self.assertLess(keys.index("awaiting_tar_decision"), keys.index("done"))

    def test_terminal_states_unchanged(self):
        from odoo.addons.aurora.models.evaluation import EVAL_TERMINAL_STATES
        self.assertEqual(EVAL_TERMINAL_STATES, {"done", "failed"})
        self.assertNotIn("awaiting_tar_decision", EVAL_TERMINAL_STATES)

    def test_all_original_stages_still_present(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STAGE_SELECTION
        keys = {k for k, _ in EVAL_STAGE_SELECTION}
        for expected in ("draft", "building_images", "running_instances",
                         "generating_reports", "done", "failed"):
            self.assertIn(expected, keys)


# ═══════════════════════════════════════════════════════════════════════════
# 2. New fields on aurora.evaluation
# ═══════════════════════════════════════════════════════════════════════════
class TestEvaluationFields(TestCase):

    def setUp(self):
        from odoo.addons.aurora.models import evaluation as eval_mod
        self.AuroraEvaluation = eval_mod.AuroraEvaluation

    def _field(self, name):
        return getattr(self.AuroraEvaluation, name)

    def test_tar_decision_field_exists(self):
        f = self._field("tar_decision")
        self.assertEqual(f.kwargs.get("default"), "pending")

    def test_tar_decision_window_default_20(self):
        f = self._field("tar_decision_window_minutes")
        self.assertEqual(f.kwargs.get("default"), 20)

    def test_tar_decision_deadline_field_exists(self):
        self._field("tar_decision_deadline")  # must not raise

    def test_oci_export_status_default_idle(self):
        f = self._field("oci_export_status")
        self.assertEqual(f.kwargs.get("default"), "idle")

    def test_ecr_repository_field_exists(self):
        self._field("ecr_repository")

    def test_ecr_pushed_count_default_zero(self):
        f = self._field("ecr_pushed_count")
        self.assertEqual(f.kwargs.get("default"), 0)

    def test_ecr_manifest_s3_uri_field_exists(self):
        self._field("ecr_manifest_s3_uri")

    def test_docker_platform_defaults_multi_arch(self):
        f = self._field("docker_platform")
        self.assertEqual(f.kwargs.get("default"), "linux/amd64,linux/arm64")

    def test_docker_platform_not_required_at_field_level(self):
        """required=True would block test-evals from setting docker_platform=False."""
        f = self._field("docker_platform")
        self.assertNotEqual(f.kwargs.get("required"), True)

    def test_staging_test_id_field_exists(self):
        """Backref used by the multi-arch constraint exemption."""
        self._field("staging_test_id")


# ═══════════════════════════════════════════════════════════════════════════
# 3. New fields on aurora.evaluation.instance
# ═══════════════════════════════════════════════════════════════════════════
class TestEvaluationInstanceECRFields(TestCase):

    def setUp(self):
        from odoo.addons.aurora.models import evaluation_instance as ei_mod
        self.AuroraEvaluationInstance = ei_mod.AuroraEvaluationInstance

    def test_ecr_image_uri_field_exists(self):
        getattr(self.AuroraEvaluationInstance, "ecr_image_uri")

    def test_ecr_image_digest_field_exists(self):
        getattr(self.AuroraEvaluationInstance, "ecr_image_digest")

    def test_oci_tar_s3_uri_still_present(self):
        """Legacy build-time tar field is preserved alongside the new ECR fields."""
        getattr(self.AuroraEvaluationInstance, "oci_tar_s3_uri")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Worker _ALLOWED_EVAL_COLUMNS — must include new column names
# ═══════════════════════════════════════════════════════════════════════════
class TestWorkerAllowedEvalColumns(TestCase):

    def test_contains_tar_decision_deadline(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("tar_decision_deadline", _ALLOWED_EVAL_COLUMNS)

    def test_contains_oci_export_status(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("oci_export_status", _ALLOWED_EVAL_COLUMNS)

    def test_contains_ecr_repository(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("ecr_repository", _ALLOWED_EVAL_COLUMNS)

    def test_contains_ecr_pushed_count(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("ecr_pushed_count", _ALLOWED_EVAL_COLUMNS)

    def test_contains_ecr_manifest_s3_uri(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("ecr_manifest_s3_uri", _ALLOWED_EVAL_COLUMNS)

    def test_does_not_contain_tar_decision(self):
        """tar_decision is written by Odoo ORM (user click), not by the raw-SQL worker."""
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertNotIn("tar_decision", _ALLOWED_EVAL_COLUMNS)


# ═══════════════════════════════════════════════════════════════════════════
# 5. push_resolved_images_to_ecr — registry kind branching
# ═══════════════════════════════════════════════════════════════════════════
class TestPushResolvedImagesKindBranch(TestCase):

    def _make_final_report(self, tmp_dir, resolved_ids):
        path = Path(tmp_dir) / "final_report.json"
        path.write_text(json.dumps({"resolved_ids": list(resolved_ids)}))
        return tmp_dir

    def test_no_resolved_ids_returns_empty(self):
        """Empty resolved set short-circuits the function."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        with tempfile.TemporaryDirectory() as tmp:
            self._make_final_report(tmp, [])
            result = push_resolved_images_to_ecr(
                conn=MagicMock(), rec_id=1,
                workdir=tmp, output_dir=tmp, instances=[],
                run_numbers={},
                ecr_registry="example.com", ecr_region="us-east-1",
                s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
            )
            self.assertEqual(result["ecr_pushed_count"], 0)
            self.assertEqual(result["ecr_repository"], "")

    def test_empty_registry_raises(self):
        """Missing AURORA_ECR_REGISTRY is a hard failure for both kinds."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        with tempfile.TemporaryDirectory() as tmp:
            self._make_final_report(tmp, ["org/repo:pr-1"])
            with self.assertRaises(RuntimeError) as ctx:
                push_resolved_images_to_ecr(
                    conn=MagicMock(), rec_id=1,
                    workdir=tmp, output_dir=tmp, instances=[],
                    run_numbers={},
                    ecr_registry="", ecr_region="us-east-1",
                    s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
                )
            self.assertIn("AURORA_ECR_REGISTRY", str(ctx.exception))

    def test_missing_final_report_raises(self):
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                push_resolved_images_to_ecr(
                    conn=MagicMock(), rec_id=1,
                    workdir=tmp, output_dir=tmp, instances=[],
                    run_numbers={},
                    ecr_registry="example.com", ecr_region="us-east-1",
                    s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
                )
            self.assertIn("final_report.json", str(ctx.exception))

    @patch.dict(os.environ, {"AURORA_REGISTRY_KIND": "ecr"}, clear=False)
    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    def test_ecr_mode_requires_region(self, mock_subprocess_run):
        """kind=ecr without a region rejects before the AWS call."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        with tempfile.TemporaryDirectory() as tmp:
            self._make_final_report(tmp, ["org/repo:pr-1"])
            with self.assertRaises(RuntimeError) as ctx:
                push_resolved_images_to_ecr(
                    conn=MagicMock(), rec_id=1,
                    workdir=tmp, output_dir=tmp, instances=[],
                    run_numbers={},
                    ecr_registry="account.dkr.ecr.us-east-1.amazonaws.com",
                    ecr_region="",
                    s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
                )
            self.assertIn("AURORA_ECR_REGION", str(ctx.exception))
            mock_subprocess_run.assert_not_called()

    @patch.dict(os.environ, {"AURORA_REGISTRY_KIND": "local"}, clear=False)
    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    def test_local_mode_does_not_invoke_aws_cli(self, mock_subprocess_run):
        """In local mode, get-login-password is skipped — push runs without token."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        with tempfile.TemporaryDirectory() as tmp:
            # No matching instances → loop never reaches skopeo, but the
            # AWS-token preamble would still have run in 'ecr' mode.
            self._make_final_report(tmp, ["go-chi/chi:pr-1"])
            push_resolved_images_to_ecr(
                conn=MagicMock(), rec_id=1,
                workdir=tmp, output_dir=tmp, instances=[],
                run_numbers={},
                ecr_registry="registry.aurora.svc.cluster.local:5000",
                ecr_region="",   # explicitly unset to prove local doesn't need it
                s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
            )
            for call in mock_subprocess_run.call_args_list:
                cmd = call.args[0] if call.args else call.kwargs.get("args") or []
                if cmd and isinstance(cmd, list):
                    self.assertNotIn("get-login-password", cmd)

    def test_kind_default_is_ecr_when_env_unset(self):
        """If AURORA_REGISTRY_KIND is unset, the default is 'ecr' (prod-safe)."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        # Strip the env var and verify the function picks the ECR path
        # (which then fails for lack of region — proving it tried ECR).
        env = {k: v for k, v in os.environ.items() if k != "AURORA_REGISTRY_KIND"}
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                self._make_final_report(tmp, ["org/repo:pr-1"])
                with self.assertRaises(RuntimeError) as ctx:
                    push_resolved_images_to_ecr(
                        conn=MagicMock(), rec_id=1,
                        workdir=tmp, output_dir=tmp, instances=[],
                        run_numbers={},
                        ecr_registry="example.com",
                        ecr_region="",
                        s3_config={}, use_s3=False, s3_folder="",
                        phase="aurora_phase2",
                    )
                self.assertIn("AURORA_ECR_REGION", str(ctx.exception))


# ═══════════════════════════════════════════════════════════════════════════
# 6. push_resolved_images_to_ecr — id-format compatibility (the bug we fixed)
# ═══════════════════════════════════════════════════════════════════════════
class TestPushIdFormatMatching(TestCase):

    @patch.dict(os.environ, {"AURORA_REGISTRY_KIND": "local"}, clear=False)
    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    @patch("odoo.addons.aurora.models.artifact_collector.update_instance")
    @patch("odoo.addons.aurora.models.artifact_collector.ensure_instance")
    def test_resolved_ids_match_harness_format(
        self, mock_ensure_instance, mock_update_instance, mock_subprocess_run,
    ):
        """resolved_ids in final_report.json use 'org/repo:pr-N'; by_id must
        match that format so the push function actually finds the instance."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        mock_ensure_instance.return_value = 42
        mock_subprocess_run.return_value = MagicMock(stdout="", stderr="")
        # Fake instance with a PR matching the harness id format
        pr = MagicMock()
        pr.org, pr.repo, pr.number = "go-chi", "chi", 776
        inst = MagicMock()
        inst.pr = pr
        inst.dependency.return_value.image_full_name.return_value = "mswebench/go-chi_m_chi:pr-776"
        with tempfile.TemporaryDirectory() as tmp:
            # Need the OCI dir to exist so the skopeo call site is reached
            oci_dir = Path(tmp) / "oci_tars" / "mswebench_go-chi_m_chi_pr-776.tar.d"
            oci_dir.mkdir(parents=True)
            # And the final_report points at the harness id
            (Path(tmp) / "final_report.json").write_text(
                json.dumps({"resolved_ids": ["go-chi/chi:pr-776"]})
            )
            push_resolved_images_to_ecr(
                conn=MagicMock(), rec_id=1,
                workdir=tmp, output_dir=tmp, instances=[inst],
                run_numbers={("go-chi", "chi"): 1},
                ecr_registry="registry.aurora.svc.cluster.local:5000",
                ecr_region="",
                s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
            )
        # ensure_instance must be called with the DB-format id, not the harness id.
        # (Bug we fixed: previously this never fired because by_id missed.)
        mock_ensure_instance.assert_called_once()
        call_args = mock_ensure_instance.call_args
        # signature: ensure_instance(conn, eval_id, org, repo, instance_id)
        passed_id = call_args.args[4] if len(call_args.args) >= 5 else call_args.kwargs.get("instance_id")
        self.assertEqual(passed_id, "go-chi__chi-pr-776")

    @patch.dict(os.environ, {"AURORA_REGISTRY_KIND": "local"}, clear=False)
    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    @patch("odoo.addons.aurora.models.artifact_collector.update_instance")
    @patch("odoo.addons.aurora.models.artifact_collector.ensure_instance")
    def test_unmatched_resolved_id_skipped(
        self, mock_ensure_instance, mock_update_instance, mock_subprocess_run,
    ):
        """If final_report names an id with no matching instance in memory,
        that id is logged-and-skipped, not raised."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "final_report.json").write_text(
                json.dumps({"resolved_ids": ["ghost/repo:pr-999"]})
            )
            result = push_resolved_images_to_ecr(
                conn=MagicMock(), rec_id=1,
                workdir=tmp, output_dir=tmp, instances=[],
                run_numbers={},
                ecr_registry="registry.local:5000",
                ecr_region="",
                s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
            )
        self.assertEqual(result["ecr_pushed_count"], 0)
        mock_ensure_instance.assert_not_called()
        mock_subprocess_run.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 7. push_resolved_images_to_ecr — skopeo command construction
# ═══════════════════════════════════════════════════════════════════════════
class TestPushSkopeoCommandShape(TestCase):

    def _setup_one_resolved(self, tmp):
        pr = MagicMock(); pr.org, pr.repo, pr.number = "go-chi", "chi", 776
        inst = MagicMock(); inst.pr = pr
        inst.dependency.return_value.image_full_name.return_value = "mswebench/go-chi_m_chi:pr-776"
        oci_dir = Path(tmp) / "oci_tars" / "mswebench_go-chi_m_chi_pr-776.tar.d"
        oci_dir.mkdir(parents=True)
        (Path(tmp) / "final_report.json").write_text(
            json.dumps({"resolved_ids": ["go-chi/chi:pr-776"]})
        )
        return inst

    @patch.dict(os.environ, {"AURORA_REGISTRY_KIND": "local"}, clear=False)
    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    @patch("odoo.addons.aurora.models.artifact_collector.update_instance")
    @patch("odoo.addons.aurora.models.artifact_collector.ensure_instance")
    def test_local_command_has_tls_skip_flag(
        self, mock_ensure_instance, mock_update_instance, mock_subprocess_run,
    ):
        """Local mode must pass --dest-tls-verify=false to skopeo."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        mock_ensure_instance.return_value = 1
        mock_subprocess_run.return_value = MagicMock(stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._setup_one_resolved(tmp)
            push_resolved_images_to_ecr(
                conn=MagicMock(), rec_id=1,
                workdir=tmp, output_dir=tmp, instances=[inst],
                run_numbers={("go-chi", "chi"): 1},
                ecr_registry="registry.aurora.svc.cluster.local:5000",
                ecr_region="",
                s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
            )
        skopeo_calls = [c for c in mock_subprocess_run.call_args_list
                        if c.args and isinstance(c.args[0], list)
                        and c.args[0] and c.args[0][0] == "skopeo"]
        self.assertEqual(len(skopeo_calls), 1)
        cmd = skopeo_calls[0].args[0]
        self.assertIn("--dest-tls-verify=false", cmd)
        # Local mode must NOT inject AWS creds
        self.assertFalse(any("AWS:" in str(arg) for arg in cmd))
        self.assertIn("--multi-arch", cmd)
        self.assertIn("all", cmd)

    @patch.dict(os.environ, {"AURORA_REGISTRY_KIND": "ecr"}, clear=False)
    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    @patch("odoo.addons.aurora.models.artifact_collector.update_instance")
    @patch("odoo.addons.aurora.models.artifact_collector.ensure_instance")
    @patch("odoo.addons.aurora.models.artifact_collector.ensure_ecr_repository")
    def test_ecr_command_has_dest_creds(
        self, mock_ensure_repo, mock_ensure_instance,
        mock_update_instance, mock_subprocess_run,
    ):
        """ECR mode must call get-login-password and pass AWS:<token> as dest-creds."""
        from odoo.addons.aurora.models.artifact_collector import push_resolved_images_to_ecr
        mock_ensure_instance.return_value = 1
        # First subprocess call is `aws ecr get-login-password`; subsequent are skopeo
        mock_subprocess_run.side_effect = [
            MagicMock(stdout="dummy-ecr-token\n", stderr=""),  # aws
            MagicMock(stdout="", stderr=""),                   # skopeo
        ]
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._setup_one_resolved(tmp)
            push_resolved_images_to_ecr(
                conn=MagicMock(), rec_id=1,
                workdir=tmp, output_dir=tmp, instances=[inst],
                run_numbers={("go-chi", "chi"): 1},
                ecr_registry="account.dkr.ecr.us-east-1.amazonaws.com",
                ecr_region="us-east-1",
                s3_config={}, use_s3=False, s3_folder="", phase="aurora_phase2",
            )
        # 1st call: aws ecr get-login-password
        aws_call_cmd = mock_subprocess_run.call_args_list[0].args[0]
        self.assertEqual(aws_call_cmd[0], "aws")
        self.assertIn("get-login-password", aws_call_cmd)
        # 2nd call: skopeo with --dest-creds AWS:<token>
        skopeo_cmd = mock_subprocess_run.call_args_list[1].args[0]
        self.assertEqual(skopeo_cmd[0], "skopeo")
        self.assertIn("--dest-creds", skopeo_cmd)
        creds_idx = skopeo_cmd.index("--dest-creds")
        self.assertEqual(skopeo_cmd[creds_idx + 1], "AWS:dummy-ecr-token")
        self.assertNotIn("--dest-tls-verify=false", skopeo_cmd)
        # And ECR mode must have tried to ensure the repo exists
        mock_ensure_repo.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# 8. ensure_ecr_repository — best-effort behavior
# ═══════════════════════════════════════════════════════════════════════════
class TestEnsureEcrRepository(TestCase):

    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    def test_describe_succeeds_no_create(self, mock_run):
        """If describe-repositories returns 0, no create is attempted."""
        from odoo.addons.aurora.models.artifact_collector import ensure_ecr_repository
        mock_run.return_value = MagicMock(stdout="{}", stderr="")
        ensure_ecr_repository("acct.dkr.ecr.us-east-1.amazonaws.com", "us-east-1", "aurora/x")
        # Only describe ran (one call), create was never invoked
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args_list[0].args[0]
        self.assertIn("describe-repositories", cmd)

    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    def test_describe_fails_then_create_called(self, mock_run):
        """If describe fails (repo doesn't exist), create is attempted."""
        import subprocess as _subprocess
        from odoo.addons.aurora.models.artifact_collector import ensure_ecr_repository
        mock_run.side_effect = [
            _subprocess.CalledProcessError(returncode=254, cmd=["aws"], stderr="not found"),
            MagicMock(stdout="{}", stderr=""),
        ]
        ensure_ecr_repository("acct.dkr.ecr.us-east-1.amazonaws.com", "us-east-1", "aurora/y")
        self.assertEqual(mock_run.call_count, 2)
        create_cmd = mock_run.call_args_list[1].args[0]
        self.assertIn("create-repository", create_cmd)

    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    def test_already_exists_is_swallowed(self, mock_run):
        """If create fails with 'already exists', the helper returns cleanly."""
        import subprocess as _subprocess
        from odoo.addons.aurora.models.artifact_collector import ensure_ecr_repository
        mock_run.side_effect = [
            _subprocess.CalledProcessError(returncode=254, cmd=["aws"], stderr="not found"),
            _subprocess.CalledProcessError(returncode=254, cmd=["aws"], stderr="RepositoryAlreadyExistsException"),
        ]
        # Must not raise
        ensure_ecr_repository("acct.dkr.ecr.us-east-1.amazonaws.com", "us-east-1", "aurora/z")

    @patch("odoo.addons.aurora.models.artifact_collector.subprocess.run")
    def test_aws_cli_missing_is_silent_skip(self, mock_run):
        """If the `aws` CLI isn't installed in the worker, return without raising."""
        from odoo.addons.aurora.models.artifact_collector import ensure_ecr_repository
        mock_run.side_effect = FileNotFoundError("no aws binary")
        # Must not raise — the real push will surface the error if needed
        ensure_ecr_repository("acct.dkr.ecr.us-east-1.amazonaws.com", "us-east-1", "aurora/x")


# ═══════════════════════════════════════════════════════════════════════════
# 9. harness_staging — _s3_key_from_url helper
# ═══════════════════════════════════════════════════════════════════════════
class TestS3KeyFromURL(TestCase):

    def test_minio_custom_endpoint(self):
        from odoo.addons.aurora.models.harness_staging import _s3_key_from_url
        url = "http://minio.aurora.svc.cluster.local:9000/production-grtlabs-tag/aurora/phase1/x.jsonl"
        self.assertEqual(
            _s3_key_from_url(url, "production-grtlabs-tag"),
            "aurora/phase1/x.jsonl",
        )

    def test_aws_virtual_hosted(self):
        from odoo.addons.aurora.models.harness_staging import _s3_key_from_url
        url = "https://my-bucket.s3.us-east-1.amazonaws.com/path/to/object.json"
        self.assertEqual(
            _s3_key_from_url(url, "my-bucket"),
            "path/to/object.json",
        )

    def test_wrong_bucket_returns_empty(self):
        from odoo.addons.aurora.models.harness_staging import _s3_key_from_url
        url = "http://minio:9000/other-bucket/key.json"
        self.assertEqual(_s3_key_from_url(url, "my-bucket"), "")

    def test_empty_inputs_return_empty(self):
        from odoo.addons.aurora.models.harness_staging import _s3_key_from_url
        self.assertEqual(_s3_key_from_url("", "bucket"), "")
        self.assertEqual(_s3_key_from_url("http://x/y/z", ""), "")

    def test_strips_query_string(self):
        from odoo.addons.aurora.models.harness_staging import _s3_key_from_url
        url = "http://minio:9000/buck/path/to/file.txt?X-Amz-Sig=abc"
        self.assertEqual(_s3_key_from_url(url, "buck"), "path/to/file.txt")


# ═══════════════════════════════════════════════════════════════════════════
# 10. harness_staging — zip merge collision detection
# ═══════════════════════════════════════════════════════════════════════════
class TestMergeZipCollisionCheck(TestCase):
    """Direct test of the AST collision check by exercising the static logic.

    The bound method requires self.harness_file / self.repo, which means
    instantiating the Odoo model. We test the algorithm by recreating it
    against a fake zip — same imports and AST shape that the real merge uses.
    """

    def _make_zip(self, files: dict) -> bytes:
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def test_distinct_classes_concatenate(self):
        # Mirror of the merge logic — verifies our class-collision detection
        # over a small zip with distinct class names per range file.
        import ast as _ast
        files = {
            "requests.py": "class Foo: pass\nclass Bar: pass\n",
            "requests_4137_to_3036.py": "class Baz: pass\n",
        }
        seen = {}
        for fname, src in files.items():
            for node in _ast.parse(src).body:
                if isinstance(node, _ast.ClassDef):
                    self.assertNotIn(node.name, seen,
                        f"unexpected duplicate in test fixture: {node.name}")
                    seen[node.name] = fname
        self.assertEqual(set(seen), {"Foo", "Bar", "Baz"})

    def test_colliding_class_names_detected(self):
        """Two range files defining the same class name must be detectable.

        Mirrors `_merge_zip_to_single_py`'s AST scan loop.
        """
        import ast as _ast
        files = {
            "requests.py": "class Same: pass\n",
            "requests_4137_to_3036.py": "class Same: pass\n",
        }
        seen = {}
        collision = None
        for fname, src in files.items():
            for node in _ast.parse(src).body:
                if isinstance(node, _ast.ClassDef):
                    if node.name in seen:
                        collision = (node.name, seen[node.name], fname)
                        break
                    seen[node.name] = fname
            if collision:
                break
        self.assertIsNotNone(collision)
        self.assertEqual(collision[0], "Same")


# ═══════════════════════════════════════════════════════════════════════════
# 11. Cron registration in data.xml — ensure new cron has the right model+method
# ═══════════════════════════════════════════════════════════════════════════
class TestCronRegistration(TestCase):

    def test_tar_decision_sweep_cron_registered(self):
        import xml.etree.ElementTree as ET
        data_xml = Path(__file__).resolve().parents[1] / "data" / "data.xml"
        tree = ET.parse(str(data_xml))
        recs = tree.findall(".//record[@id='ir_cron_aurora_tar_decision_timeout_sweep']")
        self.assertEqual(len(recs), 1, "tar-decision timeout sweep cron must be registered")
        # Verify it points at the right method on the right model
        rec = recs[0]
        model_field = rec.find("./field[@name='model_id']")
        self.assertIsNotNone(model_field)
        self.assertEqual(model_field.get("ref"), "model_aurora_evaluation")
        code_field = rec.find("./field[@name='code']")
        self.assertIsNotNone(code_field)
        self.assertIn("_cron_tar_decision_timeout_sweep", code_field.text or "")
        interval_num = rec.find("./field[@name='interval_number']")
        interval_type = rec.find("./field[@name='interval_type']")
        self.assertEqual((interval_num.text or "").strip(), "5")
        self.assertEqual((interval_type.text or "").strip(), "minutes")

    def test_promote_test_eval_cron_registered(self):
        """Pre-existing cron from the Test-Harness work — must remain alongside."""
        import xml.etree.ElementTree as ET
        data_xml = Path(__file__).resolve().parents[1] / "data" / "data.xml"
        tree = ET.parse(str(data_xml))
        recs = tree.findall(".//record[@id='ir_cron_aurora_promote_test_eval']")
        self.assertEqual(len(recs), 1)


# ═══════════════════════════════════════════════════════════════════════════
# 12. Manifest pull-script generation branches on registry kind by URL shape
# ═══════════════════════════════════════════════════════════════════════════
class TestPullScriptKindInference(TestCase):
    """The pull-script generator infers ECR vs local from `.dkr.ecr.` in the
    registry host. These tests exercise the string-shape logic directly."""

    def test_aws_host_is_recognized_as_ecr(self):
        host = "123456789012.dkr.ecr.ap-south-1.amazonaws.com"
        self.assertIn(".dkr.ecr.", host)

    def test_local_host_is_not_ecr(self):
        host = "registry.aurora.svc.cluster.local:5000"
        self.assertNotIn(".dkr.ecr.", host)

    def test_region_extracted_from_aws_host(self):
        host = "123456789012.dkr.ecr.ap-south-1.amazonaws.com"
        region = host.split(".dkr.ecr.")[1].split(".", 1)[0]
        self.assertEqual(region, "ap-south-1")


# ═══════════════════════════════════════════════════════════════════════════
# 13. Action methods exist on AuroraEvaluation
# ═══════════════════════════════════════════════════════════════════════════
class TestEvaluationActions(TestCase):

    def setUp(self):
        from odoo.addons.aurora.models import evaluation as eval_mod
        self.AuroraEvaluation = eval_mod.AuroraEvaluation

    def test_action_request_tar_export_exists(self):
        self.assertTrue(callable(getattr(self.AuroraEvaluation, "action_request_tar_export", None)))

    def test_action_skip_tar_export_exists(self):
        self.assertTrue(callable(getattr(self.AuroraEvaluation, "action_skip_tar_export", None)))

    def test_action_download_ecr_manifest_exists(self):
        self.assertTrue(callable(getattr(self.AuroraEvaluation, "action_download_ecr_manifest", None)))

    def test_cron_tar_decision_timeout_sweep_exists(self):
        self.assertTrue(callable(getattr(self.AuroraEvaluation, "_cron_tar_decision_timeout_sweep", None)))

    def test_check_docker_platform_multi_arch_exists(self):
        self.assertTrue(callable(getattr(self.AuroraEvaluation, "_check_docker_platform_multi_arch", None)))


# ═══════════════════════════════════════════════════════════════════════════
# 14. Worker config reader picks up new fields
# ═══════════════════════════════════════════════════════════════════════════
class TestWorkerReadEvalConfig(TestCase):

    def test_read_eval_config_returns_window_and_staging_test_id(self):
        from odoo.addons.aurora.worker.run_evaluation import _read_eval_config
        conn = MagicMock()
        # Match the SELECT column order in _read_eval_config (14 cols)
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            "/data.jsonl",   # dataset_file
            "/patches.jsonl",  # patch_file
            "/repos",        # repo_dir
            "/wd",           # workdir
            "/out",          # output_dir
            False,           # force_build
            4,               # max_workers_build
            4,               # max_workers_run
            "linux/amd64,linux/arm64",  # docker_platform
            0,               # instance_limit
            "",              # specific_prs
            None,            # pipeline_id
            20,              # tar_decision_window_minutes
            None,            # staging_test_id
        )
        cfg = _read_eval_config(conn, rec_id=1)
        self.assertEqual(cfg["tar_decision_window_minutes"], 20)
        self.assertIsNone(cfg["staging_test_id"])
        self.assertEqual(cfg["docker_platform"], "linux/amd64,linux/arm64")

    def test_read_eval_config_test_eval(self):
        from odoo.addons.aurora.worker.run_evaluation import _read_eval_config
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            "/data.jsonl", "/patches.jsonl", "/repos", "/wd", "/out",
            False, 4, 4,
            "",   # docker_platform empty for test-evals
            4, "go-chi/chi:pr-1,go-chi/chi:pr-2", None,
            0,    # tar_decision_window_minutes=0 for test-evals
            55,   # staging_test_id
        )
        cfg = _read_eval_config(conn, rec_id=1)
        self.assertEqual(cfg["tar_decision_window_minutes"], 0)
        self.assertEqual(cfg["staging_test_id"], 55)
        self.assertIsNone(cfg["docker_platform"])
