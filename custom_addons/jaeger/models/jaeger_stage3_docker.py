import json
import logging
import os
import time as _time

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError

from .credential_manager import get_encrypted_param
from .jaeger_repository import LANGUAGE_BASE_IMAGES

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage3(models.Model):
    _inherit = "jaeger.repository"

    def action_build_docker_images(self):
        raise UserError(
            "Queue-based dispatch is disabled (RabbitMQ consumer deleted). "
            "Use the 'Build Docker (Direct)' button instead."
        )

    def action_build_docker_direct(self):
        self.ensure_one()
        if self.current_stage != "stage3":
            raise UserError("Repository must be in Stage 3.")
        if not self.instance_ids:
            raise UserError("No instances found. Run PR collection first.")

        from psycopg2 import OperationalError as Psycopg2OpError
        try:
            self.env.cr.execute(
                "SELECT docker_build_status FROM jaeger_repository"
                " WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Psycopg2OpError:
            self.env.cr.rollback()
            raise UserError("Docker build is already being started by another user.")
        row = self.env.cr.fetchone()
        if row and row[0] in ("building", "queued"):
            raise UserError("Docker build is already in progress.")

        db_name = self.env.cr.dbname
        rec_id = self.id

        self.write({
            "docker_build_status": "queued",
            "error_message": False,
            "cancel_requested": False,
            "docker_build_progress": 0.0,
        })

        def _dispatch_k8s():
            try:
                from odoo.orm.registry import Registry
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    repo = env["jaeger.repository"].browse(rec_id)
                    repo._create_build_k8s_job()
                    repo.write({"docker_build_status": "building"})
            except Exception as e:
                _logger.exception("Build K8s Job creation failed for repo %s", rec_id)
                try:
                    from odoo.orm.registry import Registry
                    with Registry(db_name).cursor() as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        repo = env["jaeger.repository"].browse(rec_id)
                        repo.write({
                            "docker_build_status": "failed",
                            "error_message": "K8s Job creation failed: %s" % str(e)[:1500],
                        })
                except Exception:
                    _logger.exception("Failed to record build dispatch failure")

        self.env.cr.postcommit.add(_dispatch_k8s)

    def _create_build_k8s_job(self):
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

        manifest_key = self._upload_build_manifest()

        tokens_str = get_encrypted_param(self.env, "jaeger.github_tokens")
        webhook_secret = os.environ.get("JAEGER_WEBHOOK_TOKEN", "")
        base_url = (
            os.environ.get("JAEGER_WEBHOOK_BASE_URL")
            or ICP.get_param("web.base.url", "http://localhost:8069")
        )
        webhook_url = "%s/jaeger/webhook/pipeline" % base_url.rstrip("/")

        job_name = "jaeger-phase2-%s" % self.id
        secret_name = "jaeger-phase2-%s-secrets" % self.id

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
            core_v1, namespace, secret_name, secret_data,
            {"app.kubernetes.io/name": "jaeger-phase2", "repo-id": str(self.id)},
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
        platform = self.docker_platform or ICP.get_param("jaeger.docker_platform", "linux/amd64")
        agent_timeout = int(ICP.get_param("jaeger.agent_timeout", "1800"))
        max_workers = int(ICP.get_param("jaeger.max_run_workers", "2"))

        env_vars = [
            client.V1EnvVar(name="DOCKER_TLS_CERTDIR", value=""),
            client.V1EnvVar(name="REPO_ID", value=str(self.id)),
            client.V1EnvVar(name="REPO_ORG", value=self.org),
            client.V1EnvVar(name="REPO_NAME", value=self.repo_name),
            client.V1EnvVar(name="REPO_LANGUAGE", value=self.language or "python"),
            _secret_ref("GITHUB_TOKENS"),
            client.V1EnvVar(name="MANIFEST_S3_KEY", value=manifest_key),
            client.V1EnvVar(name="DOCKER_PLATFORM", value=platform),
            client.V1EnvVar(name="TEST_CONFIG_JSON", value=json.dumps(config)),
            client.V1EnvVar(name="AGENT_TIMEOUT", value=str(agent_timeout)),
            client.V1EnvVar(name="MAX_WORKERS", value=str(max_workers)),
            client.V1EnvVar(name="S3_BUCKET", value=s3_bucket),
            client.V1EnvVar(name="S3_REGION", value=s3_region),
            client.V1EnvVar(name="S3_PREFIX", value=s3_prefix),
            client.V1EnvVar(name="WEBHOOK_URL", value=webhook_url),
            _secret_ref("WEBHOOK_SECRET"),
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

        container = client.V1Container(
            name="phase2",
            image=build_image,
            image_pull_policy="Never" if sandbox else "Always",
            security_context=client.V1SecurityContext(privileged=True),
            env=env_vars,
            resources=client.V1ResourceRequirements(
                requests={"cpu": "2", "memory": "4Gi", "ephemeral-storage": "20Gi"},
                limits={"memory": "16Gi", "ephemeral-storage": "100Gi"},
            ),
        )

        pod_spec_kwargs = {
            "restart_policy": "Never",
            "containers": [container],
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
            "app.kubernetes.io/name": "jaeger-phase2",
            "app.kubernetes.io/component": "pipeline",
            "repo-id": str(self.id),
            "platform": "jaeger",
        }
        if not sandbox:
            labels["kueue.x-k8s.io/queue-name"] = "jaeger-building"

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=job_name, namespace=namespace, labels=labels),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=3600,
                backoff_limit=1,
                active_deadline_seconds=36000,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(**pod_spec_kwargs),
                ),
            ),
        )

        self.write({"build_queued_at": fields.Datetime.now()})

        try:
            batch_v1.create_namespaced_job(namespace=namespace, body=job)
        except client.ApiException as e:
            if e.status == 409:
                _logger.warning("Phase2 Job %s already exists — recreating", job_name)
                batch_v1.delete_namespaced_job(
                    name=job_name, namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
                _time.sleep(2)
                batch_v1.create_namespaced_job(namespace=namespace, body=job)
            else:
                raise

        self._append_log(f"Created K8s phase2 Job: {job_name}")
        _logger.info("Created K8s Phase2 Job %s for repo %s", job_name, self.name)

    def _upload_build_manifest(self):
        import boto3
        from botocore.config import Config

        instances = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "pending",
        )
        manifest = []
        for inst in instances:
            manifest.append({
                "id": inst.id,
                "name": inst.name,
                "org": inst.org,
                "repo": inst.repo,
                "pr_number": inst.pr_number,
                "base_sha": inst.base_sha,
                "language": inst.repository_id.language or "python",
                "selected_test_files_json": inst.selected_test_files_json or "",
                "fix_patch": inst.fix_patch or "",
                "test_patch": inst.test_patch or "",
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

        key = f"{s3_prefix}/manifests/{self.id}/build_manifest.json"
        client.put_object(Bucket=s3_bucket, Key=key, Body=manifest_json, ContentType="application/json")
        _logger.info("Uploaded build manifest: %d instances to s3://%s/%s", len(manifest), s3_bucket, key)
        return key

    # ── Test Config: human-in-the-loop overrides ───────────────────────────

    def _get_effective_config(self):
        import json as _json

        config = {}
        if self.test_config_json:
            try:
                config = _json.loads(self.test_config_json)
            except (_json.JSONDecodeError, TypeError):
                pass

        lang = (self.language or "python").lower()
        config.setdefault("base_image", LANGUAGE_BASE_IMAGES.get(lang, "python:3.11-slim"))
        config.setdefault("memory_limit", "8g" if lang in ("rust", "cpp", "c", "java") else "4g")
        config.setdefault("network", lang != "python")
        config.setdefault("parser", None)
        return config

    @api.depends("test_config_json", "language")
    def _compute_test_config_effective(self):
        import json as _json
        for rec in self:
            try:
                cfg = rec._get_effective_config()
                rec.test_config_effective = _json.dumps(cfg, indent=2, default=str)
            except Exception:
                rec.test_config_effective = "{}"

    def action_detect_config(self):
        import json as _json
        import subprocess
        import tempfile

        lang = (self.language or "python").lower()
        config = {
            "base_image": LANGUAGE_BASE_IMAGES.get(lang, "python:3.11-slim"),
            "memory_limit": "8g" if lang in ("rust", "cpp", "c", "java") else "4g",
            "network": lang != "python",
        }

        tokens_str = get_encrypted_param(self.env, "jaeger.github_tokens")
        github_token = tokens_str.split(",")[0].strip() if tokens_str else ""
        clone_url = (
            f"https://x-access-token:{github_token}@github.com/{self.org}/{self.repo_name}.git"
            if github_token
            else f"https://github.com/{self.org}/{self.repo_name}.git"
        )

        clone_dir = None
        try:
            clone_dir = tempfile.mkdtemp(prefix="jaeger_detect_")
            subprocess.run(
                ["git", "clone", "--depth=1", clone_url, clone_dir],
                check=True, capture_output=True, text=True, timeout=120,
            )
            install_cmds = self._detect_install_commands(clone_dir)
            if install_cmds:
                config["install_cmd"] = " && ".join(install_cmds)
        except Exception as e:
            _logger.warning("Config detection clone failed: %s", e)
        finally:
            if clone_dir:
                import shutil
                shutil.rmtree(clone_dir, ignore_errors=True)

        if lang == "python":
            config["test_cmd"] = "python -m pytest tests/ -v"
        elif lang in ("javascript", "typescript"):
            config["test_cmd"] = "npm test"
        elif lang == "go":
            config["test_cmd"] = "go test -v -count=1 -timeout 15m ./..."
        elif lang == "rust":
            config["test_cmd"] = "cargo test"
        elif lang == "java":
            config["test_cmd"] = "mvn clean test -fn"

        self.write({"test_config_json": _json.dumps(config, indent=2)})

    def _detect_install_commands(self, repo_dir):
        from pathlib import Path

        p = Path(repo_dir)
        cmds = []

        has_pyproject = (p / "pyproject.toml").exists()
        has_setup_py = (p / "setup.py").exists()
        has_setup_cfg = (p / "setup.cfg").exists()

        if has_pyproject or has_setup_py or has_setup_cfg:
            extras = self._detect_extras(p) if has_pyproject else ["dev", "test"]
            extras_str = ",".join(extras) if extras else "dev,test"
            cmds.append(
                f'pip install -e ".[{extras_str}]" 2>/dev/null || pip install -e . || true'
            )

        if (p / "requirements.txt").exists():
            cmds.append("pip install -r requirements.txt || true")

        if cmds:
            return cmds

        if (p / "package.json").exists():
            return ["npm install 2>/dev/null || true"]
        if (p / "go.mod").exists():
            return ["go mod download 2>/dev/null || true"]
        if (p / "Cargo.toml").exists():
            return ["cargo fetch 2>/dev/null || true"]
        return []

    def _detect_extras(self, repo_path):
        toml_path = repo_path / "pyproject.toml"
        if not toml_path.exists():
            return []
        try:
            import tomllib
        except ModuleNotFoundError:
            try:
                import tomli as tomllib  # noqa: N813
            except ModuleNotFoundError:
                return []
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            extras = list(
                data.get("project", {}).get("optional-dependencies", {}).keys()
            )
            return extras
        except Exception:
            return []
