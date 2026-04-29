#!/usr/bin/env python3
"""Lightweight K8s pod entrypoint for Jaeger SWE scrape pipeline.

NO Odoo imports. Reads all config from environment variables.
Runs the 5-step SWE pipeline, uploads JSONL outputs to S3,
and reports progress/status via webhook to the Odoo orchestrator.

Environment variables (all required unless noted):
    REPO_ID          - jaeger.repository record ID
    REPO_ORG         - GitHub org (e.g. "apache")
    REPO_NAME        - GitHub repo name (e.g. "kafka")
    GITHUB_TOKENS    - comma-separated GitHub PATs
    S3_BUCKET        - S3 bucket for output upload
    S3_REGION        - AWS region (default: ap-south-1)
    S3_PREFIX        - S3 key prefix (default: jaeger/phase1)
    WEBHOOK_URL      - Odoo webhook endpoint URL
    WEBHOOK_SECRET   - shared secret for X-Jaeger-Token header
    PIPELINE_MODE    - "swe" or "hard_swe" (default: swe)
"""
import json
import logging
import os
import signal
import sys
import shutil
import threading
import time
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("jaeger.worker")

# ── Configuration ────────────────────────────────────────────────────────

REPO_ID = os.environ.get("REPO_ID", "")
REPO_ORG = os.environ.get("REPO_ORG", "")
REPO_NAME = os.environ.get("REPO_NAME", "")
GITHUB_TOKENS = os.environ.get("GITHUB_TOKENS", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "ap-south-1")
S3_PREFIX = os.environ.get("S3_PREFIX", "jaeger/phase1")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PIPELINE_MODE = os.environ.get("PIPELINE_MODE", "swe")

# ── SIGTERM handling ─────────────────────────────────────────────────────

_cancelled = False


def _sigterm_handler(signum, frame):
    global _cancelled
    _cancelled = True
    _logger.warning("Received SIGTERM -- will stop after current step.")


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _sigterm_handler)


class PipelineCancelled(Exception):
    pass


def _check_cancelled():
    if _cancelled:
        raise PipelineCancelled("Pipeline cancelled (SIGTERM)")


# ── Webhook helpers ──────────────────────────────────────────────────────

def _post_webhook(payload):
    """POST JSON-RPC payload to WEBHOOK_URL with X-Jaeger-Token header."""
    if not WEBHOOK_URL:
        _logger.debug("No WEBHOOK_URL configured, skipping webhook")
        return
    import requests
    jsonrpc_body = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": payload,
    }
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json=jsonrpc_body,
            headers={
                "X-Jaeger-Token": WEBHOOK_SECRET,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            _logger.warning("Webhook returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        _logger.warning("Webhook POST failed: %s", e)


def send_progress(step, progress, message):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "progress",
        "step": step,
        "progress": round(progress, 1),
        "message": message,
    })


def send_heartbeat():
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "heartbeat",
    })


def send_done(s3_paths, counts):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "status",
        "status": "done",
        "s3_paths": s3_paths,
        "counts": counts,
    })


def send_failed(error):
    _post_webhook({
        "repo_id": int(REPO_ID),
        "type": "status",
        "status": "failed",
        "error": str(error)[:2000],
    })


# ── S3 upload ────────────────────────────────────────────────────────────

def _upload_to_s3(local_path, s3_key):
    """Upload a single file to S3 with retry."""
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

    client = boto3.client(
        "s3",
        region_name=S3_REGION,
        endpoint_url=endpoint or f"https://s3.{S3_REGION}.amazonaws.com",
        config=Config(**config_kwargs),
    )

    file_size = os.path.getsize(str(local_path))
    size_mb = file_size / (1024 * 1024)

    for attempt in range(3):
        try:
            t0 = time.monotonic()
            client.upload_file(str(local_path), S3_BUCKET, s3_key)
            elapsed = time.monotonic() - t0
            speed = (size_mb / elapsed) if elapsed > 0 else 0
            _logger.info(
                "S3 upload: %s (%.1f MB) -> s3://%s/%s in %.1fs (%.1f MB/s)",
                local_path.name, size_mb, S3_BUCKET, s3_key, elapsed, speed,
            )
            return f"s3://{S3_BUCKET}/{s3_key}"
        except Exception as exc:
            if attempt < 2:
                backoff = 4 * (2 ** attempt)
                _logger.warning(
                    "S3 upload attempt %d/3 failed for %s: %s. Retrying in %ds.",
                    attempt + 1, local_path.name, exc, backoff,
                )
                time.sleep(backoff)
            else:
                raise


def upload_outputs(out_dir, org, repo_name, repo_id):
    """Upload all JSONL outputs to S3. Returns dict of logical_name -> s3_uri."""
    if not S3_BUCKET:
        _logger.info("S3_BUCKET not set, skipping upload")
        return {}

    file_map = {
        "prs": f"{org}__{repo_name}_prs.jsonl",
        "filtered": f"{org}__{repo_name}_filtered_prs.jsonl",
        "issues": f"{org}__{repo_name}_related_issues.jsonl",
        "merged": f"{org}__{repo_name}_filtered_prs_with_issues.jsonl",
        "raw_dataset": f"{org}__{repo_name}_raw_dataset.jsonl",
    }

    s3_paths = {}
    for key, filename in file_map.items():
        local_path = out_dir / filename
        if not local_path.exists() or local_path.stat().st_size == 0:
            continue
        s3_key = f"{S3_PREFIX}/{repo_id}/{filename}"
        try:
            s3_paths[key] = _upload_to_s3(local_path, s3_key)
        except Exception as exc:
            _logger.error("S3 upload failed for %s: %s", filename, exc)

    return s3_paths


# ── JSONL helpers ────────────────────────────────────────────────────────

def _count_lines(path):
    if not path.exists():
        return 0
    count = 0
    with open(path) as f:
        for _ in f:
            count += 1
    return count


def _validate_step_output(filepath, step_num):
    if not filepath or not filepath.exists():
        raise RuntimeError("Step %d output file missing: %s" % (step_num, filepath))
    if filepath.stat().st_size == 0:
        raise RuntimeError("Step %d output file is empty: %s" % (step_num, filepath))
    with open(filepath) as f:
        first_line = f.readline().strip()
        if first_line:
            try:
                json.loads(first_line)
            except json.JSONDecodeError as e:
                raise RuntimeError("Step %d output has invalid JSONL: %s" % (step_num, e))


# ── Pipeline ─────────────────────────────────────────────────────────────

def run_swe_pipeline(tokens, out_dir, org, repo_name, repo_id, pipeline_mode):
    """Run the 5-step SWE pipeline. Reports progress via webhooks."""
    from tools.github_token_pool import GitHubTokenPool
    from tools.get_all_prs import main as get_all_prs
    from tools.filter_prs import main as filter_prs
    from tools.get_related_issues import main as get_related_issues
    from tools.merge_prs_with_issues import main as merge_prs_with_issues
    from tools.build_dataset import main as build_dataset

    pool = GitHubTokenPool(tokens)

    # ── Step 1: Fetch PRs ────────────────────────────────────────────
    _check_cancelled()
    send_progress("1/5", 0, "Fetching all pull requests...")
    send_heartbeat()

    get_all_prs(pool, out_dir, org, repo_name)
    prs_file = out_dir / f"{org}__{repo_name}_prs.jsonl"
    _validate_step_output(prs_file, 1)
    total_prs = _count_lines(prs_file)

    send_progress("1/5", 20, f"Fetched {total_prs} PRs")

    # ── Step 2: Filter PRs ───────────────────────────────────────────
    _check_cancelled()
    send_progress("2/5", 25, f"Filtering {total_prs} PRs...")
    send_heartbeat()

    def _filter_progress(processed, total, passed):
        _check_cancelled()
        pct = 25 + (processed / total) * 15 if total else 25
        send_progress("2/5", pct, f"Filtering {processed}/{total} PRs, {passed} passed")

    filter_prs(
        pool, out_dir, prs_file,
        skip_commit_message=False,
        progress_callback=_filter_progress,
    )
    filtered_file = out_dir / f"{org}__{repo_name}_filtered_prs.jsonl"
    filtered_count = _count_lines(filtered_file)

    send_progress("2/5", 40, f"{filtered_count}/{total_prs} PRs passed filter")

    if filtered_count == 0:
        raise ValueError(f"No PRs passed filtering for {org}/{repo_name}")

    # ── Step 3: Fetch Issues ─────────────────────────────────────────
    _check_cancelled()
    send_progress("3/5", 45, f"Fetching issues for {filtered_count} PRs...")
    send_heartbeat()

    get_related_issues(pool, out_dir, filtered_file)
    issues_file = out_dir / f"{org}__{repo_name}_related_issues.jsonl"
    if issues_file.exists():
        _validate_step_output(issues_file, 3)
    issues_count = _count_lines(issues_file)

    send_progress("3/5", 60, f"Fetched {issues_count} issues")

    # ── Step 4: Merge PRs with Issues ────────────────────────────────
    _check_cancelled()
    send_progress("4/5", 65, "Merging PRs with issues...")
    send_heartbeat()

    merge_prs_with_issues(out_dir, org, repo_name)
    merged_file = out_dir / f"{org}__{repo_name}_filtered_prs_with_issues.jsonl"
    _validate_step_output(merged_file, 4)

    send_progress("4/5", 80, "PRs merged with issues")

    # ── Step 5: Build Dataset ────────────────────────────────────────
    _check_cancelled()
    send_progress("5/5", 82, f"Building dataset from {filtered_count} PRs...")
    send_heartbeat()

    def _build_progress(processed, total, built):
        _check_cancelled()
        pct = 82 + (processed / total) * 13 if total else 82
        send_progress("5/5", pct, f"Building dataset: {processed}/{total} PRs processed, {built} built")
        send_heartbeat()

    build_dataset(pool, out_dir, merged_file, mode=pipeline_mode,
                  progress_callback=_build_progress)
    raw_dataset_file = out_dir / f"{org}__{repo_name}_raw_dataset.jsonl"
    _validate_step_output(raw_dataset_file, 5)
    raw_count = _count_lines(raw_dataset_file)

    send_progress("5/5", 95, f"Dataset built: {raw_count} entries")

    if raw_count == 0:
        raise ValueError(f"Raw dataset is empty for {org}/{repo_name}")

    # ── Upload to S3 ─────────────────────────────────────────────────
    _check_cancelled()
    send_progress("upload", 96, "Uploading outputs to S3...")

    s3_paths = upload_outputs(out_dir, org, repo_name, repo_id)
    _logger.info("S3 upload complete: %d files", len(s3_paths))

    return s3_paths, {
        "total_prs": total_prs,
        "filtered_prs": filtered_count,
        "issues": issues_count,
        "raw_dataset": raw_count,
    }


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    _logger.info(
        "Starting pipeline: repo_id=%s, org=%s, repo=%s, mode=%s",
        REPO_ID, REPO_ORG, REPO_NAME, PIPELINE_MODE,
    )

    missing = []
    for var in ("REPO_ID", "REPO_ORG", "REPO_NAME", "GITHUB_TOKENS", "S3_BUCKET", "WEBHOOK_URL"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        msg = "Missing required environment variables: %s" % ", ".join(missing)
        _logger.error(msg)
        send_failed(msg)
        sys.exit(1)

    tokens = [t.strip() for t in GITHUB_TOKENS.split(",") if t.strip()]
    if not tokens:
        msg = "GITHUB_TOKENS is set but contains no valid tokens"
        _logger.error(msg)
        send_failed(msg)
        sys.exit(1)

    repo_id = REPO_ID
    out_dir = Path("/tmp/jaeger_data") / f"{REPO_ORG}__{REPO_NAME}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        s3_paths, counts = run_swe_pipeline(
            tokens, out_dir, REPO_ORG, REPO_NAME, repo_id, PIPELINE_MODE,
        )
        send_done(s3_paths, counts)
        _logger.info("Pipeline complete: %s", counts)
    except PipelineCancelled:
        _logger.warning("Pipeline cancelled (SIGTERM) for repo %s", repo_id)
        send_failed("Pipeline cancelled (SIGTERM)")
        sys.exit(0)
    except Exception as exc:
        _logger.exception("Pipeline failed for repo %s: %s", repo_id, exc)
        send_failed(str(exc))
        sys.exit(1)

    try:
        shutil.rmtree(out_dir)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
