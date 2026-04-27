# -*- coding: utf-8 -*-
import json
import time
from unittest.mock import patch, MagicMock, call

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestConsumerOdooHelpers(TransactionCase):

    @patch("odoo.addons.talos.consumer.xmlrpc.client.ServerProxy")
    def test_get_odoo_uid_success(self, mock_proxy_cls):
        from ..consumer import _get_odoo_uid
        import odoo.addons.talos.consumer as consumer_mod
        consumer_mod._cached_uid = None

        mock_common = MagicMock()
        mock_common.authenticate.return_value = 42
        mock_proxy_cls.return_value = mock_common

        uid = _get_odoo_uid()
        self.assertEqual(uid, 42)
        consumer_mod._cached_uid = None

    @patch("odoo.addons.talos.consumer.xmlrpc.client.ServerProxy")
    def test_get_odoo_uid_failure_raises(self, mock_proxy_cls):
        from ..consumer import _get_odoo_uid
        import odoo.addons.talos.consumer as consumer_mod
        consumer_mod._cached_uid = None

        mock_common = MagicMock()
        mock_common.authenticate.return_value = False
        mock_proxy_cls.return_value = mock_common

        with self.assertRaises(RuntimeError):
            _get_odoo_uid()
        consumer_mod._cached_uid = None

    @patch("odoo.addons.talos.consumer._get_odoo_uid", return_value=1)
    @patch("odoo.addons.talos.consumer._get_odoo_models")
    def test_call_odoo_delegates_to_xmlrpc(self, mock_models, mock_uid):
        from ..consumer import _call_odoo
        mock_proxy = MagicMock()
        mock_models.return_value = mock_proxy
        mock_proxy.execute_kw.return_value = {"result": True}

        result = _call_odoo("talos.talos", "auto_process_claim_task", [99])

        mock_proxy.execute_kw.assert_called_once()
        args = mock_proxy.execute_kw.call_args[0]
        self.assertEqual(args[3], "auto_process_claim_task")
        self.assertEqual(args[4], [[99]])

    @patch("odoo.addons.talos.consumer._get_odoo_uid", return_value=1)
    @patch("odoo.addons.talos.consumer._get_odoo_models")
    def test_read_fields_passes_correct_params(self, mock_models, mock_uid):
        from ..consumer import _read_fields
        mock_proxy = MagicMock()
        mock_models.return_value = mock_proxy
        mock_proxy.execute_kw.return_value = [{"id": 1, "name": "test"}]

        result = _read_fields("talos.sandbox", [1], ["docker_status", "docker_error"])

        mock_proxy.execute_kw.assert_called_once()
        kwargs = mock_proxy.execute_kw.call_args[0][5]
        self.assertEqual(kwargs["fields"], ["docker_status", "docker_error"])

    def test_ack_message_open_channel(self):
        from ..consumer import _ack_message
        mock_channel = MagicMock()
        mock_channel.is_open = True
        _ack_message(mock_channel, 42)
        mock_channel.basic_ack.assert_called_once_with(delivery_tag=42)

    def test_ack_message_closed_channel_noop(self):
        from ..consumer import _ack_message
        mock_channel = MagicMock()
        mock_channel.is_open = False
        _ack_message(mock_channel, 42)
        mock_channel.basic_ack.assert_not_called()


@tagged("post_install", "-at_install")
class TestWaitForSandboxRunning(TransactionCase):

    @patch("odoo.addons.talos.consumer._read_fields")
    def test_wait_already_running(self, mock_read):
        from ..consumer import _wait_for_sandbox_running
        mock_read.return_value = [{"docker_status": "running", "docker_error": ""}]

        status = _wait_for_sandbox_running(1, timeout=5)
        self.assertEqual(status, "running")

    @patch("odoo.addons.talos.consumer._read_fields")
    def test_wait_transitions_to_running(self, mock_read):
        from ..consumer import _wait_for_sandbox_running
        mock_read.side_effect = [
            [{"docker_status": "starting", "docker_error": ""}],
            [{"docker_status": "starting", "docker_error": ""}],
            [{"docker_status": "running", "docker_error": ""}],
        ]

        with patch("odoo.addons.talos.consumer.time.sleep"):
            status = _wait_for_sandbox_running(1, timeout=30)
        self.assertEqual(status, "running")

    @patch("odoo.addons.talos.consumer._read_fields")
    def test_wait_error_status_raises(self, mock_read):
        from ..consumer import _wait_for_sandbox_running
        mock_read.return_value = [{"docker_status": "error", "docker_error": "OOM killed"}]

        with self.assertRaises(RuntimeError) as ctx:
            _wait_for_sandbox_running(1, timeout=5)
        self.assertIn("OOM killed", str(ctx.exception))

    @patch("odoo.addons.talos.consumer._read_fields")
    def test_wait_not_found_raises(self, mock_read):
        from ..consumer import _wait_for_sandbox_running
        mock_read.return_value = []

        with self.assertRaises(RuntimeError):
            _wait_for_sandbox_running(1, timeout=5)

    @patch("odoo.addons.talos.consumer._read_fields")
    @patch("odoo.addons.talos.consumer.time")
    def test_wait_timeout_raises(self, mock_time, mock_read):
        from ..consumer import _wait_for_sandbox_running
        mock_read.return_value = [{"docker_status": "starting", "docker_error": ""}]
        mock_time.time.side_effect = [0, 0, 100, 200, 999]
        mock_time.sleep = MagicMock()

        with self.assertRaises(RuntimeError) as ctx:
            _wait_for_sandbox_running(1, timeout=5)
        self.assertIn("timed out", str(ctx.exception))


@tagged("post_install", "-at_install")
class TestProcessTask(TransactionCase):

    def _make_body(self, task_id=1):
        return json.dumps({"task_id": task_id, "action": "auto_process"}).encode()

    @patch("odoo.addons.talos.consumer._call_odoo")
    @patch("odoo.addons.talos.consumer._wait_for_sandbox_running", return_value="running")
    @patch("odoo.addons.talos.consumer._read_fields")
    def test_claim_skip_acks_message(self, mock_read, mock_wait, mock_call):
        from ..consumer import _process_task
        mock_call.return_value = {"skip": True, "reason": "already_claimed"}

        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_channel.is_open = True

        _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        mock_conn.add_callback_threadsafe.assert_called()

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_missing_task_id_acks_message(self, mock_call):
        from ..consumer import _process_task
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_channel.is_open = True

        _process_task(mock_conn, mock_channel, 1, None, b'{}')
        mock_conn.add_callback_threadsafe.assert_called()

    @patch("odoo.addons.talos.consumer._call_odoo")
    @patch("odoo.addons.talos.consumer._wait_for_sandbox_running", return_value="running")
    def test_exception_marks_failed_and_acks(self, mock_wait, mock_call):
        from ..consumer import _process_task

        def call_side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_claim_task":
                return {
                    "task_id": 1, "sandbox_id": 10,
                    "initial_prompt": "test", "docker_status": "running",
                }
            if method == "auto_process_get_ws_info":
                raise RuntimeError("boom")
            if method == "auto_process_mark_done":
                return True
            return {}

        mock_call.side_effect = call_side_effect
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_channel.is_open = True

        _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        mark_done_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "auto_process_mark_done"
        ]
        self.assertTrue(len(mark_done_calls) > 0)
        self.assertIn("failed", str(mark_done_calls[-1]))
        mock_conn.add_callback_threadsafe.assert_called()


@tagged("post_install", "-at_install")
class TestAutoHintLoop(TransactionCase):

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_satisfied_exits(self, mock_call):
        from ..consumer import _run_auto_hint_loop

        call_count = {"n": 0}

        def call_side_effect(model, method, ids, *a, **kw):
            call_count["n"] += 1
            if method == "auto_process_poll_hint_status":
                if call_count["n"] == 1:
                    return {"last_turn_id": 5, "auto_hint_status": "idle"}
                return {
                    "auto_hint_status": "idle",
                    "last_turn_feedback": "satisfied",
                    "last_turn_id": 5,
                }
            if method == "auto_process_trigger_hint_eval":
                return {"status": "pending"}
            return {}

        mock_call.side_effect = call_side_effect
        mock_ws = MagicMock()

        with patch("odoo.addons.talos.consumer.time.sleep"):
            _run_auto_hint_loop(10, mock_ws, 1)

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_max_retries_from_trigger(self, mock_call):
        from ..consumer import _run_auto_hint_loop

        def call_side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                return {"last_turn_id": 5, "auto_hint_status": "idle"}
            if method == "auto_process_trigger_hint_eval":
                return {"status": "max_retries"}
            return {}

        mock_call.side_effect = call_side_effect
        mock_ws = MagicMock()

        _run_auto_hint_loop(10, mock_ws, 1)

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_error_status_exits(self, mock_call):
        from ..consumer import _run_auto_hint_loop

        poll_count = {"n": 0}

        def call_side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                poll_count["n"] += 1
                if poll_count["n"] == 1:
                    return {"last_turn_id": 5, "auto_hint_status": "idle"}
                return {"auto_hint_status": "error", "last_turn_id": 5}
            if method == "auto_process_trigger_hint_eval":
                return {"status": "pending"}
            return {}

        mock_call.side_effect = call_side_effect
        mock_ws = MagicMock()

        with patch("odoo.addons.talos.consumer.time.sleep"):
            _run_auto_hint_loop(10, mock_ws, 1)

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_no_turns_exits(self, mock_call):
        from ..consumer import _run_auto_hint_loop

        def call_side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                return {"last_turn_id": 0, "auto_hint_status": "idle"}
            return {}

        mock_call.side_effect = call_side_effect
        mock_ws = MagicMock()

        _run_auto_hint_loop(10, mock_ws, 1)


@tagged("post_install", "-at_install")
class TestStartConsumer(TransactionCase):

    @patch("odoo.addons.talos.consumer._get_odoo_uid", return_value=1)
    @patch("odoo.addons.talos.consumer.pika")
    def test_start_consumer_connects_and_declares_queue(self, mock_pika, mock_uid):
        from ..consumer import start_consumer

        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_channel
        mock_channel.start_consuming.side_effect = KeyboardInterrupt

        try:
            start_consumer()
        except (KeyboardInterrupt, SystemExit):
            pass

        mock_channel.queue_declare.assert_called_once_with(
            queue="talos_auto_process", durable=True
        )

    @patch("odoo.addons.talos.consumer._get_odoo_uid", return_value=1)
    @patch("odoo.addons.talos.consumer.pika")
    def test_start_consumer_sets_prefetch(self, mock_pika, mock_uid):
        from ..consumer import start_consumer, WORKER_THREADS

        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_channel
        mock_channel.start_consuming.side_effect = KeyboardInterrupt

        try:
            start_consumer()
        except (KeyboardInterrupt, SystemExit):
            pass

        mock_channel.basic_qos.assert_called_once_with(prefetch_count=WORKER_THREADS)

    @patch("odoo.addons.talos.consumer._get_odoo_uid", return_value=1)
    @patch("odoo.addons.talos.consumer.pika")
    def test_start_consumer_registers_callback(self, mock_pika, mock_uid):
        from ..consumer import start_consumer

        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_channel
        mock_channel.start_consuming.side_effect = KeyboardInterrupt

        try:
            start_consumer()
        except (KeyboardInterrupt, SystemExit):
            pass

        mock_channel.basic_consume.assert_called_once()
        call_kwargs = mock_channel.basic_consume.call_args
        self.assertEqual(
            call_kwargs.kwargs.get("queue", call_kwargs[1].get("queue")),
            "talos_auto_process",
        )


# ═══════════════════════════════════════════════════════════════════════
# NEW: TestProcessTaskFlow — full _process_task orchestration paths
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestProcessTaskFlow(TransactionCase):
    """Extended tests for _process_task covering sandbox state, WS failures,
    turn creation errors, response timeouts, history failures, and cleanup."""

    def _make_body(self, task_id=1):
        return json.dumps({"task_id": task_id}).encode()

    def _base_claim(self, docker_status="running"):
        return {
            "task_id": 1,
            "sandbox_id": 10,
            "initial_prompt": "hello",
            "docker_status": docker_status,
        }

    # ── sandbox already running → skip start ──────────────────────

    @patch("odoo.addons.talos.consumer._run_auto_hint_loop")
    @patch("odoo.addons.talos.consumer._wait_for_sandbox_running")
    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_sandbox_already_running(
        self, mock_call, mock_wait, mock_hint
    ):
        """docker_status='running' → action_start_sandbox is NOT called."""
        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst
        mock_ws_inst.wait_for_response.return_value = MagicMock(
            text="resp", tool_calls_json="[]"
        )
        mock_ws_inst.fetch_history.return_value = []

        call_results = {
            "auto_process_claim_task": self._base_claim(docker_status="running"),
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_create_turn": {"turn_id": 100},
            "auto_process_save_response": True,
            "auto_process_mark_done": True,
        }

        def side_effect(model, method, ids, *a, **kw):
            return call_results.get(method, {})

        mock_call.side_effect = side_effect

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_channel.is_open = True
            from ..consumer import _process_task
            _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        # action_start_sandbox must NOT have been called
        start_calls = [
            c for c in mock_call.call_args_list
            if c[0][1] == "action_start_sandbox"
        ]
        self.assertEqual(len(start_calls), 0)
        mock_wait.assert_not_called()

    # ── sandbox stopped → start is invoked ────────────────────────

    @patch("odoo.addons.talos.consumer._run_auto_hint_loop")
    @patch("odoo.addons.talos.consumer._wait_for_sandbox_running", return_value="running")
    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_sandbox_start_needed(
        self, mock_call, mock_wait, mock_hint
    ):
        """docker_status='stopped' → action_start_sandbox is called."""
        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst
        mock_ws_inst.wait_for_response.return_value = MagicMock(
            text="resp", tool_calls_json="[]"
        )
        mock_ws_inst.fetch_history.return_value = []

        call_results = {
            "auto_process_claim_task": self._base_claim(docker_status="stopped"),
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_create_turn": {"turn_id": 100},
            "auto_process_save_response": True,
            "auto_process_mark_done": True,
        }

        def side_effect(model, method, ids, *a, **kw):
            return call_results.get(method, {})

        mock_call.side_effect = side_effect

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_channel.is_open = True
            from ..consumer import _process_task
            _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        start_calls = [
            c for c in mock_call.call_args_list
            if c[0][1] == "action_start_sandbox"
        ]
        self.assertEqual(len(start_calls), 1)
        mock_wait.assert_called_once()

    # ── all WS connect retries fail → task failed ─────────────────

    @patch("odoo.addons.talos.consumer.time.sleep")
    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_ws_connect_all_retries_fail(self, mock_call, mock_sleep):
        """3 WS connect failures → task ends in 'failed' state."""
        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst

        from ..consumer import _process_task

        # Import the exceptions that _process_task expects
        exc_cls = type("OpenClawError", (Exception,), {})
        mock_ws_inst.connect.side_effect = exc_cls("connection refused")

        call_results = {
            "auto_process_claim_task": self._base_claim(docker_status="running"),
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_mark_done": True,
        }

        def side_effect(model, method, ids, *a, **kw):
            return call_results.get(method, {})

        mock_call.side_effect = side_effect

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            with patch("odoo.addons.talos.consumer.OpenClawError", exc_cls):
                with patch("odoo.addons.talos.consumer.OpenClawTimeoutError", exc_cls):
                    mock_conn = MagicMock()
                    mock_channel = MagicMock()
                    mock_channel.is_open = True
                    _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        # Should have called mark_done with "failed"
        mark_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "auto_process_mark_done"
        ]
        self.assertTrue(len(mark_calls) > 0)
        self.assertIn("failed", str(mark_calls[-1]))

    # ── create_turn returns error → raises ────────────────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_create_turn_error(self, mock_call):
        """create_turn returning an error dict → task fails."""
        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst

        call_results = {
            "auto_process_claim_task": self._base_claim(docker_status="running"),
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_create_turn": {"error": "sandbox locked"},
            "auto_process_mark_done": True,
        }

        def side_effect(model, method, ids, *a, **kw):
            return call_results.get(method, {})

        mock_call.side_effect = side_effect

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_channel.is_open = True
            from ..consumer import _process_task
            _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        # Task should be marked failed
        mark_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "auto_process_mark_done"
        ]
        self.assertTrue(len(mark_calls) > 0)
        self.assertIn("failed", str(mark_calls[-1]))

    # ── ws response timeout → task failed ─────────────────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_ws_response_timeout(self, mock_call):
        """wait_for_response raises timeout → task fails."""
        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst

        timeout_exc = type("OpenClawTimeoutError", (Exception,), {})
        mock_ws_inst.wait_for_response.side_effect = timeout_exc("timed out")

        call_results = {
            "auto_process_claim_task": self._base_claim(docker_status="running"),
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_create_turn": {"turn_id": 100},
            "auto_process_mark_done": True,
        }

        def side_effect(model, method, ids, *a, **kw):
            return call_results.get(method, {})

        mock_call.side_effect = side_effect

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            with patch("odoo.addons.talos.consumer.OpenClawTimeoutError", timeout_exc):
                with patch("odoo.addons.talos.consumer.OpenClawError", timeout_exc):
                    mock_conn = MagicMock()
                    mock_channel = MagicMock()
                    mock_channel.is_open = True
                    from ..consumer import _process_task
                    _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        mark_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "auto_process_mark_done"
        ]
        self.assertTrue(len(mark_calls) > 0)
        self.assertIn("failed", str(mark_calls[-1]))

    # ── fetch_history failure is non-fatal ────────────────────────

    @patch("odoo.addons.talos.consumer._run_auto_hint_loop")
    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_fetch_history_failure_nonfatal(self, mock_call, mock_hint):
        """fetch_history raising → warning logged, task still succeeds."""
        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst
        mock_ws_inst.wait_for_response.return_value = MagicMock(
            text="resp", tool_calls_json="[]"
        )
        mock_ws_inst.fetch_history.side_effect = RuntimeError("history unavailable")

        call_results = {
            "auto_process_claim_task": self._base_claim(docker_status="running"),
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_create_turn": {"turn_id": 100},
            "auto_process_save_response": True,
            "auto_process_mark_done": True,
        }

        def side_effect(model, method, ids, *a, **kw):
            return call_results.get(method, {})

        mock_call.side_effect = side_effect

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_channel.is_open = True
            from ..consumer import _process_task
            _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        # Task should still succeed (mark_done with "done")
        mark_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "auto_process_mark_done"
        ]
        self.assertTrue(len(mark_calls) > 0)
        self.assertIn("done", str(mark_calls[-1]))

    # ── ws_client.disconnect called even on exception ─────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_disconnect_in_finally(self, mock_call):
        """ws_client.disconnect is called in finally even when task fails."""
        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst
        mock_ws_inst.send_message.side_effect = RuntimeError("send failed")

        call_results = {
            "auto_process_claim_task": self._base_claim(docker_status="running"),
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_create_turn": {"turn_id": 100},
            "auto_process_mark_done": True,
        }

        def side_effect(model, method, ids, *a, **kw):
            return call_results.get(method, {})

        mock_call.side_effect = side_effect

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_channel.is_open = True
            from ..consumer import _process_task
            _process_task(mock_conn, mock_channel, 1, None, self._make_body())

        mock_ws_inst.disconnect.assert_called_once()

    # ── invalid JSON body → exception, message acked ──────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_process_task_invalid_json_body(self, mock_call):
        """Non-JSON body → exception caught, message still acked."""
        mock_call.side_effect = lambda *a, **kw: True

        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_channel.is_open = True
        from ..consumer import _process_task
        _process_task(mock_conn, mock_channel, 1, None, b"not json {{")

        # Message should still be acked via add_callback_threadsafe
        mock_conn.add_callback_threadsafe.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# NEW: TestAutoHintLoopExtended — additional hint loop paths
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestAutoHintLoopExtended(TransactionCase):
    """Additional tests for _run_auto_hint_loop covering unsatisfied flow,
    empty hint text, max retries, trigger error, WS error, and eval timeout."""

    # ── unsatisfied → hint is sent via WS and saved ───────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_unsatisfied_sends_hint(self, mock_call):
        """Unsatisfied feedback → create_turn + WS send + save_response."""
        call_count = {"n": 0}

        def side_effect(model, method, ids, *a, **kw):
            call_count["n"] += 1
            if method == "auto_process_poll_hint_status":
                if call_count["n"] == 1:
                    return {"last_turn_id": 5, "auto_hint_status": "idle"}
                if call_count["n"] <= 3:
                    return {
                        "auto_hint_status": "idle",
                        "last_turn_feedback": "unsatisfied",
                        "last_turn_hint_text": "Try again with better formatting",
                        "auto_hint_group_id": "grp-1",
                        "auto_hint_iteration": 0,
                        "last_turn_id": 5,
                    }
                # After the hint turn, return satisfied to exit loop
                return {
                    "last_turn_id": 6,
                    "auto_hint_status": "idle",
                    "last_turn_feedback": "satisfied",
                }
            if method == "auto_process_trigger_hint_eval":
                return {"status": "pending"}
            if method == "auto_process_create_turn":
                return {"turn_id": 200}
            if method == "auto_process_save_response":
                return True
            if method == "auto_process_save_trajectory":
                return True
            return {}

        mock_call.side_effect = side_effect
        mock_ws = MagicMock()
        mock_ws.wait_for_response.return_value = MagicMock(
            text="hint response", tool_calls_json="[]"
        )
        mock_ws.fetch_history.return_value = [{"msg": "history"}]

        with patch("odoo.addons.talos.consumer.time.sleep"):
            from ..consumer import _run_auto_hint_loop
            _run_auto_hint_loop(10, mock_ws, 1)

        mock_ws.send_message.assert_called_with("Try again with better formatting")
        # create_turn for hint should have been called
        create_calls = [
            c for c in mock_call.call_args_list
            if c[0][1] == "auto_process_create_turn"
        ]
        self.assertTrue(len(create_calls) > 0)

    # ── unsatisfied but no hint text → exits ──────────────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_unsatisfied_no_hint_text(self, mock_call):
        """Unsatisfied with empty hint_text → exits without sending."""
        poll_n = {"n": 0}

        def side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                poll_n["n"] += 1
                if poll_n["n"] == 1:
                    return {"last_turn_id": 5, "auto_hint_status": "idle"}
                return {
                    "auto_hint_status": "idle",
                    "last_turn_feedback": "unsatisfied",
                    "last_turn_hint_text": "",
                    "last_turn_id": 5,
                }
            if method == "auto_process_trigger_hint_eval":
                return {"status": "pending"}
            return {}

        mock_call.side_effect = side_effect
        mock_ws = MagicMock()

        with patch("odoo.addons.talos.consumer.time.sleep"):
            from ..consumer import _run_auto_hint_loop
            _run_auto_hint_loop(10, mock_ws, 1)

        mock_ws.send_message.assert_not_called()

    # ── poll returns max_retries status → exits ───────────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_max_retries(self, mock_call):
        """Poll returning max_retries status → exits loop."""
        poll_n = {"n": 0}

        def side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                poll_n["n"] += 1
                if poll_n["n"] == 1:
                    return {"last_turn_id": 5, "auto_hint_status": "idle"}
                return {"auto_hint_status": "max_retries", "last_turn_id": 5}
            if method == "auto_process_trigger_hint_eval":
                return {"status": "pending"}
            return {}

        mock_call.side_effect = side_effect
        mock_ws = MagicMock()

        with patch("odoo.addons.talos.consumer.time.sleep"):
            from ..consumer import _run_auto_hint_loop
            _run_auto_hint_loop(10, mock_ws, 1)

        mock_ws.send_message.assert_not_called()

    # ── trigger returns error → exits ─────────────────────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_trigger_error(self, mock_call):
        """trigger_hint_eval returning error → exits immediately."""

        def side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                return {"last_turn_id": 5, "auto_hint_status": "idle"}
            if method == "auto_process_trigger_hint_eval":
                return {"error": "eval service unavailable"}
            return {}

        mock_call.side_effect = side_effect
        mock_ws = MagicMock()

        from ..consumer import _run_auto_hint_loop
        _run_auto_hint_loop(10, mock_ws, 1)

        mock_ws.send_message.assert_not_called()

    # ── WS error during hint send → exits ─────────────────────────

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_ws_error_during_hint(self, mock_call):
        """WS send_message or wait_for_response failing → saves error, exits."""
        poll_n = {"n": 0}

        def side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                poll_n["n"] += 1
                if poll_n["n"] == 1:
                    return {"last_turn_id": 5, "auto_hint_status": "idle"}
                return {
                    "auto_hint_status": "idle",
                    "last_turn_feedback": "unsatisfied",
                    "last_turn_hint_text": "Try this",
                    "auto_hint_group_id": "grp-1",
                    "auto_hint_iteration": 0,
                    "last_turn_id": 5,
                }
            if method == "auto_process_trigger_hint_eval":
                return {"status": "pending"}
            if method == "auto_process_create_turn":
                return {"turn_id": 300}
            return {}

        mock_call.side_effect = side_effect

        ws_exc = type("OpenClawError", (Exception,), {})
        mock_ws = MagicMock()
        mock_ws.send_message.side_effect = ws_exc("WS broken")

        with patch("odoo.addons.talos.consumer.time.sleep"):
            with patch("odoo.addons.talos.consumer.OpenClawError", ws_exc):
                with patch("odoo.addons.talos.consumer.OpenClawTimeoutError", ws_exc):
                    from ..consumer import _run_auto_hint_loop
                    _run_auto_hint_loop(10, mock_ws, 1)

        # save_response should have been called with the error text
        save_calls = [
            c for c in mock_call.call_args_list
            if c[0][1] == "auto_process_save_response"
        ]
        self.assertTrue(len(save_calls) > 0)

    # ── eval poll timeout → resets stuck status ───────────────────

    @patch("odoo.addons.talos.consumer.HINT_EVAL_TIMEOUT", 0)
    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_hint_loop_eval_timeout(self, mock_call):
        """Poll exceeding timeout → auto_process_reset_hint_status called."""
        poll_n = {"n": 0}

        def side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_poll_hint_status":
                poll_n["n"] += 1
                if poll_n["n"] == 1:
                    return {"last_turn_id": 5, "auto_hint_status": "idle"}
                # Always return evaluating so we time out
                return {"auto_hint_status": "evaluating", "last_turn_id": 5}
            if method == "auto_process_trigger_hint_eval":
                return {"status": "pending"}
            return {}

        mock_call.side_effect = side_effect
        mock_ws = MagicMock()

        with patch("odoo.addons.talos.consumer.time.sleep"):
            with patch("odoo.addons.talos.consumer.time.time") as mock_time:
                # First time.time() for deadline = 0 + 0 = 0
                # Subsequent calls return past deadline
                mock_time.side_effect = [0, 100, 200, 300]
                from ..consumer import _run_auto_hint_loop
                _run_auto_hint_loop(10, mock_ws, 1)

        reset_calls = [
            c for c in mock_call.call_args_list
            if c[0][1] == "auto_process_reset_hint_status"
        ]
        self.assertTrue(len(reset_calls) > 0)


# ═══════════════════════════════════════════════════════════════════════
# NEW: TestConsumerHelpers — _make_callback, stats, transport
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestConsumerHelpers(TransactionCase):
    """Tests for _make_callback, stats accounting, and _make_transport."""

    def test_make_callback_submits(self):
        """on_message callback submits work to the thread pool."""
        from ..consumer import _make_callback
        mock_conn = MagicMock()
        mock_pool = MagicMock()

        callback = _make_callback(mock_conn, mock_pool)
        mock_method = MagicMock()
        mock_method.delivery_tag = 42
        callback(MagicMock(), mock_method, None, b'{}')

        mock_pool.submit.assert_called_once()
        # First arg to submit should be _process_task
        submitted_fn = mock_pool.submit.call_args[0][0]
        self.assertEqual(submitted_fn.__name__, "_process_task")

    @patch("odoo.addons.talos.consumer._run_auto_hint_loop")
    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_stats_incremented_completion(self, mock_call, mock_hint):
        """tasks_completed counter increments on successful task."""
        import odoo.addons.talos.consumer as consumer_mod

        mock_ws_cls = MagicMock()
        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst
        mock_ws_inst.wait_for_response.return_value = MagicMock(
            text="resp", tool_calls_json="[]"
        )
        mock_ws_inst.fetch_history.return_value = []

        call_results = {
            "auto_process_claim_task": {
                "task_id": 1, "sandbox_id": 10,
                "initial_prompt": "test", "docker_status": "running",
            },
            "auto_process_get_ws_info": {
                "ws_url": "ws://localhost", "gateway_token": "tok"
            },
            "auto_process_create_turn": {"turn_id": 100},
            "auto_process_save_response": True,
            "auto_process_mark_done": True,
        }
        mock_call.side_effect = lambda m, meth, ids, *a, **kw: call_results.get(meth, {})

        with consumer_mod._stats_lock:
            before = consumer_mod._stats["tasks_completed"]

        with patch("odoo.addons.talos.consumer.OpenClawClient", mock_ws_cls):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_channel.is_open = True
            consumer_mod._process_task(
                mock_conn, mock_channel, 1, None,
                json.dumps({"task_id": 1}).encode(),
            )

        with consumer_mod._stats_lock:
            after = consumer_mod._stats["tasks_completed"]
        self.assertEqual(after, before + 1)

    @patch("odoo.addons.talos.consumer._call_odoo")
    def test_stats_incremented_failure(self, mock_call):
        """tasks_failed counter increments on exception."""
        import odoo.addons.talos.consumer as consumer_mod

        def side_effect(model, method, ids, *a, **kw):
            if method == "auto_process_claim_task":
                return {
                    "task_id": 1, "sandbox_id": 10,
                    "initial_prompt": "test", "docker_status": "running",
                }
            if method == "auto_process_get_ws_info":
                raise RuntimeError("boom")
            return True

        mock_call.side_effect = side_effect

        with consumer_mod._stats_lock:
            before = consumer_mod._stats["tasks_failed"]

        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_channel.is_open = True
        consumer_mod._process_task(
            mock_conn, mock_channel, 1, None,
            json.dumps({"task_id": 1}).encode(),
        )

        with consumer_mod._stats_lock:
            after = consumer_mod._stats["tasks_failed"]
        self.assertEqual(after, before + 1)

    @patch("odoo.addons.talos.consumer.ODOO_URL", "https://example.com")
    def test_make_transport_https(self):
        """_make_transport returns SafeTransport for https URLs."""
        import xmlrpc.client
        from ..consumer import _make_transport
        transport = _make_transport()
        self.assertIsInstance(transport, xmlrpc.client.SafeTransport)

    @patch("odoo.addons.talos.consumer.ODOO_URL", "http://localhost:8069")
    def test_make_transport_http(self):
        """_make_transport returns Transport for http URLs."""
        import xmlrpc.client
        from ..consumer import _make_transport
        transport = _make_transport()
        self.assertIsInstance(transport, xmlrpc.client.Transport)
        # SafeTransport is a subclass of Transport, so check it's NOT SafeTransport
        self.assertNotIsInstance(transport, xmlrpc.client.SafeTransport)
