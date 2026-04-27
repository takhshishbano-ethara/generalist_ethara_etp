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
