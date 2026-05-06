#!/usr/bin/env python3
"""Aurora Evaluation Worker — K8s Job entrypoint for Phase 2.

Env vars: EVALUATION_ID (int), ODOO_DB (str), ODOO_CONF (path, optional).

Boots a headless Odoo registry, reads the evaluation record, then runs
the full evaluation pipeline (Docker image build + instance test execution +
report generation).  Every DB operation uses a short-lived cursor.
SIGTERM triggers graceful stop between stages.
"""

import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [aurora-eval-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger("aurora.eval_worker")

_cancelled = False


def _sigterm_handler(signum, frame):
    global _cancelled
    _cancelled = True
    _logger.warning("Received SIGTERM — will stop after current stage.")


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _sigterm_handler)


def _check_cancelled():
    if _cancelled:
        raise EvalCancelled("Evaluation cancelled (SIGTERM received)")


class EvalCancelled(Exception):
    pass


def _boot_odoo(db_name: str, conf_path: Optional[str] = None):
    """Boot headless Odoo registry — same pattern as run_pipeline.py."""
    conf_path = conf_path or os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf")
    sys.argv = ["odoo", "--no-http", f"--config={conf_path}", f"--database={db_name}"]

    from odoo import init as _odoo_init  # noqa: F401
    import odoo
    odoo.tools.config.parse_config(sys.argv[1:])

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


def _open_cursor(db_name: str):
    import psycopg2
    from odoo.tools import config as odoo_config
    return psycopg2.connect(
        dbname=db_name,
        user=odoo_config["db_user"],
        password=odoo_config["db_password"],
        host=odoo_config["db_host"],
        port=odoo_config["db_port"] or 5432,
    )


def _wait_for_docker(timeout: int = 120):
    """Wait for DinD sidecar to become ready."""
    import subprocess
    _logger.info("Waiting for Docker daemon (DinD sidecar)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                _logger.info("Docker daemon is ready.")
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(2)
    raise RuntimeError(
        f"Docker daemon did not become ready within {timeout}s. "
        "Check DinD sidecar logs."
    )


def _update_eval(conn, rec_id: int, vals: dict):
    """Write evaluation fields via raw SQL."""
    if not vals:
        return
    sets = []
    params = []
    for k, v in vals.items():
        sets.append(f"{k} = %s")
        params.append(v)
    params.append(rec_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE aurora_evaluation SET {', '.join(sets)} WHERE id = %s",
            params,
        )
    conn.commit()


def _append_log(conn, rec_id: int, message: str):
    """Append to evaluation log field."""
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {message}\n"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE aurora_evaluation SET log = COALESCE(log, '') || %s WHERE id = %s",
            (line, rec_id),
        )
    conn.commit()


def _heartbeat(conn, rec_id: int, progress_text: Optional[str] = None):
    """Update heartbeat timestamp."""
    vals = {"last_heartbeat": "NOW()"}
    sets = ["last_heartbeat = NOW()"]
    params = []
    if progress_text:
        sets.append("progress_text = %s")
        params.append(progress_text)
    params.append(rec_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE aurora_evaluation SET {', '.join(sets)} WHERE id = %s",
            params,
        )
    conn.commit()


def _fail_eval(conn, rec_id: int, message: str):
    """Mark evaluation as failed."""
    _append_log(conn, rec_id, f"FAILED: {message}")
    _update_eval(conn, rec_id, {"stage": "failed", "progress_text": "Failed"})


def _read_eval_config(conn, rec_id: int) -> dict:
    """Read evaluation configuration from DB."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dataset_file, patch_file, repo_dir, workdir, output_dir, "
            "force_build, max_workers_build, max_workers_run, docker_platform, "
            "instance_limit, specific_prs "
            "FROM aurora_evaluation WHERE id = %s",
            (rec_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Evaluation record {rec_id} not found")
    return {
        "dataset_file": row[0],
        "patch_file": row[1],
        "repo_dir": row[2],
        "workdir": row[3],
        "output_dir": row[4],
        "force_build": row[5],
        "max_workers_build": row[6] or 4,
        "max_workers_run": row[7] or 4,
        "docker_platform": row[8] or None,
        "instance_limit": row[9] or 0,
        "specific_prs": row[10] or "",
    }


def run_evaluation(db_name: str, rec_id: int):
    """Main evaluation pipeline — runs inside K8s Job."""
    conn = _open_cursor(db_name)
    heartbeat_stop = threading.Event()

    def _heartbeat_loop():
        while not heartbeat_stop.wait(timeout=60):
            try:
                hb_conn = _open_cursor(db_name)
                _heartbeat(hb_conn, rec_id, None)
                hb_conn.close()
            except Exception:
                _logger.debug("Heartbeat write failed", exc_info=True)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    try:
        _heartbeat(conn, rec_id, "Initializing evaluation worker")
        _append_log(conn, rec_id, "K8s evaluation worker started.")

        _wait_for_docker(timeout=120)
        _append_log(conn, rec_id, "Docker daemon ready.")

        _check_cancelled()

        # Read config
        cfg = _read_eval_config(conn, rec_id)
        _append_log(conn, rec_id, f"Config loaded: workdir={cfg['workdir']}")

        # Resolve remote dataset to local
        from odoo.addons.aurora.models import dataset_resolver
        dataset_file = cfg["dataset_file"]
        if dataset_resolver.is_remote(dataset_file):
            _append_log(conn, rec_id, f"Downloading remote dataset: {dataset_file}")
            dataset_file = dataset_resolver.resolve_to_local(conn, dataset_file)
            _append_log(conn, rec_id, f"Dataset cached locally: {dataset_file}")

        patch_file = cfg["patch_file"]
        log_dir = Path(cfg["output_dir"]) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Import harness
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        from odoo.addons.aurora.tools.harness_bridge.phase2_docker_build import (
            check_instance_registry,
            _import_all_repo_modules,
        )

        # Sync instance registries
        _check_cancelled()
        _append_log(conn, rec_id, "Syncing harness instance registries...")
        _heartbeat(conn, rec_id, "Syncing registries")

        try:
            import importlib
            importlib.import_module("odoo.addons.aurora.tools.harness.repos")
        except Exception:
            _logger.warning("Bulk repo import failed", exc_info=True)

        # Parse specifics filter
        specifics = None
        if cfg["specific_prs"]:
            specifics = {s.strip() for s in cfg["specific_prs"].split(",") if s.strip()}

        # Build EvalConfig
        eval_config = EvalConfig(
            mode="evaluation",
            workdir=cfg["workdir"],
            patch_files=[os.path.abspath(patch_file)] if patch_file else [],
            dataset_files=[os.path.abspath(dataset_file)],
            force_build=cfg["force_build"],
            output_dir=cfg["output_dir"],
            repo_dir=cfg["repo_dir"],
            need_clone=True,
            stop_on_error=False,
            max_workers=cfg["max_workers_build"],
            max_workers_build_image=cfg["max_workers_build"],
            max_workers_run_instance=cfg["max_workers_run"],
            log_dir=log_dir,
            log_level="INFO",
            log_to_console=True,
            platform=cfg["docker_platform"],
            specifics=specifics,
            instance_limit=cfg["instance_limit"],
        )

        total_instances = len(eval_config.instances)
        if total_instances == 0:
            _fail_eval(conn, rec_id, "No instances matched dataset + harness registry.")
            return

        _append_log(conn, rec_id, f"Found {total_instances} instances to evaluate.")
        _update_eval(conn, rec_id, {"total_instances": total_instances})

        # ── Stage 1: Build Docker Images ──────────────────────────────────
        _check_cancelled()
        _update_eval(conn, rec_id, {
            "stage": "building_images",
            "build_status": "running",
        })
        _append_log(conn, rec_id, "Building Docker images...")
        _heartbeat(conn, rec_id, "Building Docker images")

        try:
            eval_config.run_mode_image()
            _update_eval(conn, rec_id, {"build_status": "done"})
            _append_log(conn, rec_id, "Docker images built successfully.")
        except Exception as exc:
            _fail_eval(conn, rec_id, f"Image build failed: {exc}")
            raise

        # ── Stage 2: Run Instances ────────────────────────────────────────
        _check_cancelled()
        _update_eval(conn, rec_id, {
            "stage": "running_instances",
            "run_status": "running",
        })
        _append_log(conn, rec_id, "Running evaluation instances...")
        _heartbeat(conn, rec_id, "Running instances")

        try:
            eval_config.run_mode_instance_only()
            _update_eval(conn, rec_id, {"run_status": "done"})
            _append_log(conn, rec_id, "Instances run successfully.")
        except Exception as exc:
            _fail_eval(conn, rec_id, f"Instance run failed: {exc}")
            raise

        # ── Stage 3: Generate Reports ─────────────────────────────────────
        _check_cancelled()
        _update_eval(conn, rec_id, {
            "stage": "generating_reports",
            "report_status": "running",
        })
        _append_log(conn, rec_id, "Generating evaluation reports...")
        _heartbeat(conn, rec_id, "Generating reports")

        try:
            from odoo.addons.aurora.tools.harness.gen_report import ReportCliArgs
            report_args = ReportCliArgs(
                mode="evaluation",
                workdir=Path(cfg["workdir"]),
                output_dir=Path(cfg["output_dir"]),
                specifics=None,
                skips=None,
                raw_dataset_files=[os.path.abspath(dataset_file)],
                dataset_files=[os.path.abspath(dataset_file)],
                max_workers=cfg["max_workers_run"],
                log_dir=log_dir,
                log_level="INFO",
                log_to_console=True,
            )
            report_args.run()
            _update_eval(conn, rec_id, {"report_status": "done"})
            _append_log(conn, rec_id, "Reports generated successfully.")
        except Exception as exc:
            _fail_eval(conn, rec_id, f"Report generation failed: {exc}")
            raise

        # ── Finalize ──────────────────────────────────────────────────────
        import json
        final_report_path = Path(cfg["output_dir"]) / "final_report.json"
        total = resolved = unresolved = errors = 0
        if final_report_path.exists():
            try:
                with open(final_report_path, "r", encoding="utf-8") as f:
                    fr = json.load(f)
                total = fr.get("total_instances", 0)
                resolved = fr.get("resolved_instances", 0)
                unresolved = fr.get("unresolved_instances", 0)
                errors = fr.get("error_instances", 0)
            except Exception:
                _logger.warning("Failed to parse final_report.json", exc_info=True)

        _update_eval(conn, rec_id, {
            "stage": "done",
            "progress_text": "Evaluation complete",
            "total_instances": total,
            "resolved_instances": resolved,
            "unresolved_instances": unresolved,
            "error_instances": errors,
            "final_report_file": str(final_report_path) if final_report_path.exists() else None,
        })
        _append_log(conn, rec_id, f"Evaluation complete: {resolved}/{total} resolved.")
        _logger.info("Evaluation %s complete: %d/%d resolved", rec_id, resolved, total)

    except EvalCancelled:
        _logger.info("Evaluation %s cancelled via SIGTERM", rec_id)
        _update_eval(conn, rec_id, {"stage": "failed", "progress_text": "Cancelled"})
        _append_log(conn, rec_id, "Evaluation cancelled (SIGTERM).")
    except Exception:
        _logger.exception("Evaluation %s fatal error", rec_id)
        tb = traceback.format_exc()[-500:]
        try:
            _fail_eval(conn, rec_id, tb)
        except Exception:
            _logger.exception("Failed to record eval error to DB")
    finally:
        heartbeat_stop.set()
        try:
            conn.close()
        except Exception:
            pass


def main():
    rec_id = int(os.environ.get("EVALUATION_ID", "0"))
    db_name = os.environ.get("ODOO_DB", "")
    conf_path = os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf")

    if not rec_id:
        _logger.error("EVALUATION_ID env var is required")
        sys.exit(1)
    if not db_name:
        _logger.error("ODOO_DB env var is required")
        sys.exit(1)

    _logger.info("Starting evaluation worker: eval_id=%s db=%s", rec_id, db_name)

    try:
        _boot_odoo(db_name, conf_path)
    except Exception:
        _logger.exception("Failed to boot Odoo registry")
        sys.exit(1)

    run_evaluation(db_name, rec_id)


if __name__ == "__main__":
    main()
