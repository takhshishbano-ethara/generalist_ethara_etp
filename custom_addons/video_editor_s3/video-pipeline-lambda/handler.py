import json
import logging
import time

import config
from modules import callback, render, youtube_ingest

_logger = logging.getLogger()
_logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    started = time.time()
    op = (event or {}).get("op") or "echo"
    job_id = (event or {}).get("job_id")
    request_id = getattr(context, "aws_request_id", "")
    _logger.info("[job=%s op=%s req=%s] received", job_id, op, request_id)

    if op == "youtube_ingest":
        try:
            result = youtube_ingest.run(event, context)
        except Exception as exc:
            _logger.exception("[job=%s op=%s] handler crashed", job_id, op)
            result = {"status": "error", "error": "handler exception: %s" % exc}
    elif op == "render":
        try:
            result = render.run(event, context)
        except Exception as exc:
            _logger.exception("[job=%s op=%s] render crashed", job_id, op)
            result = {"status": "error", "error": "handler exception: %s" % exc}
    elif op == "echo":
        result = {
            "status": "echo",
            "op": op,
            "received_keys": sorted(list((event or {}).keys())),
        }
    else:
        result = {"status": "error", "error": "unknown op: %s" % op}

    elapsed_ms = int((time.time() - started) * 1000)
    result.update({
        "job_id": job_id,
        "lambda_request_id": request_id,
        "elapsed_ms": elapsed_ms,
    })

    callback_url = (event or {}).get("callback_url")
    if callback_url:
        callback.post(callback_url, result)
    else:
        _logger.info("[job=%s op=%s] no callback_url in payload; skipping POST", job_id, op)

    _logger.info("[job=%s op=%s req=%s] done in %dms", job_id, op, request_id, elapsed_ms)
    return result
