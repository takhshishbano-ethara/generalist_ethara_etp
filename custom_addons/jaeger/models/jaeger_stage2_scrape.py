import logging
import os
import threading
import time as _time
from datetime import datetime

from odoo import SUPERUSER_ID, api, models
from odoo.exceptions import UserError

from odoo.addons.jaeger.worker.pipeline_helpers import (
    _run_scrape_pipeline_standalone,
)

from .jaeger_repository import MAX_CONCURRENT_SCRAPES

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage2(models.Model):
    _inherit = "jaeger.repository"

    # ── Stage 1 Actions ──────────────────────────────────────────────────

    def action_validate_repo(self):
        self.ensure_one()
        if not self.repo_url:
            raise UserError("GitHub URL is required.")
        self.write({"crawl_status": "running"})
        try:
            self._validate_repo_metadata()
            self.write({"crawl_status": "done", "current_stage": "stage2"})
        except Exception as e:
            error_msg = str(e)[:2000]
            self.write(
                {
                    "crawl_status": "failed",
                    "error_message": error_msg,
                    "terminal_state": "repo_not_suitable",
                },
            )
            raise UserError(error_msg) from e

    def _validate_repo_metadata(self):
        from ..tools.github_token_pool import get_token_pool

        pool = get_token_pool(self.env)
        token = pool.get_token()

        from github import Auth, Github

        log_lines = []
        def _log(msg):
            line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
            log_lines.append(line)
            _logger.info("Validate %s/%s: %s", self.org, self.repo_name, msg)

        _log("Connecting to GitHub API...")
        g = Github(auth=Auth.Token(token))
        repo = g.get_repo(f"{self.org}/{self.repo_name}")
        _log(f"Repository found: {repo.full_name}")

        rate = g.get_rate_limit()
        pool.report_usage(token, rate.rate.remaining, rate.rate.reset.timestamp())
        _log(f"Rate limit: {rate.rate.remaining}/{rate.rate.limit} remaining")

        self.write({
            "stars": repo.stargazers_count,
            "forks": repo.forks_count,
            "is_fork": repo.fork,
            "repo_description": (repo.description or "")[:500],
            "log_output": "\n".join(log_lines) + "\n",
        })

    # ── Stage 2 Actions ──────────────────────────────────────────────────

    def action_collect_prs(self):
        self.ensure_one()

        if self.current_stage != "stage2":
            raise UserError("Repository must be in Stage 2.")
        if not self.org or not self.repo_name:
            raise UserError("GitHub URL is required and must be valid.")

        ICP = self.env["ir.config_parameter"].sudo()
        tokens = ICP.get_param("jaeger.github_tokens", "").strip()
        if not tokens:
            raise UserError(
                "No GitHub tokens configured. Go to Settings → Jaeger → GitHub Tokens.",
            )

        active_count = self.search_count([
            ("pr_collection_status", "in", ["queued", "running"]),
        ])
        if active_count >= MAX_CONCURRENT_SCRAPES:
            raise UserError(
                "Scrape queue is full (%d active jobs). Please try again shortly."
                % active_count,
            )

        from psycopg2 import OperationalError as Psycopg2OpError
        try:
            self.env.cr.execute(
                "SELECT pr_collection_status FROM jaeger_repository"
                " WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Psycopg2OpError:
            self.env.cr.rollback()
            raise UserError("PR collection is already being started by another user.")
        row = self.env.cr.fetchone()
        if row and row[0] in ("running", "queued"):
            raise UserError("PR collection is already in progress.")

        dispatch_mode = ICP.get_param("jaeger.dispatch_mode", "local")
        db_name = self.env.cr.dbname
        rec_id = self.id

        if dispatch_mode == "k8s":
            job_image = ICP.get_param("jaeger.k8s_job_image", "").strip()
            if not job_image:
                raise UserError(
                    "K8s Job image not configured. Go to Settings → Jaeger → K8s Dispatch.",
                )
            self.write({
                "pr_collection_status": "queued", "error_message": False,
                "cancel_requested": False, "log_output": "",
            })

            def _dispatch_k8s():
                try:
                    from odoo.orm.registry import Registry
                    with Registry(db_name).cursor() as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        repo = env["jaeger.repository"].browse(rec_id)
                        repo._create_scrape_k8s_job()
                except Exception as e:
                    _logger.exception("K8s Job creation failed for repo %s", rec_id)
                    try:
                        from odoo.orm.registry import Registry
                        with Registry(db_name).cursor() as cr:
                            env = api.Environment(cr, SUPERUSER_ID, {})
                            repo = env["jaeger.repository"].browse(rec_id)
                            repo.write({
                                "pr_collection_status": "failed",
                                "error_message": "K8s Job creation failed: %s" % str(e)[:1500],
                            })
                    except Exception:
                        _logger.exception("Failed to record K8s dispatch failure")

            self.env.cr.postcommit.add(_dispatch_k8s)
        else:
            self.write({
                "pr_collection_status": "running", "error_message": False,
                "cancel_requested": False, "log_output": "",
            })

            def _dispatch_local():
                t = threading.Thread(
                    target=_run_scrape_pipeline_standalone,
                    args=(db_name, rec_id),
                    daemon=True,
                    name=f"jaeger_scrape_{rec_id}",
                )
                t.start()

            self.env.cr.postcommit.add(_dispatch_local)

    def _create_scrape_k8s_job(self):
        """Create a K8s Job to run worker/run_pipeline.py (kaiju_build pattern)."""
        try:
            from kubernetes import client, config as k8s_config
        except ImportError:
            raise RuntimeError(
                "kubernetes package not installed. Required for K8s dispatch mode. "
                "pip install kubernetes"
            )

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            config_file = os.environ.get("KUBECONFIG")
            k8s_config.load_kube_config(
                config_file=config_file if config_file else None,
            )
        batch_v1 = client.BatchV1Api()

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")
        job_image = ICP.get_param("jaeger.k8s_job_image", "")
        s3_bucket = ICP.get_param("jaeger.s3_bucket", "")
        s3_region = ICP.get_param("jaeger.s3_region", "ap-south-1")
        s3_prefix = ICP.get_param("jaeger.s3_prefix", "jaeger/phase1")
        secret_name = ICP.get_param("jaeger.k8s_secret", "jaeger-secrets")
        configmap_name = ICP.get_param("jaeger.k8s_configmap", "jaeger-worker-config")
        db_name = self.env.cr.dbname
        sandbox = ICP.get_param("jaeger.sandbox_mode", "0") == "1"

        job_name = "jaeger-scrape-%s" % self.id
        env_vars = [
            client.V1EnvVar(name="REPO_ID", value=str(self.id)),
            client.V1EnvVar(name="ODOO_DB", value=db_name),
            client.V1EnvVar(name="JAEGER_S3_BUCKET", value=s3_bucket),
            client.V1EnvVar(name="JAEGER_S3_REGION", value=s3_region),
            client.V1EnvVar(name="JAEGER_S3_PREFIX", value=s3_prefix),
        ]

        if secret_name:
            for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"):
                env_vars.append(
                    client.V1EnvVar(
                        name=key,
                        value_from=client.V1EnvVarSource(
                            secret_key_ref=client.V1SecretKeySelector(
                                name=secret_name, key=key, optional=True,
                            ),
                        ),
                    ),
                )

        if sandbox:
            s3_endpoint = ICP.get_param("jaeger.s3_endpoint", "")
            if s3_endpoint:
                env_vars.append(
                    client.V1EnvVar(name="JAEGER_S3_ENDPOINT", value=s3_endpoint),
                )

        volumes = []
        volume_mounts = []
        if configmap_name:
            volumes.append(
                client.V1Volume(
                    name="odoo-config",
                    config_map=client.V1ConfigMapVolumeSource(name=configmap_name),
                ),
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="odoo-config", mount_path="/etc/odoo", read_only=True,
                ),
            )

        pod_spec_kwargs = {
            "restart_policy": "Never",
            "containers": [
                client.V1Container(
                    name="pipeline",
                    image=job_image,
                    image_pull_policy="Always",
                    command=[
                        "python",
                        "custom_addons/jaeger/worker/run_pipeline.py",
                    ],
                    env=env_vars,
                    volume_mounts=volume_mounts or None,
                    resources=client.V1ResourceRequirements(
                        requests={
                            "cpu": "500m",
                            "memory": "1Gi",
                            "ephemeral-storage": "5Gi",
                        },
                        limits={
                            "memory": "2Gi",
                            "ephemeral-storage": "10Gi",
                        },
                    ),
                ),
            ],
            "volumes": volumes or None,
        }

        if sandbox:
            pod_spec_kwargs["host_network"] = True
            pod_spec_kwargs["dns_policy"] = "None"
            pod_spec_kwargs["dns_config"] = client.V1PodDNSConfig(
                nameservers=["127.0.0.11"],
            )
        else:
            pod_spec_kwargs["service_account_name"] = "jaeger-pipeline-runner"
            pod_spec_kwargs["node_selector"] = {
                "kubernetes.io/arch": "amd64",
                "ethara.ai/node-pool": "general-purpose",
            }

        labels = {
            "app.kubernetes.io/name": "jaeger-scrape",
            "app.kubernetes.io/component": "pipeline",
            "repo-id": str(self.id),
            "platform": "jaeger",
        }
        if not sandbox:
            labels["kueue.x-k8s.io/queue-name"] = "jaeger-scraping"

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=namespace,
                labels=labels,
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=3600,
                backoff_limit=2,
                active_deadline_seconds=7200,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app.kubernetes.io/name": "jaeger-scrape",
                            "repo-id": str(self.id),
                            "platform": "jaeger",
                        },
                    ),
                    spec=client.V1PodSpec(**pod_spec_kwargs),
                ),
            ),
        )

        try:
            batch_v1.create_namespaced_job(namespace=namespace, body=job)
        except client.ApiException as e:
            if e.status == 409:
                _logger.warning(
                    "K8s Job %s already exists (409 Conflict) — deleting and recreating",
                    job_name,
                )
                batch_v1.delete_namespaced_job(
                    name=job_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Background"),
                )
                _time.sleep(2)
                batch_v1.create_namespaced_job(namespace=namespace, body=job)
            else:
                raise
        _logger.info("Created K8s Job %s for repo %s", job_name, self.name)

    def action_cancel_pipeline(self):
        self.ensure_one()
        active_statuses = ("running", "queued")
        is_active = (
            self.pr_collection_status in active_statuses
            or self.docker_build_status in active_statuses
            or self.test_execution_status in active_statuses
        )
        if not is_active:
            raise UserError("No active pipeline to cancel.")
        self.write({"cancel_requested": True})
        self._append_log("Cancellation requested by user.")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Cancel Requested",
                "message": "Pipeline will stop at the next step boundary.",
                "type": "warning",
                "sticky": False,
            },
        }

    def action_reset_to_docker_build(self):
        """Reset repo back to Stage 3 for rebuild after config change."""
        self.ensure_one()
        if self.test_execution_status in ("running", "queued"):
            raise UserError("Cannot reset while test execution is running.")
        if self.docker_build_status in ("building", "queued"):
            raise UserError("Cannot reset while Docker build is running.")

        self.write({
            "current_stage": "stage3",
            "docker_build_status": "pending",
            "base_image_status": "none",
            "base_image_name": False,
            "test_execution_status": "pending",
            "test_execution_progress": 0,
            "instances_tested_count": 0,
            "instances_valid_count": 0,
            "instances_invalid_count": 0,
            "instances_error_count": 0,
            "resolved_instances": 0,
            "unresolved_instances": 0,
            "dataset_status": "pending",
            "error_message": False,
            "terminal_state": "none",
        })
        self.instance_ids.write({
            "docker_build_status": "pending",
            "docker_image_name": False,
            "run_log": False,
            "test_patch_run_log": False,
            "fix_patch_run_log": False,
            "f2p_tests_json": False,
            "p2p_tests_json": False,
            "s2p_tests_json": False,
            "n2p_tests_json": False,
            "is_valid": False,
            "validation_error": False,
            "report_json": False,
            "run_result_json": False,
            "test_patch_result_json": False,
            "fix_patch_result_json": False,
        })
        self._append_log(
            "Reset to Docker Build stage (operator requested rebuild after config change)",
        )

    def action_collect_prs_direct(self):
        """Run PR collection in background thread (always local)."""
        self.ensure_one()
        if self.current_stage != "stage2":
            raise UserError("Repository must be in Stage 2.")
        if self.pr_collection_status in ("running", "queued"):
            raise UserError("PR collection is already in progress.")

        self.write({
            "pr_collection_status": "running", "error_message": False,
            "cancel_requested": False, "log_output": "",
        })

        db_name = self.env.cr.dbname
        rec_id = self.id

        def _dispatch_local():
            t = threading.Thread(
                target=_run_scrape_pipeline_standalone,
                args=(db_name, rec_id),
                daemon=True,
                name=f"jaeger_scrape_{rec_id}",
            )
            t.start()

        self.env.cr.postcommit.add(_dispatch_local)

    def run_scrape_pipeline(self):
        """Full Phase 1 scraping pipeline. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"pr_collection_status": "running", "error_message": False})
        self.env.cr.commit()

        ICP = self.env["ir.config_parameter"].sudo()
        tokens = [
            t.strip()
            for t in ICP.get_param("jaeger.github_tokens", "").split(",")
            if t.strip()
        ]
        from pathlib import Path

        output_dir = Path(ICP.get_param("jaeger.output_dir", "/tmp/jaeger_data"))
        out_dir = output_dir / f"{self.org}__{self.repo_name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            if self.pipeline_mode == "swe":
                raise UserError(
                    "SWE pipeline runs via standalone dispatch (action_collect_prs). "
                    "Use the Collect PRs button or the standalone function directly."
                )
            else:
                self._run_lht_pipeline(tokens, out_dir)

            vals = {"pr_collection_status": "done", "terminal_state": "none", "error_message": False}
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            self.write(
                {
                    "pr_collection_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            self._append_log("FAILED: %s" % e)
            self.env.cr.commit()
            raise

    def _run_lht_pipeline(self, tokens, out_dir):
        from ..tools.github_token_pool import GitHubTokenPool
        pool = GitHubTokenPool(tokens)

        self._append_log("Step 1/6: Fetching all pull requests...")
        self.write({"pr_collection_step": "Step 1/6: Fetching PRs...", "pr_collection_progress": 0})
        self.env.cr.commit()
        from ..tools.get_all_prs import main as get_all_prs

        get_all_prs(pool, out_dir, self.org, self.repo_name)
        prs_file = out_dir / f"{self.org}__{self.repo_name}_prs.jsonl"
        total_prs = self._count_jsonl_lines(prs_file)
        self.write({
            "prs_jsonl_path": str(prs_file),
            "total_prs_fetched": total_prs,
            "pr_collection_progress": 16,
            "pr_collection_step": f"Step 1/6 done: {total_prs} PRs fetched",
        })
        self.env.cr.commit()

        self._append_log("Step 2/6: Filtering PRs (LHT mode)...")
        self.write({"pr_collection_step": f"Step 2/6: Filtering {total_prs} PRs (LHT)...", "pr_collection_progress": 20})
        self.env.cr.commit()
        from ..tools.filter_prs import main as filter_prs

        def _lht_filter_progress(processed, total, passed):
            self.env.cr.execute(
                "SELECT cancel_requested FROM jaeger_repository WHERE id = %s",
                [self.id],
            )
            if self.env.cr.fetchone()[0]:
                raise UserError("Pipeline cancelled by user.")
            pct = 20 + (processed / total) * 13 if total else 20
            step_text = f"Step 2/6: Filtering PRs (LHT) — {processed}/{total} processed, {passed} passed"
            self.write({
                "pr_collection_step": step_text,
                "pr_collection_progress": round(pct, 1),
                "filtered_prs_count": passed,
            })
            self.env.cr.commit()

        filter_prs(
            pool,
            out_dir,
            prs_file,
            mode="lht",
            skip_commit_message=True,
            progress_callback=_lht_filter_progress,
        )
        lht_filtered = out_dir / f"{self.org}__{self.repo_name}_lht_filtered_prs.jsonl"
        filtered_fallback = out_dir / f"{self.org}__{self.repo_name}_filtered_prs.jsonl"
        filtered_file = lht_filtered if lht_filtered.exists() else filtered_fallback
        filtered_count = self._count_jsonl_lines(filtered_file)
        self.write({
            "filtered_prs_jsonl_path": str(filtered_file),
            "filtered_prs_count": filtered_count,
            "pr_collection_progress": 33,
            "pr_collection_step": f"Step 2/6 done: {filtered_count}/{total_prs} PRs passed filter",
        })
        self.env.cr.commit()

        self._append_log("Step 3/6: Fetching version tags...")
        self.write({"pr_collection_step": "Step 3/6: Fetching version tags...", "pr_collection_progress": 38})
        self.env.cr.commit()
        from ..tools.get_version_tags import main as get_version_tags

        get_version_tags(
            tokens, out_dir, self.org, self.repo_name,
            max_tags=self.max_tags or 200,
        )
        self.write({"pr_collection_progress": 50, "pr_collection_step": "Step 3/6 done: Tags fetched"})
        self.env.cr.commit()

        self._append_log("Step 4/6: Grouping PRs by version ranges...")
        self.write({"pr_collection_step": "Step 4/6: Grouping PRs by tags...", "pr_collection_progress": 55})
        self.env.cr.commit()
        from ..tools.group_prs_by_tags import main as group_prs_by_tags

        group_prs_by_tags(
            out_dir, self.org, self.repo_name,
            window_days=self.window_days or 30,
            tokens=tokens,
        )
        self.write({"pr_collection_progress": 66, "pr_collection_step": "Step 4/6 done: PRs grouped"})
        self.env.cr.commit()

        self._append_log("Step 5/6: Fetching related issues...")
        self.write({"pr_collection_step": "Step 5/6: Fetching related issues...", "pr_collection_progress": 70})
        self.env.cr.commit()
        from ..tools.get_related_issues import main as get_related_issues

        get_related_issues(pool, out_dir, filtered_file)
        self.write({"pr_collection_progress": 83, "pr_collection_step": "Step 5/6 done: Issues fetched"})
        self.env.cr.commit()

        self._append_log("Step 6/6: Building LHT dataset...")
        self.write({"pr_collection_step": "Step 6/6: Building LHT dataset...", "pr_collection_progress": 85})
        self.env.cr.commit()
        from ..tools.build_lht_dataset import main as build_lht_dataset

        build_lht_dataset(
            tokens, out_dir, self.org, self.repo_name, lang=self.language,
        )

        raw_dataset_file = (
            out_dir / f"{self.org}__{self.repo_name}_raw_dataset.jsonl"
        )
        raw_count = self._count_jsonl_lines(raw_dataset_file)
        self.write(
            {
                "raw_dataset_jsonl_path": str(raw_dataset_file),
                "raw_dataset_count": raw_count,
                "pr_collection_progress": 95,
                "pr_collection_step": f"Creating {raw_count} instances...",
            },
        )
        self.env.cr.commit()
        self._create_instances_from_dataset(raw_dataset_file)
        self.write({"pr_collection_progress": 100, "pr_collection_step": ""})

    def _create_instances_from_dataset(self, dataset_path):
        """Parse raw dataset JSONL and create jaeger.instance records."""
        import json

        MAX_PATCH_SIZE = 5 * 1024 * 1024
        MAX_BODY_SIZE = 100 * 1024

        Instance = self.env["jaeger.instance"]
        ResolvedIssue = self.env["jaeger.resolved.issue"]

        skipped = 0
        with open(dataset_path) as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                if len(line) > 10 * 1024 * 1024:
                    _logger.warning("Skipping line %d: exceeds 10MB", line_num)
                    skipped += 1
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as e:
                    _logger.warning("Skipping line %d: invalid JSON: %s", line_num, e)
                    skipped += 1
                    continue

                if len(data.get("fix_patch", "")) > MAX_PATCH_SIZE:
                    _logger.warning(
                        "Skipping PR #%s: fix_patch too large (%d bytes)",
                        data.get("number"), len(data["fix_patch"]),
                    )
                    skipped += 1
                    continue
                if len(data.get("test_patch", "")) > MAX_PATCH_SIZE:
                    _logger.warning(
                        "Skipping PR #%s: test_patch too large (%d bytes)",
                        data.get("number"), len(data["test_patch"]),
                    )
                    skipped += 1
                    continue

                instance_id = data.get("instance_id") or f"{data['org']}__{data['repo']}-{data['number']}"
                existing = Instance.search([("name", "=", instance_id), ("repository_id", "=", self.id)], limit=1)
                if existing:
                    continue

                raw_number = data.get("number", 0)
                pr_number = raw_number if isinstance(raw_number, int) else int(str(raw_number).split("-")[0] or 0)

                body = (data.get("body") or "")[:MAX_BODY_SIZE]

                instance = Instance.create(
                    {
                        "name": instance_id,
                        "repository_id": self.id,
                        "org": data.get("org", ""),
                        "repo": data.get("repo", ""),
                        "pr_number": pr_number,
                        "state": data.get("state", ""),
                        "title": data.get("title", ""),
                        "body": body,
                        "base_label": data.get("base", {}).get("label", ""),
                        "base_ref": data.get("base", {}).get("ref", ""),
                        "base_sha": data.get("base", {}).get("sha", ""),
                        "fix_patch": data.get("fix_patch", ""),
                        "test_patch": data.get("test_patch", ""),
                        "tag": data.get("tag", ""),
                        "number_interval": data.get("number_interval", ""),
                        "language": data.get("lang", self.language),
                        "resolved_issues_json": json.dumps(
                            data.get("resolved_issues", []),
                        ),
                    },
                )

                for issue in data.get("resolved_issues", []):
                    issue_body = (issue.get("body") or "")[:MAX_BODY_SIZE]
                    ResolvedIssue.create(
                        {
                            "instance_id": instance.id,
                            "issue_number": issue.get("number", 0),
                            "issue_title": issue.get("title", ""),
                            "issue_body": issue_body,
                        },
                    )

                if line_num % 100 == 0:
                    self._append_log(f"Created {line_num} instances...")
                    self.env.cr.commit()

        if skipped:
            self._append_log(f"Skipped {skipped} instances (oversized patches or lines)")

