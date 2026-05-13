# -*- coding: utf-8 -*-
"""
Deterministic trajectory QC validator.

Validates trajectory JSON against the full Skoll trajectory spec.
Extracted from the standalone script.py for use as an importable module.

Usage:
    from .trajectory_qc_validator import validate_trajectory, build_report

    checks = validate_trajectory("<virtual>", raw_json_string)
    report = build_report("<virtual>", checks)
    # report = {severity, summary, total_fails, total_warns, total_passes, checks}
"""

import json
import re
from datetime import datetime
from difflib import SequenceMatcher

# ── Constants ────────────────────────────────────────────────────────────────

EXPECTED_TOP_KEYS_STANDARD = {"meta_info", "messages"}
EXPECTED_TOP_KEYS_HINTS = {"meta_info", "past_conversations", "messages"}

EXPECTED_META_KEYS = {
    "task_type",
    "task_description",
    "task_completion_status",
    "system_prompt",
    "platform",
}

OPTIONAL_META_KEYS = {"session_id", "conv_id"}

VALID_TASK_TYPES = {
    "home_and_organization",
    "customer_service",
    "research_and_analysis",
    "creative_writing",
    "technical_support",
    "education_and_learning",
    "health_and_wellness",
    "finance_and_budgeting",
}

VALID_COMPLETION_STATUSES = {"success", "partial_success", "incomplete", "failure"}

KNOWN_PLATFORMS = {"macOS", "iOS", "Android", "Windows", "Linux", "web"}

VALID_ROLES = {"user", "assistant", "toolResult"}

VALID_CONTENT_TYPES = {"text", "thinking", "toolCall", "toolResult"}

CORE_TOOLS = {
    "web_search",
    "web_fetch",
    "zeitgeist",
    "read",
    "write",
    "edit",
    "exec",
    "process",
    "memory_search",
    "memory_get",
    "cron",
    "subagents",
    "message",
    "nodes",
    "session_status",
    "browser",
}

SKILL_TOOLS = {
    "Artifacts",
    "Spaces",
    "Imagine",
    "Slides",
    "Skill Creator",
    "Gmail",
    "Outlook Mail",
    "Apple Email",
    "Google Calendar",
    "Outlook Calendar",
    "Apple Calendar",
    "Google Contacts",
    "Outlook Contacts",
    "Apple Contacts",
    "WhatsApp",
    "Telegram",
    "Facebook Search",
    "Instagram Search",
    "Threads Search",
    "Polymarket",
    "Oura",
    "Withings",
    "Strava",
    "Tessie",
    "Shopping",
    "Viator",
    "Eventbrite",
    "Printify",
    "Google Drive",
    "Browser",
    "Wide Research",
    "Documents",
    "Feed",
    "User Context",
    "Self-Awareness",
    "Calendly",
}

SUBAGENT_TOOLS = {"sessions_spawn", "sessions_yield"}

ALL_VALID_TOOLS = CORE_TOOLS | SKILL_TOOLS | SUBAGENT_TOOLS

UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
HEX_ID_REGEX = re.compile(r"^[0-9a-f]{8}$")
ISO8601_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})?$"
)

PLACEHOLDER_PATTERNS = {
    "tbd",
    "test",
    "todo",
    "placeholder",
    "test task",
    "n/a",
    "na",
    "none",
    "xxx",
    "asdf",
}


# ── Check Result Helpers ─────────────────────────────────────────────────────


def make_check(num, name, verdict, reason, fix=None):
    c = {"check": num, "name": name, "verdict": verdict, "reason": reason}
    if fix:
        c["fix"] = fix
    return c


def pass_check(num, name, reason="OK"):
    return make_check(num, name, "PASS", reason)


def warn_check(num, name, reason, fix=None):
    return make_check(num, name, "WARN", reason, fix)


def fail_check(num, name, reason, fix=None):
    return make_check(num, name, "FAIL", reason, fix)


# ── ISO 8601 Timestamp Parsing ───────────────────────────────────────────────


def parse_iso8601(ts_str):
    if not isinstance(ts_str, str):
        return None, "timestamp is not a string"
    if not ISO8601_REGEX.match(ts_str):
        return None, f"timestamp '{ts_str}' does not match ISO 8601 format"

    try:
        normalized = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt, None
    except (ValueError, TypeError) as e:
        return None, f"timestamp '{ts_str}' cannot be parsed: {e}"


def check_seconds_overflow(ts_str):
    m = re.search(r"T\d{2}:\d{2}:(\d{2})", ts_str)
    if m:
        seconds = int(m.group(1))
        if seconds >= 60:
            return True, seconds
    return False, None


# ── Per-Message Envelope Validation Helper ───────────────────────────────────


def validate_envelopes(envelopes, label_prefix):
    envelope_issues = []
    inner_msg_issues = []
    role_issues = []
    content_array_issues = []
    content_type_issues = []
    content_structure_issues = []

    prev_timestamp = None
    timestamp_issues = []
    seen_ids = {}
    duplicate_id_issues = []
    tool_call_ids = {}
    tool_result_ids = {}
    parent_id_issues = []
    first_role = None

    for idx, msg_envelope in enumerate(envelopes):
        msg_label = f"{label_prefix}[{idx}]"

        if not isinstance(msg_envelope, dict):
            envelope_issues.append(
                f"{msg_label}: not an object ({type(msg_envelope).__name__})"
            )
            continue

        msg_type = msg_envelope.get("type")
        if msg_type != "message":
            if msg_type is None:
                envelope_issues.append(f"{msg_label}: missing 'type' field")
            else:
                envelope_issues.append(
                    f"{msg_label}: type='{msg_type}', expected 'message'"
                )

        msg_id = msg_envelope.get("id")
        if msg_id is None or (isinstance(msg_id, str) and not msg_id.strip()):
            envelope_issues.append(f"{msg_label}: 'id' is missing or empty")
        elif not isinstance(msg_id, str):
            envelope_issues.append(
                f"{msg_label}: 'id' is {type(msg_id).__name__}, expected string"
            )
        else:
            if not HEX_ID_REGEX.match(msg_id):
                envelope_issues.append(
                    f"{msg_label}: id='{msg_id}' deviates from expected ^[0-9a-f]{{8}}$ format [WARN]"
                )
            if msg_id in seen_ids:
                duplicate_id_issues.append(
                    f"Duplicate message id '{msg_id}' at {label_prefix} indices {seen_ids[msg_id]} and {idx}"
                )
            else:
                seen_ids[msg_id] = idx

        parent_id = msg_envelope.get("parentId")
        if idx == 0:
            if parent_id is not None and not isinstance(parent_id, str):
                envelope_issues.append(
                    f"{msg_label}: parentId should be string or null for first message"
                )
        else:
            if parent_id is None:
                parent_id_issues.append(
                    f"{msg_label}: parentId is null but this is not the first message"
                )
            elif not isinstance(parent_id, str):
                envelope_issues.append(
                    f"{msg_label}: parentId is {type(parent_id).__name__}, expected string or null"
                )

        ts = msg_envelope.get("timestamp")
        if ts is None:
            envelope_issues.append(f"{msg_label}: 'timestamp' is missing")
        else:
            overflow, secs = check_seconds_overflow(str(ts))
            if overflow:
                envelope_issues.append(
                    f"{msg_label}: timestamp has seconds={secs} (>= 60) [BLOCK]"
                )
            else:
                dt, err = parse_iso8601(ts)
                if err:
                    envelope_issues.append(f"{msg_label}: {err} [BLOCK]")
                else:
                    if (
                        prev_timestamp is not None
                        and dt is not None
                        and dt < prev_timestamp
                    ):
                        timestamp_issues.append(
                            f"{msg_label}: timestamp {ts} is before previous message's timestamp"
                        )
                    prev_timestamp = dt

        if "message" not in msg_envelope:
            envelope_issues.append(f"{msg_label}: 'message' field is missing")
            continue

        inner_msg = msg_envelope.get("message")

        if not isinstance(inner_msg, dict):
            inner_msg_issues.append(
                f"{msg_label}.message: not an object ({type(inner_msg).__name__})"
            )
            continue

        has_role = "role" in inner_msg
        has_content = "content" in inner_msg
        if not has_role:
            inner_msg_issues.append(f"{msg_label}.message: missing 'role' key")
        if not has_content:
            inner_msg_issues.append(f"{msg_label}.message: missing 'content' key")
        if not has_role or not has_content:
            continue

        role = inner_msg["role"]
        if role not in VALID_ROLES:
            role_issues.append(
                f"{msg_label}: role='{role}' is invalid (case-sensitive, must be one of {sorted(VALID_ROLES)})"
            )
        else:
            if first_role is None:
                first_role = role

        if role == "toolResult":
            tr_call_id = inner_msg.get("toolCallId")
            if tr_call_id and isinstance(tr_call_id, str):
                tool_result_ids[tr_call_id] = idx
            tr_is_error = inner_msg.get("isError")
            if tr_is_error is not None and not isinstance(tr_is_error, bool):
                content_structure_issues.append(
                    f"{msg_label}: toolResult role message has isError={tr_is_error!r} "
                    f"({type(tr_is_error).__name__}), expected boolean [WARN]"
                )
            tr_tool_name = inner_msg.get("toolName")
            if tr_tool_name is None:
                content_structure_issues.append(
                    f"{msg_label}: toolResult role message missing 'toolName' [WARN]"
                )
            elif not isinstance(tr_tool_name, str) or not tr_tool_name.strip():
                content_structure_issues.append(
                    f"{msg_label}: toolResult role message has empty/invalid 'toolName' [WARN]"
                )
            tr_content = inner_msg.get("content")
            if tr_content is None:
                content_structure_issues.append(
                    f"{msg_label}: toolResult role message missing 'content' [WARN]"
                )

        content = inner_msg["content"]
        if not isinstance(content, list):
            content_array_issues.append(
                f"{msg_label}.message.content: is {type(content).__name__}, expected array"
            )
            continue
        if len(content) == 0:
            content_array_issues.append(f"{msg_label}.message.content: is empty array")
            continue

        for cidx, block in enumerate(content):
            block_label = f"{msg_label}.content[{cidx}]"

            if not isinstance(block, dict):
                content_type_issues.append(
                    f"{block_label}: not an object ({type(block).__name__})"
                )
                continue

            btype = block.get("type")
            if btype is None:
                content_type_issues.append(f"{block_label}: missing 'type' field")
                continue
            if btype not in VALID_CONTENT_TYPES:
                content_type_issues.append(
                    f"{block_label}: type='{btype}' is unrecognized (valid: {sorted(VALID_CONTENT_TYPES)})"
                )
                continue

            if btype == "text":
                text_val = block.get("text")
                if text_val is None:
                    content_structure_issues.append(
                        f"{block_label}: text block missing 'text' field [BLOCK]"
                    )
                elif not isinstance(text_val, str):
                    content_structure_issues.append(
                        f"{block_label}: text block 'text' is {type(text_val).__name__}, expected string [BLOCK]"
                    )
                elif not text_val.strip():
                    content_structure_issues.append(
                        f"{block_label}: text block 'text' is empty [WARN]"
                    )

            elif btype == "thinking":
                thinking_val = block.get("thinking")
                if thinking_val is None:
                    content_structure_issues.append(
                        f"{block_label}: thinking block missing 'thinking' field [BLOCK]"
                    )
                elif not isinstance(thinking_val, str):
                    content_structure_issues.append(
                        f"{block_label}: thinking block 'thinking' is {type(thinking_val).__name__} [BLOCK]"
                    )
                elif not thinking_val.strip():
                    content_structure_issues.append(
                        f"{block_label}: thinking block 'thinking' is empty [WARN]"
                    )

                if "thinkingSignature" not in block:
                    content_structure_issues.append(
                        f"{block_label}: thinking block missing 'thinkingSignature' [WARN]"
                    )

            elif btype == "toolCall":
                nested_tc = block.get("toolCall")
                if isinstance(nested_tc, dict):
                    tc_source = nested_tc
                elif block.get("id") and block.get("name"):
                    tc_source = block
                else:
                    content_structure_issues.append(
                        f"{block_label}: toolCall block missing 'id' and 'name' fields [BLOCK]"
                    )
                    tc_source = None

                if tc_source is None:
                    continue

                tc_id = tc_source.get("id")
                tc_name = tc_source.get("name")
                tc_args = tc_source.get("arguments")

                if tc_id is None or (isinstance(tc_id, str) and not tc_id.strip()):
                    content_structure_issues.append(
                        f"{block_label}: toolCall missing or empty 'id' [BLOCK]"
                    )
                elif isinstance(tc_id, str):
                    tool_call_ids[tc_id] = idx

                if tc_name is None or (
                    isinstance(tc_name, str) and not tc_name.strip()
                ):
                    content_structure_issues.append(
                        f"{block_label}: toolCall missing or empty 'name' [BLOCK]"
                    )
                elif not isinstance(tc_name, str):
                    content_structure_issues.append(
                        f"{block_label}: toolCall 'name' is {type(tc_name).__name__}, expected string [BLOCK]"
                    )
                elif tc_name not in ALL_VALID_TOOLS:
                    content_structure_issues.append(
                        f"{block_label}: toolCall name='{tc_name}' is not a recognized tool [WARN]"
                    )

                if tc_args is not None:
                    if isinstance(tc_args, str):
                        try:
                            json.loads(tc_args)
                        except json.JSONDecodeError as e:
                            content_structure_issues.append(
                                f"{block_label}: toolCall 'arguments' is a string but not valid JSON: {e} [BLOCK]"
                            )
                    elif not isinstance(tc_args, dict):
                        content_structure_issues.append(
                            f"{block_label}: toolCall 'arguments' is {type(tc_args).__name__}, "
                            f"expected string (JSON) or object [WARN]"
                        )

            elif btype == "toolResult":
                nested_tr = block.get("toolResult")
                if not isinstance(nested_tr, dict):
                    content_structure_issues.append(
                        f"{block_label}: toolResult block missing nested 'toolResult' object [BLOCK]"
                    )
                    nested_tr = None
                tr_source = nested_tr if nested_tr is not None else block

                tr_id = tr_source.get("id") or tr_source.get("toolCallId")
                if tr_id and isinstance(tr_id, str):
                    tool_result_ids[tr_id] = idx

                tr_is_error = tr_source.get("isError")
                if tr_is_error is None:
                    content_structure_issues.append(
                        f"{block_label}: toolResult missing 'isError' [WARN]"
                    )
                elif not isinstance(tr_is_error, bool):
                    content_structure_issues.append(
                        f"{block_label}: toolResult 'isError' is {type(tr_is_error).__name__}, expected boolean [WARN]"
                    )

    return {
        "envelope_issues": envelope_issues,
        "inner_msg_issues": inner_msg_issues,
        "role_issues": role_issues,
        "content_array_issues": content_array_issues,
        "content_type_issues": content_type_issues,
        "content_structure_issues": content_structure_issues,
        "timestamp_issues": timestamp_issues,
        "seen_ids": seen_ids,
        "duplicate_id_issues": duplicate_id_issues,
        "tool_call_ids": tool_call_ids,
        "tool_result_ids": tool_result_ids,
        "parent_id_issues": parent_id_issues,
        "first_role": first_role,
    }


def emit_envelope_checks(
    result,
    checks,
    cn_envelope,
    cn_inner_msg,
    cn_role,
    cn_content_array,
    cn_content_type,
    cn_content_structure,
    name_prefix="",
):
    pfx = f"{name_prefix}_" if name_prefix else ""

    envelope_fails = [i for i in result["envelope_issues"] if "[WARN]" not in i]
    envelope_warns = [i for i in result["envelope_issues"] if "[WARN]" in i]

    if envelope_fails:
        checks.append(
            fail_check(
                cn_envelope,
                f"{pfx}message_envelope",
                f"{len(envelope_fails)} envelope error(s): "
                + "; ".join(envelope_fails[:5]),
                "Fix message envelope structure",
            )
        )
    elif envelope_warns:
        checks.append(
            warn_check(
                cn_envelope,
                f"{pfx}message_envelope",
                f"{len(envelope_warns)} envelope warning(s): "
                + "; ".join(envelope_warns[:5]),
                "Consider using standard hex8 IDs",
            )
        )
    else:
        checks.append(
            pass_check(
                cn_envelope, f"{pfx}message_envelope", "All message envelopes valid"
            )
        )

    if result["inner_msg_issues"]:
        checks.append(
            fail_check(
                cn_inner_msg,
                f"{pfx}inner_message_object",
                f"{len(result['inner_msg_issues'])} issue(s): "
                + "; ".join(result["inner_msg_issues"][:5]),
                "Ensure each message has 'role' and 'content'",
            )
        )
    else:
        checks.append(
            pass_check(
                cn_inner_msg,
                f"{pfx}inner_message_object",
                "All inner message objects valid",
            )
        )

    if result["role_issues"]:
        checks.append(
            fail_check(
                cn_role,
                f"{pfx}message_role",
                f"{len(result['role_issues'])} invalid role(s): "
                + "; ".join(result["role_issues"][:5]),
                f"Valid roles: {sorted(VALID_ROLES)}",
            )
        )
    else:
        checks.append(
            pass_check(cn_role, f"{pfx}message_role", "All message roles valid")
        )

    if result["content_array_issues"]:
        checks.append(
            fail_check(
                cn_content_array,
                f"{pfx}content_array",
                f"{len(result['content_array_issues'])} issue(s): "
                + "; ".join(result["content_array_issues"][:5]),
                "content must be a non-empty array",
            )
        )
    else:
        checks.append(
            pass_check(
                cn_content_array, f"{pfx}content_array", "All content arrays valid"
            )
        )

    if result["content_type_issues"]:
        checks.append(
            fail_check(
                cn_content_type,
                f"{pfx}content_block_type",
                f"{len(result['content_type_issues'])} issue(s): "
                + "; ".join(result["content_type_issues"][:5]),
                f"Valid content types: {sorted(VALID_CONTENT_TYPES)}",
            )
        )
    else:
        checks.append(
            pass_check(
                cn_content_type,
                f"{pfx}content_block_type",
                "All content block types valid",
            )
        )

    cs_fails = [i for i in result["content_structure_issues"] if "[BLOCK]" in i]
    cs_warns = [i for i in result["content_structure_issues"] if "[WARN]" in i]
    if cs_fails:
        checks.append(
            fail_check(
                cn_content_structure,
                f"{pfx}content_block_structure",
                f"{len(cs_fails)} structural error(s): " + "; ".join(cs_fails[:5]),
                "Fix content block structures per spec",
            )
        )
    elif cs_warns:
        checks.append(
            warn_check(
                cn_content_structure,
                f"{pfx}content_block_structure",
                f"{len(cs_warns)} structural warning(s): " + "; ".join(cs_warns[:5]),
                "Review content block fields",
            )
        )
    else:
        checks.append(
            pass_check(
                cn_content_structure,
                f"{pfx}content_block_structure",
                "All content block structures valid",
            )
        )


# ── Validation Engine ────────────────────────────────────────────────────────


def detect_hints_mode(data):
    if "past_conversations" in data:
        return True
    return False


def has_mixed_wrappers(messages):
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if isinstance(msg, dict) and "is_accepted" in msg:
            return True
    return False


def unwrap_mixed_messages(messages):
    unwrapped = []
    for msg in messages:
        if not isinstance(msg, dict):
            unwrapped.append(msg)
            continue
        if "is_accepted" in msg and "message" in msg:
            unwrapped.append(msg["message"])
        else:
            unwrapped.append(msg)
    return unwrapped


def validate_trajectory(file_path, raw_content):
    """Validate a trajectory JSON string against the full Skoll spec.

    Args:
        file_path: Label for the trajectory (used in report output).
        raw_content: The raw JSON string to validate.

    Returns:
        List of check dicts, each with keys: check, name, verdict, reason, fix (optional).
    """
    checks = []
    check_num = [0]

    def next_num():
        check_num[0] += 1
        return check_num[0]

    # ── Section 2.1: JSON Validity ──────────────────────────────────────────
    cn = next_num()
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as e:
        checks.append(
            fail_check(
                cn, "json_validity", f"Invalid JSON: {e}", "Fix JSON syntax errors"
            )
        )
        return checks

    if not isinstance(data, dict):
        checks.append(
            fail_check(
                cn,
                "json_validity",
                f"Root is {type(data).__name__}, expected object (dict)",
                "Root of JSON must be an object {{}}",
            )
        )
        return checks
    checks.append(pass_check(cn, "json_validity", "Valid JSON, root is object"))

    # ── Section 2.2: Top-Level Key Match (mode-aware) ───────────────────────
    hints_mode = detect_hints_mode(data)
    expected_top = EXPECTED_TOP_KEYS_HINTS if hints_mode else EXPECTED_TOP_KEYS_STANDARD

    cn = next_num()
    actual_top_keys = set(data.keys())
    missing_top = expected_top - actual_top_keys
    extra_top = actual_top_keys - expected_top
    if missing_top:
        checks.append(
            fail_check(
                cn,
                "top_level_keys",
                f"Missing required top-level keys: {sorted(missing_top)}"
                + (f" (hints mode={hints_mode})" if hints_mode else ""),
                f"Add missing keys: {sorted(missing_top)}",
            )
        )
    elif extra_top:
        checks.append(
            warn_check(
                cn,
                "top_level_keys",
                f"Extra top-level keys found: {sorted(extra_top)}"
                + (f" (hints mode={hints_mode})" if hints_mode else ""),
                f"Remove unexpected keys: {sorted(extra_top)}",
            )
        )
    else:
        mode_label = "hints" if hints_mode else "standard"
        checks.append(
            pass_check(
                cn,
                "top_level_keys",
                f"Top-level keys match expected set ({mode_label} mode)",
            )
        )

    # ── Section 3: meta_info Validation ─────────────────────────────────────

    meta_info = data.get("meta_info")

    # ── 3.1: meta_info existence and structure ──────────────────────────────
    cn = next_num()
    if meta_info is None:
        checks.append(
            fail_check(
                cn,
                "meta_info_exists",
                "meta_info key is missing",
                "Add meta_info object",
            )
        )
        meta_info = None
    elif not isinstance(meta_info, dict):
        checks.append(
            fail_check(
                cn,
                "meta_info_exists",
                f"meta_info is {type(meta_info).__name__}, expected object",
                "meta_info must be a JSON object",
            )
        )
        meta_info = None
    else:
        actual_meta_keys = set(meta_info.keys())
        required_missing = EXPECTED_META_KEYS - actual_meta_keys
        allowed_keys = EXPECTED_META_KEYS | OPTIONAL_META_KEYS
        extra_meta = actual_meta_keys - allowed_keys
        issues = []
        if required_missing:
            issues.append(f"Missing required keys: {sorted(required_missing)}")
        if extra_meta:
            issues.append(f"Extra keys: {sorted(extra_meta)}")
        if required_missing:
            checks.append(
                fail_check(
                    cn,
                    "meta_info_structure",
                    "; ".join(issues),
                    f"Add missing keys: {sorted(required_missing)}",
                )
            )
        elif extra_meta:
            checks.append(
                warn_check(
                    cn,
                    "meta_info_structure",
                    "; ".join(issues),
                    f"Remove extra keys: {sorted(extra_meta)}",
                )
            )
        else:
            checks.append(
                pass_check(
                    cn,
                    "meta_info_structure",
                    "meta_info has all expected keys, no extras",
                )
            )

    # ── 3.2: task_type ──────────────────────────────────────────────────────
    cn = next_num()
    if meta_info is None:
        checks.append(
            fail_check(cn, "task_type", "Cannot check: meta_info missing/invalid")
        )
    else:
        tt = meta_info.get("task_type")
        if tt is None:
            checks.append(
                fail_check(cn, "task_type", "task_type key is missing", "Add task_type")
            )
        elif not isinstance(tt, str) or not tt.strip():
            checks.append(
                fail_check(
                    cn,
                    "task_type",
                    "task_type must be a non-empty string",
                    f"Set task_type to one of: {sorted(VALID_TASK_TYPES)}",
                )
            )
        elif tt not in VALID_TASK_TYPES:
            hint = ""
            tt_lower = tt.lower().replace("-", "_").replace(" ", "_")
            for valid in VALID_TASK_TYPES:
                if tt_lower == valid:
                    hint = f" (did you mean '{valid}'? Check casing)"
                    break
                elif tt_lower.startswith(valid[:10]):
                    hint = f" (possible truncation; did you mean '{valid}'?)"
                    break
            checks.append(
                fail_check(
                    cn,
                    "task_type",
                    f"Invalid task_type '{tt}'{hint}",
                    f"Must be one of: {sorted(VALID_TASK_TYPES)}",
                )
            )
        else:
            checks.append(pass_check(cn, "task_type", f"task_type='{tt}' is valid"))

    # ── 3.3: task_description ───────────────────────────────────────────────
    cn = next_num()
    if meta_info is None:
        checks.append(
            fail_check(
                cn, "task_description", "Cannot check: meta_info missing/invalid"
            )
        )
    else:
        td = meta_info.get("task_description")
        if td is None:
            checks.append(
                fail_check(
                    cn,
                    "task_description",
                    "task_description key is missing",
                    "Add task_description string",
                )
            )
        elif not isinstance(td, str):
            checks.append(
                fail_check(
                    cn,
                    "task_description",
                    f"task_description is {type(td).__name__}, expected string",
                    "task_description must be a string",
                )
            )
        elif not td.strip():
            checks.append(
                fail_check(
                    cn,
                    "task_description",
                    "task_description is empty after stripping",
                    "Provide a meaningful task description",
                )
            )
        elif td.strip().lower() in PLACEHOLDER_PATTERNS:
            checks.append(
                fail_check(
                    cn,
                    "task_description",
                    f"task_description '{td.strip()}' is a placeholder value",
                    "Replace placeholder with actual task description",
                )
            )
        elif len(td.strip()) < 20:
            checks.append(
                fail_check(
                    cn,
                    "task_description",
                    f"task_description is only {len(td.strip())} chars, minimum is 20",
                    "Expand task_description to at least 20 characters",
                )
            )
        elif len(td.strip()) < 50:
            checks.append(
                warn_check(
                    cn,
                    "task_description",
                    f"task_description is {len(td.strip())} chars; recommend >= 50 for clarity",
                    "Consider expanding task_description for more detail",
                )
            )
        else:
            checks.append(
                pass_check(
                    cn,
                    "task_description",
                    f"task_description is {len(td.strip())} chars, valid",
                )
            )

    # ── 3.4: task_completion_status ─────────────────────────────────────────
    cn = next_num()
    if meta_info is None:
        checks.append(
            fail_check(
                cn, "task_completion_status", "Cannot check: meta_info missing/invalid"
            )
        )
    else:
        tcs = meta_info.get("task_completion_status")
        if tcs is None:
            checks.append(
                fail_check(
                    cn,
                    "task_completion_status",
                    "task_completion_status key is missing",
                    "Add task_completion_status",
                )
            )
        elif not isinstance(tcs, str) or not tcs.strip():
            checks.append(
                fail_check(
                    cn,
                    "task_completion_status",
                    "task_completion_status must be a non-empty string",
                    f"Must be one of: {sorted(VALID_COMPLETION_STATUSES)}",
                )
            )
        elif tcs not in VALID_COMPLETION_STATUSES:
            checks.append(
                fail_check(
                    cn,
                    "task_completion_status",
                    f"Invalid task_completion_status '{tcs}'",
                    f"Must be one of: {sorted(VALID_COMPLETION_STATUSES)}",
                )
            )
        else:
            checks.append(
                pass_check(
                    cn,
                    "task_completion_status",
                    f"task_completion_status='{tcs}' is valid",
                )
            )

    # ── 3.5: system_prompt ──────────────────────────────────────────────────
    cn = next_num()
    if meta_info is None:
        checks.append(
            fail_check(cn, "system_prompt", "Cannot check: meta_info missing/invalid")
        )
    else:
        sp = meta_info.get("system_prompt")
        if sp is None:
            checks.append(
                fail_check(
                    cn,
                    "system_prompt",
                    "system_prompt key is missing (most commonly failed check)",
                    "Add system_prompt with the actual system prompt text used",
                )
            )
        elif not isinstance(sp, str):
            checks.append(
                fail_check(
                    cn,
                    "system_prompt",
                    f"system_prompt is {type(sp).__name__}, expected string",
                    "system_prompt must be a string",
                )
            )
        elif not sp.strip():
            checks.append(
                fail_check(
                    cn,
                    "system_prompt",
                    "system_prompt is empty (this is the #1 most commonly failed check; "
                    "the schema template has it empty as a placeholder, but actual "
                    "trajectories MUST populate it)",
                    "Populate system_prompt with the actual system prompt text used during the conversation",
                )
            )
        else:
            checks.append(
                pass_check(
                    cn, "system_prompt", f"system_prompt is populated ({len(sp)} chars)"
                )
            )

    # ── 3.6: platform ──────────────────────────────────────────────────────
    cn = next_num()
    if meta_info is None:
        checks.append(
            fail_check(cn, "platform", "Cannot check: meta_info missing/invalid")
        )
    else:
        plat = meta_info.get("platform")
        if plat is None:
            checks.append(
                fail_check(cn, "platform", "platform key is missing", "Add platform")
            )
        elif not isinstance(plat, str) or not plat.strip():
            checks.append(
                fail_check(
                    cn,
                    "platform",
                    "platform must be a non-empty string",
                    "Set platform to a valid value",
                )
            )
        elif plat not in KNOWN_PLATFORMS:
            checks.append(
                warn_check(
                    cn,
                    "platform",
                    f"platform '{plat}' is not a standard known value",
                    f"Known values: {sorted(KNOWN_PLATFORMS)}",
                )
            )
        else:
            checks.append(pass_check(cn, "platform", f"platform='{plat}' is valid"))

    # ── 3.7: session_id (optional) ──────────────────────────────────────────
    cn = next_num()
    if meta_info is None:
        checks.append(
            pass_check(
                cn,
                "session_id",
                "Cannot check: meta_info missing (session_id is optional)",
            )
        )
    else:
        sid = meta_info.get("session_id")
        if sid is None:
            checks.append(
                pass_check(cn, "session_id", "session_id not present (optional field)")
            )
        elif not isinstance(sid, str):
            checks.append(
                warn_check(
                    cn,
                    "session_id",
                    f"session_id is {type(sid).__name__}, expected string",
                    "session_id should be a UUID string",
                )
            )
        elif not UUID_REGEX.match(sid):
            checks.append(
                warn_check(
                    cn,
                    "session_id",
                    f"session_id '{sid}' does not match UUID format",
                    "session_id should match ^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$",
                )
            )
        else:
            checks.append(
                pass_check(cn, "session_id", f"session_id='{sid}' is valid UUID")
            )

    # ── 3.8: conv_id (required in hints mode, optional otherwise) ───────────
    cn = next_num()
    if meta_info is None:
        if hints_mode:
            checks.append(
                fail_check(cn, "conv_id", "Cannot check: meta_info missing/invalid")
            )
        else:
            checks.append(
                pass_check(cn, "conv_id", "conv_id not applicable (standard mode)")
            )
    elif hints_mode:
        cid = meta_info.get("conv_id")
        if cid is None:
            checks.append(
                fail_check(
                    cn,
                    "conv_id",
                    "conv_id is required in hints mode but is missing",
                    "Add conv_id string identifying the conversation being hinted on",
                )
            )
        elif not isinstance(cid, str) or not cid.strip():
            checks.append(
                fail_check(
                    cn,
                    "conv_id",
                    "conv_id must be a non-empty string in hints mode",
                    "Set conv_id to a valid conversation identifier",
                )
            )
        else:
            checks.append(
                pass_check(cn, "conv_id", f"conv_id='{cid}' is present and valid")
            )
    else:
        checks.append(
            pass_check(cn, "conv_id", "conv_id not applicable (standard mode)")
        )

    # ── Section 4: messages Array ───────────────────────────────────────────

    messages = data.get("messages")

    # ── 4.1: messages exists, is array, non-empty ───────────────────────────
    cn = next_num()
    if messages is None:
        checks.append(
            fail_check(
                cn, "messages_exists", "messages key is missing", "Add messages array"
            )
        )
        return checks
    elif not isinstance(messages, list):
        checks.append(
            fail_check(
                cn,
                "messages_exists",
                f"messages is {type(messages).__name__}, expected array",
                "messages must be a JSON array",
            )
        )
        return checks
    elif len(messages) == 0:
        checks.append(
            fail_check(
                cn,
                "messages_exists",
                "messages array is empty",
                "Add at least one message to the trajectory",
            )
        )
        return checks
    else:
        checks.append(
            pass_check(
                cn,
                "messages_exists",
                f"messages is a non-empty array ({len(messages)} elements)",
            )
        )

    # ── Mixed wrapper format: unwrap before validation ──────────────
    mixed = has_mixed_wrappers(messages)
    if mixed and not hints_mode:
        messages = unwrap_mixed_messages(messages)

    # ── Hints mode: unwrap wrappers, validate wrappers & flow ───────────────
    if hints_mode:
        cn_hint_wrapper = next_num()
        cn_hint_flow = next_num()

        wrapper_issues = []
        unwrapped_envelopes = []
        accepted_indices = []

        for idx, wrapper in enumerate(messages):
            w_label = f"messages[{idx}]"
            if not isinstance(wrapper, dict):
                wrapper_issues.append(
                    f"{w_label}: not an object ({type(wrapper).__name__}) [BLOCK]"
                )
                continue

            for required_key in ("is_accepted", "hints", "message"):
                if required_key not in wrapper:
                    wrapper_issues.append(
                        f"{w_label}: missing required key '{required_key}' [BLOCK]"
                    )

            ia = wrapper.get("is_accepted")
            if ia is not None:
                if not isinstance(ia, int) or isinstance(ia, bool):
                    wrapper_issues.append(
                        f"{w_label}: is_accepted={ia!r} is {type(ia).__name__}, expected integer (0 or 1) [BLOCK]"
                    )
                elif ia not in (0, 1):
                    wrapper_issues.append(
                        f"{w_label}: is_accepted={ia}, expected 0 or 1 [BLOCK]"
                    )
                else:
                    if ia == 1:
                        accepted_indices.append(idx)

            h = wrapper.get("hints")
            if h is not None:
                if not isinstance(h, str):
                    wrapper_issues.append(
                        f"{w_label}: hints={h!r} is {type(h).__name__}, expected string or null [BLOCK]"
                    )
                elif not h.strip():
                    wrapper_issues.append(
                        f"{w_label}: hints is an empty string (should be null or non-empty) [WARN]"
                    )

            inner_envelope = wrapper.get("message")
            if inner_envelope is not None:
                unwrapped_envelopes.append(inner_envelope)

        wrapper_fails = [i for i in wrapper_issues if "[BLOCK]" in i]
        wrapper_warns = [i for i in wrapper_issues if "[WARN]" in i]

        if wrapper_fails:
            checks.append(
                fail_check(
                    cn_hint_wrapper,
                    "hint_wrapper",
                    f"{len(wrapper_fails)} hint wrapper error(s): "
                    + "; ".join(wrapper_fails[:5]),
                    "Each hints message must have is_accepted (int 0/1), hints (null or string), message (envelope)",
                )
            )
        elif wrapper_warns:
            checks.append(
                warn_check(
                    cn_hint_wrapper,
                    "hint_wrapper",
                    f"{len(wrapper_warns)} hint wrapper warning(s): "
                    + "; ".join(wrapper_warns[:5]),
                    "Use null instead of empty string for hints field",
                )
            )
        else:
            checks.append(
                pass_check(
                    cn_hint_wrapper,
                    "hint_wrapper",
                    f"All {len(messages)} hint wrappers are structurally valid",
                )
            )

        flow_issues = []
        if len(accepted_indices) == 0:
            flow_issues.append(
                "No entry has is_accepted=1; exactly one is required [BLOCK]"
            )
        elif len(accepted_indices) > 1:
            flow_issues.append(
                f"Multiple entries have is_accepted=1 at indices {accepted_indices}; exactly one allowed [BLOCK]"
            )
        else:
            if accepted_indices[0] != len(messages) - 1:
                flow_issues.append(
                    f"is_accepted=1 is at index {accepted_indices[0]} but must be the last element "
                    f"(index {len(messages) - 1}) [BLOCK]"
                )

        if len(messages) > 0 and isinstance(messages[0], dict):
            first_hints = messages[0].get("hints")
            if first_hints is not None:
                flow_issues.append(
                    f"First entry should have hints=null (no hint for initial attempt), "
                    f"got hints={first_hints!r} [WARN]"
                )

        flow_fails = [i for i in flow_issues if "[BLOCK]" in i]
        flow_warns = [i for i in flow_issues if "[WARN]" in i]

        if flow_fails:
            checks.append(
                fail_check(
                    cn_hint_flow,
                    "hint_flow_pattern",
                    "; ".join(flow_fails),
                    "Exactly one entry must have is_accepted=1, and it must be the last element",
                )
            )
        elif flow_warns:
            checks.append(
                warn_check(
                    cn_hint_flow,
                    "hint_flow_pattern",
                    "; ".join(flow_warns),
                    "First hint entry should have hints=null",
                )
            )
        else:
            checks.append(
                pass_check(
                    cn_hint_flow,
                    "hint_flow_pattern",
                    "Hint flow pattern is valid (exactly one accepted, last entry)",
                )
            )

        envelopes_for_msg_checks = unwrapped_envelopes
    else:
        envelopes_for_msg_checks = messages

    # ── 4.1b: past_conversations validation (hints mode only) ───────────────
    if hints_mode:
        cn_past_conv = next_num()
        past_conv = data.get("past_conversations")
        if past_conv is None:
            checks.append(
                fail_check(
                    cn_past_conv,
                    "past_conversations",
                    "past_conversations key is missing in hints mode",
                    "Add past_conversations array",
                )
            )
        elif not isinstance(past_conv, list):
            checks.append(
                fail_check(
                    cn_past_conv,
                    "past_conversations",
                    f"past_conversations is {type(past_conv).__name__}, expected array",
                    "past_conversations must be a JSON array",
                )
            )
        elif len(past_conv) == 0:
            checks.append(
                pass_check(
                    cn_past_conv,
                    "past_conversations",
                    "past_conversations is empty (no prior context, OK)",
                )
            )
        else:
            pc_result = validate_envelopes(past_conv, "past_conversations")
            cn_pc_env = next_num()
            cn_pc_inner = next_num()
            cn_pc_role = next_num()
            cn_pc_ca = next_num()
            cn_pc_ct = next_num()
            cn_pc_cs = next_num()
            emit_envelope_checks(
                pc_result,
                checks,
                cn_pc_env,
                cn_pc_inner,
                cn_pc_role,
                cn_pc_ca,
                cn_pc_ct,
                cn_pc_cs,
                name_prefix="past_conv",
            )
            checks.append(
                pass_check(
                    cn_past_conv,
                    "past_conversations",
                    f"past_conversations has {len(past_conv)} envelope(s), validated",
                )
            )

    # ── 4.2–4.7: Per-message validation ─────────────────────────────────────
    cn_envelope = next_num()
    cn_inner_msg = next_num()
    cn_role = next_num()
    cn_content_array = next_num()
    cn_content_type = next_num()
    cn_content_structure = next_num()

    msg_result = validate_envelopes(envelopes_for_msg_checks, "messages")
    emit_envelope_checks(
        msg_result,
        checks,
        cn_envelope,
        cn_inner_msg,
        cn_role,
        cn_content_array,
        cn_content_type,
        cn_content_structure,
    )

    # ── Section 5: Conversation-Level Integrity ─────────────────────────────

    # ── 5.1: First message role ─────────────────────────────────────────────
    cn = next_num()
    first_role = msg_result["first_role"]
    if first_role is None:
        checks.append(
            warn_check(
                cn,
                "first_message_role",
                "Could not determine first message role",
                "Ensure first message has role='user'",
            )
        )
    elif first_role != "user":
        checks.append(
            warn_check(
                cn,
                "first_message_role",
                f"First message role is '{first_role}', expected 'user'",
                "Conversation should start with a user message",
            )
        )
    else:
        checks.append(
            pass_check(cn, "first_message_role", "First message role is 'user'")
        )

    # ── 5.2: parentId chain + duplicate IDs ─────────────────────────────────
    cn = next_num()
    chain_issues = list(msg_result["parent_id_issues"])
    seen_ids = msg_result["seen_ids"]

    for idx2, env2 in enumerate(envelopes_for_msg_checks):
        if not isinstance(env2, dict):
            continue
        if idx2 == 0:
            continue
        pid = env2.get("parentId")
        if pid is not None and isinstance(pid, str) and pid not in seen_ids:
            chain_issues.append(
                f"messages[{idx2}]: parentId='{pid}' does not reference any preceding message id"
            )

    duplicate_id_issues = msg_result["duplicate_id_issues"]
    if duplicate_id_issues:
        checks.append(
            fail_check(
                cn,
                "parent_id_chain",
                "Duplicate IDs found: " + "; ".join(duplicate_id_issues),
                "Each message must have a unique id",
            )
        )
    elif chain_issues:
        checks.append(
            warn_check(
                cn,
                "parent_id_chain",
                f"{len(chain_issues)} chain issue(s): " + "; ".join(chain_issues[:5]),
                "Ensure parentId references valid preceding message ids",
            )
        )
    else:
        checks.append(
            pass_check(
                cn, "parent_id_chain", "parentId chain is valid, no duplicate IDs"
            )
        )

    # ── 5.2 addendum: timestamp monotonicity ────────────────────────────────
    cn = next_num()
    timestamp_issues = msg_result["timestamp_issues"]
    if timestamp_issues:
        checks.append(
            warn_check(
                cn,
                "timestamp_monotonicity",
                f"{len(timestamp_issues)} timestamp ordering issue(s): "
                + "; ".join(timestamp_issues[:3]),
                "Timestamps should be monotonically non-decreasing",
            )
        )
    else:
        checks.append(
            pass_check(
                cn,
                "timestamp_monotonicity",
                "Timestamps are monotonically non-decreasing",
            )
        )

    # ── 5.3: toolCall ↔ toolResult pairing ──────────────────────────────────
    cn = next_num()
    all_tool_call_ids = msg_result["tool_call_ids"]
    all_tool_result_ids = msg_result["tool_result_ids"]
    orphan_issues = []
    unpaired_calls = set(all_tool_call_ids.keys()) - set(all_tool_result_ids.keys())
    unpaired_results = set(all_tool_result_ids.keys()) - set(all_tool_call_ids.keys())
    if unpaired_calls:
        for uc in sorted(unpaired_calls):
            orphan_issues.append(
                f"toolCall id='{uc}' (msg {all_tool_call_ids[uc]}) has no matching toolResult"
            )
    if unpaired_results:
        for ur in sorted(unpaired_results):
            orphan_issues.append(
                f"toolResult id='{ur}' (msg {all_tool_result_ids[ur]}) has no matching toolCall"
            )
    if orphan_issues:
        checks.append(
            warn_check(
                cn,
                "tool_call_result_pairing",
                f"{len(orphan_issues)} orphaned tool call/result(s): "
                + "; ".join(orphan_issues[:5]),
                "Every toolCall should have a matching toolResult and vice versa",
            )
        )
    else:
        if all_tool_call_ids or all_tool_result_ids:
            checks.append(
                pass_check(
                    cn,
                    "tool_call_result_pairing",
                    f"All {len(all_tool_call_ids)} toolCall(s) paired with toolResult(s)",
                )
            )
        else:
            checks.append(
                pass_check(
                    cn, "tool_call_result_pairing", "No tool calls/results to pair (OK)"
                )
            )

    # ── 5.4: Minimum message count ──────────────────────────────────────────
    cn = next_num()
    effective_count = len(envelopes_for_msg_checks)
    if effective_count < 3:
        checks.append(
            warn_check(
                cn,
                "minimum_messages",
                f"Only {effective_count} message(s); recommend at least 3",
                "Add more messages to the trajectory",
            )
        )
    else:
        checks.append(
            pass_check(
                cn, "minimum_messages", f"{effective_count} messages (>= 3 minimum)"
            )
        )

    return checks


# ── Report Generation ────────────────────────────────────────────────────────


def build_report(file_path, checks):
    """Build a QC report from validation checks.

    Args:
        file_path: Label for the trajectory.
        checks: List of check dicts from validate_trajectory().

    Returns:
        Dict with keys: severity, summary, total_fails, total_warns, total_passes, checks.
        This matches the format expected by _update_qc_entry / the frontend.
    """
    total_fails = sum(1 for c in checks if c["verdict"] == "FAIL")
    total_warns = sum(1 for c in checks if c["verdict"] == "WARN")
    total_passes = sum(1 for c in checks if c["verdict"] == "PASS")

    if total_fails > 0:
        severity = "critical"
    elif total_warns >= 5:
        severity = "high"
    elif total_warns >= 1:
        severity = "medium"
    else:
        severity = "low"

    fail_names = [c["name"] for c in checks if c["verdict"] == "FAIL"]
    warn_names = [c["name"] for c in checks if c["verdict"] == "WARN"]

    summary_parts = []
    if total_fails:
        summary_parts.append(f"{total_fails} FAIL(s): {', '.join(fail_names[:5])}")
    if total_warns:
        summary_parts.append(f"{total_warns} WARN(s): {', '.join(warn_names[:5])}")
    if not summary_parts:
        summary_parts.append("All checks passed")

    summary = f"{file_path}: {'; '.join(summary_parts)}"

    return {
        "severity": severity,
        "summary": summary,
        "total_fails": total_fails,
        "total_warns": total_warns,
        "total_passes": total_passes,
        "checks": checks,
    }
