import logging
import os
import threading
import time as _time
from datetime import datetime

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

STAGE_SELECTION = [
    ("stage1", "1 - Repo Validation"),
    ("stage2", "2 - PR Collection"),
    ("stage3", "3 - Docker Build"),
    ("stage4", "4 - Test Execution"),
    ("stage5", "5 - Dataset Finalization"),
    ("stage6", "6 - Trajectory Generation"),
    ("stage7", "7 - Delivery & Export"),
    ("done", "Complete"),
    ("failed", "Failed"),
]

TERMINAL_STATE_SELECTION = [
    ("none", "None"),
    ("repo_not_suitable", "Repository Not Suitable"),
    ("no_valid_prs", "No Valid PRs Found"),
    ("build_failed", "Docker Build Failed"),
    ("no_valid_instances", "No Valid Instances"),
    ("complete", "Complete"),
]

STATUS_SELECTION = [
    ("pending", "Pending"),
    ("queued", "Queued"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]

LANGUAGE_SELECTION = [
    ("python", "Python"),
    ("java", "Java"),
    ("typescript", "TypeScript"),
    ("javascript", "JavaScript"),
    ("go", "Go"),
    ("rust", "Rust"),
    ("c", "C"),
    ("cpp", "C++"),
]

PIPELINE_MODE_SELECTION = [
    ("swe", "SWE (Single-PR Tasks)"),
    ("lht", "LHT (Long-Horizon Tasks)"),
]

TRAJECTORY_STATUS_SELECTION = [
    ("pending", "Pending"),
    ("dispatched", "Dispatched"),
    ("running", "Running"),
    ("evaluating", "Evaluating"),
    ("summarizing", "Summarizing"),
    ("done", "Done"),
    ("failed", "Failed"),
]

TASK_CATEGORY_SELECTION = [
    ("hard_swe", "Hard SWE"),
    ("long_horizon", "Long Horizon"),
    ("real_coder", "Real Coder"),
]


MAX_CONCURRENT_SCRAPES = 500  # Cluster-wide cap (kaiju_build uses 1500 for builds)

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
                retry_attempts, delay_on_error,
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
                              retry_attempts, delay_on_error):
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

    filter_prs(pool, out_dir, prs_file, skip_commit_message=False)
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
    build_dataset(pool, out_dir, dataset_file, delay_on_error, retry_attempts)
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
        "current_stage": "stage3",
    })


class JaegerRepository(models.Model):
    _name = "jaeger.repository"
    _description = "Jaeger Repository"
    _inherit = ["mail.thread"]
    _order = "create_date desc"

    # ── Identity ─────────────────────────────────────────────────────────
    name = fields.Char(
        string="Reference",
        readonly=True,
        copy=False,
        default="New",
    )
    repo_url = fields.Char(string="GitHub URL", required=True, tracking=True)
    org = fields.Char(string="Organization", compute="_compute_org_repo", store=True)
    repo_name = fields.Char(
        string="Repository Name", compute="_compute_org_repo", store=True,
    )
    language = fields.Selection(
        LANGUAGE_SELECTION, string="Language", default="python", required=True,
    )
    pipeline_mode = fields.Selection(
        PIPELINE_MODE_SELECTION,
        string="Pipeline Mode",
        default="swe",
        required=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Assigned To",
        default=lambda self: self.env.uid,
        tracking=True,
    )
    is_admin = fields.Boolean(compute="_compute_is_admin")

    # ── Stage Tracking ───────────────────────────────────────────────────
    current_stage = fields.Selection(
        STAGE_SELECTION,
        string="Current Stage",
        default="stage1",
        tracking=True,
        index=True,
    )
    terminal_state = fields.Selection(
        TERMINAL_STATE_SELECTION,
        string="Terminal State",
        default="none",
    )

    # ── Stage 1: Repo Discovery & Validation ─────────────────────────────
    stars = fields.Integer(string="Stars")
    forks = fields.Integer(string="Forks")
    has_ci = fields.Boolean(string="Has CI")
    is_maintained = fields.Boolean(string="Is Maintained")
    is_fork = fields.Boolean(string="Is Fork")
    repo_description = fields.Text(string="Description")
    crawl_status = fields.Selection(
        STATUS_SELECTION, string="Crawl Status", default="pending",
    )

    # ── Stage 2: PR Collection & Filtering ────────────────────────────────
    pr_collection_status = fields.Selection(
        STATUS_SELECTION,
        string="PR Collection Status",
        default="pending",
        index=True,
    )
    pr_collection_progress = fields.Float(string="PR Collection Progress")
    pr_collection_step = fields.Char(string="Current Step", readonly=True)
    total_prs_fetched = fields.Integer(string="Total PRs Fetched")
    filtered_prs_count = fields.Integer(string="Filtered PRs")
    issues_fetched_count = fields.Integer(string="Issues Fetched")
    raw_dataset_count = fields.Integer(string="Raw Dataset Count")
    prs_jsonl_path = fields.Char(string="PRs JSONL Path")
    filtered_prs_jsonl_path = fields.Char(string="Filtered PRs JSONL Path")
    raw_dataset_jsonl_path = fields.Char(string="Raw Dataset JSONL Path")

    # ── Stage 2 (LHT-specific) ───────────────────────────────────────────
    tags_jsonl_path = fields.Char(string="Tags JSONL Path")
    tag_groups_jsonl_path = fields.Char(string="Tag Groups JSONL Path")
    max_tags = fields.Integer(string="Max Tags", default=200)
    window_days = fields.Integer(string="Window Days", default=30)
    tags_fetched_count = fields.Integer(string="Tags Fetched")
    tag_groups_count = fields.Integer(string="Tag Groups")

    # ── Stage 3: Docker Image Building ────────────────────────────────────
    docker_build_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("queued", "Queued"),
            ("building", "Building"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Docker Build Status",
        default="pending",
        index=True,
    )
    docker_build_progress = fields.Float(string="Docker Build Progress")
    docker_image_prefix = fields.Char(string="Image Prefix")
    ecr_url = fields.Char(string="ECR URL")
    ecr_prefix = fields.Char(string="ECR Prefix")
    # kaiju_build_id = fields.Many2one("kaiju.build", string="Kaiju Build")  # Phase 2-7
    images_built_count = fields.Integer(string="Images Built")
    images_failed_count = fields.Integer(string="Images Failed")
    force_build = fields.Boolean(string="Force Build")
    docker_platform = fields.Char(
        string="Docker Platform",
        help="e.g. linux/amd64. Leave empty for native platform (recommended for local testing).",
    )
    base_image_name = fields.Char(
        string="Base Image Name",
        readonly=True,
        help="Auto-built repo base image tag. Empty = will auto-build on first Docker build.",
    )
    base_image_status = fields.Selection(
        [
            ("none", "Not Built"),
            ("building", "Building"),
            ("built", "Built"),
            ("failed", "Failed"),
        ],
        string="Base Image Status",
        default="none",
    )

    # ── Stage 4: Test Execution ───────────────────────────────────────────
    test_execution_status = fields.Selection(
        STATUS_SELECTION,
        string="Test Execution Status",
        default="pending",
        index=True,
    )
    test_execution_progress = fields.Float(string="Test Execution Progress")
    instances_tested_count = fields.Integer(string="Instances Tested")
    instances_valid_count = fields.Integer(string="Instances Valid")
    instances_invalid_count = fields.Integer(string="Instances Invalid")
    instances_error_count = fields.Integer(string="Instances Error")
    human_mode = fields.Boolean(string="Human Mode (Sequential)", default=True)
    agent_timeout = fields.Integer(string="Agent Timeout (s)", default=1800)

    # ── Stage 5: Dataset Finalization ─────────────────────────────────────
    dataset_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("generating", "Generating"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Dataset Status",
        default="pending",
    )
    final_dataset_jsonl_path = fields.Char(string="Final Dataset JSONL Path")
    final_dataset_count = fields.Integer(string="Final Dataset Count")
    final_report_json = fields.Text(string="Final Report JSON")
    total_instances = fields.Integer(string="Total Instances")
    resolved_instances = fields.Integer(string="Resolved Instances")
    unresolved_instances = fields.Integer(string="Unresolved Instances")
    empty_patch_instances = fields.Integer(string="Empty Patch Instances")
    error_instances = fields.Integer(string="Error Instances")

    # ── Stage 6: Trajectory Generation ────────────────────────────────────
    trajectory_status = fields.Selection(
        TRAJECTORY_STATUS_SELECTION,
        string="Trajectory Status",
        default="pending",
    )
    eks_job_id = fields.Char(string="EKS Job ID", index=True)
    model_name = fields.Char(string="LLM Model", default="claude")
    model_canonical_name = fields.Char(string="Model Canonical Name")
    k_runs = fields.Integer(string="K Runs (pass@k)", default=8)
    num_workers = fields.Integer(string="Inference Workers", default=1)
    max_iterations = fields.Integer(string="Max Iterations", default=300)
    max_retries = fields.Integer(string="Max Retries", default=3)
    conversation_timeout = fields.Integer(
        string="Conversation Timeout (s)", default=3600,
    )
    temperature = fields.Float(string="Temperature", default=1.0)
    llm_config_json = fields.Text(string="LLM Config JSON")
    pass_at_k = fields.Float(string="pass@k")
    pass_at_k_summary_json = fields.Text(string="pass@k Summary JSON")
    total_api_cost = fields.Float(string="Total API Cost (USD)")
    total_api_calls = fields.Integer(string="Total API Calls")
    total_prompt_tokens = fields.Integer(string="Total Prompt Tokens")
    total_completion_tokens = fields.Integer(string="Total Completion Tokens")

    # ── Stage 7: Delivery & Export ────────────────────────────────────────
    delivery_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("converting", "Converting"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Delivery Status",
        default="pending",
    )
    meta_delivery_jsonl_path = fields.Char(string="Meta Delivery JSONL Path")
    delivered_count = fields.Integer(string="Delivered Count")
    task_category = fields.Selection(
        TASK_CATEGORY_SELECTION, string="Task Category",
    )

    # ── Logging ───────────────────────────────────────────────────────────
    log_output = fields.Text(string="Log Output")
    error_message = fields.Text(string="Error Message")
    cancel_requested = fields.Boolean(default=False)
    last_heartbeat = fields.Datetime(string="Last Heartbeat", readonly=True)

    # ── Relations ─────────────────────────────────────────────────────────
    instance_ids = fields.One2many(
        "jaeger.instance", "repository_id", string="Instances",
    )
    run_ids = fields.One2many(
        "jaeger.trajectory.run", "repository_id", string="Trajectory Runs",
    )

    # ── JSONL Preview Fields ─────────────────────────────────────────────
    raw_dataset_preview = fields.Text(
        string="Raw Dataset Preview", compute="_compute_jsonl_previews",
    )
    final_dataset_preview = fields.Text(
        string="Final Dataset Preview", compute="_compute_jsonl_previews",
    )

    # ── Computed Fields ───────────────────────────────────────────────────

    @api.depends("repo_url")
    def _compute_org_repo(self):
        for rec in self:
            if rec.repo_url:
                parts = rec.repo_url.strip().rstrip("/").removesuffix(".git").split("/")
                if len(parts) >= 2:
                    rec.org = parts[-2]
                    rec.repo_name = parts[-1]
                else:
                    rec.org = ""
                    rec.repo_name = ""
            else:
                rec.org = ""
                rec.repo_name = ""

    @api.constrains("repo_url")
    def _check_repo_url(self):
        for rec in self:
            url = (rec.repo_url or "").strip().rstrip("/").removesuffix(".git")
            if not url.startswith("https://github.com/"):
                raise ValidationError(
                    "Only GitHub URLs are supported (https://github.com/org/repo).",
                )
            parts = url.replace("https://github.com/", "").split("/")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValidationError(
                    "URL must be in format: https://github.com/org/repo",
                )

    def _compute_is_admin(self):
        is_admin = self.env.user.has_group("jaeger.group_jaeger_admin")
        for rec in self:
            rec.is_admin = is_admin

    @api.depends("raw_dataset_jsonl_path", "final_dataset_jsonl_path")
    def _compute_jsonl_previews(self):
        for rec in self:
            rec.raw_dataset_preview = rec._read_jsonl_preview(
                rec.raw_dataset_jsonl_path,
            )
            rec.final_dataset_preview = rec._read_jsonl_preview(
                rec.final_dataset_jsonl_path,
            )

    def _read_jsonl_preview(self, file_path, max_lines=20):
        """Read the first N lines of a JSONL file for UI preview.

        If the local file doesn't exist, attempt to stream the first
        ``max_lines`` from S3 using the configured bucket/prefix.
        """
        import json as json_mod
        from pathlib import Path

        if not file_path:
            return ""
        p = Path(file_path)

        raw_lines = None

        # Try local file first
        if p.exists():
            with open(p, encoding="utf-8") as f:
                raw_lines = f.readlines()
        else:
            # Fallback: fetch from S3
            raw_lines = self._fetch_lines_from_s3(file_path)

        if raw_lines is None:
            return f"File not available locally or on S3: {file_path}"

        lines = []
        total_lines = len(raw_lines)
        for i, line in enumerate(raw_lines):
            if i >= max_lines:
                break
            try:
                obj = json_mod.loads(line)
                lines.append(json_mod.dumps(obj, indent=2, ensure_ascii=False))
            except json_mod.JSONDecodeError:
                lines.append(line.rstrip())
        if total_lines > max_lines:
            lines.append(f"\n... ({total_lines - max_lines} more lines)")
        return "\n".join(lines)

    def _fetch_lines_from_s3(self, file_path):
        """Try to read a JSONL file from S3 given its local path.

        Extracts the filename from *file_path*, builds the S3 key using
        the repo id, and streams the object content.  Returns a list of
        lines on success, or ``None`` on failure.
        """
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            return None

        ICP = self.env["ir.config_parameter"].sudo()
        s3_bucket = ICP.get_param("jaeger.s3_bucket", "")
        s3_region = ICP.get_param("jaeger.s3_region", "ap-south-1")
        s3_prefix = ICP.get_param("jaeger.s3_prefix", "jaeger/phase1")
        if not s3_bucket:
            return None

        filename = os.path.basename(file_path)
        s3_key = f"{s3_prefix}/{self.id}/{filename}"
        try:
            config_kwargs = {"connect_timeout": 10, "read_timeout": 30}
            if os.environ.get("JAEGER_S3_ENDPOINT"):
                config_kwargs["s3"] = {"addressing_style": "path"}
            client = boto3.client(
                "s3",
                region_name=s3_region,
                endpoint_url=os.environ.get(
                    "JAEGER_S3_ENDPOINT",
                    f"https://s3.{s3_region}.amazonaws.com",
                ),
                config=BotoConfig(**config_kwargs),
            )
            resp = client.get_object(Bucket=s3_bucket, Key=s3_key)
            body = resp["Body"].read().decode("utf-8", errors="replace")
            return body.splitlines(keepends=True)
        except Exception:
            _logger.debug("S3 preview fallback failed for %s", s3_key, exc_info=True)
            return None

    # ── Sequence ──────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "jaeger.repository",
                ) or "New"
        return super().create(vals_list)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _append_log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
        self.env.cr.execute(
            "UPDATE jaeger_repository SET log_output = "
            "CASE WHEN LENGTH(COALESCE(log_output, '')) > 200000 "
            "THEN RIGHT(log_output, 150000) || %s "
            "ELSE COALESCE(log_output, '') || %s END "
            "WHERE id = %s",
            [line, line, self.id],
        )
        self.invalidate_recordset(["log_output"])

    def _count_jsonl_lines(self, path):
        try:
            with open(path) as f:
                return sum(1 for line in f if line.strip())
        except FileNotFoundError:
            return 0

    def _run_pipeline_async(self, method_name, status_field, label):
        from odoo.orm.registry import Registry

        repo_id = self.id
        dbname = self.env.cr.dbname

        def _bg_run():
            try:
                if method_name == "run_scrape_pipeline":
                    _run_scrape_pipeline_standalone(dbname, repo_id)
                else:
                    registry = Registry(dbname)
                    with registry.cursor() as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        repo = env["jaeger.repository"].browse(repo_id)
                        try:
                            getattr(repo, method_name)()
                        except Exception as e:
                            _logger.exception(
                                "Jaeger background %s failed for repo %s",
                                label, repo_id,
                            )
                            try:
                                cr.rollback()
                                repo.write({
                                    status_field: "failed",
                                    "error_message": str(e)[:2000],
                                })
                                cr.commit()
                            except Exception:
                                _logger.exception("Failed to write error status")
            except Exception:
                _logger.exception(
                    "Jaeger background %s failed for repo %s", label, repo_id,
                )

        thread = threading.Thread(
            target=_bg_run, daemon=True,
            name=f"jaeger_{method_name}_{repo_id}",
        )
        thread.start()

    # ── Downloads ──────────────────────────────────────────────────────────

    def action_download_raw_dataset(self):
        self.ensure_one()
        if not self.raw_dataset_jsonl_path:
            raise UserError("No raw dataset available for download.")
        return {
            "type": "ir.actions.act_url",
            "url": f"/jaeger/download/{self.id}/raw_dataset",
            "target": "new",
        }

    # ── Stage 1 Actions ──────────────────────────────────────────────────

    def action_validate_repo(self):
        self.ensure_one()
        if not self.repo_url:
            raise UserError("GitHub URL is required.")
        self.write({"crawl_status": "running"})
        try:
            self._validate_repo_metadata()
            self.write({"crawl_status": "done", "current_stage": "stage2"})
        except Exception as e:
            error_msg = str(e)[:2000]
            self.write(
                {
                    "crawl_status": "failed",
                    "error_message": error_msg,
                    "terminal_state": "repo_not_suitable",
                },
            )
            raise UserError(error_msg) from e

    def _validate_repo_metadata(self):
        from ..tools.github_token_pool import get_token_pool

        pool = get_token_pool(self.env)
        token = pool.get_token()

        from github import Auth, Github

        log_lines = []
        def _log(msg):
            line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
            log_lines.append(line)
            _logger.info("Validate %s/%s: %s", self.org, self.repo_name, msg)

        _log("Connecting to GitHub API...")
        g = Github(auth=Auth.Token(token))
        repo = g.get_repo(f"{self.org}/{self.repo_name}")
        _log(f"Repository found: {repo.full_name}")

        rate = g.get_rate_limit()
        pool.report_usage(token, rate.rate.remaining, rate.rate.reset.timestamp())
        _log(f"Rate limit: {rate.rate.remaining}/{rate.rate.limit} remaining")

        self.write({
            "stars": repo.stargazers_count,
            "forks": repo.forks_count,
            "is_fork": repo.fork,
            "repo_description": (repo.description or "")[:500],
            "log_output": "\n".join(log_lines) + "\n",
        })

    # ── Stage 2 Actions ──────────────────────────────────────────────────

    def action_collect_prs(self):
        self.ensure_one()

        if self.current_stage != "stage2":
            raise UserError("Repository must be in Stage 2.")
        if not self.org or not self.repo_name:
            raise UserError("GitHub URL is required and must be valid.")

        ICP = self.env["ir.config_parameter"].sudo()
        tokens = ICP.get_param("jaeger.github_tokens", "").strip()
        if not tokens:
            raise UserError(
                "No GitHub tokens configured. Go to Settings → Jaeger → GitHub Tokens.",
            )

        active_count = self.search_count([
            ("pr_collection_status", "in", ["queued", "running"]),
        ])
        if active_count >= MAX_CONCURRENT_SCRAPES:
            raise UserError(
                "Scrape queue is full (%d active jobs). Please try again shortly."
                % active_count,
            )

        from psycopg2 import OperationalError as Psycopg2OpError
        try:
            self.env.cr.execute(
                "SELECT pr_collection_status FROM jaeger_repository"
                " WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Psycopg2OpError:
            self.env.cr.rollback()
            raise UserError("PR collection is already being started by another user.")
        row = self.env.cr.fetchone()
        if row and row[0] in ("running", "queued"):
            raise UserError("PR collection is already in progress.")

        dispatch_mode = ICP.get_param("jaeger.dispatch_mode", "local")
        db_name = self.env.cr.dbname
        rec_id = self.id

        if dispatch_mode == "k8s":
            job_image = ICP.get_param("jaeger.k8s_job_image", "").strip()
            if not job_image:
                raise UserError(
                    "K8s Job image not configured. Go to Settings → Jaeger → K8s Dispatch.",
                )
            self.write({
                "pr_collection_status": "queued", "error_message": False,
                "cancel_requested": False, "log_output": "",
            })

            def _dispatch_k8s():
                try:
                    from odoo.orm.registry import Registry
                    with Registry(db_name).cursor() as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        repo = env["jaeger.repository"].browse(rec_id)
                        repo._create_scrape_k8s_job()
                except Exception as e:
                    _logger.exception("K8s Job creation failed for repo %s", rec_id)
                    try:
                        from odoo.orm.registry import Registry
                        with Registry(db_name).cursor() as cr:
                            env = api.Environment(cr, SUPERUSER_ID, {})
                            repo = env["jaeger.repository"].browse(rec_id)
                            repo.write({
                                "pr_collection_status": "failed",
                                "error_message": "K8s Job creation failed: %s" % str(e)[:1500],
                            })
                    except Exception:
                        _logger.exception("Failed to record K8s dispatch failure")

            self.env.cr.postcommit.add(_dispatch_k8s)
        else:
            self.write({
                "pr_collection_status": "running", "error_message": False,
                "cancel_requested": False, "log_output": "",
            })

            def _dispatch_local():
                t = threading.Thread(
                    target=_run_scrape_pipeline_standalone,
                    args=(db_name, rec_id),
                    daemon=True,
                    name=f"jaeger_scrape_{rec_id}",
                )
                t.start()

            self.env.cr.postcommit.add(_dispatch_local)

    def _create_scrape_k8s_job(self):
        """Create a K8s Job to run worker/run_pipeline.py (kaiju_build pattern)."""
        try:
            from kubernetes import client, config as k8s_config
        except ImportError:
            raise RuntimeError(
                "kubernetes package not installed. Required for K8s dispatch mode. "
                "pip install kubernetes"
            )

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            config_file = os.environ.get("KUBECONFIG")
            k8s_config.load_kube_config(
                config_file=config_file if config_file else None,
            )
        batch_v1 = client.BatchV1Api()

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")
        job_image = ICP.get_param("jaeger.k8s_job_image", "")
        s3_bucket = ICP.get_param("jaeger.s3_bucket", "")
        s3_region = ICP.get_param("jaeger.s3_region", "ap-south-1")
        s3_prefix = ICP.get_param("jaeger.s3_prefix", "jaeger/phase1")
        db_name = self.env.cr.dbname
        sandbox = ICP.get_param("jaeger.sandbox_mode", "0") == "1"

        job_name = "jaeger-scrape-%s" % self.id
        env_vars = [
            client.V1EnvVar(name="REPO_ID", value=str(self.id)),
            client.V1EnvVar(name="ODOO_DB", value=db_name),
            client.V1EnvVar(name="JAEGER_S3_BUCKET", value=s3_bucket),
            client.V1EnvVar(name="JAEGER_S3_REGION", value=s3_region),
            client.V1EnvVar(name="JAEGER_S3_PREFIX", value=s3_prefix),
        ]

        if sandbox:
            s3_endpoint = ICP.get_param("jaeger.s3_endpoint", "")
            if s3_endpoint:
                env_vars.append(
                    client.V1EnvVar(name="JAEGER_S3_ENDPOINT", value=s3_endpoint),
                )

        pod_spec_kwargs = {
            "restart_policy": "Never",
            "containers": [
                client.V1Container(
                    name="pipeline",
                    image=job_image,
                    image_pull_policy="Always",
                    command=[
                        "python",
                        "custom_addons/jaeger/worker/run_pipeline.py",
                    ],
                    env=env_vars,
                    resources=client.V1ResourceRequirements(
                        requests={
                            "cpu": "500m",
                            "memory": "1Gi",
                            "ephemeral-storage": "5Gi",
                        },
                        limits={
                            "memory": "2Gi",
                            "ephemeral-storage": "10Gi",
                        },
                    ),
                ),
            ],
        }

        if sandbox:
            pod_spec_kwargs["host_network"] = True
            pod_spec_kwargs["dns_policy"] = "None"
            pod_spec_kwargs["dns_config"] = client.V1PodDNSConfig(
                nameservers=["127.0.0.11"],
            )
        else:
            pod_spec_kwargs["service_account_name"] = "jaeger-pipeline-runner"
            pod_spec_kwargs["node_selector"] = {
                "kubernetes.io/arch": "amd64",
                "ethara.ai/node-pool": "general-purpose",
            }

        labels = {
            "app.kubernetes.io/name": "jaeger-scrape",
            "app.kubernetes.io/component": "pipeline",
            "repo-id": str(self.id),
            "platform": "jaeger",
        }
        if not sandbox:
            labels["kueue.x-k8s.io/queue-name"] = "jaeger-scraping"

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=namespace,
                labels=labels,
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=3600,
                backoff_limit=2,
                active_deadline_seconds=7200,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app.kubernetes.io/name": "jaeger-scrape",
                            "repo-id": str(self.id),
                            "platform": "jaeger",
                        },
                    ),
                    spec=client.V1PodSpec(**pod_spec_kwargs),
                ),
            ),
        )

        try:
            batch_v1.create_namespaced_job(namespace=namespace, body=job)
        except client.ApiException as e:
            if e.status == 409:
                _logger.warning(
                    "K8s Job %s already exists (409 Conflict) — deleting and recreating",
                    job_name,
                )
                batch_v1.delete_namespaced_job(
                    name=job_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Background"),
                )
                _time.sleep(2)
                batch_v1.create_namespaced_job(namespace=namespace, body=job)
            else:
                raise
        _logger.info("Created K8s Job %s for repo %s", job_name, self.name)

    def action_cancel_pipeline(self):
        self.ensure_one()
        active_statuses = ("running", "queued")
        is_active = (
            self.pr_collection_status in active_statuses
            or self.docker_build_status in active_statuses
            or self.test_execution_status in active_statuses
        )
        if not is_active:
            raise UserError("No active pipeline to cancel.")
        self.write({"cancel_requested": True})
        self._append_log("Cancellation requested by user.")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Cancel Requested",
                "message": "Pipeline will stop at the next step boundary.",
                "type": "warning",
                "sticky": False,
            },
        }

    def action_collect_prs_direct(self):
        """Run PR collection in background thread (always local)."""
        self.ensure_one()
        if self.current_stage != "stage2":
            raise UserError("Repository must be in Stage 2.")
        if self.pr_collection_status in ("running", "queued"):
            raise UserError("PR collection is already in progress.")

        self.write({
            "pr_collection_status": "running", "error_message": False,
            "cancel_requested": False, "log_output": "",
        })

        db_name = self.env.cr.dbname
        rec_id = self.id

        def _dispatch_local():
            t = threading.Thread(
                target=_run_scrape_pipeline_standalone,
                args=(db_name, rec_id),
                daemon=True,
                name=f"jaeger_scrape_{rec_id}",
            )
            t.start()

        self.env.cr.postcommit.add(_dispatch_local)

    def run_scrape_pipeline(self):
        """Full Phase 1 scraping pipeline. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"pr_collection_status": "running", "error_message": False})
        self.env.cr.commit()

        ICP = self.env["ir.config_parameter"].sudo()
        tokens = [
            t.strip()
            for t in ICP.get_param("jaeger.github_tokens", "").split(",")
            if t.strip()
        ]
        from pathlib import Path

        output_dir = Path(ICP.get_param("jaeger.output_dir", "/tmp/jaeger_data"))
        out_dir = output_dir / f"{self.org}__{self.repo_name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            if self.pipeline_mode == "swe":
                raise UserError(
                    "SWE pipeline runs via standalone dispatch (action_collect_prs). "
                    "Use the Collect PRs button or the standalone function directly."
                )
            else:
                self._run_lht_pipeline(tokens, out_dir)

            vals = {"pr_collection_status": "done", "terminal_state": "none", "error_message": False}
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            self.write(
                {
                    "pr_collection_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            self._append_log("FAILED: %s" % e)
            self.env.cr.commit()
            raise

    def _run_lht_pipeline(self, tokens, out_dir):
        from ..tools.github_token_pool import GitHubTokenPool
        pool = GitHubTokenPool(tokens)

        self._append_log("Step 1/6: Fetching all pull requests...")
        self.write({"pr_collection_step": "Step 1/6: Fetching PRs...", "pr_collection_progress": 0})
        self.env.cr.commit()
        from ..tools.get_all_prs import main as get_all_prs

        get_all_prs(pool, out_dir, self.org, self.repo_name)
        prs_file = out_dir / f"{self.org}__{self.repo_name}_prs.jsonl"
        total_prs = self._count_jsonl_lines(prs_file)
        self.write({
            "prs_jsonl_path": str(prs_file),
            "total_prs_fetched": total_prs,
            "pr_collection_progress": 16,
            "pr_collection_step": f"Step 1/6 done: {total_prs} PRs fetched",
        })
        self.env.cr.commit()

        self._append_log("Step 2/6: Filtering PRs (LHT mode)...")
        self.write({"pr_collection_step": f"Step 2/6: Filtering {total_prs} PRs (LHT)...", "pr_collection_progress": 20})
        self.env.cr.commit()
        from ..tools.filter_prs import main as filter_prs

        filter_prs(
            pool,
            out_dir,
            prs_file,
            mode="lht",
            skip_commit_message=True,
        )
        lht_filtered = out_dir / f"{self.org}__{self.repo_name}_lht_filtered_prs.jsonl"
        filtered_fallback = out_dir / f"{self.org}__{self.repo_name}_filtered_prs.jsonl"
        filtered_file = lht_filtered if lht_filtered.exists() else filtered_fallback
        filtered_count = self._count_jsonl_lines(filtered_file)
        self.write({
            "filtered_prs_jsonl_path": str(filtered_file),
            "filtered_prs_count": filtered_count,
            "pr_collection_progress": 33,
            "pr_collection_step": f"Step 2/6 done: {filtered_count}/{total_prs} PRs passed filter",
        })
        self.env.cr.commit()

        self._append_log("Step 3/6: Fetching version tags...")
        self.write({"pr_collection_step": "Step 3/6: Fetching version tags...", "pr_collection_progress": 38})
        self.env.cr.commit()
        from ..tools.get_version_tags import main as get_version_tags

        get_version_tags(
            tokens, out_dir, self.org, self.repo_name,
            max_tags=self.max_tags or 200,
        )
        self.write({"pr_collection_progress": 50, "pr_collection_step": "Step 3/6 done: Tags fetched"})
        self.env.cr.commit()

        self._append_log("Step 4/6: Grouping PRs by version ranges...")
        self.write({"pr_collection_step": "Step 4/6: Grouping PRs by tags...", "pr_collection_progress": 55})
        self.env.cr.commit()
        from ..tools.group_prs_by_tags import main as group_prs_by_tags

        group_prs_by_tags(
            out_dir, self.org, self.repo_name,
            window_days=self.window_days or 30,
            tokens=tokens,
        )
        self.write({"pr_collection_progress": 66, "pr_collection_step": "Step 4/6 done: PRs grouped"})
        self.env.cr.commit()

        self._append_log("Step 5/6: Fetching related issues...")
        self.write({"pr_collection_step": "Step 5/6: Fetching related issues...", "pr_collection_progress": 70})
        self.env.cr.commit()
        from ..tools.get_related_issues import main as get_related_issues

        get_related_issues(pool, out_dir, filtered_file)
        self.write({"pr_collection_progress": 83, "pr_collection_step": "Step 5/6 done: Issues fetched"})
        self.env.cr.commit()

        self._append_log("Step 6/6: Building LHT dataset...")
        self.write({"pr_collection_step": "Step 6/6: Building LHT dataset...", "pr_collection_progress": 85})
        self.env.cr.commit()
        from ..tools.build_lht_dataset import main as build_lht_dataset

        build_lht_dataset(
            tokens, out_dir, self.org, self.repo_name, lang=self.language,
        )

        raw_dataset_file = (
            out_dir / f"{self.org}__{self.repo_name}_raw_dataset.jsonl"
        )
        raw_count = self._count_jsonl_lines(raw_dataset_file)
        self.write(
            {
                "raw_dataset_jsonl_path": str(raw_dataset_file),
                "raw_dataset_count": raw_count,
                "pr_collection_progress": 95,
                "pr_collection_step": f"Creating {raw_count} instances...",
            },
        )
        self.env.cr.commit()
        self._create_instances_from_dataset(raw_dataset_file)
        self.write({"pr_collection_progress": 100, "pr_collection_step": ""})

    def _create_instances_from_dataset(self, dataset_path):
        """Parse raw dataset JSONL and create jaeger.instance records."""
        import json

        MAX_PATCH_SIZE = 5 * 1024 * 1024
        MAX_BODY_SIZE = 100 * 1024

        Instance = self.env["jaeger.instance"]
        ResolvedIssue = self.env["jaeger.resolved.issue"]

        skipped = 0
        with open(dataset_path) as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                if len(line) > 10 * 1024 * 1024:
                    _logger.warning("Skipping line %d: exceeds 10MB", line_num)
                    skipped += 1
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as e:
                    _logger.warning("Skipping line %d: invalid JSON: %s", line_num, e)
                    skipped += 1
                    continue

                if len(data.get("fix_patch", "")) > MAX_PATCH_SIZE:
                    _logger.warning(
                        "Skipping PR #%s: fix_patch too large (%d bytes)",
                        data.get("number"), len(data["fix_patch"]),
                    )
                    skipped += 1
                    continue
                if len(data.get("test_patch", "")) > MAX_PATCH_SIZE:
                    _logger.warning(
                        "Skipping PR #%s: test_patch too large (%d bytes)",
                        data.get("number"), len(data["test_patch"]),
                    )
                    skipped += 1
                    continue

                instance_id = data.get("instance_id") or f"{data['org']}__{data['repo']}-{data['number']}"
                existing = Instance.search([("name", "=", instance_id)], limit=1)
                if existing:
                    continue

                raw_number = data.get("number", 0)
                pr_number = raw_number if isinstance(raw_number, int) else int(str(raw_number).split("-")[0] or 0)

                body = (data.get("body") or "")[:MAX_BODY_SIZE]

                instance = Instance.create(
                    {
                        "name": instance_id,
                        "repository_id": self.id,
                        "org": data.get("org", ""),
                        "repo": data.get("repo", ""),
                        "pr_number": pr_number,
                        "state": data.get("state", ""),
                        "title": data.get("title", ""),
                        "body": body,
                        "base_label": data.get("base", {}).get("label", ""),
                        "base_ref": data.get("base", {}).get("ref", ""),
                        "base_sha": data.get("base", {}).get("sha", ""),
                        "fix_patch": data.get("fix_patch", ""),
                        "test_patch": data.get("test_patch", ""),
                        "tag": data.get("tag", ""),
                        "number_interval": data.get("number_interval", ""),
                        "language": data.get("lang", self.language),
                        "resolved_issues_json": json.dumps(
                            data.get("resolved_issues", []),
                        ),
                    },
                )

                for issue in data.get("resolved_issues", []):
                    issue_body = (issue.get("body") or "")[:MAX_BODY_SIZE]
                    ResolvedIssue.create(
                        {
                            "instance_id": instance.id,
                            "issue_number": issue.get("number", 0),
                            "issue_title": issue.get("title", ""),
                            "issue_body": issue_body,
                        },
                    )

                if line_num % 100 == 0:
                    self._append_log(f"Created {line_num} instances...")
                    self.env.cr.commit()

        if skipped:
            self._append_log(f"Skipped {skipped} instances (oversized patches or lines)")

    # ── Stage 3 Actions (Phase 2-7: disabled until infra ready) ────────

    def action_build_docker_images(self):
        self.ensure_one()
        if self.current_stage != "stage3":
            raise UserError("Repository must be in Stage 3.")
        if not self.instance_ids:
            raise UserError("No instances found. Run PR collection first.")
        self.write({"docker_build_status": "queued", "error_message": False})
        from ..services.rabbitmq_service import publish_docker_task

        publish_docker_task(self.id)

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
                    'echo "CLEAN:$(git -C /testbed status --porcelain | wc -l | tr -d \" \")"',
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
        runtime = LANGUAGE_BASE_IMAGES.get(self.language, "python:3.11-slim")

        self.write({"base_image_status": "building"})
        self.env.cr.commit()

        # Get GitHub token for authenticated clones (avoids rate limiting at scale)
        ICP = self.env["ir.config_parameter"].sudo()
        tokens_str = ICP.get_param("jaeger.github_tokens", "")
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

            install_cmds = self._detect_install_commands(clone_dir)

            # Build Dockerfile content
            is_python = self.language in ("python",)
            is_node = self.language in ("javascript", "typescript")

            lines = [f"FROM {runtime}", ""]

            # System dependencies (ca-certificates required for HTTPS git clone)
            if is_python:
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

            # Use authenticated URL inside Dockerfile for rate limit avoidance
            # (token is baked into build layer — acceptable for local/private images)
            dockerfile_clone_url = authed_clone_url if github_token else clone_url
            lines += [
                "WORKDIR /testbed",
                f"RUN git clone {dockerfile_clone_url} . && git fetch --all",
                "",
            ]

            # Install dependencies (may be multiple commands)
            for cmd in install_cmds:
                lines.append(f"RUN {cmd}")
                lines.append("")

            # Add node_modules/.bin to PATH for monorepo tools (lerna, turbo, nx)
            if is_node:
                lines.append('ENV PATH="/testbed/node_modules/.bin:${PATH}"')
                lines.append("")

            # Install test framework
            if is_python:
                lines.append("RUN pip install pytest || true")
                lines.append("")

            # Metadata labels
            lines += [
                f'LABEL org.opencontainers.image.source="https://github.com/{self.org}/{self.repo_name}"',
                'LABEL jaeger.image.type="base"',
            ]

            dockerfile_content = "\n".join(lines) + "\n"

            # Write Dockerfile and build
            build_dir = Path(clone_dir) / "_docker_build"
            build_dir.mkdir(exist_ok=True)
            (build_dir / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

            ICP = self.env["ir.config_parameter"].sudo()
            platform = self.docker_platform or ICP.get_param("jaeger.docker_platform", "")

            cmd = ["docker", "build"]
            if platform:
                cmd += ["--platform", platform]
            cmd += ["-t", base_tag, "-f", str(build_dir / "Dockerfile"), str(build_dir)]

            self._append_log(f"Building base image: {base_tag}")
            self._append_log(f"  Runtime: {runtime}, Language: {self.language}")
            self._append_log(f"  Install cmds: {install_cmds or '(none detected)'}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

            if result.returncode != 0:
                self._append_log(f"Base image build FAILED:\n{result.stderr[-3000:]}")
                raise subprocess.CalledProcessError(result.returncode, cmd)

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
            # Clean up temp dir
            if clone_dir:
                import shutil
                shutil.rmtree(clone_dir, ignore_errors=True)

    def _verify_base_deps(self, base_tag):
        """Verify dependencies are cached in the base image.

        Runs the language's dep tool in offline/no-network mode.
        Returns None if OK, error string if deps are missing.
        """
        import subprocess

        lang = (self.language or "").lower()
        if lang == "rust":
            check_cmd = "cargo check --offline 2>&1 | tail -10"
        elif lang == "go":
            check_cmd = "GONOSUMCHECK=* GOFLAGS=-mod=mod go build ./... 2>&1 | tail -10"
        elif lang in ("javascript", "typescript"):
            check_cmd = "test -d node_modules && echo OK || echo MISSING"
        else:
            return None

        try:
            result = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", base_tag,
                 "bash", "-c", check_cmd],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "Dep check timed out"
        except Exception as e:
            return f"Dep check failed to run: {e}"

        if result.returncode != 0:
            output = (result.stdout + result.stderr)[-500:]
            return f"Dep check failed (exit {result.returncode}): {output}"
        return None

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
            image_tag = f"pr-{inst.pr_number}"
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
            # Re-install deps after checkout — base_sha may have different
            # dependencies than HEAD (which the base image was built from).
            # Most deps are already cached from Layer 1 so this is fast.
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
        """
        import json

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
            test_cmd = f"python -m pytest {test_files or 'tests/'} -v 2>&1"
            return (
                "#!/bin/bash\n"
                "set -uo pipefail\n"
                "cd /testbed\n"
                "echo '>>>>> Start Test Output'\n"
                f"{test_cmd}\n"
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
            f"{test_cmd}\n"
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

    # ── Stage 4 Actions ──────────────────────────────────────────────────

    def action_run_tests(self):
        self.ensure_one()
        if self.current_stage != "stage4":
            raise UserError("Repository must be in Stage 4.")
        built = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built",
        )
        if not built:
            raise UserError("No built images found.")
        self.write({"test_execution_status": "queued", "error_message": False})
        from ..services.rabbitmq_service import batch_publish_test_tasks

        batch_publish_test_tasks(built.ids)

    def action_run_tests_direct(self):
        self.ensure_one()
        if self.current_stage != "stage4":
            raise UserError("Repository must be in Stage 4.")
        built = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built",
        )
        if not built:
            raise UserError("No built images found.")
        if self.test_execution_status in ("running", "queued"):
            raise UserError("Test execution is already in progress.")
        return self._run_pipeline_async(
            "_run_all_tests", "test_execution_status", "Test Execution",
        )

    def _run_all_tests(self):
        """Run test execution for all built instances in parallel."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from .jaeger_instance import _run_instance_tests_standalone

        self.ensure_one()
        self.write({"test_execution_status": "running", "error_message": False})
        self.env.cr.commit()

        built = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built",
        )
        instance_ids = built.ids
        total = len(instance_ids)
        if not total:
            self.write({"test_execution_status": "done", "error_message": "No built instances"})
            self.env.cr.commit()
            return

        db_name = self.env.cr.dbname
        repo_id = self.id

        ICP = self.env["ir.config_parameter"].sudo()
        max_workers = int(ICP.get_param("jaeger.max_run_workers", "2"))
        agent_timeout = int(ICP.get_param("jaeger.agent_timeout", "1800"))

        _append_log_standalone(db_name, repo_id,
            f"Starting parallel test execution: {total} instances, {max_workers} workers")

        completed = valid_count = invalid_count = error_count = 0

        cancelled = False
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_instance_tests_standalone, db_name, iid, agent_timeout): iid
                for iid in instance_ids
            }
            for future in as_completed(futures):
                iid = futures[future]
                try:
                    res = future.result()
                except Exception as e:
                    _logger.error("Instance %s raised: %s", iid, e)
                    res = {"instance_id": iid, "success": False, "is_valid": False,
                           "error": str(e), "summary": f"exception: {e}"}

                completed += 1
                if res.get("is_valid"):
                    valid_count += 1
                elif res.get("success"):
                    invalid_count += 1
                if res.get("error"):
                    error_count += 1

                summary = res.get("summary") or res.get("error") or "done"
                _append_log_standalone(db_name, repo_id,
                    f"  [{completed}/{total}] instance #{iid}: {summary}")

                _write_with_retry(db_name, repo_id, {
                    "test_execution_progress": (completed / total) * 100,
                    "instances_tested_count": completed,
                    "instances_valid_count": valid_count,
                    "instances_invalid_count": invalid_count,
                    "instances_error_count": error_count,
                })

                try:
                    _check_cancelled(db_name, repo_id)
                except PipelineCancelled:
                    _append_log_standalone(db_name, repo_id, "Cancellation requested — stopping remaining instances")
                    for f in futures:
                        f.cancel()
                    cancelled = True
                    break

        if cancelled:
            _append_log_standalone(db_name, repo_id,
                f"Test execution cancelled: {completed}/{total} done, {valid_count} valid, {invalid_count} invalid")
        else:
            _append_log_standalone(db_name, repo_id,
                f"Test execution complete: {valid_count} valid, {invalid_count} invalid, {error_count} errors")

        vals = {"test_execution_status": "done", "terminal_state": "none", "error_message": False,
                "cancel_requested": False}
        try:
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception:
            _logger.warning("Final write via ORM failed, using standalone retry")
            _write_with_retry(db_name, repo_id, vals)

    # ── Stage 5 Actions ──────────────────────────────────────────────────

    def action_finalize_dataset(self):
        self.ensure_one()
        if self.current_stage != "stage5":
            raise UserError("Repository must be in Stage 5.")
        self.write({"dataset_status": "generating", "error_message": False})
        from ..services.rabbitmq_service import publish_finalize_task

        publish_finalize_task(self.id)

    def action_finalize_dataset_direct(self):
        self.ensure_one()
        if self.current_stage != "stage5":
            raise UserError("Repository must be in Stage 5.")
        if self.dataset_status in ("generating", "queued"):
            raise UserError("Dataset finalization is already in progress.")
        return self._run_pipeline_async(
            "run_dataset_finalization", "dataset_status", "Dataset Finalization",
        )

    def run_dataset_finalization(self):
        """Build final dataset JSONL. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"dataset_status": "generating", "error_message": False})
        self.env.cr.commit()
        try:
            self._build_final_dataset()
            vals = {"dataset_status": "done", "terminal_state": "none", "error_message": False}
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            self.write(
                {
                    "dataset_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            self.env.cr.commit()
            raise

    def _build_final_dataset(self):
        """Build final dataset JSONL from validated instances.

        Aggregates test results across all instances, writes the final
        dataset JSONL file, and produces a FinalReport with statistics.
        """
        import json
        from pathlib import Path

        ICP = self.env["ir.config_parameter"].sudo()
        output_dir = Path(ICP.get_param("jaeger.output_dir", "/tmp/jaeger_data"))
        out_dir = output_dir / f"{self.org}__{self.repo_name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        self._append_log("Step 1/3: Counting instance results...")

        all_instances = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built" and i.report_json,
        )
        valid = all_instances.filtered(lambda i: i.is_valid)
        invalid = all_instances.filtered(lambda i: not i.is_valid)

        total = len(all_instances)
        valid_count = len(valid)
        invalid_count = len(invalid)
        error_count = len(self.instance_ids.filtered(
            lambda i: i.validation_error and "error" in (i.validation_error or "").lower(),
        ))
        empty_patch = len(self.instance_ids.filtered(
            lambda i: not i.fix_patch,
        ))

        self._append_log(
            f"  {total} tested, {valid_count} valid, "
            f"{invalid_count} invalid, {error_count} errors",
        )

        if valid_count == 0:
            self.write({
                "terminal_state": "no_valid_instances",
                "error_message": "No valid instances after test execution.",
            })
            raise ValueError(
                f"No valid instances for {self.org}/{self.repo_name}",
            )

        self._append_log("Step 2/3: Writing final dataset JSONL...")

        final_path = out_dir / f"{self.org}__{self.repo_name}_final_dataset.jsonl"
        count = 0
        with open(final_path, "w", encoding="utf-8") as f:
            for inst in valid:
                entry = {
                    "instance_id": inst.name,
                    "org": inst.org,
                    "repo": inst.repo,
                    "pr_number": inst.pr_number,
                    "base_sha": inst.base_sha,
                    "language": inst.language,
                    "fix_patch": inst.fix_patch or "",
                    "test_patch": inst.test_patch or "",
                    "f2p_tests": json.loads(inst.f2p_tests_json or "{}"),
                    "p2p_tests": json.loads(inst.p2p_tests_json or "{}"),
                    "docker_image_name": inst.docker_image_name or "",
                    "is_valid": True,
                    "tag": inst.tag or "",
                    "version": inst.tag or inst.base_sha or "",
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                count += 1

        self._append_log(f"  Wrote {count} entries to {final_path}")

        self._append_log("Step 3/3: Generating final report...")

        report = {
            "repository": f"{self.org}/{self.repo_name}",
            "total_instances": total,
            "valid_instances": valid_count,
            "invalid_instances": invalid_count,
            "error_instances": error_count,
            "empty_patch_instances": empty_patch,
            "f2p_total": sum(inst.f2p_count for inst in valid),
            "p2p_total": sum(inst.p2p_count for inst in valid),
        }

        report_path = out_dir / f"{self.org}__{self.repo_name}_final_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.write({
            "final_dataset_jsonl_path": str(final_path),
            "final_dataset_count": count,
            "final_report_json": json.dumps(report),
            "total_instances": total,
            "resolved_instances": valid_count,
            "unresolved_instances": invalid_count,
            "empty_patch_instances": empty_patch,
            "error_instances": error_count,
        })

        self._append_log(f"Finalization complete: {count} valid instances in final dataset")

    # ── Stage 6 Actions ──────────────────────────────────────────────────

    def action_dispatch_trajectories(self):
        raise UserError("Phase 2-7 not available yet. Only Phase 1 (PR Collection) is active.")
        self.ensure_one()
        if self.current_stage != "stage6":
            raise UserError("Repository must be in Stage 6.")
        self.write({"trajectory_status": "dispatched", "error_message": False})
        from ..services.rabbitmq_service import publish_trajectory_task

        publish_trajectory_task(self.id)

    def run_trajectory_dispatch(self):
        """Dispatch trajectory generation to EKS. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"trajectory_status": "running", "error_message": False})
        try:
            self._dispatch_to_eks()
        except Exception as e:
            self.write(
                {
                    "trajectory_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            raise

    def _dispatch_to_eks(self):
        """Dispatch trajectory generation jobs to EKS.

        For each valid instance, creates K pods on EKS (one per pass@k run).
        Each pod runs the SWE agent with the configured LLM model, receives
        the problem statement, and produces a patch.
        """
        import json
        import uuid

        ICP = self.env["ir.config_parameter"].sudo()
        config = self._resolve_trajectory_config()

        # Gather valid instances
        valid_instances = self.instance_ids.filtered(
            lambda i: i.is_valid and i.docker_build_status == "built",
        )
        if not valid_instances:
            raise ValueError(f"No valid instances for trajectory generation in {self.name}")

        k_runs = config.get("k_runs", 8)
        total_pods = len(valid_instances) * k_runs

        self._append_log(
            f"Dispatching {total_pods} trajectory pods "
            f"({len(valid_instances)} instances x {k_runs} runs)",
        )

        # Generate unique job ID
        job_id = f"jaeger-traj-{self.org}-{self.repo_name}-{uuid.uuid4().hex[:8]}"
        self.write({
            "eks_job_id": job_id,
            "trajectory_status": "running",
            "llm_config_json": json.dumps(config),
        })

        # Create trajectory run records
        Run = self.env["jaeger.trajectory.run"]
        for inst in valid_instances:
            for run_num in range(1, k_runs + 1):
                Run.create({
                    "name": f"{inst.name}-run-{run_num}",
                    "instance_id": inst.id,
                    "repository_id": self.id,
                    "run_number": run_num,
                    "model": config.get("model_name", "claude"),
                    "status": "queued",
                    "eks_pod_name": f"{job_id}-{inst.name}-{run_num}".lower().replace("__", "-"),
                })

        self._append_log(f"Created {total_pods} trajectory run records (job_id={job_id})")

        # Dispatch to EKS
        try:
            eks_cluster = ICP.get_param("jaeger.eks_cluster", "")
            eks_namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

            if not eks_cluster:
                self._append_log("WARNING: No EKS cluster configured. Runs created but not dispatched.")
                return

            self._create_eks_jobs(config, valid_instances, k_runs, eks_cluster, eks_namespace)
            self._append_log("EKS jobs dispatched successfully")

        except Exception as e:
            self._append_log(f"EKS dispatch error: {e}")
            raise

    def _resolve_trajectory_config(self):
        """Build trajectory configuration, merging per-repo overrides with system defaults."""
        import json

        ICP = self.env["ir.config_parameter"].sudo()

        config = {
            "model_name": self.model_canonical_name or ICP.get_param(
                "jaeger.default_model", "claude",
            ),
            "k_runs": self.k_runs or int(ICP.get_param("jaeger.default_k", "8")),
            "num_workers": self.num_workers or int(ICP.get_param(
                "jaeger.max_run_workers", "1",
            )),
            "max_iterations": self.max_iterations or 300,
            "max_retries": self.max_retries or 3,
            "conversation_timeout": self.conversation_timeout or int(ICP.get_param(
                "jaeger.conversation_timeout", "3600",
            )),
            "temperature": self.temperature if self.temperature else float(ICP.get_param(
                "jaeger.temperature", "1.0",
            )),
        }

        template_str = ICP.get_param("jaeger.llm_config_template", "{}")
        try:
            template = json.loads(template_str) if template_str else {}
        except (json.JSONDecodeError, TypeError):
            template = {}
        template.update(config)
        return template

    def _create_eks_jobs(self, config, instances, k_runs, cluster, namespace):
        """Create K8s Job manifests and submit to EKS.

        Uses the kubernetes Python client to create batch/v1 Jobs
        in the configured EKS namespace.
        """
        try:
            from kubernetes import client
            from kubernetes import config as k8s_config

            k8s_config.load_kube_config(
                config_file=os.environ.get("KUBECONFIG") or None,
            )
            batch_v1 = client.BatchV1Api()
        except ImportError:
            _logger.warning("kubernetes Python client not installed. Skipping EKS dispatch.")
            self._append_log("kubernetes client not available — runs created but not dispatched to EKS")
            return
        except Exception as e:
            _logger.warning("Could not configure K8s client: %s", e)
            self._append_log(f"K8s config error: {e} — runs created but not dispatched")
            return

        ICP = self.env["ir.config_parameter"].sudo()
        agent_image = ICP.get_param("jaeger.agent_image", "jaeger-agent:latest")

        for inst in instances:
            for run_num in range(1, k_runs + 1):
                job_name = f"{self.eks_job_id}-{inst.pr_number}-r{run_num}"
                job_name = job_name.lower().replace("__", "-")[:63]

                container = client.V1Container(
                    name="agent",
                    image=agent_image,
                    env=[
                        client.V1EnvVar(name="INSTANCE_IMAGE", value=inst.docker_image_name or ""),
                        client.V1EnvVar(name="INSTANCE_ID", value=inst.name),
                        client.V1EnvVar(name="RUN_NUMBER", value=str(run_num)),
                        client.V1EnvVar(name="MODEL_NAME", value=config.get("model_name", "")),
                        client.V1EnvVar(name="TEMPERATURE", value=str(config.get("temperature", 1.0))),
                        client.V1EnvVar(name="MAX_ITERATIONS", value=str(config.get("max_iterations", 300))),
                        client.V1EnvVar(name="TIMEOUT", value=str(config.get("conversation_timeout", 3600))),
                        client.V1EnvVar(name="WEBHOOK_URL", value=ICP.get_param("jaeger.webhook_url", "")),
                        client.V1EnvVar(name="ODOO_RECORD_ID", value=str(inst.id)),
                    ],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "1", "memory": "4Gi"},
                        limits={"cpu": "2", "memory": "8Gi"},
                    ),
                )

                job = client.V1Job(
                    metadata=client.V1ObjectMeta(
                        name=job_name,
                        namespace=namespace,
                        labels={
                            "app": "jaeger-trajectory",
                            "jaeger-job-id": self.eks_job_id or "",
                            "instance": inst.name[:63],
                        },
                    ),
                    spec=client.V1JobSpec(
                        template=client.V1PodTemplateSpec(
                            spec=client.V1PodSpec(
                                containers=[container],
                                restart_policy="Never",
                            ),
                        ),
                        backoff_limit=1,
                        active_deadline_seconds=config.get("conversation_timeout", 3600) + 300,
                    ),
                )

                try:
                    batch_v1.create_namespaced_job(namespace=namespace, body=job)
                except Exception as e:
                    _logger.error("Failed to create K8s job %s: %s", job_name, e)

    def _handle_trajectory_webhook(self, status, results):
        """Handle incoming EKS webhook with trajectory run results.

        Called by the webhook controller when an EKS pod completes.

        Args:
            status: 'completed' or 'failed'
            results: dict with run data (agent_patch, conversation, costs, etc.)
        """
        import json

        pod_name = results.get("pod_name", "")
        Run = self.env["jaeger.trajectory.run"]
        run = Run.search([("eks_pod_name", "=", pod_name)], limit=1)

        if not run:
            _logger.warning("No trajectory run found for pod %s", pod_name)
            return

        if status == "completed":
            run.write({
                "status": "resolved" if results.get("resolved") else "unresolved",
                "resolved": results.get("resolved", False),
                "agent_patch": results.get("agent_patch", ""),
                "conversation_log": results.get("conversation", ""),
                "api_calls": results.get("api_calls", 0),
                "api_cost": results.get("api_cost", 0.0),
                "api_time_seconds": results.get("api_time", 0.0),
                "prompt_tokens": results.get("prompt_tokens", 0),
                "completion_tokens": results.get("completion_tokens", 0),
                "duration_seconds": results.get("duration", 0.0),
            })

            # Check for evaluation results
            if results.get("eval_report"):
                run.write({
                    "eval_status": "passed" if results.get("resolved") else "failed",
                    "eval_report_json": json.dumps(results["eval_report"]),
                    "eval_passed_count": results["eval_report"].get("passed", 0),
                    "eval_failed_count": results["eval_report"].get("failed", 0),
                })
        else:
            run.write({
                "status": "error",
                "conversation_log": results.get("error_message", "Unknown error"),
            })

        # Check if all runs for this repo are complete
        pending = self.run_ids.filtered(
            lambda r: r.status in ("queued", "running", "evaluating"),
        )
        if not pending:
            self._summarize_trajectories()

    # ── Stage 7 Actions ──────────────────────────────────────────────────

    def action_export_meta(self):
        raise UserError("Phase 2-7 not available yet. Only Phase 1 (PR Collection) is active.")
        self.ensure_one()
        if self.current_stage != "stage7":
            raise UserError("Repository must be in Stage 7.")
        self.write({"delivery_status": "converting", "error_message": False})
        from ..services.rabbitmq_service import publish_export_task

        publish_export_task(self.id)

    def action_export_meta_direct(self):
        raise UserError("Phase 2-7 not available yet. Only Phase 1 (PR Collection) is active.")
        self.ensure_one()
        if self.current_stage != "stage7":
            raise UserError("Repository must be in Stage 7.")
        if self.delivery_status in ("converting", "queued"):
            raise UserError("Meta export is already in progress.")
        return self._run_pipeline_async(
            "run_meta_export", "delivery_status", "Meta Export",
        )

    def run_meta_export(self):
        """Convert to Meta delivery schema. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"delivery_status": "converting", "error_message": False})
        self.env.cr.commit()
        try:
            self._convert_to_meta_schema()
            vals = {"delivery_status": "done", "terminal_state": "none", "error_message": False}
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            self.write(
                {
                    "delivery_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            self.env.cr.commit()
            raise

    def _convert_to_meta_schema(self):
        """Convert all valid instances to Meta delivery schema and generate JSONL."""
        import json
        from pathlib import Path

        ICP = self.env["ir.config_parameter"].sudo()
        output_dir = Path(ICP.get_param("jaeger.output_dir", "/tmp/jaeger_data"))
        out_dir = output_dir / f"{self.org}__{self.repo_name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        self._append_log("Step 1/4: Running pre-flight validation...")

        candidates = self.instance_ids.filtered(
            lambda i: i.is_valid and i.docker_build_status == "built",
        )
        if not candidates:
            self.write({
                "error_message": "No valid instances for Meta export.",
                "terminal_state": "no_valid_instances",
            })
            raise ValueError("No valid instances for Meta export")

        self._append_log(f"  {len(candidates)} instances passed pre-flight")

        self._append_log("Step 2/4: Converting instances to Meta schema...")
        from ..tools.dataset_converter import MetaSchemaConverter

        ecr_prefix = ICP.get_param("jaeger.ecr_prefix", "")
        converter = MetaSchemaConverter(
            ecr_prefix=ecr_prefix,
            task_category=self.task_category or "hard_swe",
            repo_category=f"{self.language}_{self.pipeline_mode}",
        )

        converted, errors = converter.convert_batch(candidates)

        for inst_name, error in errors:
            self._append_log(f"  FAILED {inst_name}: {error}")

        self._append_log(
            f"  {len(converted)} converted, {len(errors)} failed",
        )

        if not converted:
            raise ValueError("All Meta schema conversions failed")

        # Update individual instance records
        for inst in candidates:
            try:
                meta_json = converter.convert(inst)
                inst.write({
                    "meta_schema_json": json.dumps(meta_json, ensure_ascii=False),
                    "delivery_status": "converted",
                })
            except Exception:
                pass  # Already tracked in errors

        self._append_log("Step 3/4: Writing Meta delivery JSONL...")

        delivery_path = out_dir / f"{self.org}__{self.repo_name}_meta_delivery.jsonl"
        with open(delivery_path, "w", encoding="utf-8") as f:
            for entry in converted:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._append_log(f"  Wrote {len(converted)} entries to {delivery_path}")

        self._append_log("Step 4/4: Generating delivery summary...")

        summary = {
            "repository": f"{self.org}/{self.repo_name}",
            "total_candidates": len(candidates),
            "converted": len(converted),
            "failed": len(errors),
            "delivery_path": str(delivery_path),
        }
        summary_path = out_dir / f"{self.org}__{self.repo_name}_delivery_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        self.write({
            "meta_delivery_jsonl_path": str(delivery_path),
            "delivered_count": len(converted),
        })

        delivered = self.instance_ids.filtered(
            lambda i: i.delivery_status == "converted",
        )
        delivered.write({"delivery_status": "delivered"})

        self._append_log(
            f"Export complete: {len(converted)} instances delivered to {delivery_path}",
        )

    # ── Stage Advancement ─────────────────────────────────────────────────

    def action_advance_stage(self):
        self.ensure_one()
        if self.current_stage in ("done", "failed"):
            raise UserError("Repository is already in a terminal state.")
        gate_ok, msg = self._check_current_gate()
        if not gate_ok:
            raise UserError(f"Cannot advance: {msg}")
        next_stage = self._next_stage()
        if next_stage:
            self.write({"current_stage": next_stage})

    def _check_current_gate(self):
        stage = self.current_stage
        if self.terminal_state != "none":
            return False, f"Terminal state: {self.terminal_state}"

        if stage == "stage1":
            if self.crawl_status != "done":
                return False, "Repository validation not complete"
        elif stage == "stage2":
            if self.pr_collection_status != "done":
                return False, "PR collection not complete"
            if not self.instance_ids:
                return False, "No instances created"
        elif stage == "stage3":
            if self.docker_build_status != "done":
                return False, "Docker build not complete"
            if self.images_built_count == 0:
                return False, "No images built"
        elif stage == "stage4":
            if self.test_execution_status != "done":
                return False, "Test execution not complete"
        elif stage == "stage5":
            if self.dataset_status != "done":
                return False, "Dataset finalization not complete"
            if self.final_dataset_count == 0:
                return False, "No valid instances in final dataset"
        elif stage == "stage6":
            if self.trajectory_status != "done":
                return False, "Trajectory generation not complete"
        elif stage == "stage7":
            if self.delivery_status != "done":
                return False, "Delivery export not complete"

        return True, ""

    def _next_stage(self):
        mapping = {
            "stage1": "stage2",
            "stage2": "stage3",
            "stage3": "stage4",
            "stage4": "stage5",
            "stage5": "stage6",
            "stage6": "stage7",
            "stage7": "done",
        }
        return mapping.get(self.current_stage)

    # ── Cron Jobs ─────────────────────────────────────────────────────────

    @api.model
    def _cron_batch_scrape(self):
        pending = self.search(
            [
                ("pr_collection_status", "=", "pending"),
                ("current_stage", "=", "stage2"),
            ],
            limit=500,
        )
        if not pending:
            return
        from ..services.rabbitmq_service import batch_publish_scrape_tasks

        batch_publish_scrape_tasks(pending.ids)
        pending.write({"pr_collection_status": "queued"})
        self.env["ir.cron"]._commit_progress(
            processed=len(pending),
            remaining=self.search_count(
                [
                    ("pr_collection_status", "=", "pending"),
                    ("current_stage", "=", "stage2"),
                ],
            ),
        )

    @api.model
    def _cron_batch_docker(self):
        pending = self.search(
            [
                ("docker_build_status", "=", "pending"),
                ("current_stage", "=", "stage3"),
            ],
            limit=200,
        )
        if not pending:
            return
        from ..services.rabbitmq_service import batch_publish_docker_tasks

        batch_publish_docker_tasks(pending.ids)
        pending.write({"docker_build_status": "queued"})
        self.env["ir.cron"]._commit_progress(
            processed=len(pending),
            remaining=self.search_count(
                [
                    ("docker_build_status", "=", "pending"),
                    ("current_stage", "=", "stage3"),
                ],
            ),
        )

    @api.model
    def _cron_poll_eks_trajectories(self):
        running = self.search(
            [
                (
                    "trajectory_status",
                    "in",
                    ("dispatched", "running", "evaluating"),
                ),
            ],
        )
        for repo in running:
            try:
                repo._poll_eks_status()
            except Exception as e:
                _logger.error("EKS poll error for %s: %s", repo.name, e)

    def _poll_eks_status(self):
        """Poll EKS for trajectory job status and update trajectory runs."""

        if not self.eks_job_id:
            return

        _logger.info("Polling EKS status for %s (job=%s)", self.name, self.eks_job_id)

        try:
            from kubernetes import client
            from kubernetes import config as k8s_config

            k8s_config.load_kube_config(
                config_file=os.environ.get("KUBECONFIG") or None,
            )
            batch_v1 = client.BatchV1Api()
        except (ImportError, Exception) as e:
            _logger.warning("Cannot connect to K8s for polling: %s", e)
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

        try:
            jobs = batch_v1.list_namespaced_job(
                namespace=namespace,
                label_selector=f"jaeger-job-id={self.eks_job_id}",
            )
        except Exception as e:
            _logger.error("Failed to list K8s jobs for %s: %s", self.eks_job_id, e)
            return

        Run = self.env["jaeger.trajectory.run"]
        completed_count = 0
        failed_count = 0
        running_count = 0

        for job in jobs.items:
            pod_name = job.metadata.name
            run = Run.search([("eks_pod_name", "=", pod_name)], limit=1)
            if not run:
                continue

            if job.status.succeeded and job.status.succeeded > 0:
                if run.status != "resolved":
                    run.write({"status": "resolved"})
                completed_count += 1
            elif job.status.failed and job.status.failed > 0:
                if run.status != "error":
                    run.write({"status": "error"})
                failed_count += 1
            elif job.status.active and job.status.active > 0:
                if run.status not in ("running", "evaluating"):
                    run.write({"status": "running"})
                running_count += 1

        total_runs = len(self.run_ids)
        done = completed_count + failed_count

        _logger.info(
            "EKS poll for %s: %d running, %d completed, %d failed, %d total",
            self.name, running_count, completed_count, failed_count, total_runs,
        )

        # Check if all runs are done
        if total_runs > 0 and done >= total_runs:
            self._summarize_trajectories()

    def _summarize_trajectories(self):
        """Summarize trajectory results and compute pass@k."""
        import json

        self._append_log("All trajectory runs complete. Computing pass@k...")

        runs = self.run_ids
        total_runs = len(runs)
        resolved_runs = len(runs.filtered(lambda r: r.resolved))

        # Compute pass@k per instance
        instance_results = {}
        for run in runs:
            inst_name = run.instance_id.name
            if inst_name not in instance_results:
                instance_results[inst_name] = {"total": 0, "resolved": 0}
            instance_results[inst_name]["total"] += 1
            if run.resolved:
                instance_results[inst_name]["resolved"] += 1

        # pass@k = 1 - C(n-c, k) / C(n, k) where n=total, c=correct, k=k_runs
        k = self.k_runs or 8
        pass_at_k_values = []
        for inst_name, counts in instance_results.items():
            n = counts["total"]
            c = counts["resolved"]
            if n == 0:
                pass_at_k_values.append(0.0)
            elif c >= k:
                pass_at_k_values.append(1.0)
            else:
                # pass@k = 1 - prod((n-c-i)/(n-i) for i in range(k))
                numerator = 1.0
                for i in range(k):
                    if n - i == 0:
                        break
                    numerator *= (n - c - i) / (n - i)
                pass_at_k_values.append(1.0 - numerator)

        avg_pass_at_k = sum(pass_at_k_values) / max(len(pass_at_k_values), 1)

        # Aggregate costs
        total_cost = sum(r.api_cost or 0 for r in runs)
        total_calls = sum(r.api_calls or 0 for r in runs)
        total_prompt = sum(r.prompt_tokens or 0 for r in runs)
        total_completion = sum(r.completion_tokens or 0 for r in runs)

        summary = {
            "total_instances": len(instance_results),
            "total_runs": total_runs,
            "resolved_runs": resolved_runs,
            "pass_at_k": round(avg_pass_at_k, 4),
            "k": k,
            "per_instance": {
                name: {
                    "pass_at_k": round(pass_at_k_values[idx], 4),
                    **counts,
                }
                for idx, (name, counts) in enumerate(instance_results.items())
            },
            "total_cost": round(total_cost, 2),
            "total_api_calls": total_calls,
        }

        self.write({
            "trajectory_status": "done",
            "pass_at_k": avg_pass_at_k,
            "pass_at_k_summary_json": json.dumps(summary, indent=2),
            "total_api_cost": total_cost,
            "total_api_calls": total_calls,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
        })

        self._append_log(
            f"Trajectory summary: pass@{k} = {avg_pass_at_k:.4f}, "
            f"{resolved_runs}/{total_runs} resolved, cost=${total_cost:.2f}",
        )

    @api.model
    def _cron_auto_advance_stages(self):
        """Check gate conditions and auto-advance repos through stages."""
        for stage in [
            "stage1",
            "stage2",
            "stage3",
            "stage4",
            "stage5",
            "stage6",
            "stage7",
        ]:
            repos = self.search(
                [("current_stage", "=", stage), ("terminal_state", "=", "none")],
            )
            for repo in repos:
                gate_ok, _ = repo._check_current_gate()
                if gate_ok:
                    next_stage = repo._next_stage()
                    if next_stage:
                        repo.write({"current_stage": next_stage})
                        _logger.info(
                            "Auto-advanced %s from %s to %s",
                            repo.name,
                            stage,
                            next_stage,
                        )
                self.env["ir.cron"]._commit_progress(processed=1)

    @api.model
    def _cron_watchdog_stale_scrapes(self):
        """Mark repos stuck in 'running' with no heartbeat for 60+ min as failed."""
        from datetime import timedelta

        cutoff = fields.Datetime.now() - timedelta(minutes=60)
        stale = self.search([
            ("pr_collection_status", "=", "running"),
            "|",
            ("last_heartbeat", "=", False),
            ("last_heartbeat", "<", cutoff),
        ])
        for repo in stale:
            _logger.warning(
                "Watchdog: marking %s as failed (last heartbeat %s)",
                repo.name, repo.last_heartbeat,
            )
            repo.write({
                "pr_collection_status": "failed",
                "error_message": "Watchdog: pipeline appears stuck (no heartbeat for 60+ minutes).",
            })
        if stale:
            _logger.info("Watchdog: marked %d stale scrape jobs as failed", len(stale))

    @api.model
    def _cron_watchdog_stale_builds(self):
        """Reset repos stuck in 'building' for 2+ hours back to pending."""
        from datetime import timedelta

        cutoff = fields.Datetime.now() - timedelta(hours=2)
        stale = self.search([
            ("docker_build_status", "=", "building"),
            ("write_date", "<", cutoff),
        ])
        for repo in stale:
            _logger.warning(
                "Watchdog: resetting stuck build for %s (last write %s)",
                repo.name, repo.write_date,
            )
            stuck_instances = repo.instance_ids.filtered(
                lambda i: i.docker_build_status == "building",
            )
            stuck_instances.write({"docker_build_status": "pending"})
            repo.write({
                "docker_build_status": "pending",
                "error_message": "Watchdog: build appeared stuck for 2+ hours, reset to pending.",
            })
        if stale:
            _logger.info("Watchdog: reset %d stuck builds to pending", len(stale))

    @api.model
    def _cron_reconcile_scrape_jobs(self):
        """Check K8s Job status for running scrape pipelines (kaiju_build pattern).

        Safety net for pods that crash without updating the database
        (OOM kill, node failure, network partition).
        """
        running = self.search([("pr_collection_status", "=", "running")])
        if not running:
            return

        try:
            from kubernetes import client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config(
                    config_file=os.environ.get("KUBECONFIG") or None,
                )
            batch_v1 = client.BatchV1Api()
        except ImportError:
            return
        except Exception as e:
            _logger.warning("K8s config not available for reconciliation: %s", e)
            return

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

        for repo in running:
            job_name = f"jaeger-scrape-{repo.id}"
            try:
                job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
            except Exception:
                continue

            if job.status.succeeded and job.status.succeeded > 0:
                if repo.pr_collection_status != "done":
                    repo.write({"pr_collection_status": "done"})
                    _logger.info("Reconcile: %s marked done (K8s Job succeeded)", repo.name)
            elif job.status.failed and job.status.failed > 0:
                logs = ""
                try:
                    core_v1 = client.CoreV1Api()
                    pods = core_v1.list_namespaced_pod(
                        namespace=namespace,
                        label_selector=f"job-name={job_name}",
                    )
                    if pods.items:
                        pod_name = pods.items[-1].metadata.name
                        logs = core_v1.read_namespaced_pod_log(
                            name=pod_name, namespace=namespace, tail_lines=50,
                        )
                except Exception:
                    logs = "Could not retrieve pod logs"

                repo.write({
                    "pr_collection_status": "failed",
                    "error_message": f"K8s Job failed.\n{logs}"[:2000],
                })
                _logger.warning("Reconcile: %s marked failed (K8s Job failed)", repo.name)
