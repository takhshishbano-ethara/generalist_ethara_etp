#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RabbitMQ Consumer for Leviathan pipeline.

Runs OUTSIDE the Odoo HTTP process as a standalone worker.
Connects to RabbitMQ, listens on the leviathan_pipeline queue, and processes
messages by calling leviathan.job.rpc_run_full_pipeline via XML-RPC.

The RPC method triggers extraction (fire-and-forget to Lambda).
The rest of the pipeline proceeds via existing webhook -> generation -> QC.

Usage:
    python consumer.py

Environment variables required:
    RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQ_USERNAME, RABBITMQ_PASSWORD, RABBITMQ_VHOST
    ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD
    CONSUMER_WORKERS  -- concurrent worker threads (default 4)
"""

import functools
import json
import logging
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pika
import xmlrpc.client
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("leviathan.consumer")

# ── RabbitMQ Config ───────────────────────────────────────────────────────────
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "rabbit_akshat")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "rabbit_akshat")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")
QUEUE_PIPELINE = "leviathan_pipeline"

# ── Odoo XML-RPC Config ──────────────────────────────────────────────────────
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "leviathan_dev")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")
XMLRPC_TIMEOUT = int(os.getenv("XMLRPC_TIMEOUT", "1800"))  # 30 min

# ── Consumer Config ───────────────────────────────────────────────────────────
WORKER_THREADS = int(os.getenv("CONSUMER_WORKERS", "4"))
MAX_RETRIES = int(os.getenv("CONSUMER_MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = int(os.getenv("CONSUMER_RETRY_BACKOFF", "30"))

# ── Monitoring ────────────────────────────────────────────────────────────────
_stats_lock = threading.Lock()
_stats = {
    "pipeline_completed": 0,
    "pipeline_failed": 0,
    "total_time": 0.0,
}


def _log_stats():
    with _stats_lock:
        pc = _stats["pipeline_completed"]
        pf = _stats["pipeline_failed"]
        avg = (_stats["total_time"] / pc) if pc else 0
    _logger.info(
        "STATS | pipeline: %d done, %d failed (avg %.1fs)",
        pc, pf, avg,
    )


# ── Odoo XML-RPC Helpers ─────────────────────────────────────────────────────
_cached_uid = None
_uid_lock = threading.Lock()


def _make_transport():
    transport = (
        xmlrpc.client.SafeTransport()
        if ODOO_URL.startswith("https")
        else xmlrpc.client.Transport()
    )
    original_make_connection = transport.make_connection

    def _patched_make_connection(host):
        conn = original_make_connection(host)
        conn.timeout = XMLRPC_TIMEOUT
        return conn

    transport.make_connection = _patched_make_connection
    return transport


def _get_odoo_uid():
    global _cached_uid
    with _uid_lock:
        if _cached_uid is not None:
            return _cached_uid
    transport = _make_transport()
    common = xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/common", transport=transport
    )
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError(
            f"Odoo authentication failed for user {ODOO_USERNAME} on DB {ODOO_DB}"
        )
    with _uid_lock:
        _cached_uid = uid
    return uid


def _get_odoo_models():
    transport = _make_transport()
    return xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/object", transport=transport
    )


def _call_odoo_method(model, method, record_ids, args=None, kwargs=None):
    uid = _get_odoo_uid()
    models_proxy = _get_odoo_models()
    return models_proxy.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        model,
        method,
        [record_ids] + (args or []),
        kwargs or {},
    )


# ── Retry Helpers ─────────────────────────────────────────────────────────────
def _get_retry_count(properties):
    headers = (properties.headers or {}) if properties else {}
    return headers.get("x-retry-count", 0)


def _republish_with_retry(connection, queue, body, retry_count):
    headers = {"x-retry-count": retry_count + 1}
    channel = connection.channel()
    channel.basic_publish(
        exchange="",
        routing_key=queue,
        body=body,
        properties=pika.BasicProperties(
            delivery_mode=2,
            headers=headers,
        ),
    )
    channel.close()


def _is_permanent_failure(exc):
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in [
            "record does not exist",
            "has been deleted",
            "missing required fields",
            "access denied",
            "access error",
        ]
    )


def _is_gateway_error(exc):
    msg = str(exc)
    return (
        "502 Bad Gateway" in msg
        or "504 Gateway Time-out" in msg
        or "could not serialize access" in msg
        or "concurrent update" in msg
        or "deadlock detected" in msg
    )


def _retry_delay(retry_count):
    return min(RETRY_BACKOFF_BASE * (2 ** retry_count), 600)


def _ack_message(channel, delivery_tag):
    if channel.is_open:
        channel.basic_ack(delivery_tag=delivery_tag)


# ── Worker Function ───────────────────────────────────────────────────────────
def _process_pipeline(connection, channel, delivery_tag, properties, body):
    retry_count = _get_retry_count(properties)
    record_id = None
    start_time = time.time()
    try:
        message = json.loads(body)
        record_id = message.get("record_id")
        _logger.info(
            "Processing PIPELINE task for record_id=%s (attempt %d/%d)",
            record_id,
            retry_count + 1,
            MAX_RETRIES,
        )

        if not record_id:
            _logger.error("Missing record_id in pipeline message: %s", body)
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        _call_odoo_method("leviathan.job", "rpc_run_full_pipeline", [record_id])

        elapsed = time.time() - start_time
        _logger.info(
            "PIPELINE task triggered for record_id=%s (%.1fs)", record_id, elapsed
        )

        with _stats_lock:
            _stats["pipeline_completed"] += 1
            _stats["total_time"] += elapsed

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)

    except Exception as exc:
        elapsed = time.time() - start_time
        _logger.error(
            "PIPELINE task FAILED for record_id=%s (%.1fs): %s",
            record_id, elapsed, exc,
        )

        with _stats_lock:
            _stats["pipeline_failed"] += 1

        if _is_permanent_failure(exc):
            _logger.warning(
                "Permanent failure for record_id=%s, ACK-ing: %s", record_id, exc
            )
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        if retry_count < MAX_RETRIES:
            delay = _retry_delay(retry_count)
            _logger.info(
                "Retrying record_id=%s in %ds (attempt %d/%d)",
                record_id, delay, retry_count + 2, MAX_RETRIES,
            )
            time.sleep(delay)
            try:
                _republish_with_retry(
                    connection, QUEUE_PIPELINE, body, retry_count
                )
            except Exception as re_exc:
                _logger.error(
                    "Failed to republish record_id=%s: %s", record_id, re_exc
                )
        else:
            _logger.error(
                "Max retries exceeded for record_id=%s, ACK-ing permanently",
                record_id,
            )

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)


# ── Callback Factory ──────────────────────────────────────────────────────────
def _make_pipeline_callback(connection, thread_pool):
    def callback(ch, method, properties, body):
        thread_pool.submit(
            _process_pipeline, connection, ch, method.delivery_tag, properties, body
        )
    return callback


# ── Consumer Setup ────────────────────────────────────────────────────────────
def start_consumer():
    _logger.info("Starting Leviathan consumer with %d worker threads", WORKER_THREADS)
    _logger.info("Connecting to RabbitMQ at %s:%s...", RABBITMQ_HOST, RABBITMQ_PORT)
    _logger.info("Odoo: %s (DB: %s, User: %s)", ODOO_URL, ODOO_DB, ODOO_USERNAME)

    try:
        uid = _get_odoo_uid()
        _logger.info("Odoo connection verified (uid=%s)", uid)
    except Exception as e:
        _logger.error("Cannot connect to Odoo: %s. Retrying in 5s...", e)
        time.sleep(5)
        raise

    credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
    params = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )

    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.queue_declare(queue=QUEUE_PIPELINE, durable=True)
    channel.basic_qos(prefetch_count=WORKER_THREADS)

    thread_pool = ThreadPoolExecutor(max_workers=WORKER_THREADS)

    channel.basic_consume(
        queue=QUEUE_PIPELINE,
        on_message_callback=_make_pipeline_callback(connection, thread_pool),
    )

    _logger.info("Consumer started. Listening on queue: [%s]", QUEUE_PIPELINE)
    _logger.info("Press Ctrl+C to exit.")

    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        _logger.info("Consumer stopped by user.")
        channel.stop_consuming()
    finally:
        _log_stats()
        thread_pool.shutdown(wait=True)
        connection.close()
        _logger.info("RabbitMQ connection closed.")


if __name__ == "__main__":
    while True:
        try:
            start_consumer()
        except pika.exceptions.AMQPConnectionError as e:
            _logger.error("RabbitMQ connection lost: %s. Reconnecting in 5s...", e)
            time.sleep(5)
        except Exception as e:
            _logger.error("Consumer crashed: %s\n%s", e, traceback.format_exc())
            _logger.info("Restarting consumer in 10s...")
            time.sleep(10)
