"""Shared artifact collection for Phase 2 evaluation.

Used by both the K8s worker (worker/run_evaluation.py) and the local
executor (models/evaluation_executor.py) to upload per-instance artifacts
to S3 and create/update aurora_evaluation_instance DB records.
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import psycopg2

from . import s3_storage

_logger = logging.getLogger(__name__)

_SERIALIZATION_RETRIES = 3
_SERIALIZATION_BACKOFF = 0.5

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
    "report_json_s3_uri", "fix_patch_s3_uri", "oci_tar_s3_uri",
    "dockerfile_local_path", "build_log_local_path", "run_log_local_path",
    "test_patch_log_local_path", "fix_patch_log_local_path",
    "report_json_local_path",
})

_INLINE_DOCKERFILE_CAP = 16 * 1024
_INLINE_REPORT_CAP = 128 * 1024
_INLINE_FIX_PATCH_CAP = 256 * 1024
_LOG_TAIL_BYTES = 64 * 1024


def load_s3_config() -> dict:
    from .pipeline import S3_BUCKET, S3_REGION, S3_AURORA_PREFIX
    return {
        "bucket": S3_BUCKET,
        "region": S3_REGION,
        "access_key": os.environ.get("AURORA_S3_ACCESS_KEY", "").strip(),
        "secret_key": os.environ.get("AURORA_S3_SECRET_KEY", "").strip(),
        "folder": S3_AURORA_PREFIX,
    }


def resolve_run_numbers(s3_config: dict, use_s3: bool, s3_folder: str,
                        phase: str, instances) -> dict:
    result: dict = {}
    for inst in instances:
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
                    "get_next_run_number failed for %s/%s, falling back to 1",
                    key[0], key[1], exc_info=True,
                )
                result[key] = 1
        else:
            result[key] = 1
    return result


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


def update_instance(conn, instance_id: int, vals: dict[str, Any]) -> None:
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
            with conn.cursor() as cur:
                cur.execute(query, params)
            conn.commit()
            return
        except psycopg2.errors.SerializationFailure:
            conn.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                time.sleep(_SERIALIZATION_BACKOFF * (2 ** attempt))
            else:
                _logger.warning(
                    "SerializationFailure updating instance id=%s after %d retries: keys=%s",
                    instance_id, _SERIALIZATION_RETRIES, sorted(vals.keys()),
                )
                return


def ensure_instance(conn, evaluation_id: int, org: str, repo: str,
                    instance_id: str,
                    tag_start: Optional[str] = None,
                    tag_end: Optional[str] = None,
                    pr_numbers: Optional[str] = None,
                    pr_attribution_method: Optional[str] = None,
                    version_scheme: Optional[str] = None,
                    image_tag: Optional[str] = None,
                    image_workdir: Optional[str] = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM aurora_evaluation_instance "
            "WHERE evaluation_id = %s AND instance_id = %s",
            [evaluation_id, instance_id],
        )
        row = cur.fetchone()

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
            update_instance(conn, row[0], vals)
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
            with conn.cursor() as cur:
                cur.execute(insert_sql, insert_params)
                new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
        except psycopg2.errors.SerializationFailure:
            conn.rollback()
            if attempt < _SERIALIZATION_RETRIES - 1:
                time.sleep(_SERIALIZATION_BACKOFF * (2 ** attempt))
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM aurora_evaluation_instance "
                        "WHERE evaluation_id = %s AND instance_id = %s",
                        [evaluation_id, instance_id],
                    )
                    again = cur.fetchone()
                if again:
                    return again[0]
                raise
    raise RuntimeError("ensure_instance exhausted retries without result")


def instance_id_for(pr) -> str:
    return f"{pr.org}__{pr.repo}-pr-{pr.number}"


def populate_build_artifacts(conn, rec_id: int, workdir: str, instances,
                             s3_config: dict, use_s3: bool,
                             run_numbers: dict, s3_folder: str, phase: str,
                             oci_tar_dir: Optional[str] = None) -> dict:
    """Collect per-instance build artifacts and base image Dockerfile.

    Returns a dict with base image Dockerfile info (may be empty):
        {"base_dockerfile_content": str|None, "base_dockerfile_s3_uri": str|None}
    """
    from ..tools.harness.constant import BUILD_IMAGE_WORKDIR, BUILD_IMAGE_LOG_FILE
    from ..tools.harness.image import Image

    base_result: dict[str, Any] = {}
    collected_base_dirs: set[str] = set()

    for instance in instances:
        pr = instance.pr
        image = instance.dependency()
        image_workdir_name = image.workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        img_dir = Path(workdir) / pr.org / pr.repo / BUILD_IMAGE_WORKDIR / image_workdir_name
        dockerfile_name = image.dockerfile_name() if hasattr(image, "dockerfile_name") else "Dockerfile"
        dockerfile_path = img_dir / dockerfile_name
        build_log_path = img_dir / BUILD_IMAGE_LOG_FILE
        image_tag_str = image.image_full_name() if hasattr(image, "image_full_name") else image_workdir_name

        iid = instance_id_for(pr)
        db_instance_id = ensure_instance(
            conn, rec_id, pr.org, pr.repo, iid,
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
                f"cached locally — build_image.log was not written.\n"
                f"To capture a build log, tick 'Force Build' or run: docker rmi {image_tag_str}]"
            )

        if oci_tar_dir:
            safe_name = image_tag_str.replace("/", "_").replace(":", "_")
            tar_path = Path(oci_tar_dir) / f"{safe_name}.tar"
            if tar_path.exists():
                s3_uri = _upload_artifact(
                    s3_config, use_s3, str(tar_path),
                    _build_instance_key(s3_folder, phase, pr.org, pr.repo, run_number, iid, f"{safe_name}.oci.tar"),
                )
                if s3_uri:
                    vals["oci_tar_s3_uri"] = s3_uri

        update_instance(conn, db_instance_id, vals)

        # --- Collect base image Dockerfile (once per unique base) ---
        if not base_result and hasattr(image, "dependency"):
            base_image = image.dependency()
            if isinstance(base_image, Image):
                base_workdir_name = base_image.workdir()
                if base_workdir_name not in collected_base_dirs:
                    collected_base_dirs.add(base_workdir_name)
                    base_dir = Path(workdir) / pr.org / pr.repo / BUILD_IMAGE_WORKDIR / base_workdir_name
                    base_df_name = base_image.dockerfile_name() if hasattr(base_image, "dockerfile_name") else "Dockerfile"
                    base_df_path = base_dir / base_df_name
                    if base_df_path.exists():
                        content = _read_capped(str(base_df_path), _INLINE_DOCKERFILE_CAP)
                        if content is not None:
                            base_result["base_dockerfile_content"] = content
                        base_s3_key = s3_storage.build_s3_key(
                            pr.org, pr.repo, run_number,
                            f"base/{base_df_name}",
                            folder=s3_folder, phase=phase,
                        )
                        s3_uri = _upload_artifact(s3_config, use_s3, str(base_df_path), base_s3_key)
                        if s3_uri:
                            base_result["base_dockerfile_s3_uri"] = s3_uri

    return base_result


def populate_run_artifacts(conn, rec_id: int, workdir: str, instances,
                           s3_config: dict, use_s3: bool,
                           run_numbers: dict, s3_folder: str, phase: str) -> None:
    from ..tools.harness.constant import (
        EVALUATION_WORKDIR, RUN_LOG_FILE,
        TEST_PATCH_RUN_LOG_FILE, FIX_PATCH_RUN_LOG_FILE,
    )

    for instance in instances:
        pr = instance.pr
        image_workdir_name = instance.dependency().workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        eval_dir = Path(workdir) / pr.org / pr.repo / EVALUATION_WORKDIR / image_workdir_name

        iid = instance_id_for(pr)
        db_instance_id = ensure_instance(conn, rec_id, pr.org, pr.repo, iid)

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
        update_instance(conn, db_instance_id, vals)


def populate_report_artifacts(conn, rec_id: int, workdir: str, output_dir: str,
                              instances, s3_config: dict, use_s3: bool,
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

    for instance in instances:
        pr = instance.pr
        image_workdir_name = instance.dependency().workdir()
        run_number = run_numbers.get((pr.org, pr.repo), 1)
        eval_dir = Path(workdir) / pr.org / pr.repo / EVALUATION_WORKDIR / image_workdir_name

        iid = instance_id_for(pr)
        db_instance_id = ensure_instance(conn, rec_id, pr.org, pr.repo, iid)
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

        update_instance(conn, db_instance_id, vals)
