import json
import logging

from odoo import http
from odoo.http import request

from ..models.atlas_sandbox import MODEL_DEFAULTS

_logger = logging.getLogger(__name__)

_TURN_FIELD_MAX_BYTES = 1_048_576
_SAVE_RESPONSE_MAX_COMBINED_BYTES = 3 * 1_048_576


def _is_admin():
    return request.env.user.has_group('base.group_system')


def _owns_sandbox(sandbox):
    owner = sandbox.atlas_id.employee_id.user_id if sandbox.atlas_id else False
    return bool(owner) and owner.id == request.env.user.id


def _owns_turn(turn):
    owner = turn.employee_id.user_id if turn.employee_id else False
    return bool(owner) and owner.id == request.env.user.id


def _cap(value, limit=_TURN_FIELD_MAX_BYTES):
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[:limit]


class AtlasChatController(http.Controller):
    @http.route("/atlas/chat/create_turn", type="json", auth="user")
    def create_turn(
        self, sandbox_id=0, message="", model=None, timestamp="", is_hint=False, **kw
    ):
        sandbox_id = int(sandbox_id or 0)
        message = (message or "").strip()

        if not sandbox_id or not message:
            return {"error": "sandbox_id and message are required"}

        sandbox = request.env["atlas.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}
        if not (_owns_sandbox(sandbox) or _is_admin()):
            return {"error": "Access denied"}

        if model is None:
            model = MODEL_DEFAULTS.get(sandbox.model_type, "unknown")

        current_session_turns = sandbox.turn_ids.filtered(
            lambda t: t.session_id == sandbox.current_session_id
        ) if sandbox.current_session_id else sandbox.turn_ids
        next_num = len(current_session_turns) + 1
        is_hint_turn = bool(is_hint)
        vals = {
            "sandbox_id": sandbox.id,
            "session_id": sandbox.current_session_id or "",
            "turn_number": next_num,
            "model_name": model,
            "turn_status": "Pending",
            "is_hint_turn": is_hint_turn,
        }
        if is_hint_turn:
            vals["hints"] = _cap(message)
        else:
            vals["prompt"] = _cap(message)
        if timestamp:
            vals["prompt_timestamp"] = timestamp

        turn = request.env["atlas.turn"].create(vals)

        if sandbox.session_status == "not_started":
            sandbox.sudo().write({"session_status": "in_progress"})

        return {"turn_id": turn.id}

    @http.route("/atlas/chat/save_response", type="json", auth="user")
    def save_response(
        self,
        turn_id=0,
        response="",
        tool_calls="",
        raw_events="",
        run_id="",
        timestamp="",
        partial=False,
        **kw,
    ):
        turn_id = int(turn_id or 0)

        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["atlas.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}
        if not (_owns_turn(turn) or _is_admin()):
            return {"error": "Access denied"}

        response_str = response or ""
        tool_calls_str = tool_calls or ""
        raw_events_str = raw_events or ""
        combined_bytes = (
            len(response_str.encode("utf-8", errors="replace"))
            + len(tool_calls_str.encode("utf-8", errors="replace"))
            + len(raw_events_str.encode("utf-8", errors="replace"))
        )
        if combined_bytes > _SAVE_RESPONSE_MAX_COMBINED_BYTES:
            _logger.warning(
                "save_response rejected: combined payload %d > %d bytes for turn=%s user=%s",
                combined_bytes, _SAVE_RESPONSE_MAX_COMBINED_BYTES, turn_id,
                request.env.user.id,
            )
            return {
                "error": "Payload too large",
                "max_bytes": _SAVE_RESPONSE_MAX_COMBINED_BYTES,
                "status": 413,
            }

        vals = {
            "response": _cap(response_str),
            "turn_status": "Streaming" if partial else "Completed",
        }
        if run_id:
            vals["run_id"] = run_id
        if timestamp:
            vals["response_timestamp"] = timestamp
        if tool_calls_str:
            vals["tool_calls"] = _cap(tool_calls_str)
        if raw_events_str:
            vals["raw_events"] = _cap(raw_events_str)

        turn.write(vals)

        return {"success": True}

    @staticmethod
    def _update_goal_description(task):
        glm_sandbox = task.sandbox_ids.filtered(
            lambda s: s.model_type == "glm"
        )[:1]
        if glm_sandbox and glm_sandbox.current_session_id:
            turns = task.turn_ids.filtered(
                lambda t, sid=glm_sandbox.current_session_id: t.session_id == sid
            ).sorted("turn_number")
        else:
            turns = task.turn_ids.sorted("turn_number")
        prompts = [t.prompt for t in turns if t.prompt and not t.is_hint_turn]
        if not prompts:
            return
        description = "; ".join(p.strip()[:200] for p in prompts[:5])
        if len(prompts) > 5:
            description += f" (+{len(prompts) - 5} more prompts)"
        task.sudo().write({"goal_description": description})

    @http.route("/atlas/chat/save_qc", type="json", auth="user")
    def save_qc(
        self,
        turn_id=0,
        severity="",
        qc_response="",
        dismiss_reason="",
        bedrock_input_tokens=0,
        bedrock_output_tokens=0,
        **kw,
    ):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["atlas.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}
        if not (_owns_turn(turn) or _is_admin()):
            return {"error": "Access denied"}

        severity = (severity or "").strip().lower()
        valid = ("low", "medium", "high", "critical")
        if severity not in valid:
            return {"error": "Invalid severity: %s" % severity}

        try:
            in_tok = max(0, int(bedrock_input_tokens or 0))
            out_tok = max(0, int(bedrock_output_tokens or 0))
        except (TypeError, ValueError):
            in_tok = out_tok = 0

        vals = {
            "qc_severity": severity,
        }
        if qc_response:
            vals["qc_response"] = _cap(qc_response)
        if dismiss_reason:
            vals["qc_dismiss_reason"] = _cap(dismiss_reason, 8192)
        if in_tok:
            vals["qc_input_tokens"] = in_tok
        if out_tok:
            vals["qc_output_tokens"] = out_tok

        turn.write(vals)

        task = turn.atlas_id
        if task and (in_tok or out_tok):
            task.sudo().write({
                "qc_input_tokens": (task.qc_input_tokens or 0) + in_tok,
                "qc_output_tokens": (task.qc_output_tokens or 0) + out_tok,
            })

        return {"success": True}

    @http.route("/atlas/chat/save_feedback", type="json", auth="user")
    def save_feedback(self, turn_id=0, feedback="", hint_text="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["atlas.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}
        if not (_owns_turn(turn) or _is_admin()):
            return {"error": "Access denied"}

        feedback = (feedback or "").strip().lower()
        if feedback not in ("satisfied", "unsatisfied"):
            return {"error": "Invalid feedback: %s" % feedback}

        vals = {"feedback": feedback}
        if hint_text:
            vals["hint_text"] = _cap(hint_text, 16384)

        turn.write(vals)
        return {"success": True}

    @http.route("/atlas/chat/history", type="json", auth="user")
    def chat_history(self, sandbox_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)

        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox = request.env["atlas.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}
        if not (_owns_sandbox(sandbox) or _is_admin()):
            return {"error": "Access denied"}

        turns = []
        session_turns = sandbox.turn_ids
        if sandbox.current_session_id:
            session_turns = session_turns.filtered(
                lambda t: t.session_id == sandbox.current_session_id
            )
        for t in session_turns:
            turns.append(
                {
                    "id": t.id,
                    "turn_number": t.turn_number,
                    "prompt": t.prompt or "",
                    "response": t.response or "",
                    "run_id": t.run_id or "",
                    "model": t.model_name or "",
                    "status": t.turn_status or "",
                    "prompt_timestamp": t.prompt_timestamp or "",
                    "response_timestamp": t.response_timestamp or "",
                    "tool_calls": t.tool_calls or "",
                    "qc_severity": t.qc_severity or "",
                    "qc_response": t.qc_response or "",
                    "qc_dismiss_reason": t.qc_dismiss_reason or "",
                    "feedback": t.feedback or "",
                    "hints": t.hints or "",
                    "hint_text": t.hint_text or "",
                    "is_hint_turn": t.is_hint_turn or False,
                }
            )

        return {"turns": turns}
