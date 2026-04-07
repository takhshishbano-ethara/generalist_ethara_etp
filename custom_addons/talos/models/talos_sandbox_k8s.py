import logging
import os
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError

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


class TalosSandboxK8s(models.AbstractModel):
    _name = "talos.sandbox.k8s"
    _description = "Talos K8s Sandbox Deployer"

    def _get_config_param(self, key, default=""):
        return self.env["ir.config_parameter"].sudo().get_param(key, default).strip()

    def _sandbox_dir(self):
        sandbox_dir = self._get_config_param("talos.sandbox_dir")
        if not sandbox_dir:
            raise UserError("talos.sandbox_dir not configured.")
        return sandbox_dir

    def deploy_sandbox(self, task_record):
        if not K8S_AVAILABLE:
            raise UserError("kubernetes package is not installed on this server.")

        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        task_id = task_record.id
        persona = task_record.docker_persona or "marcus"
        name = _resource_name(task_record)
        labels = _sandbox_labels(task_record)
        sandbox_dir = self._sandbox_dir()

        litellm_master_key = self._get_config_param("talos.litellm_master_key")
        if not litellm_master_key:
            litellm_master_key = "sk-talos-%s" % secrets.token_hex(8)

        litellm_db_password = self._get_config_param("talos.litellm_db_password")
        if not litellm_db_password:
            litellm_db_password = secrets.token_hex(16)

        aws_bearer = self._get_config_param("talos.aws_bearer_token")
        aws_region = self._get_config_param("talos.aws_region", "ap-south-1")
        bedrock_arn = self._get_config_param("talos.bedrock_model_arn")
        openclaw_image = self._get_config_param(
            "talos.openclaw_image", "ghcr.io/openclaw/openclaw:latest"
        )
        litellm_image = self._get_config_param(
            "talos.litellm_image", "ghcr.io/berriai/litellm:main-stable"
        )
        storage_class = self._get_config_param("talos.k8s_storage_class", "gp3")

        self._create_secret(
            core_v1,
            task_id,
            labels,
            gateway_token=task_record.docker_gateway_token,
            litellm_master_key=litellm_master_key,
            litellm_db_password=litellm_db_password,
            aws_bearer=aws_bearer,
        )

        self._create_persona_configmap(
            core_v1,
            task_id,
            labels,
            sandbox_dir,
            persona,
        )

        self._create_litellm_configmap(core_v1, sandbox_dir)

        self._create_pvc(
            core_v1,
            "talos-browser-%s" % persona,
            {"platform": "talos", "component": "browser-profiles", "persona": persona},
            "5Gi",
            storage_class,
        )

        self._create_pvc(
            core_v1,
            "talos-sandbox-db-%s" % task_id,
            labels,
            "2Gi",
            storage_class,
        )

        self._create_deployment(
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

    def _create_persona_configmap(self, core_v1, task_id, labels, sandbox_dir, persona):
        persona_dir = os.path.join(sandbox_dir, "personas", persona)
        data = {}
        for filename in ("SOUL.md", "MEMORY.md", "AGENTS.md"):
            filepath = os.path.join(persona_dir, filename)
            if os.path.isfile(filepath):
                with open(filepath, "r") as f:
                    data[filename] = f.read()

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

    def _create_litellm_configmap(self, core_v1, sandbox_dir):
        config_path = os.path.join(sandbox_dir, "docker", "litellm-config.yaml")
        data = {}
        if os.path.isfile(config_path):
            with open(config_path, "r") as f:
                data["config.yaml"] = f.read()

        cm = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(
                name="talos-litellm-config",
                namespace=NAMESPACE,
                labels={
                    "platform": "talos",
                    "component": "litellm-config",
                    "app.kubernetes.io/managed-by": "talos-odoo",
                },
            ),
            data=data,
        )
        try:
            core_v1.create_namespaced_config_map(namespace=NAMESPACE, body=cm)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_pvc(self, core_v1, name, labels, size, storage_class):
        pvc = client.V1PersistentVolumeClaim(
            api_version="v1",
            kind="PersistentVolumeClaim",
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=NAMESPACE,
                labels=labels,
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name=storage_class,
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": size},
                ),
            ),
        )
        try:
            core_v1.create_namespaced_persistent_volume_claim(
                namespace=NAMESPACE,
                body=pvc,
            )
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
    ):
        task_id = task_record.id
        secret_name = "talos-sandbox-creds-%s" % task_id
        persona_cm = "talos-sandbox-persona-%s" % task_id

        db_url = "postgresql://llmproxy:%s@localhost:5432/litellm" % litellm_db_password

        openclaw_container = client.V1Container(
            name="openclaw",
            image=openclaw_image,
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

        volumes = [
            client.V1Volume(
                name="persona-files",
                config_map=client.V1ConfigMapVolumeSource(name=persona_cm),
            ),
            client.V1Volume(
                name="browser-profiles",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name="talos-browser-%s" % persona,
                ),
            ),
            client.V1Volume(
                name="openclaw-data",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            client.V1Volume(
                name="litellm-config",
                config_map=client.V1ConfigMapVolumeSource(
                    name="talos-litellm-config",
                ),
            ),
            client.V1Volume(
                name="db-data",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name="talos-sandbox-db-%s" % task_id,
                ),
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
                        containers=[
                            openclaw_container,
                            litellm_container,
                            db_container,
                        ],
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
            core_v1.delete_namespaced_persistent_volume_claim,
            "talos-sandbox-db-%s" % task_id,
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
