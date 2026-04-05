# -*- coding: utf-8 -*-
import logging
import os
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from kubernetes import client, config

    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

try:
    import boto3

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

NAMESPACE = "ethara"
ECR_REGISTRY = "426628337772.dkr.ecr.ap-south-1.amazonaws.com"
ECR_REGION = "ap-south-1"
MAX_CONCURRENT_BUILDS = 1500

ECR_TAGS = [
    {"Key": "Environment", "Value": "Production"},
    {"Key": "Project", "Value": "Kaiju"},
    {"Key": "Owner", "Value": "Ethara AI"},
    {"Key": "Team", "Value": "DevOps"},
    {"Key": "ManagedBy", "Value": "Ethara-internal"},
    {"Key": "CostCenter", "Value": "RD-001"},
    {"Key": "map-migrated", "Value": "migWBK9WXSX89"},
]

WEBHOOK_SECRET = os.environ.get("KAIJU_WEBHOOK_TOKEN", "")


class KaijuBuild(models.Model):
    _name = "kaiju.build"
    _description = "Kaiju Docker Image Build"
    _order = "create_date desc"

    build_id = fields.Char(string="Build ID", readonly=True, index=True, copy=False)
    app_id = fields.Many2one(
        "kaiju.app", string="Application", required=True, ondelete="restrict"
    )
    app_name = fields.Char(
        string="App Name", related="app_id.name", store=True, readonly=True
    )
    repo_name = fields.Char(string="Repository Name", required=True)
    dataset_json = fields.Text(string="Dataset JSON", required=True)
    tag = fields.Char(string="Image Tag", readonly=True)
    image_uri = fields.Char(string="Image URI", readonly=True)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("building", "Building"),
            ("success", "Success"),
            ("failed", "Failed"),
            ("error", "Error"),
        ],
        string="Status",
        default="draft",
        readonly=True,
        index=True,
    )
    progress = fields.Text(string="Build Logs", readonly=True)
    error_message = fields.Text(string="Error Message", readonly=True)
    started_at = fields.Datetime(string="Started At", readonly=True)
    completed_at = fields.Datetime(string="Completed At", readonly=True)

    def action_build(self):
        self.ensure_one()
        if not K8S_AVAILABLE:
            raise UserError("kubernetes package is not installed on this server.")

        if not self.dataset_json:
            raise UserError("Dataset JSON is required to trigger a build.")

        active_count = self.search_count([("status", "in", ["queued", "building"])])
        if active_count >= MAX_CONCURRENT_BUILDS:
            raise UserError(
                "Build queue is full (%d active builds). "
                "Please try again shortly." % active_count
            )

        self._ensure_ecr_repo(self.app_name)

        build_id = uuid.uuid4().hex[:12]
        tag = "%s-%s" % (build_id, fields.Datetime.now().strftime("%Y%m%d%H%M%S"))

        self.write(
            {
                "build_id": build_id,
                "tag": tag,
                "status": "queued",
                "started_at": fields.Datetime.now(),
                "progress": "Creating build job...",
            }
        )

        try:
            self._create_build_job(build_id, tag)
            self.write({"status": "building"})
        except Exception as e:
            _logger.error("Failed to create K8s Job for build %s: %s", build_id, e)
            self.write(
                {
                    "status": "error",
                    "error_message": str(e),
                }
            )

    def _ensure_ecr_repo(self, app_name):
        if not BOTO3_AVAILABLE:
            _logger.warning("boto3 not available, skipping ECR repo creation")
            return
        ecr_repo_name = "kaiju-%s" % app_name
        ecr = boto3.client("ecr", region_name=ECR_REGION)
        try:
            ecr.create_repository(
                repositoryName=ecr_repo_name,
                imageTagMutability="MUTABLE",
                imageScanningConfiguration={"scanOnPush": False},
                tags=ECR_TAGS,
            )
            _logger.info("Created ECR repository: %s", ecr_repo_name)
        except ecr.exceptions.RepositoryAlreadyExistsException:
            pass
        except Exception as e:
            _logger.warning("ECR repo check failed for %s: %s", ecr_repo_name, e)

    def _create_build_job(self, build_id, tag):
        config.load_incluster_config()
        batch_v1 = client.BatchV1Api()

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name="kaiju-build-%s" % build_id,
                namespace=NAMESPACE,
                labels={
                    "app.kubernetes.io/name": "kaiju-build",
                    "app.kubernetes.io/component": "build-system",
                    "build-id": build_id,
                    "app-name": self.app_name,
                    "platform": "kaiju",
                    "kueue.x-k8s.io/queue-name": "kaniko-builds",
                },
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=300,
                backoff_limit=1,
                active_deadline_seconds=2400,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app.kubernetes.io/name": "kaiju-build",
                            "build-id": build_id,
                            "app-name": self.app_name,
                            "platform": "kaiju",
                        },
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="kaniko-builder",
                        restart_policy="Never",
                        node_selector={
                            "kubernetes.io/arch": "amd64",
                            "ethara.ai/node-pool": "general-purpose",
                        },
                        containers=[
                            client.V1Container(
                                name="builder",
                                image="docker:27-dind",
                                security_context=client.V1SecurityContext(
                                    privileged=True,
                                ),
                                env=[
                                    client.V1EnvVar(
                                        name="DOCKER_TLS_CERTDIR",
                                        value="",
                                    ),
                                    client.V1EnvVar(
                                        name="REPO_NAME",
                                        value=self.repo_name,
                                    ),
                                    client.V1EnvVar(
                                        name="DATASET_JSON",
                                        value=self.dataset_json,
                                    ),
                                ],
                                resources=client.V1ResourceRequirements(
                                    requests={
                                        "cpu": "1",
                                        "memory": "2Gi",
                                        "ephemeral-storage": "10Gi",
                                    },
                                    limits={
                                        "cpu": "4",
                                        "memory": "8Gi",
                                        "ephemeral-storage": "20Gi",
                                    },
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )

        batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job)

    @api.model
    def _cron_reconcile_builds(self):
        active = self.search([("status", "in", ["queued", "building"])])
        if not active:
            return

        if not K8S_AVAILABLE:
            _logger.warning("kubernetes not available, skipping reconciliation")
            return

        try:
            config.load_incluster_config()
            batch_v1 = client.BatchV1Api()

            jobs = batch_v1.list_namespaced_job(
                namespace=NAMESPACE,
                label_selector="platform=kaiju",
            )

            job_map = {}
            for job in jobs.items:
                bid = job.metadata.labels.get("build-id")
                if bid:
                    job_map[bid] = job

            for build in active:
                job = job_map.get(build.build_id)
                if not job:
                    if (
                        build.started_at
                        and (fields.Datetime.now() - build.started_at).total_seconds()
                        > 300
                    ):
                        build.write(
                            {
                                "status": "error",
                                "error_message": "Build job not found in cluster",
                            }
                        )
                    continue

                if job.status.succeeded and job.status.succeeded > 0:
                    build.write(
                        {
                            "status": "success",
                            "image_uri": "%s/kaiju-%s:%s"
                            % (ECR_REGISTRY, build.app_name, build.tag),
                            "completed_at": fields.Datetime.now(),
                        }
                    )
                elif job.status.failed and job.status.failed > 0:
                    build.write(
                        {
                            "status": "failed",
                            "completed_at": fields.Datetime.now(),
                            "error_message": "Build failed (recovered by reconciliation)",
                        }
                    )
        except Exception as e:
            _logger.error("Reconciliation error: %s", e)
