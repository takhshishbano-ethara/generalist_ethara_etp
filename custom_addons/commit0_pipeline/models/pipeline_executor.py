# -*- coding: utf-8 -*-
import atexit
import logging
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

_logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="commit0")
_semaphore = threading.Semaphore(4)


def _shutdown_executor():
    _executor.shutdown(wait=False)


atexit.register(_shutdown_executor)

# ── Shared helpers ──────────────────────────────────────────────────────────


def get_tools_path(env):
    param = (
        env["ir.config_parameter"].sudo().get_param("commit0_pipeline.tools_path", "")
    )
    if param:
        return param
    module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(module_path, "tools")


def _get_config(env):
    ICP = env["ir.config_parameter"].sudo()
    return {
        "github_token": ICP.get_param("commit0_pipeline.github_token", ""),
        "github_org": ICP.get_param("commit0_pipeline.github_org", "Ethara-Ai"),
    }


def _ensure_tools_on_path(tools_path=None):
    if tools_path is None:
        module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tools_path = os.path.join(module_path, "tools")
    parent = os.path.dirname(tools_path)
    if parent not in sys.path:
        sys.path.insert(0, parent)


# ── Raw-SQL helpers (background threads have no ORM) ────────────────────────


def _update_eval(cr, eval_id, vals):
    if not vals:
        return
    cols, params = [], []
    for col, val in vals.items():
        cols.append('"%s" = %%s' % col)
        params.append(val)
    params.append(eval_id)
    cr.execute(
        "UPDATE commit0_repo_evaluation SET %s WHERE id = %%s" % ", ".join(cols), params
    )


def _append_eval_log(cr, eval_id, msg):
    line = "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg)
    cr.execute(
        "UPDATE commit0_repo_evaluation"
        " SET log_output = COALESCE(log_output, '') || %s"
        " WHERE id = %s",
        [line, eval_id],
    )
    _logger.info("Eval %s: %s", eval_id, msg)


def _fail_eval(cr, eval_id, field, exc):
    _update_eval(cr, eval_id, {field: "failed", "error_message": str(exc)[:500]})
    _append_eval_log(cr, eval_id, "%s FAILED: %s" % (field, str(exc)[:200]))
    cr.commit()


def _open_cursor(db_name):
    import odoo  # noqa: deferred
    from odoo.modules.registry import Registry

    return Registry(db_name).cursor()


def _safe_worker(fn):
    """Decorator: release semaphore in finally, log crash to DB."""

    def wrapper(db_name, uid, eval_id):
        try:
            fn(db_name, uid, eval_id)
        except Exception as exc:
            _logger.exception("%s crashed for eval %s", fn.__name__, eval_id)
            try:
                with _open_cursor(db_name) as cr:
                    _update_eval(
                        cr,
                        eval_id,
                        {
                            "error_message": "%s crashed: %s"
                            % (fn.__name__, str(exc)[:500]),
                        },
                    )
                    cr.commit()
            except Exception:
                _logger.exception("Could not record crash for eval %s", eval_id)
        finally:
            _semaphore.release()

    wrapper.__name__ = fn.__name__
    return wrapper


# ── Public async entry points ───────────────────────────────────────────────


def submit_stage3_async(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning("Executor busy — cannot submit stage3 for eval %s", eval_id)
        return False
    _executor.submit(_run_stage3_background, db_name, uid, eval_id)
    return True


def submit_stub_async(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning("Executor busy — cannot submit stub for eval %s", eval_id)
        return False
    _executor.submit(_run_stub_background, db_name, uid, eval_id)
    return True


def submit_docker_async(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning("Executor busy — cannot submit docker for eval %s", eval_id)
        return False
    _executor.submit(_run_docker_background, db_name, uid, eval_id)
    return True


# ── Background workers ──────────────────────────────────────────────────────


@_safe_worker
def _run_stage3_background(db_name, uid, eval_id):
    import odoo

    with _open_cursor(db_name) as cr:
        env = odoo.api.Environment(cr, uid, {})
        tools_path = get_tools_path(env)
        cfg = _get_config(env)
        _ensure_tools_on_path(tools_path)

        cr.execute(
            "SELECT repo_url, repo_name, src_dir"
            " FROM commit0_repo_evaluation WHERE id = %s",
            [eval_id],
        )
        row = cr.fetchone()
        if not row:
            _logger.error("Evaluation %s not found", eval_id)
            return
        repo_url, repo_name, src_dir = row

        full_name = (repo_url or "").rstrip("/").replace("https://github.com/", "")
        if full_name.endswith(".git"):
            full_name = full_name[:-4]
        token = cfg["github_token"] or os.environ.get("GITHUB_TOKEN", "")
        org = cfg["github_org"] or "Ethara-Ai"
        if token:
            os.environ["GITHUB_TOKEN"] = token

        import base64
        import glob
        import json
        import shutil
        import subprocess

        try:
            from tools.prepare_repo import (
                fork_repo,
                full_clone,
                create_stubbed_branch,
                generate_setup_dict,
                generate_test_dict,
                push_to_fork,
                create_dataset_entry,
            )
        except ImportError:
            _fail_eval(cr, eval_id, "fork_status", ImportError("tools.prepare_repo"))
            return

        _append_eval_log(cr, eval_id, "=== STAGE 3 — Automated Preparation ===")
        cr.commit()

        # Step 1: FORK
        try:
            _update_eval(cr, eval_id, {"fork_status": "running"})
            cr.commit()
            _append_eval_log(cr, eval_id, "Forking %s → %s..." % (full_name, org))
            cr.commit()
            fork_name = fork_repo(full_name, org, token=token or None)
            fork_url = "https://github.com/%s" % fork_name
            _update_eval(
                cr,
                eval_id,
                {"fork_status": "done", "fork_progress": 100.0, "fork_url": fork_url},
            )
            cr.commit()
            _append_eval_log(cr, eval_id, "Fork complete: %s" % fork_url)
            cr.commit()
        except Exception as exc:
            _fail_eval(cr, eval_id, "fork_status", exc)
            return

        # Step 2: REFERENCE COMMIT
        try:
            _update_eval(
                cr,
                eval_id,
                {
                    "reference_commit_status": "running",
                    "reference_commit_progress": 10.0,
                },
            )
            cr.commit()
            clone_dir = Path(tempfile.mkdtemp(prefix="commit0_eval_"))
            repo_dir = full_clone(full_name, clone_dir)
            _append_eval_log(cr, eval_id, "Clone complete, creating stubbed branch...")
            _update_eval(cr, eval_id, {"reference_commit_progress": 40.0})
            cr.commit()
            base_commit, reference_commit = create_stubbed_branch(
                repo_dir, full_name, src_dir or None, removal_mode="all"
            )
            setup = generate_setup_dict(repo_dir, full_name)
            test_dict = generate_test_dict(repo_dir, setup.get("test_dir") or "tests")
            _update_eval(
                cr,
                eval_id,
                {
                    "reference_commit_status": "done",
                    "reference_commit_progress": 100.0,
                    "base_commit": base_commit,
                    "reference_commit": reference_commit,
                    "clone_path": str(repo_dir),
                    "clone_path_original": str(repo_dir),
                    "src_dir": setup.get("src_dir") or src_dir or "",
                    "test_dir": test_dict.get("test_dir")
                    or setup.get("test_dir")
                    or "tests",
                    "python_version": setup.get("python") or "3.12",
                    "install_cmd": setup.get("install") or "",
                },
            )
            cr.commit()
            _append_eval_log(
                cr,
                eval_id,
                "ref=%s base=%s" % (reference_commit[:12], base_commit[:12]),
            )
            cr.commit()
        except Exception as exc:
            _fail_eval(cr, eval_id, "reference_commit_status", exc)
            return

        # Step 3: DOCUMENT (spec scraping, PDF commit, read PDF binary)
        spec_path = None
        specs_dir = None
        pdf_data = ""
        repo_short = full_name.split("/")[-1]
        try:
            _update_eval(
                cr,
                eval_id,
                {"document_create_status": "running", "document_create_progress": 10.0},
            )
            cr.commit()
            try:
                from tools.scrape_pdf import scrape_spec_sync

                spec_url = setup.get("specification") or ""
                if spec_url:
                    specs_dir = Path(tempfile.mkdtemp(prefix="commit0_specs_"))
                    _append_eval_log(cr, eval_id, "Scraping spec from %s" % spec_url)
                    _update_eval(cr, eval_id, {"document_create_progress": 30.0})
                    cr.commit()
                    spec_path = scrape_spec_sync(
                        spec_url=spec_url,
                        repo_name=repo_short,
                        output_dir=str(specs_dir),
                        github_token=token,
                        full_name=full_name,
                    )
                    _update_eval(cr, eval_id, {"document_create_progress": 70.0})
                    cr.commit()
                    if spec_path:
                        _append_eval_log(cr, eval_id, "Spec scraped: %s" % spec_path)
                    else:
                        _append_eval_log(cr, eval_id, "Spec scrape produced no PDF")
                else:
                    _append_eval_log(cr, eval_id, "No spec URL — skipping scrape")
            except ImportError:
                _logger.warning("scrape_pdf unavailable for eval %s", eval_id)
                _append_eval_log(cr, eval_id, "scrape_pdf not available — skipping")
            cr.commit()

            if spec_path:
                shutil.copy2(spec_path, repo_dir / Path(spec_path).name)
                subprocess.run(
                    ["git", "add", Path(spec_path).name],
                    cwd=repo_dir,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "Add spec PDF for %s" % repo_short],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                )
                base_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                _update_eval(cr, eval_id, {"base_commit": base_commit})
                _append_eval_log(
                    cr,
                    eval_id,
                    "Updated base_commit with spec: %s" % base_commit[:12],
                )
                cr.commit()

            if spec_path:
                with open(spec_path, "rb") as f:
                    pdf_data = base64.b64encode(f.read()).decode()

            _update_eval(
                cr,
                eval_id,
                {
                    "document_create_status": "done",
                    "document_create_progress": 100.0,
                    "specs_dir": str(specs_dir) if specs_dir else "",
                },
            )
            cr.commit()
        except Exception as exc:
            _fail_eval(cr, eval_id, "document_create_status", exc)
            return

        # Step 4: PUSH TO FORK
        try:
            _append_eval_log(cr, eval_id, "Pushing to fork...")
            cr.commit()
            push_to_fork(
                repo_dir,
                fork_name,
                branch="commit0_all",
                token=token or None,
            )
            _append_eval_log(cr, eval_id, "Push complete")
            cr.commit()
        except Exception as exc:
            _append_eval_log(
                cr,
                eval_id,
                "Push to fork failed (non-fatal): %s" % str(exc)[:200],
            )
            cr.commit()

        # Step 5: GENERATE JSON + YAML + POPULATE STAGE 4 FIELDS
        try:
            dataset_entry = create_dataset_entry(
                full_name=full_name,
                fork_name=fork_name,
                base_commit=base_commit,
                reference_commit=reference_commit,
                src_dir=setup.get("src_dir") or src_dir or "",
                setup_dict=setup,
                test_dict=test_dict,
            )
            spec_json = json.dumps(dataset_entry, indent=2)

            try:
                from tools.create_dataset import generate_commit0_yaml

                spec_yaml = generate_commit0_yaml(
                    [dataset_entry],
                    "custom",
                    "Ethara-Ai/commit0_custom",
                )
            except ImportError:
                spec_yaml = ""

            updates = {
                "spec_json": spec_json,
                "spec_yaml": spec_yaml,
            }
            _update_eval(cr, eval_id, updates)
            cr.commit()

            # Binary field with attachment=True needs ORM, not raw SQL
            if pdf_data:
                spec_pdf_filename = (
                    repo_short + ".pdf.bz2"
                    if spec_path and spec_path.endswith(".bz2")
                    else repo_short + ".pdf"
                )
                evaluation = env["commit0.repo.evaluation"].browse(eval_id)
                evaluation.write(
                    {
                        "spec_pdf": pdf_data,
                        "spec_pdf_filename": spec_pdf_filename,
                    }
                )
                cr.commit()

            _append_eval_log(
                cr,
                eval_id,
                "Stage 4 fields populated (json=%d chars, yaml=%d chars, pdf=%s)"
                % (len(spec_json), len(spec_yaml), "yes" if pdf_data else "no"),
            )
            cr.commit()
        except Exception as exc:
            _append_eval_log(
                cr,
                eval_id,
                "Dataset entry generation failed (non-fatal): %s" % str(exc)[:200],
            )
            cr.commit()

        # All steps done → advance
        _update_eval(cr, eval_id, {"current_stage": "stage4"})
        cr.commit()
        _append_eval_log(cr, eval_id, "Stage 3 complete — advancing to Stage 4")
        cr.commit()


@_safe_worker
def _run_stub_background(db_name, uid, eval_id):
    import odoo

    with _open_cursor(db_name) as cr:
        env = odoo.api.Environment(cr, uid, {})
        _ensure_tools_on_path(get_tools_path(env))

        cr.execute(
            "SELECT clone_path, src_dir FROM commit0_repo_evaluation WHERE id = %s",
            [eval_id],
        )
        row = cr.fetchone()
        if not row:
            _logger.error("Evaluation %s not found", eval_id)
            return
        clone_path, src_dir = row

        if not clone_path or not Path(clone_path).is_dir():
            _fail_eval(
                cr,
                eval_id,
                "stub_status",
                ValueError("Clone path missing: %s" % clone_path),
            )
            return
        try:
            from tools.stub import stub_directory
        except ImportError:
            _fail_eval(cr, eval_id, "stub_status", ImportError("tools.stub"))
            return

        _update_eval(cr, eval_id, {"stub_status": "running"})
        cr.commit()
        _append_eval_log(cr, eval_id, "=== STUBBING ===")
        cr.commit()

        stubbed_dir = Path(tempfile.mkdtemp(prefix="commit0_stubbed_"))
        source_dir = Path(clone_path) / src_dir if src_dir else Path(clone_path)
        _append_eval_log(cr, eval_id, "Stubbing %s → %s" % (source_dir, stubbed_dir))
        cr.commit()

        stats = stub_directory(
            source_dir=source_dir,
            output_dir=stubbed_dir,
            keep_docstrings=True,
            removal_mode="all",
        )
        _update_eval(
            cr, eval_id, {"clone_path_stubbed": str(stubbed_dir), "stub_status": "done"}
        )
        cr.commit()
        _append_eval_log(
            cr,
            eval_id,
            "Done: %d files, %d stubbed"
            % (stats.get("total_files", 0), stats.get("modified_files", 0)),
        )
        cr.commit()


@_safe_worker
def _run_docker_background(db_name, uid, eval_id):
    import odoo  # noqa: deferred
    from odoo.modules.registry import Registry

    with _open_cursor(db_name) as cr:
        _append_eval_log(cr, eval_id, "=== DOCKER GENERATION via Kaiju Build ===")
        _update_eval(
            cr, eval_id, {"docker_status": "generating", "docker_progress": 5.0}
        )
        cr.commit()

        cr.execute(
            "SELECT repo_name, spec_json FROM commit0_repo_evaluation WHERE id = %s",
            [eval_id],
        )
        row = cr.fetchone()
        if not row:
            _fail_eval(
                cr, eval_id, "docker_status", Exception("Evaluation record not found")
            )
            return
        repo_name, dataset_json = row[0], row[1]

        if not dataset_json:
            _fail_eval(
                cr,
                eval_id,
                "docker_status",
                Exception(
                    "Dataset JSON (spec_json) is empty — Stage 3 may not have completed"
                ),
            )
            return

        _append_eval_log(cr, eval_id, "Repo: %s" % repo_name)
        cr.commit()

    registry = Registry(db_name)
    with registry.cursor() as env_cr:
        env = odoo.api.Environment(env_cr, uid, {})

        app = env["kaiju.app"].search([("name", "=", repo_name)], limit=1)
        if not app:
            app = env["kaiju.app"].create(
                {
                    "name": repo_name,
                    "repo_url": "",
                }
            )
        env_cr.commit()

        build = env["kaiju.build"].create(
            {
                "app_id": app.id,
                "repo_name": repo_name,
                "dataset_json": dataset_json,
            }
        )
        env_cr.commit()

        try:
            build.action_build()
            env_cr.commit()
        except Exception as exc:
            env_cr.rollback()
            with _open_cursor(db_name) as cr:
                _fail_eval(cr, eval_id, "docker_status", exc)
            return

        build_id = build.id
        env_cr.commit()

    with _open_cursor(db_name) as cr:
        _append_eval_log(cr, eval_id, "Kaiju build started, polling for status...")
        _update_eval(
            cr, eval_id, {"docker_status": "generating", "docker_progress": 10.0}
        )
        cr.commit()

    timeout_seconds = 45 * 60
    poll_interval = 10
    elapsed = 0
    current_status = "queued"
    current_image = ""
    current_error = ""

    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval

        with registry.cursor() as env_cr:
            env = odoo.api.Environment(env_cr, uid, {})
            build = env["kaiju.build"].browse(build_id)
            current_status = build.status
            current_image = build.image_uri or ""
            current_error = build.error_message or ""

        if current_status in ("success", "failed", "error"):
            break

        progress_pct = min(10.0 + (elapsed / timeout_seconds) * 80.0, 90.0)
        docker_status = "multiarch" if elapsed > 30 else "generating"

        with _open_cursor(db_name) as cr:
            _update_eval(
                cr,
                eval_id,
                {
                    "docker_status": docker_status,
                    "docker_progress": progress_pct,
                },
            )
            if elapsed % 60 == 0:
                _append_eval_log(
                    cr,
                    eval_id,
                    "Build status: %s (elapsed %ds)" % (current_status, elapsed),
                )
            cr.commit()

    with _open_cursor(db_name) as cr:
        if current_status == "success":
            _update_eval(
                cr,
                eval_id,
                {
                    "docker_status": "done",
                    "docker_progress": 100.0,
                    "docker_image_arm": current_image,
                    "docker_image_amd": current_image,
                    "ecr_url": current_image,
                    "current_stage": "done",
                    "terminal_state": "complete",
                },
            )
            cr.commit()
            _append_eval_log(cr, eval_id, "Docker complete — image: %s" % current_image)
            cr.commit()
        elif current_status in ("failed", "error"):
            _update_eval(
                cr,
                eval_id,
                {
                    "docker_status": "failed",
                    "docker_progress": 0.0,
                    "error_message": current_error[:500]
                    if current_error
                    else "Build failed",
                },
            )
            cr.commit()
            _append_eval_log(cr, eval_id, "Docker FAILED: %s" % current_error[:200])
            cr.commit()
        else:
            _update_eval(
                cr,
                eval_id,
                {
                    "docker_status": "failed",
                    "docker_progress": 0.0,
                    "error_message": "Build timed out after %d seconds"
                    % timeout_seconds,
                },
            )
            cr.commit()
            _append_eval_log(
                cr, eval_id, "Docker FAILED: timeout after %ds" % timeout_seconds
            )
            cr.commit()
