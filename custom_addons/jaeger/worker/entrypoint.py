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
    PIPELINE_MODE    - "swe", "rct", or "lht" (default: swe)
    REPO_LANGUAGE    - repository language, e.g. "python" (default: python)
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
PIPELINE_MODE = os.environ.get("PIPELINE_MODE", "swe")
REPO_LANGUAGE = os.environ.get("REPO_LANGUAGE", "python")

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
    """POST JSON-RPC payload to WEBHOOK_URL."""
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
    """Upload a single file to S3 with retry and multipart tuning."""
    import boto3
    from boto3.s3.transfer import TransferConfig
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

    transfer_config = TransferConfig(
        multipart_threshold=50 * 1024 * 1024,
        multipart_chunksize=25 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )

    file_size = os.path.getsize(str(local_path))
    size_mb = file_size / (1024 * 1024)

    for attempt in range(3):
        try:
            t0 = time.monotonic()
            client.upload_file(str(local_path), S3_BUCKET, s3_key, Config=transfer_config)
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
        "rct_candidates": f"{org}__{repo_name}_rct_dataset_candidates.jsonl",
        "raw_dataset": f"{org}__{repo_name}_raw_dataset.jsonl",
        "lht_filtered": f"{org}__{repo_name}_lht_filtered_prs.jsonl",
        "tags": f"{org}__{repo_name}_tags.jsonl",
        "tag_groups": f"{org}__{repo_name}_tag_groups.jsonl",
    }

    s3_paths = {}
    for key, filename in file_map.items():
        local_path = out_dir / filename
        if not local_path.exists() or local_path.stat().st_size == 0:
            continue
        s3_key = f"{S3_PREFIX}/{PIPELINE_MODE}/{repo_id}/{filename}"
        try:
            s3_paths[key] = _upload_to_s3(local_path, s3_key)
        except Exception as exc:
            if key == "raw_dataset":
                raise RuntimeError(
                    "Failed to upload raw_dataset to S3: %s" % exc
                ) from exc
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


def run_rct_pipeline(tokens, out_dir, org, repo_name, repo_id, pipeline_mode):
    """Run the 6-step RCT pipeline. Reports progress via webhooks."""
    from tools.github_token_pool import GitHubTokenPool
    from tools.get_all_prs import main as get_all_prs
    from tools.filter_prs import main as filter_prs
    from tools.get_related_issues import main as get_related_issues
    from tools.merge_prs_with_issues import main as merge_prs_with_issues
    from tools.filter_rct import main as filter_rct
    from tools.build_dataset import main as build_dataset

    pool = GitHubTokenPool(tokens)

    # ── Step 1: Fetch PRs ────────────────────────────────────────────
    _check_cancelled()
    send_progress("1/6", 0, "Fetching all pull requests...")
    send_heartbeat()

    get_all_prs(pool, out_dir, org, repo_name)
    prs_file = out_dir / f"{org}__{repo_name}_prs.jsonl"
    _validate_step_output(prs_file, 1)
    total_prs = _count_lines(prs_file)

    send_progress("1/6", 15, f"Fetched {total_prs} PRs")

    # ── Step 2: Filter PRs (RCT: merged + has resolved issues) ──────
    _check_cancelled()
    send_progress("2/6", 18, f"Filtering {total_prs} PRs (RCT: merged + issue-linked)...")
    send_heartbeat()

    def _filter_progress(processed, total, passed):
        _check_cancelled()
        pct = 18 + (processed / total) * 12 if total else 18
        send_progress("2/6", pct, f"Filtering {processed}/{total} PRs, {passed} passed")

    filter_prs(
        pool, out_dir, prs_file,
        mode="rct",
        skip_commit_message=False,
        progress_callback=_filter_progress,
    )
    filtered_file = out_dir / f"{org}__{repo_name}_rct_filtered_prs.jsonl"
    if not filtered_file.exists():
        filtered_file = out_dir / f"{org}__{repo_name}_filtered_prs.jsonl"
    filtered_count = _count_lines(filtered_file)

    send_progress("2/6", 30, f"{filtered_count}/{total_prs} PRs passed initial filter")

    if filtered_count == 0:
        raise ValueError(f"No merged PRs with resolved issues for {org}/{repo_name}")

    # ── Step 3: Fetch Related Issues (WITH labels) ──────────────────
    _check_cancelled()
    send_progress("3/6", 33, f"Fetching issues + labels for {filtered_count} PRs...")
    send_heartbeat()

    get_related_issues(pool, out_dir, filtered_file)
    issues_file = out_dir / f"{org}__{repo_name}_related_issues.jsonl"
    if issues_file.exists():
        _validate_step_output(issues_file, 3)
    issues_count = _count_lines(issues_file)

    send_progress("3/6", 50, f"Fetched {issues_count} issues with labels")

    # ── Step 4: Merge PRs with Issues ───────────────────────────────
    _check_cancelled()
    send_progress("4/6", 53, "Merging PRs with issues...")
    send_heartbeat()

    # merge_prs_with_issues reads {org}__{repo}_filtered_prs.jsonl by convention.
    # RCT filter output has the _rct_ prefix, so copy it to the standard name.
    standard_filtered = out_dir / f"{org}__{repo_name}_filtered_prs.jsonl"
    if not standard_filtered.exists() and filtered_file != standard_filtered:
        import shutil
        shutil.copy2(filtered_file, standard_filtered)

    merge_prs_with_issues(out_dir, org, repo_name)
    merged_file = out_dir / f"{org}__{repo_name}_filtered_prs_with_issues.jsonl"
    _validate_step_output(merged_file, 4)

    send_progress("4/6", 60, "PRs merged with issues (labels attached)")

    # ── Step 5: RCT Bounty Filter ───────────────────────────────────
    _check_cancelled()
    send_progress("5/6", 63, "Filtering for bounty-related PRs...")
    send_heartbeat()

    rct_candidates = filter_rct(out_dir, org, repo_name)
    rct_count = _count_lines(rct_candidates)

    send_progress("5/6", 75, f"{rct_count} bounty PRs identified")

    if rct_count == 0:
        raise ValueError(
            f"No bounty-related PRs found for {org}/{repo_name}. "
            f"Checked {filtered_count} merged PRs, none had bounty labels/signals."
        )

    # ── Step 6: Build Dataset (diff fetching) ───────────────────────
    _check_cancelled()
    send_progress("6/6", 78, f"Building dataset from {rct_count} bounty PRs...")
    send_heartbeat()

    def _build_progress(processed, total, built):
        _check_cancelled()
        pct = 78 + (processed / total) * 17 if total else 78
        send_progress("6/6", pct, f"Building dataset: {processed}/{total} PRs, {built} built")
        send_heartbeat()

    build_dataset(pool, out_dir, rct_candidates, mode="swe",
                  progress_callback=_build_progress)
    raw_dataset_file = out_dir / f"{org}__{repo_name}_raw_dataset.jsonl"
    _validate_step_output(raw_dataset_file, 6)
    raw_count = _count_lines(raw_dataset_file)

    send_progress("6/6", 95, f"Dataset built: {raw_count} RCT entries")

    if raw_count == 0:
        raise ValueError(f"Raw dataset is empty for {org}/{repo_name} (RCT mode)")

    # ── Upload to S3 ────────────────────────────────────────────────
    _check_cancelled()
    send_progress("upload", 96, "Uploading outputs to S3...")

    s3_paths = upload_outputs(out_dir, org, repo_name, repo_id)
    _logger.info("S3 upload complete: %d files", len(s3_paths))

    return s3_paths, {
        "total_prs": total_prs,
        "filtered_prs": filtered_count,
        "issues": issues_count,
        "rct_candidates": rct_count,
        "raw_dataset": raw_count,
    }


def run_lht_pipeline(tokens, out_dir, org, repo_name, repo_id, pipeline_mode):
    """Run the 6-step LHT pipeline. Reports progress via webhooks."""
    from tools.github_token_pool import GitHubTokenPool
    from tools.get_all_prs import main as get_all_prs
    from tools.filter_prs import main as filter_prs
    from tools.get_version_tags import main as get_version_tags
    from tools.group_prs_by_tags import main as group_prs_by_tags
    from tools.get_related_issues import main as get_related_issues
    from tools.build_lht_dataset import main as build_lht_dataset

    pool = GitHubTokenPool(tokens)

    # ── Step 1: Fetch PRs ────────────────────────────────────────────
    _check_cancelled()
    send_progress("1/6", 0, "Fetching all pull requests...")
    send_heartbeat()

    get_all_prs(pool, out_dir, org, repo_name)
    prs_file = out_dir / f"{org}__{repo_name}_prs.jsonl"
    _validate_step_output(prs_file, 1)
    total_prs = _count_lines(prs_file)

    send_progress("1/6", 16, f"Fetched {total_prs} PRs")

    # ── Step 2: Filter PRs (LHT: merged only, no issue requirement) ─
    _check_cancelled()
    send_progress("2/6", 20, f"Filtering {total_prs} PRs (LHT: merged only)...")
    send_heartbeat()

    def _filter_progress(processed, total, passed):
        _check_cancelled()
        pct = 20 + (processed / total) * 13 if total else 20
        send_progress("2/6", pct, f"Filtering {processed}/{total} PRs, {passed} passed")

    filter_prs(
        pool, out_dir, prs_file,
        mode="lht",
        skip_commit_message=True,
        progress_callback=_filter_progress,
    )
    lht_filtered = out_dir / f"{org}__{repo_name}_lht_filtered_prs.jsonl"
    filtered_file = lht_filtered if lht_filtered.exists() else out_dir / f"{org}__{repo_name}_filtered_prs.jsonl"
    filtered_count = _count_lines(filtered_file)

    send_progress("2/6", 33, f"{filtered_count}/{total_prs} merged PRs passed filter")

    if filtered_count == 0:
        raise ValueError(f"No merged PRs found for {org}/{repo_name}")

    # ── Step 3: Fetch Version Tags ──────────────────────────────────
    _check_cancelled()
    send_progress("3/6", 38, "Fetching version tags...")
    send_heartbeat()

    get_version_tags(tokens, out_dir, org, repo_name, max_tags=200)
    tags_file = out_dir / f"{org}__{repo_name}_tags.jsonl"
    tags_count = _count_lines(tags_file) if tags_file.exists() else 0

    send_progress("3/6", 50, f"Fetched {tags_count} version tags")

    # ── Step 4: Group PRs by Version Tags ───────────────────────────
    _check_cancelled()
    send_progress("4/6", 55, "Grouping PRs by version ranges...")
    send_heartbeat()

    # group_prs_by_tags needs git clone for ancestry checks.
    # In K8s pod, use ephemeral storage under out_dir as cache dir.
    cache_dir = str(out_dir / ".repo_cache")
    group_prs_by_tags(out_dir, org, repo_name, window_days=30,
                      cache_dir=cache_dir, tokens=tokens)
    groups_file = out_dir / f"{org}__{repo_name}_tag_groups.jsonl"
    groups_count = _count_lines(groups_file) if groups_file.exists() else 0

    send_progress("4/6", 66, f"Created {groups_count} version-range groups")

    if groups_count == 0:
        raise ValueError(
            f"No valid tag groups for {org}/{repo_name}. "
            f"Repo may not have enough version tags or merged PRs per version."
        )

    # ── Step 5: Fetch Related Issues ────────────────────────────────
    _check_cancelled()
    send_progress("5/6", 70, "Fetching related issues...")
    send_heartbeat()

    get_related_issues(pool, out_dir, filtered_file)
    issues_file = out_dir / f"{org}__{repo_name}_related_issues.jsonl"
    issues_count = _count_lines(issues_file) if issues_file.exists() else 0

    send_progress("5/6", 83, f"Fetched {issues_count} issues")

    # ── Step 6: Build LHT Dataset ───────────────────────────────────
    _check_cancelled()
    send_progress("6/6", 85, f"Building LHT dataset from {groups_count} groups...")
    send_heartbeat()

    build_lht_dataset(
        tokens, out_dir, org, repo_name,
        lang=REPO_LANGUAGE,
        cache_dir=cache_dir,
    )
    raw_dataset_file = out_dir / f"{org}__{repo_name}_raw_dataset.jsonl"
    _validate_step_output(raw_dataset_file, 6)
    raw_count = _count_lines(raw_dataset_file)

    send_progress("6/6", 95, f"Dataset built: {raw_count} LHT entries")

    if raw_count == 0:
        raise ValueError(f"Raw dataset is empty for {org}/{repo_name} (LHT mode)")

    # ── Upload to S3 ────────────────────────────────────────────────
    _check_cancelled()
    send_progress("upload", 96, "Uploading outputs to S3...")

    s3_paths = upload_outputs(out_dir, org, repo_name, repo_id)
    _logger.info("S3 upload complete: %d files", len(s3_paths))

    return s3_paths, {
        "total_prs": total_prs,
        "filtered_prs": filtered_count,
        "tags": tags_count,
        "groups": groups_count,
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
        if PIPELINE_MODE == "rct":
            s3_paths, counts = run_rct_pipeline(
                tokens, out_dir, REPO_ORG, REPO_NAME, repo_id, PIPELINE_MODE,
            )
        elif PIPELINE_MODE == "lht":
            s3_paths, counts = run_lht_pipeline(
                tokens, out_dir, REPO_ORG, REPO_NAME, repo_id, PIPELINE_MODE,
            )
        else:
            if PIPELINE_MODE != "swe":
                _logger.warning(
                    "Unknown PIPELINE_MODE=%r, falling back to SWE pipeline",
                    PIPELINE_MODE,
                )
            s3_paths, counts = run_swe_pipeline(
                tokens, out_dir, REPO_ORG, REPO_NAME, repo_id, PIPELINE_MODE,
            )
        send_done(s3_paths, counts)
        _logger.info("Pipeline complete: %s", counts)
    except PipelineCancelled:
        _logger.warning("Pipeline cancelled (SIGTERM) for repo %s", repo_id)
        send_failed("Pipeline cancelled (SIGTERM)")
        sys.exit(1)
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
