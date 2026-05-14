# -*- coding: utf-8 -*-
"""
Multi-agent trajectory validator — flat interleaved schema.

Ported from multi_agent_trajectory_generation.py QC logic.
Validates the Talos multi-agent golden trajectory format using
sessions_spawn / sessions_yield with bare/wrapped message wrappers.

Returns structured results compatible with the Skoll QC pipeline.
"""
import json
import re
from datetime import datetime

# ---------------------------------------------------------------------------
# Tool Constants (per-role enforcement)
# ---------------------------------------------------------------------------

ORCHESTRATOR_TOOLS = {
    "sessions_spawn", "sessions_yield", "read", "write", "edit",
    "exec", "web_search", "web_fetch", "memory_search", "memory_get",
}

SUBAGENT_TOOLS = {
    "read", "write", "edit", "exec", "web_search", "web_fetch",
    "process", "memory_search", "memory_get", "cron", "message",
    "grep", "find", "ls", "browser", "canvas",
    "gmail", "outlook-mail", "apple-mail", "google-calendar",
    "outlook-calendar", "apple-calendar", "calendly", "google-contacts",
    "outlook-contacts", "apple-contacts", "whatsapp_cli", "telegram-cli",
    "google-drive", "imagine", "spaces", "user-context", "memory_update",
}

FORBIDDEN_IN_SUBAGENT = {"sessions_spawn", "sessions_yield"}

REQUIRED_ORCHESTRATOR_TOOLS = {"sessions_spawn", "sessions_yield"}

ALL_VALID_TOOLS = ORCHESTRATOR_TOOLS | SUBAGENT_TOOLS | {
    "browser", "canvas", "gateway", "agents_list", "sessions_list",
    "sessions_history", "sessions_send", "subagents", "session_status",
    "zeitgeist", "nodes",
}

VALID_TASK_TYPES = {
    "home_and_organization", "customer_service", "research_and_analysis",
    "creative_writing", "technical_support", "education_and_learning",
    "health_and_wellness", "finance_and_budgeting",
    "commerce_product", "creative_media", "visual_learning",
    "property_space", "operations_qa", "small_business_docs",
}

VALID_COMPLETION_STATUS = {"success", "partial_success", "incomplete", "failure"}

HEX8_RE = re.compile(r"^[0-9a-f]{8}$")

# Weighted scoring — double-weight phases
DOUBLE_WEIGHT_PHASES = {"Phase MA3", "Phase MA4", "Phase MA1"}
BLOCK_THRESHOLD = 6


# ---------------------------------------------------------------------------
# Message Navigation Helpers
# ---------------------------------------------------------------------------

def _get_inner_message(msg):
    """Navigate through wrapper to get the inner message object.

    Bare format: msg -> msg["message"]
    Wrapped format: msg -> msg["message"] -> msg["message"]["message"]
    """
    if "is_accepted" in msg:
        return msg.get("message", {}).get("message", {})
    return msg.get("message", {})


def _get_role(msg):
    return _get_inner_message(msg).get("role", "")


def _get_content(msg):
    return _get_inner_message(msg).get("content", [])


def _get_msg_id(msg):
    if "is_accepted" in msg:
        return msg.get("message", {}).get("id", "")
    return msg.get("id", "")


def _get_parent_id(msg):
    if "is_accepted" in msg:
        return msg.get("message", {}).get("parentId", "")
    return msg.get("parentId", "")


def _get_timestamp(msg):
    if "is_accepted" in msg:
        return msg.get("message", {}).get("timestamp", "")
    return msg.get("timestamp", "")


def _is_subagent_user_msg(msg):
    if _get_role(msg) != "user":
        return False
    content = _get_content(msg)
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if "[Subagent Context]" in text:
                return True
    return False


def _identify_agent_blocks(messages):
    """Identify which messages belong to which agent.

    Returns dict: {"subagent_0": [indices], "subagent_1": [indices], "orchestrator": [indices]}
    """
    agents = {}
    current_agent = None
    current_agent_idx = 0

    for i, msg in enumerate(messages):
        role = _get_role(msg)

        if role == "user" and _is_subagent_user_msg(msg):
            current_agent = "subagent_%d" % current_agent_idx
            current_agent_idx += 1
            agents.setdefault(current_agent, [])
            agents[current_agent].append(i)
        elif role == "user" and not _is_subagent_user_msg(msg):
            current_agent = "orchestrator"
            agents.setdefault(current_agent, [])
            agents[current_agent].append(i)
        else:
            if current_agent:
                agents.setdefault(current_agent, [])
                agents[current_agent].append(i)

    return agents


# ---------------------------------------------------------------------------
# QC Result
# ---------------------------------------------------------------------------

class QCResult:
    """QC result tracker with weighted scoring."""

    def __init__(self):
        self.checks = []
        self.blocks = 0
        self.warnings = 0
        self.advisories = 0
        self.weighted_warning_score = 0

    def add(self, phase, check, status, detail=""):
        self.checks.append({
            "phase": phase, "check": check,
            "status": status, "detail": detail,
        })
        if status == "BLOCK":
            self.blocks += 1
        elif status == "WARNING":
            self.warnings += 1
            weight = 2 if phase in DOUBLE_WEIGHT_PHASES else 1
            self.weighted_warning_score += weight
        elif status == "ADVISORY":
            self.advisories += 1

    @property
    def passed(self):
        return self.blocks == 0 and self.weighted_warning_score < BLOCK_THRESHOLD


# ---------------------------------------------------------------------------
# Main Validator
# ---------------------------------------------------------------------------

def validate_trajectory(json_str, task_data=None):
    """Validate a multi-agent golden trajectory JSON string.

    Args:
        json_str: raw JSON string of the trajectory
        task_data: optional dict with keys:
            - spawned_agents (list of {name, role}), etc.

    Returns:
        dict with valid (bool), errors, warnings, stats, qc_summary
    """
    errors = []
    warnings = []
    stats = {
        "message_count": 0,
        "subagent_count": 0,
        "orchestrator_msg_count": 0,
        "tool_call_count": 0,
        "unique_tools": set(),
        "spawn_count": 0,
        "yield_count": 0,
    }

    # --- parse ---
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        stripped = json_str.strip() if json_str else ""
        if stripped and not stripped.endswith("}"):
            errors.append(_err(
                "TRUNCATED",
                "JSON is truncated (output likely hit max_tokens limit). "
                "Parse error: %s" % str(e),
            ))
        else:
            errors.append(_err("PARSE_ERROR", "Invalid JSON: %s" % str(e)))
        stats["unique_tools"] = []
        return {"valid": False, "errors": errors, "warnings": warnings, "stats": stats}

    if not isinstance(data, dict):
        errors.append(_err("ROOT_TYPE", "Root must be object, got %s" % type(data).__name__))
        stats["unique_tools"] = []
        return {"valid": False, "errors": errors, "warnings": warnings, "stats": stats}

    # Run full multi-agent QC
    expected_agents = []
    if task_data and task_data.get("spawned_agents"):
        sa = task_data["spawned_agents"]
        if isinstance(sa, str):
            try:
                sa = json.loads(sa)
            except (json.JSONDecodeError, TypeError):
                sa = []
        if isinstance(sa, list):
            expected_agents = sa

    qc = run_multi_agent_qc(data, expected_agents)

    # Convert QC checks to errors/warnings
    for c in qc.checks:
        if c["status"] == "BLOCK":
            errors.append(_err(c["check"], c["detail"] or c["check"], c["phase"]))
        elif c["status"] == "WARNING":
            warnings.append(_warn(c["check"], c["detail"] or c["check"], c["phase"]))

    # Build stats
    messages = data.get("messages", [])
    stats["message_count"] = len(messages)

    agent_blocks = _identify_agent_blocks(messages)
    subagent_keys = sorted(k for k in agent_blocks if k.startswith("subagent_"))
    stats["subagent_count"] = len(subagent_keys)
    stats["orchestrator_msg_count"] = len(agent_blocks.get("orchestrator", []))

    for msg in messages:
        content = _get_content(msg)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    stats["tool_call_count"] += 1
                    name = block.get("name", "")
                    if name:
                        stats["unique_tools"].add(name)
                    if name == "sessions_spawn":
                        stats["spawn_count"] += 1
                    elif name == "sessions_yield":
                        stats["yield_count"] += 1

    stats["unique_tools"] = sorted(stats["unique_tools"])

    return {
        "valid": qc.passed,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
        "qc_summary": _format_qc_summary(qc),
    }


def run_multi_agent_qc(golden, expected_agents=None):
    """Run automated QC checks specific to multi-agent trajectories.

    Phases:
      1   - Structural schema
      2   - Conversation integrity basics
      MA1 - Spawn/yield integrity
      MA2 - Sub-agent completeness
      MA3 - ParentId chain separation
      MA4 - Wrapper schema (bare vs wrapped)
      MA5 - Orchestrator compilation
      MA6 - Single-prompt rule
      MA7 - Flow order (sub-agents before orchestrator)
      MA8 - Tool enforcement per role
    """
    if expected_agents is None:
        expected_agents = []

    qc = QCResult()
    messages = golden.get("messages", [])
    meta = golden.get("meta_info", {})

    # ── Phase 1: Structural Schema ──

    if set(golden.keys()) >= {"meta_info", "messages"}:
        qc.add("Phase 1", "1.1.2 Top-level keys", "PASS")
    else:
        qc.add("Phase 1", "1.1.2 Top-level keys", "BLOCK",
                "Missing keys. Found: %s" % list(golden.keys()))

    # meta_info checks
    task_type = meta.get("task_type", "")
    if task_type in VALID_TASK_TYPES:
        qc.add("Phase 1", "1.2.1 task_type valid", "PASS")
    else:
        qc.add("Phase 1", "1.2.1 task_type valid", "BLOCK",
                "Invalid: '%s'" % task_type)

    desc = meta.get("task_description", "")
    if len(desc) >= 20:
        qc.add("Phase 1", "1.2.2 task_description", "PASS")
    else:
        qc.add("Phase 1", "1.2.2 task_description", "BLOCK",
                "Too short (%d chars)" % len(desc))

    status_val = meta.get("task_completion_status", "")
    if status_val in VALID_COMPLETION_STATUS:
        qc.add("Phase 1", "1.2.3 completion_status", "PASS")
    else:
        qc.add("Phase 1", "1.2.3 completion_status", "BLOCK",
                "Invalid: '%s'" % status_val)

    if meta.get("platform") == "macOS":
        qc.add("Phase 1", "1.2.5 platform", "PASS")
    else:
        qc.add("Phase 1", "1.2.5 platform", "WARNING",
                "Expected 'macOS', got '%s'" % meta.get("platform"))

    if not messages:
        qc.add("Phase 1", "1.3.1 messages non-empty", "BLOCK",
                "Empty messages array")
        return qc

    qc.add("Phase 1", "1.3.1 messages non-empty", "PASS")

    # ── Phase 2: Basic Conversation Integrity ──

    # Timestamp monotonicity across entire flat array
    timestamps_valid = True
    timestamps_monotonic = True
    prev_ts = None
    for msg in messages:
        ts_str = _get_timestamp(msg)
        if not ts_str:
            continue
        try:
            if isinstance(ts_str, str):
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if prev_ts and ts < prev_ts:
                    timestamps_monotonic = False
                prev_ts = ts
        except (ValueError, TypeError):
            timestamps_valid = False

    if timestamps_valid:
        qc.add("Phase 2", "2.3.1 Valid ISO 8601", "PASS")
    else:
        qc.add("Phase 2", "2.3.1 Valid ISO 8601", "BLOCK",
                "Invalid timestamp format")

    if timestamps_monotonic:
        qc.add("Phase 2", "2.3.2 Timestamp monotonicity", "PASS")
    else:
        qc.add("Phase 2", "2.3.2 Timestamp monotonicity", "WARNING",
                "Non-monotonic timestamps in flat array")

    # ToolCall <-> ToolResult pairing
    tool_call_ids = set()
    tool_result_ids = set()
    for msg in messages:
        role = _get_role(msg)
        content = _get_content(msg)
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    tool_call_ids.add(block.get("id", ""))
        elif role == "toolResult":
            inner = _get_inner_message(msg)
            tool_result_ids.add(inner.get("toolCallId", ""))

    unmatched = tool_call_ids - tool_result_ids
    if not unmatched:
        qc.add("Phase 2", "2.4.1 ToolCall/Result pairing", "PASS")
    else:
        qc.add("Phase 2", "2.4.1 ToolCall/Result pairing", "BLOCK",
                "%d toolCalls without matching toolResult" % len(unmatched))

    # ── Phase MA1: Spawn/Yield Integrity ──

    spawn_calls = []
    yield_calls = []
    spawn_results = []
    yield_results = []

    for msg in messages:
        role = _get_role(msg)
        content = _get_content(msg)

        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    if block.get("name") == "sessions_spawn":
                        spawn_calls.append(block)
                    elif block.get("name") == "sessions_yield":
                        yield_calls.append(block)

        if role == "toolResult":
            inner = _get_inner_message(msg)
            tool_name = inner.get("toolName", "")
            if tool_name == "sessions_spawn":
                spawn_results.append(msg)
            elif tool_name == "sessions_yield":
                yield_results.append(msg)

    # MA1.1: Spawn count matches expected agents
    if expected_agents:
        if len(spawn_calls) == len(expected_agents):
            qc.add("Phase MA1", "MA1.1 Spawn count matches metadata",
                    "PASS", "%d spawns for %d agents" % (len(spawn_calls), len(expected_agents)))
        else:
            qc.add("Phase MA1", "MA1.1 Spawn count matches metadata",
                    "BLOCK",
                    "Expected %d spawns, found %d" % (len(expected_agents), len(spawn_calls)))
    else:
        # No expected agents — just check that there are spawns
        if spawn_calls:
            qc.add("Phase MA1", "MA1.1 Has spawns", "PASS",
                    "%d spawn(s)" % len(spawn_calls))
        else:
            qc.add("Phase MA1", "MA1.1 Has spawns", "BLOCK",
                    "No sessions_spawn found")

    # MA1.2: Every spawn has a matching result
    if len(spawn_calls) == len(spawn_results):
        qc.add("Phase MA1", "MA1.2 Spawn/result pairing", "PASS")
    else:
        qc.add("Phase MA1", "MA1.2 Spawn/result pairing", "BLOCK",
                "%d calls vs %d results" % (len(spawn_calls), len(spawn_results)))

    # MA1.3: At least one sessions_yield
    if yield_calls:
        qc.add("Phase MA1", "MA1.3 Has sessions_yield", "PASS",
                "%d yield(s)" % len(yield_calls))
    else:
        qc.add("Phase MA1", "MA1.3 Has sessions_yield", "BLOCK",
                "No sessions_yield found")

    # MA1.4: Spawn args have required fields
    spawn_arg_issues = []
    for i, sc in enumerate(spawn_calls):
        args = sc.get("arguments", {})
        if not args.get("task"):
            spawn_arg_issues.append("spawn[%d]: missing 'task'" % i)
        if not args.get("taskName"):
            spawn_arg_issues.append("spawn[%d]: missing 'taskName'" % i)
        if args.get("runtime") != "subagent":
            spawn_arg_issues.append("spawn[%d]: runtime != 'subagent'" % i)
        if not sc.get("partialArgs"):
            spawn_arg_issues.append("spawn[%d]: missing partialArgs" % i)

    if not spawn_arg_issues:
        qc.add("Phase MA1", "MA1.4 Spawn args complete", "PASS")
    else:
        qc.add("Phase MA1", "MA1.4 Spawn args complete", "WARNING",
                "; ".join(spawn_arg_issues[:5]))

    # ── Phase MA2: Sub-agent Completeness ──

    agent_blocks = _identify_agent_blocks(messages)
    subagent_keys = sorted(k for k in agent_blocks if k.startswith("subagent_"))

    # MA2.1: Number of sub-agent blocks
    if expected_agents:
        if len(subagent_keys) == len(expected_agents):
            qc.add("Phase MA2", "MA2.1 Sub-agent block count", "PASS",
                    "%d blocks" % len(subagent_keys))
        else:
            qc.add("Phase MA2", "MA2.1 Sub-agent block count", "WARNING",
                    "Expected %d, found %d" % (len(expected_agents), len(subagent_keys)))
    else:
        if subagent_keys:
            qc.add("Phase MA2", "MA2.1 Sub-agent blocks present", "PASS",
                    "%d blocks" % len(subagent_keys))
        else:
            qc.add("Phase MA2", "MA2.1 Sub-agent blocks present", "WARNING",
                    "No sub-agent conversation blocks found")

    # MA2.2: Each sub-agent starts with [Subagent Context] user msg
    for sa_key in subagent_keys:
        indices = agent_blocks[sa_key]
        if indices:
            first_msg = messages[indices[0]]
            if _is_subagent_user_msg(first_msg):
                qc.add("Phase MA2", "MA2.2 %s starts with context" % sa_key, "PASS")
            else:
                qc.add("Phase MA2", "MA2.2 %s starts with context" % sa_key,
                        "WARNING", "Missing [Subagent Context] prefix")

    # MA2.3: Each sub-agent ends with stopReason: "stop"
    for sa_key in subagent_keys:
        indices = agent_blocks[sa_key]
        if indices:
            last_msg = messages[indices[-1]]
            inner = _get_inner_message(last_msg)
            if inner.get("stopReason") == "stop":
                qc.add("Phase MA2", "MA2.3 %s ends with stop" % sa_key, "PASS")
            else:
                qc.add("Phase MA2", "MA2.3 %s ends with stop" % sa_key,
                        "WARNING",
                        "stopReason: '%s'" % inner.get("stopReason"))

    # MA2.4: Each sub-agent has at least 3 tool calls
    for sa_key in subagent_keys:
        indices = agent_blocks[sa_key]
        tc_count = 0
        for idx in indices:
            content = _get_content(messages[idx])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        tc_count += 1
        if tc_count >= 3:
            qc.add("Phase MA2", "MA2.4 %s tool call count" % sa_key,
                    "PASS", "%d calls" % tc_count)
        else:
            qc.add("Phase MA2", "MA2.4 %s tool call count" % sa_key,
                    "WARNING", "Only %d calls (expected >= 3)" % tc_count)

    # ── Phase MA3: ParentId Chain Separation ──

    for agent_key, indices in agent_blocks.items():
        if len(indices) < 2:
            continue
        ids_in_chain = set()
        chain_broken = False
        for idx in indices:
            msg = messages[idx]
            msg_id = _get_msg_id(msg)
            parent_id = _get_parent_id(msg)

            if idx == indices[0]:
                ids_in_chain.add(msg_id)
                continue

            if parent_id not in ids_in_chain:
                chain_broken = True
                break
            ids_in_chain.add(msg_id)

        if not chain_broken:
            qc.add("Phase MA3", "MA3.1 %s parentId chain" % agent_key, "PASS")
        else:
            qc.add("Phase MA3", "MA3.1 %s parentId chain" % agent_key,
                    "BLOCK", "Chain broken or crosses agent boundary")

    # ── Phase MA4: Wrapper Schema ──

    wrapper_issues = 0
    for msg in messages:
        role = _get_role(msg)
        if role == "user":
            if "is_accepted" in msg:
                wrapper_issues += 1
        elif role in ("assistant", "toolResult"):
            if "is_accepted" not in msg:
                wrapper_issues += 1

    if wrapper_issues == 0:
        qc.add("Phase MA4", "MA4.1 Wrapper schema correct", "PASS")
    else:
        qc.add("Phase MA4", "MA4.1 Wrapper schema correct", "BLOCK",
                "%d messages with wrong wrapper format" % wrapper_issues)

    # MA4.2: Assistant messages have required fields
    missing_fields = 0
    for msg in messages:
        if _get_role(msg) != "assistant":
            continue
        inner = _get_inner_message(msg)
        if not inner.get("stopReason"):
            missing_fields += 1
        if not inner.get("responseId"):
            missing_fields += 1

    if missing_fields == 0:
        qc.add("Phase MA4", "MA4.2 Assistant required fields", "PASS")
    else:
        qc.add("Phase MA4", "MA4.2 Assistant required fields", "WARNING",
                "%d missing stopReason/responseId fields" % missing_fields)

    # MA4.3: toolCalls have partialArgs
    missing_partial = 0
    total_tool_calls = 0
    for msg in messages:
        content = _get_content(msg)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    total_tool_calls += 1
                    if not block.get("partialArgs"):
                        missing_partial += 1

    if missing_partial == 0:
        qc.add("Phase MA4", "MA4.3 partialArgs on toolCalls", "PASS",
                "All %d toolCalls have partialArgs" % total_tool_calls)
    else:
        qc.add("Phase MA4", "MA4.3 partialArgs on toolCalls", "WARNING",
                "%d/%d missing partialArgs" % (missing_partial, total_tool_calls))

    # ── Phase MA5: Orchestrator Compilation ──

    orch_indices = agent_blocks.get("orchestrator", [])

    if orch_indices:
        qc.add("Phase MA5", "MA5.1 Orchestrator present", "PASS")
    else:
        qc.add("Phase MA5", "MA5.1 Orchestrator present", "BLOCK",
                "No orchestrator conversation found")
        return qc

    # MA5.2: Orchestrator has a write call (compilation)
    has_write = False
    has_read = False
    for idx in orch_indices:
        content = _get_content(messages[idx])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    if block.get("name") == "write":
                        has_write = True
                    elif block.get("name") == "read":
                        has_read = True

    if has_read:
        qc.add("Phase MA5", "MA5.2 Orchestrator reads sub-agent outputs", "PASS")
    else:
        qc.add("Phase MA5", "MA5.2 Orchestrator reads sub-agent outputs",
                "WARNING", "No read calls found in orchestrator")

    if has_write:
        qc.add("Phase MA5", "MA5.3 Orchestrator compiles deliverable", "PASS")
    else:
        qc.add("Phase MA5", "MA5.3 Orchestrator compiles deliverable",
                "WARNING", "No write call for final compilation")

    # MA5.4: Orchestrator ends with stopReason: "stop"
    if orch_indices:
        last_orch = messages[orch_indices[-1]]
        inner = _get_inner_message(last_orch)
        if inner.get("stopReason") == "stop":
            qc.add("Phase MA5", "MA5.4 Orchestrator final stop", "PASS")
        else:
            qc.add("Phase MA5", "MA5.4 Orchestrator final stop", "WARNING",
                    "stopReason: '%s'" % inner.get("stopReason"))

    # ── Phase MA6: Single-Prompt Rule ──

    orchestrator_user_msgs = 0
    for idx in orch_indices:
        if _get_role(messages[idx]) == "user":
            orchestrator_user_msgs += 1

    if orchestrator_user_msgs == 1:
        qc.add("Phase MA6", "MA6.1 Single user prompt", "PASS")
    else:
        qc.add("Phase MA6", "MA6.1 Single user prompt", "BLOCK",
                "Expected 1 orchestrator user msg, found %d" % orchestrator_user_msgs)

    # ── Phase MA7: Flow Order ──

    if subagent_keys and orch_indices:
        last_subagent_idx = max(
            max(agent_blocks[k]) for k in subagent_keys
            if agent_blocks[k]
        )
        first_orch_idx = min(orch_indices)

        if last_subagent_idx < first_orch_idx:
            qc.add("Phase MA7", "MA7.1 Sub-agents before orchestrator", "PASS")
        else:
            qc.add("Phase MA7", "MA7.1 Sub-agents before orchestrator",
                    "WARNING",
                    "Some sub-agent messages appear after orchestrator start")

    # ── Phase MA8: Tool Enforcement ──

    subagent_forbidden_usage = []
    for sa_key in subagent_keys:
        for idx in agent_blocks[sa_key]:
            content = _get_content(messages[idx])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        tool_name = block.get("name", "")
                        if tool_name in FORBIDDEN_IN_SUBAGENT:
                            subagent_forbidden_usage.append(
                                "%s: %s" % (sa_key, tool_name)
                            )

    if not subagent_forbidden_usage:
        qc.add("Phase MA8", "MA8.1 No forbidden tools in sub-agents", "PASS")
    else:
        qc.add("Phase MA8", "MA8.1 No forbidden tools in sub-agents",
                "BLOCK",
                "Forbidden tools used: %s" % subagent_forbidden_usage)

    # MA8.2: Orchestrator uses required tools
    orch_tools_used = set()
    for idx in orch_indices:
        content = _get_content(messages[idx])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    orch_tools_used.add(block.get("name", ""))

    missing_required = REQUIRED_ORCHESTRATOR_TOOLS - orch_tools_used
    if not missing_required:
        qc.add("Phase MA8", "MA8.2 Orchestrator uses required tools", "PASS")
    else:
        qc.add("Phase MA8", "MA8.2 Orchestrator uses required tools",
                "BLOCK", "Missing: %s" % missing_required)

    # MA8.3: All tools are valid
    all_tools_used = set()
    for msg in messages:
        content = _get_content(msg)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    all_tools_used.add(block.get("name", ""))

    invalid_tools = all_tools_used - ALL_VALID_TOOLS
    if not invalid_tools:
        qc.add("Phase MA8", "MA8.3 All tools valid", "PASS")
    else:
        qc.add("Phase MA8", "MA8.3 All tools valid", "WARNING",
                "Invalid tools: %s" % invalid_tools)

    return qc


# ---------------------------------------------------------------------------
# Output Formatting
# ---------------------------------------------------------------------------

def _err(code, msg, path=""):
    return {"severity": "error", "code": code, "message": msg, "path": path}


def _warn(code, msg, path=""):
    return {"severity": "warning", "code": code, "message": msg, "path": path}


def _format_qc_summary(qc):
    """Format QCResult as human-readable summary."""
    verdict = "PASS" if qc.passed else "FAIL"
    lines = ["QC VERDICT: %s" % verdict]
    lines.append(
        "  BLOCKs: %d | WARNINGs: %d (weighted: %d/%d) | ADVISORYs: %d"
        % (qc.blocks, qc.warnings, qc.weighted_warning_score,
           BLOCK_THRESHOLD, qc.advisories)
    )
    if not qc.passed:
        lines.append("  BLOCKING issues:")
        for c in qc.checks:
            if c["status"] == "BLOCK":
                lines.append("    - [%s] %s: %s" % (c["phase"], c["check"], c["detail"]))
        if qc.weighted_warning_score >= BLOCK_THRESHOLD:
            lines.append(
                "  WARNING threshold breached (%d >= %d):"
                % (qc.weighted_warning_score, BLOCK_THRESHOLD)
            )
            for c in qc.checks:
                if c["status"] == "WARNING":
                    lines.append("    - [%s] %s: %s" % (c["phase"], c["check"], c["detail"]))
    return "\n".join(lines)


def format_result(result):
    """Format validation result as human-readable text."""
    lines = []
    lines.append("STRUCTURAL VALIDATION: %s" % ("PASS" if result["valid"] else "FAIL"))
    lines.append("")

    # Include QC summary if available
    if result.get("qc_summary"):
        lines.append(result["qc_summary"])
        lines.append("")

    if result["errors"]:
        lines.append("ERRORS (%d):" % len(result["errors"]))
        for e in result["errors"]:
            loc = " @ %s" % e["path"] if e.get("path") else ""
            lines.append("  [%s] %s%s" % (e["code"], e["message"], loc))
        lines.append("")

    if result["warnings"]:
        lines.append("WARNINGS (%d):" % len(result["warnings"]))
        for w in result["warnings"]:
            loc = " @ %s" % w["path"] if w.get("path") else ""
            lines.append("  [%s] %s%s" % (w["code"], w["message"], loc))
        lines.append("")

    s = result.get("stats", {})
    lines.append("STATISTICS:")
    lines.append("  Messages: %d" % s.get("message_count", 0))
    lines.append("  Sub-agents: %d" % s.get("subagent_count", 0))
    lines.append("  Orchestrator messages: %d" % s.get("orchestrator_msg_count", 0))
    lines.append("  Tool calls: %d" % s.get("tool_call_count", 0))
    lines.append("  Spawns: %d" % s.get("spawn_count", 0))
    lines.append("  Yields: %d" % s.get("yield_count", 0))
    lines.append("  Unique tools: %s" % ", ".join(s.get("unique_tools", [])))

    return "\n".join(lines)
