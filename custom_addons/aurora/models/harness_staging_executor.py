import atexit
import json
import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import psycopg2

_logger = logging.getLogger(__name__)

_MAX_STAGING_THREADS = 1
_MAX_CONCURRENT_TESTS = 1

_executor = ThreadPoolExecutor(max_workers=_MAX_STAGING_THREADS)
_semaphore = threading.Semaphore(_MAX_CONCURRENT_TESTS)
atexit.register(_executor.shutdown, wait=True, cancel_futures=True)

_ALLOWED_COLUMNS = frozenset({
    "stage", "test_result", "test_log",
})

_MAX_LOG_SIZE = 200_000


def _open_cursor(db_name):
    from odoo.modules.registry import Registry
    return Registry(db_name).cursor()


def _update_staging(cr: Any, rec_id: int, vals: dict[str, Any]) -> None:
    if not vals:
        return
    invalid = set(vals) - _ALLOWED_COLUMNS
    if invalid:
        raise ValueError(f"Attempted to update disallowed columns: {invalid}")
    sets = ", ".join(f"{k} = %s" for k in vals)
    params = list(vals.values()) + [rec_id]
    query = f"UPDATE aurora_harness_staging SET {sets} WHERE id = %s"
    cr.execute(query, params)


def _append_test_log(cr: Any, rec_id: int, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    cr.execute(
        "UPDATE aurora_harness_staging SET test_log = RIGHT(COALESCE(test_log, '') || %s, %s) WHERE id = %s",
        [line + "\n", _MAX_LOG_SIZE, rec_id],
    )


def _read_staging_config(db_name: str, rec_id: int) -> dict[str, Any]:
    from odoo import api, SUPERUSER_ID
    cr = _open_cursor(db_name)
    try:
        env = api.Environment(cr, SUPERUSER_ID, {})
        rec = env["aurora.harness.staging"].browse(rec_id)
        if not rec.exists():
            raise RuntimeError(f"Staging record {rec_id} not found")
        staging_path = rec._ensure_staging_file()
        return {
            "org": rec.org,
            "repo": rec.repo,
            "dataset_file": rec.dataset_file,
            "staging_path": staging_path,
            "user_id": rec.user_id.id,
        }
    finally:
        cr.close()


def _safe_worker(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            _logger.exception("Aurora staging test worker crashed")
            db_name = args[0] if args else None
            rec_id = args[2] if len(args) > 2 else None
            if db_name and rec_id:
                try:
                    cr = _open_cursor(db_name)
                    _update_staging(cr, rec_id, {
                        "stage": "failed",
                        "test_result": "failed",
                    })
                    _append_test_log(cr, rec_id, f"FATAL: {traceback.format_exc()[-500:]}")
                    cr.commit()
                    cr.close()
                except Exception:
                    _logger.exception("Failed to record staging test crash to DB")
        finally:
            _semaphore.release()
    return wrapper


@_safe_worker
def _run_staging_test(db_name, uid, rec_id):
    import importlib
    from ..tools.harness import dataset as _ds_mod, run_evaluation as _re_mod
    importlib.reload(_ds_mod)
    importlib.reload(_re_mod)
    from ..tools.harness.run_evaluation import EvalConfig
    from ..tools.harness.staging_loader import load_staging_harness, unload_staging_harness

    cr = _open_cursor(db_name)
    originals = None

    try:
        cfg = _read_staging_config(db_name, rec_id)

        _append_test_log(cr, rec_id, "Loading staging harness...")
        cr.commit()

        originals = load_staging_harness(
            cfg["staging_path"], cfg["org"], cfg["repo"],
        )

        _append_test_log(cr, rec_id, "Validating dataset...")
        cr.commit()

        dataset_path = cfg["dataset_file"]
        org, repo = cfg["org"], cfg["repo"]
        matching_prs: list[str] = []

        with open(dataset_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("org") == org and entry.get("repo") == repo:
                    pr_id = f"{org}/{repo}:pr-{entry.get('number', '')}"
                    matching_prs.append(pr_id)

        if not matching_prs:
            msg = f"Dataset has no PRs for {org}/{repo}. Select a different dataset."
            _update_staging(cr, rec_id, {
                "stage": "tested",
                "test_result": "failed",
            })
            _append_test_log(cr, rec_id, msg)
            cr.commit()
            return

        test_prs = matching_prs[:4]
        specifics = set(test_prs)

        _append_test_log(
            cr, rec_id,
            f"Found {len(matching_prs)} matching PRs. Testing with {len(test_prs)}: {', '.join(test_prs)}",
        )
        cr.commit()

        ICP_cr = _open_cursor(db_name)
        try:
            from odoo import api, SUPERUSER_ID
            env = api.Environment(ICP_cr, SUPERUSER_ID, {})
            base_dir = env["ir.config_parameter"].sudo().get_param(
                "aurora.output_dir", "/tmp/aurora_output",
            )
        finally:
            ICP_cr.close()

        test_output_dir = os.path.abspath(os.path.join(
            base_dir, "harness_staging", str(cfg["user_id"]), "test_output",
        ))
        test_workdir = os.path.join(test_output_dir, "workdir")
        test_repo_dir = os.path.abspath(os.path.join(base_dir, "repos"))
        log_dir = Path(test_output_dir) / "logs"

        os.makedirs(test_output_dir, exist_ok=True)
        os.makedirs(test_workdir, exist_ok=True)
        os.makedirs(test_repo_dir, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        # Absolute paths: EvalConfig._expand_{patch,dataset}_files uses glob.glob
        # which silently returns [] on relative paths when worker thread CWD
        # doesn't match the expected root. Force-resolve before passing in.
        dataset_path_abs = os.path.abspath(dataset_path)
        patch_path = os.path.abspath(os.path.join(test_output_dir, "patches.jsonl"))
        with open(dataset_path_abs, "r", encoding="utf-8") as f_in, \
             open(patch_path, "w", encoding="utf-8") as f_out:
            for line in f_in:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("org") == org and entry.get("repo") == repo:
                    from odoo.addons.aurora.models.evaluation import AuroraEvaluation
                    number = AuroraEvaluation._resolve_entry_number(entry)
                    if number is None:
                        continue
                    patch_entry = {
                        "org": entry["org"],
                        "repo": entry["repo"],
                        "number": number,
                        "fix_patch": entry.get("fix_patch", ""),
                    }
                    f_out.write(json.dumps(patch_entry, ensure_ascii=False) + "\n")

        _append_test_log(cr, rec_id, "Building EvalConfig for mini-evaluation...")
        cr.commit()

        eval_config = EvalConfig(
            mode="evaluation",
            workdir=Path(test_workdir),
            patch_files=[patch_path],
            dataset_files=[dataset_path_abs],
            force_build=False,
            output_dir=Path(test_output_dir),
            repo_dir=Path(test_repo_dir),
            need_clone=True,
            stop_on_error=False,
            max_workers=2,
            max_workers_build_image=2,
            max_workers_run_instance=2,
            log_dir=log_dir,
            log_level="DEBUG",
            log_to_console=False,
            specifics=specifics,
            instance_limit=4,
        )

        from ..tools.harness.instance import Instance as _Inst
        staging_key = f"{org}/{repo}"
        _append_test_log(
            cr, rec_id,
            f"DEBUG registry: '{staging_key}' present={staging_key in _Inst._registry}, "
            f"total_keys={len(_Inst._registry)}",
        )
        _append_test_log(cr, rec_id, f"DEBUG dataset_file (abs): {dataset_path_abs}")
        _append_test_log(cr, rec_id, f"DEBUG patch_file (abs): {patch_path}")
        _append_test_log(cr, rec_id, f"DEBUG dataset count: {len(eval_config.dataset)}")
        _append_test_log(cr, rec_id, f"DEBUG patch count: {len(eval_config.patches)}")
        _append_test_log(cr, rec_id, f"DEBUG patch_numbers: {eval_config.patch_numbers}")
        _append_test_log(cr, rec_id, f"DEBUG specifics: {specifics}")
        if hasattr(eval_config, '_logger'):
            for h in eval_config._logger.handlers:
                if hasattr(h, 'baseFilename'):
                    try:
                        with open(h.baseFilename, 'r') as _lf:
                            _log_tail = _lf.read()[-2000:]
                            _append_test_log(cr, rec_id, f"DEBUG eval log:\n{_log_tail}")
                    except Exception:
                        pass
        cr.commit()

        total_instances = len(eval_config.instances)
        if total_instances == 0:
            msg = (
                f"No instances matched for {org}/{repo}. "
                f"Check that @Instance.register('{org}', '{repo}') is correct."
            )
            _update_staging(cr, rec_id, {
                "stage": "tested",
                "test_result": "failed",
            })
            _append_test_log(cr, rec_id, msg)
            cr.commit()
            return

        _append_test_log(cr, rec_id, f"Matched {total_instances} instances. Building Docker images...")
        cr.commit()

        try:
            eval_config.run_mode_image()
            _append_test_log(cr, rec_id, "Docker images built successfully.")
            cr.commit()
        except Exception as exc:
            _update_staging(cr, rec_id, {
                "stage": "tested",
                "test_result": "failed",
            })
            _append_test_log(cr, rec_id, f"Docker build failed: {exc}")
            cr.commit()
            return

        _append_test_log(cr, rec_id, "Running test instances...")
        cr.commit()

        try:
            eval_config.run_mode_instance_only()
            _append_test_log(cr, rec_id, "Test instances completed successfully.")
            cr.commit()
        except Exception as exc:
            _update_staging(cr, rec_id, {
                "stage": "tested",
                "test_result": "failed",
            })
            _append_test_log(cr, rec_id, f"Instance run failed: {exc}")
            cr.commit()
            return

        _update_staging(cr, rec_id, {
            "stage": "tested",
            "test_result": "success",
        })
        _append_test_log(cr, rec_id, "Staging test PASSED.")
        cr.commit()

    except Exception:
        _logger.exception("Staging test fatal error rec=%s", rec_id)
        try:
            cr.rollback()
            _update_staging(cr, rec_id, {
                "stage": "failed",
                "test_result": "failed",
            })
            _append_test_log(cr, rec_id, f"FATAL: {traceback.format_exc()[-500:]}")
            cr.commit()
        except Exception:
            _logger.exception("Failed to record staging test error")
    finally:
        if originals is not None:
            try:
                unload_staging_harness(originals)
            except Exception:
                _logger.debug("Failed to unload staging harness", exc_info=True)
        try:
            cr.close()
        except Exception:
            pass


def submit_test_async(db_name: str, uid: int, rec_id: int) -> bool:
    acquired = _semaphore.acquire(blocking=False)
    if not acquired:
        _logger.warning("Staging test semaphore full – test %s not started", rec_id)
        return False
    _executor.submit(_run_staging_test, db_name, uid, rec_id)
    return True


def is_test_slot_available() -> bool:
    """Return True if a staging test can start right now, False if busy.

    The executor uses a single-slot semaphore; only one staging test runs at a
    time to avoid races on the process-global ``Instance._registry``. This
    helper lets the UI-action layer surface a UserError up front instead of
    silently dropping the request in ``submit_test_async``.

    Not thread-safe vs. ``submit_test_async`` (TOCTOU race window), but the
    tail-call to ``submit_test_async`` via ``cr.postcommit`` still returns
    False and logs a warning if we lose the race. The user at least gets an
    accurate answer in the common case.
    """
    acquired = _semaphore.acquire(blocking=False)
    if acquired:
        _semaphore.release()
        return True
    return False
