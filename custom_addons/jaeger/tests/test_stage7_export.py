import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestMetaExportPreconditions(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/metaorg/metarepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage7",
            "delivery_status": "pending",
        })

    def test_export_requires_stage7(self):
        self.repo.write({"current_stage": "stage6"})
        with self.assertRaises(UserError):
            self.repo.action_export_meta_direct()
        self.repo.write({"current_stage": "stage7"})

    def test_export_blocks_if_converting(self):
        self.repo.write({"delivery_status": "converting"})
        with self.assertRaises(UserError):
            self.repo.action_export_meta_direct()
        self.repo.write({"delivery_status": "pending"})

    def test_export_allowed_when_pending(self):
        self.repo.write({"delivery_status": "pending"})
        with patch.object(type(self.repo), "run_meta_export", return_value=None):
            with patch(
                "odoo.addons.jaeger.models.jaeger_repository.JaegerRepository._run_pipeline_async",
                return_value=None,
            ):
                self.repo.action_export_meta_direct()


class TestTrajectoryRunModel(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/runorg/runrepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
        })
        cls.inst = cls.env["jaeger.instance"].create({
            "name": "runorg__runrepo-1",
            "repository_id": cls.repo.id,
            "org": "runorg",
            "repo": "runrepo",
            "pr_number": 1,
        })

    def test_create_run_record(self):
        Run = self.env["jaeger.trajectory.run"]
        run = Run.create({
            "name": "runorg__runrepo-1-run-1",
            "instance_id": self.inst.id,
            "repository_id": self.repo.id,
            "run_number": 1,
            "model": "claude",
            "status": "queued",
        })
        self.assertTrue(run.exists())
        self.assertEqual(run.run_number, 1)

    def test_resolved_boolean_independent_of_status(self):
        Run = self.env["jaeger.trajectory.run"]
        run = Run.create({
            "name": "runorg__runrepo-1-run-2",
            "instance_id": self.inst.id,
            "repository_id": self.repo.id,
            "run_number": 2,
            "model": "claude",
            "status": "error",
            "resolved": True,
        })
        self.assertTrue(run.resolved)
        self.assertEqual(run.status, "error")


class TestPassAtKComputation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/passorg/passrepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage6",
            "k_runs": 8,
        })
        cls.inst = cls.env["jaeger.instance"].create({
            "name": "passorg__passrepo-1",
            "repository_id": cls.repo.id,
            "org": "passorg",
            "repo": "passrepo",
            "pr_number": 1,
        })

    def _create_runs(self, resolved_count, total=8):
        Run = self.env["jaeger.trajectory.run"]
        Run.search([("repository_id", "=", self.repo.id)]).unlink()
        for i in range(1, total + 1):
            Run.create({
                "name": f"passorg__passrepo-1-run-{i}",
                "instance_id": self.inst.id,
                "repository_id": self.repo.id,
                "run_number": i,
                "model": "claude",
                "status": "resolved" if i <= resolved_count else "unresolved",
                "resolved": i <= resolved_count,
            })

    def test_all_resolved_returns_1(self):
        self._create_runs(8)
        self.repo._summarize_trajectories()
        self.assertAlmostEqual(self.repo.pass_at_k, 1.0)

    def test_none_resolved_returns_0(self):
        self._create_runs(0)
        self.repo._summarize_trajectories()
        self.assertAlmostEqual(self.repo.pass_at_k, 0.0)

    def test_single_run_resolved(self):
        self.repo.write({"k_runs": 1})
        self._create_runs(1, total=1)
        self.repo._summarize_trajectories()
        self.assertAlmostEqual(self.repo.pass_at_k, 1.0)
        self.repo.write({"k_runs": 8})

    def test_single_run_unresolved(self):
        self.repo.write({"k_runs": 1})
        self._create_runs(0, total=1)
        self.repo._summarize_trajectories()
        self.assertAlmostEqual(self.repo.pass_at_k, 0.0)
        self.repo.write({"k_runs": 8})
