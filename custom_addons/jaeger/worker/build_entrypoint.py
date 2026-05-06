#!/usr/bin/env python3
"""K8s pod entrypoint for Jaeger Stage 3 — Docker Image Build.

NO Odoo imports. Reads config from environment variables.
Builds base image (Layer 1) + per-instance images (Layer 2),
pushes to container registry, reports progress via webhook.

Environment variables (required unless noted):
    REPO_ID            - jaeger.repository record ID
    REPO_ORG           - GitHub org
    REPO_NAME          - GitHub repo name
    REPO_LANGUAGE      - Repository language (default: python)
    GITHUB_TOKENS      - Comma-separated GitHub PATs (for BuildKit secret)
    MANIFEST_S3_KEY    - S3 key for instances manifest JSON
    DOCKER_PLATFORM    - Build platform (default: linux/amd64)
    CONTAINER_REGISTRY - Registry to push images (e.g. ECR prefix or localhost:5000)
    TEST_CONFIG_JSON   - Effective test config JSON (optional)
    BASE_IMAGE_STATUS  - "built" if base already exists (optional)
    S3_BUCKET          - S3 bucket
    S3_REGION          - AWS region (default: ap-south-1)
    S3_PREFIX          - S3 key prefix (default: jaeger/phase1)
    WEBHOOK_URL        - Odoo webhook endpoint
"""
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("jaeger.build_worker")

# ── Configuration ────────────────────────────────────────────────────────

REPO_ID = os.environ.get("REPO_ID", "")
REPO_ORG = os.environ.get("REPO_ORG", "")
REPO_NAME = os.environ.get("REPO_NAME", "")
REPO_LANGUAGE = os.environ.get("REPO_LANGUAGE", "python")
GITHUB_TOKENS = os.environ.get("GITHUB_TOKENS", "")
MANIFEST_S3_KEY = os.environ.get("MANIFEST_S3_KEY", "")
DOCKER_PLATFORM = os.environ.get("DOCKER_PLATFORM", "linux/amd64")
CONTAINER_REGISTRY = os.environ.get("CONTAINER_REGISTRY", "")
TEST_CONFIG_JSON = os.environ.get("TEST_CONFIG_JSON", "")
BASE_IMAGE_STATUS = os.environ.get("BASE_IMAGE_STATUS", "none")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "ap-south-1")
S3_PREFIX = os.environ.get("S3_PREFIX", "jaeger/phase1")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

LANGUAGE_BASE_IMAGES = {
    "python": "python:3.11-slim",
    "javascript": "node:20-slim",
    "typescript": "node:20-slim",
    "java": "eclipse-temurin:17-jdk",
    "go": "golang:1.22",
    "rust": "rust:1.85",
    "c": "ubuntu:22.04",
    "cpp": "ubuntu:22.04",
}

# ── SIGTERM handling ─────────────────────────────────────────────────────

_cancelled = False


def _sigterm_handler(signum, frame):
    global _cancelled
    _cancelled = True
    _logger.warning("Received SIGTERM — will stop after current build.")


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _sigterm_handler)


def _check_cancelled():
    if _cancelled:
        raise RuntimeError("Pipeline cancelled (SIGTERM)")


# ── Webhook helpers ──────────────────────────────────────────────────────

def _post_webhook(payload):
    if not WEBHOOK_URL:
        return
    import requests
    body = {"jsonrpc": "2.0", "method": "call", "params": payload}
    try:
        resp = requests.post(
            WEBHOOK_URL, json=body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            _logger.warning("Webhook returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        _logger.warning("Webhook POST failed: %s", e)


def send_heartbeat():
    _post_webhook({"repo_id": int(REPO_ID), "type": "heartbeat"})


def send_build_progress(instance_id, status, image_name="", log_tail=""):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "build_progress",
        "instance_id": instance_id,
        "status": status,
        "image_name": image_name,
        "log_tail": log_tail[-3000:] if log_tail else "",
    })


def send_build_base_done(base_image_name, base_image_status):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "build_base_done",
        "base_image_name": base_image_name,
        "base_image_status": base_image_status,
    })


def send_build_done(images_built, images_failed):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "build_done",
        "images_built_count": images_built,
        "images_failed_count": images_failed,
    })


def send_build_failed(error):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "build_failed",
        "error": str(error)[:2000],
    })


# ── S3 helpers ───────────────────────────────────────────────────────────

def _get_s3_client():
    import boto3
    from botocore.config import Config
    config_kwargs = {
        "retries": {"mode": "standard", "max_attempts": 5},
        "connect_timeout": 30,
        "read_timeout": 60,
    }
    endpoint = os.environ.get("JAEGER_S3_ENDPOINT")
    if endpoint:
        config_kwargs["s3"] = {"addressing_style": "path"}
    return boto3.client(
        "s3", region_name=S3_REGION,
        endpoint_url=endpoint or f"https://s3.{S3_REGION}.amazonaws.com",
        config=Config(**config_kwargs),
    )


def download_manifest():
    """Download instances manifest from S3."""
    if not MANIFEST_S3_KEY:
        raise RuntimeError("MANIFEST_S3_KEY not set")
    client = _get_s3_client()
    resp = client.get_object(Bucket=S3_BUCKET, Key=MANIFEST_S3_KEY)
    return json.loads(resp["Body"].read().decode("utf-8"))


# ── Docker helpers ───────────────────────────────────────────────────────

def _wait_for_docker(timeout=60):
    """Wait for DinD sidecar to become ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                _logger.info("Docker daemon is ready")
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("Docker daemon not ready after %ds" % timeout)


def _docker_image_exists(tag):
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_effective_config():
    config = {}
    if TEST_CONFIG_JSON:
        try:
            config = json.loads(TEST_CONFIG_JSON)
        except (json.JSONDecodeError, TypeError):
            pass
    lang = REPO_LANGUAGE.lower()
    config.setdefault("base_image", LANGUAGE_BASE_IMAGES.get(lang, "python:3.11-slim"))
    config.setdefault("memory_limit", "8g" if lang in ("rust", "cpp", "c", "java") else "4g")
    config.setdefault("network", lang != "python")
    config.setdefault("parser", None)
    return config


# ── Dockerfile generation (mirrors jaeger_stage3_docker.py logic) ────────

def _detect_install_commands(repo_dir):
    p = Path(repo_dir)
    cmds = []
    has_pyproject = (p / "pyproject.toml").exists()
    has_setup_py = (p / "setup.py").exists()
    has_setup_cfg = (p / "setup.cfg").exists()

    if has_pyproject or has_setup_py or has_setup_cfg:
        cmds.append('pip install -e ".[dev,test]" 2>/dev/null || pip install -e . || true')
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


def _generate_base_dockerfile(config, install_cmds):
    """Generate Dockerfile content for the repo base image (Layer 1)."""
    lang = REPO_LANGUAGE.lower()
    runtime = config.get("base_image", LANGUAGE_BASE_IMAGES.get(lang, "python:3.11-slim"))
    is_python = lang == "python"
    is_node = lang in ("javascript", "typescript")
    clone_url = f"https://github.com/{REPO_ORG}/{REPO_NAME}.git"

    lines = [
        "# syntax=docker/dockerfile:1.6",
        "",
        f"FROM {runtime}",
        "",
        'ARG TARGETARCH',
        'ARG http_proxy=""',
        'ARG https_proxy=""',
        "",
        "ENV DEBIAN_FRONTEND=noninteractive \\",
        "    LANG=C.UTF-8 \\",
        "    TZ=UTC",
        "",
        f'LABEL org.opencontainers.image.title="{REPO_ORG}/{REPO_NAME}" \\',
        f'      org.opencontainers.image.source="https://github.com/{REPO_ORG}/{REPO_NAME}" \\',
        '      org.opencontainers.image.authors="https://www.ethara.ai/"',
        "",
    ]

    # System deps
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
    elif lang in ("c", "cpp"):
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
        f"        git clone \"https://x-access-token:$(cat $TOKEN_FILE)@github.com/{REPO_ORG}/{REPO_NAME}.git\" . ; \\",
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

    config_env = config.get("env")
    if config_env and isinstance(config_env, dict):
        for k, v in config_env.items():
            lines.append(f'ENV {k}="{v}"')
        lines.append("")

    lines.append('LABEL jaeger.image.type="base"')
    return "\n".join(lines) + "\n"


def _generate_instance_dockerfile(instance, base_image_name, config):
    """Generate Dockerfile for a per-instance image (Layer 2)."""
    lang = REPO_LANGUAGE.lower()
    base_sha = instance.get("base_sha", "")

    checkout_cmd = ""
    reinstall_cmd = ""
    if base_sha:
        checkout_cmd = (
            f"RUN git checkout -- . && git clean -fd && "
            f"(git checkout {base_sha} || (git fetch origin {base_sha} && git checkout {base_sha}))\n"
        )
        config_install = config.get("install_cmd")
        if config_install:
            reinstall_cmd = f"RUN {config_install} 2>/dev/null || true\n"
        else:
            reinstall_cmd = _dep_reinstall_commands(lang)

    org = instance.get("org", REPO_ORG)
    repo = instance.get("repo", REPO_NAME)
    name = instance.get("name", "")

    return f"""FROM {base_image_name}

WORKDIR /testbed
{checkout_cmd}{reinstall_cmd}
COPY fix-run.sh /jaeger/fix-run.sh
RUN chmod +x /jaeger/fix-run.sh

LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.revision="{base_sha}"
LABEL jaeger.instance="{name}"
"""


def _dep_reinstall_commands(language):
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


def _generate_fix_run_script(instance, config):
    """Generate test runner script (mirrors _generate_fix_run_script from stage3)."""
    lang = REPO_LANGUAGE.lower()

    if config.get("test_cmd"):
        prepare = config.get("prepare_cmd", "")
        test_cmd = config["test_cmd"]
        lines = ["#!/bin/bash", "set -uo pipefail", "cd /testbed", "echo '>>>>> Start Test Output'"]
        if prepare:
            lines.append(f"{prepare} 2>&1")
        lines += [f"{test_cmd} 2>&1", "echo '>>>>> End Test Output'"]
        return "\n".join(lines) + "\n"

    test_files = ""
    selected = instance.get("selected_test_files_json")
    if selected:
        try:
            files = json.loads(selected)
            if files:
                test_files = " ".join(files)
        except (json.JSONDecodeError, TypeError):
            pass

    if lang == "python":
        if test_files:
            test_target = test_files
        else:
            test_target = (
                '$(if [ -d tests ]; then echo tests/; '
                'elif [ -d test ]; then echo test/; '
                'else echo .; fi)'
            )
        test_cmd = f"python -m pytest {test_target} -v 2>&1"
    elif lang in ("javascript", "typescript"):
        return _generate_js_fix_run_script()
    elif lang == "go":
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


def _generate_js_fix_run_script():
    return r"""#!/bin/bash
set -uo pipefail
cd /testbed
echo '>>>>> Start Test Output'

if [ -f package.json ]; then
    npm install --ignore-scripts 2>/dev/null || true
    npm run build 2>/dev/null || true
fi

if [ -f package.json ]; then
    TEST_SCRIPT=$(node -e "try{const p=require('./package.json');console.log(p.scripts&&p.scripts.test||'')}catch(e){console.log('')}" 2>/dev/null)
    if [ -n "$TEST_SCRIPT" ]; then
        npm test 2>&1
    else
        if command -v jest &>/dev/null || [ -f node_modules/.bin/jest ]; then
            npx jest --verbose 2>&1
        elif command -v mocha &>/dev/null || [ -f node_modules/.bin/mocha ]; then
            npx mocha --recursive 2>&1
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


# ── Build logic ──────────────────────────────────────────────────────────

def build_base_image(config):
    """Build the repo base image (Layer 1). Returns the image tag."""
    base_tag = f"mswebench/{REPO_ORG}_m_{REPO_NAME}:base".lower()

    if _docker_image_exists(base_tag):
        _logger.info("Base image already exists: %s", base_tag)
        send_build_base_done(base_tag, "built")
        return base_tag

    _logger.info("Building base image: %s", base_tag)
    tokens = [t.strip() for t in GITHUB_TOKENS.split(",") if t.strip()]
    github_token = tokens[0] if tokens else ""

    authed_clone_url = (
        f"https://x-access-token:{github_token}@github.com/{REPO_ORG}/{REPO_NAME}.git"
        if github_token else f"https://github.com/{REPO_ORG}/{REPO_NAME}.git"
    )

    clone_dir = tempfile.mkdtemp(prefix="jaeger_base_")
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", authed_clone_url, clone_dir],
            check=True, capture_output=True, text=True, timeout=120,
        )

        config_install = config.get("install_cmd")
        if config_install:
            install_cmds = [config_install]
        else:
            install_cmds = _detect_install_commands(clone_dir)

        dockerfile_content = _generate_base_dockerfile(config, install_cmds)

        build_dir = Path(clone_dir) / "_docker_build"
        build_dir.mkdir(exist_ok=True)
        (build_dir / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

        cmd = ["docker", "buildx", "build", "--load"]
        if DOCKER_PLATFORM:
            cmd += ["--platform", DOCKER_PLATFORM]

        token_file = None
        if github_token:
            token_file = build_dir / ".github_token"
            token_file.write_text(github_token, encoding="utf-8")
            token_file.chmod(0o600)
            cmd += ["--secret", f"id=github_token,src={token_file}"]

        cmd += ["-t", base_tag, "-f", str(build_dir / "Dockerfile"), str(build_dir)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

        if token_file and token_file.exists():
            token_file.unlink()

        if result.returncode != 0:
            _logger.error("Base image build FAILED:\n%s", result.stderr[-3000:])
            send_build_base_done(base_tag, "failed")
            raise RuntimeError(f"Base image build failed: {result.stderr[-1000:]}")

        # Push to registry if configured
        if CONTAINER_REGISTRY:
            registry_tag = f"{CONTAINER_REGISTRY}/{base_tag}"
            subprocess.run(["docker", "tag", base_tag, registry_tag], check=True, timeout=30)
            subprocess.run(["docker", "push", registry_tag], check=True, timeout=600)
            _logger.info("Pushed base image to registry: %s", registry_tag)

        _logger.info("Base image built: %s", base_tag)
        send_build_base_done(base_tag, "built")
        return base_tag

    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def build_instance_image(instance, base_image_name, config, workdir):
    """Build a single per-instance image (Layer 2). Returns (tag, success, log)."""
    inst_id = instance["id"]
    pr_number = instance["pr_number"]
    org = instance.get("org", REPO_ORG)
    repo = instance.get("repo", REPO_NAME)

    image_name = f"mswebench/{org}_m_{repo}".lower()
    image_tag = f"pr-{pr_number}-{inst_id}"
    full_tag = f"{image_name}:{image_tag}"
    if CONTAINER_REGISTRY:
        full_tag = f"{CONTAINER_REGISTRY}/{image_name}:{image_tag}"

    build_dir = workdir / f"{org}__{repo}" / f"pr-{pr_number}"
    build_dir.mkdir(parents=True, exist_ok=True)

    try:
        dockerfile = _generate_instance_dockerfile(instance, base_image_name, config)
        (build_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")

        fix_run = _generate_fix_run_script(instance, config)
        (build_dir / "fix-run.sh").write_text(fix_run, encoding="utf-8")

        cmd = ["docker", "build"]
        if DOCKER_PLATFORM:
            cmd += ["--platform", DOCKER_PLATFORM]
        cmd += ["-t", full_tag, "-f", str(build_dir / "Dockerfile"), str(build_dir)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        build_log = result.stdout[-5000:] + result.stderr[-5000:]

        if result.returncode != 0:
            return full_tag, False, build_log

        # Push to registry
        if CONTAINER_REGISTRY:
            subprocess.run(["docker", "push", full_tag], check=True, timeout=600)

        return full_tag, True, build_log

    except subprocess.TimeoutExpired:
        return full_tag, False, "Build timed out (1800s)"
    except Exception as e:
        return full_tag, False, str(e)[:5000]


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    _logger.info(
        "Stage 3 Build Worker: repo_id=%s, org=%s, repo=%s, lang=%s",
        REPO_ID, REPO_ORG, REPO_NAME, REPO_LANGUAGE,
    )

    missing = []
    for var in ("REPO_ID", "REPO_ORG", "REPO_NAME", "GITHUB_TOKENS", "S3_BUCKET", "WEBHOOK_URL", "MANIFEST_S3_KEY"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        msg = "Missing required env vars: %s" % ", ".join(missing)
        _logger.error(msg)
        send_build_failed(msg)
        sys.exit(1)

    try:
        _wait_for_docker(timeout=90)
    except RuntimeError as e:
        send_build_failed(str(e))
        sys.exit(1)

    config = _get_effective_config()
    workdir = Path("/tmp/jaeger_docker")
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Build base image
        _check_cancelled()
        send_heartbeat()
        base_tag = build_base_image(config)

        # 2. Download instance manifest
        _check_cancelled()
        instances = download_manifest()
        total = len(instances)
        _logger.info("Building %d instance images", total)

        # 3. Build each instance image
        built_count = 0
        failed_count = 0

        for idx, instance in enumerate(instances, 1):
            _check_cancelled()
            send_heartbeat()

            inst_id = instance["id"]
            if not instance.get("base_sha"):
                send_build_progress(inst_id, "failed", log_tail="Missing base_sha")
                failed_count += 1
                continue

            send_build_progress(inst_id, "building")
            full_tag, success, build_log = build_instance_image(
                instance, base_tag, config, workdir,
            )

            if success:
                send_build_progress(inst_id, "built", image_name=full_tag)
                built_count += 1
            else:
                send_build_progress(inst_id, "failed", log_tail=build_log)
                failed_count += 1

            _logger.info("  [%d/%d] %s %s", idx, total, "BUILT" if success else "FAILED", full_tag)

        # 4. Report final status
        if built_count == 0 and total > 0:
            send_build_failed("All %d image builds failed" % total)
        else:
            send_build_done(built_count, failed_count)

        _logger.info("Build complete: %d built, %d failed", built_count, failed_count)

    except RuntimeError as e:
        if "cancelled" in str(e).lower():
            _logger.warning("Build cancelled")
            send_build_failed("Pipeline cancelled (SIGTERM)")
        else:
            _logger.exception("Build failed")
            send_build_failed(str(e))
        sys.exit(1)
    except Exception as e:
        _logger.exception("Build failed unexpectedly")
        send_build_failed(str(e))
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
