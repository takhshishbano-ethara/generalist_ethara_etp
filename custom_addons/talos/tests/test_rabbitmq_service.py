# -*- coding: utf-8 -*-
import json
from unittest.mock import patch, MagicMock, call

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestRabbitMQGetChannel(TransactionCase):

    def setUp(self):
        super().setUp()
        import odoo.addons.talos.services.rabbitmq_service as svc
        self._svc = svc
        self._orig_conn = svc._connection
        self._orig_chan = svc._channel
        svc._connection = None
        svc._channel = None

    def tearDown(self):
        self._svc._connection = self._orig_conn
        self._svc._channel = self._orig_chan
        super().tearDown()

    @patch("odoo.addons.talos.services.rabbitmq_service.pika")
    def test_get_channel_creates_connection(self, mock_pika):
        mock_conn = MagicMock()
        mock_chan = MagicMock()
        mock_conn.is_open = True
        mock_chan.is_open = True
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_chan

        ch = self._svc._get_channel()

        mock_pika.BlockingConnection.assert_called_once()
        self.assertEqual(ch, mock_chan)

    @patch("odoo.addons.talos.services.rabbitmq_service.pika")
    def test_get_channel_declares_durable_queue(self, mock_pika):
        mock_conn = MagicMock()
        mock_chan = MagicMock()
        mock_conn.is_open = True
        mock_chan.is_open = True
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_chan

        self._svc._get_channel()

        mock_chan.queue_declare.assert_called_once_with(
            queue="talos_auto_process", durable=True
        )

    @patch("odoo.addons.talos.services.rabbitmq_service.pika")
    def test_get_channel_reuses_open_connection(self, mock_pika):
        mock_conn = MagicMock()
        mock_chan = MagicMock()
        mock_conn.is_open = True
        mock_chan.is_open = True
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_chan

        ch1 = self._svc._get_channel()
        ch2 = self._svc._get_channel()

        self.assertEqual(mock_pika.BlockingConnection.call_count, 1)
        self.assertEqual(ch1, ch2)


@tagged("post_install", "-at_install")
class TestRabbitMQPublish(TransactionCase):

    def setUp(self):
        super().setUp()
        import odoo.addons.talos.services.rabbitmq_service as svc
        self._svc = svc
        self._orig_conn = svc._connection
        self._orig_chan = svc._channel
        svc._connection = None
        svc._channel = None

    def tearDown(self):
        self._svc._connection = self._orig_conn
        self._svc._channel = self._orig_chan
        super().tearDown()

    @patch("odoo.addons.talos.services.rabbitmq_service.pika")
    def test_publish_auto_process_task_message_format(self, mock_pika):
        mock_conn = MagicMock()
        mock_chan = MagicMock()
        mock_conn.is_open = True
        mock_chan.is_open = True
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_chan

        self._svc.publish_auto_process_task(42)

        mock_chan.basic_publish.assert_called_once()
        call_kwargs = mock_chan.basic_publish.call_args
        body = call_kwargs[1]["body"] if "body" in (call_kwargs[1] or {}) else call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None
        if body is None:
            body = mock_chan.basic_publish.call_args.kwargs.get("body", mock_chan.basic_publish.call_args[1].get("body"))
        parsed = json.loads(body)
        self.assertEqual(parsed["task_id"], 42)
        self.assertEqual(parsed["action"], "auto_process")

    @patch("odoo.addons.talos.services.rabbitmq_service.pika")
    def test_publish_persistent_delivery_mode(self, mock_pika):
        mock_conn = MagicMock()
        mock_chan = MagicMock()
        mock_conn.is_open = True
        mock_chan.is_open = True
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_chan

        self._svc.publish_auto_process_task(1)

        call_args = mock_chan.basic_publish.call_args
        properties = call_args.kwargs.get("properties") or call_args[1].get("properties")
        self.assertEqual(properties.delivery_mode, 2)

    @patch("odoo.addons.talos.services.rabbitmq_service._get_channel")
    def test_publish_connection_error_retries(self, mock_get_channel):
        import pika.exceptions
        mock_chan_fail = MagicMock()
        mock_chan_fail.basic_publish.side_effect = pika.exceptions.AMQPConnectionError("lost")
        mock_chan_ok = MagicMock()
        mock_get_channel.side_effect = [mock_chan_fail, mock_chan_ok]

        self._svc._connection = None
        self._svc._channel = None

        self._svc._publish("talos_auto_process", '{"test": true}')
        self.assertEqual(mock_chan_ok.basic_publish.call_count, 1)

    @patch("odoo.addons.talos.services.rabbitmq_service.pika")
    def test_batch_publish_chunks_correctly(self, mock_pika):
        mock_conn = MagicMock()
        mock_chan = MagicMock()
        mock_conn.is_open = True
        mock_chan.is_open = True
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_chan

        task_ids = list(range(1, 121))
        with patch.object(self._svc, "BATCH_CHUNK_SIZE", 50):
            self._svc.batch_publish_auto_process_tasks(task_ids)

        self.assertEqual(mock_chan.basic_publish.call_count, 120)

    @patch("odoo.addons.talos.services.rabbitmq_service.pika")
    def test_batch_publish_empty_list_no_calls(self, mock_pika):
        mock_conn = MagicMock()
        mock_chan = MagicMock()
        mock_conn.is_open = True
        mock_chan.is_open = True
        mock_pika.BlockingConnection.return_value = mock_conn
        mock_conn.channel.return_value = mock_chan

        self._svc.batch_publish_auto_process_tasks([])

        mock_chan.basic_publish.assert_not_called()
