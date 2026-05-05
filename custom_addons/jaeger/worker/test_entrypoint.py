#!/usr/bin/env python3
"""K8s pod entrypoint for Jaeger Stage 4 — Test Execution (3-Run Pattern).

NO Odoo imports. Reads config from environment variables.
Pulls pre-built images, executes 3-run test pattern per instance,
parses results, reports via webhook.

Environment variables (required unless noted):
    REPO_ID            - jaeger.repository record ID
    REPO_ORG           - GitHub org
    REPO_NAME          - GitHub repo name
    REPO_LANGUAGE      - Repository language (default: python)
    MANIFEST_S3_KEY    - S3 key for instances manifest JSON
    AGENT_TIMEOUT      - Per-container timeout in seconds (default: 1800)
    MAX_WORKERS        - Concurrent test workers (default: 2)
    TEST_CONFIG_JSON   - Effective test config JSON (optional)
    CONTAINER_REGISTRY - Registry prefix for pulling images (optional)
    S3_BUCKET          - S3 bucket
    S3_REGION          - AWS region (default: ap-south-1)
    S3_PREFIX          - S3 key prefix
    WEBHOOK_URL        - Odoo webhook endpoint
    WEBHOOK_SECRET     - Shared secret for X-Jaeger-Token header
"""
import json
import logging
import os
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
_logger = logging.getLogger("jaeger.test_worker")

# ── Configuration ────────────────────────────────────────────────────────

REPO_ID = os.environ.get("REPO_ID", "")
REPO_ORG = os.environ.get("REPO_ORG", "")
REPO_NAME = os.environ.get("REPO_NAME", "")
REPO_LANGUAGE = os.environ.get("REPO_LANGUAGE", "python")
MANIFEST_S3_KEY = os.environ.get("MANIFEST_S3_KEY", "")
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "1800"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
TEST_CONFIG_JSON = os.environ.get("TEST_CONFIG_JSON", "")
CONTAINER_REGISTRY = os.environ.get("CONTAINER_REGISTRY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "ap-south-1")
S3_PREFIX = os.environ.get("S3_PREFIX", "jaeger/phase1")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ── SIGTERM handling ─────────────────────────────────────────────────────

_cancelled = False


def _sigterm_handler(signum, frame):
    global _cancelled
    _cancelled = True
    _logger.warning("Received SIGTERM — stopping after current tests.")


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
            headers={"X-Jaeger-Token": WEBHOOK_SECRET, "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            _logger.warning("Webhook returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        _logger.warning("Webhook POST failed: %s", e)


def send_heartbeat():
    _post_webhook({"repo_id": int(REPO_ID), "type": "heartbeat"})


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

def _wait_for_docker(timeout=60):
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


def _cleanup_container(container_name):
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def _execute_docker_run(inst_name, docker_image, mode, patches, timeout,
                        memory_limit="4g", language="python", network_enabled=None):
    """Execute a single Docker run for the 3-run test pattern.

    Mirrors _execute_docker_run_pure from jaeger_instance.py exactly.
    """
    tag = uuid4().hex[:8]
    container_name = (
        f"jaeger-{inst_name}-{mode}-{tag}"
        .replace("/", "-").replace("__", "-").lower()
    )

    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True, timeout=30,
    )

    cmd = [
        "docker", "run",
        "--name", container_name,
        "--memory", memory_limit,
        "--memory-swap", memory_limit,
    ]

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
                    (Path(tmpdir) / f"{patch_name}.diff").write_text(
                        patch_content, encoding="utf-8",
                    )

            cmd.extend(["-v", f"{tmpdir}:/patches:ro"])
            patches_path = "/patches"
            cmd.append(docker_image)

            if mode == "test_patch":
                cmd.extend(["bash", "-c",
                    f"cd /testbed && {reset_prefix}"
                    f"git apply --whitespace=nowarn {patches_path}/test_patch.diff && "
                    "bash /jaeger/fix-run.sh"])
            elif mode == "fix_patch":
                cmd.extend(["bash", "-c",
                    f"cd /testbed && {reset_prefix}"
                    f"git apply --whitespace=nowarn {patches_path}/fix_patch.diff && "
                    f"git apply --whitespace=nowarn {patches_path}/test_patch.diff && "
                    "bash /jaeger/fix-run.sh"])

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", container_name],
                               capture_output=True, timeout=30)
                raise
            finally:
                _cleanup_container(container_name)
    else:
        cmd.append(docker_image)
        cmd.extend(["bash", "-c", "cd /testbed && bash /jaeger/fix-run.sh"])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", container_name],
                           capture_output=True, timeout=30)
            raise
        finally:
            _cleanup_container(container_name)

    return result.stdout + "\n" + result.stderr


# ── Log parsing (simplified — mirrors jaeger_instance.py parsers) ────────

def _parse_test_log(log_text):
    """Parse test output log and extract pass/fail/skip counts.

    Simplified version — uses the >>>>> markers and basic regex patterns.
    Returns dict with passed_count, failed_count, skipped_count, passed_tests, failed_tests.
    """
    import re

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
            return {
                "passed_count": int(match.group(1)),
                "failed_count": int(m.group(1)) if (m := re.search(r"(\d+) failed", test_output)) else 0,
                "skipped_count": int(m.group(1)) if (m := re.search(r"(\d+) skipped", test_output)) else 0,
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
    """Classify tests into f2p/p2p/s2p/n2p categories from 3-run results.

    Returns (f2p_count, is_valid, validation_error).
    """
    run_passed = set(run_result.get("passed_tests", []))
    run_failed = set(run_result.get("failed_tests", []))
    fix_passed = set(fix_result.get("passed_tests", []))
    fix_failed = set(fix_result.get("failed_tests", []))

    f2p = run_failed & fix_passed
    p2p = run_passed & fix_passed

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


# ── Per-instance test execution ──────────────────────────────────────────

def run_instance_tests(instance):
    """Run 3-run test pattern for a single instance. Returns result dict."""
    inst_id = instance["id"]
    inst_name = instance["name"]
    docker_image = instance["docker_image_name"]
    fix_patch = instance.get("fix_patch", "")
    test_patch = instance.get("test_patch", "")

    config = {}
    if TEST_CONFIG_JSON:
        try:
            config = json.loads(TEST_CONFIG_JSON)
        except (json.JSONDecodeError, TypeError):
            pass

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
        run_log = _execute_docker_run(
            inst_name, docker_image, "run", None,
            AGENT_TIMEOUT, memory_limit, lang, network_enabled,
        )
        test_patch_log = _execute_docker_run(
            inst_name, docker_image, "test_patch",
            {"test_patch": test_patch}, AGENT_TIMEOUT, memory_limit, lang, network_enabled,
        )
        fix_patch_log = _execute_docker_run(
            inst_name, docker_image, "fix_patch",
            {"fix_patch": fix_patch, "test_patch": test_patch},
            AGENT_TIMEOUT, memory_limit, lang, network_enabled,
        )
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

    summary = (
        f"valid ({f2p_count} f2p)" if is_valid
        else (validation_error or "invalid")
    )

    send_test_progress(
        inst_id, is_valid, summary,
        run_log=run_log, test_patch_log=test_patch_log, fix_patch_log=fix_patch_log,
        run_result=run_result, test_result=test_result, fix_result=fix_result,
    )

    return {
        "instance_id": inst_id,
        "success": True,
        "is_valid": is_valid,
        "error": validation_error if not is_valid else None,
        "summary": summary,
    }


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    _logger.info(
        "Stage 4 Test Worker: repo_id=%s, org=%s, repo=%s, workers=%d, timeout=%d",
        REPO_ID, REPO_ORG, REPO_NAME, MAX_WORKERS, AGENT_TIMEOUT,
    )

    missing = []
    for var in ("REPO_ID", "REPO_ORG", "REPO_NAME", "S3_BUCKET", "WEBHOOK_URL", "MANIFEST_S3_KEY"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        msg = "Missing required env vars: %s" % ", ".join(missing)
        _logger.error(msg)
        send_test_failed(msg)
        sys.exit(1)

    try:
        _wait_for_docker(timeout=90)
    except RuntimeError as e:
        send_test_failed(str(e))
        sys.exit(1)

    try:
        instances = download_manifest()
        total = len(instances)
        _logger.info("Testing %d instances with %d workers", total, MAX_WORKERS)

        if not total:
            send_test_done(0, 0, 0)
            sys.exit(0)

        valid_count = 0
        invalid_count = 0
        error_count = 0
        completed = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(run_instance_tests, inst): inst
                for inst in instances
            }
            for future in as_completed(futures):
                inst = futures[future]
                try:
                    res = future.result()
                except Exception as e:
                    _logger.error("Instance %s raised: %s", inst.get("id"), e)
                    res = {"instance_id": inst.get("id"), "success": False,
                           "is_valid": False, "error": str(e), "summary": f"exception: {e}"}

                completed += 1
                if res.get("is_valid"):
                    valid_count += 1
                elif res.get("success"):
                    invalid_count += 1
                if res.get("error"):
                    error_count += 1

                _logger.info("  [%d/%d] instance #%s: %s",
                             completed, total, res.get("instance_id"), res.get("summary"))
                send_heartbeat()

                if _cancelled:
                    _logger.warning("Cancellation requested — stopping")
                    for f in futures:
                        f.cancel()
                    break

        if _cancelled:
            send_test_failed("Cancelled by user after %d/%d instances" % (completed, total))
        else:
            send_test_done(valid_count, invalid_count, error_count)

        _logger.info("Test execution complete: %d valid, %d invalid, %d errors",
                     valid_count, invalid_count, error_count)

    except RuntimeError as e:
        if "cancelled" in str(e).lower():
            send_test_failed("Pipeline cancelled (SIGTERM)")
        else:
            _logger.exception("Test execution failed")
            send_test_failed(str(e))
        sys.exit(1)
    except Exception as e:
        _logger.exception("Test execution failed unexpectedly")
        send_test_failed(str(e))
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
