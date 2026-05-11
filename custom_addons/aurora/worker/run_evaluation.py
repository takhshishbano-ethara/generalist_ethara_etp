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


def _ensure_docker_ready(timeout: int = 60):
    """Quick Docker readiness re-check between stages (handles DinD restart)."""
    import subprocess
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(5)
    raise RuntimeError(
        f"Docker daemon not available after {timeout}s re-check (possible DinD restart)."
    )


def _setup_buildx_builder():
    """Create a buildx builder that leverages QEMU binfmt for multi-arch builds."""
    import subprocess

    builder_name = "aurora-multiarch"

    inspect = subprocess.run(
        ["docker", "buildx", "inspect", builder_name],
        capture_output=True,
        timeout=10,
    )
    if inspect.returncode == 0:
        subprocess.run(
            ["docker", "buildx", "use", builder_name],
            capture_output=True,
            timeout=10,
        )
        _logger.info("Buildx builder '%s' already exists, selected.", builder_name)
        return

    result = subprocess.run(
        [
            "docker", "buildx", "create",
            "--name", builder_name,
            "--driver", "docker-container",
            "--bootstrap",
            "--use",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        _logger.warning(
            "Failed to create buildx builder '%s': %s",
            builder_name,
            result.stderr.strip(),
        )
        return

    _logger.info("Created and bootstrapped buildx builder '%s'.", builder_name)


_ALLOWED_EVAL_COLUMNS = frozenset({
    "stage", "build_status", "run_status", "report_status",
    "total_instances", "resolved_instances", "unresolved_instances", "error_instances",
    "final_report_file", "missing_registries", "patch_file", "repo_dir", "workdir",
    "output_dir", "last_heartbeat", "progress_text", "log", "s3_run_number",
})


def _update_eval(conn, rec_id: int, vals: dict):
    """Write evaluation fields via raw SQL."""
    if not vals:
        return
    invalid_cols = set(vals.keys()) - _ALLOWED_EVAL_COLUMNS
    if invalid_cols:
        raise ValueError(f"Disallowed columns in _update_eval: {invalid_cols}")
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
            "instance_limit, specific_prs, pipeline_id "
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
        "pipeline_id": row[11],
    }


def _lease_tokens(db_name: str, rec_id: int, count: int = 1) -> list[str]:
    """Lease GitHub tokens from the pool (same pattern as run_pipeline.py)."""
    from odoo.addons.aurora.models.github_token import AuroraGithubToken
    last_exc = None
    for attempt in range(3):
        conn = None
        try:
            conn = _open_cursor(db_name)
            with conn.cursor() as cur:
                tokens = AuroraGithubToken.lease_tokens(cur, rec_id, count=count)
                conn.commit()
            return tokens
        except Exception as exc:
            last_exc = exc
            _logger.warning("Token lease attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    raise last_exc


def _release_tokens(db_name: str, rec_id: int) -> None:
    """Release leased GitHub tokens back to the pool."""
    from odoo.addons.aurora.models.github_token import AuroraGithubToken
    conn = _open_cursor(db_name)
    try:
        with conn.cursor() as cur:
            AuroraGithubToken.release_tokens(cur, rec_id)
            conn.commit()
    except Exception:
        _logger.exception("Failed to release tokens for eval=%s", rec_id)
    finally:
        conn.close()


def _resolve_entry_number(entry: dict) -> int | None:
    """Extract PR number from a dataset entry (mirrors evaluation.py logic)."""
    if "number" in entry:
        val = entry["number"]
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
        if isinstance(val, str) and "-" in val:
            head = val.split("-", 1)[0]
            if head.isdigit():
                return int(head)
    pr_numbers = entry.get("pr_numbers") or []
    if isinstance(pr_numbers, list) and pr_numbers:
        try:
            return int(pr_numbers[0])
        except (TypeError, ValueError):
            pass
    return None


def _generate_patch_file(dataset_path: str, output_path: str) -> str:
    """Regenerate patches.jsonl from dataset file.

    The Odoo server generates this file locally but it's not available
    inside the K8s pod. We regenerate it from the already-resolved dataset.
    Returns the output path.
    """
    import json as _json
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    total_lines = 0
    error_lines = 0
    with open(dataset_path, "r", encoding="utf-8") as f_in, \
         open(output_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                entry = _json.loads(line)
            except (ValueError, _json.JSONDecodeError) as e:
                error_lines += 1
                _logger.warning("Skipping corrupt dataset line %d: %s", total_lines, e)
                continue
            number = _resolve_entry_number(entry)
            if number is None:
                continue
            patch_entry = {
                "org": entry.get("org", ""),
                "repo": entry.get("repo", ""),
                "number": number,
                "fix_patch": entry.get("fix_patch", ""),
            }
            f_out.write(_json.dumps(patch_entry, ensure_ascii=False) + "\n")
    if total_lines > 0 and error_lines / total_lines > 0.5:
        raise RuntimeError(
            f"Dataset appears corrupted: {error_lines}/{total_lines} lines failed to parse"
        )
    return output_path


def _safe_collect(conn, rec_id: int, phase_label: str, fn):
    try:
        fn()
    except Exception as exc:
        _logger.warning("Artifact collection %s failed for eval=%s: %s",
                        phase_label, rec_id, exc, exc_info=True)
        _append_log(conn, rec_id, f"[warn] {phase_label} artifact collection failed: {exc}")


def _seed_instance_records(conn, rec_id: int, instances):
    from odoo.addons.aurora.models import artifact_collector
    for inst in instances:
        pr = inst.pr
        iid = artifact_collector.instance_id_for(pr)
        image = inst.dependency()
        artifact_collector.ensure_instance(
            conn, rec_id, pr.org, pr.repo, iid,
            image_tag=(image.image_full_name() if hasattr(image, "image_full_name") else image.workdir()),
            image_workdir=image.workdir(),
        )


def _setup_git_auth(token: str) -> Optional[str]:
    """Create a git credential helper script for authenticated clone.

    Uses the standard git credential mechanism: a helper script that
    outputs protocol/host/username/password. This ensures all git
    operations (clone, fetch) are authenticated.

    Returns the path to the script, or None on failure.
    """
    import stat
    import tempfile
    script_path = os.path.join(tempfile.gettempdir(), f"aurora_git_cred_{os.getpid()}.sh")
    try:
        with open(script_path, "w") as f:
            f.write("#!/bin/sh\n")
            f.write("echo protocol=https\n")
            f.write("echo host=github.com\n")
            f.write("echo username=x-access-token\n")
            f.write(f"echo password={token}\n")
            f.write("echo\n")
        os.chmod(script_path, stat.S_IRWXU)
        os.environ["GIT_TERMINAL_PROMPT"] = "0"
        import subprocess
        subprocess.run(
            ["git", "config", "--global", "credential.helper", f"!{script_path}"],
            check=True, capture_output=True,
        )
        _logger.info("Git credential helper configured for authenticated access")
        return script_path
    except Exception:
        _logger.warning("Failed to configure git auth", exc_info=True)
        return None


def _cleanup_git_auth(script_path: Optional[str]) -> None:
    if script_path and os.path.exists(script_path):
        try:
            os.remove(script_path)
        except OSError:
            pass
    os.environ.pop("GIT_TERMINAL_PROMPT", None)
    try:
        import subprocess
        subprocess.run(
            ["git", "config", "--global", "--unset", "credential.helper"],
            capture_output=True,
        )
    except Exception:
        pass


def run_evaluation(db_name: str, rec_id: int):
    """Main evaluation pipeline — runs inside K8s Job."""
    conn = _open_cursor(db_name)
    heartbeat_stop = threading.Event()

    def _heartbeat_loop():
        consecutive_failures = 0
        while not heartbeat_stop.wait(timeout=60):
            hb_conn = None
            try:
                hb_conn = _open_cursor(db_name)
                _heartbeat(hb_conn, rec_id, None)
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    _logger.error(
                        "Heartbeat failed %d consecutive times — "
                        "watchdog may kill this evaluation",
                        consecutive_failures,
                    )
                else:
                    _logger.debug("Heartbeat write failed (attempt %d)", consecutive_failures)
                if consecutive_failures > 3:
                    backoff = min(30 * consecutive_failures, 300)
                    heartbeat_stop.wait(timeout=backoff)
            finally:
                if hb_conn is not None:
                    try:
                        hb_conn.close()
                    except Exception:
                        pass

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    tokens = None
    git_askpass_script = None
    pipeline_id = None

    try:
        _heartbeat(conn, rec_id, "Initializing evaluation worker")
        _append_log(conn, rec_id, "K8s evaluation worker started.")

        _wait_for_docker(timeout=120)
        _append_log(conn, rec_id, "Docker daemon ready.")

        _check_cancelled()

        # Read config
        cfg = _read_eval_config(conn, rec_id)
        _append_log(conn, rec_id, f"Config loaded: workdir={cfg['workdir']}")

        if cfg.get("docker_platform"):
            _setup_buildx_builder()
            _append_log(conn, rec_id, f"Buildx multi-arch builder ready (platform={cfg['docker_platform']}).")

        # Ensure working directories exist (K8s pod starts with empty volume)
        for dir_key in ("workdir", "output_dir", "repo_dir"):
            if cfg.get(dir_key):
                Path(cfg[dir_key]).mkdir(parents=True, exist_ok=True)

        # Lease tokens from the pool (same lifecycle as Phase 1 worker)
        # Use pipeline_id as the lease key (FK references aurora_pipeline)
        pipeline_id = cfg.get("pipeline_id")
        if not pipeline_id:
            _fail_eval(conn, rec_id, "Evaluation has no linked pipeline_id; cannot lease tokens.")
            return
        tokens = _lease_tokens(db_name, pipeline_id, count=1)
        if not tokens:
            _fail_eval(conn, rec_id, "No GitHub tokens available in the pool.")
            return
        _append_log(conn, rec_id, f"Leased {len(tokens)} token(s) from pool.")
        github_token = tokens[0]

        # Configure authenticated git access for repo cloning
        git_askpass_script = _setup_git_auth(github_token)

        # Resolve remote dataset to local
        from odoo.addons.aurora.models import dataset_resolver
        from odoo.addons.aurora.models import s3_storage as s3_storage_mod
        dataset_file = cfg["dataset_file"]
        if dataset_resolver.is_remote(dataset_file):
            _append_log(conn, rec_id, f"Downloading remote dataset: {dataset_file}")
            dataset_file = dataset_resolver.resolve_to_local(conn, dataset_file)
            _append_log(conn, rec_id, f"Dataset cached locally: {dataset_file}")
        elif not os.path.isfile(dataset_file):
            # Local path from server doesn't exist in pod — fetch original S3 URL from pipeline
            original_url = None
            if pipeline_id:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT step6_file FROM aurora_pipeline WHERE id = %s",
                        (pipeline_id,),
                    )
                    prow = cur.fetchone()
                    if prow and prow[0] and dataset_resolver.is_remote(prow[0]):
                        original_url = prow[0]
            if original_url:
                _append_log(conn, rec_id, f"Dataset local path missing, downloading from pipeline S3: {original_url}")
                dataset_file = dataset_resolver.resolve_to_local(conn, original_url)
                _append_log(conn, rec_id, f"Dataset cached locally: {dataset_file}")
            else:
                _fail_eval(conn, rec_id, f"Dataset file not found and no remote source available: {dataset_file}")
                return

        # Regenerate patch file locally (server-generated file isn't in the pod)
        patch_file = cfg["patch_file"]
        if patch_file and not os.path.isfile(patch_file):
            _append_log(conn, rec_id, f"Patch file not found locally, regenerating from dataset...")
            _generate_patch_file(dataset_file, patch_file)
            _append_log(conn, rec_id, f"Patch file regenerated: {patch_file}")

        log_dir = Path(cfg["output_dir"]) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Import harness
        from odoo.addons.aurora.tools.harness.run_evaluation import EvalConfig
        from odoo.addons.aurora.tools.harness_bridge.phase2_docker_build import (
            check_instance_registry,
            _import_all_repo_modules,
        )

        # Sync instance registries — first try vendored, then sync missing from GitHub
        _check_cancelled()
        _append_log(conn, rec_id, "Syncing harness instance registries...")
        _heartbeat(conn, rec_id, "Syncing registries")

        try:
            import importlib
            importlib.import_module("odoo.addons.aurora.tools.harness.repos")
        except Exception:
            _logger.warning("Bulk repo import failed", exc_info=True)

        # Sync any repos missing from vendored tree using token from pool
        from odoo.addons.aurora.tools.harness.instance import Instance
        import json
        missing_pairs: set[tuple[str, str, str]] = set()
        try:
            with open(os.path.abspath(dataset_file), "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    org_name = entry.get("org", "")
                    repo_name = entry.get("repo", "")
                    lang = entry.get("lang", "") or "python"
                    if org_name and repo_name:
                        key = f"{org_name}/{repo_name}"
                        if key not in Instance._registry:
                            missing_pairs.add((org_name, repo_name, lang))
        except Exception:
            _logger.warning("Failed to parse dataset for registry check", exc_info=True)

        if missing_pairs:
            _append_log(conn, rec_id, f"{len(missing_pairs)} repo(s) missing from local registry, syncing from GitHub...")
            synced = 0
            for org_name, repo_name, lang in missing_pairs:
                try:
                    if check_instance_registry(org_name, repo_name, lang, github_token=github_token):
                        _import_all_repo_modules(org_name, repo_name, lang)
                        synced += 1
                        _append_log(conn, rec_id, f"Synced registry: {org_name}/{repo_name} (lang={lang})")
                except Exception as exc:
                    _logger.exception("Registry sync failed for %s/%s", org_name, repo_name)
                    _append_log(conn, rec_id, f"Registry sync failed for {org_name}/{repo_name}: {exc}")
            _append_log(conn, rec_id, f"Registry sync complete: {synced}/{len(missing_pairs)} synced.")

        # Parse specifics filter
        specifics = None
        if cfg["specific_prs"]:
            specifics = {s.strip() for s in cfg["specific_prs"].split(",") if s.strip()}

        # Build EvalConfig
        oci_tar_dir = Path(cfg["workdir"]) / "oci_tars" if cfg.get("docker_platform") else None
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
            output_tar=oci_tar_dir,
            specifics=specifics,
            instance_limit=cfg["instance_limit"],
        )

        total_instances = len(eval_config.instances)
        if total_instances == 0:
            _fail_eval(conn, rec_id, "No instances matched dataset + harness registry.")
            return

        _append_log(conn, rec_id, f"Found {total_instances} instances to evaluate.")
        _update_eval(conn, rec_id, {"total_instances": total_instances})

        from odoo.addons.aurora.models import artifact_collector
        s3_config = artifact_collector.load_s3_config()
        use_s3 = s3_storage_mod.is_configured(s3_config)
        s3_folder = (s3_config.get("folder") or "").strip("/")
        phase = "aurora_phase2"
        run_numbers = artifact_collector.resolve_run_numbers(
            s3_config, use_s3, s3_folder, phase, eval_config.instances,
        )
        if use_s3:
            _append_log(conn, rec_id, f"S3 artifact upload enabled (run_numbers={dict(run_numbers)})")
        else:
            _append_log(conn, rec_id, "S3 not configured — artifacts will only persist in DB inline fields.")

        _seed_instance_records(conn, rec_id, eval_config.instances)

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

        _safe_collect(conn, rec_id, "post-build", lambda: artifact_collector.populate_build_artifacts(
            conn, rec_id, cfg["workdir"], eval_config.instances,
            s3_config, use_s3, run_numbers, s3_folder, phase,
            oci_tar_dir=str(oci_tar_dir) if oci_tar_dir else None,
        ))

        # ── Stage 2: Run Instances ────────────────────────────────────────
        _check_cancelled()
        _ensure_docker_ready(timeout=60)
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

        _safe_collect(conn, rec_id, "post-run", lambda: artifact_collector.populate_run_artifacts(
            conn, rec_id, cfg["workdir"], eval_config.instances,
            s3_config, use_s3, run_numbers, s3_folder, phase,
        ))

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

        _safe_collect(conn, rec_id, "post-report", lambda: artifact_collector.populate_report_artifacts(
            conn, rec_id, cfg["workdir"], cfg["output_dir"], eval_config.instances,
            s3_config, use_s3, run_numbers, s3_folder, phase,
        ))

        # ── Finalize ──────────────────────────────────────────────────────
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
        _cleanup_git_auth(git_askpass_script)
        if tokens:
            _release_tokens(db_name, pipeline_id)
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
