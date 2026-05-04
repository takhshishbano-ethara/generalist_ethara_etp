import logging
import os
import time as _time
from datetime import datetime

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError

from .credential_manager import get_encrypted_param
from .jaeger_repository import MAX_CONCURRENT_SCRAPES

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage2(models.Model):
    _inherit = "jaeger.repository"

    # ── Stage 1 Actions ──────────────────────────────────────────────────

    def action_validate_repo(self):
        self.ensure_one()
        if not self.repo_url:
            raise UserError("GitHub URL is required.")
        try:
            self.write({"crawl_status": "running"})
            self._validate_repo_metadata()
            # Write crawl_status=done BEFORE the gate check so the stage1 gate
            # (which reads crawl_status from the DB) sees the completed state
            # and auto-advance can proceed.
            self.write({"crawl_status": "done"})
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    # Acquire row lock before advancing stage to prevent
                    # concurrent race conditions (mirrors action_advance_stage).
                    from psycopg2 import OperationalError as Psycopg2OpError
                    try:
                        self.env.cr.execute(
                            "SELECT id FROM jaeger_repository"
                            " WHERE id = %s FOR UPDATE NOWAIT",
                            [self.id],
                        )
                    except Psycopg2OpError:
                        self.env.cr.rollback()
                        # Another process is advancing — let it handle stage write
                        return
                    self.write({"current_stage": next_stage})
        except Exception as e:
            error_msg = str(e)[:2000]
            vals = {
                "crawl_status": "failed",
                "error_message": error_msg,
            }
            # Only set terminal state for definitive failures (repo not found,
            # DMCA takedown).  Transient errors (rate limits, network timeouts,
            # 500s) should remain retryable.
            is_terminal = False
            try:
                from github import UnknownObjectException
                if isinstance(e, UnknownObjectException):
                    is_terminal = True
            except ImportError:
                pass
            if is_terminal or "not found" in error_msg.lower():
                vals["terminal_state"] = "repo_not_suitable"
            self.write(vals)
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

        tokens = get_encrypted_param(self.env, "jaeger.github_tokens").strip()
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

        db_name = self.env.cr.dbname
        rec_id = self.id

        self.write({
            "pr_collection_status": "queued", "error_message": False,
            "cancel_requested": False, "log_output": "",
            "scrape_queued_at": fields.Datetime.now(),
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
                        cr.commit()
                except Exception:
                    _logger.exception("Failed to record K8s dispatch failure")

        self.env.cr.postcommit.add(_dispatch_k8s)

    def _create_scrape_k8s_job(self):
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
        core_v1 = client.CoreV1Api()

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

        self._ensure_k8s_namespace(core_v1, namespace)
        s3_bucket = ICP.get_param("jaeger.s3_bucket", "")
        s3_region = ICP.get_param("jaeger.s3_region", "ap-south-1")
        s3_prefix = ICP.get_param("jaeger.s3_prefix", "jaeger/phase1")
        sandbox = ICP.get_param("jaeger.sandbox_mode", "0") == "1"

        tokens_str = get_encrypted_param(self.env, "jaeger.github_tokens")
        webhook_secret = os.environ.get("JAEGER_WEBHOOK_TOKEN", "")
        base_url = (
            os.environ.get("JAEGER_WEBHOOK_BASE_URL")
            or ICP.get_param("web.base.url", "http://localhost:8069")
        )
        webhook_url = "%s/jaeger/webhook/pipeline" % base_url.rstrip("/")

        job_name = "jaeger-scrape-%s" % self.id
        secret_name = "jaeger-scrape-%s-secrets" % self.id

        secret_data = {
            "GITHUB_TOKENS": tokens_str,
            "WEBHOOK_SECRET": webhook_secret,
        }

        if sandbox:
            aws_key = ICP.get_param("jaeger.s3_access_key", "")
            aws_secret_val = ICP.get_param("jaeger.s3_secret_key", "")
            if aws_key:
                secret_data["AWS_ACCESS_KEY_ID"] = aws_key
            if aws_secret_val:
                secret_data["AWS_SECRET_ACCESS_KEY"] = aws_secret_val

        self._upsert_k8s_secret(
            core_v1, namespace, secret_name,
            secret_data,
            {"app.kubernetes.io/name": "jaeger-scrape", "repo-id": str(self.id)},
        )

        def _secret_ref(key):
            return client.V1EnvVar(
                name=key,
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=secret_name, key=key,
                    ),
                ),
            )

        env_vars = [
            client.V1EnvVar(name="REPO_ID", value=str(self.id)),
            client.V1EnvVar(name="REPO_ORG", value=self.org),
            client.V1EnvVar(name="REPO_NAME", value=self.repo_name),
            _secret_ref("GITHUB_TOKENS"),
            client.V1EnvVar(name="S3_BUCKET", value=s3_bucket),
            client.V1EnvVar(name="S3_REGION", value=s3_region),
            client.V1EnvVar(name="S3_PREFIX", value=s3_prefix),
            client.V1EnvVar(name="WEBHOOK_URL", value=webhook_url),
            _secret_ref("WEBHOOK_SECRET"),
            client.V1EnvVar(name="PIPELINE_MODE", value=self.pipeline_mode or "swe"),
            client.V1EnvVar(name="REPO_LANGUAGE", value=self.language or "python"),
        ]

        if sandbox:
            s3_endpoint = ICP.get_param("jaeger.s3_endpoint", "")
            if s3_endpoint:
                env_vars.append(
                    client.V1EnvVar(name="JAEGER_S3_ENDPOINT", value=s3_endpoint),
                )
            if secret_data.get("AWS_ACCESS_KEY_ID"):
                env_vars.append(_secret_ref("AWS_ACCESS_KEY_ID"))
            if secret_data.get("AWS_SECRET_ACCESS_KEY"):
                env_vars.append(_secret_ref("AWS_SECRET_ACCESS_KEY"))

        scrape_image = ICP.get_param(
            "jaeger.scrape_image",
            "426628337772.dkr.ecr.ap-south-1.amazonaws.com/jaeger-scrape:latest",
        )

        pod_spec_kwargs = {
            "restart_policy": "Never",
            "containers": [
                client.V1Container(
                    name="pipeline",
                    image=scrape_image,
                    image_pull_policy="Never" if sandbox else "Always",
                    command=["python", "entrypoint.py"],
                    env=env_vars,
                    resources=client.V1ResourceRequirements(
                        requests={
                            "cpu": "500m",
                            "memory": "1Gi",
                            "ephemeral-storage": "10Gi" if self.pipeline_mode == "lht" else "5Gi",
                        },
                        limits={
                            "memory": "2Gi",
                            "ephemeral-storage": "20Gi" if self.pipeline_mode == "lht" else "10Gi",
                        },
                    ),
                ),
            ],
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
                            "app.kubernetes.io/component": "pipeline",
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

    @staticmethod
    def _upsert_k8s_secret(core_v1, namespace, name, data, labels):
        import base64
        from kubernetes import client

        encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
            data=encoded,
        )
        try:
            core_v1.replace_namespaced_secret(name=name, namespace=namespace, body=secret)
        except client.ApiException as e:
            if e.status == 404:
                core_v1.create_namespaced_secret(namespace=namespace, body=secret)
            else:
                raise

    @staticmethod
    def _ensure_k8s_namespace(core_v1, namespace):
        from kubernetes import client

        try:
            core_v1.read_namespace(name=namespace)
        except client.ApiException as e:
            if e.status == 404:
                _logger.info("K8s namespace '%s' not found — creating it", namespace)
                core_v1.create_namespace(
                    body=client.V1Namespace(
                        metadata=client.V1ObjectMeta(name=namespace),
                    ),
                )
            else:
                raise

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

    def _create_instances_from_dataset(self, dataset_path):
        """Parse raw dataset JSONL and create jaeger.instance records (batched)."""
        import json

        BATCH_SIZE = 100
        MAX_PATCH_SIZE = 5 * 1024 * 1024
        MAX_BODY_SIZE = 100 * 1024

        Instance = self.env["jaeger.instance"]
        ResolvedIssue = self.env["jaeger.resolved.issue"]

        existing_names = set()
        self.env.cr.execute(
            "SELECT name FROM jaeger_instance WHERE repository_id = %s",
            [self.id],
        )
        existing_names = {row[0] for row in self.env.cr.fetchall()}

        skipped = 0
        instance_batch = []
        issue_data_batch = []

        def _flush_batch():
            if not instance_batch:
                return
            created = Instance.create(instance_batch)
            for inst, issues in zip(created, issue_data_batch):
                for issue in issues:
                    issue["instance_id"] = inst.id
            flat_issues = [iss for issues in issue_data_batch for iss in issues]
            if flat_issues:
                ResolvedIssue.create(flat_issues)
            instance_batch.clear()
            issue_data_batch.clear()

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
                if instance_id in existing_names:
                    continue
                existing_names.add(instance_id)

                raw_number = data.get("number", 0)
                try:
                    pr_number = raw_number if isinstance(raw_number, int) else int(str(raw_number).split("-")[0])
                except (ValueError, TypeError):
                    pr_number = 0

                body = (data.get("body") or "")[:MAX_BODY_SIZE]

                instance_batch.append({
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
                    "number_interval": (
                        "-".join(str(n) for n in data["prs_in_bundle"])
                        if data.get("prs_in_bundle")
                        else data.get("number_interval", "")
                    ),
                    "language": data.get("lang", self.language),
                    "resolved_issues_json": json.dumps(
                        data.get("resolved_issues", []),
                    ),
                })

                issue_data_batch.append([
                    {
                        "issue_number": issue.get("number", 0),
                        "issue_title": issue.get("title", ""),
                        "issue_body": (issue.get("body") or "")[:MAX_BODY_SIZE],
                    }
                    for issue in data.get("resolved_issues", [])
                ])

                if len(instance_batch) >= BATCH_SIZE:
                    _flush_batch()
                    self._append_log(f"Created {line_num} instances...")
                    self.env.cr.commit()

        _flush_batch()
        self.env.cr.commit()

        if skipped:
            self._append_log(f"Skipped {skipped} instances (oversized patches or lines)")

    def _create_instances_from_s3(self, s3_paths):
        from pathlib import Path
        from urllib.parse import urlparse

        raw_dataset_s3 = s3_paths.get("raw_dataset", "")
        if not raw_dataset_s3:
            raise ValueError("No raw_dataset S3 path in webhook payload")

        s3_region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")

        # Prefer the bucket that the worker actually wrote to (from the s3://
        # URI in the webhook payload). Only fall back to the configured
        # bucket when the caller passed a plain key — e.g. the reconciler
        # path via _recover_instances_from_s3.
        if raw_dataset_s3.startswith("s3://"):
            parsed = urlparse(raw_dataset_s3)
            s3_bucket = parsed.netloc
            s3_key = parsed.path.lstrip("/")
            if not s3_bucket or not s3_key:
                raise ValueError("Malformed S3 URI: %r" % raw_dataset_s3)
        else:
            s3_bucket = os.environ.get("JAEGER_S3_BUCKET", "")
            s3_key = raw_dataset_s3
            if not s3_bucket:
                raise ValueError("S3 bucket not configured")

        import boto3
        from botocore.config import Config as BotoConfig

        config_kwargs = {"connect_timeout": 30, "read_timeout": 120}
        endpoint = os.environ.get("JAEGER_S3_ENDPOINT")
        if endpoint:
            config_kwargs["s3"] = {"addressing_style": "path"}

        client = boto3.client(
            "s3",
            region_name=s3_region,
            endpoint_url=endpoint or f"https://s3.{s3_region}.amazonaws.com",
            config=BotoConfig(**config_kwargs),
        )

        local_path = Path("/tmp") / f"jaeger_s3_download_{self.id}.jsonl"
        try:
            client.download_file(s3_bucket, s3_key, str(local_path))
            self._create_instances_from_dataset(local_path)
        finally:
            if local_path.exists():
                local_path.unlink()

    def _recover_instances_from_s3(self):
        """Reconstruct S3 path from ICP params and create instances.

        Used by the reconciler when the webhook was missed but the K8s job
        succeeded — the S3 artifacts exist but no instances were created.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        s3_prefix = ICP.get_param("jaeger.s3_prefix", "jaeger/phase1")
        filename = f"{self.org}__{self.repo_name}_raw_dataset.jsonl"
        mode = self.pipeline_mode or "swe"
        s3_key = f"{s3_prefix}/{mode}/{self.id}/{filename}"
        self._create_instances_from_s3({"raw_dataset": s3_key})

