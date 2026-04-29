import atexit
import json
import logging
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import psycopg2

_logger = logging.getLogger(__name__)

_MAX_EVAL_THREADS = 2
_MAX_CONCURRENT_EVALS = 2

_executor = ThreadPoolExecutor(max_workers=_MAX_EVAL_THREADS)
_semaphore = threading.Semaphore(_MAX_CONCURRENT_EVALS)
atexit.register(_executor.shutdown, wait=True, cancel_futures=True)

_cancel_events = {}
_cancel_lock = threading.Lock()

_ALLOWED_COLUMNS = frozenset({
    "stage", "build_status", "run_status", "report_status",
    "log", "last_heartbeat", "progress_text",
    "total_instances", "resolved_instances", "unresolved_instances",
    "error_instances", "final_report_file", "missing_registries",
})

_MAX_LOG_SIZE = 500_000


def request_cancel(rec_id: int) -> bool:
    with _cancel_lock:
        event = _cancel_events.get(rec_id)
        if event:
            event.set()
            return True
    return False


def _register_cancel_event(rec_id: int) -> threading.Event:
    event = threading.Event()
    with _cancel_lock:
        _cancel_events[rec_id] = event
    return event


def _unregister_cancel_event(rec_id: int) -> None:
    with _cancel_lock:
        _cancel_events.pop(rec_id, None)


def _open_cursor(db_name):
    from odoo.modules.registry import Registry
    return Registry(db_name).cursor()


_SERIALIZATION_RETRIES = 3
_SERIALIZATION_BACKOFF = 0.5  # seconds, doubles each retry


def _update_eval(cr: Any, rec_id: int, vals: dict[str, Any]) -> None:
    if not vals:
        return
    invalid = set(vals) - _ALLOWED_COLUMNS
    if invalid:
        raise ValueError(f"Attempted to update disallowed columns: {invalid}")
    sets = ", ".join(f"{k} = %s" for k in vals)
    params = list(vals.values()) + [rec_id]
    query = f"UPDATE aurora_evaluation SET {sets} WHERE id = %s"

    for attempt in range(_SERIALIZATION_RETRIES):
        try:
            cr.execute(query, params)
            return
        except psycopg2.errors.SerializationFailure:
            cr.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                delay = _SERIALIZATION_BACKOFF * (2 ** attempt)
                _logger.warning(
                    "SerializationFailure on aurora_evaluation id=%s, "
                    "retry %d/%d in %.1fs",
                    rec_id, attempt + 1, _SERIALIZATION_RETRIES, delay,
                )
                time.sleep(delay)
            else:
                _logger.error(
                    "SerializationFailure on aurora_evaluation id=%s, "
                    "exhausted %d retries",
                    rec_id, _SERIALIZATION_RETRIES,
                )
                raise


def _append_log(cr: Any, rec_id: int, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    cr.execute(
        "UPDATE aurora_evaluation SET log = RIGHT(COALESCE(log, '') || %s, %s) WHERE id = %s",
        [line + "\n", _MAX_LOG_SIZE, rec_id],
    )


def _notify_bus(db_name: str, rec_id: int) -> None:
    notify_cr = None
    try:
        from odoo import api, SUPERUSER_ID
        notify_cr = _open_cursor(db_name)
        env = api.Environment(notify_cr, SUPERUSER_ID, {})
        env["bus.bus"]._sendone(
            f"aurora_evaluation_{rec_id}",
            "aurora_evaluation_update",
            {"evaluation_id": rec_id},
        )
        notify_cr.commit()
    except Exception:
        _logger.debug("Failed to send bus notification for eval rec=%s", rec_id, exc_info=True)
        if notify_cr:
            try:
                notify_cr.rollback()
            except Exception:
                pass
    finally:
        if notify_cr:
            try:
                notify_cr.close()
            except Exception:
                pass


def _heartbeat(cr: Any, rec_id: int, progress_text: Optional[str] = None) -> None:
    vals = {"last_heartbeat": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if progress_text is not None:
        vals["progress_text"] = progress_text
    _update_eval(cr, rec_id, vals)
    cr.commit()


def _fail_eval(cr: Any, rec_id: int, step_field: str, exc) -> None:
    _update_eval(cr, rec_id, {step_field: "failed", "stage": "failed"})
    _append_log(cr, rec_id, f"FAILED ({step_field}): {exc}")


def _post_chatter(db_name: str, uid: Optional[int], rec_id: int, body: str) -> None:
    cr = None
    try:
        from odoo import api, SUPERUSER_ID
        cr = _open_cursor(db_name)
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        rec = env["aurora.evaluation"].browse(rec_id)
        rec.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")
        cr.commit()
    except Exception:
        _logger.exception("Failed to post chatter message for eval rec=%s", rec_id)
    finally:
        if cr:
            cr.close()


def _read_eval_config(db_name: str, rec_id: int) -> dict[str, Any]:
    from odoo import api, SUPERUSER_ID
    cr = _open_cursor(db_name)
    try:
        env = api.Environment(cr, SUPERUSER_ID, {})
        rec = env["aurora.evaluation"].browse(rec_id)
        if not rec.exists():
            raise RuntimeError(f"Evaluation record {rec_id} not found")
        return {
            "dataset_file": rec.dataset_file,
            "patch_file": rec.patch_file,
            "repo_dir": rec.repo_dir,
            "workdir": rec.workdir,
            "output_dir": rec.output_dir,
            "force_build": rec.force_build,
            "max_workers_build": rec.max_workers_build or 4,
            "max_workers_run": rec.max_workers_run or 4,
            "docker_platform": rec.docker_platform or None,
            "instance_limit": rec.instance_limit or 0,
            "specific_prs": rec.specific_prs or "",
        }
    finally:
        cr.close()


class EvalCancelled(Exception):
    pass


def _safe_worker(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            _logger.exception("Aurora evaluation worker crashed")
            db_name = args[0] if args else None
            rec_id = args[2] if len(args) > 2 else None
            if db_name and rec_id:
                try:
                    cr = _open_cursor(db_name)
                    _fail_eval(cr, rec_id, "build_status", exc)
                    cr.commit()
                    cr.close()
                except Exception:
                    _logger.exception("Failed to record eval crash to DB")
        finally:
            _semaphore.release()
    return wrapper


def _load_staging_for_eval(db_name: str, user_id: int) -> dict:
    from ..tools.harness.staging_loader import load_staging_harness
    staging_cr = _open_cursor(db_name)
    all_originals: dict = {}
    try:
        from odoo import api, SUPERUSER_ID
        env = api.Environment(staging_cr, SUPERUSER_ID, {})
        staging_records = env["aurora.harness.staging"].search([
            ("stage", "in", ["tested", "evaluating"]),
            ("user_id", "=", user_id),
            ("active", "=", True),
        ])
        if not staging_records:
            return all_originals

        # Pre-load production harnesses. EvalConfig.instances has a gate
        # `if not Instance._registry: import repos`; staging load would
        # populate the registry and bypass that import, leaving production
        # harnesses unregistered for datasets that span multiple repos.
        try:
            import importlib
            importlib.import_module("odoo.addons.aurora.tools.harness.repos")
        except Exception:
            _logger.warning("Bulk repo import failed during staging preload", exc_info=True)

        for rec in staging_records:
            try:
                staging_path = rec._ensure_staging_file()
                originals = load_staging_harness(staging_path, rec.org, rec.repo)
                all_originals.update(originals)
            except Exception:
                _logger.warning(
                    "Failed to load staging harness for %s/%s",
                    rec.org, rec.repo, exc_info=True,
                )
    finally:
        staging_cr.close()
    return all_originals


@_safe_worker
def _run_evaluation(db_name, uid, rec_id):
    from ..tools.harness.run_evaluation import EvalConfig
    from ..tools.harness.staging_loader import load_staging_harness, unload_staging_harness

    cancel_event = _register_cancel_event(rec_id)
    _heartbeat_stop = None
    staging_originals: dict = {}

    try:
        cr = _open_cursor(db_name)
    except Exception:
        _unregister_cancel_event(rec_id)
        raise

    try:
        cfg = _read_eval_config(db_name, rec_id)

        staging_originals = _load_staging_for_eval(db_name, uid)

        log_dir = Path(cfg["output_dir"]) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        _heartbeat_stop = threading.Event()

        def _heartbeat_loop():
            first_run = True
            while True:
                if not first_run:
                    if _heartbeat_stop.wait(timeout=120):
                        break
                first_run = False
                hb_cr = None
                try:
                    hb_cr = _open_cursor(db_name)
                    _heartbeat(hb_cr, rec_id)
                    hb_cr.close()
                    hb_cr = None
                except Exception:
                    _logger.debug("Eval heartbeat failed for rec=%s", rec_id, exc_info=True)
                    if hb_cr:
                        try:
                            hb_cr.rollback()
                            hb_cr.close()
                        except Exception:
                            pass
                        hb_cr = None

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        def _check_cancelled():
            if cancel_event.is_set():
                raise EvalCancelled(f"Evaluation {rec_id} cancelled by user")

        _check_cancelled()
        _update_eval(cr, rec_id, {"build_status": "running", "stage": "building_images"})
        _append_log(cr, rec_id, "Starting evaluation pipeline...")
        _heartbeat(cr, rec_id, "Initializing...")

        specifics = None
        if cfg["specific_prs"]:
            specifics = {s.strip() for s in cfg["specific_prs"].split(",") if s.strip()}

        # EvalConfig uses glob.glob() on patch/dataset paths; relative paths
        # silently resolve to [] in background threads. Force absolute here.
        patch_file_abs = os.path.abspath(cfg["patch_file"]) if cfg["patch_file"] else cfg["patch_file"]
        dataset_file_abs = os.path.abspath(cfg["dataset_file"]) if cfg["dataset_file"] else cfg["dataset_file"]

        eval_config = EvalConfig(
            mode="evaluation",
            workdir=cfg["workdir"],
            patch_files=[patch_file_abs],
            dataset_files=[dataset_file_abs],
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
            log_to_console=False,
            platform=cfg["docker_platform"],
            specifics=specifics,
            instance_limit=cfg["instance_limit"],
        )

        _check_cancelled()
        _append_log(cr, rec_id, "Validating dataset instances...")
        _heartbeat(cr, rec_id, "Validating instances")
        cr.commit()

        total_dataset = len(eval_config.dataset)
        total_instances = len(eval_config.instances)
        if total_instances == 0:
            missing_repos = set()
            from ..tools.harness.instance import Instance
            for pr in eval_config.dataset.values():
                key = f"{pr.org}/{pr.repo}"
                if key not in Instance._registry:
                    missing_repos.add(key)
            missing_list = ", ".join(sorted(missing_repos))
            msg = (
                f"No registered harness instances found. "
                f"Dataset has {total_dataset} entries but 0 could be matched to a harness implementation.\n"
                f"Missing harness registry for: {missing_list}\n"
                f"Add a harness implementation in tools/harness/repos/ for these repos."
            )
            _fail_eval(cr, rec_id, "build_status", msg)
            _update_eval(cr, rec_id, {"missing_registries": missing_list})
            _append_log(cr, rec_id, msg)
            cr.commit()
            _post_chatter(db_name, uid, rec_id, msg)
            return

        _append_log(cr, rec_id, f"Found {total_instances}/{total_dataset} instances with harness implementations.")
        cr.commit()

        _check_cancelled()
        _append_log(cr, rec_id, "Building Docker images...")
        _heartbeat(cr, rec_id, "Building Docker images")
        cr.commit()

        try:
            eval_config.run_mode_image()
            _update_eval(cr, rec_id, {"build_status": "done"})
            _append_log(cr, rec_id, "Docker images built successfully.")
            cr.commit()
            _notify_bus(db_name, rec_id)
            cr.commit()
        except Exception as exc:
            _fail_eval(cr, rec_id, "build_status", exc)
            cr.commit()
            _notify_bus(db_name, rec_id)
            cr.commit()
            raise

        _check_cancelled()
        _update_eval(cr, rec_id, {"run_status": "running", "stage": "running_instances"})
        _append_log(cr, rec_id, "Running evaluation instances...")
        _heartbeat(cr, rec_id, "Running instances")
        cr.commit()
        _notify_bus(db_name, rec_id)
        cr.commit()

        try:
            eval_config.run_mode_instance_only()
            _update_eval(cr, rec_id, {"run_status": "done"})
            _append_log(cr, rec_id, "Instances run successfully.")
            cr.commit()
            _notify_bus(db_name, rec_id)
            cr.commit()
        except Exception as exc:
            _fail_eval(cr, rec_id, "run_status", exc)
            cr.commit()
            _notify_bus(db_name, rec_id)
            cr.commit()
            raise

        _check_cancelled()
        _update_eval(cr, rec_id, {"report_status": "running", "stage": "generating_reports"})
        _append_log(cr, rec_id, "Generating evaluation reports...")
        _heartbeat(cr, rec_id, "Generating reports")
        cr.commit()
        _notify_bus(db_name, rec_id)
        cr.commit()

        try:
            from ..tools.harness.gen_report import ReportCliArgs
            report_args = ReportCliArgs(
                mode="evaluation",
                workdir=Path(cfg["workdir"]),
                output_dir=Path(cfg["output_dir"]),
                specifics=None,
                skips=None,
                raw_dataset_files=[dataset_file_abs],
                dataset_files=[dataset_file_abs],
                max_workers=cfg["max_workers_run"],
                log_dir=log_dir,
                log_level="INFO",
                log_to_console=False,
            )
            report_args.run()
            _update_eval(cr, rec_id, {"report_status": "done"})
            _append_log(cr, rec_id, "Reports generated successfully.")
            cr.commit()
        except Exception as exc:
            _fail_eval(cr, rec_id, "report_status", exc)
            cr.commit()
            raise

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
                _logger.debug("Failed to parse final_report.json for rec=%s", rec_id, exc_info=True)

        _update_eval(cr, rec_id, {
            "stage": "done",
            "progress_text": "Evaluation complete",
            "total_instances": total,
            "resolved_instances": resolved,
            "unresolved_instances": unresolved,
            "error_instances": errors,
            "final_report_file": str(final_report_path) if final_report_path.exists() else None,
        })
        _append_log(cr, rec_id, f"Evaluation complete: {resolved}/{total} resolved.")
        cr.commit()
        _notify_bus(db_name, rec_id)
        cr.commit()

        _post_chatter(
            db_name, uid, rec_id,
            f"Evaluation completed — {resolved}/{total} instances resolved, "
            f"{unresolved} unresolved, {errors} errors."
        )

    except EvalCancelled:
        _logger.info("Aurora evaluation %s cancelled by user", rec_id)
        try:
            cr.rollback()
            _update_eval(cr, rec_id, {"stage": "failed", "progress_text": "Cancelled"})
            _append_log(cr, rec_id, "Evaluation cancelled by user.")
            cr.commit()
            _notify_bus(db_name, rec_id)
            cr.commit()
            _post_chatter(db_name, uid, rec_id, "Evaluation cancelled by user.")
        except Exception:
            _logger.exception("Failed to record eval cancellation for rec=%s", rec_id)
    except Exception:
        _logger.exception("Aurora evaluation fatal error rec=%s", rec_id)
        try:
            cr.rollback()
            err_msg = traceback.format_exc()[-500:]
            _fail_eval(cr, rec_id, "build_status", err_msg)
            cr.commit()
            _notify_bus(db_name, rec_id)
            cr.commit()
            _post_chatter(db_name, uid, rec_id, f"Evaluation failed:\n{err_msg}")
        except Exception:
            _logger.exception("Failed to record eval error")
    finally:
        _unregister_cancel_event(rec_id)
        if staging_originals:
            try:
                unload_staging_harness(staging_originals)
            except Exception:
                _logger.debug("Failed to unload staging harnesses", exc_info=True)
        if _heartbeat_stop:
            _heartbeat_stop.set()
        try:
            cr.close()
        except Exception:
            pass


def submit_evaluation_async(db_name: str, uid: int, rec_id: int) -> bool:
    acquired = _semaphore.acquire(blocking=False)
    if not acquired:
        _logger.warning("Aurora eval semaphore full – evaluation %s not started", rec_id)
        return False
    _executor.submit(_run_evaluation, db_name, uid, rec_id)
    return True
