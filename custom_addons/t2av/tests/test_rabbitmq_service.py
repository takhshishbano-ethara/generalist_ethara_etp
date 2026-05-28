# -*- coding: utf-8 -*-
"""Pure-Python unit tests for t2av/services/rabbitmq_service.py.

No broker connection required: pika channel interactions are mocked.
"""
import json
import os
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.t2av.services import rabbitmq_service


@tagged("post_install", "-at_install", "t2av", "t2av_rabbitmq")
class TestRabbitMqService(TransactionCase):

    def test_serialize_shape(self):
        body = rabbitmq_service._serialize(42, 7, "list_view_publish")
        self.assertIsInstance(body, bytes)
        payload = json.loads(body)
        self.assertEqual(payload["record_id"], 42)
        self.assertEqual(payload["user_id"], 7)
        self.assertEqual(payload["source"], "list_view_publish")
        self.assertIn("published_at", payload)
        self.assertTrue(payload["published_at"].endswith("Z"))

    def test_serialize_null_user(self):
        body = rabbitmq_service._serialize(1, None, "manual_retry")
        payload = json.loads(body)
        self.assertIsNone(payload["user_id"])

    def test_serialize_coerces_int(self):
        body = rabbitmq_service._serialize("123", "9", "src")
        payload = json.loads(body)
        self.assertEqual(payload["record_id"], 123)
        self.assertEqual(payload["user_id"], 9)

    def test_basic_properties(self):
        props = rabbitmq_service._basic_properties(retry_count=3)
        self.assertEqual(props.delivery_mode, 2)
        self.assertEqual(props.headers, {"x-retry-count": 3})
        self.assertEqual(props.content_type, "application/json")

    def test_basic_properties_default_retry_zero(self):
        props = rabbitmq_service._basic_properties()
        self.assertEqual(props.headers, {"x-retry-count": 0})

    def test_require_env_missing_raises(self):
        os.environ.pop("T2AV_TEST_NONEXISTENT_VAR", None)
        with self.assertRaises(RuntimeError) as ctx:
            rabbitmq_service._require_env("T2AV_TEST_NONEXISTENT_VAR")
        self.assertIn("T2AV_TEST_NONEXISTENT_VAR", str(ctx.exception))

    def test_require_env_present_returns_value(self):
        with mock.patch.dict(os.environ, {"T2AV_TEST_REQUIRE_VAR": "hello"}):
            self.assertEqual(
                rabbitmq_service._require_env("T2AV_TEST_REQUIRE_VAR"), "hello"
            )

    def test_module_constants_defaults(self):
        self.assertEqual(
            rabbitmq_service.QUEUE_PIPELINE,
            os.getenv("RABBITMQ_QUEUE", "t2av_pipeline"),
        )
        self.assertEqual(
            rabbitmq_service.DLX_EXCHANGE,
            os.getenv("RABBITMQ_DLX", "t2av_pipeline.dlx"),
        )
        self.assertEqual(
            rabbitmq_service.DLQ_QUEUE,
            os.getenv("RABBITMQ_DLQ", "t2av_pipeline.dead"),
        )
        self.assertIsInstance(rabbitmq_service.BATCH_CHUNK_SIZE, int)
        self.assertGreater(rabbitmq_service.BATCH_CHUNK_SIZE, 0)
        self.assertIsInstance(rabbitmq_service.BATCH_CHUNK_DELAY, float)
        self.assertGreaterEqual(rabbitmq_service.BATCH_CHUNK_DELAY, 0.0)

    def test_declare_topology(self):
        ch = mock.MagicMock()
        rabbitmq_service._declare_topology(ch)
        ch.exchange_declare.assert_called_once_with(
            exchange=rabbitmq_service.DLX_EXCHANGE,
            exchange_type="direct",
            durable=True,
        )
        self.assertEqual(ch.queue_declare.call_count, 2)
        ch.queue_bind.assert_called_once_with(
            queue=rabbitmq_service.DLQ_QUEUE,
            exchange=rabbitmq_service.DLX_EXCHANGE,
            routing_key=rabbitmq_service.DLQ_QUEUE,
        )
        main_calls = [
            c for c in ch.queue_declare.call_args_list
            if c.kwargs.get("queue") == rabbitmq_service.QUEUE_PIPELINE
        ]
        self.assertEqual(len(main_calls), 1)
        args = main_calls[0].kwargs
        self.assertTrue(args.get("durable"))
        self.assertEqual(
            args["arguments"]["x-dead-letter-exchange"],
            rabbitmq_service.DLX_EXCHANGE,
        )
        self.assertEqual(
            args["arguments"]["x-dead-letter-routing-key"],
            rabbitmq_service.DLQ_QUEUE,
        )

    def test_publish_pipeline_task_basic_publish_args(self):
        fake_channel = mock.MagicMock()
        fake_channel.is_open = True
        with mock.patch.object(
            rabbitmq_service, "_get_channel", return_value=fake_channel
        ):
            rabbitmq_service.publish_pipeline_task(
                99, user_id=5, source="test_src", retry_count=2
            )
        fake_channel.basic_publish.assert_called_once()
        kwargs = fake_channel.basic_publish.call_args.kwargs
        self.assertEqual(kwargs["exchange"], "")
        self.assertEqual(kwargs["routing_key"], rabbitmq_service.QUEUE_PIPELINE)
        body = json.loads(kwargs["body"])
        self.assertEqual(body["record_id"], 99)
        self.assertEqual(body["user_id"], 5)
        self.assertEqual(body["source"], "test_src")
        self.assertEqual(kwargs["properties"].delivery_mode, 2)
        self.assertEqual(kwargs["properties"].headers["x-retry-count"], 2)

    def test_batch_publish_chunks_size_and_count(self):
        ids = list(range(125))
        fake_channel = mock.MagicMock()
        with mock.patch.object(
            rabbitmq_service, "_get_channel", return_value=fake_channel
        ), mock.patch.object(
            rabbitmq_service.time, "sleep", return_value=None
        ):
            count = rabbitmq_service.batch_publish_pipeline_tasks(
                ids, user_id=1, source="batch_test"
            )
        self.assertEqual(count, 125)
        self.assertEqual(fake_channel.basic_publish.call_count, 125)

    def test_batch_publish_empty_returns_zero(self):
        count = rabbitmq_service.batch_publish_pipeline_tasks([])
        self.assertEqual(count, 0)

    def test_republish_with_retry_propagates_retry(self):
        fake_channel = mock.MagicMock()
        fake_channel.is_open = True
        with mock.patch.object(
            rabbitmq_service, "_get_channel", return_value=fake_channel
        ):
            rabbitmq_service.republish_with_retry(
                42, retry_count=4, source="consumer_retry"
            )
        kwargs = fake_channel.basic_publish.call_args.kwargs
        self.assertEqual(kwargs["properties"].headers["x-retry-count"], 4)
        body = json.loads(kwargs["body"])
        self.assertEqual(body["record_id"], 42)
        self.assertEqual(body["source"], "consumer_retry")
