#!/usr/bin/env python3
"""Jaeger Phase 2 — Combined Build + Test in single privileged pod.

Kaiju-style: single container with Docker daemon built in (docker:27-dind).
Builds per-PR images, runs 3-run test pattern, reports results via webhook.

Environment variables:
    REPO_ID, REPO_ORG, REPO_NAME, REPO_LANGUAGE
    GITHUB_TOKENS       - Comma-separated GitHub PATs
    MANIFEST_S3_KEY     - S3 key for instances manifest JSON
    DOCKER_PLATFORM     - Build platform (default: linux/amd64)
    TEST_CONFIG_JSON    - Effective test config JSON (optional)
    AGENT_TIMEOUT       - Per-container timeout (default: 1800)
    S3_BUCKET, S3_REGION, S3_PREFIX
    WEBHOOK_URL
"""
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("jaeger.phase2")

# ── Configuration ────────────────────────────────────────────────────────

REPO_ID = os.environ.get("REPO_ID", "")
REPO_ORG = os.environ.get("REPO_ORG", "")
REPO_NAME = os.environ.get("REPO_NAME", "")
REPO_LANGUAGE = os.environ.get("REPO_LANGUAGE", "python")
GITHUB_TOKENS = os.environ.get("GITHUB_TOKENS", "")
MANIFEST_S3_KEY = os.environ.get("MANIFEST_S3_KEY", "")
DOCKER_PLATFORM = os.environ.get("DOCKER_PLATFORM", "linux/amd64")
TEST_CONFIG_JSON = os.environ.get("TEST_CONFIG_JSON", "")
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "1800"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
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
    _logger.warning("Received SIGTERM — stopping after current operation.")


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


def send_test_progress(instance_id, is_valid, summary, run_log="", test_patch_log="",
                       fix_patch_log="", run_result=None, test_result=None, fix_result=None):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "test_progress",
        "instance_id": instance_id,
        "is_valid": is_valid,
        "summary": summary[:500],
        "run_log": run_log[-5000:],
        "test_patch_log": test_patch_log[-5000:],
        "fix_patch_log": fix_patch_log[-5000:],
        "run_result": run_result,
        "test_result": test_result,
        "fix_result": fix_result,
    })


def send_test_done(valid_count, invalid_count, error_count):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "test_done",
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "error_count": error_count,
    })


def send_test_failed(error):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "test_failed",
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
    if not MANIFEST_S3_KEY:
        raise RuntimeError("MANIFEST_S3_KEY not set")
    client = _get_s3_client()
    resp = client.get_object(Bucket=S3_BUCKET, Key=MANIFEST_S3_KEY)
    return json.loads(resp["Body"].read().decode("utf-8"))


# ── Docker helpers ───────────────────────────────────────────────────────

def _wait_for_docker(timeout=120):
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


# ── Dockerfile generation ────────────────────────────────────────────────

def _detect_install_commands(repo_dir):
    p = Path(repo_dir)
    cmds = []
    if (p / "pyproject.toml").exists() or (p / "setup.py").exists() or (p / "setup.cfg").exists():
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
        "ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 TZ=UTC",
        "",
    ]

    custom_deps = config.get("system_deps")
    if custom_deps:
        deps_str = " ".join(custom_deps)
        lines += [
            f"RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git {deps_str} && rm -rf /var/lib/apt/lists/*",
            "",
        ]
    elif is_python:
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git make gcc g++ curl && rm -rf /var/lib/apt/lists/*",
            "",
        ]
    elif is_node:
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git make gcc g++ python3 && rm -rf /var/lib/apt/lists/*",
            "",
        ]
    elif lang in ("c", "cpp"):
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git make gcc g++ cmake python3 python3-pip curl && rm -rf /var/lib/apt/lists/*",
            "",
        ]
    else:
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git make curl && rm -rf /var/lib/apt/lists/*",
            "",
        ]

    lines += [
        "WORKDIR /testbed",
        "RUN --mount=type=secret,id=github_token,required=false \\",
        '    TOKEN_FILE=/run/secrets/github_token && \\',
        '    if [ -f "$TOKEN_FILE" ] && [ -s "$TOKEN_FILE" ]; then \\',
        f'        git clone "https://x-access-token:$(cat $TOKEN_FILE)@github.com/{REPO_ORG}/{REPO_NAME}.git" . ; \\',
        "    else \\",
        f'        git clone "{clone_url}" . ; \\',
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

    return "\n".join(lines) + "\n"


def _generate_instance_dockerfile(instance, base_image_name, config):
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

    return f"""FROM {base_image_name}
WORKDIR /testbed
{checkout_cmd}{reinstall_cmd}
COPY fix-run.sh /jaeger/fix-run.sh
RUN chmod +x /jaeger/fix-run.sh
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
        test_target = test_files if test_files else '$(if [ -d tests ]; then echo tests/; elif [ -d test ]; then echo test/; else echo .; fi)'
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
            "mkdir -p build && cd build && cmake -DBUILD_TESTING=ON .. && make -j$(nproc) && "
            "ctest --output-on-failure 2>&1"
        )
    else:
        test_cmd = f"python -m pytest {test_files or '.'} -v 2>&1"

    return (
        "#!/bin/bash\nset -uo pipefail\ncd /testbed\n"
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
    base_tag = f"mswebench/{REPO_ORG}_m_{REPO_NAME}:base".lower()

    if _docker_image_exists(base_tag):
        _logger.info("Base image already exists: %s", base_tag)
        send_build_base_done(base_tag, "built")
        return base_tag

    _logger.info("Building base image: %s", base_tag)
    tokens = [t.strip() for t in GITHUB_TOKENS.split(",") if t.strip()]
    github_token = tokens[0] if tokens else ""

    clone_dir = tempfile.mkdtemp(prefix="jaeger_base_")
    try:
        authed_url = (
            f"https://x-access-token:{github_token}@github.com/{REPO_ORG}/{REPO_NAME}.git"
            if github_token else f"https://github.com/{REPO_ORG}/{REPO_NAME}.git"
        )
        subprocess.run(
            ["git", "clone", "--depth=1", authed_url, clone_dir],
            check=True, capture_output=True, text=True, timeout=120,
        )

        config_install = config.get("install_cmd")
        install_cmds = [config_install] if config_install else _detect_install_commands(clone_dir)

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

        _logger.info("Base image built: %s", base_tag)
        send_build_base_done(base_tag, "built")
        return base_tag

    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def build_instance_image(instance, base_image_name, config, workdir):
    inst_id = instance["id"]
    pr_number = instance["pr_number"]
    org = instance.get("org", REPO_ORG)
    repo = instance.get("repo", REPO_NAME)

    image_name = f"mswebench/{org}_m_{repo}".lower()
    image_tag = f"pr-{pr_number}-{inst_id}"
    full_tag = f"{image_name}:{image_tag}"

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

        return full_tag, True, build_log

    except subprocess.TimeoutExpired:
        return full_tag, False, "Build timed out (1800s)"
    except Exception as e:
        return full_tag, False, str(e)[:5000]


# ── Test logic ───────────────────────────────────────────────────────────

def _cleanup_container(container_name):
    try:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=30)
    except Exception:
        pass


def _execute_docker_run(inst_name, docker_image, mode, patches, timeout,
                        memory_limit="4g", language="python", network_enabled=None):
    tag = uuid4().hex[:8]
    container_name = f"jaeger-{inst_name}-{mode}-{tag}".replace("/", "-").replace("__", "-").lower()

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=30)

    cmd = ["docker", "run", "--name", container_name, "--memory", memory_limit, "--memory-swap", memory_limit]

    if network_enabled is not None:
        if not network_enabled:
            cmd.extend(["--network", "none"])
    elif (language or "").lower() == "python":
        cmd.extend(["--network", "none"])

    reset_prefix = "git checkout -- . && git clean -fd && "

    if patches:
        with tempfile.TemporaryDirectory() as tmpdir:
            for patch_name, patch_content in patches.items():
                if patch_content:
                    (Path(tmpdir) / f"{patch_name}.diff").write_text(patch_content, encoding="utf-8")

            cmd.extend(["-v", f"{tmpdir}:/patches:ro"])
            cmd.append(docker_image)

            if mode == "test_patch":
                cmd.extend(["bash", "-c",
                    f"cd /testbed && {reset_prefix}"
                    f"git apply --whitespace=nowarn /patches/test_patch.diff && "
                    "bash /jaeger/fix-run.sh"])
            elif mode == "fix_patch":
                cmd.extend(["bash", "-c",
                    f"cd /testbed && {reset_prefix}"
                    f"git apply --whitespace=nowarn /patches/fix_patch.diff && "
                    f"git apply --whitespace=nowarn /patches/test_patch.diff && "
                    "bash /jaeger/fix-run.sh"])

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                _cleanup_container(container_name)
                raise
            finally:
                _cleanup_container(container_name)
    else:
        cmd.append(docker_image)
        cmd.extend(["bash", "-c", "cd /testbed && bash /jaeger/fix-run.sh"])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _cleanup_container(container_name)
            raise
        finally:
            _cleanup_container(container_name)

    return result.stdout + "\n" + result.stderr


def _parse_test_log(log_text):
    if not log_text:
        return {"passed_count": 0, "failed_count": 0, "skipped_count": 0,
                "passed_tests": [], "failed_tests": [], "skipped_tests": []}

    start_marker = ">>>>> Start Test Output"
    end_marker = ">>>>> End Test Output"
    start_idx = log_text.find(start_marker)
    end_idx = log_text.find(end_marker)
    if start_idx >= 0 and end_idx > start_idx:
        test_output = log_text[start_idx + len(start_marker):end_idx]
    else:
        test_output = log_text

    passed_tests = []
    failed_tests = []
    skipped_tests = []
    lang = REPO_LANGUAGE.lower()

    if lang == "go":
        for m in re.finditer(r"--- (PASS|FAIL|SKIP): (\S+)", test_output):
            status, name = m.group(1), m.group(2)
            if status == "PASS":
                passed_tests.append(name)
            elif status == "FAIL":
                failed_tests.append(name)
            else:
                skipped_tests.append(name)
    elif lang == "rust":
        for m in re.finditer(r"test (\S+) \.\.\. (ok|FAILED|ignored)", test_output):
            name, status = m.group(1), m.group(2)
            if status == "ok":
                passed_tests.append(name)
            elif status == "FAILED":
                failed_tests.append(name)
            else:
                skipped_tests.append(name)
    elif lang in ("javascript", "typescript"):
        for m in re.finditer(r"[✓✔] (.+)", test_output):
            passed_tests.append(m.group(1).strip())
        for m in re.finditer(r"[✕✗✖×] (.+)", test_output):
            failed_tests.append(m.group(1).strip())
    elif lang == "java":
        for m in re.finditer(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)", test_output):
            p = int(m.group(1)) - int(m.group(2)) - int(m.group(3)) - int(m.group(4))
            return {
                "passed_count": max(p, 0), "failed_count": int(m.group(2)) + int(m.group(3)),
                "skipped_count": int(m.group(4)),
                "passed_tests": [], "failed_tests": [], "skipped_tests": [],
            }
    else:
        for m in re.finditer(r"(PASSED|FAILED|ERROR|SKIPPED) (.+)", test_output):
            status, name = m.group(1), m.group(2).strip()
            if status == "PASSED":
                passed_tests.append(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.append(name)
            else:
                skipped_tests.append(name)
        match = re.search(r"(\d+) passed", test_output)
        if match and not passed_tests:
            failed_m = re.search(r"(\d+) failed", test_output)
            skipped_m = re.search(r"(\d+) skipped", test_output)
            return {
                "passed_count": int(match.group(1)),
                "failed_count": int(failed_m.group(1)) if failed_m else 0,
                "skipped_count": int(skipped_m.group(1)) if skipped_m else 0,
                "passed_tests": [], "failed_tests": [], "skipped_tests": [],
            }

    return {
        "passed_count": len(passed_tests),
        "failed_count": len(failed_tests),
        "skipped_count": len(skipped_tests),
        "passed_tests": passed_tests[:200],
        "failed_tests": failed_tests[:200],
        "skipped_tests": skipped_tests[:200],
    }


def _classify_tests(run_result, test_result, fix_result):
    run_passed = set(run_result.get("passed_tests", []))
    run_failed = set(run_result.get("failed_tests", []))
    fix_passed = set(fix_result.get("passed_tests", []))
    fix_failed = set(fix_result.get("failed_tests", []))

    f2p = run_failed & fix_passed
    f2p_count = len(f2p)
    total_captured = fix_result.get("passed_count", 0) + fix_result.get("failed_count", 0)

    regressions = run_passed & fix_failed
    if regressions:
        return f2p_count, False, "Regressions detected: %d tests PASS→FAIL" % len(regressions)
    if total_captured == 0:
        return 0, False, "No tests captured in fix run"
    if f2p_count == 0:
        return 0, False, "No f2p tests (fix doesn't resolve any failing test)"

    return f2p_count, True, ""


def run_instance_tests(instance, config):
    inst_id = instance["id"]
    inst_name = instance["name"]
    docker_image = instance["docker_image_tag"]
    fix_patch = instance.get("fix_patch", "")
    test_patch = instance.get("test_patch", "")

    lang = REPO_LANGUAGE.lower()
    memory_limit = config.get("memory_limit", "8g" if lang in ("rust", "cpp", "c", "java") else "4g")
    network_enabled = config.get("network")

    if not fix_patch or not fix_patch.strip():
        return {"instance_id": inst_id, "success": False, "is_valid": False,
                "error": "Empty fix_patch", "summary": "Empty fix_patch"}
    if not test_patch or not test_patch.strip():
        return {"instance_id": inst_id, "success": False, "is_valid": False,
                "error": "Empty test_patch", "summary": "Empty test_patch"}

    try:
        run_log = _execute_docker_run(inst_name, docker_image, "run", None, AGENT_TIMEOUT, memory_limit, lang, network_enabled)
        test_patch_log = _execute_docker_run(inst_name, docker_image, "test_patch", {"test_patch": test_patch}, AGENT_TIMEOUT, memory_limit, lang, network_enabled)
        fix_patch_log = _execute_docker_run(inst_name, docker_image, "fix_patch", {"fix_patch": fix_patch, "test_patch": test_patch}, AGENT_TIMEOUT, memory_limit, lang, network_enabled)
    except subprocess.TimeoutExpired:
        return {"instance_id": inst_id, "success": False, "is_valid": False,
                "error": "Container timeout", "summary": "timeout"}
    except Exception as e:
        return {"instance_id": inst_id, "success": False, "is_valid": False,
                "error": str(e)[:500], "summary": f"docker error: {e}"}

    run_result = _parse_test_log(run_log)
    test_result = _parse_test_log(test_patch_log)
    fix_result = _parse_test_log(fix_patch_log)

    f2p_count, is_valid, validation_error = _classify_tests(run_result, test_result, fix_result)
    summary = f"valid ({f2p_count} f2p)" if is_valid else (validation_error or "invalid")

    send_test_progress(
        inst_id, is_valid, summary,
        run_log=run_log, test_patch_log=test_patch_log, fix_patch_log=fix_patch_log,
        run_result=run_result, test_result=test_result, fix_result=fix_result,
    )

    return {"instance_id": inst_id, "success": True, "is_valid": is_valid,
            "error": validation_error if not is_valid else None, "summary": summary}


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    _logger.info(
        "Phase 2 Worker: repo_id=%s, org=%s, repo=%s, lang=%s",
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
        _wait_for_docker(timeout=120)
    except RuntimeError as e:
        send_build_failed(str(e))
        sys.exit(1)

    config = _get_effective_config()
    workdir = Path("/tmp/jaeger_docker")
    workdir.mkdir(parents=True, exist_ok=True)

    # ── PHASE A: Build all images ────────────────────────────────────────

    try:
        _check_cancelled()
        send_heartbeat()
        base_tag = build_base_image(config)

        _check_cancelled()
        instances = download_manifest()
        total = len(instances)
        _logger.info("Building %d instance images", total)

        built_instances = []
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
            full_tag, success, build_log = build_instance_image(instance, base_tag, config, workdir)

            if success:
                send_build_progress(inst_id, "built", image_name=full_tag)
                instance["docker_image_tag"] = full_tag
                built_instances.append(instance)
            else:
                send_build_progress(inst_id, "failed", log_tail=build_log)
                failed_count += 1

            _logger.info("  [%d/%d] %s %s", idx, total, "BUILT" if success else "FAILED", full_tag)

        built_count = len(built_instances)
        if built_count == 0 and total > 0:
            send_build_failed("All %d image builds failed" % total)
            sys.exit(1)

        send_build_done(built_count, failed_count)
        _logger.info("Build phase complete: %d built, %d failed", built_count, failed_count)

    except RuntimeError as e:
        if "cancelled" in str(e).lower():
            send_build_failed("Pipeline cancelled (SIGTERM)")
        else:
            _logger.exception("Build phase failed")
            send_build_failed(str(e))
        sys.exit(1)
    except Exception as e:
        _logger.exception("Build phase failed unexpectedly")
        send_build_failed(str(e))
        sys.exit(1)

    # ── PHASE B: Run 3-run tests on built images ─────────────────────────

    try:
        _check_cancelled()
        _logger.info("Testing %d instances", len(built_instances))

        valid_count = 0
        invalid_count = 0
        error_count = 0

        completed = 0
        for idx, instance in enumerate(built_instances, 1):
            _check_cancelled()
            send_heartbeat()

            res = run_instance_tests(instance, config)
            completed = idx

            if res.get("is_valid"):
                valid_count += 1
            elif res.get("success"):
                invalid_count += 1
            else:
                error_count += 1

            _logger.info("  [%d/%d] instance #%s: %s", idx, len(built_instances), res.get("instance_id"), res.get("summary"))

        if _cancelled:
            send_test_failed("Cancelled by user after %d/%d instances" % (completed, len(built_instances)))
        else:
            send_test_done(valid_count, invalid_count, error_count)

        _logger.info("Test phase complete: %d valid, %d invalid, %d errors", valid_count, invalid_count, error_count)

    except RuntimeError as e:
        if "cancelled" in str(e).lower():
            send_test_failed("Pipeline cancelled (SIGTERM)")
        else:
            _logger.exception("Test phase failed")
            send_test_failed(str(e))
        sys.exit(1)
    except Exception as e:
        _logger.exception("Test phase failed unexpectedly")
        send_test_failed(str(e))
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
