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
        "lambda_pdf_function_name": ICP.get_param(
            "commit0_pipeline.lambda_pdf_function_name", ""
        ),
        "lambda_pdf_region": ICP.get_param("commit0_pipeline.lambda_pdf_region", ""),
        "lambda_pdf_access_key": ICP.get_param(
            "commit0_pipeline.lambda_pdf_access_key", ""
        ),
        "lambda_pdf_secret_key": ICP.get_param(
            "commit0_pipeline.lambda_pdf_secret_key", ""
        ),
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
    import subprocess as _sp

    msg = str(exc)[:500]
    # Include stderr from CalledProcessError (git errors, etc.)
    if isinstance(exc, _sp.CalledProcessError) and exc.stderr:
        msg = "%s | stderr: %s" % (msg[:250], str(exc.stderr)[:250])
    _update_eval(cr, eval_id, {field: "failed", "error_message": msg})
    _append_eval_log(cr, eval_id, "%s FAILED: %s" % (field, msg[:300]))
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


def submit_fork_async(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning("Executor busy — cannot submit fork for eval %s", eval_id)
        return False
    _executor.submit(_run_fork_background, db_name, uid, eval_id)
    return True


def submit_reference_commit_async(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning(
            "Executor busy — cannot submit reference commit for eval %s", eval_id
        )
        return False
    _executor.submit(_run_reference_commit_background, db_name, uid, eval_id)
    return True


def submit_document_create_async(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning(
            "Executor busy — cannot submit document create for eval %s", eval_id
        )
        return False
    _executor.submit(_run_document_create_background, db_name, uid, eval_id)
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


def submit_test_ids_async(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning("Executor busy — cannot submit test_ids for eval %s", eval_id)
        return False
    _executor.submit(_run_test_ids_background, db_name, uid, eval_id)
    return True


def submit_docker_poll(db_name, uid, eval_id):
    if not _semaphore.acquire(blocking=False):
        _logger.warning(
            "Executor busy — cannot submit docker poll for eval %s", eval_id
        )
        return False
    _executor.submit(_run_docker_poll_background, db_name, uid, eval_id)
    return True


# ── Shared Stage 3 helpers ──────────────────────────────────────────────────


def _get_stage3_context(cr, env, eval_id):
    """Read common fields needed by Stage 3 sub-tasks."""
    _ensure_tools_on_path(get_tools_path(env))
    cfg = _get_config(env)
    cr.execute(
        "SELECT repo_url, repo_name, src_dir, fork_url, clone_path,"
        "       base_commit, reference_commit, specs_dir"
        " FROM commit0_repo_evaluation WHERE id = %s",
        [eval_id],
    )
    row = cr.fetchone()
    if not row:
        return None
    token = cfg["github_token"] or os.environ.get("GITHUB_TOKEN", "")
    org = cfg["github_org"] or "Ethara-Ai"
    if token:
        os.environ["GITHUB_TOKEN"] = token
    repo_url = row[0] or ""
    full_name = repo_url.rstrip("/").replace("https://github.com/", "")
    if full_name.endswith(".git"):
        full_name = full_name[:-4]
    return {
        "full_name": full_name,
        "repo_name": row[1],
        "src_dir": row[2] or "",
        "fork_url": row[3] or "",
        "clone_path": row[4] or "",
        "base_commit": row[5] or "",
        "reference_commit": row[6] or "",
        "specs_dir": row[7] or "",
        "token": token,
        "org": org,
        "lambda_pdf_function_name": cfg.get("lambda_pdf_function_name", ""),
        "lambda_pdf_region": cfg.get("lambda_pdf_region", ""),
        "lambda_pdf_access_key": cfg.get("lambda_pdf_access_key", ""),
        "lambda_pdf_secret_key": cfg.get("lambda_pdf_secret_key", ""),
    }


def _generate_and_store_test_ids(cr, env, eval_id, repo_dir, repo_short):
    """Collect pytest test IDs from the cloned repo and store the .bz2 as an attachment."""
    import base64
    import bz2 as _bz2
    import shutil
    import subprocess as _sp
    import venv

    _append_eval_log(cr, eval_id, "Collecting test IDs (creating temp venv)...")
    cr.commit()

    cr.execute(
        "SELECT test_dir, install_cmd FROM commit0_repo_evaluation WHERE id = %s",
        [eval_id],
    )
    row = cr.fetchone()
    test_dir = (row[0] if row and row[0] else None) or "tests"
    install_cmd = (row[1] if row and row[1] else None) or 'pip install -e "."'

    venv_dir = Path(tempfile.mkdtemp(prefix="commit0_testids_venv_"))
    try:
        test_ids = _collect_in_venv(
            cr, eval_id, repo_dir, venv_dir, test_dir, install_cmd
        )
    finally:
        shutil.rmtree(venv_dir, ignore_errors=True)

    if not test_ids:
        _append_eval_log(cr, eval_id, "No test IDs collected for %s" % repo_short)
        cr.commit()
        return

    content = "\n".join(test_ids)
    bz2_data_raw = _bz2.compress(content.encode("utf-8"))
    bz2_data_b64 = base64.b64encode(bz2_data_raw).decode()

    bz2_filename = repo_short + ".bz2"
    evaluation = env["commit0.repo.evaluation"].browse(eval_id)
    evaluation.write(
        {
            "test_ids_bz2": bz2_data_b64,
            "test_ids_filename": bz2_filename,
            "test_ids_count": len(test_ids),
        }
    )
    cr.commit()

    _append_eval_log(
        cr,
        eval_id,
        "Test IDs generated: %d tests → %s" % (len(test_ids), bz2_filename),
    )
    cr.commit()


def _install_test_deps(venv_python, repo_dir, _sp):
    pip_prefix = "%s -m pip install --quiet" % str(venv_python)

    for extra in ("test", "testing", "dev", "all"):
        try:
            r = _sp.run(
                ["bash", "-c", '%s -e ".[%s]"' % (pip_prefix, extra)],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode == 0:
                return
        except Exception:
            continue

    for req_file in (
        "requirements-test.txt",
        "requirements_test.txt",
        "requirements-dev.txt",
        "requirements_dev.txt",
        "test-requirements.txt",
        "test_requirements.txt",
    ):
        req_path = repo_dir / req_file
        if req_path.exists():
            try:
                _sp.run(
                    [
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--quiet",
                        "-r",
                        str(req_path),
                    ],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except Exception:
                pass
            return


def _collect_in_venv(cr, eval_id, repo_dir, venv_dir, test_dir, install_cmd):
    import subprocess as _sp

    venv_python = venv_dir / "bin" / "python"

    # Resolve the real Python binary (not a venv symlink) to avoid broken dylib paths
    real_python = os.path.realpath(sys.executable)

    try:
        _sp.run(
            [real_python, "-m", "venv", "--clear", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        _append_eval_log(cr, eval_id, "Venv creation failed: %s" % str(exc)[:200])
        cr.commit()
        return []

    if not venv_python.exists():
        _append_eval_log(cr, eval_id, "Venv python not found at %s" % venv_python)
        cr.commit()
        return []

    _append_eval_log(cr, eval_id, "Venv created, installing repo dependencies...")
    cr.commit()

    try:
        _sp.run(
            [str(venv_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        pass

    # install_cmd is e.g. 'pip install -e "."' — replace "pip" with the venv python
    pip_prefix = "%s -m pip install" % str(venv_python)
    pip_cmd = install_cmd.replace("pip install", pip_prefix)
    try:
        install_result = _sp.run(
            ["bash", "-c", pip_cmd],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if install_result.returncode != 0:
            _append_eval_log(
                cr,
                eval_id,
                "pip install failed (rc=%d): %s"
                % (install_result.returncode, install_result.stderr[-300:]),
            )
            cr.commit()
            # Continue anyway — some tests may still be collectable
    except _sp.TimeoutExpired:
        _append_eval_log(cr, eval_id, "pip install timed out after 300s")
        cr.commit()
        return []

    _install_test_deps(venv_python, repo_dir, _sp)

    try:
        _sp.run(
            [str(venv_python), "-m", "pip", "install", "--quiet", "pytest"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        pass

    _append_eval_log(cr, eval_id, "Dependencies installed, collecting test IDs...")
    cr.commit()

    test_ids: list[str] = []

    for mode, extra_args in [("verbose", []), ("quiet", ["-q", "--no-header"])]:
        cmd = (
            [
                str(venv_python),
                "-m",
                "pytest",
                "--collect-only",
                "--override-ini=addopts=",
                "-p",
                "no:cacheprovider",
            ]
            + extra_args
            + [test_dir]
        )

        try:
            result = _sp.run(
                cmd,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except _sp.TimeoutExpired:
            _append_eval_log(cr, eval_id, "pytest --collect-only (%s) timed out" % mode)
            cr.commit()
            continue

        if result.stderr:
            stderr_snippet = result.stderr.strip()[-300:]
            _append_eval_log(
                cr, eval_id, "pytest stderr (%s): %s" % (mode, stderr_snippet)
            )
            cr.commit()

        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if "::" not in line:
                continue
            if line.startswith(("=", "-")):
                continue
            if line.startswith("<") and "::" in line:
                parts = line.split("::")
                id_parts: list[str] = []
                for part in parts:
                    part = part.strip()
                    if part.startswith("<") and part.endswith(">"):
                        inner = part[1:-1]
                        idx = inner.find(" ")
                        id_parts.append(inner[idx + 1 :] if idx != -1 else inner)
                    elif part:
                        id_parts.append(part)
                if id_parts:
                    test_ids.append("::".join(id_parts))
            else:
                test_id = line.split(" ")[0]
                if test_id and "::" in test_id:
                    test_ids.append(test_id)

        if test_ids:
            _append_eval_log(
                cr,
                eval_id,
                "Collected %d test IDs in %s mode" % (len(test_ids), mode),
            )
            cr.commit()
            break

    return test_ids

    cr.execute("SELECT test_dir FROM commit0_repo_evaluation WHERE id = %s", [eval_id])
    row = cr.fetchone()
    test_dir = (row[0] if row and row[0] else None) or "tests"

    test_ids: list[str] = []

    for mode, extra_args in [("verbose", []), ("quiet", ["-q", "--no-header"])]:
        cmd = (
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "--override-ini=addopts=",
                "-p",
                "no:cacheprovider",
            ]
            + extra_args
            + [test_dir]
        )

        try:
            result = _sp.run(
                cmd,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except _sp.TimeoutExpired:
            _append_eval_log(cr, eval_id, "pytest --collect-only (%s) timed out" % mode)
            cr.commit()
            continue

        if result.stderr:
            _append_eval_log(
                cr,
                eval_id,
                "pytest stderr (%s): %s" % (mode, result.stderr[:300]),
            )
            cr.commit()

        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if "::" not in line:
                continue
            if line.startswith(("=", "-")):
                continue
            if line.startswith("<") and "::" in line:
                parts = line.split("::")
                id_parts: list[str] = []
                for part in parts:
                    part = part.strip()
                    if part.startswith("<") and part.endswith(">"):
                        inner = part[1:-1]
                        idx = inner.find(" ")
                        id_parts.append(inner[idx + 1 :] if idx != -1 else inner)
                    elif part:
                        id_parts.append(part)
                if id_parts:
                    test_ids.append("::".join(id_parts))
            else:
                test_id = line.split(" ")[0]
                if test_id and "::" in test_id:
                    test_ids.append(test_id)

        if test_ids:
            _append_eval_log(
                cr,
                eval_id,
                "Collected %d test IDs in %s mode" % (len(test_ids), mode),
            )
            cr.commit()
            break

    if not test_ids:
        _append_eval_log(
            cr,
            eval_id,
            "No test IDs collected for %s (pytest may need dependencies installed)"
            % repo_short,
        )
        cr.commit()
        return

    content = "\n".join(test_ids)
    bz2_data_raw = _bz2.compress(content.encode("utf-8"))
    bz2_data_b64 = base64.b64encode(bz2_data_raw).decode()

    bz2_filename = repo_short + ".bz2"
    evaluation = env["commit0.repo.evaluation"].browse(eval_id)
    evaluation.write(
        {
            "test_ids_bz2": bz2_data_b64,
            "test_ids_filename": bz2_filename,
            "test_ids_count": len(test_ids),
        }
    )
    cr.commit()

    _append_eval_log(
        cr,
        eval_id,
        "Test IDs generated: %d tests → %s" % (len(test_ids), bz2_filename),
    )
    cr.commit()


@_safe_worker
def _run_test_ids_background(db_name, uid, eval_id):
    import odoo

    with _open_cursor(db_name) as cr:
        env = odoo.api.Environment(cr, uid, {})
        _ensure_tools_on_path(get_tools_path(env))

        cr.execute(
            "SELECT clone_path, repo_url FROM commit0_repo_evaluation WHERE id = %s",
            [eval_id],
        )
        row = cr.fetchone()
        if not row:
            _logger.error("Evaluation %s not found", eval_id)
            return

        clone_path, repo_url = row
        if not clone_path or not Path(clone_path).is_dir():
            _update_eval(
                cr,
                eval_id,
                {
                    "test_ids_status": "failed",
                    "error_message": "Clone path missing: %s" % clone_path,
                },
            )
            cr.commit()
            return

        full_name = (repo_url or "").rstrip("/").replace("https://github.com/", "")
        if full_name.endswith(".git"):
            full_name = full_name[:-4]
        repo_short = full_name.split("/")[-1]
        repo_dir = Path(clone_path)

        _update_eval(cr, eval_id, {"test_ids_status": "running"})
        cr.commit()

        _generate_and_store_test_ids(cr, env, eval_id, repo_dir, repo_short)

        cr.execute(
            "SELECT test_ids_count FROM commit0_repo_evaluation WHERE id = %s",
            [eval_id],
        )
        count_row = cr.fetchone()
        count = count_row[0] if count_row else 0

        if count > 0:
            _update_eval(cr, eval_id, {"test_ids_status": "done"})
        else:
            _update_eval(cr, eval_id, {"test_ids_status": "failed"})
        cr.commit()


# ── Individual Stage 3 background workers ───────────────────────────────────


@_safe_worker
def _run_fork_background(db_name, uid, eval_id):
    """Step 1: Fork the public repo into the org."""
    import odoo

    with _open_cursor(db_name) as cr:
        env = odoo.api.Environment(cr, uid, {})
        ctx = _get_stage3_context(cr, env, eval_id)
        if not ctx:
            _logger.error("Evaluation %s not found", eval_id)
            return

        try:
            from tools.prepare_repo import fork_repo
        except ImportError:
            _fail_eval(cr, eval_id, "fork_status", ImportError("tools.prepare_repo"))
            return

        _update_eval(cr, eval_id, {"fork_status": "running"})
        cr.commit()
        _append_eval_log(
            cr, eval_id, "Forking %s → %s..." % (ctx["full_name"], ctx["org"])
        )
        cr.commit()

        try:
            fork_name = fork_repo(
                ctx["full_name"], ctx["org"], token=ctx["token"] or None
            )
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


@_safe_worker
def _run_reference_commit_background(db_name, uid, eval_id):
    """Step 2: Clone, create stubbed branch, extract setup/test info."""
    import odoo

    with _open_cursor(db_name) as cr:
        env = odoo.api.Environment(cr, uid, {})
        ctx = _get_stage3_context(cr, env, eval_id)
        if not ctx:
            _logger.error("Evaluation %s not found", eval_id)
            return

        try:
            from tools.prepare_repo import (
                full_clone,
                create_stubbed_branch,
                generate_setup_dict,
                generate_test_dict,
                push_to_fork,
                git as repo_git,
                get_default_branch,
            )
        except ImportError:
            _fail_eval(
                cr,
                eval_id,
                "reference_commit_status",
                ImportError("tools.prepare_repo"),
            )
            return

        _update_eval(
            cr,
            eval_id,
            {"reference_commit_status": "running", "reference_commit_progress": 10.0},
        )
        cr.commit()
        _append_eval_log(
            cr, eval_id, "Cloning %s for reference commit..." % ctx["full_name"]
        )
        cr.commit()

        try:
            clone_dir = Path(tempfile.mkdtemp(prefix="commit0_eval_"))
            repo_dir = full_clone(ctx["full_name"], clone_dir)
            _append_eval_log(cr, eval_id, "Clone complete, creating stubbed branch...")
            _update_eval(cr, eval_id, {"reference_commit_progress": 40.0})
            cr.commit()

            base_commit, reference_commit = create_stubbed_branch(
                repo_dir,
                ctx["full_name"],
                ctx["src_dir"] or None,
                removal_mode="all",
            )

            fork_url = ctx["fork_url"]
            if fork_url:
                fork_name = fork_url.replace("https://github.com/", "")
                _append_eval_log(
                    cr,
                    eval_id,
                    "Pushing commit0_all to %s..." % fork_name,
                )
                cr.commit()
                try:
                    push_to_fork(
                        repo_dir,
                        fork_name,
                        branch="commit0_all",
                        token=ctx["token"] or None,
                    )
                    _append_eval_log(cr, eval_id, "Push complete")
                    cr.commit()
                except Exception as exc_push:
                    import subprocess as _sp

                    stderr_msg = ""
                    if isinstance(exc_push, _sp.CalledProcessError) and exc_push.stderr:
                        stderr_msg = " | stderr: %s" % str(exc_push.stderr)[:300]
                    _append_eval_log(
                        cr,
                        eval_id,
                        "Push to fork failed (non-fatal): %s%s"
                        % (str(exc_push)[:200], stderr_msg),
                    )
                    cr.commit()

            default_branch = get_default_branch(repo_dir)
            repo_git(repo_dir, "checkout", default_branch)
            setup = generate_setup_dict(repo_dir, ctx["full_name"])
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
                    "src_dir": setup.get("src_dir") or ctx["src_dir"] or "",
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


@_safe_worker
def _run_document_create_background(db_name, uid, eval_id):
    """Step 3: Scrape spec PDF, push to fork, generate JSON/YAML."""
    import base64
    import json

    import odoo

    with _open_cursor(db_name) as cr:
        env = odoo.api.Environment(cr, uid, {})
        ctx = _get_stage3_context(cr, env, eval_id)
        if not ctx:
            _logger.error("Evaluation %s not found", eval_id)
            return

        if not ctx["clone_path"] or not Path(ctx["clone_path"]).is_dir():
            _fail_eval(
                cr,
                eval_id,
                "document_create_status",
                ValueError(
                    "Clone path missing — run 'Commit Reference Code' first. "
                    "Path: %s" % ctx["clone_path"]
                ),
            )
            return

        if not ctx["fork_url"]:
            _fail_eval(
                cr,
                eval_id,
                "document_create_status",
                ValueError("Fork URL missing — run 'Fork Repo' first."),
            )
            return

        try:
            from tools.prepare_repo import (
                generate_setup_dict,
                generate_test_dict,
                push_to_fork,
                create_dataset_entry,
            )
        except ImportError:
            _fail_eval(
                cr,
                eval_id,
                "document_create_status",
                ImportError("tools.prepare_repo"),
            )
            return

        repo_dir = Path(ctx["clone_path"])
        full_name = ctx["full_name"]
        fork_url = ctx["fork_url"]
        fork_name = fork_url.replace("https://github.com/", "")
        token = ctx["token"]
        base_commit = ctx["base_commit"]
        reference_commit = ctx["reference_commit"]
        src_dir = ctx["src_dir"]
        repo_short = full_name.split("/")[-1]

        _update_eval(
            cr,
            eval_id,
            {"document_create_status": "running", "document_create_progress": 10.0},
        )
        cr.commit()
        _append_eval_log(cr, eval_id, "=== Document creation ===")
        cr.commit()

        # Scrape spec PDF
        spec_path = None
        specs_dir = None
        pdf_data = ""
        try:
            from tools.scrape_pdf import scrape_spec_sync

            setup = generate_setup_dict(repo_dir, full_name)
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
                    lambda_function_name=ctx.get("lambda_pdf_function_name", ""),
                    lambda_region=ctx.get("lambda_pdf_region", ""),
                    lambda_access_key=ctx.get("lambda_pdf_access_key", ""),
                    lambda_secret_key=ctx.get("lambda_pdf_secret_key", ""),
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
        except Exception as exc:
            _append_eval_log(
                cr,
                eval_id,
                "Spec scrape failed (non-fatal): %s" % str(exc)[:200],
            )
        cr.commit()

        # Commit spec PDF to the commit0_all branch (so it lives alongside
        # the stubbed code), but do NOT update base_commit — it must stay
        # pointing at the original stubbed commit for the build pipeline.
        if spec_path:
            import shutil

            try:
                from tools.prepare_repo import (
                    git as repo_git,
                    get_default_branch,
                )

                repo_git(repo_dir, "checkout", "commit0_all")
                dest = repo_dir / Path(spec_path).name
                shutil.copy2(spec_path, dest)
                repo_git(repo_dir, "add", dest.name)
                repo_git(
                    repo_dir,
                    "commit",
                    "-m",
                    "Add spec PDF for %s" % repo_short,
                )
                _append_eval_log(
                    cr,
                    eval_id,
                    "Spec PDF committed on commit0_all (base_commit unchanged)",
                )
                cr.commit()
                # Restore to default branch for the file browser
                default_branch = get_default_branch(repo_dir)
                repo_git(repo_dir, "checkout", default_branch)
            except Exception as exc:
                _append_eval_log(
                    cr,
                    eval_id,
                    "Spec PDF git commit failed (non-fatal): %s" % str(exc)[:200],
                )
                cr.commit()

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

        # Push to fork
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

        # Generate JSON + YAML + populate Stage 4 fields
        try:
            setup = generate_setup_dict(repo_dir, full_name)
            test_dict = generate_test_dict(repo_dir, setup.get("test_dir") or "tests")
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

            _update_eval(cr, eval_id, {"spec_json": spec_json, "spec_yaml": spec_yaml})
            cr.commit()

            # Binary field with attachment=True needs ORM
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

        cr.execute(
            "SELECT fork_status, reference_commit_status, document_create_status"
            " FROM commit0_repo_evaluation WHERE id = %s",
            [eval_id],
        )
        row = cr.fetchone()
        if row and row[0] == "done" and row[1] == "done" and row[2] == "done":
            _update_eval(cr, eval_id, {"current_stage": "stage4"})
            cr.commit()
            _append_eval_log(cr, eval_id, "Stage 3 complete — advancing to Stage 4")
            cr.commit()


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

        try:
            from tools.prepare_repo import (
                fork_repo,
                full_clone,
                create_stubbed_branch,
                generate_setup_dict,
                generate_test_dict,
                push_to_fork,
                create_dataset_entry,
                git as repo_git,
                get_default_branch,
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
            # Restore working tree to original code so clone_path / clone_path_original
            # serve the unstubbed source in the file browsers.
            default_branch = get_default_branch(repo_dir)
            repo_git(repo_dir, "checkout", default_branch)
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
                        lambda_function_name=cfg.get("lambda_pdf_function_name", ""),
                        lambda_region=cfg.get("lambda_pdf_region", ""),
                        lambda_access_key=cfg.get("lambda_pdf_access_key", ""),
                        lambda_secret_key=cfg.get("lambda_pdf_secret_key", ""),
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
            except Exception as exc_scrape:
                _append_eval_log(
                    cr,
                    eval_id,
                    "Spec scrape failed (non-fatal): %s" % str(exc_scrape)[:200],
                )
            cr.commit()

            # Commit spec PDF to commit0_all (do NOT update base_commit).
            if spec_path:
                import shutil as _shutil

                try:
                    repo_git(repo_dir, "checkout", "commit0_all")
                    dest = repo_dir / Path(spec_path).name
                    _shutil.copy2(spec_path, dest)
                    repo_git(repo_dir, "add", dest.name)
                    repo_git(
                        repo_dir,
                        "commit",
                        "-m",
                        "Add spec PDF for %s" % repo_short,
                    )
                    _append_eval_log(
                        cr,
                        eval_id,
                        "Spec PDF committed on commit0_all (base_commit unchanged)",
                    )
                    cr.commit()
                    repo_git(repo_dir, "checkout", default_branch)
                except Exception as exc_pdf:
                    _append_eval_log(
                        cr,
                        eval_id,
                        "Spec PDF git commit failed (non-fatal): %s"
                        % str(exc_pdf)[:200],
                    )
                    cr.commit()

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
    """Mark stubbing as done — stubs already exist on the commit0_all branch
    (created during Stage 3 by create_stubbed_branch).  No temp directory needed;
    the Stage 5 UI reads directly from the git tree at base_commit."""
    with _open_cursor(db_name) as cr:
        cr.execute(
            "SELECT clone_path, base_commit FROM commit0_repo_evaluation WHERE id = %s",
            [eval_id],
        )
        row = cr.fetchone()
        if not row:
            _logger.error("Evaluation %s not found", eval_id)
            return
        clone_path, base_commit = row

        if not clone_path or not Path(clone_path).is_dir():
            _fail_eval(
                cr,
                eval_id,
                "stub_status",
                ValueError("Clone path missing: %s" % clone_path),
            )
            return

        if not base_commit:
            _fail_eval(
                cr,
                eval_id,
                "stub_status",
                ValueError("base_commit missing — Stage 3 may not have completed"),
            )
            return

        _update_eval(cr, eval_id, {"stub_status": "running"})
        cr.commit()
        _append_eval_log(cr, eval_id, "=== STUB REVIEW (git-based) ===")
        cr.commit()

        # Verify the base_commit (stubbed code) is reachable in the clone
        try:
            import subprocess as _sp

            result = _sp.run(
                ["git", "cat-file", "-t", base_commit],
                cwd=clone_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                _fail_eval(
                    cr,
                    eval_id,
                    "stub_status",
                    ValueError(
                        "base_commit %s not found in clone — "
                        "run Stage 3 first" % base_commit[:12]
                    ),
                )
                return
        except Exception as exc:
            _fail_eval(cr, eval_id, "stub_status", exc)
            return

        _update_eval(cr, eval_id, {"stub_status": "done"})
        cr.commit()
        _append_eval_log(
            cr,
            eval_id,
            "Stub review ready — viewing base_commit %s via git tree"
            % base_commit[:12],
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

        eval_rec = env["commit0.repo.evaluation"].browse(eval_id)
        eval_rec.write({"kaiju_build_id": build_id})
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

        if current_status in ("success", "failed", "error", "image_broken"):
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
        elif current_status == "image_broken":
            _update_eval(
                cr,
                eval_id,
                {
                    "docker_status": "image_broken",
                    "docker_progress": 0.0,
                    "error_message": current_error[:500]
                    if current_error
                    else "Image broken — test gate failed",
                },
            )
            cr.commit()
            _append_eval_log(
                cr, eval_id, "Docker IMAGE BROKEN: %s" % current_error[:200]
            )
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


@_safe_worker
def _run_docker_poll_background(db_name, uid, eval_id):
    import odoo  # noqa: deferred
    from odoo.modules.registry import Registry

    registry = Registry(db_name)

    with registry.cursor() as env_cr:
        env = odoo.api.Environment(env_cr, uid, {})
        eval_rec = env["commit0.repo.evaluation"].browse(eval_id)
        if not eval_rec.kaiju_build_id:
            with _open_cursor(db_name) as cr:
                _fail_eval(cr, eval_id, "docker_status", Exception("No linked build"))
            return
        build_id = eval_rec.kaiju_build_id.id

    with _open_cursor(db_name) as cr:
        _append_eval_log(cr, eval_id, "Rebuild started, polling for status...")
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

        if current_status in ("success", "failed", "error", "image_broken"):
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
        elif current_status == "image_broken":
            _update_eval(
                cr,
                eval_id,
                {
                    "docker_status": "image_broken",
                    "docker_progress": 0.0,
                    "error_message": current_error[:500]
                    if current_error
                    else "Image broken — test gate failed",
                },
            )
            cr.commit()
            _append_eval_log(
                cr, eval_id, "Docker IMAGE BROKEN: %s" % current_error[:200]
            )
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
