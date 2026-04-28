import logging
import os

from odoo import api, fields, models

from .jaeger_repository import (
    _CRON_LOCK_RECONCILE_SCRAPES,
    _CRON_LOCK_WATCHDOG_BUILDS,
    _CRON_LOCK_WATCHDOG_SCRAPES,
)

_logger = logging.getLogger(__name__)


class JaegerRepositoryCrons(models.Model):
    _inherit = "jaeger.repository"

    # ── Cron Jobs ─────────────────────────────────────────────────────────

    @api.model
    def _cron_batch_scrape(self):
        pending = self.search(
            [
                ("pr_collection_status", "=", "pending"),
                ("current_stage", "=", "stage2"),
            ],
            limit=500,
        )
        if not pending:
            return
        from ..services.rabbitmq_service import batch_publish_scrape_tasks

        batch_publish_scrape_tasks(pending.ids)
        pending.write({"pr_collection_status": "queued"})
        self.env["ir.cron"]._commit_progress(
            processed=len(pending),
            remaining=self.search_count(
                [
                    ("pr_collection_status", "=", "pending"),
                    ("current_stage", "=", "stage2"),
                ],
            ),
        )

    @api.model
    def _cron_batch_docker(self):
        pending = self.search(
            [
                ("docker_build_status", "=", "pending"),
                ("current_stage", "=", "stage3"),
            ],
            limit=200,
        )
        if not pending:
            return
        from ..services.rabbitmq_service import batch_publish_docker_tasks

        batch_publish_docker_tasks(pending.ids)
        pending.write({"docker_build_status": "queued"})
        self.env["ir.cron"]._commit_progress(
            processed=len(pending),
            remaining=self.search_count(
                [
                    ("docker_build_status", "=", "pending"),
                    ("current_stage", "=", "stage3"),
                ],
            ),
        )

    @api.model
    def _cron_poll_eks_trajectories(self):
        running = self.search(
            [
                (
                    "trajectory_status",
                    "in",
                    ("dispatched", "running", "evaluating"),
                ),
            ],
        )
        for repo in running:
            try:
                repo._poll_eks_status()
            except Exception as e:
                _logger.error("EKS poll error for %s: %s", repo.name, e)

    def _poll_eks_status(self):
        """Poll EKS for trajectory job status and update trajectory runs."""

        if not self.eks_job_id:
            return

        _logger.info("Polling EKS status for %s (job=%s)", self.name, self.eks_job_id)

        try:
            from kubernetes import client
            from kubernetes import config as k8s_config

            k8s_config.load_kube_config(
                config_file=os.environ.get("KUBECONFIG") or None,
            )
            batch_v1 = client.BatchV1Api()
        except (ImportError, Exception) as e:
            _logger.warning("Cannot connect to K8s for polling: %s", e)
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

        try:
            jobs = batch_v1.list_namespaced_job(
                namespace=namespace,
                label_selector=f"jaeger-job-id={self.eks_job_id}",
            )
        except Exception as e:
            _logger.error("Failed to list K8s jobs for %s: %s", self.eks_job_id, e)
            return

        Run = self.env["jaeger.trajectory.run"]
        completed_count = 0
        failed_count = 0
        running_count = 0

        for job in jobs.items:
            pod_name = job.metadata.name
            run = Run.search([("eks_pod_name", "=", pod_name)], limit=1)
            if not run:
                continue

            if job.status.succeeded and job.status.succeeded > 0:
                if run.status != "resolved":
                    run.write({"status": "resolved"})
                completed_count += 1
            elif job.status.failed and job.status.failed > 0:
                if run.status != "error":
                    run.write({"status": "error"})
                failed_count += 1
            elif job.status.active and job.status.active > 0:
                if run.status not in ("running", "evaluating"):
                    run.write({"status": "running"})
                running_count += 1

        total_runs = len(self.run_ids)
        done = completed_count + failed_count

        _logger.info(
            "EKS poll for %s: %d running, %d completed, %d failed, %d total",
            self.name, running_count, completed_count, failed_count, total_runs,
        )

        # Check if all runs are done
        if total_runs > 0 and done >= total_runs:
            self._summarize_trajectories()

    @api.model
    def _cron_auto_advance_stages(self):
        """Check gate conditions and auto-advance repos through stages."""
        for stage in [
            "stage1",
            "stage2",
            "stage3",
            "stage4",
            "stage5",
            "stage6",
            "stage7",
        ]:
            repos = self.search(
                [("current_stage", "=", stage), ("terminal_state", "=", "none")],
            )
            for repo in repos:
                gate_ok, _ = repo._check_current_gate()
                if gate_ok:
                    next_stage = repo._next_stage()
                    if next_stage:
                        repo.write({"current_stage": next_stage})
                        _logger.info(
                            "Auto-advanced %s from %s to %s",
                            repo.name,
                            stage,
                            next_stage,
                        )
                self.env["ir.cron"]._commit_progress(processed=1)

    @api.model
    def _cron_watchdog_stale_scrapes(self):
        """Mark repos stuck in 'running' with no heartbeat for 60+ min as failed."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_WATCHDOG_SCRAPES,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._run_watchdog_stale_scrapes()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_WATCHDOG_SCRAPES,))

    def _run_watchdog_stale_scrapes(self):
        from datetime import timedelta

        cutoff = fields.Datetime.now() - timedelta(minutes=60)
        stale = self.search([
            ("pr_collection_status", "=", "running"),
            "|",
            ("last_heartbeat", "=", False),
            ("last_heartbeat", "<", cutoff),
        ])
        for repo in stale:
            _logger.warning(
                "Watchdog: marking %s as failed (last heartbeat %s)",
                repo.name, repo.last_heartbeat,
            )
            repo.write({
                "pr_collection_status": "failed",
                "error_message": "Watchdog: pipeline appears stuck (no heartbeat for 60+ minutes).",
            })
        if stale:
            _logger.info("Watchdog: marked %d stale scrape jobs as failed", len(stale))

    @api.model
    def _cron_watchdog_stale_builds(self):
        """Reset repos stuck in 'building' for 2+ hours back to pending."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_WATCHDOG_BUILDS,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._run_watchdog_stale_builds()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_WATCHDOG_BUILDS,))

    def _run_watchdog_stale_builds(self):
        from datetime import timedelta

        cutoff = fields.Datetime.now() - timedelta(hours=2)
        stale = self.search([
            ("docker_build_status", "=", "building"),
            ("write_date", "<", cutoff),
        ])
        for repo in stale:
            _logger.warning(
                "Watchdog: resetting stuck build for %s (last write %s)",
                repo.name, repo.write_date,
            )
            stuck_instances = repo.instance_ids.filtered(
                lambda i: i.docker_build_status == "building",
            )
            stuck_instances.write({"docker_build_status": "pending"})
            repo.write({
                "docker_build_status": "pending",
                "error_message": "Watchdog: build appeared stuck for 2+ hours, reset to pending.",
            })
        if stale:
            _logger.info("Watchdog: reset %d stuck builds to pending", len(stale))

    @api.model
    def _cron_reconcile_scrape_jobs(self):
        """Check K8s Job status for running scrape pipelines (kaiju_build pattern)."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_RECONCILE_SCRAPES,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._run_reconcile_scrape_jobs()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_RECONCILE_SCRAPES,))

    def _run_reconcile_scrape_jobs(self):
        """Safety net for pods that crash without updating the database
        (OOM kill, node failure, network partition).
        """
        running = self.search([("pr_collection_status", "=", "running")])
        if not running:
            return

        try:
            from kubernetes import client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config(
                    config_file=os.environ.get("KUBECONFIG") or None,
                )
            batch_v1 = client.BatchV1Api()
        except ImportError:
            return
        except Exception as e:
            _logger.warning("K8s config not available for reconciliation: %s", e)
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

        for repo in running:
            job_name = f"jaeger-scrape-{repo.id}"
            try:
                job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
            except Exception:
                continue

            if job.status.succeeded and job.status.succeeded > 0:
                if repo.pr_collection_status != "done":
                    repo.write({"pr_collection_status": "done"})
                    _logger.info("Reconcile: %s marked done (K8s Job succeeded)", repo.name)
            elif job.status.failed and job.status.failed > 0:
                logs = ""
                try:
                    core_v1 = client.CoreV1Api()
                    pods = core_v1.list_namespaced_pod(
                        namespace=namespace,
                        label_selector=f"job-name={job_name}",
                    )
                    if pods.items:
                        pod_name = pods.items[-1].metadata.name
                        logs = core_v1.read_namespaced_pod_log(
                            name=pod_name, namespace=namespace, tail_lines=50,
                        )
                except Exception:
                    logs = "Could not retrieve pod logs"

                repo.write({
                    "pr_collection_status": "failed",
                    "error_message": f"K8s Job failed.\n{logs}"[:2000],
                })
                _logger.warning("Reconcile: %s marked failed (K8s Job failed)", repo.name)
