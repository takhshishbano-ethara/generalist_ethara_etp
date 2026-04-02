# -*- coding: utf-8 -*-
"""Background pipeline execution service.

Uses ThreadPoolExecutor to run commit0 CLI tools as subprocesses
without blocking the Odoo HTTP worker.
"""

import atexit
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from odoo import fields

_logger = logging.getLogger(__name__)

# Module-level thread pool (2 workers — Docker builds are heavy)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="commit0")
_semaphore = threading.Semaphore(4)


def _shutdown_executor():
    _executor.shutdown(wait=False)


atexit.register(_shutdown_executor)


def get_tools_path(env):
    """Get the path to commit0 tools directory."""
    param = (
        env["ir.config_parameter"].sudo().get_param("commit0_pipeline.tools_path", "")
    )
    if param:
        return param
    # Default: tools/ inside this module
    module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(module_path, "tools")


def submit_pipeline_async(db_name, uid, run_id):
    """Submit a pipeline run for background execution.

    Args:
        db_name: Database name for creating new cursor
        uid: User ID for the run
        run_id: ID of the commit0.pipeline.run record
    """
    if not _semaphore.acquire(blocking=False):
        _logger.warning("Pipeline semaphore full, rejecting run %s", run_id)
        return False

    _executor.submit(_run_pipeline_background, db_name, uid, run_id)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _append_log(run, cr, message):
    """Atomically append a log message to the pipeline run and commit."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = "[%s] %s\n" % (timestamp, message)
    current = run.log_output or ""
    run.write({"log_output": current + line})
    cr.commit()
    _logger.info("Run %s: %s", run.id, message)


def _set_state(run, cr, state, extra_vals=None):
    """Transition pipeline run state and commit."""
    vals = {"state": state}
    if extra_vals:
        vals.update(extra_vals)
    run.write(vals)
    cr.commit()


def _is_cancelled(run, cr):
    """Re-read the run record to check if user cancelled mid-execution."""
    cr.execute("SELECT state FROM commit0_pipeline_run WHERE id = %s", [run.id])
    row = cr.fetchone()
    return row and row[0] == "cancelled"


def _ensure_tools_on_path(tools_path):
    """Put the tools parent directory on sys.path so `from tools.X import Y` works."""
    parent = os.path.dirname(tools_path)
    if parent not in sys.path:
        sys.path.insert(0, parent)


def _get_config(env):
    """Read commit0_pipeline config parameters."""
    ICP = env["ir.config_parameter"].sudo()
    return {
        "github_token": ICP.get_param("commit0_pipeline.github_token", ""),
        "github_org": ICP.get_param("commit0_pipeline.github_org", "Ethara-Ai"),
    }


# ---------------------------------------------------------------------------
# State machine — SINGLE mode
# ---------------------------------------------------------------------------


def _run_single_discovering(run, cr, env, cfg, tools_path):
    """Discovering step for single-repo mode.

    If repo_url is provided directly, create one candidate from it.
    Otherwise search GitHub with discover tools.
    """
    _append_log(run, cr, "=== DISCOVERING ===")
    _ensure_tools_on_path(tools_path)

    token = cfg["github_token"] or os.environ.get("GITHUB_TOKEN", "")
    repo_url = run.repo_url or ""

    if repo_url:
        # Direct URL — extract owner/repo, create a single candidate
        full_name = repo_url.rstrip("/").replace("https://github.com/", "")
        if full_name.endswith(".git"):
            full_name = full_name[:-4]
        repo_name = full_name.split("/")[-1]

        _append_log(run, cr, "Direct repo URL provided: %s" % full_name)
        env["commit0.discovery.candidate"].create(
            {
                "pipeline_run_id": run.id,
                "full_name": full_name,
                "stars": 0,
                "python_pct": 0.0,
                "has_pytest": False,
                "description": "Direct entry from URL",
                "selected": True,
                "validation_status": "pending",
            }
        )
        cr.commit()
        _append_log(run, cr, "Created 1 candidate record for %s" % full_name)
        return

    # No direct URL — run GitHub search + enrichment
    from tools.discover import search_python_repos, enrich_candidates

    min_stars = run.min_stars or 5000
    max_results = run.max_results or 200

    _append_log(
        run,
        cr,
        "Searching GitHub: min_stars=%d, max_results=%d" % (min_stars, max_results),
    )
    repos = search_python_repos(
        min_stars=min_stars,
        max_results=max_results,
        token=token or None,
    )
    _append_log(run, cr, "Found %d raw repos from GitHub search" % len(repos))

    if _is_cancelled(run, cr):
        return

    _append_log(run, cr, "Enriching candidates (language + pytest check)...")
    candidates = enrich_candidates(repos, token=token or None)
    _append_log(
        run, cr, "Enrichment complete: %d candidates passed filters" % len(candidates)
    )

    # Create candidate records
    for cand in candidates:
        env["commit0.discovery.candidate"].create(
            {
                "pipeline_run_id": run.id,
                "full_name": cand.get("full_name", ""),
                "stars": cand.get("stars", 0),
                "python_pct": cand.get("python_pct", 0.0),
                "has_pytest": cand.get("has_pytest", False),
                "license": cand.get("license", ""),
                "description": (cand.get("description") or "")[:500],
                "release_tag": cand.get("release_tag", ""),
                "selected": True,
                "validation_status": "pending",
            }
        )
    cr.commit()
    _append_log(run, cr, "Created %d discovery candidate records" % len(candidates))


def _run_single_validating(run, cr, env, cfg, tools_path):
    """Validating step for single-repo mode — clone + structural analysis."""
    _append_log(run, cr, "=== VALIDATING ===")
    _ensure_tools_on_path(tools_path)

    from tools.validate import validate_candidates

    token = cfg["github_token"] or os.environ.get("GITHUB_TOKEN", "")
    clone_dir = Path(tempfile.mkdtemp(prefix="commit0_validate_"))

    candidates = run.candidate_ids.filtered(lambda c: c.selected)
    if not candidates:
        _append_log(run, cr, "No candidates to validate — skipping")
        return

    # Build list-of-dict for validate_candidates()
    cand_list = []
    for c in candidates:
        cand_list.append(
            {
                "full_name": c.full_name,
                "stars": c.stars or 0,
                "default_branch": "main",
            }
        )

    _append_log(
        run, cr, "Validating %d candidates (clone + analysis)..." % len(cand_list)
    )
    results = validate_candidates(cand_list, clone_dir=clone_dir)

    # Update candidate records with validation results
    for res in results:
        full_name = res.get("full_name", "")
        matching = candidates.filtered(lambda c: c.full_name == full_name)
        if matching:
            status = "pass" if res.get("status") == "pass" else "fail"
            issues = ", ".join(res.get("issues", []))
            matching[0].write(
                {
                    "validation_status": status,
                    "validation_issues": issues or False,
                }
            )
    cr.commit()

    passed = [r for r in results if r.get("status") == "pass"]
    failed = [r for r in results if r.get("status") != "pass"]
    _append_log(
        run,
        cr,
        "Validation complete: %d passed, %d failed" % (len(passed), len(failed)),
    )

    # Store clone_dir path for reuse in later steps (via run context)
    # We pass it back via a transient field on log
    _append_log(run, cr, "Clone directory: %s" % clone_dir)


def _run_single_preparing(run, cr, env, cfg, tools_path):
    """Preparing step for single-repo mode — fork, stub, push."""
    _append_log(run, cr, "=== PREPARING ===")
    _ensure_tools_on_path(tools_path)

    from tools.prepare_repo import prepare_repos

    token = cfg["github_token"] or ""
    org = cfg["github_org"] or "Ethara-Ai"
    removal_mode = run.stubbing_mode or "combined"
    clone_dir = Path(tempfile.mkdtemp(prefix="commit0_prepare_"))

    # Set GITHUB_TOKEN env for tools that read from os.environ
    if token:
        os.environ["GITHUB_TOKEN"] = token

    # Build candidate list from validated candidates
    validated = run.candidate_ids.filtered(lambda c: c.validation_status == "pass")
    if not validated:
        # If direct URL mode, all candidates are selected regardless of validation
        validated = run.candidate_ids.filtered(lambda c: c.selected)

    if not validated:
        _append_log(run, cr, "No validated candidates to prepare")
        return

    cand_list = []
    for c in validated:
        cand_list.append(
            {
                "full_name": c.full_name,
                "stars": c.stars or 0,
                "default_branch": "main",
                "status": "pass",
                "release_tag": c.release_tag or None,
                "analysis": None,  # will be detected during prepare
            }
        )

    _append_log(run, cr, "Preparing %d repos (fork + stub + push)..." % len(cand_list))

    entries = prepare_repos(
        cand_list,
        clone_dir=clone_dir,
        org=org,
        removal_mode=removal_mode,
    )

    _append_log(run, cr, "Prepare complete: %d dataset entries created" % len(entries))

    # Create commit0.repo.entry records
    for idx, entry in enumerate(entries):
        full_name = entry.get("original_repo", "")
        repo_name = full_name.split("/")[-1] if "/" in full_name else full_name
        fork_name = entry.get("repo", "")
        fork_url = "https://github.com/%s" % fork_name if fork_name else ""
        setup = entry.get("setup", {})
        test = entry.get("test", {})

        env["commit0.repo.entry"].create(
            {
                "pipeline_run_id": run.id,
                "sequence": (idx + 1) * 10,
                "repo_name": repo_name,
                "repo_url": "https://github.com/%s" % full_name if full_name else "",
                "fork_url": fork_url,
                "state": "dataset_created",
                "base_commit": entry.get("base_commit", ""),
                "reference_commit": entry.get("reference_commit", ""),
                "src_dir": entry.get("src_dir", ""),
                "test_dir": test.get("test_dir", "tests"),
                "python_version": setup.get("python", "3.12"),
                "install_cmd": setup.get("install", ""),
                "stubbing_mode": removal_mode,
                "clone_path": str(clone_dir / full_name.replace("/", "__"))
                if full_name
                else "",
            }
        )
    cr.commit()

    # Save dataset entries JSON for later steps
    dataset_dir = Path(tempfile.mkdtemp(prefix="commit0_dataset_"))
    dataset_path = dataset_dir / "dataset_entries.json"
    dataset_path.write_text(json.dumps(entries, indent=2))
    run.write({"entries_json_path": str(dataset_path)})
    cr.commit()
    _append_log(run, cr, "Saved entries JSON: %s" % dataset_path)


def _run_single_creating_dataset(run, cr, env, cfg, tools_path):
    """Creating dataset step — validate and write HF-compatible dataset JSON."""
    _append_log(run, cr, "=== CREATING DATASET ===")
    _ensure_tools_on_path(tools_path)

    from tools.create_dataset import validate_dataset, create_hf_dataset_dict

    entries_path = run.entries_json_path
    if not entries_path or not Path(entries_path).exists():
        _append_log(run, cr, "No entries JSON found — building from repo entries")
        # Reconstruct entries from repo entry records
        entries = []
        for re_entry in run.repo_entry_ids:
            full_name = ""
            if re_entry.repo_url:
                full_name = re_entry.repo_url.replace("https://github.com/", "")
            fork_name = ""
            if re_entry.fork_url:
                fork_name = re_entry.fork_url.replace("https://github.com/", "")
            entries.append(
                {
                    "instance_id": "commit-0/%s" % (re_entry.repo_name or ""),
                    "repo": fork_name,
                    "original_repo": full_name,
                    "base_commit": re_entry.base_commit or "",
                    "reference_commit": re_entry.reference_commit or "",
                    "setup": {
                        "install": re_entry.install_cmd or 'pip install -e "."',
                        "packages": "",
                        "pip_packages": ["pytest", "pytest-json-report"],
                        "pre_install": [],
                        "python": re_entry.python_version or "3.12",
                        "specification": "",
                    },
                    "test": {
                        "test_cmd": "pytest",
                        "test_dir": re_entry.test_dir or "tests",
                    },
                    "src_dir": re_entry.src_dir or "",
                }
            )
    else:
        entries = json.loads(Path(entries_path).read_text())

    valid, issues = validate_dataset(entries)
    if issues:
        _append_log(run, cr, "Dataset validation issues: %s" % "; ".join(issues[:10]))
    _append_log(
        run, cr, "Dataset validation: %d/%d entries valid" % (len(valid), len(entries))
    )

    hf_entries = create_hf_dataset_dict(valid)

    # Write dataset JSON
    dataset_dir = Path(tempfile.mkdtemp(prefix="commit0_hf_"))
    dataset_path = dataset_dir / "custom_dataset.json"
    dataset_path.write_text(json.dumps(hf_entries, indent=2))

    run.write({"dataset_json_path": str(dataset_path)})
    cr.commit()
    _append_log(
        run, cr, "Wrote dataset JSON (%d entries): %s" % (len(hf_entries), dataset_path)
    )


def _run_single_generating_tests(run, cr, env, cfg, tools_path):
    """Generating tests step — collect pytest test IDs from cloned repos."""
    _append_log(run, cr, "=== GENERATING TESTS ===")
    _ensure_tools_on_path(tools_path)

    from tools.generate_test_ids import collect_test_ids_local, save_test_ids

    test_ids_dir = Path(tempfile.mkdtemp(prefix="commit0_test_ids_"))

    for re_entry in run.repo_entry_ids:
        if _is_cancelled(run, cr):
            return

        repo_name = re_entry.repo_name or ""
        test_dir = re_entry.test_dir or "tests"
        full_name = ""
        if re_entry.repo_url:
            full_name = re_entry.repo_url.replace("https://github.com/", "")

        _append_log(run, cr, "Collecting test IDs for %s..." % repo_name)

        # Try to find an existing clone directory
        repo_dir = None
        possible_dirs = [
            Path(tempfile.gettempdir()),
        ]
        for base in possible_dirs:
            for prefix in ["commit0_prepare_", "commit0_validate_"]:
                for d in base.iterdir() if base.exists() else []:
                    if d.is_dir() and d.name.startswith(prefix):
                        candidate = d / full_name.replace("/", "__")
                        if candidate.is_dir():
                            repo_dir = candidate
                            break
                if repo_dir:
                    break
            if repo_dir:
                break

        if not repo_dir or not repo_dir.is_dir():
            _append_log(
                run,
                cr,
                "  Clone dir not found for %s — skipping test ID collection"
                % repo_name,
            )
            continue

        # Checkout reference commit for accurate test collection
        if re_entry.reference_commit:
            try:
                subprocess.run(
                    ["git", "checkout", re_entry.reference_commit],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
            except Exception as e:
                _append_log(
                    run,
                    cr,
                    "  Could not checkout reference_commit for %s: %s" % (repo_name, e),
                )

        test_ids = collect_test_ids_local(
            repo_dir=repo_dir,
            test_dir=test_dir,
        )

        if test_ids:
            out_file = save_test_ids(test_ids, repo_name, test_ids_dir)
            re_entry.write({"test_count": len(test_ids), "state": "tests_generated"})
            cr.commit()
            _append_log(
                run,
                cr,
                "  %s: %d test IDs saved to %s" % (repo_name, len(test_ids), out_file),
            )
        else:
            _append_log(run, cr, "  %s: 0 test IDs collected" % repo_name)
            re_entry.write({"test_count": 0, "state": "tests_generated"})
            cr.commit()

    run.write({"test_ids_path": str(test_ids_dir)})
    cr.commit()
    _append_log(run, cr, "Test ID generation complete. Output dir: %s" % test_ids_dir)


def _run_single_setting_up(run, cr, env, cfg, tools_path):
    """Setting up step — run `commit0 setup` via subprocess."""
    _append_log(run, cr, "=== SETTING UP (commit0 setup) ===")

    dataset_path = run.dataset_json_path
    if not dataset_path or not Path(dataset_path).exists():
        _append_log(run, cr, "No dataset JSON found — skipping setup")
        return

    cmd = [
        sys.executable,
        "-m",
        "commit0",
        "setup",
        "all",
        "--dataset-name",
        dataset_path,
    ]
    _append_log(run, cr, "Running: %s" % " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            _append_log(run, cr, "commit0 setup completed successfully")
            for re_entry in run.repo_entry_ids:
                if re_entry.state not in ("failed",):
                    re_entry.write({"state": "setup_done"})
            cr.commit()
        else:
            stderr = (result.stderr or "")[:1000]
            _append_log(
                run,
                cr,
                "commit0 setup failed (rc=%d): %s" % (result.returncode, stderr),
            )
    except subprocess.TimeoutExpired:
        _append_log(run, cr, "commit0 setup timed out after 600s")
    except FileNotFoundError:
        _append_log(run, cr, "commit0 not found — skipping setup (not installed)")


def _run_single_building(run, cr, env, cfg, tools_path):
    """Building step — run `commit0 build` via subprocess."""
    _append_log(run, cr, "=== BUILDING (commit0 build) ===")

    dataset_path = run.dataset_json_path
    if not dataset_path or not Path(dataset_path).exists():
        _append_log(run, cr, "No dataset JSON found — skipping build")
        return

    cmd = [
        sys.executable,
        "-m",
        "commit0",
        "build",
        "--dataset-name",
        dataset_path,
    ]
    _append_log(run, cr, "Running: %s" % " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode == 0:
            _append_log(run, cr, "commit0 build completed successfully")
            for re_entry in run.repo_entry_ids:
                if re_entry.state not in ("failed",):
                    re_entry.write({"state": "built"})
            cr.commit()
        else:
            stderr = (result.stderr or "")[:1000]
            _append_log(
                run,
                cr,
                "commit0 build failed (rc=%d): %s" % (result.returncode, stderr),
            )
    except subprocess.TimeoutExpired:
        _append_log(run, cr, "commit0 build timed out after 1800s")
    except FileNotFoundError:
        _append_log(run, cr, "commit0 not found — skipping build (not installed)")


# ---------------------------------------------------------------------------
# State machine — BATCH mode
# ---------------------------------------------------------------------------


def _run_batch_discovering(run, cr, env, cfg, tools_path):
    """Discovering step for batch mode — parse CSV and create repo entries."""
    _append_log(run, cr, "=== DISCOVERING (batch CSV) ===")

    rows = run._parse_csv()
    if not rows:
        _append_log(run, cr, "CSV parse returned 0 rows")
        return

    _append_log(run, cr, "Parsed %d rows from CSV" % len(rows))

    for idx, row in enumerate(rows):
        github_url = row.get("github_url", "")
        lib_name = row.get("library_name", "")
        org_name = row.get("organization_name", "")

        if not github_url:
            _append_log(run, cr, "  Row %d: no GitHub URL — skipping" % (idx + 1))
            continue

        full_name = github_url.rstrip("/").replace("https://github.com/", "")
        if full_name.endswith(".git"):
            full_name = full_name[:-4]
        repo_name = lib_name or (
            full_name.split("/")[-1] if "/" in full_name else full_name
        )

        env["commit0.repo.entry"].create(
            {
                "pipeline_run_id": run.id,
                "sequence": (idx + 1) * 10,
                "repo_name": repo_name,
                "repo_url": github_url,
                "state": "pending",
                "stubbing_mode": run.stubbing_mode or "combined",
            }
        )
    cr.commit()
    _append_log(
        run, cr, "Created %d repo entry records from CSV" % len(run.repo_entry_ids)
    )


def _run_batch_validating(run, cr, env, cfg, tools_path):
    """Validating step for batch mode — check each repo URL is accessible."""
    _append_log(run, cr, "=== VALIDATING (batch) ===")

    token = cfg["github_token"] or os.environ.get("GITHUB_TOKEN", "")
    _ensure_tools_on_path(tools_path)
    from tools.discover import github_api

    for re_entry in run.repo_entry_ids:
        if _is_cancelled(run, cr):
            return

        repo_url = re_entry.repo_url or ""
        full_name = repo_url.rstrip("/").replace("https://github.com/", "")
        if full_name.endswith(".git"):
            full_name = full_name[:-4]

        _append_log(run, cr, "Validating %s..." % full_name)

        try:
            github_api("/repos/%s" % full_name, token=token or None)
            re_entry.write({"state": "preparing"})
            cr.commit()
            _append_log(run, cr, "  %s: accessible ✓" % full_name)
        except Exception as e:
            re_entry.write(
                {
                    "state": "failed",
                    "error_message": "Validation failed: %s" % str(e)[:500],
                }
            )
            cr.commit()
            _append_log(run, cr, "  %s: FAILED — %s" % (full_name, str(e)[:200]))


def _run_batch_preparing(run, cr, env, cfg, tools_path):
    """Preparing step for batch mode — fork + stub + push each repo."""
    _append_log(run, cr, "=== PREPARING (batch) ===")
    _ensure_tools_on_path(tools_path)

    from tools.batch_prepare import prepare_single_repo

    token = cfg["github_token"] or ""
    org = cfg["github_org"] or "Ethara-Ai"
    removal_mode = run.stubbing_mode or "combined"
    clone_dir = Path(tempfile.mkdtemp(prefix="commit0_batch_"))

    if token:
        os.environ["GITHUB_TOKEN"] = token

    active_entries = run.repo_entry_ids.filtered(lambda e: e.state not in ("failed",))
    all_dataset_entries = []

    for re_entry in active_entries:
        if _is_cancelled(run, cr):
            return

        repo_url = re_entry.repo_url or ""
        full_name = repo_url.rstrip("/").replace("https://github.com/", "")
        if full_name.endswith(".git"):
            full_name = full_name[:-4]
        repo_name = re_entry.repo_name or (
            full_name.split("/")[-1] if "/" in full_name else full_name
        )

        _append_log(run, cr, "Preparing %s..." % full_name)
        re_entry.write({"state": "forking"})
        cr.commit()

        try:
            entry = prepare_single_repo(
                full_name=full_name,
                clone_dir=clone_dir,
                org=org,
                removal_mode=removal_mode,
                tag=run.tag or None,
            )
        except Exception as e:
            re_entry.write(
                {
                    "state": "failed",
                    "error_message": "Prepare failed: %s" % str(e)[:500],
                }
            )
            cr.commit()
            _append_log(run, cr, "  %s: FAILED — %s" % (repo_name, str(e)[:200]))
            continue

        if entry is None:
            re_entry.write(
                {
                    "state": "failed",
                    "error_message": "prepare_single_repo returned None",
                }
            )
            cr.commit()
            _append_log(run, cr, "  %s: preparation returned no entry" % repo_name)
            continue

        fork_name = entry.get("repo", "")
        fork_url = "https://github.com/%s" % fork_name if fork_name else ""
        setup = entry.get("setup", {})
        test = entry.get("test", {})

        re_entry.write(
            {
                "state": "dataset_created",
                "fork_url": fork_url,
                "base_commit": entry.get("base_commit", ""),
                "reference_commit": entry.get("reference_commit", ""),
                "src_dir": entry.get("src_dir", ""),
                "test_dir": test.get("test_dir", "tests"),
                "python_version": setup.get("python", "3.12"),
                "install_cmd": setup.get("install", ""),
                "clone_path": str(clone_dir / full_name.replace("/", "__"))
                if full_name
                else "",
            }
        )
        cr.commit()
        all_dataset_entries.append(entry)
        _append_log(
            run,
            cr,
            "  %s: prepared (base=%s)" % (repo_name, entry.get("base_commit", "")[:12]),
        )

    # Save combined dataset entries JSON
    if all_dataset_entries:
        dataset_dir = Path(tempfile.mkdtemp(prefix="commit0_dataset_"))
        dataset_path = dataset_dir / "dataset_entries.json"
        dataset_path.write_text(json.dumps(all_dataset_entries, indent=2))
        run.write({"entries_json_path": str(dataset_path)})
        cr.commit()
        _append_log(
            run, cr, "Saved %d entries to %s" % (len(all_dataset_entries), dataset_path)
        )


def _run_batch_creating_dataset(run, cr, env, cfg, tools_path):
    """Creating dataset step for batch mode — same as single mode."""
    _run_single_creating_dataset(run, cr, env, cfg, tools_path)


def _run_batch_generating_tests(run, cr, env, cfg, tools_path):
    """Generating tests step for batch mode — use dataset JSON + Docker if available."""
    _append_log(run, cr, "=== GENERATING TESTS (batch) ===")
    _ensure_tools_on_path(tools_path)

    from tools.generate_test_ids import (
        generate_for_dataset,
        save_test_ids,
        collect_test_ids_docker,
    )

    dataset_path = run.dataset_json_path
    test_ids_dir = Path(tempfile.mkdtemp(prefix="commit0_test_ids_"))

    if dataset_path and Path(dataset_path).exists():
        _append_log(run, cr, "Generating test IDs from dataset: %s" % dataset_path)
        results = generate_for_dataset(
            dataset_path=Path(dataset_path),
            output_dir=test_ids_dir,
            use_docker=False,
            timeout=300,
        )
        # Update repo entry test counts
        for re_entry in run.repo_entry_ids:
            repo_name = re_entry.repo_name or ""
            count = results.get(repo_name, 0)
            re_entry.write(
                {
                    "test_count": abs(count),
                    "state": "tests_generated"
                    if re_entry.state not in ("failed",)
                    else "failed",
                }
            )
        cr.commit()
        total = sum(abs(v) for v in results.values())
        _append_log(run, cr, "Test ID generation complete: %d total test IDs" % total)
    else:
        _append_log(run, cr, "No dataset JSON — skipping test ID generation")

    run.write({"test_ids_path": str(test_ids_dir)})
    cr.commit()


def _run_batch_setting_up(run, cr, env, cfg, tools_path):
    """Setting up step for batch mode — same as single."""
    _run_single_setting_up(run, cr, env, cfg, tools_path)


def _run_batch_building(run, cr, env, cfg, tools_path):
    """Building step for batch mode — same as single."""
    _run_single_building(run, cr, env, cfg, tools_path)


# ---------------------------------------------------------------------------
# Main background worker
# ---------------------------------------------------------------------------

# Pipeline state order and corresponding handler functions
_SINGLE_STEPS = [
    ("discovering", _run_single_discovering),
    ("validating", _run_single_validating),
    ("preparing", _run_single_preparing),
    ("creating_dataset", _run_single_creating_dataset),
    ("generating_tests", _run_single_generating_tests),
    ("setting_up", _run_single_setting_up),
    ("building", _run_single_building),
]

_BATCH_STEPS = [
    ("discovering", _run_batch_discovering),
    ("validating", _run_batch_validating),
    ("preparing", _run_batch_preparing),
    ("creating_dataset", _run_batch_creating_dataset),
    ("generating_tests", _run_batch_generating_tests),
    ("setting_up", _run_batch_setting_up),
    ("building", _run_batch_building),
]


def _run_pipeline_background(db_name, uid, run_id):
    """Background worker for pipeline execution.

    Opens a fresh cursor, walks the state machine, calls subprocess
    for each tool, captures output, updates state.
    """
    try:
        import odoo
        from odoo.modules.registry import Registry

        registry = Registry(db_name)
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, uid, {})
            run = env["commit0.pipeline.run"].browse(run_id)
            if not run.exists():
                _logger.error("Pipeline run %s not found", run_id)
                return

            _logger.info("Starting pipeline run %s (%s)", run.name, run.entry_type)
            _append_log(
                run, cr, "Pipeline execution started (mode=%s)" % run.entry_type
            )

            tools_path = get_tools_path(env)
            cfg = _get_config(env)

            # Select step handlers based on entry type
            steps = _BATCH_STEPS if run.entry_type == "batch" else _SINGLE_STEPS

            for state_name, handler_fn in steps:
                # Check cancellation before each step
                if _is_cancelled(run, cr):
                    _append_log(run, cr, "Pipeline cancelled by user — stopping")
                    return

                # Transition to this step's state
                _set_state(run, cr, state_name)

                try:
                    handler_fn(run, cr, env, cfg, tools_path)
                except Exception as step_err:
                    _logger.exception(
                        "Pipeline run %s failed at step '%s': %s",
                        run_id,
                        state_name,
                        step_err,
                    )
                    _append_log(
                        run,
                        cr,
                        "FAILED at step '%s': %s" % (state_name, str(step_err)[:1000]),
                    )
                    _set_state(
                        run,
                        cr,
                        "failed",
                        {
                            "error_message": "Failed at %s: %s"
                            % (state_name, str(step_err)),
                            "end_time": fields.Datetime.now(),
                        },
                    )
                    return

                # Check cancellation after each step
                if _is_cancelled(run, cr):
                    _append_log(
                        run,
                        cr,
                        "Pipeline cancelled by user — stopping after '%s'" % state_name,
                    )
                    return

            # All steps complete — mark repo entries and run as complete
            for re_entry in run.repo_entry_ids:
                if re_entry.state not in ("failed",):
                    re_entry.write({"state": "complete"})
            cr.commit()

            _set_state(run, cr, "complete", {"end_time": fields.Datetime.now()})
            _append_log(run, cr, "Pipeline completed successfully")

    except Exception as e:
        _logger.exception("Pipeline run %s failed: %s", run_id, e)
        try:
            import odoo
            from odoo.modules.registry import Registry

            registry = Registry(db_name)
            with registry.cursor() as cr:
                env = odoo.api.Environment(cr, uid, {})
                run = env["commit0.pipeline.run"].browse(run_id)
                if run.exists():
                    run.write(
                        {
                            "state": "failed",
                            "error_message": str(e),
                            "end_time": fields.Datetime.now(),
                        }
                    )
                    cr.commit()
        except Exception:
            _logger.exception("Failed to update pipeline run %s state", run_id)
    finally:
        _semaphore.release()
