import logging

from odoo import api, models
from odoo.exceptions import UserError

from .credential_manager import get_encrypted_param
from .jaeger_repository import LANGUAGE_BASE_IMAGES

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage3(models.Model):
    _inherit = "jaeger.repository"

    # ── Stage 3 Actions (Phase 2-7: disabled until infra ready) ────────

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

        self.write({"docker_build_status": "building", "error_message": False})
        self.env.cr.commit()

        return self._run_pipeline_async(
            "run_docker_build", "docker_build_status", "Docker Build",
        )

    def run_docker_build(self):
        """Build Docker images. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        pending_before = len(self.instance_ids.filtered(
            lambda i: i.docker_build_status == "pending",
        ))
        vals = {"error_message": False, "images_built_count": 0, "images_failed_count": 0}
        if self.docker_build_status != "building":
            vals["docker_build_status"] = "building"
        self.write(vals)
        self.env.cr.commit()
        try:
            self._build_via_local_docker()

            built = self.instance_ids.filtered(
                lambda i: i.docker_build_status == "built",
            )
            failed = self.instance_ids.filtered(
                lambda i: i.docker_build_status == "failed",
            )
            self.write(
                {
                    "images_built_count": len(built),
                    "images_failed_count": len(failed),
                },
            )

            if not built and pending_before > 0:
                self.write(
                    {
                        "docker_build_status": "failed",
                        "error_message": "All image builds failed.",
                        "terminal_state": "build_failed",
                    },
                )
                self.env.cr.commit()
                raise ValueError("All Docker image builds failed")

            if not built and pending_before == 0:
                self._append_log("No pending instances to build — all already built.")

            vals = {
                "docker_build_status": "done",
                "docker_build_progress": 100.0,
                "terminal_state": "none",
                "error_message": False,
            }
            # Auto-advance stage after successful build
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self._append_log(
                f"Docker build complete: {len(built)} built, {len(failed)} failed",
            )
            self.env.cr.commit()
        except Exception as e:
            if self.docker_build_status != "failed":
                self.env.cr.rollback()
                self.write(
                    {
                        "docker_build_status": "failed",
                        "error_message": str(e)[:2000],
                    },
                )
                self.env.cr.commit()
            raise

    def _docker_image_exists(self, image_tag):
        """Check if a Docker image exists locally."""
        import subprocess

        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_tag],
                capture_output=True, text=True, timeout=30,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _validate_docker_image(self, instance, image_tag):
        """Post-build smoke test: verify image contents are correct.

        Returns None if valid, error string if not.
        """
        import subprocess

        if not instance.base_sha:
            return None

        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    image_tag, "bash", "-c",
                    'echo "SHA:$(git -C /testbed rev-parse HEAD)" && '
                    'echo "FIXRUN:$(test -f /jaeger/fix-run.sh && echo OK || echo MISSING)" && '
                    'echo "CLEAN:$(git -C /testbed status --porcelain -uno | wc -l | tr -d \" \")"',
                ],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return "Smoke test timed out (container may be broken)"
        except Exception as e:
            return f"Smoke test failed to run: {e}"

        if result.returncode != 0:
            return f"Container failed to start: {result.stderr[-500:]}"

        output = result.stdout.strip()
        checks = {}
        for line in output.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                checks[key.strip()] = val.strip()

        errors = []
        actual_sha = checks.get("SHA", "")
        if actual_sha != instance.base_sha:
            errors.append(f"SHA mismatch: expected {instance.base_sha[:12]}, got {actual_sha[:12]}")

        if checks.get("FIXRUN") != "OK":
            errors.append("fix-run.sh missing from /jaeger")

        dirty_count = int(checks.get("CLEAN", "0") or "0")
        if dirty_count > 0:
            errors.append(f"Working tree has {dirty_count} modified files")

        return "; ".join(errors) if errors else None

    # ── Test Config: human-in-the-loop overrides ───────────────────────

    def _get_effective_config(self):
        """Return merged test config: manual overrides + auto-detected defaults.

        All pipeline methods call this single source of truth instead of
        hardcoding language-based decisions.
        """
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
        """Auto-detect test config from repo and populate test_config_json."""
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
        """Detect how to install dependencies from repo files.

        Returns a list of RUN commands to include in the Dockerfile.
        Many repos define test deps separately from the package install
        (e.g. requirements.txt alongside pyproject.toml), so we install both.
        """
        from pathlib import Path

        p = Path(repo_dir)
        cmds = []

        # ── Python: editable install + extras ──
        has_pyproject = (p / "pyproject.toml").exists()
        has_setup_py = (p / "setup.py").exists()
        has_setup_cfg = (p / "setup.cfg").exists()

        if has_pyproject or has_setup_py or has_setup_cfg:
            # Try common test/dev extras; fall back to bare editable install
            extras = self._detect_extras(p) if has_pyproject else ["dev", "test"]
            extras_str = ",".join(extras) if extras else "dev,test"
            cmds.append(
                f'pip install -e ".[{extras_str}]" 2>/dev/null || pip install -e . || true'
            )

        # Always install requirements.txt if it exists (test deps often live here)
        if (p / "requirements.txt").exists():
            cmds.append("pip install -r requirements.txt || true")

        if cmds:
            return cmds

        # ── Non-Python languages ──
        if (p / "package.json").exists():
            return ["npm install 2>/dev/null || true"]
        if (p / "go.mod").exists():
            return ["go mod download 2>/dev/null || true"]
        if (p / "Cargo.toml").exists():
            return ["cargo fetch 2>/dev/null || true"]
        return []

    def _detect_extras(self, repo_path):
        """Read pyproject.toml and return available optional-dependency group names."""
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

    def _build_base_image(self):
        """Build a repo-level base image from scratch.

        Detects language, runtime, and dependencies from the repo.
        Built once per repo, cached as mswebench/{org}_m_{repo}:base
        """
        import subprocess
        import tempfile
        from pathlib import Path

        base_tag = f"mswebench/{self.org}_m_{self.repo_name}:base".lower()
        config = self._get_effective_config()
        runtime = config.get("base_image", LANGUAGE_BASE_IMAGES.get(self.language, "python:3.11-slim"))

        self.write({"base_image_status": "building"})
        self.env.cr.commit()

        # Get GitHub token for authenticated clones (avoids rate limiting at scale)
        tokens_str = get_encrypted_param(self.env, "jaeger.github_tokens")
        github_token = tokens_str.split(",")[0].strip() if tokens_str else ""

        clone_url = f"https://github.com/{self.org}/{self.repo_name}.git"
        authed_clone_url = (
            f"https://x-access-token:{github_token}@github.com/{self.org}/{self.repo_name}.git"
            if github_token else clone_url
        )

        clone_dir = None
        try:
            # Shallow-clone repo to detect dependency files
            clone_dir = tempfile.mkdtemp(prefix="jaeger_base_")
            clone_cmd = [
                "git", "clone", "--depth=1",
                authed_clone_url,
                clone_dir,
            ]
            subprocess.run(clone_cmd, check=True, capture_output=True, text=True, timeout=120)

            config_install = config.get("install_cmd")
            if config_install:
                install_cmds = [config_install]
            else:
                install_cmds = self._detect_install_commands(clone_dir)

            is_python = self.language in ("python",)
            is_node = self.language in ("javascript", "typescript")

            lines = [
                "# syntax=docker/dockerfile:1.6",
                "",
                f"FROM {runtime}",
                "",
                'ARG TARGETARCH',
                'ARG http_proxy=""',
                'ARG https_proxy=""',
                'ARG HTTP_PROXY=""',
                'ARG HTTPS_PROXY=""',
                'ARG no_proxy="localhost,127.0.0.1,::1"',
                'ARG NO_PROXY="localhost,127.0.0.1,::1"',
                'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"',
                "",
                "ENV DEBIAN_FRONTEND=noninteractive \\",
                "    LANG=C.UTF-8 \\",
                "    TZ=UTC \\",
                "    http_proxy=${http_proxy} \\",
                "    https_proxy=${https_proxy} \\",
                "    HTTP_PROXY=${HTTP_PROXY} \\",
                "    HTTPS_PROXY=${HTTPS_PROXY} \\",
                "    no_proxy=${no_proxy} \\",
                "    NO_PROXY=${NO_PROXY} \\",
                "    SSL_CERT_FILE=${CA_CERT_PATH} \\",
                "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\",
                "    CURL_CA_BUNDLE=${CA_CERT_PATH}",
                "",
                f'LABEL org.opencontainers.image.title="{self.org}/{self.repo_name}" \\',
                f'      org.opencontainers.image.source="https://github.com/{self.org}/{self.repo_name}" \\',
                '      org.opencontainers.image.authors="https://www.ethara.ai/"',
                "",
                "RUN mkdir -p /etc/pki/tls/certs /etc/ssl/certs && \\",
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt 2>/dev/null || true && \\",
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem 2>/dev/null || true",
                "",
            ]

            # System dependencies — config override or language-based defaults
            custom_deps = config.get("system_deps")
            if custom_deps:
                deps_str = " ".join(custom_deps)
                lines += [
                    "RUN apt-get update && apt-get install -y --no-install-recommends \\",
                    f"    ca-certificates git {deps_str} && \\",
                    "    rm -rf /var/lib/apt/lists/*",
                    "",
                ]
            elif is_python:
                lines += [
                    "RUN apt-get update && apt-get install -y --no-install-recommends \\",
                    "    ca-certificates git make gcc g++ curl && \\",
                    "    rm -rf /var/lib/apt/lists/*",
                    "",
                ]
            elif is_node:
                lines += [
                    "RUN apt-get update && apt-get install -y --no-install-recommends \\",
                    "    ca-certificates git make gcc g++ python3 && \\",
                    "    rm -rf /var/lib/apt/lists/*",
                    "",
                ]
            elif self.language in ("c", "cpp"):
                lines += [
                    "RUN apt-get update && apt-get install -y --no-install-recommends \\",
                    "    ca-certificates git make gcc g++ cmake python3 python3-pip curl && \\",
                    "    rm -rf /var/lib/apt/lists/*",
                    "",
                ]
            else:
                lines += [
                    "RUN apt-get update && apt-get install -y --no-install-recommends \\",
                    "    ca-certificates git make curl && \\",
                    "    rm -rf /var/lib/apt/lists/*",
                    "",
                ]

            lines += [
                "WORKDIR /testbed",
                "RUN --mount=type=secret,id=github_token,required=false \\",
                "    TOKEN_FILE=/run/secrets/github_token && \\",
                "    if [ -f \"$TOKEN_FILE\" ] && [ -s \"$TOKEN_FILE\" ]; then \\",
                f"        git clone \"https://x-access-token:$(cat $TOKEN_FILE)@github.com/{self.org}/{self.repo_name}.git\" . ; \\",
                "    else \\",
                f"        git clone \"{clone_url}\" . ; \\",
                "    fi && \\",
                "    git fetch --all",
                "",
            ]

            for cmd in install_cmds:
                lines.append(f"RUN {cmd}")
                lines.append("")

            if is_node:
                lines.append('ENV PATH="/testbed/node_modules/.bin:${PATH}"')
                lines.append("")

            if is_python:
                lines.append("RUN pip install pytest || true")
                lines.append("")

            # Custom env vars from test config
            config_env = config.get("env")
            if config_env and isinstance(config_env, dict):
                for k, v in config_env.items():
                    lines.append(f'ENV {k}="{v}"')
                lines.append("")

            # Metadata labels
            lines += [
                'LABEL jaeger.image.type="base"',
            ]

            dockerfile_content = "\n".join(lines) + "\n"

            # Write Dockerfile and build
            build_dir = Path(clone_dir) / "_docker_build"
            build_dir.mkdir(exist_ok=True)
            (build_dir / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

            ICP = self.env["ir.config_parameter"].sudo()
            platform = self.docker_platform or ICP.get_param("jaeger.docker_platform", "")

            cmd = ["docker", "buildx", "build", "--load"]
            if platform:
                cmd += ["--platform", platform]

            token_file = None
            if github_token:
                token_file = Path(build_dir) / ".github_token"
                token_file.write_text(github_token, encoding="utf-8")
                token_file.chmod(0o600)
                cmd += ["--secret", f"id=github_token,src={token_file}"]

            cmd += ["-t", base_tag, "-f", str(build_dir / "Dockerfile"), str(build_dir)]

            self._append_log(f"Building base image: {base_tag}")
            self._append_log(f"  Runtime: {runtime}, Language: {self.language}")
            self._append_log(f"  Install cmds: {install_cmds or '(none detected)'}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

            if result.returncode != 0:
                self._append_log(f"Base image build FAILED:\n{result.stderr[-3000:]}")
                raise subprocess.CalledProcessError(result.returncode, cmd)

            if token_file and token_file.exists():
                token_file.unlink()

            self._append_log(f"Base image built successfully: {base_tag}")
            self.write({
                "base_image_name": base_tag,
                "base_image_status": "built",
            })
            self.env.cr.commit()

        except Exception as e:
            _logger.exception("Base image build failed for %s/%s", self.org, self.repo_name)
            self.env.cr.rollback()
            self.write({
                "base_image_status": "failed",
                "error_message": f"Base image build failed: {e!s}"[:2000],
            })
            self.env.cr.commit()
            raise
        finally:
            if github_token and clone_dir:
                tf = Path(clone_dir) / "_docker_build" / ".github_token"
                if tf.exists():
                    tf.unlink()
            if clone_dir:
                import shutil
                shutil.rmtree(clone_dir, ignore_errors=True)

    def _build_via_local_docker(self):
        """Build Docker images for all instances using kaiju_build or local Docker.

        For each instance:
        1. Generate a Dockerfile from the instance metadata
        2. Build the image (via kaiju_build K8s job or local docker CLI)
        3. Tag and optionally push to ECR
        4. Update instance.docker_build_status and docker_image_name
        """
        import subprocess

        ICP = self.env["ir.config_parameter"].sudo()
        ecr_prefix = ICP.get_param("jaeger.ecr_prefix", "")
        platform = self.docker_platform or ICP.get_param(
            "jaeger.docker_platform", "",
        )
        build_mode = ICP.get_param("jaeger.docker_build_mode", "local")
        workdir = ICP.get_param("jaeger.docker_workdir", "/tmp/jaeger_docker")
        from pathlib import Path

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        # Step 0: Ensure repo base image exists (auto-build if needed)
        base_tag = f"mswebench/{self.org}_m_{self.repo_name}:base".lower()
        if self._docker_image_exists(base_tag):
            if self.base_image_status != "built":
                self.write({"base_image_status": "built", "base_image_name": base_tag})
                self.env.cr.commit()
        elif self.base_image_status != "built":
            self._append_log("No base image found — building repo base image (first time)...")
            self._build_base_image()

        all_pending = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "pending",
        )
        no_sha = all_pending.filtered(lambda i: not i.base_sha)
        if no_sha:
            for inst in no_sha:
                inst.write({
                    "docker_build_status": "failed",
                    "docker_build_log": "Missing base_sha — cannot build image",
                })
            self._append_log(f"Skipped {len(no_sha)} instances with missing base_sha")
            self.env.cr.commit()
        instances = all_pending.filtered(lambda i: i.base_sha)
        total = len(instances)
        built_count = 0
        failed_count = 0
        self._append_log(f"Building Docker images for {total} instances (mode={build_mode})")

        for idx, inst in enumerate(instances, 1):
            image_name = f"mswebench/{inst.org}_m_{inst.repo}".lower()
            image_tag = f"pr-{inst.pr_number}-{inst.id}"
            full_tag = f"{image_name}:{image_tag}"
            if ecr_prefix:
                full_tag = f"{ecr_prefix}/{image_name}:{image_tag}"

            inst.write({"docker_build_status": "building"})
            self.env.cr.commit()

            try:
                # Generate Dockerfile
                dockerfile = self._generate_dockerfile(inst)
                inst.write({"dockerfile_content": dockerfile})

                build_dir = workdir / f"{inst.org}__{inst.repo}" / f"pr-{inst.pr_number}"
                build_dir.mkdir(parents=True, exist_ok=True)
                dockerfile_path = build_dir / "Dockerfile"
                dockerfile_path.write_text(dockerfile, encoding="utf-8")

                # Generate fix-run.sh test runner script in build context
                fix_run_script = self._generate_fix_run_script(inst)
                (build_dir / "fix-run.sh").write_text(fix_run_script, encoding="utf-8")

                if build_mode == "kaiju" and self.env.registry.get("kaiju.build"):
                    # Use kaiju_build for K8s-based builds
                    self._build_via_kaiju(inst, full_tag, str(dockerfile_path))
                else:
                    # Local Docker build
                    cmd = ["docker", "build"]
                    if platform:
                        cmd += ["--platform", platform]
                    cmd += [
                        "-t", full_tag,
                        "-f", str(dockerfile_path),
                        str(build_dir),
                    ]
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=1800,
                    )

                    inst.write({"docker_build_log": result.stdout[-5000:] + result.stderr[-5000:]})

                    if result.returncode != 0:
                        raise subprocess.CalledProcessError(result.returncode, cmd)

                    # Push to ECR if configured
                    if ecr_prefix:
                        push_cmd = ["docker", "push", full_tag]
                        subprocess.run(push_cmd, check=True, timeout=600)

                validation_err = self._validate_docker_image(inst, full_tag)
                if validation_err:
                    inst.write({
                        "docker_build_status": "failed",
                        "docker_build_log": f"Post-build validation failed: {validation_err}",
                    })
                    failed_count += 1
                    self._append_log(f"  [{idx}/{total}] VALIDATION FAILED {inst.name}: {validation_err}")
                else:
                    inst.write({
                        "docker_build_status": "built",
                        "docker_image_name": full_tag,
                    })
                    built_count += 1
                    self._append_log(f"  [{idx}/{total}] Built {full_tag}")

            except Exception as e:
                inst.write({
                    "docker_build_status": "failed",
                    "docker_build_log": str(e)[:5000],
                })
                failed_count += 1
                _logger.warning("Docker build failed for %s: %s", inst.name, e)
                self._append_log(f"  [{idx}/{total}] FAILED {inst.name}: {e}")

            self.write({
                "docker_build_progress": (idx / total) * 100,
                "images_built_count": built_count,
                "images_failed_count": failed_count,
            })
            self.env.cr.commit()

    def _generate_dockerfile(self, instance):
        """Generate a Dockerfile for a single instance.

        Uses SWE-bench base image if available locally, otherwise falls back
        to the auto-built repo base image (3-layer chain).
        """
        swebench_image = (
            f"swebench/sweb.eval.x86_64.{instance.org}_1776_"
            f"{instance.repo}-{instance.pr_number}:latest"
        )

        if self._docker_image_exists(swebench_image):
            # Original 2-layer path: SWE-bench base has repo @ base_sha already
            base_image = swebench_image
            checkout_cmd = ""
            reinstall_cmd = ""
        else:
            # 3-layer path: auto-built base has repo at default branch HEAD
            # Need to fetch+checkout the specific base_sha for this PR
            base_image = (
                self.base_image_name
                or f"mswebench/{instance.org}_m_{instance.repo}:base".lower()
            )
            checkout_cmd = (
                f"RUN git checkout -- . && git clean -fd && (git checkout {instance.base_sha} || (git fetch origin {instance.base_sha} && git checkout {instance.base_sha}))\n"
                if instance.base_sha else ""
            )
            config = self._get_effective_config()
            config_install = config.get("install_cmd")
            if config_install:
                reinstall_cmd = f"RUN {config_install} 2>/dev/null || true\n"
            else:
                reinstall_cmd = self._dep_reinstall_commands(instance.language)

        return f"""FROM {base_image}

WORKDIR /testbed
{checkout_cmd}{reinstall_cmd}
# Apply fix-run.sh outside repo tree to avoid linters/license checkers
COPY fix-run.sh /jaeger/fix-run.sh
RUN chmod +x /jaeger/fix-run.sh

# Metadata
LABEL org.opencontainers.image.source="https://github.com/{instance.org}/{instance.repo}"
LABEL org.opencontainers.image.revision="{instance.base_sha or ''}"
LABEL jaeger.instance="{instance.name}"
"""

    def _dep_reinstall_commands(self, language):
        """Return Dockerfile RUN lines to conditionally re-install deps.

        Only reinstalls when dep files differ between HEAD and the checked-out
        base_sha. Skips entirely (~80% of PRs) when deps haven't changed.
        """
        if language in ("python",):
            return (
                "RUN if ! git diff HEAD --quiet -- requirements.txt setup.py pyproject.toml setup.cfg 2>/dev/null; then "
                "pip install -r requirements.txt 2>/dev/null || true && "
                'pip install -e ".[dev,test]" 2>/dev/null || pip install -e . 2>/dev/null || true; fi\n'
            )
        if language in ("javascript", "typescript"):
            return (
                "RUN if ! git diff HEAD --quiet -- package.json package-lock.json 2>/dev/null; then "
                "npm install 2>/dev/null || true; fi\n"
            )
        if language == "go":
            return (
                "RUN if ! git diff HEAD --quiet -- go.mod go.sum 2>/dev/null; then "
                "go mod download 2>/dev/null || true; fi\n"
            )
        if language == "rust":
            return (
                "RUN if ! git diff HEAD --quiet -- Cargo.toml Cargo.lock 2>/dev/null; then "
                "cargo fetch 2>/dev/null || true; fi\n"
            )
        return ""

    def _generate_fix_run_script(self, instance):
        """Generate the test runner script for a Docker instance.

        This script runs inside the container after patches have been applied
        at runtime by _execute_docker_run(). It only needs to run the test suite.

        If test_config_json provides test_cmd/prepare_cmd, those override
        the auto-detected commands entirely.
        """
        import json

        config = self._get_effective_config()

        # Config-override path: human-specified test_cmd takes precedence
        if config.get("test_cmd"):
            prepare = config.get("prepare_cmd", "")
            test_cmd = config["test_cmd"]
            lines = [
                "#!/bin/bash",
                "set -uo pipefail",
                "cd /testbed",
                "echo '>>>>> Start Test Output'",
            ]
            if prepare:
                lines.append(f"{prepare} 2>&1")
            lines += [
                f"{test_cmd} 2>&1",
                "echo '>>>>> End Test Output'",
            ]
            return "\n".join(lines) + "\n"

        # Auto-detection path (existing behavior)
        test_files = ""
        if instance.selected_test_files_json:
            try:
                files = json.loads(instance.selected_test_files_json)
                if files:
                    test_files = " ".join(files)
            except (json.JSONDecodeError, TypeError):
                pass

        lang = (instance.language or "python").lower()

        if lang == "python":
            if test_files:
                test_target = test_files
            else:
                # Auto-detect: check common test directory names at runtime
                test_target = (
                    '$(if [ -d tests ]; then echo tests/; '
                    'elif [ -d test ]; then echo test/; '
                    'else echo .; fi)'
                )
            test_cmd = f"python -m pytest {test_target} -v 2>&1"
            return (
                "#!/bin/bash\n"
                "set -uo pipefail\n"
                "cd /testbed\n"
                "echo '>>>>> Start Test Output'\n"
                f"{test_cmd} || true\n"
                "echo '>>>>> End Test Output'\n"
            )
        if lang in ("javascript", "typescript"):
            return self._generate_js_fix_run_script(test_files)
        if lang == "go":
            if test_files:
                packages = set()
                for f in test_files.split():
                    parts = f.rsplit("/", 1)
                    packages.add("./" + parts[0] if len(parts) > 1 else "./...")
                pkg_arg = " ".join(sorted(packages))
            else:
                pkg_arg = "./..."
            test_cmd = f"go test -v -count=1 -timeout 15m {pkg_arg} 2>&1"
        elif lang == "rust":
            test_cmd = "cargo test 2>&1"
        elif lang == "java":
            test_cmd = (
                "if [ -f pom.xml ]; then mvn clean test -fn 2>&1; "
                "elif [ -f build.gradle ] || [ -f build.gradle.kts ]; then ./gradlew test 2>&1; "
                "else echo 'No build system detected' && exit 1; fi"
            )
        elif lang == "c":
            test_cmd = (
                "if [ -f CMakeLists.txt ]; then "
                "mkdir -p build && cd build && cmake .. && make -j$(nproc) && ctest --output-on-failure 2>&1; "
                "elif [ -f Makefile ]; then make test 2>&1; "
                "else echo 'No build system detected' && exit 1; fi"
            )
        elif lang == "cpp":
            test_cmd = (
                "mkdir -p build && cd build && "
                "cmake -DBUILD_TESTING=ON .. && make -j$(nproc) && "
                "ctest --output-on-failure 2>&1"
            )
        else:
            test_cmd = f"python -m pytest {test_files or '.'} -v 2>&1"

        return (
            "#!/bin/bash\n"
            "set -uo pipefail\n"
            "cd /testbed\n"
            "echo '>>>>> Start Test Output'\n"
            f"{test_cmd} || true\n"
            "echo '>>>>> End Test Output'\n"
        )

    def _generate_js_fix_run_script(self, test_files=""):
        """Generate a runtime-adaptive test script for JS/TS repos.

        Instead of hardcoding 'npm test', the script inspects the checked-out
        code at base_sha to find the actual test runner and install deps if needed.
        This handles repos where old commits use different test setups than HEAD.
        """
        return r"""#!/bin/bash
set -uo pipefail
cd /testbed
echo '>>>>> Start Test Output'

# Install deps if package.json exists at this commit
if [ -f package.json ]; then
    npm install --ignore-scripts 2>/dev/null || true
    # Some repos need postinstall/build steps
    npm run build 2>/dev/null || true
fi

# Detect and run the test command from the actual checked-out code
if [ -f package.json ]; then
    # Read the test script from package.json
    TEST_SCRIPT=$(node -e "try{const p=require('./package.json');console.log(p.scripts&&p.scripts.test||'')}catch(e){console.log('')}" 2>/dev/null)
    if [ -n "$TEST_SCRIPT" ]; then
        npm test 2>&1
    else
        # No test script — try common runners directly
        if command -v jest &>/dev/null || [ -f node_modules/.bin/jest ]; then
            npx jest --verbose 2>&1
        elif command -v mocha &>/dev/null || [ -f node_modules/.bin/mocha ]; then
            npx mocha --recursive 2>&1
        elif command -v ava &>/dev/null || [ -f node_modules/.bin/ava ]; then
            npx ava 2>&1
        elif command -v vitest &>/dev/null || [ -f node_modules/.bin/vitest ]; then
            npx vitest run 2>&1
        else
            echo 'No test runner found' 2>&1
        fi
    fi
else
    echo 'No package.json at this commit' 2>&1
fi

echo '>>>>> End Test Output'
"""

    def _build_via_kaiju(self, instance, full_tag, dockerfile_path):
        """Build via kaiju_build K8s job system."""
        KaijuBuild = self.env["kaiju.build"]
        build = KaijuBuild.create({
            "name": f"jaeger-{instance.name}",
            "image_name": full_tag,
            "dockerfile_path": dockerfile_path,
        })
        self.write({"kaiju_build_id": build.id})
        build.action_start_build()
