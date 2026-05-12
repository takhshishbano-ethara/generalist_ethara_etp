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

from . import dataset_resolver, s3_storage

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
    "error_instances", "final_report_file", "dataset_jsonl_url", "missing_registries",
    "s3_run_number", "base_dockerfile_content", "base_dockerfile_s3_uri",
})

_MAX_LOG_SIZE = 2_000_000

_ALLOWED_INSTANCE_COLUMNS = frozenset({
    "status", "resolved", "error_message",
    "f2p_count", "p2p_count", "s2p_count", "n2p_count",
    "image_tag", "image_workdir",
    "tag_start", "tag_end", "pr_numbers",
    "pr_attribution_method", "version_scheme",
    "dockerfile_content", "report_json_content", "fix_patch_content",
    "build_log_tail", "run_log_tail", "test_patch_log_tail", "fix_patch_log_tail",
    "dockerfile_s3_uri", "build_log_s3_uri", "run_log_s3_uri",
    "test_patch_log_s3_uri", "fix_patch_log_s3_uri",
    "report_json_s3_uri", "fix_patch_s3_uri",
    "dockerfile_local_path", "build_log_local_path", "run_log_local_path",
    "test_patch_log_local_path", "fix_patch_log_local_path",
    "report_json_local_path",
})

_INLINE_DOCKERFILE_CAP = 16 * 1024
_INLINE_REPORT_CAP = 128 * 1024
_INLINE_FIX_PATCH_CAP = 256 * 1024
_LOG_TAIL_BYTES = 64 * 1024


_lht_metadata_cache: dict[tuple[str, str, int], dict[str, Any]] = {}
_lht_metadata_lock = threading.Lock()


def register_lht_metadata(org: str, repo: str, pr_number: int,
                          metadata: dict[str, Any]) -> None:
    """Register LHT fields for a (org, repo, pr_number) before evaluation.

    Called by phase2_docker_build._translate_phase1_jsonl (and the pipeline
    worker's equivalent) to bridge LHT dataset fields into the executor,
    which consumes harness `pr` objects that lack these fields natively.

    ``metadata`` keys: instance_id, tag_start, tag_end, pr_numbers,
    pr_attribution_method, version_scheme.
    """
    with _lht_metadata_lock:
        _lht_metadata_cache[(org, repo, int(pr_number))] = dict(metadata)


def _lookup_lht(org: str, repo: str, pr_number: int) -> dict[str, Any]:
    with _lht_metadata_lock:
        return dict(_lht_metadata_cache.get((org, repo, int(pr_number)), {}))


def _instance_id_for(pr) -> str:
    """Resolve the LHT instance_id for a harness pr object.

    Falls back to ``{org}__{repo}-pr-{number}`` only if no LHT metadata was
    registered — which is an authoring bug (the translator should always
    register before evaluation runs).
    """
    meta = _lookup_lht(pr.org, pr.repo, pr.number)
    iid = meta.get("instance_id")
    if iid:
        return iid
    return f"{pr.org}__{pr.repo}-pr-{pr.number}"


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
    query = "UPDATE aurora_evaluation SET log = RIGHT(COALESCE(log, '') || %s, %s) WHERE id = %s"
    params = [line + "\n", _MAX_LOG_SIZE, rec_id]

    for attempt in range(_SERIALIZATION_RETRIES):
        try:
            cr.execute(query, params)
            return
        except psycopg2.errors.SerializationFailure:
            cr.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                delay = _SERIALIZATION_BACKOFF * (2 ** attempt)
                _logger.warning(
                    "SerializationFailure on _append_log id=%s, retry %d/%d in %.1fs",
                    rec_id, attempt + 1, _SERIALIZATION_RETRIES, delay,
                )
                time.sleep(delay)
            else:
                _logger.warning(
                    "SerializationFailure on _append_log id=%s, dropping log line after %d retries",
                    rec_id, _SERIALIZATION_RETRIES,
                )
                return


def _notify_bus(db_name: str, rec_id: int) -> None:
    """Deprecated no-op: UI updates flow through HTTP polling after DB writes."""
    return


def _heartbeat(cr: Any, rec_id: int, progress_text: Optional[str] = None) -> None:
    vals = {"last_heartbeat": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if progress_text is not None:
        vals["progress_text"] = progress_text
    _update_eval(cr, rec_id, vals)
    cr.commit()


def _fail_eval(cr: Any, rec_id: int, step_field: str, exc) -> None:
    _update_eval(cr, rec_id, {step_field: "failed", "stage": "failed"})
    _append_log(cr, rec_id, f"FAILED ({step_field}): {exc}")


def _promote_linked_staging_record(cr: Any, eval_rec_id: int) -> None:
    try:
        cr.execute(
            "UPDATE aurora_harness_staging "
            "SET stage = 'done' "
            "WHERE eval_id = %s AND stage = 'evaluating'",
            [eval_rec_id],
        )
    except Exception:
        _logger.exception(
            "Failed to promote linked harness staging record for eval_id=%s", eval_rec_id,
        )


def _sync_missing_registries(cr: Any, rec_id: int, eval_config, pipeline_id: int = None) -> None:
    """Ensure every (org, repo) in the dataset has a harness class registered.

    Lookup order per (org, repo): staging overlay (already loaded before this
    call in _load_staging_for_eval) > in-process registry (vendored tree that
    may have been imported earlier) > remote GitHub sync from the harness
    registry repo using a token from the pool.

    AURORA_HARNESS_GIT_TOKEN is reserved exclusively for *pushing* to
    multi-swe-bench.  All read operations (registry sync) use the token pool.

    Anything still missing after this step surfaces in the downstream
    'missing registries' error branch with actionable guidance.
    """
    from ..tools.harness.instance import Instance

    pairs: set[tuple[str, str, str]] = set()
    for pr in eval_config.dataset.values():
        pairs.add((pr.org, pr.repo, getattr(pr, "lang", "") or "python"))

    needing_sync = [
        (org, repo, lang) for (org, repo, lang) in pairs
        if f"{org}/{repo}" not in Instance._registry
    ]
    if not needing_sync:
        return

    # Lease a single token from the pool for read-only registry sync.
    # pipeline_id is used as the FK references aurora_pipeline.
    from .github_token import AuroraGithubToken

    if not pipeline_id:
        _append_log(
            cr, rec_id,
            f"{len(needing_sync)} repo(s) missing from local registry and "
            f"no pipeline_id linked. Cannot lease tokens (FK constraint). "
            f"Skipping dynamic registry sync.",
        )
        return

    tokens = AuroraGithubToken.lease_tokens(cr, pipeline_id, count=1)
    cr.commit()
    if not tokens:
        _append_log(
            cr, rec_id,
            f"{len(needing_sync)} repo(s) missing from local registry and "
            f"no tokens available in the pool. Skipping dynamic registry "
            f"sync; evaluation will fail unless a staging upload is present.",
        )
        return
    token = tokens[0]

    try:
        from ..tools.harness_bridge.phase2_docker_build import (
            check_instance_registry,
            _import_all_repo_modules,
        )
    except Exception as exc:
        _logger.warning("harness_bridge not importable: %s", exc, exc_info=True)
        AuroraGithubToken.release_tokens(cr, pipeline_id)
        cr.commit()
        return

    synced_count = 0
    failed: list[str] = []
    try:
        for org, repo, lang in needing_sync:
            try:
                if check_instance_registry(org, repo, lang, github_token=token):
                    _import_all_repo_modules(org, repo, lang)
                    synced_count += 1
                    _append_log(cr, rec_id, f"Synced harness registry for {org}/{repo} (lang={lang}) from GitHub.")
                else:
                    failed.append(f"{org}/{repo}")
            except Exception as exc:
                _logger.exception("Registry sync failed for %s/%s", org, repo)
                failed.append(f"{org}/{repo}")
                _append_log(cr, rec_id, f"Registry sync failed for {org}/{repo}: {exc}")
    finally:
        AuroraGithubToken.release_tokens(cr, pipeline_id)

    if synced_count:
        cr.commit()


def _load_s3_config(cr: Any) -> dict:
    from .artifact_collector import load_s3_config
    return load_s3_config()


def _update_instance(cr: Any, instance_id: int, vals: dict[str, Any]) -> None:
    if not vals:
        return
    invalid = set(vals) - _ALLOWED_INSTANCE_COLUMNS
    if invalid:
        raise ValueError(f"Attempted to update disallowed instance columns: {invalid}")
    sets = ", ".join(f"{k} = %s" for k in vals)
    params = list(vals.values()) + [instance_id]
    query = f"UPDATE aurora_evaluation_instance SET {sets} WHERE id = %s"
    for attempt in range(_SERIALIZATION_RETRIES):
        try:
            cr.execute(query, params)
            return
        except psycopg2.errors.SerializationFailure:
            cr.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                time.sleep(_SERIALIZATION_BACKOFF * (2 ** attempt))
            else:
                _logger.warning(
                    "SerializationFailure on aurora_evaluation_instance id=%s, "
                    "dropping artifact update after %d retries: keys=%s",
                    instance_id, _SERIALIZATION_RETRIES, sorted(vals.keys()),
                )
                return


def _ensure_instance(cr: Any, evaluation_id: int, org: str, repo: str,
                     instance_id: str,
                     tag_start: Optional[str] = None,
                     tag_end: Optional[str] = None,
                     pr_numbers: Optional[str] = None,
                     pr_attribution_method: Optional[str] = None,
                     version_scheme: Optional[str] = None,
                     image_tag: Optional[str] = None,
                     image_workdir: Optional[str] = None) -> int:
    cr.execute(
        "SELECT id FROM aurora_evaluation_instance "
        "WHERE evaluation_id = %s AND instance_id = %s",
        [evaluation_id, instance_id],
    )
    row = cr.fetchone()
    if row:
        vals: dict[str, Any] = {}
        for key, val in (
            ("tag_start", tag_start),
            ("tag_end", tag_end),
            ("pr_numbers", pr_numbers),
            ("pr_attribution_method", pr_attribution_method),
            ("version_scheme", version_scheme),
            ("image_tag", image_tag),
            ("image_workdir", image_workdir),
        ):
            if val:
                vals[key] = val
        if vals:
            _update_instance(cr, row[0], vals)
        return row[0]

    insert_sql = (
        "INSERT INTO aurora_evaluation_instance "
        "(evaluation_id, org, repo, instance_id, tag_start, tag_end, "
        "pr_numbers, pr_attribution_method, version_scheme, "
        "image_tag, image_workdir, status, resolved, "
        "f2p_count, p2p_count, s2p_count, n2p_count, "
        "create_uid, create_date, write_uid, write_date) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "'pending', FALSE, 0, 0, 0, 0, 1, NOW(), 1, NOW()) "
        "RETURNING id"
    )
    insert_params = [
        evaluation_id, org, repo, instance_id, tag_start, tag_end,
        pr_numbers, pr_attribution_method, version_scheme,
        image_tag, image_workdir,
    ]
    for attempt in range(_SERIALIZATION_RETRIES):
        try:
            cr.execute(insert_sql, insert_params)
            return cr.fetchone()[0]
        except psycopg2.errors.SerializationFailure:
            cr.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                time.sleep(_SERIALIZATION_BACKOFF * (2 ** attempt))
            else:
                _logger.warning(
                    "SerializationFailure inserting aurora_evaluation_instance "
                    "eval=%s instance_id=%s after %d retries; "
                    "re-selecting in case a concurrent transaction won the race",
                    evaluation_id, instance_id, _SERIALIZATION_RETRIES,
                )
                cr.execute(
                    "SELECT id FROM aurora_evaluation_instance "
                    "WHERE evaluation_id = %s AND instance_id = %s",
                    [evaluation_id, instance_id],
                )
                again = cr.fetchone()
                if again:
                    return again[0]
                raise


def _read_tail(path: Optional[str], max_bytes: int = _LOG_TAIL_BYTES) -> Optional[str]:
    if not path:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def _read_capped(path: Optional[str], cap: int) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(cap + 1)
    except OSError:
        return None
    if len(data) > cap:
        return data[:cap].decode("utf-8", errors="replace") + "\n\n[truncated]"
    return data.decode("utf-8", errors="replace")


def _upload_artifact(s3_config: dict, use_s3: bool, local_path: Optional[str],
                     s3_key: str) -> Optional[str]:
    if not (use_s3 and local_path and os.path.isfile(local_path)):
        return None
    try:
        return s3_storage.upload_file(s3_config, local_path, s3_key)
    except Exception:
        _logger.warning("S3 upload failed for %s -> %s", local_path, s3_key, exc_info=True)
        return None


def _build_instance_key(s3_folder: str, phase: str, org: str, repo: str,
                        run_number: int, instance_id: str, filename: str) -> str:
    nested = f"{instance_id}/{filename}"
    return s3_storage.build_s3_key(org, repo, run_number, nested, folder=s3_folder, phase=phase)


def _resolve_run_numbers(s3_config: dict, use_s3: bool, s3_folder: str, phase: str,
                         eval_config) -> dict:
    result: dict = {}
    for inst in eval_config.instances:
        key = (inst.pr.org, inst.pr.repo)
        if key in result:
            continue
        if use_s3:
            try:
                result[key] = s3_storage.get_next_run_number(
                    s3_config, key[0], key[1], folder=s3_folder, phase=phase,
                )
            except Exception:
                _logger.warning(
                    "Phase-2: get_next_run_number failed for %s/%s, falling back to 1",
                    key[0], key[1], exc_info=True,
                )
                result[key] = 1
        else:
            result[key] = 1
    return result


def _populate_build_artifacts(cr: Any, rec_id: int, workdir: str, eval_config,
                              s3_config: dict, use_s3: bool,
                              run_numbers: dict, s3_folder: str, phase: str) -> None:
    from ..tools.harness.constant import (
        BUILD_IMAGE_WORKDIR, BUILD_IMAGE_LOG_FILE,
    )
    from ..tools.harness.image import Image

    oci_tar_dir = eval_config.output_tar
    collected_base_dirs: set[str] = set()

    for instance in eval_config.instances:
        pr = instance.pr
        image = instance.dependency()
        image_workdir_name = image.workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        img_dir = Path(workdir) / pr.org / pr.repo / BUILD_IMAGE_WORKDIR / image_workdir_name
        dockerfile_name = image.dockerfile_name() if hasattr(image, "dockerfile_name") else "Dockerfile"
        dockerfile_path = img_dir / dockerfile_name
        build_log_path = img_dir / BUILD_IMAGE_LOG_FILE
        image_tag_str = image.image_full_name() if hasattr(image, "image_full_name") else image_workdir_name
        lht = _lookup_lht(pr.org, pr.repo, pr.number)
        iid = lht.get("instance_id") or _instance_id_for(pr)
        instance_id = _ensure_instance(
            cr, rec_id, pr.org, pr.repo, iid,
            tag_start=lht.get("tag_start"),
            tag_end=lht.get("tag_end"),
            pr_numbers=lht.get("pr_numbers"),
            pr_attribution_method=lht.get("pr_attribution_method"),
            version_scheme=lht.get("version_scheme"),
            image_tag=image_tag_str,
            image_workdir=image_workdir_name,
        )
        vals: dict[str, Any] = {"status": "built"}
        if dockerfile_path.exists():
            vals["dockerfile_local_path"] = str(dockerfile_path)
            content = _read_capped(str(dockerfile_path), _INLINE_DOCKERFILE_CAP)
            if content is not None:
                vals["dockerfile_content"] = content
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(dockerfile_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, dockerfile_name),
            )
            if s3_uri:
                vals["dockerfile_s3_uri"] = s3_uri
        if build_log_path.exists():
            vals["build_log_local_path"] = str(build_log_path)
            tail = _read_tail(str(build_log_path))
            if tail is not None:
                vals["build_log_tail"] = tail
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(build_log_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, BUILD_IMAGE_LOG_FILE),
            )
            if s3_uri:
                vals["build_log_s3_uri"] = s3_uri
        else:
            vals["build_log_tail"] = (
                f"[Docker image {image_tag_str} was already "
                f"cached locally, so the harness skipped writing build_image.log.\n"
                f"To capture a real build log, tick 'Force Build' on the evaluation, "
                f"or run: docker rmi {image_tag_str}]"
            )

        if oci_tar_dir:
            safe_name = image_tag_str.replace("/", "_").replace(":", "_")
            tar_path = Path(str(oci_tar_dir)) / f"{safe_name}.tar"
            if tar_path.exists():
                s3_uri = _upload_artifact(
                    s3_config, use_s3, str(tar_path),
                    _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, f"{safe_name}.oci.tar"),
                )
                if s3_uri:
                    vals["oci_tar_s3_uri"] = s3_uri

        _update_instance(cr, instance_id, vals)

        if not collected_base_dirs and hasattr(image, "dependency"):
            base_image = image.dependency()
            if isinstance(base_image, Image):
                base_workdir_name = base_image.workdir()
                if base_workdir_name not in collected_base_dirs:
                    collected_base_dirs.add(base_workdir_name)
                    base_dir = Path(workdir) / pr.org / pr.repo / BUILD_IMAGE_WORKDIR / base_workdir_name
                    base_df_name = base_image.dockerfile_name() if hasattr(base_image, "dockerfile_name") else "Dockerfile"
                    base_df_path = base_dir / base_df_name
                    if base_df_path.exists():
                        base_vals: dict[str, Any] = {}
                        content = _read_capped(str(base_df_path), _INLINE_DOCKERFILE_CAP)
                        if content is not None:
                            base_vals["base_dockerfile_content"] = content
                        base_s3_key = s3_storage.build_s3_key(
                            pr.org, pr.repo, run_number,
                            f"base/{base_df_name}",
                            folder=s3_folder, phase=phase,
                        )
                        s3_uri = _upload_artifact(s3_config, use_s3, str(base_df_path), base_s3_key)
                        if s3_uri:
                            base_vals["base_dockerfile_s3_uri"] = s3_uri
                        if base_vals:
                            _update_eval(cr, rec_id, base_vals)


def _populate_run_artifacts(cr: Any, rec_id: int, workdir: str, eval_config,
                            s3_config: dict, use_s3: bool,
                            run_numbers: dict, s3_folder: str, phase: str) -> None:
    from ..tools.harness.constant import (
        EVALUATION_WORKDIR, RUN_LOG_FILE,
        TEST_PATCH_RUN_LOG_FILE, FIX_PATCH_RUN_LOG_FILE,
    )
    for instance in eval_config.instances:
        pr = instance.pr
        image_workdir_name = instance.dependency().workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        eval_dir = Path(workdir) / pr.org / pr.repo / EVALUATION_WORKDIR / image_workdir_name
        iid = _instance_id_for(pr)
        instance_id = _ensure_instance(cr, rec_id, pr.org, pr.repo, iid)
        vals: dict[str, Any] = {"status": "running"}
        for attr_local, attr_tail, attr_s3, fname in [
            ("run_log_local_path", "run_log_tail", "run_log_s3_uri", RUN_LOG_FILE),
            ("test_patch_log_local_path", "test_patch_log_tail", "test_patch_log_s3_uri", TEST_PATCH_RUN_LOG_FILE),
            ("fix_patch_log_local_path", "fix_patch_log_tail", "fix_patch_log_s3_uri", FIX_PATCH_RUN_LOG_FILE),
        ]:
            p = eval_dir / fname
            if p.exists():
                vals[attr_local] = str(p)
                tail = _read_tail(str(p))
                if tail is not None:
                    vals[attr_tail] = tail
                s3_uri = _upload_artifact(
                    s3_config, use_s3, str(p),
                    _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, fname),
                )
                if s3_uri:
                    vals[attr_s3] = s3_uri
        fix_patch_path = eval_dir / "fix.patch"
        if fix_patch_path.exists():
            content = _read_capped(str(fix_patch_path), _INLINE_FIX_PATCH_CAP)
            if content is not None:
                vals["fix_patch_content"] = content
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(fix_patch_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, "fix.patch"),
            )
            if s3_uri:
                vals["fix_patch_s3_uri"] = s3_uri
        _update_instance(cr, instance_id, vals)


def _populate_report_artifacts(cr: Any, rec_id: int, workdir: str, output_dir: str,
                               eval_config, s3_config: dict, use_s3: bool,
                               run_numbers: dict, s3_folder: str, phase: str) -> None:
    from ..tools.harness.constant import EVALUATION_WORKDIR, REPORT_FILE

    final_report_path = Path(output_dir) / "final_report.json"
    resolved_set: set = set()
    error_set: set = set()
    if final_report_path.exists():
        try:
            with open(final_report_path, "r", encoding="utf-8") as f:
                fr = json.load(f)
            resolved_set = set(fr.get("resolved_ids", []) or [])
            error_set = set(fr.get("error_ids", []) or [])
        except Exception:
            _logger.debug("Failed to parse final_report.json", exc_info=True)

    for instance in eval_config.instances:
        pr = instance.pr
        image_workdir_name = instance.dependency().workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        eval_dir = Path(workdir) / pr.org / pr.repo / EVALUATION_WORKDIR / image_workdir_name
        iid = _instance_id_for(pr)
        instance_id = _ensure_instance(cr, rec_id, pr.org, pr.repo, iid)
        pr_id = f"{pr.org}/{pr.repo}:pr-{pr.number}"

        vals: dict[str, Any] = {}
        if pr_id in error_set:
            vals["status"] = "error"
            vals["resolved"] = False
        elif pr_id in resolved_set:
            vals["status"] = "resolved"
            vals["resolved"] = True
        else:
            vals["status"] = "unresolved"
            vals["resolved"] = False

        report_path = eval_dir / REPORT_FILE
        if report_path.exists():
            vals["report_json_local_path"] = str(report_path)
            raw = _read_capped(str(report_path), _INLINE_REPORT_CAP)
            if raw is not None:
                try:
                    parsed = json.loads(raw)
                    vals["report_json_content"] = json.dumps(parsed, indent=2, sort_keys=True)
                except Exception:
                    vals["report_json_content"] = raw
            try:
                rpt = json.loads(Path(report_path).read_text("utf-8"))
                vals["f2p_count"] = len(rpt.get("f2p_tests", {}) or {})
                vals["p2p_count"] = len(rpt.get("p2p_tests", {}) or {})
                vals["s2p_count"] = len(rpt.get("s2p_tests", {}) or {})
                vals["n2p_count"] = len(rpt.get("n2p_tests", {}) or {})
                err_msg = rpt.get("error_msg") or ""
                if err_msg:
                    vals["error_message"] = err_msg[:4000]
            except Exception:
                _logger.debug("Failed to parse per-instance report for instance=%s", iid, exc_info=True)
            s3_uri = _upload_artifact(
                s3_config, use_s3, str(report_path),
                _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, REPORT_FILE),
            )
            if s3_uri:
                vals["report_json_s3_uri"] = s3_uri

        for attr_local, attr_tail in [
            ("run_log_local_path", "run_log_tail"),
            ("test_patch_log_local_path", "test_patch_log_tail"),
            ("fix_patch_log_local_path", "fix_patch_log_tail"),
        ]:
            cr.execute(
                f"SELECT {attr_local} FROM aurora_evaluation_instance WHERE id = %s",
                [instance_id],
            )
            existing = cr.fetchone()
            if existing and existing[0]:
                tail = _read_tail(existing[0])
                if tail is not None:
                    vals[attr_tail] = tail
        _update_instance(cr, instance_id, vals)


def _safe_phase_hook(cr: Any, rec_id: int, phase_label: str, fn):
    try:
        fn()
        cr.commit()
    except Exception as exc:
        _logger.warning("Phase-2 %s hook failed for eval=%s: %s",
                        phase_label, rec_id, exc, exc_info=True)
        try:
            cr.rollback()
            _append_log(cr, rec_id, f"[warn] {phase_label} artifact collection failed: {exc}")
            cr.commit()
        except Exception:
            _logger.exception("Failed to record hook warning")


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
            "pipeline_id": rec.pipeline_id.id if rec.pipeline_id else None,
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
    from ..tools.harness.staging_loader import load_staging_harness, load_staging_directory
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
                if rec._is_zip_upload():
                    staging_dir = os.path.dirname(staging_path)
                    originals = load_staging_directory(staging_dir, rec.org, rec.repo)
                else:
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
        # Dataset may also be a remote URL (Phase-1 S3 upload); resolve to a local
        # cached file first so abspath/glob see a real path.
        patch_file_abs = os.path.abspath(cfg["patch_file"]) if cfg["patch_file"] else cfg["patch_file"]
        dataset_file_local = (
            dataset_resolver.resolve_to_local(cr, cfg["dataset_file"])
            if cfg["dataset_file"] else cfg["dataset_file"]
        )
        dataset_file_abs = os.path.abspath(dataset_file_local) if dataset_file_local else dataset_file_local

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
            output_tar=Path(cfg["workdir"]) / "oci_tars" if cfg.get("docker_platform") else None,
            specifics=specifics,
            instance_limit=cfg["instance_limit"],
        )

        _check_cancelled()
        _append_log(cr, rec_id, "Validating dataset instances...")
        _heartbeat(cr, rec_id, "Validating instances")
        cr.commit()

        total_dataset = len(eval_config.dataset)

        _sync_missing_registries(cr, rec_id, eval_config, pipeline_id=cfg.get("pipeline_id"))

        total_instances = len(eval_config.instances)
        parse_failures = getattr(eval_config, "_dataset_parse_failures", []) or []
        total_lines = getattr(eval_config, "_dataset_total_lines", 0)
        if total_instances == 0:
            missing_list = ""
            if total_dataset == 0:
                if parse_failures:
                    sample = parse_failures[0]
                    sample_summary = (
                        f"  line {sample[1]}: {sample[2]}"
                    )
                    msg = (
                        f"Dataset file found but every record failed schema validation.\n"
                        f"Dataset file: {dataset_file_abs}\n"
                        f"Lines read: {total_lines}. Parse failures: {len(parse_failures)}.\n"
                        f"First failure:\n{sample_summary}\n"
                        f"Typical cause: the dataset was built before a harness-schema change "
                        f"(e.g. missing required `number` field). "
                        f"Rebuild the dataset via the pipeline and retry."
                    )
                elif specifics:
                    msg = (
                        f"No dataset entries matched the Specific PRs filter.\n"
                        f"Specifics: {sorted(specifics)}\n"
                        f"Dataset file: {dataset_file_abs}\n"
                        f"Clear the 'Specific PRs' field to evaluate all entries, "
                        f"or set it to real instance_id values from the dataset."
                    )
                else:
                    msg = (
                        f"Dataset is empty or unreadable.\n"
                        f"Dataset file: {dataset_file_abs}\n"
                        f"File exists on worker: {os.path.isfile(dataset_file_abs) if dataset_file_abs else False}\n"
                        f"Verify the dataset path is absolute and the file is reachable "
                        f"from the Odoo worker process."
                    )
            else:
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

        s3_config = _load_s3_config(cr)
        use_s3 = s3_storage.is_configured(s3_config)
        s3_folder = (s3_config.get("folder") or "").strip("/")
        phase = "aurora_phase2"
        run_numbers = _resolve_run_numbers(s3_config, use_s3, s3_folder, phase, eval_config)
        if run_numbers:
            example = next(iter(run_numbers.items()))
            _append_log(cr, rec_id,
                        f"Phase-2 S3 run_number resolved: {dict(run_numbers)} "
                        f"(layout: {s3_folder}/{phase}/{example[0][0]}__{example[0][1]}/run_{example[1]}/pr-N/)")
            _update_eval(cr, rec_id, {"s3_run_number": max(run_numbers.values())})
            cr.commit()

        def _seed_instances():
            for inst in eval_config.instances:
                pr = inst.pr
                lht = _lookup_lht(pr.org, pr.repo, pr.number)
                iid = lht.get("instance_id") or _instance_id_for(pr)
                _ensure_instance(
                    cr, rec_id, pr.org, pr.repo, iid,
                    tag_start=lht.get("tag_start"),
                    tag_end=lht.get("tag_end"),
                    pr_numbers=lht.get("pr_numbers"),
                    pr_attribution_method=lht.get("pr_attribution_method"),
                    version_scheme=lht.get("version_scheme"),
                    image_tag=(inst.dependency().image_full_name()
                               if hasattr(inst.dependency(), "image_full_name")
                               else inst.dependency().workdir()),
                    image_workdir=inst.dependency().workdir(),
                )

        _safe_phase_hook(cr, rec_id, "pre-build-seed", _seed_instances)
        _update_eval(cr, rec_id, {"total_instances": total_instances})
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

        _safe_phase_hook(cr, rec_id, "post-build",
                         lambda: _populate_build_artifacts(
                             cr, rec_id, cfg["workdir"], eval_config,
                             s3_config, use_s3, run_numbers, s3_folder, phase))

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

        _safe_phase_hook(cr, rec_id, "post-run",
                         lambda: _populate_run_artifacts(
                             cr, rec_id, cfg["workdir"], eval_config,
                             s3_config, use_s3, run_numbers, s3_folder, phase))

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

        _safe_phase_hook(cr, rec_id, "post-report",
                         lambda: _populate_report_artifacts(
                             cr, rec_id, cfg["workdir"], cfg["output_dir"],
                             eval_config, s3_config, use_s3,
                             run_numbers, s3_folder, phase))

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

        _promote_linked_staging_record(cr, rec_id)
        cr.commit()

        _post_chatter(
            db_name, uid, rec_id,
            f"Evaluation completed \u2014 {resolved}/{total} instances resolved, "
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
