import logging
import os

from odoo import api, fields, models

from .jaeger_repository import (
    _CRON_LOCK_AUTO_ADVANCE,
    _CRON_LOCK_POLL_EKS,
    _CRON_LOCK_RECONCILE_BUILDS,
    _CRON_LOCK_RECONCILE_SCRAPES,
    _CRON_LOCK_RECONCILE_TESTS,
    _CRON_LOCK_WATCHDOG_BUILDS,
    _CRON_LOCK_WATCHDOG_DATASET,
    _CRON_LOCK_WATCHDOG_SCRAPES,
    _CRON_LOCK_WATCHDOG_TESTS,
)

_logger = logging.getLogger(__name__)

_CRON_LOCK_WATCHDOG_TRAJECTORIES = 83927471
_CRON_LOCK_RECONCILE_TRAJECTORIES = 83927472


class JaegerRepositoryCrons(models.Model):
    _inherit = "jaeger.repository"

    # ── Cron Jobs ─────────────────────────────────────────────────────────

    @api.model
    def _cron_batch_scrape(self):
        """Disabled: RabbitMQ consumer was deleted. Use direct dispatch via action_collect_prs."""
        _logger.debug("_cron_batch_scrape is disabled (RabbitMQ consumer deleted)")
        return

    @api.model
    def _cron_batch_docker(self):
        """Disabled: RabbitMQ consumer was deleted. Use direct dispatch via action_build_docker_direct."""
        _logger.debug("_cron_batch_docker is disabled (RabbitMQ consumer deleted)")
        return

    @api.model
    def _cron_poll_eks_trajectories(self):
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_POLL_EKS,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._run_poll_eks_trajectories()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_POLL_EKS,))

    def _run_poll_eks_trajectories(self):
        running = self.search(
            [
                (
                    "trajectory_status",
                    "in",
                    ("dispatched", "running", "evaluating"),
                ),
            ],
            limit=500,
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
                limit=500,
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
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_AUTO_ADVANCE,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._run_auto_advance_stages()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_AUTO_ADVANCE,))

    def _run_auto_advance_stages(self):
        from psycopg2 import OperationalError as Psycopg2OpError

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
                limit=500,
            )
            for repo in repos:
                gate_ok, _ = repo._check_current_gate()
                if gate_ok:
                    next_stage = repo._next_stage()
                    if next_stage:
                        try:
                            self.env.cr.execute(
                                "SELECT id FROM jaeger_repository"
                                " WHERE id = %s FOR UPDATE NOWAIT",
                                [repo.id],
                            )
                        except Psycopg2OpError:
                            self.env.cr.rollback()
                            continue
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
        ], limit=500)
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

        # Stage 1 validation is synchronous — if crawl_status stuck in
        # "running" for >10 min, the process crashed mid-validation.
        crawl_cutoff = fields.Datetime.now() - timedelta(minutes=10)
        stale_validations = self.search([
            ("crawl_status", "=", "running"),
            ("write_date", "<", crawl_cutoff),
        ], limit=500)
        for repo in stale_validations:
            _logger.warning(
                "Watchdog: resetting stuck validation for %s (write_date %s)",
                repo.name, repo.write_date,
            )
            repo.write({
                "crawl_status": "failed",
                "error_message": "Watchdog: validation appears stuck (>10 minutes). Retry manually.",
            })
        if stale_validations:
            _logger.info("Watchdog: reset %d stuck validations", len(stale_validations))

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
        ], limit=500)
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
        """Safety net: recover from missed webhooks (OOM, node failure, network partition)."""
        active = self.search([
            ("pr_collection_status", "in", ["queued", "running"]),
        ], limit=500)
        if not active:
            return

        try:
            from kubernetes import client, config as k8s_config
        except ImportError:
            _logger.error(
                "Reconcile: kubernetes package not installed — "
                "cannot reconcile %d active scrape jobs", len(active),
            )
            return

        try:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config(
                    config_file=os.environ.get("KUBECONFIG") or None,
                )
            batch_v1 = client.BatchV1Api()
        except Exception as e:
            _logger.error(
                "Reconcile: K8s config failed — cannot reconcile %d active scrape jobs: %s",
                len(active), e,
            )
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

        try:
            jobs = batch_v1.list_namespaced_job(
                namespace=namespace,
                label_selector="platform=jaeger,app.kubernetes.io/name=jaeger-scrape",
                limit=500,
            )
        except Exception as e:
            _logger.error(
                "Reconcile: failed to list jobs in namespace '%s': %s",
                namespace, e,
            )
            return

        _logger.info(
            "Reconcile: found %d K8s jobs in namespace '%s', %d repos active",
            len(jobs.items), namespace, len(active),
        )

        job_map = {}
        for job in jobs.items:
            repo_id_label = job.metadata.labels.get("repo-id")
            if repo_id_label:
                job_map[repo_id_label] = job

        for repo in active:
            job = job_map.get(str(repo.id))
            if not job:
                queued_at = repo.scrape_queued_at or repo.write_date
                if (
                    queued_at
                    and (fields.Datetime.now() - queued_at).total_seconds() > 300
                ):
                    repo.write({
                        "pr_collection_status": "failed",
                        "error_message": "Job not found in cluster",
                    })
                    _logger.warning(
                        "Reconcile: %s marked failed (job not found, >5min)",
                        repo.name,
                    )
                continue

            if job.status.succeeded and job.status.succeeded > 0:
                if repo.pr_collection_status != "done":
                    if not repo.instance_ids:
                        try:
                            repo._recover_instances_from_s3()
                        except Exception as e:
                            queued_at = repo.scrape_queued_at or repo.write_date
                            age_minutes = (
                                (fields.Datetime.now() - queued_at).total_seconds() / 60
                                if queued_at else 999
                            )
                            if age_minutes > 120:
                                repo.write({
                                    "pr_collection_status": "failed",
                                    "error_message": (
                                        "S3 recovery failed after 2h (artifacts may be missing): %s"
                                        % str(e)[:1500]
                                    ),
                                })
                                _logger.warning(
                                    "Reconcile: %s marked failed (S3 recovery exhausted after 2h)",
                                    repo.name,
                                )
                            else:
                                _logger.warning(
                                    "Reconcile: %s instance recovery failed, "
                                    "will retry next run: %s",
                                    repo.name, e,
                                )
                            continue
                    vals = {
                        "pr_collection_status": "done",
                        "pr_collection_step": "",
                        "error_message": False,
                    }
                    try:
                        gate_ok, _ = repo._check_current_gate()
                        if gate_ok:
                            next_stage = repo._next_stage()
                            if next_stage:
                                vals["current_stage"] = next_stage
                    except Exception:
                        _logger.warning(
                            "Reconcile: %s stage advance failed (cron will catch up)",
                            repo.name, exc_info=True,
                        )
                    repo.write(vals)
                    _logger.info(
                        "Reconcile: %s marked done (K8s Job succeeded)", repo.name,
                    )
            elif job.status.failed and job.status.failed > 0:
                logs = ""
                try:
                    core_v1 = client.CoreV1Api()
                    pods = core_v1.list_namespaced_pod(
                        namespace=namespace,
                        label_selector=f"job-name=jaeger-scrape-{repo.id}",
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
                    "error_message": f"K8s Job failed (recovered by reconciliation).\n{logs}"[:2000],
                })
                _logger.warning(
                    "Reconcile: %s marked failed (K8s Job failed)", repo.name,
                )

    @api.model
    def _cron_watchdog_stale_tests(self):
        """Mark repos stuck in test_execution_status='running' for 4+ hours as failed."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_WATCHDOG_TESTS,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._run_watchdog_stale_tests()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_WATCHDOG_TESTS,))

    def _run_watchdog_stale_tests(self):
        from datetime import timedelta

        cutoff = fields.Datetime.now() - timedelta(hours=4)
        stale = self.search([
            ("test_execution_status", "=", "running"),
            ("write_date", "<", cutoff),
        ], limit=500)
        for repo in stale:
            _logger.warning(
                "Watchdog: marking %s test execution as failed (stuck >4 hours, last write %s)",
                repo.name, repo.write_date,
            )
            repo.write({
                "test_execution_status": "failed",
                "error_message": "Watchdog: test execution appears stuck (>4 hours). Retry manually.",
            })
        if stale:
            _logger.info("Watchdog: marked %d stale test executions as failed", len(stale))

    @api.model
    def _cron_watchdog_stale_dataset(self):
        """Mark repos stuck in dataset_status='generating' for 2+ hours as failed."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_WATCHDOG_DATASET,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._run_watchdog_stale_dataset()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_WATCHDOG_DATASET,))

    def _run_watchdog_stale_dataset(self):
        from datetime import timedelta

        cutoff = fields.Datetime.now() - timedelta(hours=2)
        stale = self.search([
            ("dataset_status", "=", "generating"),
            ("write_date", "<", cutoff),
        ], limit=500)
        for repo in stale:
            _logger.warning(
                "Watchdog: marking %s dataset finalization as failed (stuck >2 hours, last write %s)",
                repo.name, repo.write_date,
            )
            repo.write({
                "dataset_status": "failed",
                "error_message": "Watchdog: dataset finalization appears stuck (>2 hours). Retry manually.",
            })
        if stale:
            _logger.info("Watchdog: marked %d stale dataset finalizations as failed", len(stale))

    @api.model
    def _cron_reconcile_build_jobs(self):
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_RECONCILE_BUILDS,))
        if not cr.fetchone()[0]:
            return
        try:
            self._run_reconcile_build_jobs()
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_RECONCILE_BUILDS,))

    def _run_reconcile_build_jobs(self):
        repos = self.search([
            ("docker_build_status", "in", ("queued", "building")),
        ], limit=500)
        if not repos:
            return

        try:
            from kubernetes import client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                config_file = os.environ.get("KUBECONFIG")
                k8s_config.load_kube_config(config_file=config_file if config_file else None)
            batch_v1 = client.BatchV1Api()
        except (ImportError, Exception) as e:
            _logger.warning("Cannot connect to K8s for build reconciliation: %s", e)
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=5)

        for repo in repos:
            try:
                job_name = "jaeger-phase2-%s" % repo.id
                try:
                    job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
                except client.ApiException as e:
                    if e.status == 404:
                        if repo.build_queued_at and repo.build_queued_at < cutoff:
                            repo.write({
                                "docker_build_status": "failed",
                                "error_message": "Phase2 Job not found in K8s (may have been deleted)",
                            })
                        continue
                    raise

                if job.status.succeeded and job.status.succeeded > 0:
                    if repo.docker_build_status != "done":
                        built = len(repo.instance_ids.filtered(
                            lambda i: i.docker_build_status == "built",
                        ))
                        failed_inst = len(repo.instance_ids.filtered(
                            lambda i: i.docker_build_status == "failed",
                        ))
                        vals = {
                            "docker_build_status": "done",
                            "docker_build_progress": 100.0,
                            "images_built_count": built,
                            "images_failed_count": failed_inst,
                            "error_message": False,
                        }
                        if built > 0:
                            vals["current_stage"] = "stage4"
                            vals["test_execution_status"] = "running"
                        _logger.info(
                            "Reconciler: phase2 Job %s succeeded — %d built, %d failed",
                            job_name, built, failed_inst,
                        )
                        repo.write(vals)
                elif job.status.failed and job.status.failed > 0:
                    repo.write({
                        "docker_build_status": "failed",
                        "error_message": "Phase2 Job failed (K8s reported failure)",
                    })
                elif job.status.active:
                    if repo.docker_build_status == "queued":
                        repo.write({"docker_build_status": "building"})
            except Exception as e:
                _logger.error("Build reconcile error for %s: %s", repo.name, e)

    @api.model
    def _cron_reconcile_test_jobs(self):
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_RECONCILE_TESTS,))
        if not cr.fetchone()[0]:
            return
        try:
            self._run_reconcile_test_jobs()
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_RECONCILE_TESTS,))

    def _run_reconcile_test_jobs(self):
        repos = self.search([
            ("test_execution_status", "in", ("queued", "running")),
        ], limit=500)
        if not repos:
            return

        try:
            from kubernetes import client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                config_file = os.environ.get("KUBECONFIG")
                k8s_config.load_kube_config(config_file=config_file if config_file else None)
            batch_v1 = client.BatchV1Api()
        except (ImportError, Exception) as e:
            _logger.warning("Cannot connect to K8s for test reconciliation: %s", e)
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=5)

        for repo in repos:
            try:
                job_name = "jaeger-test-%s" % repo.id
                try:
                    job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
                except client.ApiException as e:
                    if e.status == 404:
                        if repo.test_queued_at and repo.test_queued_at < cutoff:
                            repo.write({
                                "test_execution_status": "failed",
                                "error_message": "Test Job not found in K8s (may have been deleted)",
                            })
                        continue
                    raise

                if job.status.succeeded and job.status.succeeded > 0:
                    if repo.test_execution_status != "done":
                        # Recover instance counts from DB (webhook was missed)
                        valid = len(repo.instance_ids.filtered(lambda i: i.is_valid))
                        invalid = len(repo.instance_ids.filtered(
                            lambda i: i.report_json and not i.is_valid,
                        ))
                        error = len(repo.instance_ids.filtered(
                            lambda i: i.validation_error and "error" in (i.validation_error or "").lower(),
                        ))
                        vals = {
                            "test_execution_status": "done",
                            "test_execution_progress": 100.0,
                            "instances_valid_count": valid,
                            "instances_invalid_count": invalid,
                            "instances_error_count": error,
                            "instances_tested_count": valid + invalid + error,
                            "error_message": False,
                        }
                        if valid == 0 and (invalid + error) > 0:
                            vals["terminal_state"] = "no_valid_instances"
                            vals["error_message"] = (
                                "All %d tested instances are invalid "
                                "(recovered by test reconciler)" % (invalid + error)
                            )
                        if valid > 0:
                            try:
                                gate_ok, _ = repo._check_current_gate()
                                if gate_ok:
                                    vals["current_stage"] = "stage5"
                            except Exception:
                                _logger.warning(
                                    "Reconciler: %s stage advance failed (cron will catch up)",
                                    repo.name, exc_info=True,
                                )
                        repo.write(vals)
                        _logger.info(
                            "Reconciler: test Job %s succeeded — %d valid, %d invalid, %d error",
                            job_name, valid, invalid, error,
                        )
                elif job.status.failed and job.status.failed > 0:
                    repo.write({
                        "test_execution_status": "failed",
                        "error_message": "Test Job failed (K8s reported failure)",
                    })
                elif job.status.active:
                    if repo.test_execution_status == "queued":
                        repo.write({"test_execution_status": "running"})
            except Exception as e:
                _logger.error("Test reconcile error for %s: %s", repo.name, e)

    @api.model
    def _cron_watchdog_stale_trajectories(self):
        """Mark trajectory jobs as failed if no heartbeat for 2 hours."""
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_WATCHDOG_TRAJECTORIES,))
        if not cr.fetchone()[0]:
            return
        try:
            self._run_watchdog_stale_trajectories()
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_WATCHDOG_TRAJECTORIES,))

    def _run_watchdog_stale_trajectories(self):
        from datetime import timedelta

        cutoff = fields.Datetime.now() - timedelta(hours=2)
        stale = self.search([
            ("trajectory_status", "in", ("dispatched", "running")),
            "|",
            ("last_heartbeat", "=", False),
            ("last_heartbeat", "<", cutoff),
        ], limit=500)
        for repo in stale:
            _logger.warning(
                "Watchdog: marking %s trajectory as failed (stale heartbeat)",
                repo.name,
            )
            repo.write({
                "trajectory_status": "failed",
                "error_message": "Watchdog: trajectory job stale — no heartbeat for 2+ hours.",
            })
            repo._append_log("Watchdog: trajectory job marked failed (stale heartbeat)")
        if stale:
            _logger.info("Watchdog: marked %d stale trajectory jobs as failed", len(stale))

    @api.model
    def _cron_reconcile_trajectory_jobs(self):
        """Reconcile trajectory K8s Jobs (check if completed/failed)."""
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", (_CRON_LOCK_RECONCILE_TRAJECTORIES,))
        if not cr.fetchone()[0]:
            return
        try:
            self._run_reconcile_trajectory_jobs()
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", (_CRON_LOCK_RECONCILE_TRAJECTORIES,))

    def _run_reconcile_trajectory_jobs(self):
        repos = self.search([
            ("trajectory_status", "in", ("dispatched", "running")),
        ], limit=500)
        if not repos:
            return

        try:
            from kubernetes import client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                config_file = os.environ.get("KUBECONFIG")
                k8s_config.load_kube_config(config_file=config_file if config_file else None)
            batch_v1 = client.BatchV1Api()
        except (ImportError, Exception) as e:
            _logger.warning("Cannot connect to K8s for trajectory reconciliation: %s", e)
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

        for repo in repos:
            try:
                job_name = f"jaeger-traj-{repo.id}"
                try:
                    job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
                except client.ApiException as e:
                    if e.status == 404:
                        queued_at = repo.write_date
                        if (
                            queued_at
                            and (fields.Datetime.now() - queued_at).total_seconds() > 600
                        ):
                            repo.write({
                                "trajectory_status": "failed",
                                "error_message": "Trajectory Job not found in K8s (>10 min since dispatch)",
                            })
                        continue
                    raise

                if job.status.succeeded and job.status.succeeded > 0:
                    if repo.trajectory_status != "done":
                        _logger.info(
                            "Reconciler: trajectory Job %s succeeded but status is %s",
                            job_name, repo.trajectory_status,
                        )
                elif job.status.failed and job.status.failed > 0:
                    if repo.trajectory_status != "failed":
                        repo.write({
                            "trajectory_status": "failed",
                            "error_message": "Trajectory Job failed (K8s reported failure)",
                        })
                elif job.status.active:
                    if repo.trajectory_status == "dispatched":
                        repo.write({"trajectory_status": "running"})
            except Exception as e:
                _logger.error("Trajectory reconcile error for %s: %s", repo.name, e)
