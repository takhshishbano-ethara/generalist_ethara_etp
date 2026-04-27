# -*- coding: utf-8 -*-
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from odoo import api, http, SUPERUSER_ID
from odoo.http import request
from odoo.modules.module import get_module_path
from odoo.modules.registry import Registry

from ..models.talos import _load_dotenv, _is_degenerate_output
from .llm_assisst_qc import _call_bedrock_converse, _parse_json_response

_logger = logging.getLogger(__name__)

_AUTO_HINT_POOL = ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="talos-auto-hint"
)

_satisfactory_prompt_cache = {"text": None, "mtime": 0}
_hint_gen_prompt_cache = {"text": None, "mtime": 0}


def _get_prompt_file(filename, cache):
    mod_path = get_module_path("talos")
    if not mod_path:
        return ""

    path = os.path.join(mod_path, "prompts", filename)
    if not os.path.isfile(path):
        return ""

    mtime = os.path.getmtime(path)
    if cache["text"] is not None and cache["mtime"] == mtime:
        return cache["text"]

    with open(path, "r") as f:
        cache["text"] = f.read().strip()
    cache["mtime"] = mtime
    return cache["text"]


def _get_satisfactory_prompt():
    return _get_prompt_file("auto_hint_satisfactory.md", _satisfactory_prompt_cache)


def _get_hint_gen_prompt():
    return _get_prompt_file("auto_hint_gen.md", _hint_gen_prompt_cache)


def _format_conversation(messages):
    lines = []
    for msg in messages:
        inner = msg
        if isinstance(msg, dict) and "message" in msg and "is_accepted" in msg:
            inner = msg["message"]

        message_body = inner
        if isinstance(inner, dict) and "message" in inner and "id" in inner:
            message_body = inner["message"]

        if not isinstance(message_body, dict):
            continue

        role = message_body.get("role", "")
        content = message_body.get("content", [])

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "text")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "toolCall":
                    name = block.get("name", "unknown")
                    args = block.get("arguments", {})
                    args_str = (
                        json.dumps(args, ensure_ascii=False)
                        if isinstance(args, dict)
                        else str(args)
                    )
                    if len(args_str) > 500:
                        args_str = args_str[:500] + "..."
                    text_parts.append("%s(%s)" % (name, args_str))
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if not text.strip():
            continue

        if role == "toolResult":
            tool_name = message_body.get("toolName", "")
            if len(text) > 500:
                text = text[:500] + "..."
            label = (
                "[Tool Result]" if not tool_name else "[Tool Result: %s]" % tool_name
            )
        elif role == "user":
            label = "[User]"
        elif role == "assistant":
            if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "toolCall" for b in content
            ):
                label = "[Tool Call]"
            else:
                label = "[Assistant]"
        elif role == "system":
            label = "[System]"
        else:
            label = "[%s]" % role.capitalize()

        lines.append("%s: %s" % (label, text.strip()))

    return "\n".join(lines)


def _call_kimi_with_retry(
    api_key,
    inference_arn,
    region,
    user_message,
    label,
    sandbox_id,
    turn_id,
    iteration,
    total_usage,
    max_tokens=2048,
    temperature=0.3,
):
    _logger.info(
        "%s: calling GLM sandbox=%s turn=%s iter=%d arn=%s region=%s prompt_len=%d",
        label,
        sandbox_id,
        turn_id,
        iteration,
        inference_arn,
        region,
        len(user_message),
    )
    t0 = time.monotonic()
    response_text, usage = _call_bedrock_converse(
        api_key=api_key,
        inference_arn=inference_arn,
        region=region,
        system_prompt=None,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    elapsed = time.monotonic() - t0
    total_usage["input_tokens"] += usage.get("input_tokens", 0)
    total_usage["output_tokens"] += usage.get("output_tokens", 0)
    _logger.info(
        "%s: GLM response sandbox=%s turn=%s iter=%d "
        "elapsed=%.2fs in_tokens=%d out_tokens=%d "
        "response_len=%d raw_output=%.500s",
        label,
        sandbox_id,
        turn_id,
        iteration,
        elapsed,
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        len(response_text),
        response_text,
    )

    is_degenerate = _is_degenerate_output(response_text)
    if is_degenerate:
        try:
            json.loads(response_text)
            is_degenerate = False
        except (json.JSONDecodeError, TypeError):
            pass

    if is_degenerate:
        _logger.warning(
            "%s: degenerate output (%d chars), retrying temp=0.1: %.200s",
            label,
            len(response_text),
            response_text,
        )
        t0 = time.monotonic()
        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=None,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=0.1,
            top_p=0.7,
        )
        elapsed = time.monotonic() - t0
        total_usage["input_tokens"] += usage.get("input_tokens", 0)
        total_usage["output_tokens"] += usage.get("output_tokens", 0)
        _logger.info(
            "%s: GLM retry response sandbox=%s turn=%s iter=%d "
            "elapsed=%.2fs in_tokens=%d out_tokens=%d "
            "response_len=%d raw_output=%.500s",
            label,
            sandbox_id,
            turn_id,
            iteration,
            elapsed,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            len(response_text),
            response_text,
        )

    return response_text


def _extract_last_prompt_and_response(turn, sandbox):
    prompt_text = turn.prompt or turn.hints or ""
    response_text = turn.response or ""
    return prompt_text, response_text


def _auto_hint_eval_bg(
    db_name, sandbox_id, turn_id, group_id, iteration, notify_partner_id
):
    _logger.info(
        "auto_hint bg: STARTED sandbox=%s turn=%s iter=%d thread=%s",
        sandbox_id,
        turn_id,
        iteration,
        threading.current_thread().name,
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Phase 1: read all inputs (short cursor)
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})

                sandbox = env["talos.sandbox"].browse(sandbox_id)
                if not sandbox.exists():
                    _logger.error("auto_hint bg: sandbox %s does not exist", sandbox_id)
                    _push_error_by_partner(env, sandbox_id, iteration, notify_partner_id)
                    return

                turn = env["talos.turn"].browse(turn_id)
                if not turn.exists():
                    _logger.error("auto_hint bg: turn %s does not exist", turn_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                    return

                task = sandbox.talos_id
                if not task.exists():
                    _logger.error("auto_hint bg: task not found for sandbox %s", sandbox_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                    return

                task_id = task.id

                persona = task.persona_id
                soul_md = ""
                memory_md = ""
                if persona:
                    soul_md = (persona.soul_md or "")[:5000]
                    memory_md = (persona.memory_md or "")[:5000]
                agent_md = (task.agent_md or "")[:5000]

                prompt_text, response_text_raw = _extract_last_prompt_and_response(
                    turn, sandbox
                )

                conversation_str = ""
                if turn.trajectory_messages:
                    try:
                        traj_msgs = json.loads(turn.trajectory_messages)
                        if isinstance(traj_msgs, list) and traj_msgs:
                            conversation_str = _format_conversation(traj_msgs)
                    except (json.JSONDecodeError, TypeError):
                        pass

                if not conversation_str:
                    all_turns = sandbox.turn_ids.sorted("turn_number")
                    fallback_msgs = []
                    for t in all_turns:
                        if t.prompt:
                            fallback_msgs.append(
                                {"message": {"role": "user", "content": [{"type": "text", "text": t.prompt}]}}
                            )
                        if t.hints:
                            fallback_msgs.append(
                                {"message": {"role": "user", "content": [{"type": "text", "text": t.hints}]}}
                            )
                        if t.response:
                            fallback_msgs.append(
                                {"message": {"role": "assistant", "content": [{"type": "text", "text": t.response}]}}
                            )
                    conversation_str = _format_conversation(fallback_msgs)

                if len(conversation_str) > 30000:
                    conversation_str = conversation_str[-30000:]

                ICP = env["ir.config_parameter"].sudo()
                inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
                region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()

            # cursor closed here — all DB reads are done

            dotenv = _load_dotenv()
            api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

            if not api_key or not inference_arn:
                _logger.warning("auto_hint bg: missing Bedrock credentials")
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                return

            total_usage = {"input_tokens": 0, "output_tokens": 0}

            sat_template = _get_satisfactory_prompt()
            if not sat_template:
                _logger.warning("auto_hint bg: no auto_hint_satisfactory.md")
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                return

            sat_message = (
                sat_template
                .replace("{prompt}", prompt_text)
                .replace("{response}", response_text_raw[:15000])
                .replace("{conversation}", conversation_str)
                .replace("{soul_md}", soul_md or "(not provided)")
                .replace("{agent_md}", agent_md or "(not provided)")
                .replace("{memory_md}", memory_md or "(not provided)")
            )

            # Phase 2: call Bedrock (long-running, no cursor held)
            try:
                eval_response = _call_kimi_with_retry(
                    api_key, inference_arn, region, sat_message,
                    "auto_hint eval", sandbox_id, turn_id, iteration,
                    total_usage,
                )
            except Exception:
                _logger.exception(
                    "auto_hint eval: Bedrock call failed sandbox=%s turn=%s iter=%d",
                    sandbox_id, turn_id, iteration,
                )
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    _accumulate_kimi_tokens(env, task_id, total_usage)
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                return

            eval_parsed = _parse_json_response(eval_response)
            if not eval_parsed or not isinstance(eval_parsed, dict):
                _logger.warning(
                    "auto_hint eval: failed to parse JSON (%d chars) "
                    "sandbox=%s turn=%s iter=%d raw=%.500s",
                    len(eval_response), sandbox_id, turn_id, iteration, eval_response,
                )
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    _accumulate_kimi_tokens(env, task_id, total_usage)
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                return

            satisfied = eval_parsed.get("satisfied", False)
            reasoning = eval_parsed.get("reasoning", "")
            persona_flag = eval_parsed.get("persona_flag", False)

            _logger.info(
                "auto_hint eval: verdict sandbox=%s turn=%s iter=%d "
                "satisfied=%s persona_flag=%s reasoning=%.200s",
                sandbox_id, turn_id, iteration, satisfied, persona_flag, reasoning,
            )

            # Phase 3: write results back (fresh cursor)
            if satisfied:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    _accumulate_kimi_tokens(env, task_id, total_usage)
                    turn = env["talos.turn"].browse(turn_id)
                    if turn.exists():
                        turn.write({"feedback": "satisfied"})
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    if sandbox.exists():
                        sandbox.write({
                            "auto_hint_status": "idle",
                            "auto_hint_iteration": 0,
                            "auto_hint_group_id": False,
                        })
                    _push_bus(env, sandbox, notify_partner_id, {
                        "sandbox_id": sandbox_id,
                        "status": "satisfied",
                        "iteration": iteration,
                        "reasoning": reasoning,
                        "persona_flag": persona_flag,
                    })
                _logger.info(
                    "auto_hint bg: satisfied sandbox=%s turn=%s iter=%d",
                    sandbox_id, turn_id, iteration,
                )
                return

            hint_template = _get_hint_gen_prompt()
            if not hint_template:
                _logger.warning("auto_hint bg: no auto_hint_gen.md")
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    _accumulate_kimi_tokens(env, task_id, total_usage)
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                return

            eval_report_str = json.dumps(eval_parsed, ensure_ascii=False)

            hint_message = (
                hint_template
                .replace("{prompt}", prompt_text)
                .replace("{response}", response_text_raw[:15000])
                .replace("{conversation}", conversation_str)
                .replace("{soul_md}", soul_md or "(not provided)")
                .replace("{agent_md}", agent_md or "(not provided)")
                .replace("{memory_md}", memory_md or "(not provided)")
                .replace("{eval_report}", eval_report_str)
            )

            try:
                hint_response = _call_kimi_with_retry(
                    api_key, inference_arn, region, hint_message,
                    "auto_hint gen", sandbox_id, turn_id, iteration,
                    total_usage,
                )
            except Exception:
                _logger.exception(
                    "auto_hint gen: Bedrock call failed sandbox=%s turn=%s iter=%d",
                    sandbox_id, turn_id, iteration,
                )
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    _accumulate_kimi_tokens(env, task_id, total_usage)
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id)
                return

            hint_parsed = _parse_json_response(hint_response)
            hint = ""
            if hint_parsed and isinstance(hint_parsed, dict):
                hint = hint_parsed.get("hint", "")
            if not hint:
                hint = hint_response.strip()[:500]

            _logger.info(
                "auto_hint gen: hint sandbox=%s turn=%s iter=%d hint=%.200s",
                sandbox_id, turn_id, iteration, hint,
            )

            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                _accumulate_kimi_tokens(env, task_id, total_usage)
                turn = env["talos.turn"].browse(turn_id)
                sandbox = env["talos.sandbox"].browse(sandbox_id)

                if turn.exists():
                    turn.write({"feedback": "unsatisfied", "hint_text": hint})

                if iteration >= 5:
                    if sandbox.exists():
                        sandbox.write({"auto_hint_status": "max_retries"})
                    _push_bus(env, sandbox, notify_partner_id, {
                        "sandbox_id": sandbox_id,
                        "status": "max_retries",
                        "iteration": iteration,
                        "reasoning": reasoning,
                        "hint": hint,
                    })
                    _logger.info(
                        "auto_hint bg: max_retries sandbox=%s iter=%d",
                        sandbox_id, iteration,
                    )
                else:
                    if sandbox.exists():
                        sandbox.write({"auto_hint_status": "sending_hint"})
                    _push_bus(env, sandbox, notify_partner_id, {
                        "sandbox_id": sandbox_id,
                        "status": "unsatisfied",
                        "iteration": iteration,
                        "reasoning": reasoning,
                        "hint": hint,
                        "turn_id": turn_id,
                        "group_id": group_id,
                    })
                    _logger.info(
                        "auto_hint bg: unsatisfied sandbox=%s turn=%s iter=%d hint=%.80s",
                        sandbox_id, turn_id, iteration, hint,
                    )
            return
        except Exception as e:
            err_str = str(e).lower()
            if ("serialize" in err_str or "concurrent" in err_str) and attempt < max_retries - 1:
                _logger.warning(
                    "auto_hint bg: serialization conflict (attempt %d/%d) sandbox=%s turn=%s: %s",
                    attempt + 1, max_retries, sandbox_id, turn_id, e,
                )
                time.sleep(1 + attempt)
                continue
            _logger.exception(
                "auto_hint bg: unhandled error sandbox=%s turn=%s iter=%d",
                sandbox_id, turn_id, iteration,
            )


def _accumulate_kimi_tokens(env, task_id, usage):
    try:
        t_in = usage.get("input_tokens", 0)
        t_out = usage.get("output_tokens", 0)
        if t_in <= 0 and t_out <= 0:
            return
        task = env["talos.talos"].browse(task_id)
        if not task.exists():
            return
        task.write({
            "kimi_eval_input_tokens": (task.kimi_eval_input_tokens or 0) + t_in,
            "kimi_eval_output_tokens": (task.kimi_eval_output_tokens or 0) + t_out,
        })
    except Exception:
        _logger.exception("_accumulate_glm_tokens failed for task=%s", task_id)


def _push_bus(env, sandbox, notify_partner_id, payload):
    partner = None
    if notify_partner_id:
        partner = env["res.partner"].browse(notify_partner_id)
        if not partner.exists():
            partner = None
    if not partner:
        try:
            partner = sandbox.employee_id.user_id.partner_id
        except Exception:
            partner = None
    if not partner:
        partner = env["res.users"].browse(SUPERUSER_ID).partner_id
    env["bus.bus"]._sendone(partner, "talos/auto_hint_result", payload)


def _push_error(env, sandbox, sandbox_id, iteration, notify_partner_id):
    try:
        if sandbox.exists():
            sandbox.write({"auto_hint_status": "error"})
    except Exception:
        _logger.exception(
            "auto_hint bg: failed to write error status on sandbox %s", sandbox_id
        )
    _push_bus(
        env,
        sandbox,
        notify_partner_id,
        {
            "sandbox_id": sandbox_id,
            "status": "error",
            "iteration": iteration,
        },
    )


def _push_error_by_partner(env, sandbox_id, iteration, notify_partner_id):
    partner = None
    if notify_partner_id:
        partner = env["res.partner"].browse(notify_partner_id)
        if not partner.exists():
            partner = None
    if not partner:
        partner = env["res.users"].browse(SUPERUSER_ID).partner_id
    env["bus.bus"]._sendone(
        partner,
        "talos/auto_hint_result",
        {
            "sandbox_id": sandbox_id,
            "status": "error",
            "iteration": iteration,
        },
    )


class AutoHintController(http.Controller):

    @http.route("/talos/auto_hint_eval", type="json", auth="user")
    def auto_hint_eval(self, turn_id=0, sandbox_id=0, **kw):
        turn_id = int(turn_id or 0)
        sandbox_id = int(sandbox_id or 0)
        _logger.info(
            "auto_hint_eval: received request turn_id=%s sandbox_id=%s",
            turn_id, sandbox_id,
        )
        if not turn_id or not sandbox_id:
            _logger.info(
                "auto_hint_eval: REJECTED missing params turn_id=%s sandbox_id=%s",
                turn_id, sandbox_id,
            )
            return {"error": "turn_id and sandbox_id are required"}

        turn = request.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            _logger.info("auto_hint_eval: REJECTED turn %s not found", turn_id)
            return {"error": "Turn not found"}

        if turn.turn_status != "Completed":
            _logger.info(
                "auto_hint_eval: REJECTED turn %s status='%s' (expected 'Completed')",
                turn_id, turn.turn_status,
            )
            return {"error": "Turn is not completed (status=%s)" % turn.turn_status}

        if not turn.response:
            _logger.info("auto_hint_eval: REJECTED turn %s has no response", turn_id)
            return {"error": "Turn has no response"}

        sandbox = request.env["talos.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            _logger.info("auto_hint_eval: REJECTED sandbox %s not found", sandbox_id)
            return {"error": "Sandbox not found"}

        current_iter = sandbox.auto_hint_iteration or 0
        if current_iter >= 5:
            _logger.info(
                "auto_hint_eval: REJECTED sandbox %s max_retries (iter=%d)",
                sandbox_id, current_iter,
            )
            return {"status": "max_retries"}

        group_id = sandbox.auto_hint_group_id or ""
        if current_iter == 0:
            group_id = uuid.uuid4().hex

        new_iter = current_iter + 1
        sandbox.write({
            "auto_hint_status": "evaluating",
            "auto_hint_iteration": new_iter,
            "auto_hint_group_id": group_id,
        })

        db_name = request.env.cr.dbname
        notify_partner_id = request.env.user.partner_id.id

        _logger.info(
            "auto_hint_eval: submitting bg worker sandbox=%s turn=%s iter=%d group=%s",
            sandbox_id, turn_id, new_iter, group_id,
        )

        try:
            future = _AUTO_HINT_POOL.submit(
                _auto_hint_eval_bg,
                db_name,
                sandbox_id,
                turn_id,
                group_id,
                new_iter,
                notify_partner_id,
            )
            _logger.info(
                "auto_hint_eval: submitted to pool sandbox=%s turn=%s future=%s",
                sandbox_id, turn_id, future,
            )
        except Exception:
            _logger.exception(
                "auto_hint_eval: failed to submit to pool sandbox=%s turn=%s",
                sandbox_id, turn_id,
            )
            return {"error": "Failed to submit background task"}

        return {"status": "pending"}
