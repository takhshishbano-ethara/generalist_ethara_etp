# -*- coding: utf-8 -*-
import json
import logging
import queue

from odoo import http
from odoo.http import request, Response

from ..services.sse_manager import SSEConnectionManager

logger = logging.getLogger("kensei2.sse")


class Kensei2SSEController(http.Controller):

    @http.route("/kensei2/sse/stream", type="http", auth="user", cors="*", methods=["GET"])
    def sse_stream(self, sandbox_id=None, ws_url=None, token=None, **kw):
        if not sandbox_id or not ws_url or not token:
            return Response(
                json.dumps({"error": "sandbox_id, ws_url, and token are required"}),
                status=400, content_type="application/json",
            )

        sandbox_id = int(sandbox_id)
        sandbox = request.env["kensei2.sandbox"].sudo().browse(sandbox_id)
        if not sandbox.exists():
            return Response(
                json.dumps({"error": "Sandbox not found"}),
                status=404, content_type="application/json",
            )

        manager = SSEConnectionManager.instance()
        sub_queue = manager.subscribe(sandbox_id, ws_url, token)

        def event_stream():
            try:
                while True:
                    try:
                        event = sub_queue.get(timeout=45)
                        yield f"event: {event.event}\ndata: {event.data}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            except GeneratorExit:
                pass
            finally:
                manager.unsubscribe(sandbox_id, sub_queue)

        return Response(
            event_stream(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
            direct_passthrough=True,
        )

    @http.route("/kensei2/sse/send", type="json", auth="user", methods=["POST"])
    def sse_send(self, sandbox_id=None, text="", attachments=None, **kw):
        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox_id = int(sandbox_id)
        manager = SSEConnectionManager.instance()

        if not manager.is_connected(sandbox_id):
            return {"error": "Not connected to sandbox"}

        try:
            result = manager.send_message(sandbox_id, text, attachments)
            return {"ok": True, **result}
        except Exception as e:
            logger.error("SSE send failed for sandbox %s: %s", sandbox_id, e)
            return {"error": str(e)}

    @http.route("/kensei2/sse/abort", type="json", auth="user", methods=["POST"])
    def sse_abort(self, sandbox_id=None, **kw):
        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox_id = int(sandbox_id)
        manager = SSEConnectionManager.instance()

        if not manager.is_connected(sandbox_id):
            return {"error": "Not connected to sandbox"}

        try:
            result = manager.abort(sandbox_id)
            return {"ok": True, **result}
        except Exception as e:
            logger.error("SSE abort failed for sandbox %s: %s", sandbox_id, e)
            return {"error": str(e)}

    @http.route("/kensei2/sse/history", type="json", auth="user", methods=["POST"])
    def sse_history(self, sandbox_id=None, limit=200, **kw):
        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox_id = int(sandbox_id)
        manager = SSEConnectionManager.instance()

        if not manager.is_connected(sandbox_id):
            return {"error": "Not connected to sandbox"}

        try:
            messages = manager.fetch_history(sandbox_id, limit=int(limit))
            return {"ok": True, "messages": messages}
        except Exception as e:
            logger.error("SSE history failed for sandbox %s: %s", sandbox_id, e)
            return {"error": str(e)}
