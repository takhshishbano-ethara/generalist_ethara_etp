import json
import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError

from .talos import _DEFAULT_LITELLM_CONFIG, _load_dotenv
from .talos_sandbox import MODEL_DEFAULTS

_logger = logging.getLogger(__name__)

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

NAMESPACE = "talos"

WS_ROUTER_NAME = "talos-ws-router"

NODE_SELECTOR = {
    "kubernetes.io/arch": "amd64",
    "ethara.ai/node-pool": "general-purpose",
}

SANDBOX_SERVICE_ACCOUNT = "talos-sandbox"

S3_BUCKET = "production-grtlabs-tag"
S3_TALOS_PREFIX = "talos"

TERMINATION_GRACE_PERIOD = 300

WS_ROUTER_NGINX_CONF = """\
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
server {
    listen 80;
    server_name _;
    resolver kube-dns.kube-system.svc.cluster.local valid=10s;
    location ~ ^/sandbox/(\\d+)(?:/(.*))? {
        set $task_id $1;
        set $path $2;
        set $backend talos-sandbox-$task_id.%(namespace)s.svc.cluster.local:18789;
        proxy_pass http://$backend/$path$is_args$args;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host localhost;
        proxy_set_header Origin $http_origin;
        proxy_set_header User-Agent $http_user_agent;
        proxy_hide_header X-Frame-Options;
        proxy_hide_header Content-Security-Policy;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_buffering off;
        proxy_buffer_size 256k;
        proxy_buffers 8 256k;
        proxy_busy_buffers_size 512k;
    }
    location ~ ^/litellm/(\\d+)(?:/(.*))? {
        set $task_id $1;
        set $path $2;
        set $backend talos-sandbox-$task_id.%(namespace)s.svc.cluster.local:4000;
        proxy_pass http://$backend/$path$is_args$args;
        proxy_http_version 1.1;
        proxy_set_header Host localhost;
        proxy_set_header Authorization $http_authorization;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }
    location /healthz {
        return 200 'ok';
        add_header Content-Type text/plain;
    }
}
""" % {"namespace": NAMESPACE}

WS_ROUTER_LABELS = {
    "platform": "talos",
    "component": "ws-router",
    "app.kubernetes.io/name": "talos-ws-router",
    "app.kubernetes.io/managed-by": "talos-odoo",
}


def _sandbox_labels(sandbox_record):
    return {
        "platform": "talos",
        "component": "sandbox",
        "task-id": str(sandbox_record.id),
        "app.kubernetes.io/name": "talos-sandbox",
        "app.kubernetes.io/managed-by": "talos-odoo",
    }


def _resource_name(sandbox_record):
    return "talos-sandbox-%s" % sandbox_record.id


def _s3_session_path(sandbox_record):
    return "s3://%s/%s/tasks/%s/sessions/" % (
        S3_BUCKET,
        S3_TALOS_PREFIX,
        sandbox_record.id,
    )


def _s3_browser_path(persona_name):
    return "s3://%s/%s/tasks/browser-profiles/%s/" % (
        S3_BUCKET,
        S3_TALOS_PREFIX,
        persona_name,
    )


def _build_prestop_script(task_id, persona_name):
    session_path = "s3://%s/%s/sessions/%s/" % (S3_BUCKET, S3_TALOS_PREFIX, task_id)
    browser_path = "s3://%s/%s/browser-profiles/%s/%s/" % (
        S3_BUCKET,
        S3_TALOS_PREFIX,
        persona_name,
        task_id,
    )
    return (
        "RUN_ID=$(cat /home/node/.openclaw/.talos-run-id 2>/dev/null || TZ=Asia/Kolkata date +%%Y-%%m-%%d_%%H-%%M-%%S-IST) && "
        'echo "[talos] preStop: run=$RUN_ID backing up session data to S3..." && '
        "aws s3 sync /home/node/.openclaw/ %(session)slatest/ "
        "--no-progress --quiet 2>/dev/null || true && "
        "aws s3 sync /home/node/.openclaw/ %(session)shistory/run-$RUN_ID/stop/ "
        "--no-progress --quiet 2>/dev/null || true && "
        "aws s3 sync /home/node/.openclaw/browser-profiles/ %(browser)s "
        "--no-progress --quiet 2>/dev/null || true && "
        "echo '[talos] preStop: backup complete, waiting for connections to drain...' && "
        "sleep 10"
    ) % {"session": session_path, "browser": browser_path}


def _build_openclaw_config(gateway_token, env, model_type="claude"):
    aws_bearer = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    aws_region = env.get("AWS_REGION", "ap-south-1").strip()
    bedrock_arn = env.get("BEDROCK_MODEL_ARN", "").strip()
    litellm_key = env.get("LITELLM_MASTER_KEY", "").strip()
    if not litellm_key:
        litellm_key = "sk-talos-%s" % secrets.token_hex(8)

    config_dict = {
        "gateway": {
            "bind": "lan",
            "auth": {
                "mode": "token",
                "token": gateway_token,
                "rateLimit": {
                    "maxAttempts": 9999,
                    "windowMs": 1000,
                    "lockoutMs": 1000,
                },
            },
            "trustedProxies": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
            "controlUi": {
                "allowedOrigins": [
                    "https://projects.ethara.ai",
                    "http://projects.ethara.ai",
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
                    "maxTokens": 128000,
                }
            ],
        }

    providers["litellm"] = {
        "baseUrl": "http://localhost:4000/v1",
        "apiKey": litellm_key,
        "auth": "api-key",
        "api": "openai-completions",
        "models": [
            {
                "id": "claude-opus-4.7",
                "name": "claude-opus-4.7",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 200000,
                "maxTokens": 128000,
            },
            {
                "id": "qwen-3-32b",
                "name": "qwen-3-32b",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 32768,
                "maxTokens": 8192,
            },
            {
                "id": "quiet_sand",
                "name": "quiet_sand",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 131072,
                "maxTokens": 32768,
            },
        ],
    }
    config_dict["agents"] = {
        "defaults": {
            "model": MODEL_DEFAULTS.get(model_type, "litellm/claude-opus-4.7"),
            "thinkingDefault": "xhigh",
        }
    }

    return config_dict


class TalosSandboxK8s(models.AbstractModel):
    _name = "talos.sandbox.k8s"
    _description = "Talos K8s Sandbox Deployer"

    def _get_config_param(self, key, default=""):
        return self.env["ir.config_parameter"].sudo().get_param(key, default).strip()

    def deploy_sandbox(self, sandbox_record):
        if not K8S_AVAILABLE:
            raise UserError("kubernetes package is not installed on this server.")

        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        sandbox_id = sandbox_record.id
        task_id = sandbox_id
        persona = sandbox_record.talos_id.persona_id
        if not persona:
            raise UserError(
                "No persona selected on task '%s'. Please select a persona and save before starting."
                % (sandbox_record.talos_id.display_name or sandbox_record.talos_id.id)
            )
        persona_name = persona.name
        name = _resource_name(sandbox_record)
        labels = _sandbox_labels(sandbox_record)

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
        llama_api_key = env.get("LLAMA_API_KEY", "").strip()

        openclaw_image = self._get_config_param(
            "talos.openclaw_image",
            "426628337772.dkr.ecr.ap-south-1.amazonaws.com/talos-q1-coding:v1.0.0",
        )
        litellm_image = self._get_config_param(
            "talos.litellm_image", "ghcr.io/berriai/litellm:main-stable"
        )
        postgres_image = self._get_config_param("talos.postgres_image", "postgres:16")
        aws_cli_image = self._get_config_param(
            "talos.aws_cli_image", "amazon/aws-cli:latest"
        )
        s3_bucket = self._get_config_param("talos.s3_bucket", S3_BUCKET)
        s3_prefix = self._get_config_param("talos.s3_prefix", S3_TALOS_PREFIX)

        gateway_token = sandbox_record.docker_gateway_token

        self._create_secret(
            core_v1,
            task_id,
            labels,
            gateway_token=gateway_token,
            litellm_master_key=litellm_master_key,
            litellm_db_password=litellm_db_password,
            aws_bearer=aws_bearer,
            llama_api_key=llama_api_key,
        )

        self._create_persona_configmap(
            core_v1,
            task_id,
            labels,
            persona,
        )

        self._create_gog_secret(
            core_v1,
            task_id,
            labels,
            sandbox_record,
        )

        openclaw_config = _build_openclaw_config(
            gateway_token, env, sandbox_record.model_type
        )
        self._create_openclaw_config_configmap(
            core_v1,
            task_id,
            labels,
            openclaw_config,
        )

        litellm_yaml = persona.litellm_config_yaml
        if not litellm_yaml:
            qwen_arn = env.get("QWEN_BEDROCK_MODEL_ARN", "").strip()
            qwen_region = env.get("QWEN_AWS_REGION", "us-east-1").strip()
            glm_arn = env.get("GLM_BEDROCK_MODEL_ARN", "").strip()
            glm_region = env.get("GLM_AWS_REGION", "us-east-1").strip()
            litellm_yaml = _DEFAULT_LITELLM_CONFIG.format(
                bedrock_arn=bedrock_arn or "PLACEHOLDER",
                aws_region=aws_region,
                qwen_bedrock_arn=qwen_arn or "PLACEHOLDER",
                qwen_aws_region=qwen_region,
                glm_bedrock_arn=glm_arn or "PLACEHOLDER",
                glm_aws_region=glm_region,
            )
        self._create_litellm_configmap(core_v1, task_id, labels, litellm_yaml)

        self._create_deployment(
            apps_v1,
            sandbox_record,
            labels,
            persona_name,
            name,
            openclaw_image,
            litellm_image,
            postgres_image,
            aws_cli_image,
            s3_bucket,
            s3_prefix,
            litellm_master_key,
            litellm_db_password,
            aws_bearer,
            aws_region,
            bedrock_arn,
            gateway_token,
        )

        self._create_service(core_v1, sandbox_record, labels, name)

        networking_v1 = client.NetworkingV1Api()
        talos_ws_host = self._get_config_param("talos.ws_router_host", "")
        nginx_image = self._get_config_param("talos.nginx_image", "nginx:alpine")
        self._ensure_ws_router(
            core_v1, apps_v1, networking_v1, talos_ws_host, nginx_image
        )

    def _create_secret(
        self,
        core_v1,
        task_id,
        labels,
        gateway_token,
        litellm_master_key,
        litellm_db_password,
        aws_bearer,
        llama_api_key="",
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
                "LLAMA_API_KEY": llama_api_key,
            },
        )
        try:
            core_v1.create_namespaced_secret(namespace=NAMESPACE, body=secret)
        except ApiException as e:
            if e.status == 409:
                core_v1.replace_namespaced_secret(
                    name="talos-sandbox-creds-%s" % task_id,
                    namespace=NAMESPACE,
                    body=secret,
                )
            else:
                raise

    def _create_persona_configmap(self, core_v1, task_id, labels, persona):
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
            if e.status == 409:
                core_v1.replace_namespaced_config_map(
                    name="talos-sandbox-persona-%s" % task_id,
                    namespace=NAMESPACE,
                    body=cm,
                )
            else:
                raise

    def _create_gog_secret(self, core_v1, task_id, labels, sandbox_record):
        string_data = {}

        gog_auth_raw = sandbox_record.talos_id.gog_auth or ""
        if gog_auth_raw:
            try:
                gog_data = json.loads(gog_auth_raw)
                if isinstance(gog_data, dict):
                    client_secret_obj = None
                    if "client_secret" in gog_data and isinstance(
                        gog_data["client_secret"], dict
                    ):
                        client_secret_obj = gog_data["client_secret"]
                    elif "installed" in gog_data or "web" in gog_data:
                        client_secret_obj = gog_data
                    if client_secret_obj:
                        string_data["client_secret.json"] = json.dumps(
                            client_secret_obj
                        )
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→K8s] Could not parse gog_auth for task %s", task_id
                )

        # Extract token files from gog_auth_token
        gog_auth_token_raw = sandbox_record.talos_id.gog_auth_token or ""
        if gog_auth_token_raw:
            try:
                token_data = json.loads(gog_auth_token_raw)
                if isinstance(token_data, dict):
                    gog_files = token_data.get("tokens", {})
                    for rel_path, content in gog_files.items():
                        if rel_path in ("client_secret", "tokens"):
                            continue
                        if not isinstance(content, str):
                            continue
                        import re

                        safe_key = re.sub(
                            r"[^-._a-zA-Z0-9]",
                            lambda m: "_%02x_" % ord(m.group()),
                            rel_path,
                        )
                        string_data[safe_key] = content
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→K8s] Could not parse gog_auth_token for task %s", task_id
                )

        # Always include config.json default
        if "config.json" not in string_data and "config.json" not in [
            k.replace("__", "/") for k in string_data
        ]:
            string_data["config.json"] = json.dumps({"keyring_backend": "file"})

        # Store GOG_KEYRING_PASSWORD and GOG_ACCOUNT in the secret too
        gog_kp = sandbox_record.talos_id.password or ""
        gog_account = sandbox_record.talos_id.email or ""
        string_data["_GOG_KEYRING_PASSWORD"] = gog_kp
        string_data["_GOG_ACCOUNT"] = gog_account

        # Warn if encrypted token files exist but no keyring password is set
        _reserved_keys = {
            "client_secret.json",
            "config.json",
            "_GOG_KEYRING_PASSWORD",
            "_GOG_ACCOUNT",
        }
        _has_token_files = any(k not in _reserved_keys for k in string_data)
        if _has_token_files and not gog_kp:
            _logger.warning(
                "[GogAuth→K8s] GOG_KEYRING_PASSWORD is empty for task %s but %d "
                "encrypted token file(s) are present; gog CLI will fail to "
                "decrypt JWE tokens. Ensure talos_id.password is set.",
                task_id,
                sum(1 for k in string_data if k not in _reserved_keys),
            )

        _logger.info(
            "[GogAuth→K8s] Creating GOG secret for task %s with %d keys: %s",
            task_id,
            len(string_data),
            list(string_data.keys()),
        )

        secret = client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=client.V1ObjectMeta(
                name="talos-sandbox-gog-%s" % task_id,
                namespace=NAMESPACE,
                labels=labels,
            ),
            string_data=string_data,
        )
        try:
            core_v1.create_namespaced_secret(namespace=NAMESPACE, body=secret)
        except ApiException as e:
            if e.status == 409:
                core_v1.replace_namespaced_secret(
                    name="talos-sandbox-gog-%s" % task_id,
                    namespace=NAMESPACE,
                    body=secret,
                )
            else:
                raise

    def _create_openclaw_config_configmap(
        self, core_v1, task_id, labels, openclaw_config
    ):
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
            if e.status == 409:
                core_v1.replace_namespaced_config_map(
                    name="talos-sandbox-openclaw-config-%s" % task_id,
                    namespace=NAMESPACE,
                    body=cm,
                )
            else:
                raise

    def _create_litellm_configmap(self, core_v1, task_id, labels, litellm_yaml):
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
            if e.status == 409:
                core_v1.replace_namespaced_config_map(
                    name="talos-litellm-config-%s" % task_id,
                    namespace=NAMESPACE,
                    body=cm,
                )
            else:
                raise

    def _create_deployment(
        self,
        apps_v1,
        sandbox_record,
        labels,
        persona,
        name,
        openclaw_image,
        litellm_image,
        postgres_image,
        aws_cli_image,
        s3_bucket,
        s3_prefix,
        litellm_master_key,
        litellm_db_password,
        aws_bearer,
        aws_region,
        bedrock_arn,
        gateway_token,
    ):
        task_id = sandbox_record.id
        secret_name = "talos-sandbox-creds-%s" % task_id
        gog_secret_name = "talos-sandbox-gog-%s" % task_id
        persona_cm = "talos-sandbox-persona-%s" % task_id
        openclaw_config_cm = "talos-sandbox-openclaw-config-%s" % task_id
        litellm_config_cm = "talos-litellm-config-%s" % task_id

        session_s3_path = "s3://%s/%s/tasks/%s/sessions/" % (
            s3_bucket,
            s3_prefix,
            task_id,
        )
        browser_s3_path = "s3://%s/%s/tasks/browser-profiles/%s/%s/" % (
            s3_bucket,
            s3_prefix,
            persona,
            task_id,
        )

        db_url = ""

        prestop_script = _build_prestop_script(task_id, persona)

        init_containers = [
            client.V1Container(
                name="session-restore",
                image=aws_cli_image,
                command=[
                    "sh",
                    "-c",
                    "RUN_ID=$(TZ=Asia/Kolkata date +%%Y-%%m-%%d_%%H-%%M-%%S-IST) && "
                    "echo $RUN_ID > /data/session/.talos-run-id && "
                    "cp /openclaw-config-src/openclaw.json /data/session/openclaw.json && "
                    "chown 1000:1000 /data/session/openclaw.json && "
                    "chmod 644 /data/session/openclaw.json && "
                    "chown -R 1000:1000 /data/session /data/browser-profiles; "
                    "aws s3 ls %s >/dev/null 2>&1 && "
                    "aws s3 sync %s /data/browser-profiles/ --no-progress --quiet || true; "
                    "chown -R 1000:1000 /data/session /data/browser-profiles"
                    % (
                        browser_s3_path,
                        browser_s3_path,
                    ),
                ],
                volume_mounts=[
                    client.V1VolumeMount(
                        name="openclaw-data",
                        mount_path="/data/session",
                    ),
                    client.V1VolumeMount(
                        name="browser-profiles",
                        mount_path="/data/browser-profiles",
                    ),
                    client.V1VolumeMount(
                        name="openclaw-config",
                        mount_path="/openclaw-config-src",
                        read_only=True,
                    ),
                ],
                resources=client.V1ResourceRequirements(
                    requests={"cpu": "50m", "memory": "64Mi"},
                ),
            ),
            client.V1Container(
                name="persona-setup",
                image=openclaw_image,
                command=[
                    "sh",
                    "-c",
                    "mkdir -p /data/workspace/memory /data/workspace/skills && "
                    "cp -L /persona-src/* /data/workspace/ 2>/dev/null; "
                    "ls -la /data/workspace/ && "
                    "chown -R 1000:1000 /data/workspace",
                ],
                volume_mounts=[
                    client.V1VolumeMount(
                        name="persona-files",
                        mount_path="/persona-src",
                        read_only=True,
                    ),
                    client.V1VolumeMount(
                        name="openclaw-data",
                        mount_path="/data",
                    ),
                ],
                resources=client.V1ResourceRequirements(
                    requests={"cpu": "50m", "memory": "64Mi"},
                ),
            ),
            client.V1Container(
                name="snapshot-start",
                image=aws_cli_image,
                command=[
                    "sh",
                    "-c",
                    "RUN_ID=$(cat /data/.talos-run-id 2>/dev/null || TZ=Asia/Kolkata date +%%Y-%%m-%%d_%%H-%%M-%%S-IST) && "
                    "echo '[talos] snapshot-start: saving fresh state as run-'$RUN_ID && "
                    "aws s3 sync /data/ %shistory/run-$RUN_ID/start/ "
                    "--no-progress --quiet 2>/dev/null || true" % (session_s3_path,),
                ],
                volume_mounts=[
                    client.V1VolumeMount(
                        name="openclaw-data",
                        mount_path="/data",
                    ),
                ],
                resources=client.V1ResourceRequirements(
                    requests={"cpu": "50m", "memory": "64Mi"},
                ),
            ),
            client.V1Container(
                name="gog-setup",
                image=openclaw_image,
                command=[
                    "sh",
                    "-c",
                    "mkdir -p /gog-out/gogcli/keyring && "
                    "for f in /gog-src/*; do "
                    '  bn=$(basename "$f"); '
                    '  case "$bn" in _GOG_*) continue;; esac; '
                    '  real=$(python3 -c "'
                    "import re,sys; "
                    "print(re.sub(r'_([0-9a-f]{2})_', lambda m: chr(int(m.group(1),16)), sys.argv[1]))\" "
                    '  "$bn") && '
                    '  mkdir -p "/gog-out/gogcli/$(dirname "$real")" && '
                    '  cp -L "$f" "/gog-out/gogcli/${real}.tmp" && mv "/gog-out/gogcli/${real}.tmp" "/gog-out/gogcli/$real"; '
                    "done && "
                    "ls -laR /gog-out/gogcli/ && "
                    "chown -R 1000:1000 /gog-out 2>/dev/null || true",
                ],
                volume_mounts=[
                    client.V1VolumeMount(
                        name="gog-secret",
                        mount_path="/gog-src",
                        read_only=True,
                    ),
                    client.V1VolumeMount(
                        name="gog-config",
                        mount_path="/gog-out",
                    ),
                ],
                resources=client.V1ResourceRequirements(
                    requests={"cpu": "50m", "memory": "64Mi"},
                ),
            ),
            client.V1Container(
                name="gog-install",
                image=openclaw_image,
                command=[
                    "sh",
                    "-c",
                    "which gog && cp $(which gog) /gog-bin/gog && "
                    "chmod +x /gog-bin/gog && "
                    "ls -la /gog-bin/ || "
                    "echo '[talos] gog binary not found in image, skipping'",
                ],
                volume_mounts=[
                    client.V1VolumeMount(
                        name="gog-bin",
                        mount_path="/gog-bin",
                    ),
                ],
                resources=client.V1ResourceRequirements(
                    requests={"cpu": "50m", "memory": "64Mi"},
                ),
            ),
        ]

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
                client.V1EnvVar(
                    name="GOG_KEYRING_PASSWORD",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=gog_secret_name,
                            key="_GOG_KEYRING_PASSWORD",
                            optional=True,
                        ),
                    ),
                ),
                client.V1EnvVar(
                    name="GOG_ACCOUNT",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=gog_secret_name,
                            key="_GOG_ACCOUNT",
                            optional=True,
                        ),
                    ),
                ),
                client.V1EnvVar(
                    name="PATH",
                    value="/gog-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                ),
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
                    name="gog-config",
                    mount_path="/home/node/.config",
                ),
                client.V1VolumeMount(
                    name="gog-bin",
                    mount_path="/gog-bin",
                    read_only=True,
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "50m", "memory": "1536Mi"},
                limits={"memory": "4Gi"},
            ),
            lifecycle=client.V1Lifecycle(
                pre_stop=client.V1LifecycleHandler(
                    _exec=client.V1ExecAction(
                        command=["sh", "-c", prestop_script],
                    ),
                ),
            ),
            startup_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz",
                    port=18789,
                ),
                initial_delay_seconds=10,
                period_seconds=5,
                failure_threshold=60,
                timeout_seconds=5,
            ),
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz",
                    port=18789,
                ),
                period_seconds=5,
                timeout_seconds=3,
            ),
            liveness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz",
                    port=18789,
                ),
                initial_delay_seconds=30,
                period_seconds=15,
                timeout_seconds=5,
            ),
        )

        litellm_container = client.V1Container(
            name="litellm",
            image=litellm_image,
            ports=[client.V1ContainerPort(container_port=4000)],
            command=["litellm", "--config", "/app/config.yaml", "--port", "4000"],
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
                # k8s expands $(LITELLM_DB_PASSWORD) using the env var
                # defined above; keep that ordering. localhost is the
                # postgres sidecar sharing this pod's netns.
                client.V1EnvVar(
                    name="DATABASE_URL",
                    value=(
                        "postgresql://llmproxy:$(LITELLM_DB_PASSWORD)"
                        "@localhost:5432/litellm"
                    ),
                ),
                client.V1EnvVar(name="STORE_MODEL_IN_DB", value="True"),
                client.V1EnvVar(name="AWS_REGION", value=aws_region),
                client.V1EnvVar(
                    name="LLAMA_API_KEY",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="LLAMA_API_KEY",
                        ),
                    ),
                ),
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="litellm-config",
                    mount_path="/app/config.yaml",
                    sub_path="config.yaml",
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "25m", "memory": "1Gi"},
            ),
            startup_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port=4000),
                initial_delay_seconds=5,
                period_seconds=2,
                failure_threshold=30,
                timeout_seconds=2,
            ),
            readiness_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port=4000),
                period_seconds=5,
                timeout_seconds=2,
            ),
        )

        session_backup_container = client.V1Container(
            name="session-backup",
            image=aws_cli_image,
            command=[
                "sh",
                "-c",
                "while true; do "
                "sleep 300; "
                "aws s3 sync /data/session/ %s "
                "--no-progress --quiet 2>/dev/null || true; "
                "aws s3 sync /data/browser-profiles/ %s "
                "--no-progress --quiet 2>/dev/null || true; "
                "done" % (session_s3_path, browser_s3_path),
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="openclaw-data",
                    mount_path="/data/session",
                    read_only=True,
                ),
                client.V1VolumeMount(
                    name="browser-profiles",
                    mount_path="/data/browser-profiles",
                    read_only=True,
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "50m", "memory": "64Mi"},
            ),
        )

        postgres_container = client.V1Container(
            name="postgres",
            image=postgres_image,
            ports=[client.V1ContainerPort(container_port=5432)],
            env=[
                client.V1EnvVar(name="POSTGRES_DB", value="litellm"),
                client.V1EnvVar(name="POSTGRES_USER", value="llmproxy"),
                client.V1EnvVar(
                    name="POSTGRES_PASSWORD",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key="LITELLM_DB_PASSWORD",
                        ),
                    ),
                ),
                client.V1EnvVar(name="PGDATA", value="/var/lib/postgresql/data/pgdata"),
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="litellm-db",
                    mount_path="/var/lib/postgresql/data",
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "25m", "memory": "128Mi"},
                limits={"memory": "512Mi"},
            ),
            startup_probe=client.V1Probe(
                _exec=client.V1ExecAction(
                    command=["pg_isready", "-U", "llmproxy", "-d", "litellm"],
                ),
                initial_delay_seconds=2,
                period_seconds=2,
                failure_threshold=30,
                timeout_seconds=2,
            ),
            readiness_probe=client.V1Probe(
                _exec=client.V1ExecAction(
                    command=["pg_isready", "-U", "llmproxy", "-d", "litellm"],
                ),
                period_seconds=10,
                timeout_seconds=3,
            ),
        )

        containers = [
            openclaw_container,
            litellm_container,
            postgres_container,
            session_backup_container,
        ]

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
                name="litellm-db",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            client.V1Volume(
                name="gog-secret",
                secret=client.V1SecretVolumeSource(
                    secret_name=gog_secret_name,
                    optional=True,
                ),
            ),
            client.V1Volume(
                name="gog-config",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            client.V1Volume(
                name="gog-bin",
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
                    metadata=client.V1ObjectMeta(
                        labels=labels,
                        annotations={"karpenter.sh/do-not-disrupt": "true"},
                    ),
                    spec=client.V1PodSpec(
                        service_account_name=SANDBOX_SERVICE_ACCOUNT,
                        termination_grace_period_seconds=TERMINATION_GRACE_PERIOD,
                        node_selector=NODE_SELECTOR,
                        init_containers=init_containers,
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

    def _create_service(self, core_v1, sandbox_record, labels, name):
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
                    "task-id": str(sandbox_record.id),
                    "component": "sandbox",
                },
                ports=[
                    client.V1ServicePort(
                        name="gateway",
                        port=18789,
                        target_port=18789,
                    ),
                    client.V1ServicePort(
                        name="litellm",
                        port=4000,
                        target_port=4000,
                    ),
                ],
            ),
        )
        try:
            core_v1.create_namespaced_service(namespace=NAMESPACE, body=svc)
        except ApiException as e:
            if e.status != 409:
                raise

    def _ensure_ws_router(self, core_v1, apps_v1, networking_v1, ws_host, nginx_image):
        # Reconcile the ConfigMap on EVERY call. The deployment itself is
        # idempotent below, but ConfigMap drift (WS_ROUTER_NGINX_CONF edited
        # in code after the router was first deployed) would otherwise go
        # undetected - nginx would keep serving the old routes forever.
        import hashlib

        desired_conf = WS_ROUTER_NGINX_CONF
        desired_hash = hashlib.sha256(desired_conf.encode("utf-8")).hexdigest()[:16]
        cm_changed = False

        cm_body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name="talos-ws-router-conf",
                namespace=NAMESPACE,
                labels=WS_ROUTER_LABELS,
                annotations={"talos.ethara.ai/conf-hash": desired_hash},
            ),
            data={"default.conf": desired_conf},
        )

        try:
            existing_cm = core_v1.read_namespaced_config_map(
                name="talos-ws-router-conf", namespace=NAMESPACE
            )
            existing_conf = (existing_cm.data or {}).get("default.conf", "")
            if existing_conf != desired_conf:
                core_v1.replace_namespaced_config_map(
                    name="talos-ws-router-conf",
                    namespace=NAMESPACE,
                    body=cm_body,
                )
                cm_changed = True
                _logger.info(
                    "WS router ConfigMap drift detected, patched (new hash=%s)",
                    desired_hash,
                )
        except ApiException as e:
            if e.status == 404:
                try:
                    core_v1.create_namespaced_config_map(
                        namespace=NAMESPACE, body=cm_body
                    )
                    cm_changed = True
                except ApiException as e2:
                    if e2.status != 409:
                        raise
            else:
                raise

        # Deployment existence check comes AFTER ConfigMap reconciliation so a
        # running router with stale config still gets its ConfigMap fixed.
        try:
            apps_v1.read_namespaced_deployment(
                name=WS_ROUTER_NAME, namespace=NAMESPACE
            )
            if cm_changed:
                # Rolling restart so nginx picks up the new config. Annotation
                # change on pod template forces ReplicaSet rollover.
                import datetime as _dt

                patch = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "talos.ethara.ai/conf-hash": desired_hash,
                                    "talos.ethara.ai/restarted-at": _dt.datetime.utcnow().isoformat(),
                                }
                            }
                        }
                    }
                }
                apps_v1.patch_namespaced_deployment(
                    name=WS_ROUTER_NAME, namespace=NAMESPACE, body=patch
                )
                _logger.info(
                    "Triggered rolling restart of WS router to pick up new ConfigMap"
                )
            return
        except ApiException as e:
            if e.status != 404:
                raise

        _logger.info("Creating WS router deployment in namespace %s", NAMESPACE)

        container = client.V1Container(
            name="nginx",
            image=nginx_image,
            ports=[client.V1ContainerPort(container_port=80)],
            volume_mounts=[
                client.V1VolumeMount(
                    name="conf",
                    mount_path="/etc/nginx/conf.d/default.conf",
                    sub_path="default.conf",
                    read_only=True,
                ),
            ],
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/healthz", port=80),
                period_seconds=5,
                timeout_seconds=2,
            ),
            resources=client.V1ResourceRequirements(
                requests={"cpu": "50m", "memory": "32Mi"},
            ),
        )

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=WS_ROUTER_NAME,
                namespace=NAMESPACE,
                labels=WS_ROUTER_LABELS,
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(
                    match_labels={"component": "ws-router"},
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=WS_ROUTER_LABELS),
                    spec=client.V1PodSpec(
                        containers=[container],
                        volumes=[
                            client.V1Volume(
                                name="conf",
                                config_map=client.V1ConfigMapVolumeSource(
                                    name="talos-ws-router-conf",
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )
        try:
            apps_v1.create_namespaced_deployment(namespace=NAMESPACE, body=deployment)
        except ApiException as e:
            if e.status != 409:
                raise

        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=WS_ROUTER_NAME,
                namespace=NAMESPACE,
                labels=WS_ROUTER_LABELS,
            ),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector={"component": "ws-router"},
                ports=[
                    client.V1ServicePort(name="http", port=80, target_port=80),
                ],
            ),
        )
        try:
            core_v1.create_namespaced_service(namespace=NAMESPACE, body=svc)
        except ApiException as e:
            if e.status != 409:
                raise

        if ws_host:
            ingress = client.V1Ingress(
                metadata=client.V1ObjectMeta(
                    name=WS_ROUTER_NAME,
                    namespace=NAMESPACE,
                    labels=WS_ROUTER_LABELS,
                    annotations={
                        "nginx.ingress.kubernetes.io/proxy-read-timeout": "600",
                        "nginx.ingress.kubernetes.io/proxy-send-timeout": "600",
                        "nginx.ingress.kubernetes.io/proxy-http-version": "1.1",
                        "nginx.ingress.kubernetes.io/upstream-hash-by": "$request_uri",
                        "nginx.ingress.kubernetes.io/proxy-buffering": "off",
                        "nginx.ingress.kubernetes.io/proxy-buffer-size": "256k",
                        "nginx.ingress.kubernetes.io/proxy-body-size": "100m",
                    },
                ),
                spec=client.V1IngressSpec(
                    rules=[
                        client.V1IngressRule(
                            host=ws_host,
                            http=client.V1HTTPIngressRuleValue(
                                paths=[
                                    client.V1HTTPIngressPath(
                                        path="/sandbox/",
                                        path_type="Prefix",
                                        backend=client.V1IngressBackend(
                                            service=client.V1IngressServiceBackend(
                                                name=WS_ROUTER_NAME,
                                                port=client.V1ServicePort(
                                                    number=80,
                                                ),
                                            ),
                                        ),
                                    ),
                                ],
                            ),
                        ),
                    ],
                ),
            )
            try:
                networking_v1.create_namespaced_ingress(
                    namespace=NAMESPACE, body=ingress
                )
            except ApiException as e:
                if e.status != 409:
                    raise

        _logger.info("WS router created (host=%s)", ws_host or "no-ingress")

    def destroy_sandbox(self, sandbox_record):
        if not K8S_AVAILABLE:
            return

        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        task_id = sandbox_record.id
        name = _resource_name(sandbox_record)

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
            core_v1.delete_namespaced_secret,
            "talos-sandbox-gog-%s" % task_id,
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

    def get_sandbox_status(self, sandbox_record):
        if not K8S_AVAILABLE:
            return "stopped"

        config.load_incluster_config()
        apps_v1 = client.AppsV1Api()
        name = _resource_name(sandbox_record)

        try:
            dep = apps_v1.read_namespaced_deployment(name=name, namespace=NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                if (
                    sandbox_record.docker_status == "starting"
                    and sandbox_record.write_date
                ):
                    elapsed = (
                        fields.Datetime.now() - sandbox_record.write_date
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
        """Reconcile K8s sandbox deployments with DB state.

        NOTE: Prefer using talos.sandbox._cron_reconcile() (called via the
        ir.cron job) which is the canonical reconciliation entry-point.
        This method is kept for backward-compatibility and delegates to the
        same per-sandbox logic.
        """
        if not K8S_AVAILABLE:
            _logger.warning("kubernetes not available, skipping sandbox reconciliation")
            return

        sandboxes = (
            self.env["talos.sandbox"]
            .sudo()
            .search(
                [
                    ("docker_status", "in", ["starting", "running"]),
                ]
            )
        )
        if not sandboxes:
            return

        for sandbox in sandboxes:
            try:
                status = self.get_sandbox_status(sandbox)
                if status != sandbox.docker_status:
                    sandbox.write({"docker_status": status})
                    if status == "error":
                        sandbox.write(
                            {
                                "docker_error": "Sandbox deployment not found after timeout",
                            }
                        )
            except Exception as e:
                _logger.error(
                    "Reconciliation error for sandbox %s: %s",
                    sandbox.id,
                    e,
                )
