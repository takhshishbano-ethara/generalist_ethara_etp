import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from ..tools.util import AuroraPipelineError

_logger = logging.getLogger(__name__)

_ALLOWED_COLUMNS = frozenset({
    "step1_status", "step1_file", "step2_status", "step2_file",
    "step3_status", "step3_file", "step4_status", "step4_file",
    "step5_status", "step5_file", "step6_status", "step6_file",
    "step1_log", "step2_log", "step3_log", "step4_log", "step5_log", "step6_log",
    "stage", "pr_count", "filtered_pr_count", "tag_count",
    "group_count", "issue_count", "dataset_count",
    "dataset_url", "dataset_filename", "progress_text",
    "last_heartbeat",
    "phase1_status", "phase1_file",
    "phase2_status", "phase2_file", "phase2_image_count",
    "phase2_instance_count", "phase2_resolved_count",
    "phase2_log", "phase2_has_registry",
    "phase3_status", "phase3_file", "phase3_inference_count",
    "phase3_pass_at_k", "phase3_log",
})

_MAX_LOG_SIZE = 500_000


def _update_pipeline(cr: Any, rec_id: int, vals: dict[str, Any]) -> None:
    if not vals:
        return
    invalid = set(vals) - _ALLOWED_COLUMNS
    if invalid:
        raise ValueError(f"Attempted to update disallowed columns: {invalid}")
    sorted_keys = sorted(vals.keys())
    sets = ", ".join(f"{k} = %s" for k in sorted_keys)
    cr.execute(
        f"UPDATE aurora_pipeline SET {sets} WHERE id = %s",
        [vals[k] for k in sorted_keys] + [rec_id],
    )


def _append_log(cr: Any, rec_id: int, msg: str) -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    cr.execute(
        "UPDATE aurora_pipeline SET log = RIGHT(COALESCE(log, '') || %s, %s) WHERE id = %s",
        [line + "\n", _MAX_LOG_SIZE, rec_id],
    )


def _append_step_log(cr: Any, rec_id: int, step_num: int, msg: str) -> None:
    col = f"step{step_num}_log"
    if col not in _ALLOWED_COLUMNS:
        return
    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    cr.execute(
        f"UPDATE aurora_pipeline SET {col} = RIGHT(COALESCE({col}, '') || %s, %s) WHERE id = %s",
        [line + "\n", _MAX_LOG_SIZE, rec_id],
    )


def _heartbeat(cr: Any, rec_id: int, progress_text: Optional[str] = None) -> None:
    vals = {"last_heartbeat": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    if progress_text is not None:
        vals["progress_text"] = progress_text
    _update_pipeline(cr, rec_id, vals)


def _fail_pipeline(cr: Any, rec_id: int, step_field: str, exc) -> None:
    _update_pipeline(cr, rec_id, {step_field: "failed", "stage": "failed"})
    _append_log(cr, rec_id, f"FAILED ({step_field}): {exc}")


def _count_jsonl_lines(filepath: Optional[str]) -> int:
    if not filepath or not os.path.isfile(filepath):
        return 0
    with open(filepath, "r") as f:
        return sum(1 for _ in f)


def _validate_step_output(filepath: Optional[str], step_num: int) -> None:
    import json
    if not filepath or not os.path.isfile(filepath):
        raise AuroraPipelineError(f"Step {step_num} output file missing: {filepath}")
    size = os.path.getsize(filepath)
    if size == 0:
        raise AuroraPipelineError(f"Step {step_num} output file is empty: {filepath}")
    with open(filepath, "r") as f:
        first_line = f.readline().strip()
        if first_line:
            try:
                json.loads(first_line)
            except json.JSONDecodeError as exc:
                raise AuroraPipelineError(
                    f"Step {step_num} output file has invalid JSONL on line 1: {exc}"
                ) from exc
