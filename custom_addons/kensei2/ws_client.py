"""
Asyncio-based WebSocket client for OpenClaw gateways in Kensei2 sandbox containers.

Provides a synchronous-API wrapper (OpenClawClient) that can be used by a
standalone RabbitMQ consumer running outside Odoo.

Dependencies: websockets (asyncio)
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass

import websockets

logger = logging.getLogger("kensei2.ws_client")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OpenClawResponse:
    text: str
    tool_calls_json: str  # JSON string of tool calls, or ""


class OpenClawError(Exception):
    pass


class OpenClawAuthError(OpenClawError):
    pass


class OpenClawTimeoutError(OpenClawError):
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenClawClient:
    """Synchronous wrapper around an asyncio WebSocket connection to OpenClaw."""

    def __init__(self, ws_url, gateway_token, sandbox_id, logger_override=None):
        self._ws_url = ws_url
        self._gateway_token = gateway_token
        self._sandbox_id = int(sandbox_id)
        self._log = logger_override or logger

        # Message ID counter
        self._msg_counter = 0

        # Threading primitives
        self._connected = threading.Event()
        self._loop = None
        self._thread = None
        self._ws = None

        self._pending_rpcs: dict = {}
        self._stream_buf = ""
        self._tool_calls: list = []
        self._response_future = None
        self._stop_event = None
        self._connect_error = None

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _next_id(self):
        self._msg_counter += 1
        ts = int(time.time())
        return f"kensei2-{self._msg_counter}-{ts:x}"

    # ------------------------------------------------------------------
    # Session key
    # ------------------------------------------------------------------

    @property
    def _session_key(self):
        return f"odoo:sandbox:{self._sandbox_id}"

    # ------------------------------------------------------------------
    # Public sync API
    # ------------------------------------------------------------------

    def connect(self, timeout=30):
        if self._thread is not None and self._thread.is_alive():
            raise OpenClawError("Already connected")

        self._connected.clear()
        self._connect_error = None
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="openclaw-ws"
        )
        self._thread.start()

        if not self._connected.wait(timeout=timeout):
            self.disconnect()
            raise OpenClawTimeoutError(
                f"Connection to {self._ws_url} timed out after {timeout}s"
            )

        err = self._connect_error
        if err:
            self._connect_error = None
            self.disconnect()
            raise OpenClawError(f"Connection failed: {err}")

        self._log.info("Connected to OpenClaw at %s", self._ws_url)

    def _require_loop(self):
        loop = self._loop
        if loop is None:
            raise OpenClawError("Not connected")
        return loop

    def send_message(self, text, attachments=None):
        """Send a chat.send message. Does NOT wait for response."""
        if not self._connected.is_set():
            raise OpenClawError("Not connected")
        loop = self._require_loop()

        fut = asyncio.run_coroutine_threadsafe(self._async_send_message(text, attachments), loop)
        # Wait briefly for the send to complete (network write)
        fut.result(timeout=10)

    def send_message_with_file_ids(self, text, file_ids, env):
        """Resolve ir.attachment file_ids to base64 and send via chat.send."""
        attachments = []
        if file_ids:
            for att in env["ir.attachment"].sudo().browse(file_ids):
                if att.exists() and att.datas:
                    attachments.append({
                        "fileName": att.name,
                        "mimeType": att.mimetype,
                        "content": att.datas.decode(),
                    })
        self.send_message(text, attachments if attachments else None)

    def wait_for_response(self, timeout=600):
        """Wait for response completion. Returns OpenClawResponse."""
        if not self._connected.is_set():
            raise OpenClawError("Not connected")
        loop = self._require_loop()

        fut = asyncio.run_coroutine_threadsafe(
            self._async_wait_for_response(timeout), loop
        )
        try:
            return fut.result(timeout=timeout + 5)
        except TimeoutError:
            raise OpenClawTimeoutError(f"Response not received within {timeout}s")

    def fetch_history(self, limit=1000):
        if not self._connected.is_set():
            raise OpenClawError("Not connected")
        loop = self._require_loop()

        fut = asyncio.run_coroutine_threadsafe(self._async_fetch_history(limit), loop)
        return fut.result(timeout=35)

    def disconnect(self):
        """Close WS, stop event loop thread."""
        self._log.info("Disconnecting from OpenClaw")
        if self._loop and not self._loop.is_closed():
            if self._stop_event:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            if self._ws:
                asyncio.run_coroutine_threadsafe(self._close_ws(), self._loop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._connected.clear()
        self._ws = None
        self._loop = None
        self._thread = None

    # ------------------------------------------------------------------
    # Internal: event-loop thread entry point
    # ------------------------------------------------------------------

    def _run_loop(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._log.error("Event loop crashed: %s", exc)
        finally:
            self._connected.clear()

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        try:
            async with websockets.connect(
                self._ws_url,
                max_size=1650 * 1024 * 1024,
                ping_interval=None,
                close_timeout=5,
            ) as ws:
                self._ws = ws
                await self._handshake(ws)
                # Run recv loop + keepalive concurrently, stop on _stop_event
                await asyncio.gather(
                    self._recv_loop(ws),
                    self._keepalive_loop(ws),
                    self._wait_for_stop(),
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("WS connection error: %s", exc)
            if not self._connected.is_set():
                self._connect_error = exc
                self._connected.set()

    async def _wait_for_stop(self):
        stop = self._stop_event
        if stop is None:
            return
        await stop.wait()
        raise asyncio.CancelledError()

    async def _close_ws(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self, ws):
        """Wait for connect.challenge, send auth, wait for success."""
        # Step 1: wait for connect.challenge
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenClawTimeoutError("Timed out waiting for connect.challenge")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise OpenClawTimeoutError("Timed out waiting for connect.challenge")

            frame = self._parse_frame(raw)
            if frame is None:
                continue
            if (
                frame.get("type") == "event"
                and frame.get("event") == "connect.challenge"
            ):
                self._log.debug("Received connect.challenge")
                break

        # Step 2: send connect request
        connect_msg = {
            "type": "req",
            "id": self._next_id(),
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 4,
                "client": {
                    "id": "gateway-client",
                    "version": "1.0",
                    "platform": "server",
                    "mode": "backend",
                },
                "role": "operator",
                "scopes": [
                    "operator.admin",
                    "operator.read",
                    "operator.write",
                    "operator.approvals",
                    "operator.pairing",
                ],
                "caps": ["tool-events", "thinking-events"],
                "auth": {"token": self._gateway_token},
                "userAgent": "kensei2-auto-process/1.0",
                "locale": "en-US",
            },
        }
        await ws.send(json.dumps(connect_msg))
        self._log.debug("Sent connect request")

        # Step 3: wait for connect response
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenClawTimeoutError("Timed out waiting for connect response")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise OpenClawTimeoutError("Timed out waiting for connect response")

            frame = self._parse_frame(raw)
            if frame is None:
                continue
            if frame.get("type") == "res":
                if frame.get("ok"):
                    self._log.debug("Connect OK")
                    self._connected.set()
                    return
                else:
                    err = frame.get("error", {})
                    msg = (
                        err.get("message", json.dumps(err))
                        if isinstance(err, dict)
                        else str(err)
                    )
                    raise OpenClawAuthError(msg)

    # ------------------------------------------------------------------
    # Recv loop
    # ------------------------------------------------------------------

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                self._handle_raw(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error("Recv loop error: %s", exc)
            self._connected.clear()

    def _handle_raw(self, raw):
        """Process a single incoming WS frame."""
        # Drop heartbeat text frames
        if isinstance(raw, str) and raw.strip() in (
            "HEARTBEAT_OK",
            "HEARTBEAT",
            "PONG",
        ):
            return

        frame = self._parse_frame(raw)
        if frame is None:
            return

        frame_type = frame.get("type")
        event = frame.get("event")

        # Silently ignored events
        if frame_type == "event" and event in (
            "tick",
            "health",
            "presence",
            "heartbeat",
        ):
            return

        # RPC responses
        if frame_type == "res":
            frame_id = frame.get("id")
            if frame_id and frame_id in self._pending_rpcs:
                future = self._pending_rpcs.pop(frame_id)
                if not future.done():
                    if frame.get("ok"):
                        future.set_result(frame)
                    else:
                        future.set_exception(
                            OpenClawError(
                                frame.get("error", {}).get("message", "RPC failed")
                                if isinstance(frame.get("error"), dict)
                                else str(frame.get("error", "RPC failed"))
                            )
                        )
                return
            # Non-pending RPC response during streaming — check for error
            if (
                not frame.get("ok")
                and self._response_future
                and not self._response_future.done()
            ):
                err = frame.get("error", {})
                msg = (
                    err.get("message", json.dumps(err))
                    if isinstance(err, dict)
                    else str(err)
                )
                self._response_future.set_exception(OpenClawError(msg))
            return

        # Chat events
        if frame_type == "event" and event == "chat":
            payload = frame.get("payload", {})
            self._handle_chat_event(payload)
            return

        # Tool/session.tool events — log only
        if frame_type == "event" and event in ("session.tool", "tool"):
            self._log.debug("Tool event: %s", event)
            return

        # Agent events — log only
        if frame_type == "event" and event == "agent":
            self._log.debug("Agent event received")
            return

        # Unhandled events
        if frame_type == "event":
            self._log.debug("Unhandled event: %s", event)

    def _handle_chat_event(self, payload):
        """Handle a chat event payload."""
        state = payload.get("state")
        stream = payload.get("stream")
        data = payload.get("message") or payload.get("data") or payload

        # Check for heartbeat in data text
        data_text = ""
        if isinstance(data, dict):
            data_text = data.get("text", "")
        if isinstance(data_text, str) and "HEARTBEAT" in data_text:
            return

        self._log.debug(
            "Chat event: stream=%s state=%s", stream or "none", state or "none"
        )

        # Stream accumulation: assistant text (data.text is FULL text, not delta)
        if stream == "assistant" and isinstance(data, dict) and data.get("text"):
            self._stream_buf = data["text"]
            return

        # Tool stream — log but don't process
        if stream == "tool":
            self._log.debug(
                "Tool stream event: phase=%s",
                data.get("phase", "") if isinstance(data, dict) else "",
            )
            return

        # Lifecycle events
        if stream == "lifecycle" and isinstance(data, dict):
            phase = data.get("phase", "")
            if phase == "start":
                self._log.debug("Lifecycle start — thinking")
                return
            if phase == "end":
                self._log.debug("Lifecycle end — completing response")
                self._complete_response(self._stream_buf, self._tool_calls)
                return
            if phase == "error":
                err_text = (
                    data.get("message")
                    or data.get("error")
                    or data.get("reason")
                    or json.dumps(data)
                )
                self._log.error("Lifecycle error: %s", err_text)
                if self._response_future and not self._response_future.done():
                    self._response_future.set_exception(OpenClawError(err_text))
                return

        # State-based events
        if state == "final":
            message = payload.get("message")
            final_text = self._extract_text(message)
            tool_calls = self._extract_tool_calls(message)
            self._log.debug(
                "Final state: text_len=%d tools=%d",
                len(final_text),
                len(tool_calls),
            )
            self._complete_response(
                final_text or self._stream_buf, tool_calls or self._tool_calls
            )
            return

        if state == "error":
            err_text = payload.get("errorMessage") or "Chat error"
            self._log.error("Chat error state: %s", err_text)
            if self._response_future and not self._response_future.done():
                self._response_future.set_exception(OpenClawError(err_text))
            return

        if state == "aborted":
            self._log.warning("Chat aborted")
            text = self._stream_buf or "[Aborted]"
            self._complete_response(text, self._tool_calls)
            return

    def _complete_response(self, text, tool_calls):
        """Resolve the response future with accumulated data."""
        tool_json = json.dumps(tool_calls) if tool_calls else ""
        response = OpenClawResponse(text=text or "", tool_calls_json=tool_json)
        if self._response_future and not self._response_future.done():
            self._response_future.set_result(response)
        # Reset buffers
        self._stream_buf = ""
        self._tool_calls = []

    # ------------------------------------------------------------------
    # Text extraction (mirrors JS _extractText)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(message):
        if not message:
            return ""
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            if isinstance(message.get("text"), str):
                return message["text"]
            content = message.get("content")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("text"):
                        parts.append(block["text"])
                return "".join(parts)
            if isinstance(content, str):
                return content
            if message.get("role") and content:
                return OpenClawClient._extract_text(content)
        return json.dumps(message)

    @staticmethod
    def _extract_tool_calls(message):
        if not message:
            return []
        content = message.get("content") or message.get("messages") or []
        if not isinstance(content, list):
            return []
        tools = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("tool_use", "toolCall"):
                tools.append(
                    {
                        "toolCallId": block.get("id") or block.get("toolCallId", ""),
                        "name": block.get("name", ""),
                        "args": block.get("input")
                        or block.get("args")
                        or block.get("arguments"),
                    }
                )
        return tools

    # ------------------------------------------------------------------
    # Keepalive
    # ------------------------------------------------------------------

    async def _keepalive_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(30)
                msg = {
                    "type": "req",
                    "id": self._next_id(),
                    "method": "sessions.list",
                    "params": {},
                }
                self._log.debug("Sending keepalive")
                await ws.send(json.dumps(msg))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.debug("Keepalive loop ended: %s", exc)

    # ------------------------------------------------------------------
    # Async implementations of public methods
    # ------------------------------------------------------------------

    async def _async_send_message(self, text, attachments=None):
        loop = asyncio.get_running_loop()
        ws = self._ws
        if ws is None:
            raise OpenClawError("WebSocket not available")
        idempotency_key = str(uuid.uuid4())
        params = {
            "message": text,
            "sessionKey": self._session_key,
            "deliver": False,
            "idempotencyKey": idempotency_key,
        }
        if attachments:
            params["attachments"] = attachments
        msg = {
            "type": "req",
            "id": self._next_id(),
            "method": "chat.send",
            "params": params,
        }
        # Reset response state
        self._stream_buf = ""
        self._tool_calls = []
        self._response_future = loop.create_future()

        truncated = text[:200] + ("..." if len(text) > 200 else "")
        self._log.info("Sending message: %s (attachments=%d)", truncated, len(attachments or []))
        await ws.send(json.dumps(msg))

    async def _async_wait_for_response(self, timeout):
        if not self._response_future:
            raise OpenClawError("No pending message — call send_message first")
        try:
            return await asyncio.wait_for(self._response_future, timeout=timeout)
        except asyncio.TimeoutError:
            raise OpenClawTimeoutError(f"Response not received within {timeout}s")

    async def _async_fetch_history(self, limit):
        loop = asyncio.get_running_loop()
        ws = self._ws
        if ws is None:
            raise OpenClawError("WebSocket not available")
        msg_id = self._next_id()
        msg = {
            "type": "req",
            "id": msg_id,
            "method": "chat.history",
            "params": {
                "sessionKey": self._session_key,
                "limit": limit,
            },
        }
        future = loop.create_future()
        self._pending_rpcs[msg_id] = future

        self._log.info("Fetching chat history (limit=%d)", limit)
        await ws.send(json.dumps(msg))

        try:
            frame = await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self._pending_rpcs.pop(msg_id, None)
            raise OpenClawTimeoutError("chat.history RPC timed out")

        result = frame.get("result", {})
        if isinstance(result, dict) and "messages" in result:
            return result["messages"]
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_frame(raw):
        """Parse a JSON frame, returning None for unparseable data."""
        if not isinstance(raw, str):
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
