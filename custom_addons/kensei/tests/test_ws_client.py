# -*- coding: utf-8 -*-
import json
import threading
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..ws_client import (
    OpenClawClient,
    OpenClawResponse,
    OpenClawError,
    OpenClawAuthError,
    OpenClawTimeoutError,
)


def _make_client(**kw):
    defaults = {
        "ws_url": "wss://test.example.com/ws",
        "gateway_token": "tok_test_123",
        "sandbox_id": 42,
    }
    defaults.update(kw)
    return OpenClawClient(**defaults)


@tagged("post_install", "-at_install")
class TestOpenClawClientStatic(TransactionCase):

    def test_extract_text_plain_string(self):
        self.assertEqual(OpenClawClient._extract_text("hello"), "hello")

    def test_extract_text_dict_with_text_key(self):
        self.assertEqual(OpenClawClient._extract_text({"text": "hi"}), "hi")

    def test_extract_text_dict_with_content_string(self):
        self.assertEqual(OpenClawClient._extract_text({"content": "hi"}), "hi")

    def test_extract_text_content_blocks(self):
        msg = {"content": [{"type": "text", "text": "hi"}]}
        self.assertEqual(OpenClawClient._extract_text(msg), "hi")

    def test_extract_text_multiple_blocks(self):
        msg = {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " World"},
            ]
        }
        self.assertEqual(OpenClawClient._extract_text(msg), "Hello World")

    def test_extract_text_none(self):
        self.assertEqual(OpenClawClient._extract_text(None), "")

    def test_extract_text_nested_role_content(self):
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
        }
        self.assertEqual(OpenClawClient._extract_text(msg), "ok")

    def test_extract_tool_calls_tool_use(self):
        msg = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "bash",
                    "input": {"cmd": "ls"},
                }
            ]
        }
        result = OpenClawClient._extract_tool_calls(msg)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["toolCallId"], "tu_1")
        self.assertEqual(result[0]["name"], "bash")
        self.assertEqual(result[0]["args"], {"cmd": "ls"})

    def test_extract_tool_calls_toolCall(self):
        msg = {
            "content": [
                {
                    "type": "toolCall",
                    "toolCallId": "tc_99",
                    "name": "read_file",
                    "args": {"path": "/tmp"},
                }
            ]
        }
        result = OpenClawClient._extract_tool_calls(msg)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["toolCallId"], "tc_99")
        self.assertEqual(result[0]["name"], "read_file")
        self.assertEqual(result[0]["args"], {"path": "/tmp"})

    def test_extract_tool_calls_empty(self):
        msg = {"content": [{"type": "text", "text": "no tools here"}]}
        self.assertEqual(OpenClawClient._extract_tool_calls(msg), [])

    def test_extract_tool_calls_none_message(self):
        self.assertEqual(OpenClawClient._extract_tool_calls(None), [])

    def test_parse_frame_valid_json(self):
        result = OpenClawClient._parse_frame('{"type":"event"}')
        self.assertEqual(result, {"type": "event"})

    def test_parse_frame_invalid_json(self):
        self.assertIsNone(OpenClawClient._parse_frame("not json"))

    def test_parse_frame_non_string(self):
        self.assertIsNone(OpenClawClient._parse_frame(b"\x00\x01"))


@tagged("post_install", "-at_install")
class TestOpenClawClientInit(TransactionCase):

    def test_init_stores_params(self):
        c = _make_client()
        self.assertEqual(c._ws_url, "wss://test.example.com/ws")
        self.assertEqual(c._gateway_token, "tok_test_123")
        self.assertEqual(c._sandbox_id, 42)
        self.assertEqual(c._msg_counter, 0)

    def test_session_key(self):
        c = _make_client(sandbox_id=7)
        self.assertEqual(c._session_key, "odoo:sandbox:7")

    def test_next_id_format(self):
        c = _make_client()
        nid = c._next_id()
        parts = nid.split("-")
        self.assertEqual(parts[0], "kensei")
        self.assertEqual(parts[1], "1")
        int(parts[2], 16)

    def test_next_id_increments(self):
        c = _make_client()
        id1 = c._next_id()
        id2 = c._next_id()
        counter1 = int(id1.split("-")[1])
        counter2 = int(id2.split("-")[1])
        self.assertEqual(counter2, counter1 + 1)


@tagged("post_install", "-at_install")
class TestOpenClawClientConnection(TransactionCase):

    def test_connect_already_connected_raises(self):
        c = _make_client()
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True
        c._thread = mock_thread

        with self.assertRaises(OpenClawError):
            c.connect()

    def test_connect_timeout_raises(self):
        c = _make_client()
        with patch("odoo.addons.kensei.ws_client.threading.Thread") as MockThread:
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            MockThread.return_value = mock_thread
            with patch.object(c._connected, "wait", return_value=False):
                with self.assertRaises(OpenClawTimeoutError):
                    c.connect(timeout=1)

    def test_send_message_not_connected_raises(self):
        c = _make_client()
        with self.assertRaises(OpenClawError):
            c.send_message("hello")

    def test_wait_for_response_not_connected_raises(self):
        c = _make_client()
        with self.assertRaises(OpenClawError):
            c.wait_for_response()

    def test_fetch_history_not_connected_raises(self):
        c = _make_client()
        with self.assertRaises(OpenClawError):
            c.fetch_history()

    def test_disconnect_clears_state(self):
        c = _make_client()
        c._ws = MagicMock()
        c._thread = MagicMock()
        c._thread.is_alive.return_value = False
        c._loop = None
        c._connected.set()

        c.disconnect()

        self.assertIsNone(c._ws)
        self.assertIsNone(c._thread)
        self.assertIsNone(c._loop)
        self.assertFalse(c._connected.is_set())


@tagged("post_install", "-at_install")
class TestOpenClawClientMessageHandling(TransactionCase):

    def test_handle_raw_heartbeat_ignored(self):
        c = _make_client()
        for hb in ("HEARTBEAT_OK", "HEARTBEAT", "PONG"):
            c._stream_buf = "original"
            c._handle_raw(hb)
            self.assertEqual(c._stream_buf, "original")

    def test_handle_raw_unparseable_ignored(self):
        c = _make_client()
        c._stream_buf = "original"
        c._handle_raw("%%%garbage%%%")
        self.assertEqual(c._stream_buf, "original")

    def test_complete_response_resolves_future(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            c._response_future = loop.create_future()

            tools = [{"toolCallId": "t1", "name": "bash", "args": {}}]
            c._complete_response("done", tools)

            self.assertTrue(c._response_future.done())
            resp = c._response_future.result()
            self.assertIsInstance(resp, OpenClawResponse)
            self.assertEqual(resp.text, "done")
            self.assertEqual(json.loads(resp.tool_calls_json), tools)
        finally:
            loop.close()

    def test_complete_response_resets_buffers(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            c._response_future = loop.create_future()
            c._stream_buf = "buffered text"
            c._tool_calls = [{"name": "x"}]

            c._complete_response("final", [])

            self.assertEqual(c._stream_buf, "")
            self.assertEqual(c._tool_calls, [])
        finally:
            loop.close()


@tagged("post_install", "-at_install")
class TestOpenClawResponse(TransactionCase):

    def test_dataclass_fields(self):
        r = OpenClawResponse(text="hi", tool_calls_json='[{"name":"x"}]')
        self.assertEqual(r.text, "hi")
        self.assertEqual(r.tool_calls_json, '[{"name":"x"}]')

    def test_default_values(self):
        r = OpenClawResponse(text="", tool_calls_json="")
        self.assertEqual(r.text, "")
        self.assertEqual(r.tool_calls_json, "")


@tagged("post_install", "-at_install")
class TestHandleRaw(TransactionCase):

    def test_handle_raw_tick_event_ignored(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_raw(json.dumps({"type": "event", "event": "tick"}))
        self.assertEqual(c._stream_buf, "keep")

    def test_handle_raw_health_event_ignored(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_raw(json.dumps({"type": "event", "event": "health"}))
        self.assertEqual(c._stream_buf, "keep")

    def test_handle_raw_rpc_response_success(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            fut = loop.create_future()
            c._pending_rpcs["rpc-1"] = fut

            c._handle_raw(json.dumps({
                "type": "res", "id": "rpc-1", "ok": True, "result": {"data": 1},
            }))

            self.assertTrue(fut.done())
            self.assertEqual(fut.result()["ok"], True)
            self.assertNotIn("rpc-1", c._pending_rpcs)
        finally:
            loop.close()

    def test_handle_raw_rpc_response_error(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            fut = loop.create_future()
            c._pending_rpcs["rpc-2"] = fut

            c._handle_raw(json.dumps({
                "type": "res", "id": "rpc-2", "ok": False,
                "error": {"message": "bad request"},
            }))

            self.assertTrue(fut.done())
            with self.assertRaises(OpenClawError):
                fut.result()
            self.assertNotIn("rpc-2", c._pending_rpcs)
        finally:
            loop.close()

    def test_handle_raw_rpc_unknown_id_ignored(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_raw(json.dumps({
            "type": "res", "id": "unknown-999", "ok": True, "result": {},
        }))
        self.assertEqual(c._stream_buf, "keep")

    def test_handle_raw_chat_event_dispatches(self):
        c = _make_client()
        called_with = []
        original = c._handle_chat_event
        c._handle_chat_event = lambda payload: called_with.append(payload)

        c._handle_raw(json.dumps({
            "type": "event", "event": "chat",
            "payload": {"stream": "assistant", "data": {"text": "hi"}},
        }))

        self.assertEqual(len(called_with), 1)
        self.assertEqual(called_with[0]["stream"], "assistant")

    def test_handle_raw_tool_event_logged(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_raw(json.dumps({
            "type": "event", "event": "session.tool",
        }))
        self.assertEqual(c._stream_buf, "keep")

    def test_handle_raw_agent_event_logged(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_raw(json.dumps({
            "type": "event", "event": "agent",
        }))
        self.assertEqual(c._stream_buf, "keep")


@tagged("post_install", "-at_install")
class TestHandleChatEvent(TransactionCase):

    def test_chat_stream_assistant_accumulates(self):
        c = _make_client()
        c._stream_buf = ""
        c._handle_chat_event({
            "stream": "assistant",
            "data": {"text": "Hello"},
        })
        self.assertEqual(c._stream_buf, "Hello")

    def test_chat_heartbeat_in_data_ignored(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_chat_event({
            "stream": "assistant",
            "data": {"text": "HEARTBEAT_OK"},
        })
        self.assertEqual(c._stream_buf, "keep")

    def test_chat_tool_stream_logged(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_chat_event({
            "stream": "tool",
            "data": {"phase": "running"},
        })
        self.assertEqual(c._stream_buf, "keep")

    def test_chat_lifecycle_start(self):
        c = _make_client()
        c._stream_buf = "keep"
        c._handle_chat_event({
            "stream": "lifecycle",
            "data": {"phase": "start"},
        })
        self.assertEqual(c._stream_buf, "keep")

    def test_chat_lifecycle_end_completes(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            c._response_future = loop.create_future()
            c._stream_buf = "accumulated text"
            c._tool_calls = []

            c._handle_chat_event({
                "stream": "lifecycle",
                "data": {"phase": "end"},
            })

            self.assertTrue(c._response_future.done())
            resp = c._response_future.result()
            self.assertIsInstance(resp, OpenClawResponse)
            self.assertEqual(resp.text, "accumulated text")
        finally:
            loop.close()

    def test_chat_lifecycle_error_sets_exception(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            c._response_future = loop.create_future()

            c._handle_chat_event({
                "stream": "lifecycle",
                "data": {"phase": "error", "message": "something broke"},
            })

            self.assertTrue(c._response_future.done())
            with self.assertRaises(OpenClawError):
                c._response_future.result()
        finally:
            loop.close()

    def test_chat_state_final(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            c._response_future = loop.create_future()
            c._stream_buf = ""

            c._handle_chat_event({
                "state": "final",
                "message": {"text": "Final answer"},
            })

            self.assertTrue(c._response_future.done())
            resp = c._response_future.result()
            self.assertIsInstance(resp, OpenClawResponse)
            self.assertEqual(resp.text, "Final answer")
        finally:
            loop.close()

    def test_chat_state_error(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            c._response_future = loop.create_future()

            c._handle_chat_event({
                "state": "error",
                "errorMessage": "model overloaded",
            })

            self.assertTrue(c._response_future.done())
            with self.assertRaises(OpenClawError):
                c._response_future.result()
        finally:
            loop.close()

    def test_chat_state_aborted(self):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            c = _make_client()
            c._response_future = loop.create_future()
            c._stream_buf = ""

            c._handle_chat_event({"state": "aborted"})

            self.assertTrue(c._response_future.done())
            resp = c._response_future.result()
            self.assertIsInstance(resp, OpenClawResponse)
            self.assertEqual(resp.text, "[Aborted]")
        finally:
            loop.close()


@tagged("post_install", "-at_install")
class TestCompleteResponse(TransactionCase):

    def test_complete_response_no_future_no_crash(self):
        c = _make_client()
        c._response_future = None
        c._stream_buf = "buf"
        c._tool_calls = [{"name": "y"}]

        c._complete_response("text", [])

        self.assertEqual(c._stream_buf, "")
        self.assertEqual(c._tool_calls, [])


@tagged("post_install", "-at_install")
class TestConnectionLifecycle(TransactionCase):

    def test_require_loop_not_connected(self):
        c = _make_client()
        c._loop = None
        with self.assertRaises(OpenClawError):
            c._require_loop()

    def test_disconnect_idempotent(self):
        c = _make_client()
        c._ws = None
        c._loop = None
        c._thread = None
        c._connected.clear()

        c.disconnect()

        self.assertIsNone(c._ws)
        self.assertIsNone(c._loop)
        self.assertIsNone(c._thread)
        self.assertFalse(c._connected.is_set())
