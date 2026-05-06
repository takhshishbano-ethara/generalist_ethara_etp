import json
import logging
import os
import time as _time

from odoo import api, fields, models
from odoo.exceptions import UserError

from .credential_manager import get_encrypted_param

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage4(models.Model):
    _inherit = "jaeger.repository"

    def action_run_tests(self):
        raise UserError(
            "Queue-based dispatch is disabled (RabbitMQ consumer deleted). "
            "Use the 'Run Tests (Direct)' button instead."
        )

    def action_run_tests_direct(self):
        raise UserError(
            "Test execution now runs automatically as part of the Build Images step. "
            "Tests are executed in the same pod immediately after images are built."
        )

    def _create_test_k8s_job(self):
        try:
            from kubernetes import client, config as k8s_config
        except ImportError:
            raise RuntimeError("kubernetes package not installed.")

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

        sandbox = ICP.get_param("jaeger.sandbox_mode", "0") == "1"
        s3_bucket = os.environ.get("JAEGER_S3_BUCKET", "")
        s3_region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
        s3_prefix = os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")
        container_registry = ICP.get_param("jaeger.container_registry", "")
        ecr_prefix = ICP.get_param("jaeger.ecr_prefix", "")
        registry = container_registry or ecr_prefix

        manifest_key = self._upload_test_manifest()

        base_url = (
            os.environ.get("JAEGER_WEBHOOK_BASE_URL")
            or ICP.get_param("web.base.url", "http://localhost:8069")
        )
        webhook_url = "%s/jaeger/webhook/pipeline" % base_url.rstrip("/")

        job_name = "jaeger-test-%s" % self.id
        secret_name = "jaeger-test-%s-secrets" % self.id

        secret_data = {}
        if sandbox:
            aws_key = ICP.get_param("jaeger.s3_access_key", "")
            aws_secret_val = ICP.get_param("jaeger.s3_secret_key", "")
            if aws_key:
                secret_data["AWS_ACCESS_KEY_ID"] = aws_key
            if aws_secret_val:
                secret_data["AWS_SECRET_ACCESS_KEY"] = aws_secret_val

        if secret_data:
            self._upsert_k8s_secret(
                core_v1, namespace, secret_name, secret_data,
                {"app.kubernetes.io/name": "jaeger-test", "repo-id": str(self.id)},
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

        config = self._get_effective_config()
        max_workers = int(ICP.get_param("jaeger.max_run_workers", "2"))
        agent_timeout = int(ICP.get_param("jaeger.agent_timeout", "1800"))

        env_vars = [
            client.V1EnvVar(name="REPO_ID", value=str(self.id)),
            client.V1EnvVar(name="REPO_ORG", value=self.org),
            client.V1EnvVar(name="REPO_NAME", value=self.repo_name),
            client.V1EnvVar(name="REPO_LANGUAGE", value=self.language or "python"),
            client.V1EnvVar(name="MANIFEST_S3_KEY", value=manifest_key),
            client.V1EnvVar(name="AGENT_TIMEOUT", value=str(agent_timeout)),
            client.V1EnvVar(name="MAX_WORKERS", value=str(max_workers)),
            client.V1EnvVar(name="TEST_CONFIG_JSON", value=json.dumps(config)),
            client.V1EnvVar(name="CONTAINER_REGISTRY", value=registry),
            client.V1EnvVar(name="S3_BUCKET", value=s3_bucket),
            client.V1EnvVar(name="S3_REGION", value=s3_region),
            client.V1EnvVar(name="S3_PREFIX", value=s3_prefix),
            client.V1EnvVar(name="WEBHOOK_URL", value=webhook_url),
            client.V1EnvVar(name="DOCKER_HOST", value="tcp://localhost:2375"),
        ]

        if sandbox:
            s3_endpoint = ICP.get_param("jaeger.s3_endpoint", "")
            if s3_endpoint:
                env_vars.append(client.V1EnvVar(name="JAEGER_S3_ENDPOINT", value=s3_endpoint))
            if secret_data.get("AWS_ACCESS_KEY_ID"):
                env_vars.append(_secret_ref("AWS_ACCESS_KEY_ID"))
            if secret_data.get("AWS_SECRET_ACCESS_KEY"):
                env_vars.append(_secret_ref("AWS_SECRET_ACCESS_KEY"))

        build_image = ICP.get_param(
            "jaeger.build_image",
            "426628337772.dkr.ecr.ap-south-1.amazonaws.com/jaeger-phase2:latest",
        )

        worker_container = client.V1Container(
            name="worker",
            image=build_image,
            image_pull_policy="Never" if sandbox else "Always",
            command=["python", "worker/test_entrypoint.py"],
            env=env_vars,
            resources=client.V1ResourceRequirements(
                requests={"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "5Gi"},
                limits={"memory": "2Gi", "ephemeral-storage": "20Gi"},
            ),
        )

        dind_container = client.V1Container(
            name="dind",
            image="docker:27-dind",
            security_context=client.V1SecurityContext(privileged=True),
            env=[client.V1EnvVar(name="DOCKER_TLS_CERTDIR", value="")],
            volume_mounts=[
                client.V1VolumeMount(name="docker-storage", mount_path="/var/lib/docker"),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "2", "memory": "4Gi", "ephemeral-storage": "20Gi"},
                limits={"memory": "16Gi", "ephemeral-storage": "50Gi"},
            ),
        )

        volumes = [
            client.V1Volume(name="docker-storage", empty_dir=client.V1EmptyDirVolumeSource()),
        ]

        pod_spec_kwargs = {
            "restart_policy": "Never",
            "containers": [worker_container, dind_container],
            "volumes": volumes,
        }

        if sandbox:
            pod_spec_kwargs["host_network"] = True
            pod_spec_kwargs["dns_policy"] = "None"
            pod_spec_kwargs["dns_config"] = client.V1PodDNSConfig(nameservers=["127.0.0.11"])
        else:
            pod_spec_kwargs["service_account_name"] = "jaeger-pipeline-runner"
            pod_spec_kwargs["node_selector"] = {
                "kubernetes.io/arch": "amd64",
                "ethara.ai/node-pool": "build",
            }

        labels = {
            "app.kubernetes.io/name": "jaeger-test",
            "app.kubernetes.io/component": "pipeline",
            "repo-id": str(self.id),
            "platform": "jaeger",
        }
        if not sandbox:
            labels["kueue.x-k8s.io/queue-name"] = "jaeger-testing"

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=job_name, namespace=namespace, labels=labels),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=3600,
                backoff_limit=1,
                active_deadline_seconds=21600,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(**pod_spec_kwargs),
                ),
            ),
        )

        try:
            batch_v1.create_namespaced_job(namespace=namespace, body=job)
        except client.ApiException as e:
            if e.status == 409:
                _logger.warning("Test Job %s already exists — recreating", job_name)
                batch_v1.delete_namespaced_job(
                    name=job_name, namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
                _time.sleep(2)
                batch_v1.create_namespaced_job(namespace=namespace, body=job)
            else:
                raise

        self.write({"test_queued_at": fields.Datetime.now()})
        self._append_log(f"Created K8s test Job: {job_name}")
        _logger.info("Created K8s Test Job %s for repo %s", job_name, self.name)

    def _upload_test_manifest(self):
        import boto3
        from botocore.config import Config

        built_instances = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built" and i.docker_image_name,
        )
        manifest = []
        for inst in built_instances:
            manifest.append({
                "id": inst.id,
                "name": inst.name,
                "docker_image_name": inst.docker_image_name,
                "fix_patch": inst.fix_patch or "",
                "test_patch": inst.test_patch or "",
                "selected_test_files_json": inst.selected_test_files_json or "",
            })

        manifest_json = json.dumps(manifest).encode("utf-8")
        s3_bucket = os.environ.get("JAEGER_S3_BUCKET", "")
        s3_region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
        s3_prefix = os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")

        config_kwargs = {"connect_timeout": 10, "read_timeout": 60}
        endpoint = os.environ.get("JAEGER_S3_ENDPOINT")
        if endpoint:
            config_kwargs["s3"] = {"addressing_style": "path"}

        client = boto3.client(
            "s3", region_name=s3_region,
            endpoint_url=endpoint or f"https://s3.{s3_region}.amazonaws.com",
            config=Config(**config_kwargs),
        )

        key = f"{s3_prefix}/manifests/{self.id}/test_manifest.json"
        client.put_object(Bucket=s3_bucket, Key=key, Body=manifest_json, ContentType="application/json")
        _logger.info("Uploaded test manifest: %d instances to s3://%s/%s", len(manifest), s3_bucket, key)
        return key
