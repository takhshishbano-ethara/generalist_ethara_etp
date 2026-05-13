# -*- coding: utf-8 -*-
import json
import logging
import os
import threading
import time

import pika
from dotenv import load_dotenv

load_dotenv(override=True)

_logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "rabbit_akshat")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "rabbit_akshat")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

QUEUE_AUTO_PROCESS = "skoll_auto_process"

_conn_lock = threading.RLock()
_connection = None
_channel = None


def _get_channel():
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
        _channel.queue_declare(queue=QUEUE_AUTO_PROCESS, durable=True)
        _logger.info(
            "RabbitMQ connection established (%s:%s)", RABBITMQ_HOST, RABBITMQ_PORT
        )
        return _channel


def _publish(queue, message_body):
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


def publish_auto_process_task(task_id):
    try:
        message = json.dumps({"task_id": task_id, "action": "auto_process"})
        _publish(QUEUE_AUTO_PROCESS, message)
        _logger.info("Published auto_process task for task_id=%s", task_id)
    except Exception as e:
        _logger.error(
            "Failed to publish auto_process task for task_id=%s: %s", task_id, e
        )
        raise


BATCH_CHUNK_SIZE = int(os.getenv("RABBITMQ_BATCH_CHUNK", "50"))
BATCH_CHUNK_DELAY = float(os.getenv("RABBITMQ_CHUNK_DELAY", "0.1"))


def batch_publish_auto_process_tasks(task_ids):
    total = len(task_ids)
    published = 0
    for chunk_start in range(0, total, BATCH_CHUNK_SIZE):
        chunk = task_ids[chunk_start : chunk_start + BATCH_CHUNK_SIZE]
        try:
            ch = _get_channel()
            for tid in chunk:
                message = json.dumps({"task_id": tid, "action": "auto_process"})
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_AUTO_PROCESS,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)
            _logger.info(
                "Batch-published chunk %d-%d / %d auto_process tasks",
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
            for tid in chunk:
                message = json.dumps({"task_id": tid, "action": "auto_process"})
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_AUTO_PROCESS,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)

        if chunk_start + BATCH_CHUNK_SIZE < total:
            time.sleep(BATCH_CHUNK_DELAY)

    _logger.info("Batch-published %d/%d auto_process tasks", published, total)
