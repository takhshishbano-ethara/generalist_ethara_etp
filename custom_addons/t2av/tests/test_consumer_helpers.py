# -*- coding: utf-8 -*-
"""Pure-Python unit tests for t2av/consumer.py helpers.

The consumer module calls _require_env at import time for ODOO_URL / ODOO_DB /
ODOO_USERNAME / ODOO_PASSWORD. We preload dummy values here before loading the
module via importlib so the tests do not require a running broker or Odoo HTTP.
"""
import importlib.util
import os
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


os.environ.setdefault("ODOO_URL", "http://t2av.test.local")
os.environ.setdefault("ODOO_DB", "t2av_test_db")
os.environ.setdefault("ODOO_USERNAME", "t2av_test_user")
os.environ.setdefault("ODOO_PASSWORD", "t2av_test_password")


_CONSUMER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "consumer.py",
)
_spec = importlib.util.spec_from_file_location(
    "t2av_consumer_under_test", _CONSUMER_PATH,
)
consumer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(consumer)


@tagged("post_install", "-at_install", "t2av", "t2av_consumer")
class TestConsumerHelpers(TransactionCase):

    def test_get_retry_count_default_when_no_headers(self):
        props = mock.MagicMock(headers=None)
        self.assertEqual(consumer._get_retry_count(props), 0)

    def test_get_retry_count_from_header_value(self):
        props = mock.MagicMock(headers={"x-retry-count": 3})
        self.assertEqual(consumer._get_retry_count(props), 3)

    def test_get_retry_count_none_properties(self):
        self.assertEqual(consumer._get_retry_count(None), 0)

    def test_get_retry_count_empty_headers_dict(self):
        props = mock.MagicMock(headers={})
        self.assertEqual(consumer._get_retry_count(props), 0)

    def test_is_permanent_failure_for_each_phrase(self):
        permanent_phrases = (
            "Permanent failure: missing prompt",
            "Record does not exist (id=42)",
            "this record has been deleted",
            "Missing required fields: foo",
            "ACCESS DENIED for user",
            "Access Error: no rights",
            "Validation error: bad field",
        )
        for phrase in permanent_phrases:
            self.assertTrue(
                consumer._is_permanent_failure(Exception(phrase)),
                f"Expected permanent failure for: {phrase!r}",
            )

    def test_is_permanent_failure_negative_cases(self):
        transient_phrases = (
            "Connection refused",
            "Timeout occurred",
            "Unknown error",
            "500 Internal Server Error",
            "broken pipe",
            "Read timed out",
        )
        for phrase in transient_phrases:
            self.assertFalse(
                consumer._is_permanent_failure(Exception(phrase)),
                f"Expected transient for: {phrase!r}",
            )

    def test_is_gateway_error_for_each_phrase(self):
        gateway_phrases = (
            "502 Bad Gateway",
            "504 Gateway Time-out",
            "could not serialize access to record",
            "concurrent update detected",
            "deadlock detected on row 42",
        )
        for phrase in gateway_phrases:
            self.assertTrue(
                consumer._is_gateway_error(Exception(phrase)),
                f"Expected gateway for: {phrase!r}",
            )

    def test_is_gateway_error_negative_cases(self):
        non_gateway = ("Connection refused", "Timeout", "500 Internal", "")
        for phrase in non_gateway:
            self.assertFalse(consumer._is_gateway_error(Exception(phrase)))

    def test_retry_delay_exponential_growth(self):
        base = consumer.RETRY_BACKOFF_BASE
        cap = consumer.RETRY_BACKOFF_CAP
        self.assertEqual(consumer._retry_delay(0), min(base, cap))
        self.assertEqual(consumer._retry_delay(1), min(base * 2, cap))
        self.assertEqual(consumer._retry_delay(2), min(base * 4, cap))
        self.assertEqual(consumer._retry_delay(3), min(base * 8, cap))

    def test_retry_delay_caps_at_max(self):
        for n in range(5, 30):
            self.assertLessEqual(
                consumer._retry_delay(n), consumer.RETRY_BACKOFF_CAP
            )

    def test_require_env_missing_raises(self):
        os.environ.pop("T2AV_NONEXISTENT_HELPER_VAR", None)
        with self.assertRaises(RuntimeError) as ctx:
            consumer._require_env("T2AV_NONEXISTENT_HELPER_VAR")
        self.assertIn("T2AV_NONEXISTENT_HELPER_VAR", str(ctx.exception))

    def test_ack_message_open_channel(self):
        ch = mock.MagicMock()
        ch.is_open = True
        consumer._ack_message(ch, delivery_tag=42)
        ch.basic_ack.assert_called_once_with(delivery_tag=42)

    def test_ack_message_closed_channel_noop(self):
        ch = mock.MagicMock()
        ch.is_open = False
        consumer._ack_message(ch, delivery_tag=42)
        ch.basic_ack.assert_not_called()

    def test_nack_to_dlq_open_channel(self):
        ch = mock.MagicMock()
        ch.is_open = True
        consumer._nack_message_to_dlq(ch, delivery_tag=99)
        ch.basic_nack.assert_called_once_with(
            delivery_tag=99, requeue=False,
        )

    def test_nack_to_dlq_closed_channel_noop(self):
        ch = mock.MagicMock()
        ch.is_open = False
        consumer._nack_message_to_dlq(ch, delivery_tag=99)
        ch.basic_nack.assert_not_called()

    def test_declare_topology_calls(self):
        ch = mock.MagicMock()
        consumer._declare_topology(ch)
        ch.exchange_declare.assert_called_once_with(
            exchange=consumer.DLX_EXCHANGE,
            exchange_type="direct",
            durable=True,
        )
        self.assertEqual(ch.queue_declare.call_count, 2)
        ch.queue_bind.assert_called_once_with(
            queue=consumer.DLQ_QUEUE,
            exchange=consumer.DLX_EXCHANGE,
            routing_key=consumer.DLQ_QUEUE,
        )

    def test_republish_with_retry_increments_header(self):
        connection = mock.MagicMock()
        publish_channel = mock.MagicMock()
        connection.channel.return_value = publish_channel
        consumer._republish_with_retry(
            connection, mock.MagicMock(), b'{"record_id":7}', retry_count=4,
        )
        publish_channel.basic_publish.assert_called_once()
        kwargs = publish_channel.basic_publish.call_args.kwargs
        self.assertEqual(kwargs["routing_key"], consumer.QUEUE_PIPELINE)
        self.assertEqual(kwargs["properties"].headers["x-retry-count"], 5)
        self.assertEqual(kwargs["properties"].delivery_mode, 2)
        publish_channel.close.assert_called_once()
