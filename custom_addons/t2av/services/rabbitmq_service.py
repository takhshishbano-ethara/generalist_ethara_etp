# -*- coding: utf-8 -*-
import datetime
import json
import logging
import os
import threading
import time
from typing import Iterable, List, Optional

import pika

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

_logger = logging.getLogger(__name__)

QUEUE_PIPELINE = os.getenv("RABBITMQ_QUEUE", "t2av_pipeline")
DLX_EXCHANGE = os.getenv("RABBITMQ_DLX", "t2av_pipeline.dlx")
DLQ_QUEUE = os.getenv("RABBITMQ_DLQ", "t2av_pipeline.dead")

BATCH_CHUNK_SIZE = int(os.getenv("RABBITMQ_BATCH_CHUNK", "50"))
BATCH_CHUNK_DELAY = float(os.getenv("RABBITMQ_CHUNK_DELAY", "0.1"))

_conn_lock = threading.Lock()
_connection: Optional[pika.BlockingConnection] = None
_channel = None


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            "Environment variable %s is required for the T2AV "
            "RabbitMQ pipeline. Configure it in the Odoo host or "
            "the consumer .env file." % name
        )
    return val


def _connection_parameters() -> pika.ConnectionParameters:
    credentials = pika.PlainCredentials(
        _require_env("RABBITMQ_USERNAME"),
        _require_env("RABBITMQ_PASSWORD"),
    )
    return pika.ConnectionParameters(
        host=_require_env("RABBITMQ_HOST"),
        port=int(os.getenv("RABBITMQ_PORT", "5672")),
        virtual_host=os.getenv("RABBITMQ_VHOST", "/"),
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )


def _declare_topology(channel) -> None:
    """Idempotent: declares DLX exchange, DLQ, and main queue bound to DLX."""
    channel.exchange_declare(
        exchange=DLX_EXCHANGE,
        exchange_type="direct",
        durable=True,
    )
    channel.queue_declare(queue=DLQ_QUEUE, durable=True)
    channel.queue_bind(
        queue=DLQ_QUEUE,
        exchange=DLX_EXCHANGE,
        routing_key=DLQ_QUEUE,
    )
    channel.queue_declare(
        queue=QUEUE_PIPELINE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": DLQ_QUEUE,
        },
    )


def _get_channel():
    global _connection, _channel
    with _conn_lock:
        if (
            _connection
            and _connection.is_open
            and _channel
            and _channel.is_open
        ):
            return _channel

        if _connection and _connection.is_open:
            try:
                _connection.close()
            except Exception:  # noqa: BLE001
                pass

        _connection = pika.BlockingConnection(_connection_parameters())
        _channel = _connection.channel()
        _declare_topology(_channel)
        _logger.info(
            "T2AV RabbitMQ connection established (queue=%s)",
            QUEUE_PIPELINE,
        )
        return _channel


def _reset_connection() -> None:
    global _connection, _channel
    with _conn_lock:
        _connection = None
        _channel = None


def _basic_properties(retry_count: int = 0) -> pika.BasicProperties:
    headers = {"x-retry-count": retry_count}
    return pika.BasicProperties(
        delivery_mode=2,
        headers=headers,
        content_type="application/json",
    )


def _serialize(record_id: int, user_id: Optional[int], source: str) -> bytes:
    payload = {
        "record_id": int(record_id),
        "published_at": datetime.datetime.utcnow().isoformat() + "Z",
        "user_id": int(user_id) if user_id else None,
        "source": source,
    }
    return json.dumps(payload).encode("utf-8")


def publish_pipeline_task(
    record_id: int,
    user_id: Optional[int] = None,
    source: str = "list_view_publish",
    retry_count: int = 0,
) -> None:
    """One reconnect attempt on AMQP connection/channel error; further failure propagates."""
    body = _serialize(record_id, user_id, source)
    try:
        ch = _get_channel()
        ch.basic_publish(
            exchange="",
            routing_key=QUEUE_PIPELINE,
            body=body,
            properties=_basic_properties(retry_count),
        )
    except (
        pika.exceptions.AMQPConnectionError,
        pika.exceptions.AMQPChannelError,
    ) as exc:
        _logger.warning(
            "T2AV publish lost connection (rid=%s); reconnecting: %s",
            record_id, exc,
        )
        _reset_connection()
        ch = _get_channel()
        ch.basic_publish(
            exchange="",
            routing_key=QUEUE_PIPELINE,
            body=body,
            properties=_basic_properties(retry_count),
        )
    _logger.info(
        "T2AV published rid=%s source=%s retry=%d",
        record_id, source, retry_count,
    )


def batch_publish_pipeline_tasks(
    record_ids: Iterable[int],
    user_id: Optional[int] = None,
    source: str = "list_view_publish",
) -> int:
    """Chunked publish with per-chunk reconnect-once. Returns count published."""
    ids: List[int] = [int(rid) for rid in record_ids]
    total = len(ids)
    published = 0
    if total == 0:
        return 0

    for chunk_start in range(0, total, BATCH_CHUNK_SIZE):
        chunk = ids[chunk_start: chunk_start + BATCH_CHUNK_SIZE]
        try:
            ch = _get_channel()
            for rid in chunk:
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_PIPELINE,
                    body=_serialize(rid, user_id, source),
                    properties=_basic_properties(0),
                )
            published += len(chunk)
            _logger.info(
                "T2AV batch-published chunk %d-%d / %d",
                chunk_start + 1, chunk_start + len(chunk), total,
            )
        except (
            pika.exceptions.AMQPConnectionError,
            pika.exceptions.AMQPChannelError,
        ) as exc:
            _logger.warning(
                "T2AV connection lost at chunk %d; reconnecting: %s",
                chunk_start, exc,
            )
            _reset_connection()
            ch = _get_channel()
            for rid in chunk:
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_PIPELINE,
                    body=_serialize(rid, user_id, source),
                    properties=_basic_properties(0),
                )
            published += len(chunk)

        if chunk_start + BATCH_CHUNK_SIZE < total:
            time.sleep(BATCH_CHUNK_DELAY)

    _logger.info("T2AV batch-published %d/%d pipeline tasks", published, total)
    return published


def republish_with_retry(record_id: int, retry_count: int, source: str = "consumer_retry") -> None:
    """Called from consumer worker via add_callback_threadsafe; runs on the IO thread."""
    publish_pipeline_task(
        record_id,
        user_id=None,
        source=source,
        retry_count=retry_count,
    )
