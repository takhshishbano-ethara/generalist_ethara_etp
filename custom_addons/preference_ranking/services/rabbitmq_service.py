# -*- coding: utf-8 -*-
"""
RabbitMQ Service for Preference Ranking eval/QC pipeline.

Provides helpers to publish messages with persistent connection pooling
and batch-publish support.
"""
import json
import logging
import os
import threading
import time

import pika
from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)

# ── Connection parameters (from env or defaults) ─────────────────────────────
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "13.233.171.194")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", 5672))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "grtlabs")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "grtlabs#hddn#nc#$")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

QUEUE_EVAL = "preference_ranking_eval"
QUEUE_QC = "preference_ranking_qc"
QUEUE_REEVAL = "preference_ranking_reeval"

# ── Persistent connection pool ────────────────────────────────────────────────
_conn_lock = threading.Lock()
_connection = None
_channel = None


def _get_channel():
    """Return a reusable channel, reconnecting if the connection dropped."""
    global _connection, _channel
    with _conn_lock:
        if _connection and _connection.is_open and _channel and _channel.is_open:
            return _channel

        if _connection and _connection.is_open:
            try:
                _connection.close()
            except Exception:
                pass

        credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
        params = pika.ConnectionParameters(
            host=RABBITMQ_HOST,
            port=RABBITMQ_PORT,
            virtual_host=RABBITMQ_VHOST,
            credentials=credentials,
            heartbeat=600,
            blocked_connection_timeout=300,
        )
        _connection = pika.BlockingConnection(params)
        _channel = _connection.channel()
        _channel.queue_declare(queue=QUEUE_EVAL, durable=True)
        _channel.queue_declare(queue=QUEUE_QC, durable=True)
        _channel.queue_declare(queue=QUEUE_REEVAL, durable=True)
        _logger.info(
            "RabbitMQ connection established (%s:%s)", RABBITMQ_HOST, RABBITMQ_PORT
        )
        return _channel


def _publish(queue, message_body):
    """Publish a single message to a queue using the persistent connection."""
    try:
        ch = _get_channel()
        ch.basic_publish(
            exchange="",
            routing_key=queue,
            body=message_body,
            properties=pika.BasicProperties(delivery_mode=2),
        )
    except (pika.exceptions.AMQPConnectionError, pika.exceptions.AMQPChannelError) as e:
        _logger.warning("Connection lost during publish, reconnecting: %s", e)
        global _connection, _channel
        with _conn_lock:
            _connection = None
            _channel = None
        ch = _get_channel()
        ch.basic_publish(
            exchange="",
            routing_key=queue,
            body=message_body,
            properties=pika.BasicProperties(delivery_mode=2),
        )


def publish_eval_task(record_id):
    """Publish an eval task message to the EVAL queue."""
    try:
        message = json.dumps({"record_id": record_id, "action": "eval"})
        _publish(QUEUE_EVAL, message)
        _logger.info("Published eval task for record_id=%s", record_id)
    except Exception as e:
        _logger.error("Failed to publish eval task for record_id=%s: %s", record_id, e)
        raise


def publish_qc_task(record_id):
    """Publish a QC task message to the QC queue."""
    try:
        message = json.dumps({"record_id": record_id, "action": "qc"})
        _publish(QUEUE_QC, message)
        _logger.info("Published QC task for record_id=%s", record_id)
    except Exception as e:
        _logger.error("Failed to publish QC task for record_id=%s: %s", record_id, e)
        raise


BATCH_CHUNK_SIZE = int(os.getenv("RABBITMQ_BATCH_CHUNK", "50"))
BATCH_CHUNK_DELAY = float(os.getenv("RABBITMQ_CHUNK_DELAY", "0.1"))


def batch_publish_eval_tasks(record_ids):
    """Publish multiple eval tasks in chunks with pauses to avoid overwhelming RabbitMQ.

    Publishes BATCH_CHUNK_SIZE messages at a time, pausing BATCH_CHUNK_DELAY seconds
    between chunks so RabbitMQ can flush to disk and won't trigger
    Connection.Blocked (low on disk).
    """
    total = len(record_ids)
    published = 0
    for chunk_start in range(0, total, BATCH_CHUNK_SIZE):
        chunk = record_ids[chunk_start : chunk_start + BATCH_CHUNK_SIZE]
        try:
            ch = _get_channel()
            for rid in chunk:
                message = json.dumps({"record_id": rid, "action": "eval"})
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_EVAL,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)
            _logger.info(
                "Batch-published chunk %d-%d / %d eval tasks",
                chunk_start + 1,
                chunk_start + len(chunk),
                total,
            )
        except (
            pika.exceptions.AMQPConnectionError,
            pika.exceptions.AMQPChannelError,
        ) as e:
            _logger.warning(
                "Connection lost at chunk %d, reconnecting: %s", chunk_start, e
            )
            global _connection, _channel
            with _conn_lock:
                _connection = None
                _channel = None
            ch = _get_channel()
            for rid in chunk:
                message = json.dumps({"record_id": rid, "action": "eval"})
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_EVAL,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)

        if chunk_start + BATCH_CHUNK_SIZE < total:
            time.sleep(BATCH_CHUNK_DELAY)

    _logger.info("Batch-published %d/%d eval tasks", published, total)


def publish_reeval_task(record_id):
    """Publish a re-evaluation task message to the REEVAL queue."""
    try:
        message = json.dumps({"record_id": record_id, "action": "reeval"})
        _publish(QUEUE_REEVAL, message)
        _logger.info("Published reeval task for record_id=%s", record_id)
    except Exception as e:
        _logger.error(
            "Failed to publish reeval task for record_id=%s: %s", record_id, e
        )
        raise


def batch_publish_reeval_tasks(record_ids):
    """Publish multiple reeval tasks in chunks with pauses to avoid overwhelming RabbitMQ."""
    total = len(record_ids)
    published = 0
    for chunk_start in range(0, total, BATCH_CHUNK_SIZE):
        chunk = record_ids[chunk_start : chunk_start + BATCH_CHUNK_SIZE]
        try:
            ch = _get_channel()
            for rid in chunk:
                message = json.dumps({"record_id": rid, "action": "reeval"})
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_REEVAL,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)
            _logger.info(
                "Batch-published chunk %d-%d / %d reeval tasks",
                chunk_start + 1,
                chunk_start + len(chunk),
                total,
            )
        except (
            pika.exceptions.AMQPConnectionError,
            pika.exceptions.AMQPChannelError,
        ) as e:
            _logger.warning(
                "Connection lost at chunk %d, reconnecting: %s", chunk_start, e
            )
            global _connection, _channel
            with _conn_lock:
                _connection = None
                _channel = None
            ch = _get_channel()
            for rid in chunk:
                message = json.dumps({"record_id": rid, "action": "reeval"})
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_REEVAL,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)

        if chunk_start + BATCH_CHUNK_SIZE < total:
            time.sleep(BATCH_CHUNK_DELAY)

    _logger.info("Batch-published %d/%d reeval tasks", published, total)
