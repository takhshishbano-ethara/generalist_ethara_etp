import json
import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError

from .talos import _load_dotenv, _DEFAULT_LITELLM_CONFIG

_logger = logging.getLogger(__name__)

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

NAMESPACE = "ethara"

NODE_SELECTOR = {
    "kubernetes.io/arch": "amd64",
    "ethara.ai/node-pool": "general-purpose",
}


def _sandbox_labels(task_record):
    return {
        "platform": "talos",
        "component": "sandbox",
        "task-id": str(task_record.id),
        "app.kubernetes.io/name": "talos-sandbox",
        "app.kubernetes.io/managed-by": "talos-odoo",
    }


def _resource_name(task_record):
    return "talos-sandbox-%s" % task_record.id


def _build_openclaw_config(gateway_token, env):
    """Build the openclaw.json dict — same logic as local mode."""
    aws_bearer = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    aws_region = env.get("AWS_REGION", "ap-south-1").strip()
    bedrock_arn = env.get("BEDROCK_MODEL_ARN", "").strip()
    litellm_key = env.get("LITELLM_MASTER_KEY", "").strip()
    if not litellm_key:
        litellm_key = "sk-talos-%s" % secrets.token_hex(8)

    config_dict = {
        "gateway": {
            "bind": "lan",
            "auth": {"mode": "token", "token": gateway_token},
            "controlUi": {
                "allowedOrigins": [
                    "http://localhost:18789",
                    "http://127.0.0.1:18789",
                    "http://0.0.0.0:18789",
                ],
                "dangerouslyDisableDeviceAuth": True,
            },
        },
        "browser": {
            "enabled": True,
            "headless": True,
            "noSandbox": True,
            "defaultProfile": "openclaw",
        },
        "models": {"providers": {}},
    }

    providers = config_dict["models"]["providers"]

    if aws_bearer and bedrock_arn:
        providers["talos-bedrock"] = {
            "baseUrl": "https://bedrock-runtime.%s.amazonaws.com" % aws_region,
            "apiKey": aws_bearer,
            "auth": "api-key",
            "api": "bedrock-converse-stream",
            "models": [
                {
                    "id": bedrock_arn,
                    "name": "claude-inference",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 200000,
                    "maxTokens": 8192,
                }
            ],
        }

    providers["litellm"] = {
        "baseUrl": "http://localhost:4000/v1",
        "apiKey": litellm_key,
        "auth": "api-key",
        "api": "openai-responses",
        "models": [
            {
                "id": "claude-opus-4.6",
                "name": "claude-opus-4.6",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 200000,
                "maxTokens": 8192,
            },
            {
                "id": "kimi-k2.5",
                "name": "kimi-k2.5",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 131072,
                "maxTokens": 8192,
            },
        ],
    }
    config_dict["agents"] = {"defaults": {"model": "litellm/claude-opus-4.6"}}

    return config_dict


class TalosSandboxK8s(models.AbstractModel):
    _name = "talos.sandbox.k8s"
    _description = "Talos K8s Sandbox Deployer"

    def _get_config_param(self, key, default=""):
        return self.env["ir.config_parameter"].sudo().get_param(key, default).strip()

    def deploy_sandbox(self, task_record):
        if not K8S_AVAILABLE:
            raise UserError("kubernetes package is not installed on this server.")

        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        task_id = task_record.id
        persona = task_record.persona_id
        if not persona:
            raise UserError("No persona selected for task %s." % task_id)
        persona_name = persona.name
        name = _resource_name(task_record)
        labels = _sandbox_labels(task_record)

        env = _load_dotenv()
        aws_bearer = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        aws_region = env.get("AWS_REGION", "ap-south-1").strip()
        bedrock_arn = env.get("BEDROCK_MODEL_ARN", "").strip()
        litellm_master_key = env.get("LITELLM_MASTER_KEY", "").strip()
        if not litellm_master_key:
            litellm_master_key = "sk-talos-%s" % secrets.token_hex(8)
        litellm_db_password = env.get("LITELLM_DB_PASSWORD", "").strip()
        if not litellm_db_password:
            litellm_db_password = secrets.token_hex(16)

        openclaw_image = self._get_config_param(
            "talos.openclaw_image", "ghcr.io/openclaw/openclaw:latest"
        )
        litellm_image = self._get_config_param(
            "talos.litellm_image", "ghcr.io/berriai/litellm:main-stable"
        )

        s3_bucket = env.get("TALOS_S3_BUCKET", "").strip()

        gateway_token = task_record.docker_gateway_token

        self._create_secret(
            core_v1,
            task_id,
            labels,
            gateway_token=gateway_token,
            litellm_master_key=litellm_master_key,
            litellm_db_password=litellm_db_password,
            aws_bearer=aws_bearer,
        )

        self._create_persona_configmap(
            core_v1,
            task_id,
            labels,
            persona,
        )

        openclaw_config = _build_openclaw_config(gateway_token, env)
        self._create_openclaw_config_configmap(
            core_v1,
            task_id,
            labels,
            openclaw_config,
        )

        litellm_yaml = persona.litellm_config_yaml
        if not litellm_yaml:
            kimi_arn = env.get("KIMI_BEDROCK_MODEL_ARN", "").strip()
            kimi_region = env.get("KIMI_AWS_REGION", "us-east-1").strip()
            litellm_yaml = _DEFAULT_LITELLM_CONFIG.format(
                bedrock_arn=bedrock_arn or "PLACEHOLDER",
                aws_region=aws_region,
                kimi_bedrock_arn=kimi_arn or "PLACEHOLDER",
                kimi_aws_region=kimi_region,
            )
        self._create_litellm_configmap(core_v1, task_id, labels, litellm_yaml)

        self._create_deployment(
            apps_v1,
            task_record,
            labels,
            persona_name,
            name,
            openclaw_image,
            litellm_image,
            litellm_master_key,
            litellm_db_password,
            aws_bearer,
            aws_region,
            bedrock_arn,
            gateway_token,
            s3_bucket,
        )

        self._create_service(core_v1, task_record, labels, name)

    def _create_secret(
        self,
        core_v1,
        task_id,
        labels,
        gateway_token,
        litellm_master_key,
        litellm_db_password,
        aws_bearer,
    ):
        secret = client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=client.V1ObjectMeta(
                name="talos-sandbox-creds-%s" % task_id,
                namespace=NAMESPACE,
                labels=labels,
            ),
            string_data={
                "OPENCLAW_GATEWAY_TOKEN": gateway_token or "",
                "LITELLM_MASTER_KEY": litellm_master_key,
                "LITELLM_DB_PASSWORD": litellm_db_password,
                "AWS_BEARER_TOKEN_BEDROCK": aws_bearer,
            },
        )
        try:
            core_v1.create_namespaced_secret(namespace=NAMESPACE, body=secret)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_persona_configmap(self, core_v1, task_id, labels, persona):
        """Create ConfigMap from talos.persona DB fields."""
        data = {}
        if persona.soul_md:
            data["SOUL.md"] = persona.soul_md
        if persona.memory_md:
            data["MEMORY.md"] = persona.memory_md
        if persona.agents_md:
            data["AGENTS.md"] = persona.agents_md

        cm = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(
                name="talos-sandbox-persona-%s" % task_id,
                namespace=NAMESPACE,
                labels=labels,
            ),
            data=data,
        )
        try:
            core_v1.create_namespaced_config_map(namespace=NAMESPACE, body=cm)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_openclaw_config_configmap(
        self, core_v1, task_id, labels, openclaw_config
    ):
        """Create ConfigMap with pre-built openclaw.json so the entrypoint is bypassed."""
        cm = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(
                name="talos-sandbox-openclaw-config-%s" % task_id,
                namespace=NAMESPACE,
                labels=labels,
            ),
            data={"openclaw.json": json.dumps(openclaw_config)},
        )
        try:
            core_v1.create_namespaced_config_map(namespace=NAMESPACE, body=cm)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_litellm_configmap(self, core_v1, task_id, labels, litellm_yaml):
        """Create per-task LiteLLM config ConfigMap from persona DB field."""
        cm = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(
                name="talos-litellm-config-%s" % task_id,
                namespace=NAMESPACE,
                labels=labels,
            ),
            data={"config.yaml": litellm_yaml},
        )
        try:
            core_v1.create_namespaced_config_map(namespace=NAMESPACE, body=cm)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_deployment(
        self,
        apps_v1,
        task_record,
        labels,
        persona,
        name,
        openclaw_image,
        litellm_image,
        litellm_master_key,
        litellm_db_password,
        aws_bearer,
        aws_region,
        bedrock_arn,
        gateway_token,
        s3_bucket,
    ):
        task_id = task_record.id
        secret_name = "talos-sandbox-creds-%s" % task_id
        persona_cm = "talos-sandbox-persona-%s" % task_id
        openclaw_config_cm = "talos-sandbox-openclaw-config-%s" % task_id
        litellm_config_cm = "talos-litellm-config-%s" % task_id

        s3_browser_path = "s3://%s/browser-profiles/%s/" % (s3_bucket, persona)

        db_url = "postgresql://llmproxy:%s@localhost:5432/litellm" % litellm_db_password

        # -- Init container: download browser profiles from S3 --
        init_containers = []
        if s3_bucket:
            init_containers.append(
                client.V1Container(
                    name="browser-sync-init",
                    image="amazon/aws-cli:latest",
                    command=[
                        "sh",
                        "-c",
                        "aws s3 sync %s /data/browser-profiles/ "
                        "--no-progress || true" % s3_browser_path,
                    ],
                    volume_mounts=[
                        client.V1VolumeMount(
                            name="browser-profiles",
                            mount_path="/data/browser-profiles",
                        ),
                    ],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "100m", "memory": "128Mi"},
                        limits={"cpu": "500m", "memory": "256Mi"},
                    ),
                )
            )

        openclaw_container = client.V1Container(
            name="openclaw",
            image=openclaw_image,
            command=[
                "node",
                "openclaw.mjs",
                "gateway",
                "--allow-unconfigured",
                "--token",
                gateway_token,
            ],
            ports=[client.V1ContainerPort(container_port=18789)],
            env=[
                client.V1EnvVar(
                    name="OPENCLAW_GATEWAY_TOKEN",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="OPENCLAW_GATEWAY_TOKEN",
                        ),
                    ),
                ),
                client.V1EnvVar(
                    name="AWS_BEARER_TOKEN_BEDROCK",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="AWS_BEARER_TOKEN_BEDROCK",
                        ),
                    ),
                ),
                client.V1EnvVar(
                    name="LITELLM_MASTER_KEY",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="LITELLM_MASTER_KEY",
                        ),
                    ),
                ),
                client.V1EnvVar(name="PERSONA", value=persona),
                client.V1EnvVar(name="HOME", value="/home/node"),
                client.V1EnvVar(name="TERM", value="xterm-256color"),
                client.V1EnvVar(
                    name="PLAYWRIGHT_BROWSERS_PATH",
                    value="/home/node/.cache/ms-playwright",
                ),
                client.V1EnvVar(name="AWS_REGION", value=aws_region),
                client.V1EnvVar(name="BEDROCK_MODEL_ARN", value=bedrock_arn),
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="persona-files",
                    mount_path="/sandbox/personas/%s" % persona,
                    read_only=True,
                ),
                client.V1VolumeMount(
                    name="browser-profiles",
                    mount_path="/home/node/.openclaw/browser-profiles",
                ),
                client.V1VolumeMount(
                    name="openclaw-data",
                    mount_path="/home/node/.openclaw",
                ),
                client.V1VolumeMount(
                    name="openclaw-config",
                    mount_path="/home/node/.openclaw/openclaw.json",
                    sub_path="openclaw.json",
                    read_only=True,
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "1", "memory": "2Gi"},
                limits={"cpu": "2", "memory": "4Gi"},
            ),
            startup_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz",
                    port=18789,
                ),
                initial_delay_seconds=10,
                period_seconds=5,
                failure_threshold=30,
            ),
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz",
                    port=18789,
                ),
                period_seconds=10,
                timeout_seconds=5,
            ),
            liveness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz",
                    port=18789,
                ),
                initial_delay_seconds=60,
                period_seconds=15,
                timeout_seconds=5,
            ),
        )

        litellm_container = client.V1Container(
            name="litellm",
            image=litellm_image,
            ports=[client.V1ContainerPort(container_port=4000)],
            command=["--config", "/app/config.yaml", "--port", "4000"],
            env=[
                client.V1EnvVar(
                    name="LITELLM_MASTER_KEY",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="LITELLM_MASTER_KEY",
                        ),
                    ),
                ),
                client.V1EnvVar(
                    name="AWS_BEARER_TOKEN_BEDROCK",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="AWS_BEARER_TOKEN_BEDROCK",
                        ),
                    ),
                ),
                client.V1EnvVar(
                    name="LITELLM_DB_PASSWORD",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="LITELLM_DB_PASSWORD",
                        ),
                    ),
                ),
                client.V1EnvVar(name="DATABASE_URL", value=db_url),
                client.V1EnvVar(name="STORE_MODEL_IN_DB", value="True"),
                client.V1EnvVar(name="AWS_REGION", value=aws_region),
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="litellm-config",
                    mount_path="/app/config.yaml",
                    sub_path="config.yaml",
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "500m", "memory": "512Mi"},
                limits={"cpu": "1", "memory": "2Gi"},
            ),
            readiness_probe=client.V1Probe(
                _exec=client.V1ExecAction(
                    command=[
                        "python3",
                        "-c",
                        "import urllib.request; "
                        "urllib.request.urlopen('http://localhost:4000/health/liveliness')",
                    ],
                ),
                period_seconds=15,
                timeout_seconds=10,
                failure_threshold=5,
            ),
        )

        db_container = client.V1Container(
            name="db",
            image="postgres:16",
            ports=[client.V1ContainerPort(container_port=5432)],
            env=[
                client.V1EnvVar(
                    name="POSTGRES_PASSWORD",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="LITELLM_DB_PASSWORD",
                        ),
                    ),
                ),
                client.V1EnvVar(name="POSTGRES_DB", value="litellm"),
                client.V1EnvVar(name="POSTGRES_USER", value="llmproxy"),
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="db-data",
                    mount_path="/var/lib/postgresql/data",
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "250m", "memory": "256Mi"},
                limits={"cpu": "500m", "memory": "512Mi"},
            ),
            readiness_probe=client.V1Probe(
                _exec=client.V1ExecAction(
                    command=["pg_isready", "-d", "litellm", "-U", "llmproxy"],
                ),
                initial_delay_seconds=5,
                period_seconds=5,
                timeout_seconds=5,
            ),
            liveness_probe=client.V1Probe(
                _exec=client.V1ExecAction(
                    command=["pg_isready", "-d", "litellm", "-U", "llmproxy"],
                ),
                initial_delay_seconds=30,
                period_seconds=10,
            ),
        )

        # -- Sidecar: periodically sync browser profiles to S3 --
        containers = [openclaw_container, litellm_container, db_container]
        if s3_bucket:
            containers.append(
                client.V1Container(
                    name="browser-sync-sidecar",
                    image="amazon/aws-cli:latest",
                    command=[
                        "sh",
                        "-c",
                        "while true; do "
                        "sleep 60; "
                        "aws s3 sync /data/browser-profiles/ %s "
                        "--no-progress --quiet 2>/dev/null || true; "
                        "done" % s3_browser_path,
                    ],
                    volume_mounts=[
                        client.V1VolumeMount(
                            name="browser-profiles",
                            mount_path="/data/browser-profiles",
                            read_only=True,
                        ),
                    ],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "50m", "memory": "64Mi"},
                        limits={"cpu": "200m", "memory": "128Mi"},
                    ),
                )
            )

        volumes = [
            client.V1Volume(
                name="persona-files",
                config_map=client.V1ConfigMapVolumeSource(name=persona_cm),
            ),
            client.V1Volume(
                name="browser-profiles",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            client.V1Volume(
                name="openclaw-data",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            client.V1Volume(
                name="openclaw-config",
                config_map=client.V1ConfigMapVolumeSource(
                    name=openclaw_config_cm,
                ),
            ),
            client.V1Volume(
                name="litellm-config",
                config_map=client.V1ConfigMapVolumeSource(
                    name=litellm_config_cm,
                ),
            ),
            client.V1Volume(
                name="db-data",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
        ]

        deployment = client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=NAMESPACE,
                labels=labels,
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(
                    match_labels={
                        "task-id": str(task_id),
                        "component": "sandbox",
                    },
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(
                        node_selector=NODE_SELECTOR,
                        init_containers=init_containers or None,
                        containers=containers,
                        volumes=volumes,
                    ),
                ),
            ),
        )
        try:
            apps_v1.create_namespaced_deployment(namespace=NAMESPACE, body=deployment)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_service(self, core_v1, task_record, labels, name):
        svc = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=NAMESPACE,
                labels=labels,
            ),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector={
                    "task-id": str(task_record.id),
                    "component": "sandbox",
                },
                ports=[
                    client.V1ServicePort(
                        name="gateway",
                        port=18789,
                        target_port=18789,
                    ),
                ],
            ),
        )
        try:
            core_v1.create_namespaced_service(namespace=NAMESPACE, body=svc)
        except ApiException as e:
            if e.status != 409:
                raise

    def destroy_sandbox(self, task_record):
        if not K8S_AVAILABLE:
            return

        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        task_id = task_record.id
        name = _resource_name(task_record)

        self._delete_resource(
            apps_v1.delete_namespaced_deployment,
            name,
            NAMESPACE,
        )
        self._delete_resource(
            core_v1.delete_namespaced_service,
            name,
            NAMESPACE,
        )
        self._delete_resource(
            core_v1.delete_namespaced_secret,
            "talos-sandbox-creds-%s" % task_id,
            NAMESPACE,
        )
        self._delete_resource(
            core_v1.delete_namespaced_config_map,
            "talos-sandbox-persona-%s" % task_id,
            NAMESPACE,
        )
        self._delete_resource(
            core_v1.delete_namespaced_config_map,
            "talos-sandbox-openclaw-config-%s" % task_id,
            NAMESPACE,
        )
        self._delete_resource(
            core_v1.delete_namespaced_config_map,
            "talos-litellm-config-%s" % task_id,
            NAMESPACE,
        )

    def _delete_resource(self, delete_func, name, namespace):
        try:
            delete_func(name=name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                _logger.warning(
                    "Failed to delete K8s resource %s: %s",
                    name,
                    e,
                )

    def get_sandbox_status(self, task_record):
        if not K8S_AVAILABLE:
            return "stopped"

        config.load_incluster_config()
        apps_v1 = client.AppsV1Api()
        name = _resource_name(task_record)

        try:
            dep = apps_v1.read_namespaced_deployment(name=name, namespace=NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                if task_record.docker_status == "starting" and task_record.write_date:
                    elapsed = (
                        fields.Datetime.now() - task_record.write_date
                    ).total_seconds()
                    if elapsed > 300:
                        return "error"
                return "stopped"
            raise

        status = dep.status
        if status.available_replicas and status.available_replicas >= 1:
            return "running"
        if status.replicas and status.replicas > 0:
            return "starting"
        return "stopped"

    @api.model
    def reconcile_sandboxes(self):
        if not K8S_AVAILABLE:
            _logger.warning("kubernetes not available, skipping sandbox reconciliation")
            return

        tasks = (
            self.env["talos.talos"]
            .sudo()
            .search(
                [
                    ("docker_status", "in", ["starting", "running"]),
                ]
            )
        )
        if not tasks:
            return

        for task in tasks:
            try:
                status = self.get_sandbox_status(task)
                if status != task.docker_status:
                    task.write({"docker_status": status})
                    if status == "error":
                        task.write(
                            {
                                "docker_error": "Sandbox deployment not found after timeout",
                            }
                        )
            except Exception as e:
                _logger.error(
                    "Reconciliation error for task %s: %s",
                    task.id,
                    e,
                )
