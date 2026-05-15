#!/usr/bin/env python3
import argparse
import json
import sys
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
        print("No messages found in trajectory.", file=sys.stderr)
        sys.exit(1)

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
                    task_name = "_".join(w.lower().strip(".,;:!?'\"") for w in words if w.strip())

                spawns.append({
                    "task_name": task_name or "agent_%d" % len(spawns),
                    "task_desc": task_desc,
                    "session_key": child_session_key,
                    "model": args.get("model", ""),
                    "msg_idx": i,
                })

            elif tool_name == "sessions_yield":
                result_text = _find_tool_result(messages, i, tool_call_id)
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


BOX_H = "─"
BOX_V = "│"
BOX_TL = "┌"
BOX_TR = "┐"
BOX_BL = "└"
BOX_BR = "┘"
TREE_T = "├"
TREE_L = "└"
TREE_V = "│"
ARROW = "▶"

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
    return ", ".join("%s×%d" % (k, v) for k, v in sorted(tools.items(), key=lambda x: -x[1]))


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
        "  messages: %d  |  spawned: %d  |  yielded: %d" % (
            orch["msg_count"], orch["spawn_count"], orch["yield_count"]),
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


def render_graphviz(tree, output_path, fmt="png"):
    try:
        import graphviz
    except ImportError:
        print("graphviz package not installed. Install with: pip install graphviz", file=sys.stderr)
        print("Falling back to ASCII output.\n", file=sys.stderr)
        print(render_ascii(tree))
        sys.exit(1)

    meta = tree.get("meta_info", {})
    orch = tree["orchestrator"]
    children = tree["children"]

    dot = graphviz.Digraph(
        "spawn_tree",
        format=fmt,
        graph_attr={
            "rankdir": "TB",
            "bgcolor": "#1e1e2e",
            "fontcolor": "#cdd6f4",
            "fontname": "SF Mono, Consolas, monospace",
            "pad": "0.5",
            "nodesep": "0.6",
            "ranksep": "0.8",
            "label": "Spawn Tree — %s" % meta.get("task_type", "trajectory"),
            "labelloc": "t",
            "fontsize": "18",
        },
        node_attr={
            "shape": "record",
            "style": "filled,rounded",
            "fontname": "SF Mono, Consolas, monospace",
            "fontsize": "11",
            "fontcolor": "#cdd6f4",
            "color": "#313244",
            "penwidth": "1.5",
        },
        edge_attr={
            "color": "#89b4fa",
            "penwidth": "2",
            "arrowsize": "0.8",
        },
    )

    orch_label = "Orchestrator | msgs: %d | spawned: %d" % (orch["msg_count"], orch["spawn_count"])
    if orch["tools"]:
        orch_label += "\\n%s" % _tool_summary(orch["tools"])
    dot.node("orchestrator", orch_label, fillcolor="#181825", color="#89b4fa")

    for i, child in enumerate(children):
        node_id = "sub_%d" % i
        label = child["task_name"]
        if child["model"]:
            label += "\\nmodel: %s" % child["model"]
        if child["task_desc"]:
            desc = child["task_desc"]
            if len(desc) > 80:
                desc = desc[:77] + "..."
            label += "\\ntask: %s" % desc.replace('"', '\\"')

        dot.node(node_id, label, fillcolor="#181825", color="#89b4fa")

        edge_label = ""
        if child["session_key"]:
            sk = child["session_key"]
            if len(sk) > 20:
                sk = sk[:20]
            edge_label = sk
        dot.edge("orchestrator", node_id, label=edge_label, fontcolor="#6c7086", fontsize="9")

    if output_path:
        dot.render(output_path, cleanup=True)
        print("Saved to %s.%s" % (output_path, fmt))
    else:
        dot.render("/tmp/skoll_spawn_tree", view=True, cleanup=True)
        print("Opened in default viewer.")


def main():
    parser = argparse.ArgumentParser(description="Visualize Skoll trajectory spawn tree.")
    parser.add_argument("trajectory", help="Path to trajectory JSON file")
    parser.add_argument("--format", "-f", choices=["ascii", "png", "svg", "pdf"], default="ascii",
                        help="Output format (default: ascii)")
    parser.add_argument("--output", "-o", help="Output file path (for graphviz formats)")
    args = parser.parse_args()

    with open(args.trajectory, "r") as f:
        traj = json.load(f)

    tree = parse_trajectory(traj)

    if args.format == "ascii":
        print(render_ascii(tree))
    else:
        render_graphviz(tree, args.output, fmt=args.format)


if __name__ == "__main__":
    main()
