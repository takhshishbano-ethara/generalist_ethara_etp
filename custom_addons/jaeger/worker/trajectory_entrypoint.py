"""K8s pod entrypoint for Stage 6 trajectory generation.

Runs the full run_custom_eval.sh pipeline for one repository,
reporting progress via webhooks to the Jaeger Odoo server.

Environment variables (from K8s Job spec):
    REPO_ID, DATASET_S3_KEY, LLM_CONFIG_JSON, ECR_PREFIX, LANGUAGE,
    K_RUNS, NUM_WORKERS, MAX_ITERATIONS, MAX_RETRIES,
    CONVERSATION_TIMEOUT, TEMPERATURE, WEBHOOK_URL,
    S3_BUCKET, S3_REGION, S3_PREFIX, JOB_ID
"""
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

_logger = logging.getLogger(__name__)

REPO_ID = os.environ["REPO_ID"]
DATASET_S3_KEY = os.environ["DATASET_S3_KEY"]
LLM_CONFIG_JSON = os.environ["LLM_CONFIG_JSON"]
ECR_PREFIX = os.environ.get("ECR_PREFIX", "")
LANGUAGE = os.environ.get("LANGUAGE", "python")
K_RUNS = int(os.environ.get("K_RUNS", "8"))
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "1"))
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "300"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
CONVERSATION_TIMEOUT = int(os.environ.get("CONVERSATION_TIMEOUT", "3600"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "ap-south-1")
S3_PREFIX = os.environ.get("S3_PREFIX", "jaeger/phase3")
JOB_ID = os.environ["JOB_ID"]


def send_webhook(payload):
    headers = {"Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "method": "call", "params": payload}
    try:
        resp = requests.post(WEBHOOK_URL, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        _logger.warning("Webhook failed: %s", e)


def send_heartbeat():
    send_webhook({"type": "heartbeat", "repo_id": int(REPO_ID)})


def send_trajectory_progress(status, data):
    send_webhook({
        "type": f"trajectory_{status}" if status == "progress" else f"trajectory_{status}",
        "repo_id": int(REPO_ID),
        "job_id": JOB_ID,
        **data,
    })


def main():
    work_dir = Path("/app/work")
    work_dir.mkdir(parents=True, exist_ok=True)

    send_webhook({
        "type": "trajectory_progress",
        "repo_id": int(REPO_ID),
        "job_id": JOB_ID,
        "step": "Downloading dataset from S3...",
    })

    dataset_path = work_dir / "dataset.jsonl"
    _download_from_s3(DATASET_S3_KEY, dataset_path)

    llm_config_path = work_dir / "llm_config.json"
    llm_config = json.loads(LLM_CONFIG_JSON)
    llm_config["temperature"] = TEMPERATURE
    with open(llm_config_path, "w") as f:
        json.dump(llm_config, f)

    send_webhook({
        "type": "trajectory_progress",
        "repo_id": int(REPO_ID),
        "job_id": JOB_ID,
        "step": "Starting trajectory generation...",
    })
    send_heartbeat()

    benchmarks_dir = Path("/app/benchmarks")
    cmd = [
        "bash", str(benchmarks_dir / "run_custom_eval.sh"),
        "--llm-config", str(llm_config_path),
        "--dataset", str(dataset_path),
        "--ecr-prefix", ECR_PREFIX,
        "--lang", LANGUAGE,
        "-k", str(K_RUNS),
        "--num-workers", str(NUM_WORKERS),
        "--max-iter", str(MAX_ITERATIONS),
        "--max-retries", str(MAX_RETRIES),
    ]

    env = os.environ.copy()
    env["CONVERSATION_TIMEOUT"] = str(CONVERSATION_TIMEOUT)

    process = subprocess.Popen(
        cmd,
        cwd=str(benchmarks_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    last_heartbeat = time.time()
    output_lines = []
    for line in iter(process.stdout.readline, ""):
        output_lines.append(line)
        if time.time() - last_heartbeat > 120:
            send_heartbeat()
            last_heartbeat = time.time()
        if "run_" in line and ("PASS" in line or "FAIL" in line):
            send_webhook({
                "type": "trajectory_progress",
                "repo_id": int(REPO_ID),
                "job_id": JOB_ID,
                "step": line.strip()[:200],
            })

    process.wait()

    if process.returncode != 0:
        error_tail = "".join(output_lines[-50:])
        send_webhook({
            "type": "trajectory_failed",
            "repo_id": int(REPO_ID),
            "job_id": JOB_ID,
            "error": f"run_custom_eval.sh exited with code {process.returncode}",
            "log_tail": error_tail[:2000],
        })
        sys.exit(1)

    send_webhook({
        "type": "trajectory_progress",
        "repo_id": int(REPO_ID),
        "job_id": JOB_ID,
        "step": "Collecting results...",
    })

    eval_outputs = benchmarks_dir / "eval_outputs"
    summary_files = list(eval_outputs.rglob("pass_at_*_summary.json"))

    if not summary_files:
        send_webhook({
            "type": "trajectory_failed",
            "repo_id": int(REPO_ID),
            "job_id": JOB_ID,
            "error": "No summary file found after run_custom_eval.sh",
        })
        sys.exit(1)

    with open(summary_files[0]) as f:
        summary = json.load(f)

    per_run_results = []
    for run_dir in sorted(eval_outputs.rglob("run_*")):
        if not run_dir.is_dir():
            continue
        report_file = run_dir / "output.report.json"
        output_file = run_dir / "output.jsonl"
        if report_file.exists():
            with open(report_file) as f:
                report = json.load(f)
            run_data = {
                "run_number": int(run_dir.name.replace("run_", "")),
                "resolved": report.get("resolved", False),
                "report": report,
            }
            if output_file.exists():
                with open(output_file) as f:
                    for line in f:
                        output = json.loads(line)
                        run_data["api_calls"] = output.get("metrics", {}).get("api_calls", 0)
                        run_data["api_cost"] = output.get("metrics", {}).get("cost", 0.0)
                        run_data["api_time"] = output.get("metrics", {}).get("elapsed_time", 0.0)
                        run_data["prompt_tokens"] = output.get("metrics", {}).get("prompt_tokens", 0)
                        run_data["completion_tokens"] = output.get("metrics", {}).get("completion_tokens", 0)
                        run_data["agent_patch"] = output.get("git_patch", "")
                        break
            per_run_results.append(run_data)

    send_webhook({
        "type": "trajectory_progress",
        "repo_id": int(REPO_ID),
        "job_id": JOB_ID,
        "step": "Uploading results to S3...",
    })
    s3_output_key = f"{S3_PREFIX}/{REPO_ID}/trajectory_results/"
    _upload_directory_to_s3(eval_outputs, s3_output_key)

    send_webhook({
        "type": "trajectory_done",
        "repo_id": int(REPO_ID),
        "job_id": JOB_ID,
        "summary": summary,
        "per_run_results": per_run_results,
        "s3_output_key": s3_output_key,
    })

    _logger.info("Trajectory generation complete. pass@%d = %s",
                 K_RUNS, summary.get("pass_at_k", "N/A"))


def _download_from_s3(key, local_path):
    import boto3
    s3 = boto3.client("s3", region_name=S3_REGION)
    s3.download_file(S3_BUCKET, key, str(local_path))


def _upload_directory_to_s3(local_dir, s3_prefix):
    import boto3
    s3 = boto3.client("s3", region_name=S3_REGION)
    for path in local_dir.rglob("*"):
        if path.is_file():
            rel = path.relative_to(local_dir)
            key = f"{s3_prefix}{rel}"
            s3.upload_file(str(path), S3_BUCKET, key)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
