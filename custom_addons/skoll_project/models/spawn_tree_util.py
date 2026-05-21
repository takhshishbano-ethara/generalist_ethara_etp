import json
from collections import defaultdict


def _get_inner(msg):
    if isinstance(msg, dict):
        return msg.get("message", {})
    return {}


def _get_role(msg):
    inner = _get_inner(msg)
    return inner.get("role", "") if isinstance(inner, dict) else ""


def _get_content(msg):
    inner = _get_inner(msg)
    return inner.get("content", "") if isinstance(inner, dict) else ""


def _extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _find_tool_result(messages, start_idx, tool_call_id):
    for j in range(start_idx + 1, min(start_idx + 30, len(messages))):
        inner = _get_inner(messages[j])
        role = inner.get("role", "") if isinstance(inner, dict) else ""
        if role != "toolResult":
            continue
        msg_call_id = inner.get("toolCallId", "") if isinstance(inner, dict) else ""
        if msg_call_id == tool_call_id:
            return _extract_text(_get_content(messages[j]))
    return ""


def _wrap_text(text, width, indent=""):
    """Word-wrap text to fit within width, preserving words."""
    if not text:
        return []
    words = text.split()
    lines = []
    current = indent
    for word in words:
        if current == indent:
            current += word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = indent + word
    if current.strip():
        lines.append(current)
    return lines


def parse_trajectory(traj):
    messages = traj.get("messages") or traj.get("conversation", [])
    if not messages:
        return {
            "orchestrator": {"msg_count": 0, "spawn_count": 0, "yield_count": 0, "tools": {}},
            "children": [],
            "meta_info": traj.get("meta_info", {}),
        }

    spawns = []
    yields = {}
    orch_tools = defaultdict(int)
    total_msgs = len(messages)

    for i, msg in enumerate(messages):
        role = _get_role(msg)
        content = _get_content(msg)

        if role != "assistant" or not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue

            tool_name = block.get("name", "")
            tool_call_id = block.get("id", "")
            args = block.get("arguments", {})

            if tool_name == "sessions_spawn":
                result_text = _find_tool_result(messages, i, tool_call_id)
                child_session_key = ""
                try:
                    rdata = json.loads(result_text)
                    child_session_key = rdata.get("childSessionKey", "")
                    if not child_session_key:
                        details = rdata.get("details", {})
                        if isinstance(details, dict):
                            child_session_key = details.get("childSessionKey", "")
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

                task_desc = args.get("task", "")
                task_name = args.get("taskName", "")
                if not task_name and task_desc:
                    words = task_desc.split()[:4]
                    task_name = "_".join(
                        w.lower().strip(".,;:!?'\"") for w in words if w.strip()
                    )

                spawns.append({
                    "task_name": task_name or "agent_%d" % len(spawns),
                    "task_desc": task_desc,
                    "session_key": child_session_key,
                    "model": args.get("model", ""),
                    "msg_idx": i,
                })

            elif tool_name == "sessions_yield":
                result_text = _find_tool_result(messages, i, tool_call_id)
                yield_msg = args.get("message", "")
                status = ""
                output = ""
                session_key = ""
                try:
                    rdata = json.loads(result_text)
                    status = rdata.get("status", "")
                    output = rdata.get("message", rdata.get("output", ""))
                    session_key = rdata.get("childSessionKey", "")
                    if not session_key:
                        details = rdata.get("details", {})
                        if isinstance(details, dict):
                            session_key = details.get("childSessionKey", "")
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

                if session_key:
                    yields[session_key] = {"status": status, "output": output}

            else:
                orch_tools[tool_name] += 1

    children = []
    for spawn in spawns:
        key = spawn["session_key"]
        yield_info = yields.get(key, {})
        children.append({
            "task_name": spawn["task_name"],
            "task_desc": spawn["task_desc"],
            "session_key": key,
            "model": spawn["model"],
            "output": yield_info.get("output", ""),
        })

    return {
        "orchestrator": {
            "msg_count": total_msgs,
            "spawn_count": len(spawns),
            "yield_count": len(yields),
            "tools": dict(orch_tools),
        },
        "children": children,
        "meta_info": traj.get("meta_info", {}),
    }


BOX_H = "\u2500"
BOX_V = "\u2502"
BOX_TL = "\u250c"
BOX_TR = "\u2510"
BOX_BL = "\u2514"
BOX_BR = "\u2518"
TREE_T = "\u251c"
TREE_L = "\u2514"
TREE_V = "\u2502"
ARROW = "\u25b6"

BOX_WIDTH = 70


def _box(lines, width=None):
    if width is None:
        width = max(len(l) for l in lines) + 2
    out = [BOX_TL + BOX_H * width + BOX_TR]
    for line in lines:
        out.append(BOX_V + " " + line.ljust(width - 2) + " " + BOX_V)
    out.append(BOX_BL + BOX_H * width + BOX_BR)
    return out


def _tool_summary(tools):
    if not tools:
        return "no other tools"
    return ", ".join(
        "%s\u00d7%d" % (k, v) for k, v in sorted(tools.items(), key=lambda x: -x[1])
    )


def render_ascii(tree):
    meta = tree.get("meta_info", {})
    orch = tree["orchestrator"]
    children = tree["children"]

    output = []
    output.append("")

    title = "SPAWN TREE"
    if meta.get("task_type"):
        title += "  (%s)" % meta["task_type"]
    output.append("  " + title)
    output.append("  " + "=" * len(title))
    output.append("")

    if meta.get("task_description"):
        output.append("  %s" % meta["task_description"])
        output.append("")

    orch_lines = [
        "%s Orchestrator" % ARROW,
        "  messages: %d  |  spawned: %d  |  yielded: %d"
        % (orch["msg_count"], orch["spawn_count"], orch["yield_count"]),
    ]
    if orch["tools"]:
        orch_lines.append("  tools: %s" % _tool_summary(orch["tools"]))
    orch_box = _box(orch_lines, width=BOX_WIDTH)
    for line in orch_box:
        output.append("  " + line)

    if not children:
        output.append("  (no sub-agents spawned)")
        output.append("")
        return "\n".join(output)

    output.append("  " + BOX_V)

    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        connector = TREE_L if is_last else TREE_T

        child_lines = [
            "%s %s" % (ARROW, child["task_name"]),
        ]
        if child["session_key"]:
            child_lines.append("  session: %s" % child["session_key"])
        if child["model"]:
            child_lines.append("  model: %s" % child["model"])
        if child["task_desc"]:
            child_lines.append("")
            child_lines.append("  task:")
            inner_width = BOX_WIDTH - 6
            for wl in _wrap_text(child["task_desc"], inner_width, indent="    "):
                child_lines.append(wl)
        if child["output"]:
            child_lines.append("")
            child_lines.append("  output:")
            inner_width = BOX_WIDTH - 6
            for wl in _wrap_text(child["output"], inner_width, indent="    "):
                child_lines.append(wl)

        child_box = _box(child_lines, width=BOX_WIDTH - 4)

        for j, line in enumerate(child_box):
            if j == 0:
                prefix = "  %s%s%s " % (connector, BOX_H, BOX_H)
            else:
                prefix = "  %s    " % (TREE_V if not is_last else " ")
            output.append(prefix + line)

        if not is_last:
            output.append("  " + TREE_V)

    output.append("")
    return "\n".join(output)


def _strip_code_fences(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
    return cleaned


def build_spawn_tree(trajectory_json_str):
    if not trajectory_json_str or not trajectory_json_str.strip():
        return ""
    try:
        traj = json.loads(_strip_code_fences(trajectory_json_str))
    except (json.JSONDecodeError, TypeError):
        return "(Unable to parse trajectory JSON for spawn tree)"
    if not isinstance(traj, dict):
        return "(Trajectory is not a JSON object)"
    tree = parse_trajectory(traj)
    if not tree["children"] and tree["orchestrator"]["msg_count"] == 0:
        return "(No messages found in trajectory)"
    return render_ascii(tree)
