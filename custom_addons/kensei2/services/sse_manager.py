"""
SSE Connection Manager for Kensei2.

Manages server-side OpenClaw WebSocket connections per sandbox and distributes
incoming events to SSE consumers via thread-safe queues.

Each sandbox gets one persistent WebSocket connection to the OpenClaw gateway.
Multiple SSE consumers (browser tabs) can subscribe to the same sandbox.
"""

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import websockets

logger = logging.getLogger("kensei2.sse_manager")

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SSEEvent:
    """A single event to be pushed to SSE consumers."""
    event: str          # SSE event name: "chat", "connected", "error", "heartbeat"
    data: str           # JSON string payload


@dataclass
class _SandboxConnection:
    """Internal state for a single sandbox's OpenClaw connection."""
    sandbox_id: int
    ws_url: str
    gateway_token: str
    # Threading / asyncio
    thread: Optional[threading.Thread] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    ws: Optional[object] = None  # websockets connection
    stop_event: Optional[asyncio.Event] = None
    connected: threading.Event = field(default_factory=threading.Event)
    # Subscribers: list of queue.Queue for SSE consumers
    subscribers: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # WS state
    msg_counter: int = 0
    pending_rpcs: dict = field(default_factory=dict)
    # Response tracking for send
    _pending_send_futures: dict = field(default_factory=dict)


class SSEConnectionManager:
    """
    Singleton-style manager for SSE connections.

    Usage:
        manager = SSEConnectionManager.instance()
        q = manager.subscribe(sandbox_id, ws_url, token)
        # ... yield from q in SSE endpoint ...
        manager.unsubscribe(sandbox_id, q)
    """

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._connections: dict[int, _SandboxConnection] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, sandbox_id: int, ws_url: str, gateway_token: str) -> queue.Queue:
        """
        Subscribe to events for a sandbox. Creates the WS connection if needed.
        Returns a Queue that will receive SSEEvent objects.
        """
        sandbox_id = int(sandbox_id)
        with self._lock:
            conn = self._connections.get(sandbox_id)
            if conn is None:
                conn = _SandboxConnection(
                    sandbox_id=sandbox_id,
                    ws_url=ws_url,
                    gateway_token=gateway_token,
                )
                self._connections[sandbox_id] = conn
                self._start_connection(conn)
            elif not conn.connected.is_set():
                # Connection exists but died — restart
                conn.ws_url = ws_url
                conn.gateway_token = gateway_token
                self._start_connection(conn)

        q = queue.Queue(maxsize=500)
        with conn._lock:
            conn.subscribers.append(q)

        # If already connected, send immediate connected event
        if conn.connected.is_set():
            q.put(SSEEvent(event="connected", data=json.dumps({"status": "connected"})))

        return q

    def unsubscribe(self, sandbox_id: int, q: queue.Queue):
        """Remove a subscriber queue. If no more subscribers, close the WS."""
        sandbox_id = int(sandbox_id)
        with self._lock:
            conn = self._connections.get(sandbox_id)
            if conn is None:
                return
            with conn._lock:
                try:
                    conn.subscribers.remove(q)
                except ValueError:
                    pass
                remaining = len(conn.subscribers)

            if remaining == 0:
                logger.info("No more SSE subscribers for sandbox %s — closing WS", sandbox_id)
                self._stop_connection(conn)
                self._connections.pop(sandbox_id, None)

    def send_message(self, sandbox_id: int, text: str, attachments=None, timeout=10):
        """Send a chat.send message through the server-side WS connection."""
        conn = self._get_connected(sandbox_id)
        loop = conn.loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Event loop not available")

        fut = asyncio.run_coroutine_threadsafe(
            self._async_send_message(conn, text, attachments), loop
        )
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            logger.error("send_message failed for sandbox %s: %s", sandbox_id, e)
            raise

    def abort(self, sandbox_id: int, timeout=5):
        """Send chat.abort through the server-side WS connection."""
        conn = self._get_connected(sandbox_id)
        loop = conn.loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Event loop not available")

        fut = asyncio.run_coroutine_threadsafe(
            self._async_abort(conn), loop
        )
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            logger.error("abort failed for sandbox %s: %s", sandbox_id, e)
            raise

    def fetch_history(self, sandbox_id: int, limit=200, timeout=30):
        """Send chat.history RPC and return messages."""
        conn = self._get_connected(sandbox_id)
        loop = conn.loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Event loop not available")

        fut = asyncio.run_coroutine_threadsafe(
            self._async_fetch_history(conn, limit), loop
        )
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            logger.error("fetch_history failed for sandbox %s: %s", sandbox_id, e)
            raise

    def is_connected(self, sandbox_id: int) -> bool:
        conn = self._connections.get(int(sandbox_id))
        return conn is not None and conn.connected.is_set()

    # ------------------------------------------------------------------
    # Internal: connection lifecycle
    # ------------------------------------------------------------------

    def _get_connected(self, sandbox_id: int) -> _SandboxConnection:
        sandbox_id = int(sandbox_id)
        conn = self._connections.get(sandbox_id)
        if conn is None or not conn.connected.is_set():
            raise RuntimeError(f"Not connected to sandbox {sandbox_id}")
        return conn

    def _start_connection(self, conn: _SandboxConnection):
        """Start the WS connection in a daemon thread."""
        if conn.thread is not None and conn.thread.is_alive():
            return
        conn.connected.clear()
        conn.thread = threading.Thread(
            target=self._run_loop, args=(conn,),
            daemon=True, name=f"sse-ws-{conn.sandbox_id}"
        )
        conn.thread.start()

    def _stop_connection(self, conn: _SandboxConnection):
        """Signal the WS connection to stop."""
        if conn.loop and not conn.loop.is_closed() and conn.stop_event:
            conn.loop.call_soon_threadsafe(conn.stop_event.set)
        if conn.ws and conn.loop and not conn.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._close_ws(conn), conn.loop)
        if conn.thread and conn.thread.is_alive():
            conn.thread.join(timeout=5)
        conn.connected.clear()
        conn.ws = None
        conn.loop = None
        conn.thread = None

    # ------------------------------------------------------------------
    # Internal: event-loop thread
    # ------------------------------------------------------------------

    def _run_loop(self, conn: _SandboxConnection):
        try:
            asyncio.run(self._run(conn))
        except Exception as exc:
            logger.error("SSE WS loop crashed for sandbox %s: %s", conn.sandbox_id, exc)
        finally:
            conn.connected.clear()
            self._broadcast(conn, SSEEvent(
                event="error",
                data=json.dumps({"error": "Connection closed"})
            ))

    async def _run(self, conn: _SandboxConnection):
        conn.loop = asyncio.get_running_loop()
        conn.stop_event = asyncio.Event()

        try:
            async with websockets.connect(
                conn.ws_url,
                max_size=1650 * 1024 * 1024,
                ping_interval=None,
                close_timeout=5,
            ) as ws:
                conn.ws = ws
                await self._handshake(conn, ws)
                await asyncio.gather(
                    self._recv_loop(conn, ws),
                    self._keepalive_loop(conn, ws),
                    self._wait_for_stop(conn),
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("SSE WS connection error for sandbox %s: %s", conn.sandbox_id, exc)
            if not conn.connected.is_set():
                conn.connected.set()  # Unblock waiting subscribers

    async def _wait_for_stop(self, conn: _SandboxConnection):
        if conn.stop_event:
            await conn.stop_event.wait()
        raise asyncio.CancelledError()

    async def _close_ws(self, conn: _SandboxConnection):
        if conn.ws:
            try:
                await conn.ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Handshake (mirrors ws_client.py)
    # ------------------------------------------------------------------

    async def _handshake(self, conn: _SandboxConnection, ws):
        """Wait for connect.challenge, send auth, wait for success."""
        # Step 1: wait for connect.challenge
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for connect.challenge")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError("Timed out waiting for connect.challenge")

            frame = self._parse_frame(raw)
            if frame is None:
                continue
            if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
                logger.debug("SSE sandbox %s: received connect.challenge", conn.sandbox_id)
                break

        # Step 2: send connect request
        connect_msg = {
            "type": "req",
            "id": self._next_id(conn),
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
                    "operator.admin", "operator.read", "operator.write",
                    "operator.approvals", "operator.pairing",
                ],
                "caps": ["tool-events", "thinking-events"],
                "auth": {"token": conn.gateway_token},
                "userAgent": "kensei2-sse-proxy/1.0",
                "locale": "en-US",
            },
        }
        await ws.send(json.dumps(connect_msg))

        # Step 3: wait for connect response
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for connect response")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError("Timed out waiting for connect response")

            frame = self._parse_frame(raw)
            if frame is None:
                continue
            if frame.get("type") == "res":
                if frame.get("ok"):
                    logger.info("SSE sandbox %s: connected OK", conn.sandbox_id)
                    conn.connected.set()
                    self._broadcast(conn, SSEEvent(
                        event="connected",
                        data=json.dumps({"status": "connected"})
                    ))
                    return
                else:
                    err = frame.get("error", {})
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"Auth failed: {msg}")

    # ------------------------------------------------------------------
    # Recv loop
    # ------------------------------------------------------------------

    async def _recv_loop(self, conn: _SandboxConnection, ws):
        try:
            async for raw in ws:
                self._handle_raw(conn, raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("SSE recv loop error for sandbox %s: %s", conn.sandbox_id, exc)
            conn.connected.clear()

    def _handle_raw(self, conn: _SandboxConnection, raw):
        """Process a single incoming WS frame and broadcast to SSE subscribers."""
        # Drop heartbeat text frames
        if isinstance(raw, str) and raw.strip() in ("HEARTBEAT_OK", "HEARTBEAT", "PONG"):
            return

        frame = self._parse_frame(raw)
        if frame is None:
            return

        frame_type = frame.get("type")
        event = frame.get("event")

        # Silently ignored events
        if frame_type == "event" and event in ("tick", "health", "presence", "heartbeat"):
            return

        # RPC responses — resolve pending futures
        if frame_type == "res":
            frame_id = frame.get("id")
            if frame_id and frame_id in conn.pending_rpcs:
                future = conn.pending_rpcs.pop(frame_id)
                if not future.done():
                    if frame.get("ok"):
                        future.set_result(frame)
                    else:
                        future.set_exception(RuntimeError(
                            frame.get("error", {}).get("message", "RPC failed")
                            if isinstance(frame.get("error"), dict)
                            else str(frame.get("error", "RPC failed"))
                        ))
                return

        # Chat events — broadcast to all SSE subscribers
        if frame_type == "event" and event == "chat":
            payload = frame.get("payload", {})
            self._broadcast(conn, SSEEvent(
                event="chat",
                data=json.dumps(payload, default=str)
            ))
            return

        # Tool/session.tool events — transform and broadcast as chat tool events
        if frame_type == "event" and event in ("session.tool", "tool"):
            tool_payload = frame.get("payload", {})
            self._broadcast(conn, SSEEvent(
                event="chat",
                data=json.dumps({
                    "stream": "tool",
                    "message": tool_payload,
                    "data": tool_payload,
                    **tool_payload,
                }, default=str)
            ))
            return

        # Agent events — transform and broadcast
        if frame_type == "event" and event == "agent":
            payload = frame.get("payload", {})
            agent_stream = payload.get("stream")
            agent_data = payload.get("data", {})

            if agent_stream == "tool":
                phase = "end" if agent_data.get("phase") == "result" else agent_data.get("phase")
                self._broadcast(conn, SSEEvent(
                    event="chat",
                    data=json.dumps({
                        "stream": "tool",
                        "message": {
                            "phase": phase,
                            "toolCallId": agent_data.get("toolCallId", ""),
                            "name": agent_data.get("name", ""),
                            "args": agent_data.get("args"),
                            "result": agent_data.get("result") or agent_data.get("meta"),
                            "isError": bool(agent_data.get("isError")),
                        },
                        "data": {
                            "phase": phase,
                            "toolCallId": agent_data.get("toolCallId", ""),
                            "name": agent_data.get("name", ""),
                            "args": agent_data.get("args"),
                            "result": agent_data.get("result") or agent_data.get("meta"),
                            "isError": bool(agent_data.get("isError")),
                        },
                    }, default=str)
                ))
                return

            if agent_stream == "item":
                phase = agent_data.get("phase")
                kind = agent_data.get("kind")
                if kind == "tool" and agent_data.get("toolCallId"):
                    if phase == "start":
                        self._broadcast(conn, SSEEvent(
                            event="chat",
                            data=json.dumps({
                                "stream": "tool",
                                "message": {
                                    "phase": "start",
                                    "toolCallId": agent_data.get("toolCallId", ""),
                                    "name": agent_data.get("name", ""),
                                    "args": None,
                                    "result": None,
                                    "isError": False,
                                },
                                "data": {
                                    "phase": "start",
                                    "toolCallId": agent_data.get("toolCallId", ""),
                                    "name": agent_data.get("name", ""),
                                    "args": None,
                                    "result": None,
                                    "isError": False,
                                },
                            }, default=str)
                        ))
                    elif phase == "end":
                        self._broadcast(conn, SSEEvent(
                            event="chat",
                            data=json.dumps({
                                "stream": "tool",
                                "source_event": "agent.item",
                                "message": {
                                    "phase": "end",
                                    "toolCallId": agent_data.get("toolCallId", ""),
                                    "name": agent_data.get("name", ""),
                                    "result": agent_data.get("meta") or agent_data.get("title") or "(completed)",
                                    "isError": agent_data.get("status") == "error",
                                },
                                "data": {
                                    "phase": "end",
                                    "toolCallId": agent_data.get("toolCallId", ""),
                                    "name": agent_data.get("name", ""),
                                    "result": agent_data.get("meta") or agent_data.get("title") or "(completed)",
                                    "isError": agent_data.get("status") == "error",
                                },
                            }, default=str)
                        ))
                return

            if agent_stream in ("assistant", "lifecycle", "thinking"):
                self._broadcast(conn, SSEEvent(
                    event="chat",
                    data=json.dumps({
                        "stream": agent_stream,
                        "message": agent_data,
                        "data": agent_data,
                    }, default=str)
                ))
                return

            # Other agent events — log only
            logger.debug("SSE sandbox %s: unhandled agent stream %s", conn.sandbox_id, agent_stream)
            return

        if frame_type == "event":
            logger.debug("SSE sandbox %s: unhandled event %s", conn.sandbox_id, event)

    # ------------------------------------------------------------------
    # Keepalive
    # ------------------------------------------------------------------

    async def _keepalive_loop(self, conn: _SandboxConnection, ws):
        try:
            while True:
                await asyncio.sleep(30)
                msg = {
                    "type": "req",
                    "id": self._next_id(conn),
                    "method": "sessions.list",
                    "params": {},
                }
                await ws.send(json.dumps(msg))
                # Also send SSE heartbeat to keep the HTTP connection alive
                self._broadcast(conn, SSEEvent(
                    event="heartbeat",
                    data=json.dumps({"ts": int(time.time())})
                ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("SSE keepalive ended for sandbox %s: %s", conn.sandbox_id, exc)

    # ------------------------------------------------------------------
    # Async operations
    # ------------------------------------------------------------------

    async def _async_send_message(self, conn: _SandboxConnection, text, attachments=None):
        ws = conn.ws
        if ws is None:
            raise RuntimeError("WebSocket not available")

        idempotency_key = str(uuid.uuid4())
        session_key = f"odoo:sandbox:{conn.sandbox_id}"
        params = {
            "message": text,
            "sessionKey": session_key,
            "deliver": False,
            "idempotencyKey": idempotency_key,
        }
        if attachments:
            params["attachments"] = attachments
        msg = {
            "type": "req",
            "id": self._next_id(conn),
            "method": "chat.send",
            "params": params,
        }
        await ws.send(json.dumps(msg))
        logger.info("SSE sandbox %s: sent chat.send (text=%d chars, attachments=%d)",
                     conn.sandbox_id, len(text), len(attachments or []))
        return {"ok": True, "idempotencyKey": idempotency_key}

    async def _async_abort(self, conn: _SandboxConnection):
        ws = conn.ws
        if ws is None:
            raise RuntimeError("WebSocket not available")

        session_key = f"odoo:sandbox:{conn.sandbox_id}"
        msg = {
            "type": "req",
            "id": self._next_id(conn),
            "method": "chat.abort",
            "params": {"sessionKey": session_key},
        }
        await ws.send(json.dumps(msg))
        logger.info("SSE sandbox %s: sent chat.abort", conn.sandbox_id)
        return {"ok": True}

    async def _async_fetch_history(self, conn: _SandboxConnection, limit=200):
        ws = conn.ws
        if ws is None:
            raise RuntimeError("WebSocket not available")

        loop = asyncio.get_running_loop()
        session_key = f"odoo:sandbox:{conn.sandbox_id}"
        msg_id = self._next_id(conn)
        msg = {
            "type": "req",
            "id": msg_id,
            "method": "chat.history",
            "params": {
                "sessionKey": session_key,
                "limit": limit,
            },
        }
        future = loop.create_future()
        conn.pending_rpcs[msg_id] = future
        await ws.send(json.dumps(msg))

        try:
            frame = await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            conn.pending_rpcs.pop(msg_id, None)
            raise TimeoutError("chat.history RPC timed out")

        result = frame.get("result", {})
        if isinstance(result, dict) and "messages" in result:
            return result["messages"]
        return result

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    def _broadcast(self, conn: _SandboxConnection, event: SSEEvent):
        """Push an SSE event to all subscriber queues."""
        with conn._lock:
            dead = []
            for q in conn.subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    conn.subscribers.remove(q)
                except ValueError:
                    pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_id(self, conn: _SandboxConnection):
        conn.msg_counter += 1
        ts = int(time.time())
        return f"sse-{conn.sandbox_id}-{conn.msg_counter}-{ts:x}"

    @staticmethod
    def _parse_frame(raw):
        if not isinstance(raw, str):
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
