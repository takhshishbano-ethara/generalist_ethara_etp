#!/usr/bin/env python3
"""Aurora Pipeline Worker — K8s Job entrypoint.

Env vars: PIPELINE_ID (int), ODOO_DB (str), ODOO_CONF (path, optional).

Boots a headless Odoo registry, leases tokens, runs 6 steps, writes
progress via HTTP webhook + aurora_pipeline.log.  Every DB operation uses a
short-lived cursor (open-write-commit-close).  SIGTERM triggers graceful
stop between steps.
"""

import hashlib
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [aurora-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger("aurora.worker")

_cancelled = False


def _sigterm_handler(signum, frame):
    global _cancelled
    _cancelled = True
    _logger.warning("Received SIGTERM — will stop after current step.")


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _sigterm_handler)


def _check_cancelled():
    if _cancelled:
        raise PipelineCancelled("Pipeline cancelled (SIGTERM received)")


class PipelineCancelled(Exception):
    pass


def _boot_odoo(db_name: str, conf_path: Optional[str] = None):
    conf_path = conf_path or os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf")
    sys.argv = ["odoo", "--no-http", f"--config={conf_path}", f"--database={db_name}"]

    from odoo import init as _odoo_init  # noqa: F401 — triggers Odoo 19 lazy bootstrap
    import odoo
    odoo.tools.config.parse_config(sys.argv[1:])

    # Override DB credentials from K8s Secret env vars
    _DB_ENV_OVERRIDES = {
        "DB_HOST": "db_host",
        "DB_PORT": "db_port",
        "DB_USER": "db_user",
        "DB_PASSWORD": "db_password",
    }
    for env_key, conf_key in _DB_ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val:
            odoo.tools.config[conf_key] = val

    from odoo.modules.registry import Registry
    registry = Registry(db_name)
    _logger.info("Odoo registry booted for db=%s", db_name)
    return registry


# ---------------------------------------------------------------------------
# Shared DB helpers — imported from pipeline_executor (single source of truth)
# ---------------------------------------------------------------------------
# _update_pipeline, _append_log, _heartbeat, _fail_pipeline receive a raw
# cursor and are imported from the shared module.  _count_jsonl_lines and
# _validate_step_output are pure-Python helpers also shared.
# ---------------------------------------------------------------------------

def _lazy_import_executor():
    from odoo.addons.aurora.models.pipeline_executor import (
        _update_pipeline,
        _append_log,
        _append_step_log,
        _heartbeat,
        _fail_pipeline,
        _count_jsonl_lines,
        _validate_step_output,
    )
    return (
        _update_pipeline,
        _append_log,
        _append_step_log,
        _heartbeat,
        _fail_pipeline,
        _count_jsonl_lines,
        _validate_step_output,
    )


_update_pipeline = None
_append_log = None
_append_step_log = None
_heartbeat = None
_fail_pipeline = None
_count_jsonl_lines = None
_validate_step_output = None


def _init_shared_functions():
    global _update_pipeline, _append_log, _append_step_log, _heartbeat, _fail_pipeline
    global _count_jsonl_lines, _validate_step_output
    (
        _update_pipeline,
        _append_log,
        _append_step_log,
        _heartbeat,
        _fail_pipeline,
        _count_jsonl_lines,
        _validate_step_output,
    ) = _lazy_import_executor()


# 40001 = serialization_failure, 40P01 = deadlock_detected,
# 08006 = connection_failure, 08001 = sqlclient_unable_to_establish_sqlconnection
_TRANSIENT_PG_CODES = frozenset({"40001", "40P01", "08006", "08001"})
_DB_WRITE_MAX_RETRIES = 3
_DB_WRITE_RETRY_BASE_DELAY = 0.5


def _is_transient_db_error(exc: Exception) -> bool:
    pgcode = getattr(exc, "pgcode", None)
    if pgcode and pgcode in _TRANSIENT_PG_CODES:
        return True
    exc_name = type(exc).__name__
    if exc_name == "OperationalError" and pgcode is None:
        return True
    return False


def _open_cursor(registry):
    return registry.cursor()


def _notify_webhook(registry, db_name: str, rec_id: int, stage: str, progress_text: Optional[str] = None) -> None:
    import hashlib
    import hmac
    import json
    import os
    import time
    import requests
    webhook_url = os.environ.get("AURORA_WEBHOOK_URL", "")
    webhook_secret = os.environ.get("AURORA_WEBHOOK_SECRET", "")
    if not webhook_url or not webhook_secret:
        return
    jsonrpc_body = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "pipeline_id": rec_id,
            "stage": stage,
            "progress_text": progress_text or "",
        },
    }
    raw_body = json.dumps(jsonrpc_body).encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac.new(
        webhook_secret.encode("utf-8"),
        msg=f"{ts}.".encode("utf-8") + raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    try:
        requests.post(
            webhook_url.rstrip("/") + "/aurora/webhook/pipeline",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Aurora-Timestamp": ts,
                "X-Aurora-Signature": sig,
                "X-Aurora-Token": webhook_secret,
            },
            timeout=30,
        )
    except Exception:
        _logger.debug("webhook notification failed for rec=%s", rec_id, exc_info=True)


def _post_chatter(registry, uid: Optional[int], rec_id: int, body: str) -> None:
    cr = None
    try:
        from odoo import api, SUPERUSER_ID
        cr = _open_cursor(registry)
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        rec = env["aurora.pipeline"].browse(rec_id)
        rec.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")
        cr.commit()
    except Exception:
        _logger.exception("Failed to post chatter for rec=%s", rec_id)
    finally:
        if cr:
            cr.close()


def _create_phase2_results(registry, rec_id: int, results: list[dict]) -> None:
    """Create aurora.pipeline.result records from Phase 2 output."""
    cr = None
    try:
        from odoo import api, SUPERUSER_ID
        cr = _open_cursor(registry)
        env = api.Environment(cr, SUPERUSER_ID, {})
        Result = env["aurora.pipeline.result"]

        Result.search([("pipeline_id", "=", rec_id)]).unlink()

        for idx, r in enumerate(results):
            f2p = r.get("f2p", [])
            p2p = r.get("p2p", [])
            s2p = r.get("s2p", [])
            n2p = r.get("n2p", [])
            fixed = r.get("fixed_tests", [])

            Result.create({
                "pipeline_id": rec_id,
                "sequence": idx + 1,
                "instance_id": r.get("instance_id", ""),
                "tag_start": r.get("tag_start", ""),
                "tag_end": r.get("tag_end", ""),
                "pr_numbers": r.get("pr_numbers", ""),
                "pr_attribution_method": r.get("pr_attribution_method") or False,
                "version_scheme": r.get("version_scheme") or False,
                "valid": r.get("valid", False),
                "f2p_count": len(f2p),
                "p2p_count": len(p2p),
                "s2p_count": len(s2p),
                "n2p_count": len(n2p),
                "fixed_count": len(fixed),
                "run_passed": r.get("run_passed", 0),
                "run_failed": r.get("run_failed", 0),
                "run_skipped": r.get("run_skipped", 0),
                "test_passed": r.get("test_passed", 0),
                "test_failed": r.get("test_failed", 0),
                "test_skipped": r.get("test_skipped", 0),
                "fix_passed": r.get("fix_passed", 0),
                "fix_failed": r.get("fix_failed", 0),
                "fix_skipped": r.get("fix_skipped", 0),
                "f2p_tests": "\n".join(f2p) if f2p else "",
                "p2p_tests": "\n".join(p2p) if p2p else "",
                "s2p_tests": "\n".join(s2p) if s2p else "",
                "n2p_tests": "\n".join(n2p) if n2p else "",
                "fixed_tests": "\n".join(fixed) if fixed else "",
                "error_msg": r.get("error_msg") or r.get("error", ""),
            })

        cr.commit()
        _logger.info("Created %d Phase 2 result records for pipeline %d", len(results), rec_id)
    except Exception:
        _logger.exception("Failed to create Phase 2 result records for rec=%s", rec_id)
        if cr:
            try:
                cr.rollback()
            except Exception:
                pass
    finally:
        if cr:
            cr.close()


def _read_config(registry, rec_id: int) -> dict[str, Any]:
    from odoo import api, SUPERUSER_ID
    from odoo.addons.aurora.models.credential_manager import get_encrypted_param_raw
    from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
    cr = _open_cursor(registry)
    try:
        env = api.Environment(cr, SUPERUSER_ID, {})
        pipeline = env["aurora.pipeline"].browse(rec_id)
        if not pipeline.exists():
            raise AuroraPipelineError(f"Pipeline record {rec_id} not found")

        ICP = env["ir.config_parameter"].sudo()
        return {
            "org": pipeline.github_org,
            "repo": pipeline.github_repo,
            "output_dir": pipeline.output_dir,
            "skip_pr_fetch": pipeline.skip_pr_fetch,
            "lang": pipeline.detected_lang or ICP.get_param("aurora.lang", "python"),
            "delay_on_error": int(ICP.get_param("aurora.delay_on_error", "300")),
            "retry_attempts": int(ICP.get_param("aurora.retry_attempts", "3")),
            "max_tags": int(ICP.get_param("aurora.max_tags", "200")),
            "window_days": int(ICP.get_param("aurora.window_days", "30")),
            "cache_dir": ICP.get_param("aurora.cache_dir", "/data/repo_cache"),
            "s3_bucket": ICP.get_param("aurora.s3_bucket", ""),
            "s3_access_key": get_encrypted_param_raw(cr, "aurora.s3_access_key"),
            "s3_secret_key": get_encrypted_param_raw(cr, "aurora.s3_secret_key"),
            "s3_region": ICP.get_param("aurora.s3_region", "ap-south-1"),
            "s3_folder": ICP.get_param("aurora.s3_folder", ""),
            "uid": pipeline.user_id.id or SUPERUSER_ID,
        }
    finally:
        cr.close()


def _lease_tokens(registry, rec_id: int, count: int = 3) -> list[str]:
    from odoo.addons.aurora.models.github_token import AuroraGithubToken
    cr = _open_cursor(registry)
    try:
        tokens = AuroraGithubToken.lease_tokens(cr, rec_id, count=count)
        cr.commit()
        return tokens
    finally:
        cr.close()


def _release_tokens(registry, rec_id: int, token_summaries: Optional[dict] = None) -> None:
    from odoo.addons.aurora.models.github_token import AuroraGithubToken
    cr = _open_cursor(registry)
    try:
        AuroraGithubToken.release_tokens(cr, rec_id, token_summaries)
        cr.commit()
    except Exception:
        _logger.exception("Failed to release tokens for rec=%s", rec_id)
    finally:
        cr.close()


def _heartbeat_rate_limits(registry, rec_id: int, tokens: list[str]) -> None:
    import requests
    from odoo.addons.aurora.models.github_token import AuroraGithubToken

    summaries = {}
    for tok in tokens:
        try:
            resp = requests.get(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=10,
            )
            if resp.status_code == 200:
                core = resp.json().get("resources", {}).get("core", {})
                tok_hash = hashlib.sha256(tok.encode()).hexdigest()
                summaries[tok_hash] = {
                    "remaining": core.get("remaining", 0),
                    "reset": core.get("reset"),
                }
        except Exception:
            _logger.debug("Rate limit probe failed for a token", exc_info=True)

    if summaries:
        cr = _open_cursor(registry)
        try:
            AuroraGithubToken.heartbeat_rate_limits(cr, rec_id, summaries)
            cr.commit()
        finally:
            cr.close()


def _build_s3_config(cfg: dict) -> dict:
    return {
        "bucket": cfg["s3_bucket"],
        "access_key": cfg["s3_access_key"],
        "secret_key": cfg["s3_secret_key"],
        "region": cfg["s3_region"],
    }


def run_pipeline(registry, db_name: str, rec_id: int):
    if _update_pipeline is None:
        _init_shared_functions()

    _docker_bin = "/Applications/Docker.app/Contents/Resources/bin"
    if os.path.isdir(_docker_bin) and _docker_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _docker_bin + ":" + os.environ.get("PATH", "")

    from odoo.addons.aurora.tools.collect.get_all_prs import main as fetch_all_prs
    from odoo.addons.aurora.tools.collect.filter_prs import main as filter_prs
    from odoo.addons.aurora.tools.collect.get_version_tags import main as get_version_tags
    from odoo.addons.aurora.tools.collect.group_prs_by_tags import main as group_prs_by_tags
    from odoo.addons.aurora.tools.collect.get_related_issues import main as get_related_issues
    from odoo.addons.aurora.tools.collect.build_dataset import main as build_dataset
    from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
    from odoo.addons.aurora.models import s3_storage

    tokens = None
    temp_dir = None
    cfg = None

    def _db_write(fn, *args, **kwargs):
        last_exc = None
        for attempt in range(_DB_WRITE_MAX_RETRIES):
            cr = _open_cursor(registry)
            try:
                fn(cr, *args, **kwargs)
                cr.commit()
                return
            except Exception as exc:
                last_exc = exc
                try:
                    cr.rollback()
                except Exception:
                    pass
                if _is_transient_db_error(exc) and attempt < _DB_WRITE_MAX_RETRIES - 1:
                    delay = _DB_WRITE_RETRY_BASE_DELAY * (2 ** attempt)
                    _logger.warning(
                        "Transient DB error (fn=%s, attempt=%d/%d): %s — retrying in %.1fs",
                        fn.__name__, attempt + 1, _DB_WRITE_MAX_RETRIES, exc, delay,
                    )
                    time.sleep(delay)
                    continue
                _logger.exception("DB write failed (fn=%s, attempt=%d/%d)", fn.__name__, attempt + 1, _DB_WRITE_MAX_RETRIES)
                raise
            finally:
                cr.close()
        raise last_exc

    def _log(msg):
        _logger.info("[Pipeline %d] %s", rec_id, msg)
        _db_write(_append_log, rec_id, msg)

    def _beat(progress_text=None):
        _db_write(_heartbeat, rec_id, progress_text)

    def _bus(stage, text=None):
            _notify_webhook(registry, db_name, rec_id, stage, text)

    try:
        cfg = _read_config(registry, rec_id)
        uid = cfg.pop("uid")
        org = cfg["org"]
        repo = cfg["repo"]
        prefix = f"{org}__{repo}"

        _logger.info("Pipeline %d starting: %s/%s", rec_id, org, repo)

        tokens = _lease_tokens(registry, rec_id, count=3)
        if not tokens:
            raise AuroraPipelineError(
                "No GitHub tokens available. Import tokens via Configuration -> Import Tokens."
            )
        _logger.info("Leased %d token(s) for pipeline %d", len(tokens), rec_id)

        s3_config = _build_s3_config(cfg)
        use_s3 = s3_storage.is_configured(s3_config)

        if use_s3:
            temp_dir = tempfile.mkdtemp(prefix="aurora_")
            out = Path(temp_dir)
            run_number = s3_storage.get_next_run_number(
                s3_config, org, repo, cfg.get("s3_folder", ""),
            )
        else:
            out = Path(cfg["output_dir"])
            os.makedirs(str(out), exist_ok=True)
            run_number = None

        step1_file = out / f"{prefix}_prs.jsonl"
        step2_file = out / f"{prefix}_lht_filtered_prs.jsonl"
        step3_file = out / f"{prefix}_tags.jsonl"
        step4_file = out / f"{prefix}_tag_groups.jsonl"
        step5_file = out / f"{prefix}_related_issues.jsonl"
        step6_file = out / f"{prefix}_lht_dataset.jsonl"

        s3_folder = cfg.get("s3_folder", "")

        def _upload_to_s3(local_path, step_num):
            if not use_s3:
                return str(local_path)
            fname = os.path.basename(str(local_path))
            s3_key = s3_storage.build_s3_key(org, repo, run_number, fname, s3_folder)
            url = s3_storage.upload_file(s3_config, str(local_path), s3_key)
            _log(f"Step {step_num}: uploaded to S3 -> {s3_key}")
            return url

        def _run_step(step_num, step_field, stage_val, label, fn, count_file):
            _check_cancelled()
            _db_write(_update_pipeline, rec_id, {
                step_field: "running",
                "stage": stage_val,
                f"step{step_num}_log": "",
            })
            _log(f"Step {step_num}: {label} ...")
            _db_write(_append_step_log, rec_id, step_num, f"{label} ...")
            _beat(f"Step {step_num}: {label}")

            stop_heartbeat = threading.Event()

            def _heartbeat_loop():
                while not stop_heartbeat.wait(timeout=120):
                    try:
                        _beat(f"Step {step_num}: {label} (running)")
                    except Exception:
                        _logger.debug("Intra-step heartbeat failed", exc_info=True)

            hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
            hb_thread.start()

            class _StepLogHandler(logging.Handler):
                def __init__(self):
                    super().__init__(level=logging.INFO)
                    self._last_msg = None

                def emit(self, record):
                    try:
                        msg = self.format(record)
                    except Exception:
                        return
                    if not msg or msg == self._last_msg:
                        return
                    self._last_msg = msg
                    try:
                        _db_write(_append_step_log, rec_id, step_num, msg)
                    except Exception:
                        pass

            step_log_handler = _StepLogHandler()
            step_log_handler.setFormatter(logging.Formatter("%(message)s"))
            tools_logger = logging.getLogger("odoo.addons.aurora.tools")
            tools_logger.addHandler(step_log_handler)

            try:
                fn()
                _check_cancelled()
                _validate_step_output(str(count_file), step_num)
                count = _count_jsonl_lines(str(count_file))
                file_ref = _upload_to_s3(count_file, step_num)
                _db_write(_update_pipeline, rec_id, {
                    step_field: "done",
                    f"step{step_num}_file": file_ref,
                    "progress_text": f"Step {step_num} done",
                })
                _db_write(_append_step_log, rec_id, step_num, f"Done — {count} records.")
                _log(f"Step {step_num} done - {count} records.")
                _beat(f"Step {step_num} complete ({count} records)")
                _bus(stage_val, f"Step {step_num} done")
                return count
            except PipelineCancelled:
                raise
            except Exception as exc:
                _db_write(_append_step_log, rec_id, step_num, f"FAILED: {exc}")
                _db_write(_fail_pipeline, rec_id, step_field, exc)
                _bus(stage_val, f"Step {step_num} failed: {exc}")
                return None
            finally:
                tools_logger.removeHandler(step_log_handler)
                stop_heartbeat.set()
                hb_thread.join(timeout=5)

        def _rate_limit_heartbeat():
            try:
                _heartbeat_rate_limits(registry, rec_id, tokens)
            except Exception:
                _logger.debug("Rate limit heartbeat failed", exc_info=True)

        _db_write(_update_pipeline, rec_id, {"phase1_status": "running"})

        if not cfg["skip_pr_fetch"]:
            result = _run_step(
                1, "step1_status", "fetch_prs", "Fetching all PRs",
                lambda: fetch_all_prs(tokens, out, org, repo),
                step1_file,
            )
            if result is None:
                return
            _db_write(_update_pipeline, rec_id, {"pr_count": result})
        else:
            _db_write(_update_pipeline, rec_id, {"step1_status": "done", "step1_file": str(step1_file)})
            _log("Step 1: Skipped (re-using existing data).")
            _beat("Step 1 skipped")

        _rate_limit_heartbeat()

        result = _run_step(
            2, "step2_status", "filter_prs", "Filtering PRs",
            lambda: filter_prs(tokens, out, step1_file, skip_commit_message=True),
            step2_file,
        )
        if result is None:
            return
        _db_write(_update_pipeline, rec_id, {"filtered_pr_count": result})

        def _make_progress_cb(step_num):
            last_pct = [-1]
            def _cb(done, total):
                try:
                    pct = int((done * 100) / total) if total else 0
                except Exception:
                    pct = 0
                if pct == last_pct[0]:
                    return
                last_pct[0] = pct
                msg = f"progress {done}/{total} ({pct}%)"
                try:
                    _db_write(_append_step_log, rec_id, step_num, msg)
                except Exception:
                    pass
                try:
                    _beat(f"Step {step_num}: {done}/{total}")
                except Exception:
                    pass
            return _cb

        _rate_limit_heartbeat()

        result = _run_step(
            3, "step3_status", "discover_tags", "Discovering version tags",
            lambda: get_version_tags(
                tokens, out, org, repo,
                max_tags=cfg["max_tags"],
                progress_callback=_make_progress_cb(3),
            ),
            step3_file,
        )
        if result is None:
            return
        _db_write(_update_pipeline, rec_id, {"tag_count": result})

        _rate_limit_heartbeat()

        result = _run_step(
            4, "step4_status", "group_prs", "Grouping PRs by tag pairs",
            lambda: group_prs_by_tags(
                tokens, out, org, repo,
                window_days=cfg["window_days"],
                cache_dir=cfg["cache_dir"],
            ),
            step4_file,
        )
        if result is None:
            return
        _db_write(_update_pipeline, rec_id, {"group_count": result})

        _rate_limit_heartbeat()

        result = _run_step(
            5, "step5_status", "fetch_issues", "Fetching related issues",
            lambda: get_related_issues(
                tokens, out, step2_file,
                progress_callback=_make_progress_cb(5),
            ),
            step5_file,
        )
        if result is None:
            return
        _db_write(_update_pipeline, rec_id, {"issue_count": result})

        _rate_limit_heartbeat()

        result = _run_step(
            6, "step6_status", "build_dataset", "Building final dataset",
            lambda: build_dataset(
                tokens, out, org, repo,
                delay_on_error=cfg["delay_on_error"],
                retry_attempts=cfg["retry_attempts"],
                cache_dir=cfg["cache_dir"],
                lang=cfg["lang"],
                progress_callback=_make_progress_cb(6),
            ),
            step6_file,
        )
        if result is None:
            return
        _db_write(_update_pipeline, rec_id, {"dataset_count": result})

        dataset_fname = None
        dataset_url = None
        if os.path.isfile(step6_file):
            dataset_fname = os.path.basename(str(step6_file))
            if use_s3:
                s3_key = s3_storage.build_s3_key(org, repo, run_number, dataset_fname, s3_folder)
                dataset_url = f"https://{s3_config['bucket']}.s3.{s3_config['region']}.amazonaws.com/{s3_key}"
            else:
                dataset_url = f"file://{step6_file}"

        _db_write(_update_pipeline, rec_id, {
            "dataset_filename": dataset_fname,
            "dataset_url": dataset_url,
            "phase1_status": "done",
            "phase1_file": dataset_url or str(step6_file),
            "progress_text": "Phase 1 complete",
        })
        _log("Phase 1 (Data Collection) complete.")
        _bus("build_dataset", "Phase 1 complete")

        _check_cancelled()

        _db_write(_update_pipeline, rec_id, {
            "stage": "done",
            "progress_text": "Pipeline complete",
        })
        _log("Pipeline complete.")
        _bus("done", "Pipeline complete")
        _post_chatter(
            registry, uid, rec_id,
            f"Pipeline completed - {org}/{repo} ({_count_jsonl_lines(str(step6_file))} dataset records)",
        )
        _logger.info("Pipeline %d finished successfully", rec_id)

    except PipelineCancelled:
        _logger.info("Pipeline %d stopped (SIGTERM)", rec_id)
        try:
            _db_write(_update_pipeline, rec_id, {"stage": "failed", "progress_text": "Stopped"})
            _db_write(_append_log, rec_id, "Pipeline stopped (received SIGTERM — cancelled or timed out).")
            _notify_webhook(registry, db_name, rec_id, "failed", "Stopped")
            if cfg:
                _post_chatter(registry, cfg.get("uid"), rec_id, "Pipeline stopped (SIGTERM received).")
        except Exception:
            _logger.exception("Failed to record cancellation for rec=%s", rec_id)

    except Exception:
        _logger.exception("Pipeline %d fatal error", rec_id)
        try:
            err_msg = traceback.format_exc()[-500:]
            _db_write(_update_pipeline, rec_id, {"stage": "failed", "progress_text": "Fatal error"})
            _db_write(_append_log, rec_id, f"FATAL: {err_msg}")
            _notify_webhook(registry, db_name, rec_id, "failed", err_msg[:200])
            if cfg:
                _post_chatter(registry, cfg.get("uid"), rec_id, f"Pipeline failed:\n{err_msg}")
        except Exception:
            _logger.exception("Failed to record pipeline error for rec=%s", rec_id)

    finally:
        if tokens:
            _release_tokens(registry, rec_id)
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    pipeline_id = os.environ.get("PIPELINE_ID")
    db_name = os.environ.get("ODOO_DB")
    conf_path = os.environ.get("ODOO_CONF")

    if not pipeline_id:
        _logger.error("PIPELINE_ID environment variable is required")
        sys.exit(1)
    if not db_name:
        _logger.error("ODOO_DB environment variable is required")
        sys.exit(1)

    try:
        pipeline_id = int(pipeline_id)
    except ValueError:
        _logger.error("PIPELINE_ID must be an integer, got: %s", pipeline_id)
        sys.exit(1)

    _logger.info("Worker starting: pipeline_id=%d, db=%s", pipeline_id, db_name)

    try:
        registry = _boot_odoo(db_name, conf_path)
    except Exception:
        _logger.exception("Failed to boot Odoo registry")
        sys.exit(2)

    _init_shared_functions()

    try:
        run_pipeline(registry, db_name, pipeline_id)
    except Exception:
        _logger.exception("Unhandled exception in worker")
        sys.exit(3)

    _logger.info("Worker exiting cleanly")
    sys.exit(0)


if __name__ == "__main__":
    main()
