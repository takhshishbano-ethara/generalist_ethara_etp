"""Standalone pipeline helpers for Jaeger background threads and K8s workers.

These functions run outside the ORM (no self, no env) — they open their own
cursors and are safe to call from background threads or K8s pod entrypoints.
"""
import logging
import os
import shutil
import threading
import time as _time
from datetime import datetime

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _write_with_retry(db_name, repo_id, vals):
    import time
    from odoo.orm.registry import Registry
    for attempt in range(3):
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                repo = env["jaeger.repository"].browse(repo_id)
                if not repo.exists():
                    _logger.error("Repo %s does not exist", repo_id)
                    return
                repo.write(vals)
            return
        except Exception as e:
            if "serialize" in str(e).lower() and attempt < 2:
                _logger.warning("Serialization conflict (attempt %d/3)", attempt + 1)
                time.sleep(1 + attempt)
                continue
            raise


def _append_log_standalone(db_name, repo_id, msg):
    from odoo.orm.registry import Registry
    line = "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg)
    for attempt in range(3):
        try:
            with Registry(db_name).cursor() as cr:
                cr.execute(
                    "UPDATE jaeger_repository SET log_output = "
                    "CASE WHEN LENGTH(COALESCE(log_output, '')) > 200000 "
                    "THEN RIGHT(log_output, 150000) || %s "
                    "ELSE COALESCE(log_output, '') || %s END "
                    "WHERE id = %s",
                    [line, line, repo_id],
                )
            return
        except Exception as e:
            if "serialize" in str(e).lower() and attempt < 2:
                _time.sleep(1 + attempt)
                continue
            _logger.warning("Failed to append log: %s", e)
            return


def _count_lines(path):
    if not path.exists():
        return 0
    count = 0
    with open(path) as f:
        for _ in f:
            count += 1
    return count


class PipelineCancelled(Exception):
    pass


def _check_cancelled(db_name, repo_id):
    from odoo.orm.registry import Registry
    with Registry(db_name).cursor() as cr:
        cr.execute(
            "SELECT cancel_requested FROM jaeger_repository WHERE id = %s",
            [repo_id],
        )
        row = cr.fetchone()
        if row and row[0]:
            raise PipelineCancelled("Pipeline %s cancelled by user" % repo_id)


def _heartbeat_standalone(db_name, repo_id, text=None):
    vals = {"last_heartbeat": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if text:
        vals["pr_collection_step"] = text
    _write_with_retry(db_name, repo_id, vals)


def _validate_step_output(filepath, step_num):
    import json as _json
    if not filepath or not filepath.exists():
        raise RuntimeError("Step %d output file missing: %s" % (step_num, filepath))
    if filepath.stat().st_size == 0:
        raise RuntimeError("Step %d output file is empty: %s" % (step_num, filepath))
    with open(filepath) as f:
        first_line = f.readline().strip()
        if first_line:
            try:
                _json.loads(first_line)
            except _json.JSONDecodeError as e:
                raise RuntimeError(
                    "Step %d output has invalid JSONL: %s" % (step_num, e),
                )


class DbLogHandler(logging.Handler):

    def __init__(self, db_name, repo_id, flush_interval=3.0, max_buffer=30):
        super().__init__()
        self.db_name = db_name
        self.repo_id = repo_id
        self.flush_interval = flush_interval
        self.max_buffer = max_buffer
        self._buffer = []
        self._lock = threading.Lock()
        self._last_flush = _time.monotonic()

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            self._buffer.append(msg)
            now = _time.monotonic()
            if len(self._buffer) >= self.max_buffer or (now - self._last_flush) >= self.flush_interval:
                self._drain()

    def _drain(self):
        if not self._buffer:
            return
        chunk = "\n".join(self._buffer) + "\n"
        self._buffer.clear()
        self._last_flush = _time.monotonic()
        try:
            from odoo.orm.registry import Registry
            with Registry(self.db_name).cursor() as cr:
                cr.execute(
                    "UPDATE jaeger_repository SET log_output = "
                    "CASE WHEN LENGTH(COALESCE(log_output, '')) > 200000 "
                    "THEN RIGHT(log_output, 150000) || %s "
                    "ELSE COALESCE(log_output, '') || %s END "
                    "WHERE id = %s",
                    [chunk, chunk, self.repo_id],
                )
        except Exception:
            pass

    def flush(self):
        with self._lock:
            self._drain()

    def close(self):
        self.flush()
        super().close()


def _run_scrape_pipeline_standalone(db_name, repo_id):
    from pathlib import Path
    from odoo.orm.registry import Registry

    _logger.info("Starting local pipeline for repo_id=%s, db=%s", repo_id, db_name)

    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        repo = env["jaeger.repository"].browse(repo_id)
        if not repo.exists():
            _logger.error("Repo %s not found", repo_id)
            return
        org = repo.org
        repo_name = repo.repo_name
        pipeline_mode = repo.pipeline_mode
        tokens_str = env["ir.config_parameter"].sudo().get_param("jaeger.github_tokens", "")
        retry_attempts = int(env["ir.config_parameter"].sudo().get_param("jaeger.retry_attempts", "3"))
        delay_on_error = int(env["ir.config_parameter"].sudo().get_param("jaeger.delay_on_error", "300"))
        output_dir = env["ir.config_parameter"].sudo().get_param("jaeger.output_dir", "/tmp/jaeger_data")

    tokens = [t.strip() for t in tokens_str.split(",") if t.strip()]
    if not tokens:
        _write_with_retry(db_name, repo_id, {
            "pr_collection_status": "failed",
            "error_message": "No GitHub tokens configured.",
        })
        return

    out_dir = Path(output_dir) / f"{org}__{repo_name}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    handler = DbLogHandler(db_name, repo_id, flush_interval=1.0, max_buffer=10)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
    ))
    handler.setLevel(logging.INFO)
    _tool_logger_names = [
        "odoo.addons.jaeger.tools.get_all_prs",
        "odoo.addons.jaeger.tools.filter_prs",
        "odoo.addons.jaeger.tools.get_related_issues",
        "odoo.addons.jaeger.tools.merge_prs_with_issues",
        "odoo.addons.jaeger.tools.build_dataset",
        "odoo.addons.jaeger.tools.get_version_tags",
        "odoo.addons.jaeger.tools.group_prs_by_tags",
        "odoo.addons.jaeger.tools.build_lht_dataset",
        "jaeger.tools.get_all_prs",
        "jaeger.tools.filter_prs",
        "jaeger.tools.get_related_issues",
        "jaeger.tools.merge_prs_with_issues",
        "jaeger.tools.build_dataset",
        "jaeger.tools.get_version_tags",
        "jaeger.tools.group_prs_by_tags",
        "jaeger.tools.build_lht_dataset",
    ]
    tool_loggers = [logging.getLogger(name) for name in _tool_logger_names]
    for tl in tool_loggers:
        tl.addHandler(handler)

    try:
        if pipeline_mode == "lht":
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                repo = env["jaeger.repository"].browse(repo_id)
                repo.run_scrape_pipeline()
        else:
            _run_swe_steps_standalone(
                db_name, repo_id, org, repo_name, tokens, out_dir,
                retry_attempts, delay_on_error, pipeline_mode,
            )
    except PipelineCancelled:
        _logger.info("Pipeline %s cancelled by user", repo_id)
        _write_with_retry(
            db_name, repo_id,
            {
                "pr_collection_status": "failed",
                "error_message": "Cancelled by user.",
                "pr_collection_step": "",
            },
        )
        return
    except Exception as e:
        _logger.exception("Pipeline failed for repo_id=%s: %s", repo_id, e)
        _write_with_retry(
            db_name, repo_id,
            {"pr_collection_status": "failed", "error_message": str(e)[:2000]},
        )
        return
    finally:
        handler.close()
        for tl in tool_loggers:
            tl.removeHandler(handler)

    _logger.info("Pipeline complete for repo_id=%s", repo_id)


def _upload_outputs_to_s3(db_name, repo_id, out_dir, org, repo_name):
    from odoo.orm.registry import Registry
    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        ICP = env["ir.config_parameter"].sudo()
        s3_bucket = ICP.get_param("jaeger.s3_bucket", "")
        s3_region = ICP.get_param("jaeger.s3_region", "ap-south-1")
        s3_prefix = ICP.get_param("jaeger.s3_prefix", "jaeger/phase1")

    if not s3_bucket:
        _logger.info("S3 bucket not configured — skipping upload for repo %s", repo_id)
        return []

    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        _logger.warning("boto3 not installed — skipping S3 upload for repo %s", repo_id)
        return []

    client = boto3.client(
        "s3",
        region_name=s3_region,
        endpoint_url=os.environ.get("JAEGER_S3_ENDPOINT", f"https://s3.{s3_region}.amazonaws.com"),
        config=BotoConfig(
            retries={"mode": "standard", "max_attempts": 5},
            connect_timeout=30,
            read_timeout=60,
            **({"s3": {"addressing_style": "path"}} if os.environ.get("JAEGER_S3_ENDPOINT") else {}),
        ),
    )

    from pathlib import Path
    out_dir_p = Path(out_dir)
    all_jsonl = sorted(out_dir_p.glob(f"{org}__{repo_name}_*.jsonl"))
    if not all_jsonl:
        _logger.info("S3 upload: no JSONL files found in %s", out_dir)
        return []

    _logger.info("S3 upload: found %d JSONL files to upload: %s",
                 len(all_jsonl), [f.name for f in all_jsonl])

    uploaded = []
    for local_path in all_jsonl:
        filename = local_path.name
        if not local_path.exists():
            _logger.info("S3 upload: %s does not exist, skipping", filename)
            continue
        file_size = local_path.stat().st_size
        if file_size == 0:
            _logger.info("S3 upload: %s is empty, skipping", filename)
            continue
        s3_key = f"{s3_prefix}/{repo_id}/{filename}"
        size_mb = file_size / (1024 * 1024)
        _logger.info("S3 upload: %s (%.2f MB) -> s3://%s/%s", filename, size_mb, s3_bucket, s3_key)
        for attempt in range(3):
            try:
                t0 = _time.monotonic()
                client.upload_file(str(local_path), s3_bucket, s3_key)
                elapsed = _time.monotonic() - t0
                speed = (size_mb / elapsed) if elapsed > 0 else 0
                _logger.info(
                    "S3 upload OK: %s (%.2f MB) in %.1fs (%.1f MB/s) -> s3://%s/%s",
                    filename, size_mb, elapsed, speed, s3_bucket, s3_key,
                )
                uploaded.append(s3_key)
                break
            except Exception as exc:
                if attempt < 2:
                    backoff = 4 * (2 ** attempt)
                    _logger.warning(
                        "S3 upload attempt %d/3 failed for %s: %s. Retrying in %ds.",
                        attempt + 1, filename, exc, backoff,
                    )
                    _time.sleep(backoff)
                else:
                    _logger.error("S3 upload FAILED after 3 attempts: %s — %s", filename, exc)

    _logger.info("S3 upload complete: %d/%d files uploaded for repo %s", len(uploaded), len(all_jsonl), repo_id)
    return uploaded


def _run_swe_steps_standalone(db_name, repo_id, org, repo_name, tokens, out_dir,
                              retry_attempts, delay_on_error, pipeline_mode="swe"):
    from odoo.addons.jaeger.tools.github_token_pool import GitHubTokenPool
    from odoo.addons.jaeger.tools.get_all_prs import main as get_all_prs
    from odoo.addons.jaeger.tools.filter_prs import main as filter_prs
    from odoo.addons.jaeger.tools.get_related_issues import main as get_related_issues
    from odoo.addons.jaeger.tools.merge_prs_with_issues import main as merge_prs_with_issues
    from odoo.addons.jaeger.tools.build_dataset import main as build_dataset
    from odoo.orm.registry import Registry

    pool = GitHubTokenPool(tokens)

    # ── Step 1: Fetch PRs ────────────────────────────────────────────────
    _check_cancelled(db_name, repo_id)
    _heartbeat_standalone(db_name, repo_id, "Step 1/5: Fetching PRs...")
    _append_log_standalone(db_name, repo_id, "Step 1/5: Fetching all pull requests...")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_step": "Step 1/5: Fetching PRs...",
        "pr_collection_progress": 0,
    })

    get_all_prs(pool, out_dir, org, repo_name)
    prs_file = out_dir / f"{org}__{repo_name}_prs.jsonl"
    _validate_step_output(prs_file, 1)
    total_prs = _count_lines(prs_file)

    _append_log_standalone(db_name, repo_id, f"Step 1/5 done: {total_prs} PRs fetched")
    _write_with_retry(db_name, repo_id, {
        "prs_jsonl_path": str(prs_file),
        "total_prs_fetched": total_prs,
        "pr_collection_progress": 20,
        "pr_collection_step": f"Step 1/5 done: {total_prs} PRs fetched",
    })

    # ── Step 2: Filter PRs ───────────────────────────────────────────────
    _check_cancelled(db_name, repo_id)
    _heartbeat_standalone(db_name, repo_id, f"Step 2/5: Filtering {total_prs} PRs...")
    _append_log_standalone(db_name, repo_id, f"Step 2/5: Filtering {total_prs} PRs...")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_step": f"Step 2/5: Filtering {total_prs} PRs...",
        "pr_collection_progress": 25,
    })

    def _filter_progress(processed, total, passed):
        _check_cancelled(db_name, repo_id)
        pct = 25 + (processed / total) * 15 if total else 25
        step_text = f"Step 2/5: Filtering PRs — {processed}/{total} processed, {passed} passed so far"
        _heartbeat_standalone(db_name, repo_id, step_text)
        _write_with_retry(db_name, repo_id, {
            "pr_collection_step": step_text,
            "pr_collection_progress": round(pct, 1),
            "filtered_prs_count": passed,
        })

    filter_prs(pool, out_dir, prs_file, skip_commit_message=False,
               progress_callback=_filter_progress)
    filtered_file = out_dir / f"{org}__{repo_name}_filtered_prs.jsonl"
    filtered_count = _count_lines(filtered_file)

    _append_log_standalone(db_name, repo_id, f"Step 2/5 done: {filtered_count}/{total_prs} PRs passed filter")
    _write_with_retry(db_name, repo_id, {
        "filtered_prs_jsonl_path": str(filtered_file),
        "filtered_prs_count": filtered_count,
        "pr_collection_progress": 40,
        "pr_collection_step": f"Step 2/5 done: {filtered_count}/{total_prs} PRs passed filter",
    })

    if filtered_count == 0:
        _write_with_retry(db_name, repo_id, {"terminal_state": "no_valid_prs"})
        raise ValueError(f"No PRs passed filtering for {org}/{repo_name}")

    # ── Step 3: Fetch Issues ─────────────────────────────────────────────
    _check_cancelled(db_name, repo_id)
    _heartbeat_standalone(db_name, repo_id, f"Step 3/5: Fetching issues for {filtered_count} PRs...")
    _append_log_standalone(db_name, repo_id, f"Step 3/5: Fetching issues for {filtered_count} PRs...")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_step": f"Step 3/5: Fetching issues for {filtered_count} PRs...",
        "pr_collection_progress": 45,
    })

    get_related_issues(pool, out_dir, filtered_file)
    issues_file = out_dir / f"{org}__{repo_name}_related_issues.jsonl"
    if issues_file.exists():
        _validate_step_output(issues_file, 3)
    issues_count = _count_lines(issues_file)

    _append_log_standalone(db_name, repo_id, f"Step 3/5 done: {issues_count} issues fetched")
    _write_with_retry(db_name, repo_id, {
        "issues_fetched_count": issues_count,
        "pr_collection_progress": 60,
        "pr_collection_step": f"Step 3/5 done: {issues_count} issues fetched",
    })

    # ── Step 4: Merge PRs with Issues ────────────────────────────────────
    _check_cancelled(db_name, repo_id)
    _heartbeat_standalone(db_name, repo_id, "Step 4/5: Merging PRs with issues...")
    _append_log_standalone(db_name, repo_id, "Step 4/5: Merging PRs with issues...")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_step": "Step 4/5: Merging PRs with issues...",
        "pr_collection_progress": 65,
    })

    merge_prs_with_issues(out_dir, org, repo_name)
    merged_file = out_dir / f"{org}__{repo_name}_filtered_prs_with_issues.jsonl"
    _validate_step_output(merged_file, 4)

    _append_log_standalone(db_name, repo_id, "Step 4/5 done: PRs merged with issues")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_progress": 80,
        "pr_collection_step": "Step 4/5 done: PRs merged with issues",
    })

    # ── Step 5: Build Dataset ────────────────────────────────────────────
    _check_cancelled(db_name, repo_id)
    _heartbeat_standalone(db_name, repo_id, f"Step 5/5: Building dataset from {filtered_count} PRs...")
    _append_log_standalone(db_name, repo_id, f"Step 5/5: Building dataset from {filtered_count} PRs...")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_step": f"Step 5/5: Building dataset from {filtered_count} PRs...",
        "pr_collection_progress": 82,
    })

    dataset_file = out_dir / f"{org}__{repo_name}_filtered_prs_with_issues.jsonl"
    build_dataset(pool, out_dir, dataset_file, delay_on_error, retry_attempts, mode=pipeline_mode)
    raw_dataset_file = out_dir / f"{org}__{repo_name}_raw_dataset.jsonl"
    _validate_step_output(raw_dataset_file, 5)
    raw_count = _count_lines(raw_dataset_file)

    _append_log_standalone(db_name, repo_id, f"Step 5/5 done: {raw_count} dataset entries built")
    _write_with_retry(db_name, repo_id, {
        "raw_dataset_jsonl_path": str(raw_dataset_file),
        "raw_dataset_count": raw_count,
        "pr_collection_progress": 95,
        "pr_collection_step": f"Step 5/5 done: {raw_count} dataset entries built",
    })

    if raw_count == 0:
        _write_with_retry(db_name, repo_id, {"terminal_state": "no_valid_prs"})
        raise ValueError(f"Raw dataset is empty for {org}/{repo_name}")

    # ── Create Instances ─────────────────────────────────────────────────
    _check_cancelled(db_name, repo_id)
    _append_log_standalone(db_name, repo_id, f"Creating {raw_count} instances...")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_step": f"Creating {raw_count} instances...",
        "pr_collection_progress": 97,
    })

    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        repo = env["jaeger.repository"].browse(repo_id)
        repo._create_instances_from_dataset(raw_dataset_file)

    _append_log_standalone(db_name, repo_id, f"Instances created: {raw_count}")

    # ── Upload outputs to S3 ────────────────────────────────────────────
    _append_log_standalone(db_name, repo_id, "Uploading output files to S3...")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_step": "Uploading to S3...",
        "pr_collection_progress": 98,
    })
    try:
        uploaded = _upload_outputs_to_s3(db_name, repo_id, out_dir, org, repo_name)
        if uploaded:
            _append_log_standalone(
                db_name, repo_id,
                f"S3 upload complete: {len(uploaded)} files uploaded",
            )
        else:
            _append_log_standalone(db_name, repo_id, "S3 upload skipped (not configured or no files)")
    except Exception as s3_err:
        _logger.warning("S3 upload failed for repo %s: %s (pipeline continues)", repo_id, s3_err)
        _append_log_standalone(db_name, repo_id, f"S3 upload failed (non-fatal): {s3_err}")

    _append_log_standalone(db_name, repo_id, f"Pipeline complete: {raw_count} instances created")
    _write_with_retry(db_name, repo_id, {
        "pr_collection_status": "done",
        "pr_collection_progress": 100,
        "pr_collection_step": "",
        "terminal_state": "none",
        "error_message": False,
    })

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            repo = env["jaeger.repository"].browse(repo_id)
            if repo.exists():
                gate_ok, _ = repo._check_current_gate()
                if gate_ok:
                    next_stage = repo._next_stage()
                    if next_stage:
                        repo.write({"current_stage": next_stage})
                        _logger.info("Repo %s advanced to %s", repo_id, next_stage)
    except Exception:
        _logger.warning("Stage advancement failed for repo %s (cron will catch up)", repo_id, exc_info=True)
