# -*- coding: utf-8 -*-
"""Deterministic structural QC for golden trajectories.

Validates against the ideal schema (prompts/ideal_schema.json):
  top-level: meta_info + messages
  meta_info: cluster, task_type, task_description, task_completion_status,
             system_prompt, platform, agents{root, spawned[]}
  messages[]: {type, id, parentId, timestamp, message}
"""
import json
import re
from collections import Counter
from datetime import datetime

# ── ID format ──────────────────────────────────────────────────────
HEX8 = re.compile(r"^[0-9a-f]{8}$")
TOOLUSE_PREFIX = re.compile(r"^tooluse_")

# ── meta_info ──────────────────────────────────────────────────────
EXPECTED_META_KEYS = {
    "cluster", "task_type", "task_description",
    "task_completion_status", "system_prompt", "platform", "agents",
}

VALID_CLUSTERS = {
    "create_and_act",
    "understand_and_find",
    "remember_and_anticipate",
    "navigate_and_adapt",
}

VALID_TASK_TYPES = {
    "search_and_retrieval",
    "productivity_flow",
    "code_intelligence",
    "creative_synthesis",
    "skill_use_and_orchestration",
    "skill_creation_and_editing",
    "communication_and_messaging",
    "device_and_environment_control",
    "memory_and_personalization",
    "scheduling_and_long_running",
    "proactive_assistance",
    "social_interaction",
    "multi_turn_robustness",
    "safety_alignment",
}

AGENTS_KEYS = {"root", "spawned"}

# ── message wrapper ────────────────────────────────────────────────
WRAPPER_KEYS = {"type", "id", "parentId", "timestamp", "message"}

# ── inner message by role ──────────────────────────────────────────
USER_INNER_KEYS = {"role", "content"}
ASSISTANT_REQUIRED_KEYS = {"role", "content"}
ASSISTANT_OPTIONAL_KEYS = {"stopReason", "responseId"}
TOOL_RESULT_INNER_KEYS = {"role", "content", "toolCallId", "toolName", "isError"}

# ── content block keys ─────────────────────────────────────────────
TEXT_BLOCK_KEYS = {"type", "text"}
THINKING_BLOCK_KEYS = {"type", "thinking", "thinkingSignature"}
TOOL_CALL_REQUIRED_KEYS = {"type", "id", "name", "arguments"}
TOOL_CALL_OPTIONAL_KEYS = {"partialArgs"}

# ── spawn arguments (ideal schema uses name+prompt, not task+taskName)
SPAWN_REQUIRED_ARGS = {"name", "prompt"}


def _strip_fences(raw):
    s = (raw or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3].rstrip()
    return s


# ── public API ─────────────────────────────────────────────────────

def validate_trajectory(json_str, task_data=None):
    """Validate golden trajectory JSON against ideal schema.

    Returns dict with: valid, errors, warnings, captured, counts,
    depth_map (structural inventory of every key at every depth).
    """
    cleaned = _strip_fences(json_str)
    errors = []
    warnings = []
    depth_map = {}

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        if cleaned and not cleaned.rstrip().endswith("}"):
            errors.append(_e("TRUNCATED", "JSON truncated (likely hit max_tokens). Parse error: %s" % e))
        else:
            errors.append(_e("PARSE_ERROR", "Invalid JSON: %s" % e))
        return _result(False, errors, warnings, {}, {}, depth_map)

    if not isinstance(data, dict):
        errors.append(_e("ROOT_TYPE", "Root must be object, got %s" % type(data).__name__))
        return _result(False, errors, warnings, {}, {}, depth_map)

    captured = {}
    counts = Counter()

    # ── 1. top-level keys (must be exactly meta_info + messages) ───
    top_keys = set(data.keys())
    counts["top_level_keys"] = len(top_keys)
    depth_map["/"] = sorted(top_keys)

    if "meta_info" not in top_keys:
        errors.append(_e("MISSING_TOP_KEY", "Missing 'meta_info'"))
    if "messages" not in top_keys:
        errors.append(_e("MISSING_TOP_KEY", "Missing 'messages'"))

    extra_top = top_keys - {"meta_info", "messages"}
    if extra_top:
        errors.append(_e("EXTRA_TOP_KEY", "Unexpected top-level keys: %s" % sorted(extra_top)))

    # ── 2. meta_info ───────────────────────────────────────────────
    meta = data.get("meta_info", {})
    if "meta_info" in top_keys:
        _validate_meta(meta, errors, warnings, captured, counts, depth_map)

    # ── 3. messages ────────────────────────────────────────────────
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        errors.append(_e("MESSAGES_TYPE", "messages must be array, got %s" % type(messages).__name__))
        return _result(len(errors) == 0, errors, warnings, captured, counts, depth_map)

    counts["messages_total"] = len(messages)
    if not messages:
        errors.append(_e("EMPTY_MESSAGES", "messages array is empty"))
        return _result(len(errors) == 0, errors, warnings, captured, counts, depth_map)

    _validate_messages(messages, errors, warnings, captured, counts, depth_map, task_data)

    return _result(len(errors) == 0, errors, warnings, captured, counts, depth_map)


# ── meta_info validation ───────────────────────────────────────────

def _validate_meta(meta, errors, warnings, captured, counts, depth_map):
    if not isinstance(meta, dict):
        errors.append(_e("META_TYPE", "meta_info must be object"))
        return

    actual = set(meta.keys())
    missing = EXPECTED_META_KEYS - actual
    extra = actual - EXPECTED_META_KEYS
    counts["meta_info_keys"] = len(actual)
    depth_map["/meta_info"] = sorted(actual)

    if missing:
        errors.append(_e("META_MISSING_KEYS", "meta_info missing: %s" % sorted(missing)))
    if extra:
        errors.append(_e("META_EXTRA_KEYS", "meta_info unexpected keys: %s" % sorted(extra)))

    # cluster
    cluster = meta.get("cluster", "")
    captured["cluster"] = cluster
    if not cluster:
        errors.append(_e("META_EMPTY", "cluster is empty"))
    elif cluster not in VALID_CLUSTERS:
        errors.append(_e("META_INVALID_CLUSTER",
                         "cluster '%s' not in %s" % (cluster, sorted(VALID_CLUSTERS))))

    # task_type
    task_type = meta.get("task_type", "")
    captured["task_type"] = task_type
    if not task_type:
        errors.append(_e("META_EMPTY", "task_type is empty"))
    elif task_type not in VALID_TASK_TYPES:
        errors.append(_e("META_INVALID_TASK_TYPE",
                         "task_type '%s' not in %s" % (task_type, sorted(VALID_TASK_TYPES))))

    # task_description
    desc = meta.get("task_description", "")
    captured["task_description"] = desc
    if not desc:
        errors.append(_e("META_EMPTY", "task_description is empty"))

    # task_completion_status
    status = meta.get("task_completion_status", "")
    captured["task_completion_status"] = status
    if status != "success":
        errors.append(_e("META_INVALID_STATUS",
                         "task_completion_status must be 'success', got '%s'" % status))

    # system_prompt
    sp = meta.get("system_prompt")
    captured["system_prompt"] = repr(str(sp or ""))[:120]
    if sp is None:
        pass  # caught by missing key check
    elif isinstance(sp, str) and not sp:
        warnings.append(_w("META_EMPTY_SYSPROMPT",
                           "system_prompt is empty string (expected assembled-from-files marker)"))

    # platform
    plat = meta.get("platform", "")
    captured["platform"] = plat
    if plat != "macOS":
        errors.append(_e("META_INVALID_PLATFORM", "platform must be 'macOS', got '%s'" % plat))

    # agents
    agents = meta.get("agents")
    if agents is None:
        pass  # caught by missing key check
    elif not isinstance(agents, dict):
        errors.append(_e("AGENTS_TYPE", "agents must be object, got %s" % type(agents).__name__))
    else:
        agents_keys = set(agents.keys())
        depth_map["/meta_info/agents"] = sorted(agents_keys)

        if "root" not in agents_keys:
            errors.append(_e("AGENTS_MISSING_ROOT", "agents missing 'root' key"))
        else:
            root = agents["root"]
            captured["agents_root"] = root
            if not isinstance(root, str) or not root:
                errors.append(_e("AGENTS_ROOT_EMPTY", "agents.root must be non-empty string"))

        if "spawned" not in agents_keys:
            errors.append(_e("AGENTS_MISSING_SPAWNED", "agents missing 'spawned' key"))
        else:
            spawned = agents["spawned"]
            captured["agents_spawned"] = spawned
            if not isinstance(spawned, list):
                errors.append(_e("AGENTS_SPAWNED_TYPE", "agents.spawned must be array"))
            else:
                counts["agents_spawned_count"] = len(spawned)
                for i, s in enumerate(spawned):
                    if not isinstance(s, str) or not s:
                        errors.append(_e("AGENTS_SPAWNED_ITEM",
                                         "agents.spawned[%d] must be non-empty string" % i))

        extra_agents = agents_keys - AGENTS_KEYS
        if extra_agents:
            errors.append(_e("AGENTS_EXTRA_KEYS",
                             "agents unexpected keys: %s" % sorted(extra_agents)))


# ── messages validation ────────────────────────────────────────────

def _validate_messages(messages, errors, warnings, captured, counts, depth_map, task_data):
    all_ids = []
    all_roles = []
    tool_call_ids_seen = set()
    tool_result_ids_seen = set()
    tool_names = Counter()
    content_type_counts = Counter()
    spawn_names = []
    spawn_session_ids = []
    yield_messages_list = []

    # structural pattern collectors (unique key sets per depth)
    wrapper_key_patterns = set()
    inner_key_patterns = {}          # role → frozenset of key sets
    block_key_patterns = {}          # block type → frozenset of key sets

    prev_ts = None
    ts_issues = 0

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            errors.append(_e("MSG_TYPE", "messages[%d] is not an object" % idx))
            continue

        # ── wrapper keys ───────────────────────────────────────────
        present = set(msg.keys())
        wrapper_key_patterns.add(frozenset(present))
        missing_w = WRAPPER_KEYS - present
        extra_w = present - WRAPPER_KEYS
        if missing_w:
            errors.append(_e("WRAPPER_MISSING",
                             "messages[%d] missing wrapper keys: %s" % (idx, sorted(missing_w))))
        if extra_w:
            errors.append(_e("WRAPPER_EXTRA",
                             "messages[%d] unexpected wrapper keys: %s" % (idx, sorted(extra_w))))

        if msg.get("type") != "message":
            errors.append(_e("WRAPPER_TYPE",
                             "messages[%d].type = '%s', expected 'message'" % (idx, msg.get("type"))))

        # ── id ─────────────────────────────────────────────────────
        msg_id = msg.get("id", "")
        if msg_id:
            all_ids.append(msg_id)
            if not HEX8.match(str(msg_id)):
                errors.append(_e("ID_FORMAT", "messages[%d].id '%s' not 8-hex" % (idx, msg_id)))
        else:
            errors.append(_e("ID_MISSING", "messages[%d].id is missing or empty" % idx))

        # ── parentId (first message must be null) ──────────────────
        parent_id = msg.get("parentId")
        if idx == 0:
            if parent_id is not None:
                errors.append(_e("PARENT_FIRST",
                                 "messages[0].parentId = %r, expected null" % parent_id))
        else:
            expected_parent = messages[idx - 1].get("id", "") if isinstance(messages[idx - 1], dict) else ""
            if parent_id != expected_parent:
                errors.append(_e("PARENT_CHAIN",
                                 "messages[%d].parentId = '%s', expected '%s'" % (idx, parent_id, expected_parent)))

        # ── timestamp ──────────────────────────────────────────────
        ts_str = msg.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if prev_ts and ts < prev_ts:
                    ts_issues += 1
                prev_ts = ts
            except (ValueError, TypeError):
                errors.append(_e("TIMESTAMP_FORMAT",
                                 "messages[%d].timestamp '%s' invalid ISO 8601" % (idx, str(ts_str)[:40])))
        else:
            errors.append(_e("TIMESTAMP_MISSING", "messages[%d].timestamp is missing" % idx))

        # ── inner message ──────────────────────────────────────────
        inner = msg.get("message")
        if not isinstance(inner, dict):
            errors.append(_e("INNER_TYPE", "messages[%d].message is not an object" % idx))
            continue

        role = inner.get("role", "")
        all_roles.append(role)
        counts["role_%s" % role] += 1
        inner_key_patterns.setdefault(role, set()).add(frozenset(inner.keys()))

        if role == "user":
            _check_keys(inner, USER_INNER_KEYS,
                        "messages[%d].message (user)" % idx, errors, strict=False)
        elif role == "assistant":
            _check_keys_optional(inner, ASSISTANT_REQUIRED_KEYS, ASSISTANT_OPTIONAL_KEYS,
                                 "messages[%d].message (assistant)" % idx, errors)
        elif role == "toolResult":
            _check_keys(inner, TOOL_RESULT_INNER_KEYS,
                        "messages[%d].message (toolResult)" % idx, errors, strict=False)
            tc_id = inner.get("toolCallId", "")
            if tc_id:
                tool_result_ids_seen.add(tc_id)
            if "isError" not in inner:
                errors.append(_e("ISERROR_MISSING",
                                 "messages[%d] toolResult missing 'isError'" % idx))
        else:
            errors.append(_e("UNKNOWN_ROLE",
                             "messages[%d] role '%s' not in (user, assistant, toolResult)" % (idx, role)))

        # ── content blocks ─────────────────────────────────────────
        content = inner.get("content")
        if not isinstance(content, list):
            if content is not None:
                errors.append(_e("CONTENT_TYPE", "messages[%d] content is not array" % idx))
            continue

        for bi, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            content_type_counts[btype] += 1
            block_key_patterns.setdefault(btype, set()).add(frozenset(block.keys()))
            path = "messages[%d].content[%d]" % (idx, bi)

            if btype == "text":
                _check_block_keys(block, TEXT_BLOCK_KEYS, "%s (text)" % path, errors)
            elif btype == "thinking":
                _check_block_keys(block, THINKING_BLOCK_KEYS, "%s (thinking)" % path, errors)
            elif btype == "toolCall":
                _check_block_keys_optional(block, TOOL_CALL_REQUIRED_KEYS, TOOL_CALL_OPTIONAL_KEYS,
                                           "%s (toolCall)" % path, errors)
                tc_id = block.get("id", "")
                if tc_id:
                    tool_call_ids_seen.add(tc_id)
                    if not TOOLUSE_PREFIX.match(tc_id):
                        errors.append(_e("TOOLCALL_ID_PREFIX",
                                         "%s toolCall.id '%s' should start with 'tooluse_'" % (path, tc_id[:30])))
                name = block.get("name", "")
                if name:
                    tool_names[name] += 1
                    # spawn argument checks (new schema: name+prompt)
                    if name == "sessions_spawn":
                        args = block.get("arguments", {})
                        if isinstance(args, dict):
                            missing_spawn = SPAWN_REQUIRED_ARGS - set(args.keys())
                            if missing_spawn:
                                warnings.append(_w("SPAWN_ARGS",
                                                   "messages[%d] sessions_spawn missing args: %s" % (idx, sorted(missing_spawn))))
                            sn = args.get("name", "")
                            if sn:
                                spawn_names.append(sn)
            elif btype:
                errors.append(_e("UNKNOWN_BLOCK_TYPE",
                                 "%s type '%s' not in (text, thinking, toolCall)" % (path, btype)))

        # ── extract spawn session IDs from toolResult ──────────────
        if role == "toolResult" and inner.get("toolName") == "sessions_spawn":
            for block in (content or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        rd = json.loads(block.get("text", ""))
                        sid = rd.get("session_id", "")
                        if sid:
                            spawn_session_ids.append(sid)
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass

        if role == "toolResult" and inner.get("toolName") == "sessions_yield":
            for block in (content or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        rd = json.loads(block.get("text", ""))
                        ym = (rd.get("output", "") or rd.get("message", ""))[:80]
                        if ym:
                            yield_messages_list.append(ym)
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass

    # ── cross-message checks ───────────────────────────────────────
    if ts_issues:
        errors.append(_e("TIMESTAMP_ORDER",
                         "%d timestamp(s) go backwards (non-monotonic)" % ts_issues))

    id_counts = Counter(all_ids)
    dupes = {k: v for k, v in id_counts.items() if v > 1}
    if dupes:
        errors.append(_e("DUPLICATE_IDS", "Duplicate message IDs: %s" % dict(dupes)))

    unmatched_calls = tool_call_ids_seen - tool_result_ids_seen
    if unmatched_calls:
        errors.append(_e("UNMATCHED_TOOLCALL",
                         "%d toolCall(s) without matching toolResult: %s" % (
                             len(unmatched_calls), sorted(unmatched_calls)[:5])))

    role_counter = Counter(all_roles)
    if role_counter.get("user", 0) != 1:
        errors.append(_e("USER_COUNT",
                         "Expected exactly 1 user message, found %d" % role_counter.get("user", 0)))
    if role_counter.get("assistant", 0) < 1:
        errors.append(_e("NO_ASSISTANT", "No assistant messages found"))

    if all_roles and all_roles[0] != "user":
        errors.append(_e("FIRST_ROLE",
                         "First message role is '%s', expected 'user'" % all_roles[0]))
    if all_roles and all_roles[-1] != "assistant":
        errors.append(_e("LAST_ROLE",
                         "Last message role is '%s', expected 'assistant'" % all_roles[-1]))

    if "sessions_spawn" not in tool_names:
        errors.append(_e("NO_SPAWN", "No sessions_spawn toolCall found"))
    if "sessions_yield" not in tool_names:
        errors.append(_e("NO_YIELD", "No sessions_yield toolCall found"))

    # ── captured ───────────────────────────────────────────────────
    captured["message_ids"] = all_ids
    captured["roles_sequence"] = all_roles
    captured["tool_names"] = dict(tool_names)
    captured["spawn_names"] = spawn_names
    captured["spawn_session_ids"] = spawn_session_ids
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

    # ── depth map (structural inventory) ───────────────────────────
    for fp in wrapper_key_patterns:
        depth_map.setdefault("/messages[*]", []).append(sorted(fp))
    for role, fps in inner_key_patterns.items():
        key = "/messages[*]/message (%s)" % role
        for fp in fps:
            depth_map.setdefault(key, []).append(sorted(fp))
    for btype, fps in block_key_patterns.items():
        key = "/messages[*]/message/content[*] (%s)" % btype
        for fp in fps:
            depth_map.setdefault(key, []).append(sorted(fp))


# ── helpers ────────────────────────────────────────────────────────

def _check_keys(obj, required, path, errors, strict=True):
    present = set(obj.keys())
    missing = required - present
    if missing:
        errors.append(_e("MISSING_FIELD", "%s missing required: %s" % (path, sorted(missing))))
    if strict:
        extra = present - required
        if extra:
            errors.append(_e("EXTRA_FIELD", "%s unexpected: %s" % (path, sorted(extra))))


def _check_keys_optional(obj, required, optional, path, errors):
    present = set(obj.keys())
    missing = required - present
    if missing:
        errors.append(_e("MISSING_FIELD", "%s missing required: %s" % (path, sorted(missing))))
    extra = present - required - optional
    if extra:
        errors.append(_e("EXTRA_FIELD", "%s unexpected: %s" % (path, sorted(extra))))


def _check_block_keys(block, required, path, errors):
    present = set(block.keys())
    missing = required - present
    if missing:
        errors.append(_e("BLOCK_MISSING", "%s missing: %s" % (path, sorted(missing))))


def _check_block_keys_optional(block, required, optional, path, errors):
    present = set(block.keys())
    missing = required - present
    if missing:
        errors.append(_e("BLOCK_MISSING", "%s missing: %s" % (path, sorted(missing))))
    extra = present - required - optional
    if extra:
        errors.append(_e("BLOCK_EXTRA", "%s unexpected: %s" % (path, sorted(extra))))


def _e(code, message):
    return {"code": code, "message": message, "severity": "error"}


def _w(code, message):
    return {"code": code, "message": message, "severity": "warning"}


def _result(valid, errors, warnings, captured, counts, depth_map):
    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "captured": captured,
        "counts": dict(counts),
        "depth_map": depth_map,
        "stats": {
            "message_count": counts.get("messages_total", 0),
            "tool_call_count": counts.get("tool_calls_total", 0),
            "unique_tools": sorted(set(
                k.replace("tool_", "", 1) for k in counts
                if k.startswith("tool_") and k != "tool_calls_total"
            )),
            "spawn_count": counts.get("spawns", 0),
            "yield_count": counts.get("yields", 0),
        },
    }


# ── formatter ──────────────────────────────────────────────────────

def format_result(result):
    lines = []

    verdict = "PASS" if result["valid"] else "FAIL"
    lines.append("STRUCTURAL QC: %s" % verdict)
    lines.append("")

    if result["errors"]:
        lines.append("ERRORS (%d):" % len(result["errors"]))
        for e in result["errors"]:
            lines.append("  [%s] %s" % (e["code"], e["message"]))
        lines.append("")

    if result.get("warnings"):
        lines.append("WARNINGS (%d):" % len(result["warnings"]))
        for w in result["warnings"]:
            lines.append("  [%s] %s" % (w["code"], w["message"]))
        lines.append("")

    counts = result.get("counts", {})
    if counts:
        lines.append("COUNTS:")
        lines.append("  Messages total:      %d" % counts.get("messages_total", 0))
        lines.append("  User messages:       %d" % counts.get("role_user", 0))
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
        lines.append("CAPTURED META:")
        for key in ("cluster", "task_type", "task_description",
                     "task_completion_status", "platform"):
            val = captured.get(key, "")
            if val:
                lines.append("  %-25s %s" % (key + ":", str(val)[:120]))
        if captured.get("system_prompt"):
            lines.append("  %-25s %s" % ("system_prompt:", captured["system_prompt"]))

        if captured.get("agents_root"):
            lines.append("  ---")
            lines.append("  agents.root: %s" % captured["agents_root"])
        if captured.get("agents_spawned"):
            lines.append("  agents.spawned (%d): %s" % (
                len(captured["agents_spawned"]),
                ", ".join(str(s)[:50] for s in captured["agents_spawned"]),
            ))

        if captured.get("spawn_names"):
            lines.append("  ---")
            lines.append("  Spawn names (%d):" % len(captured["spawn_names"]))
            for n in captured["spawn_names"]:
                lines.append("    - %s" % n)
        if captured.get("spawn_session_ids"):
            lines.append("  Spawn session IDs (%d):" % len(captured["spawn_session_ids"]))
            for sid in captured["spawn_session_ids"]:
                lines.append("    - %s" % sid[:60])
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
            seq = captured["roles_sequence"]
            if len(seq) > 20:
                lines.append("  Role sequence (%d): %s ... %s" % (
                    len(seq),
                    " > ".join(seq[:5]),
                    " > ".join(seq[-3:]),
                ))
            else:
                lines.append("  Role sequence: %s" % " > ".join(seq))
        lines.append("")

    # ── depth map (structural inventory) ───────────────────────────
    dm = result.get("depth_map", {})
    if dm:
        lines.append("DEPTH MAP (keys at each structural level):")
        for path in sorted(dm.keys()):
            patterns = dm[path]
            if isinstance(patterns, list) and patterns:
                if isinstance(patterns[0], list):
                    unique = sorted(set(tuple(p) for p in patterns))
                    for p in unique:
                        lines.append("  %-50s %s" % (path, list(p)))
                else:
                    lines.append("  %-50s %s" % (path, patterns))
            else:
                lines.append("  %-50s %s" % (path, patterns))
        lines.append("")

    return "\n".join(lines)
