# -*- coding: utf-8 -*-
"""SSE streaming endpoint for prompt_qc (single, browser-driven "Start QC").

One POST route, /prompt_qc/run/stream, streams an AWS Bedrock converse-stream judge call
to the browser as Server-Sent Events. This is the kensei2 convention: fire-forward SSE
(data: lines only, no `id:`, no Last-Event-ID, no replay buffer).

The Bedrock client itself lives in services/bedrock_judge.py and is SHARED with the bulk
background worker (models/prompt_qc_run.py). This controller only adds the HTTP/SSE framing
on top of `bedrock_judge.iter_judge_events`, so single-run and bulk-run go through one
Bedrock code path.

Persistence happens in the generator's `finally` via a fresh Registry().cursor() (the DB
cursor is never held across the stream) and routes through `prompt.qc.run._mark_done` /
`_mark_failed` — the same terminal-state writers the bulk worker uses. The response is
written with state='done' ONLY when the stream reaches Bedrock's terminal event (tracked by
`completed`); a partial or aborted stream is written as state='failed'.
"""
import json
import logging
import time

from werkzeug.wrappers import Response as WerkzeugResponse

from odoo import _, api, http, SUPERUSER_ID
from odoo.http import request
from odoo.modules.registry import Registry

from ..services import bedrock_judge

_logger = logging.getLogger(__name__)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ----------------------------------------------------------------------
# SSE framing.
# ----------------------------------------------------------------------
def _sse_line(data):
    return ("data: %s\n\n" % json.dumps(data, ensure_ascii=False)).encode("utf-8")


def _sse_error_response(message):
    body = (
        _sse_line({"type": "error", "message": message})
        + _sse_line({"type": "complete", "status": "failed"})
    )
    return WerkzeugResponse(body, mimetype="text/event-stream", headers=SSE_HEADERS,
                            direct_passthrough=True)


def _persist_result(db_name, rec_id, accumulated, usage, ok, error, duration, model_label):
    """Write the terminal result in a fresh cursor, via the shared model writers."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            run = env["prompt.qc.run"].browse(rec_id)
            if not run.exists():
                return
            if ok:
                run._mark_done(accumulated, usage, duration, model_label)
            else:
                # D1: do not store partial text. Mark failed with the reason.
                run._mark_failed(error, duration, model_label)
    except Exception:
        _logger.exception("prompt_qc: failed to persist result for run %s", rec_id)


class PromptQcStreamingController(http.Controller):

    @http.route("/prompt_qc/run/stream", type="http", auth="user", methods=["POST"],
                csrf=False)
    def run_stream(self, **_kw):
        try:
            body = json.loads(request.httprequest.data or b"{}")
        except Exception:
            body = {}
        record_id = body.get("record_id")
        if not record_id:
            return _sse_error_response(_("Missing record_id."))
        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            return _sse_error_response(_("Invalid record_id."))

        ICP = request.env["ir.config_parameter"].sudo()
        api_key = bedrock_judge.get_api_key(ICP)
        if not api_key:
            return _sse_error_response(_(
                "Missing Bedrock API key. Set prompt_qc.bedrock_api_key in Settings or "
                "AWS_BEARER_TOKEN_BEDROCK in .env."
            ))
        inference_arn, region = bedrock_judge.resolve_arn_and_region(ICP)
        if not inference_arn:
            return _sse_error_response(_("Missing prompt_qc.bedrock_inference_arn in Settings."))

        run = request.env["prompt.qc.run"].browse(record_id)
        if not run.exists():
            return _sse_error_response(_("QC run %s not found.") % record_id)
        if not (run.user_prompt and run.user_prompt.strip()):
            return _sse_error_response(_("The user prompt is empty."))

        system_prompt = run._get_system_prompt()
        if not (system_prompt and system_prompt.strip()):
            return _sse_error_response(_(
                "No system prompt configured. Upload a .md judge prompt in Settings → Prompt QC."
            ))
        user_prompt = run.user_prompt
        rubric_text = run._get_rubric()

        # Mark streaming and clear any prior result before the run starts.
        run.write({"state": "streaming", "response": False, "error_message": False})

        db_name = request.env.cr.dbname
        record_db_id = run.id
        model_label = inference_arn.rsplit("/", 1)[-1][:120] or inference_arn[:120]

        def _logged_stream():
            t0 = time.time()
            accumulated = ""
            usage = {}
            completed = False
            error = None
            try:
                for kind, data in bedrock_judge.iter_judge_events(
                    api_key, inference_arn, region, system_prompt, user_prompt,
                    rubric_text=rubric_text,
                    max_tokens=bedrock_judge.JUDGE_MAX_TOKENS,
                    temperature=bedrock_judge.JUDGE_TEMPERATURE,
                    timeout=600.0,
                ):
                    if kind == "delta":
                        accumulated += data
                        yield _sse_line({"type": "delta", "text": data})
                    elif kind == "metadata":
                        usage = data or {}
                        yield _sse_line({"type": "metadata", "usage": usage})
                    elif kind == "done":
                        completed = True
                        yield _sse_line({"type": "done"})
                    elif kind == "error":
                        error = (data or "")[:1000]
                        yield _sse_line({"type": "error", "message": error})
            except Exception as exc:
                _logger.exception("prompt_qc: stream loop failed")
                error = str(exc)[:1000]
                yield _sse_line({"type": "error", "message": error})
            finally:
                duration = round(time.time() - t0, 2)
                ok = completed and not error
                # Persist BEFORE the trailing yield so a client disconnect still saves.
                _persist_result(db_name, record_db_id, accumulated, usage, ok, error,
                                 duration, model_label)
                status = "done" if ok else "failed"
                yield _sse_line({"type": "complete", "status": status, "duration": duration})

        return WerkzeugResponse(
            _logged_stream(),
            mimetype="text/event-stream",
            headers=SSE_HEADERS,
            direct_passthrough=True,
        )
