# -*- coding: utf-8 -*-
import json
import re
from collections import Counter
from datetime import datetime

HEX8 = re.compile(r"^[0-9a-f]{8}$")
TOOLUSE_PREFIX = re.compile(r"^tooluse_")

EXPECTED_META_KEYS = {"task_type", "task_description", "task_completion_status", "system_prompt", "platform"}
VALID_COMPLETION = {"success", "partial_success", "incomplete", "failure"}
WRAPPER_KEYS = {"type", "id", "parentId", "timestamp", "message"}
USER_INNER_KEYS = {"role", "content"}
ASSISTANT_INNER_KEYS = {"role", "content", "stopReason", "responseId"}
TOOL_RESULT_INNER_KEYS = {"role", "toolCallId", "toolName", "isError", "content"}
VALID_STOP_REASONS = {"tool_calls", "stop"}
VALID_CONTENT_TYPES = {"text", "thinking", "toolCall"}
TEXT_BLOCK_KEYS = {"type", "text"}
THINKING_BLOCK_KEYS = {"type", "thinking", "thinkingSignature"}
TOOL_CALL_BLOCK_KEYS = {"type", "id", "name", "arguments", "partialArgs"}


def _strip_fences(raw):
    s = (raw or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3].rstrip()
    return s


def validate_trajectory(json_str, task_data=None):
    cleaned = _strip_fences(json_str)
    errors = []

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        if cleaned and not cleaned.rstrip().endswith("}"):
            errors.append(_e("TRUNCATED", "JSON truncated (likely hit max_tokens). Parse error: %s" % e))
        else:
            errors.append(_e("PARSE_ERROR", "Invalid JSON: %s" % e))
        return _result(False, errors, {}, {})

    if not isinstance(data, dict):
        errors.append(_e("ROOT_TYPE", "Root must be object, got %s" % type(data).__name__))
        return _result(False, errors, {}, {})

    captured = {}
    counts = Counter()

    has_meta = "meta_info" in data
    has_conv = "conversation" in data or "messages" in data
    counts["top_level_keys"] = len(data)

    if not has_meta:
        errors.append(_e("MISSING_TOP_KEY", "Missing 'meta_info'"))
    if not has_conv:
        errors.append(_e("MISSING_TOP_KEY", "Missing 'conversation' (or 'messages')"))

    extra_top = set(data.keys()) - {"meta_info", "conversation", "messages"}
    if extra_top:
        errors.append(_e("EXTRA_TOP_KEY", "Unexpected top-level keys: %s" % sorted(extra_top)))

    meta = data.get("meta_info", {})
    if has_meta:
        _validate_meta(meta, errors, captured, counts)

    messages = data.get("conversation") or data.get("messages", [])
    if not isinstance(messages, list):
        errors.append(_e("CONV_TYPE", "conversation must be array, got %s" % type(messages).__name__))
        return _result(len(errors) == 0, errors, captured, counts)

    counts["messages_total"] = len(messages)

    if not messages:
        errors.append(_e("EMPTY_CONV", "conversation array is empty"))
        return _result(len(errors) == 0, errors, captured, counts)

    _validate_messages(messages, errors, captured, counts, task_data)

    return _result(len(errors) == 0, errors, captured, counts)


def _validate_meta(meta, errors, captured, counts):
    if not isinstance(meta, dict):
        errors.append(_e("META_TYPE", "meta_info must be object"))
        return

    actual = set(meta.keys())
    missing = EXPECTED_META_KEYS - actual
    extra = actual - EXPECTED_META_KEYS
    counts["meta_info_keys"] = len(actual)

    if missing:
        errors.append(_e("META_MISSING_KEYS", "meta_info missing: %s" % sorted(missing)))
    if extra:
        errors.append(_e("META_EXTRA_KEYS", "meta_info unexpected keys: %s" % sorted(extra)))

    captured["task_type"] = meta.get("task_type", "")
    captured["task_description"] = meta.get("task_description", "")
    captured["task_completion_status"] = meta.get("task_completion_status", "")
    captured["system_prompt"] = repr(meta.get("system_prompt", "<ABSENT>"))
    captured["platform"] = meta.get("platform", "")

    if not meta.get("task_type"):
        errors.append(_e("META_EMPTY", "task_type is empty"))
    if not meta.get("task_description"):
        errors.append(_e("META_EMPTY", "task_description is empty"))

    status = meta.get("task_completion_status", "")
    if status not in VALID_COMPLETION:
        errors.append(_e("META_INVALID", "task_completion_status '%s' not in %s" % (status, sorted(VALID_COMPLETION))))

    sp = meta.get("system_prompt")
    if sp is None:
        pass
    elif sp != "":
        errors.append(_e("META_INVALID", "system_prompt must be empty string, got: '%s'" % str(sp)[:60]))

    plat = meta.get("platform")
    if plat is not None and plat != "macOS":
        errors.append(_e("META_INVALID", "platform must be 'macOS', got: '%s'" % plat))


def _validate_messages(messages, errors, captured, counts, task_data):
    all_ids = []
    all_roles = []
    tool_call_ids_seen = set()
    tool_result_ids_seen = set()
    tool_names = Counter()
    content_type_counts = Counter()
    spawn_task_names = []
    spawn_child_keys = []
    yield_messages_list = []

    prev_ts = None
    ts_issues = 0

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            errors.append(_e("MSG_TYPE", "conversation[%d] is not an object" % idx))
            continue

        present = set(msg.keys())
        missing_w = WRAPPER_KEYS - present
        extra_w = present - WRAPPER_KEYS
        if missing_w:
            errors.append(_e("WRAPPER_MISSING", "conversation[%d] missing wrapper keys: %s" % (idx, sorted(missing_w))))
        if extra_w:
            errors.append(_e("WRAPPER_EXTRA", "conversation[%d] unexpected wrapper keys: %s" % (idx, sorted(extra_w))))

        if msg.get("type") != "message":
            errors.append(_e("WRAPPER_TYPE", "conversation[%d].type = '%s', expected 'message'" % (idx, msg.get("type"))))

        msg_id = msg.get("id", "")
        if msg_id:
            all_ids.append(msg_id)
            if not HEX8.match(msg_id):
                errors.append(_e("ID_FORMAT", "conversation[%d].id '%s' not 8-hex" % (idx, msg_id)))
        else:
            errors.append(_e("ID_MISSING", "conversation[%d].id is missing or empty" % idx))

        parent_id = msg.get("parentId", "")
        if idx == 0:
            if parent_id != "00000000":
                errors.append(_e("PARENT_FIRST", "conversation[0].parentId = '%s', expected '00000000'" % parent_id))
        else:
            expected_parent = messages[idx - 1].get("id", "") if isinstance(messages[idx - 1], dict) else ""
            if parent_id != expected_parent:
                errors.append(_e("PARENT_CHAIN", "conversation[%d].parentId = '%s', expected '%s'" % (idx, parent_id, expected_parent)))

        ts_str = msg.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if prev_ts and ts < prev_ts:
                    ts_issues += 1
                prev_ts = ts
            except (ValueError, TypeError):
                errors.append(_e("TIMESTAMP_FORMAT", "conversation[%d].timestamp '%s' invalid ISO 8601" % (idx, ts_str[:40])))
        else:
            errors.append(_e("TIMESTAMP_MISSING", "conversation[%d].timestamp is missing" % idx))

        inner = msg.get("message")
        if not isinstance(inner, dict):
            errors.append(_e("INNER_TYPE", "conversation[%d].message is not an object" % idx))
            continue

        role = inner.get("role", "")
        all_roles.append(role)
        counts["role_%s" % role] += 1

        if role == "user":
            _check_keys(inner, USER_INNER_KEYS, "conversation[%d].message (user)" % idx, errors, strict=False)
        elif role == "assistant":
            _check_keys(inner, ASSISTANT_INNER_KEYS, "conversation[%d].message (assistant)" % idx, errors)
            sr = inner.get("stopReason", "")
            if sr and sr not in VALID_STOP_REASONS:
                errors.append(_e("STOP_REASON", "conversation[%d] stopReason '%s' not in %s" % (idx, sr, sorted(VALID_STOP_REASONS))))
            rid = inner.get("responseId", "")
            if rid and not rid.startswith("chatcmpl-"):
                errors.append(_e("RESPONSE_ID", "conversation[%d] responseId '%s' should start with 'chatcmpl-'" % (idx, rid[:30])))
        elif role == "toolResult":
            _check_keys(inner, TOOL_RESULT_INNER_KEYS, "conversation[%d].message (toolResult)" % idx, errors, strict=False)
            tc_id = inner.get("toolCallId", "")
            if tc_id:
                tool_result_ids_seen.add(tc_id)
            tn = inner.get("toolName", "")
            if "isError" not in inner:
                errors.append(_e("ISERROR_MISSING", "conversation[%d] toolResult missing 'isError'" % idx))
            if tn in ("sessions_spawn", "sessions_yield", "exec"):
                if "details" not in inner:
                    errors.append(_e("DETAILS_MISSING", "conversation[%d] toolResult(%s) missing 'details'" % (idx, tn)))
                else:
                    details = inner.get("details", {})
                    if tn == "sessions_spawn":
                        csk = details.get("childSessionKey", "")
                        if csk:
                            spawn_child_keys.append(csk)
                        tn_detail = details.get("taskName", "")
                        if tn_detail:
                            spawn_task_names.append(tn_detail)
                    elif tn == "sessions_yield":
                        ym = details.get("message", "")
                        if ym:
                            yield_messages_list.append(ym[:80])
        else:
            errors.append(_e("UNKNOWN_ROLE", "conversation[%d] role '%s' not in (user, assistant, toolResult)" % (idx, role)))

        content = inner.get("content")
        if not isinstance(content, list):
            if role != "toolResult":
                errors.append(_e("CONTENT_TYPE", "conversation[%d] content is not array" % idx))
            continue

        for bi, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            content_type_counts[btype] += 1

            if btype == "text":
                _check_block_keys(block, TEXT_BLOCK_KEYS, "conversation[%d].content[%d] (text)" % (idx, bi), errors)
            elif btype == "thinking":
                _check_block_keys(block, THINKING_BLOCK_KEYS, "conversation[%d].content[%d] (thinking)" % (idx, bi), errors)
            elif btype == "toolCall":
                _check_block_keys(block, TOOL_CALL_BLOCK_KEYS, "conversation[%d].content[%d] (toolCall)" % (idx, bi), errors)
                tc_id = block.get("id", "")
                if tc_id:
                    tool_call_ids_seen.add(tc_id)
                    if not TOOLUSE_PREFIX.match(tc_id):
                        errors.append(_e("TOOLCALL_ID_PREFIX", "conversation[%d].content[%d] toolCall.id '%s' should start with 'tooluse_'" % (idx, bi, tc_id[:30])))
                name = block.get("name", "")
                if name:
                    tool_names[name] += 1
                    if name == "sessions_spawn":
                        args = block.get("arguments", {})
                        task_str = args.get("task", "")
                        tn_arg = args.get("taskName", "")
                        runtime = args.get("runtime", "")
                        if not task_str:
                            errors.append(_e("SPAWN_ARGS", "conversation[%d] sessions_spawn missing 'task' in arguments" % idx))
                        if not tn_arg:
                            errors.append(_e("SPAWN_ARGS", "conversation[%d] sessions_spawn missing 'taskName' in arguments" % idx))
                        if runtime != "subagent":
                            errors.append(_e("SPAWN_ARGS", "conversation[%d] sessions_spawn runtime='%s', expected 'subagent'" % (idx, runtime)))
                if not block.get("partialArgs") and block.get("partialArgs") != "":
                    errors.append(_e("PARTIAL_ARGS", "conversation[%d].content[%d] toolCall missing 'partialArgs'" % (idx, bi)))
            elif btype:
                errors.append(_e("UNKNOWN_BLOCK_TYPE", "conversation[%d].content[%d] type '%s' not in (text, thinking, toolCall)" % (idx, bi, btype)))

    if ts_issues:
        errors.append(_e("TIMESTAMP_ORDER", "%d timestamp(s) go backwards (non-monotonic)" % ts_issues))

    id_counts = Counter(all_ids)
    dupes = {k: v for k, v in id_counts.items() if v > 1}
    if dupes:
        errors.append(_e("DUPLICATE_IDS", "Duplicate message IDs: %s" % dict(dupes)))

    unmatched_calls = tool_call_ids_seen - tool_result_ids_seen
    if unmatched_calls:
        errors.append(_e("UNMATCHED_TOOLCALL", "%d toolCall(s) without matching toolResult: %s" % (len(unmatched_calls), sorted(unmatched_calls)[:5])))

    role_counter = Counter(all_roles)
    if role_counter.get("user", 0) != 1:
        errors.append(_e("USER_COUNT", "Expected exactly 1 user message, found %d" % role_counter.get("user", 0)))
    if role_counter.get("assistant", 0) < 1:
        errors.append(_e("NO_ASSISTANT", "No assistant messages found"))

    if all_roles and all_roles[0] != "user":
        errors.append(_e("FIRST_ROLE", "First message role is '%s', expected 'user'" % all_roles[0]))
    if all_roles and all_roles[-1] != "assistant":
        errors.append(_e("LAST_ROLE", "Last message role is '%s', expected 'assistant'" % all_roles[-1]))

    if "sessions_spawn" not in tool_names:
        errors.append(_e("NO_SPAWN", "No sessions_spawn toolCall found"))
    if "sessions_yield" not in tool_names:
        errors.append(_e("NO_YIELD", "No sessions_yield toolCall found"))

    captured["message_ids"] = all_ids
    captured["roles_sequence"] = all_roles
    captured["tool_names"] = dict(tool_names)
    captured["spawn_task_names"] = spawn_task_names
    captured["spawn_child_session_keys"] = spawn_child_keys
    captured["yield_messages"] = yield_messages_list

    counts["unique_message_ids"] = len(set(all_ids))
    for r, c in role_counter.items():
        counts["role_%s" % r] = c
    for ct, c in content_type_counts.items():
        counts["block_%s" % ct] = c
    for tn, c in tool_names.items():
        counts["tool_%s" % tn] = c
    counts["tool_calls_total"] = sum(tool_names.values())
    counts["spawns"] = tool_names.get("sessions_spawn", 0)
    counts["yields"] = tool_names.get("sessions_yield", 0)


def _check_keys(obj, required, path, errors, strict=True):
    present = set(obj.keys())
    missing = required - present
    if missing:
        errors.append(_e("MISSING_FIELD", "%s missing required: %s" % (path, sorted(missing))))
    if strict:
        extra = present - required
        if extra and extra != {"details"}:
            pass


def _check_block_keys(block, required, path, errors):
    present = set(block.keys())
    missing = required - present
    if missing:
        errors.append(_e("BLOCK_MISSING", "%s missing: %s" % (path, sorted(missing))))


def _e(code, message):
    return {"code": code, "message": message}


def _result(valid, errors, captured, counts):
    return {
        "valid": valid,
        "errors": errors,
        "warnings": [],
        "captured": captured,
        "counts": dict(counts),
        "stats": {
            "message_count": counts.get("messages_total", 0),
            "tool_call_count": counts.get("tool_calls_total", 0),
            "unique_tools": sorted(set(
                k.replace("tool_", "", 1) for k in counts if k.startswith("tool_") and k != "tool_calls_total"
            )),
            "spawn_count": counts.get("spawns", 0),
            "yield_count": counts.get("yields", 0),
        },
    }


def format_result(result):
    lines = []

    verdict = "PASS" if result["valid"] else "FAIL"
    lines.append("SCHEMA VALIDATION: %s" % verdict)
    lines.append("")

    if result["errors"]:
        lines.append("ERRORS (%d):" % len(result["errors"]))
        for e in result["errors"]:
            lines.append("  [%s] %s" % (e["code"], e["message"]))
        lines.append("")

    counts = result.get("counts", {})
    if counts:
        lines.append("TAG COUNTS:")
        lines.append("  Messages total:     %d" % counts.get("messages_total", 0))
        lines.append("  User messages:      %d" % counts.get("role_user", 0))
        lines.append("  Assistant messages:  %d" % counts.get("role_assistant", 0))
        lines.append("  ToolResult messages: %d" % counts.get("role_toolResult", 0))
        lines.append("  Unique message IDs:  %d" % counts.get("unique_message_ids", 0))
        lines.append("  ---")
        lines.append("  text blocks:         %d" % counts.get("block_text", 0))
        lines.append("  thinking blocks:     %d" % counts.get("block_thinking", 0))
        lines.append("  toolCall blocks:     %d" % counts.get("block_toolCall", 0))
        lines.append("  ---")
        lines.append("  Tool calls total:    %d" % counts.get("tool_calls_total", 0))
        lines.append("  sessions_spawn:      %d" % counts.get("spawns", 0))
        lines.append("  sessions_yield:      %d" % counts.get("yields", 0))

        tool_keys = sorted(k for k in counts if k.startswith("tool_") and k not in ("tool_calls_total",))
        if tool_keys:
            lines.append("  ---")
            for tk in tool_keys:
                display = tk.replace("tool_", "", 1)
                lines.append("  %s: %d" % (display, counts[tk]))
        lines.append("")

    captured = result.get("captured", {})
    if captured:
        lines.append("CAPTURED ELEMENTS:")
        if captured.get("task_type"):
            lines.append("  task_type:              %s" % captured["task_type"])
        if captured.get("task_description"):
            lines.append("  task_description:       %s" % captured["task_description"][:120])
        if captured.get("task_completion_status"):
            lines.append("  task_completion_status:  %s" % captured["task_completion_status"])
        if captured.get("platform"):
            lines.append("  platform:               %s" % captured["platform"])
        if captured.get("system_prompt"):
            lines.append("  system_prompt:          %s" % captured["system_prompt"])

        if captured.get("spawn_task_names"):
            lines.append("  ---")
            lines.append("  Spawned agents (%d):" % len(captured["spawn_task_names"]))
            for tn in captured["spawn_task_names"]:
                lines.append("    - %s" % tn)

        if captured.get("spawn_child_session_keys"):
            lines.append("  Child session keys (%d):" % len(captured["spawn_child_session_keys"]))
            for csk in captured["spawn_child_session_keys"]:
                lines.append("    - %s" % csk[:60])

        if captured.get("yield_messages"):
            lines.append("  Yield messages (%d):" % len(captured["yield_messages"]))
            for ym in captured["yield_messages"]:
                lines.append("    - %s" % ym)

        if captured.get("tool_names"):
            lines.append("  ---")
            lines.append("  Tools used:")
            for tn, c in sorted(captured["tool_names"].items()):
                lines.append("    %s: %d call(s)" % (tn, c))

        if captured.get("roles_sequence"):
            lines.append("  ---")
            lines.append("  Role sequence: %s" % " → ".join(captured["roles_sequence"]))
        lines.append("")

    return "\n".join(lines)
