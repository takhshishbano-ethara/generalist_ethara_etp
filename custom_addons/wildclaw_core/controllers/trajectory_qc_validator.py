import json
import logging
from typing import Any, Dict, List

_logger = logging.getLogger(__name__)

EXPECTED_TOP_KEYS_STANDARD = {"meta_info", "messages"}
EXPECTED_META_KEYS = {"task_type", "task_description", "task_completion_status", "system_prompt", "platform"}
OPTIONAL_META_KEYS = {"session_id", "conv_id"}
VALID_TASK_TYPES = {
    "home_and_organization", "customer_service", "research_and_analysis",
    "creative_writing", "technical_support", "education_and_learning",
    "health_and_wellness", "finance_and_budgeting",
}
VALID_COMPLETION_STATUSES = {"success", "partial_success", "incomplete", "failure"}
KNOWN_PLATFORMS = {"macOS", "iOS", "Android", "Windows", "Linux", "web"}
VALID_ROLES = {"user", "assistant", "toolResult"}
VALID_CONTENT_TYPES = {"text", "thinking", "toolCall", "toolResult"}

SEVERITY_BLOCK = "BLOCK"
SEVERITY_WARNING = "WARNING"
SEVERITY_ADVISORY = "ADVISORY"


def validate_trajectory(path: str, raw_json: Any) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    if not isinstance(raw_json, dict):
        checks.append({"severity": SEVERITY_BLOCK, "code": "not_object",
                       "message": "trajectory root must be a JSON object"})
        return checks

    top_keys = set(raw_json.keys())
    missing = EXPECTED_TOP_KEYS_STANDARD - top_keys
    if missing:
        checks.append({"severity": SEVERITY_BLOCK, "code": "missing_top_keys",
                       "message": f"missing required top-level keys: {sorted(missing)}"})

    meta = raw_json.get("meta_info") or {}
    if isinstance(meta, dict):
        meta_missing = EXPECTED_META_KEYS - set(meta.keys())
        if meta_missing:
            checks.append({"severity": SEVERITY_BLOCK, "code": "missing_meta_keys",
                           "message": f"missing required meta_info keys: {sorted(meta_missing)}"})
        if meta.get("task_type") and meta["task_type"] not in VALID_TASK_TYPES:
            checks.append({"severity": SEVERITY_WARNING, "code": "unknown_task_type",
                           "message": f"unknown task_type: {meta['task_type']}"})
        if meta.get("task_completion_status") and meta["task_completion_status"] not in VALID_COMPLETION_STATUSES:
            checks.append({"severity": SEVERITY_BLOCK, "code": "bad_completion_status",
                           "message": f"invalid task_completion_status: {meta['task_completion_status']}"})
        if meta.get("platform") and meta["platform"] not in KNOWN_PLATFORMS:
            checks.append({"severity": SEVERITY_ADVISORY, "code": "unknown_platform",
                           "message": f"unknown platform: {meta['platform']}"})

    messages = raw_json.get("messages") or []
    if not isinstance(messages, list) or len(messages) == 0:
        checks.append({"severity": SEVERITY_BLOCK, "code": "no_messages",
                       "message": "messages array missing or empty"})
    else:
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                checks.append({"severity": SEVERITY_BLOCK, "code": "bad_message",
                               "message": f"messages[{idx}] is not an object"})
                continue
            role = msg.get("role")
            if role not in VALID_ROLES:
                checks.append({"severity": SEVERITY_BLOCK, "code": "bad_role",
                               "message": f"messages[{idx}].role invalid: {role}"})

    return checks


def build_report(path: str, checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    fails = [c for c in checks if c["severity"] == SEVERITY_BLOCK]
    warns = [c for c in checks if c["severity"] == SEVERITY_WARNING]
    passes = [c for c in checks if c["severity"] == SEVERITY_ADVISORY]
    severity = SEVERITY_BLOCK if (fails or len(warns) >= 5) else (SEVERITY_WARNING if warns else SEVERITY_ADVISORY)
    return {
        "severity": severity,
        "summary": f"{len(fails)} fails, {len(warns)} warns, {len(passes)} advisories",
        "total_fails": len(fails),
        "total_warns": len(warns),
        "total_passes": len(passes),
        "checks": checks,
    }
