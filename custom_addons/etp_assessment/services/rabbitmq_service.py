# -*- coding: utf-8 -*-
"""RabbitMQ publisher for per-question subjective (LLM) scoring.

Mirrors the house pattern (see preference_ranking/services/rabbitmq_service).
ONE queue: 'etp_assessment_score'. Each message carries a single
response_id; the consumer calls etp.assessment.response.rmq_score_subjective.

Connection params come from env (RABBITMQ_*) with safe local defaults.
Publishing is best-effort: if the broker is unreachable the caller catches
the exception and falls back to llm_state='pending' so the in-Odoo cron
drainer scores it instead (local dev needs no broker).
"""
import json
import logging
import os
import threading

import pika
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv optional
    pass

_logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", 5672))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

QUEUE_SCORE = os.getenv("ETP_SCORE_QUEUE", "etp_assessment_score")

_conn_lock = threading.Lock()
_connection = None
_channel = None


def _get_channel():
    """Reusable channel, reconnecting if the connection dropped."""
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
            connection_attempts=1,
            socket_timeout=5,
        )
        _connection = pika.BlockingConnection(params)
        _channel = _connection.channel()
        _channel.queue_declare(queue=QUEUE_SCORE, durable=True)
        _logger.info(
            "etp_assessment RabbitMQ connected (%s:%s)",
            RABBITMQ_HOST, RABBITMQ_PORT)
        return _channel


def publish_score_task(response_id):
    """Publish a per-question subjective scoring task. Raises on failure."""
    body = json.dumps({"response_id": response_id, "action": "score"})
    try:
        ch = _get_channel()
        ch.basic_publish(
            exchange="", routing_key=QUEUE_SCORE, body=body,
            properties=pika.BasicProperties(delivery_mode=2),
        )
        _logger.info("Published score task for response_id=%s", response_id)
    except (pika.exceptions.AMQPConnectionError,
            pika.exceptions.AMQPChannelError) as e:
        global _connection, _channel
        with _conn_lock:
            _connection = None
            _channel = None
        # one reconnect attempt; if it still fails, raise to the caller's
        # broker-down fallback
        ch = _get_channel()
        ch.basic_publish(
            exchange="", routing_key=QUEUE_SCORE, body=body,
            properties=pika.BasicProperties(delivery_mode=2),
        )
        _logger.info(
            "Published score task for response_id=%s (after reconnect)",
            response_id)


def batch_publish_score_tasks(response_ids):
    """Publish many per-question scoring tasks."""
    ch = _get_channel()
    for rid in response_ids:
        body = json.dumps({"response_id": rid, "action": "score"})
        ch.basic_publish(
            exchange="", routing_key=QUEUE_SCORE, body=body,
            properties=pika.BasicProperties(delivery_mode=2),
        )
    _logger.info("Batch-published %d score tasks", len(response_ids))
