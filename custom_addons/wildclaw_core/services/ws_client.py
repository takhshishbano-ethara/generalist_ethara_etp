import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Optional

_logger = logging.getLogger(__name__)


class OpenClawError(Exception):
    pass


class OpenClawAuthError(OpenClawError):
    pass


class OpenClawTimeoutError(OpenClawError):
    pass


@dataclass
class OpenClawResponse:
    text: str
    tool_calls_json: Optional[str] = None


class OpenClawClient:

    def __init__(self, ws_url: str, gateway_token: str, sandbox_id: Optional[int] = None):
        self.ws_url = ws_url
        self.gateway_token = gateway_token
        self.sandbox_id = sandbox_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    def connect(self, timeout: int = 30) -> None:
        try:
            import websockets  # noqa: F401
        except ImportError:
            raise RuntimeError("websockets package required; pip install websockets")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._async_connect(), self._loop)
        fut.result(timeout=timeout)

    async def _async_connect(self):
        import websockets
        headers = {"Authorization": f"Bearer {self.gateway_token}"} if self.gateway_token else {}
        self._ws = await websockets.connect(self.ws_url, extra_headers=headers, max_size=None)

    def send_message(self, content: str, timeout: int = 600) -> OpenClawResponse:
        if not self._loop or not self._ws:
            raise OpenClawError("not connected")
        fut = asyncio.run_coroutine_threadsafe(self._send_and_wait(content), self._loop)
        try:
            return fut.result(timeout=timeout)
        except asyncio.TimeoutError:
            raise OpenClawTimeoutError(f"WS response timeout after {timeout}s")

    async def _send_and_wait(self, content: str) -> OpenClawResponse:
        await self._ws.send(json.dumps({"type": "message", "content": content}))
        text_buf = []
        while True:
            raw = await self._ws.recv()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("type") == "auth_error":
                raise OpenClawAuthError(data.get("message", "auth failed"))
            if data.get("type") == "text":
                text_buf.append(data.get("content", ""))
            if data.get("type") in ("done", "final"):
                break
        return OpenClawResponse(text="".join(text_buf))

    def disconnect(self) -> None:
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop).result(timeout=5)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
