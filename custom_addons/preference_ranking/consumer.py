#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RabbitMQ Consumer for Preference Ranking eval & QC pipeline.

This script runs OUTSIDE the Odoo HTTP process as a standalone worker.
It connects to RabbitMQ, listens on the eval and QC queues, and processes
messages by calling the corresponding Odoo model methods via XML-RPC.

Supports concurrent processing via a ThreadPoolExecutor dispatcher.

Usage:
    python consumer.py

Environment variables required:
    RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQ_USERNAME, RABBITMQ_PASSWORD, RABBITMQ_VHOST
    ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD
    CONSUMER_WORKERS  -- number of concurrent worker threads (default 15)
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

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger('preference_ranking.consumer')

# ── RabbitMQ Config ───────────────────────────────────────────────────────────
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', '13.233.171.194')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', 5672))
RABBITMQ_USERNAME = os.getenv('RABBITMQ_USERNAME', 'grtlabs')
RABBITMQ_PASSWORD = os.getenv('RABBITMQ_PASSWORD', 'grtlabs#hddn#nc#$')
RABBITMQ_VHOST = os.getenv('RABBITMQ_VHOST', '/')

QUEUE_EVAL = 'preference_ranking_eval'
QUEUE_QC = 'preference_ranking_qc'

MAX_RETRIES = int(os.getenv('CONSUMER_MAX_RETRIES', '5'))
WORKER_THREADS = int(os.getenv('CONSUMER_WORKERS', '5'))
RETRY_BACKOFF_BASE = int(os.getenv('CONSUMER_RETRY_BACKOFF', '30'))

# ── Odoo XML-RPC Config ──────────────────────────────────────────────────────
ODOO_URL = os.getenv('ODOO_URL', 'http://localhost:8071')
ODOO_DB = os.getenv('ODOO_DB', 'odoo18')
ODOO_USERNAME = os.getenv('ODOO_USERNAME', 'admin')
ODOO_PASSWORD = os.getenv('ODOO_PASSWORD', 'admin')

XMLRPC_TIMEOUT = int(os.getenv('XMLRPC_TIMEOUT', '1800'))

# ── Rate Limiting ─────────────────────────────────────────────────────────────
KIMI_MAX_CONCURRENT = int(os.getenv('KIMI_MAX_CONCURRENT', '20'))
OPENAI_MAX_CONCURRENT = int(os.getenv('OPENAI_MAX_CONCURRENT', '10'))
GEMINI_MAX_CONCURRENT = int(os.getenv('GEMINI_MAX_CONCURRENT', '10'))
GENAI_MAX_CONCURRENT = int(os.getenv('GENAI_MAX_CONCURRENT', '10'))

# ── Monitoring ────────────────────────────────────────────────────────────────
_stats_lock = threading.Lock()
_stats = {
    "eval_completed": 0,
    "eval_failed": 0,
    "qc_completed": 0,
    "qc_failed": 0,
    "reeval_completed": 0,
    "reeval_failed": 0,
    "total_eval_time": 0.0,
    "total_qc_time": 0.0,
    "total_reeval_time": 0.0,
}


def _log_stats():
    """Log current processing statistics."""
    with _stats_lock:
        ec = _stats["eval_completed"]
        ef = _stats["eval_failed"]
        qc = _stats["qc_completed"]
        qf = _stats["qc_failed"]
        rc = _stats["reeval_completed"]
        rf = _stats["reeval_failed"]
        avg_eval = (_stats["total_eval_time"] / ec) if ec else 0
        avg_qc = (_stats["total_qc_time"] / qc) if qc else 0
        avg_reeval = (_stats["total_reeval_time"] / rc) if rc else 0
    _logger.info(
        "STATS | eval: %d done, %d failed (avg %.1fs) | qc: %d done, %d failed (avg %.1fs) | reeval: %d done, %d failed (avg %.1fs)",
        ec,
        ef,
        avg_eval,
        qc,
        qf,
        avg_qc,
        rc,
        rf,
        avg_reeval,
    )


# ── Odoo XML-RPC Helpers (with UID caching) ──────────────────────────────────
_cached_uid = None
_uid_lock = threading.Lock()


def _make_transport():
    """Create an XML-RPC transport with a long timeout for eval_task calls."""
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
    """Authenticate with Odoo and return uid. Caches after first success."""
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
    """Return Odoo object endpoint proxy with extended timeout."""
    transport = _make_transport()
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", transport=transport)


def _call_odoo_method(model, method, record_ids, args=None, kwargs=None):
    """Call a method on Odoo model records via XML-RPC."""
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


def _get_retry_count(properties):
    """Extract retry count from message headers."""
    headers = (properties.headers or {}) if properties else {}
    return headers.get("x-retry-count", 0)


def _republish_with_retry(connection, queue, body, retry_count):
    """Republish message with incremented retry count using the shared connection.

    Must be called from the main pika thread via add_callback_threadsafe.
    """
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


# ── Threaded Worker Functions ─────────────────────────────────────────────────

def _is_permanent_failure(exc):
    """Return True for errors that will never succeed on retry."""
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
    """Return True for 502/504 gateway errors or DB serialization conflicts."""
    msg = str(exc)
    return (
        "502 Bad Gateway" in msg
        or "504 Gateway Time-out" in msg
        or "could not serialize access" in msg
        or "concurrent update" in msg
        or "deadlock detected" in msg
    )


def _retry_delay(retry_count):
    """Exponential backoff: 30s, 60s, 120s, 240s, ..."""
    return min(RETRY_BACKOFF_BASE * (2**retry_count), 600)


def _process_eval(connection, channel, delivery_tag, properties, body):
    """Process a single eval message in a worker thread."""
    retry_count = _get_retry_count(properties)
    record_id = None
    start_time = time.time()
    try:
        message = json.loads(body)
        record_id = message.get("record_id")
        _logger.info(
            "Processing EVAL task for record_id=%s (attempt %d/%d)",
            record_id,
            retry_count + 1,
            MAX_RETRIES,
        )

        if not record_id:
            _logger.error("Missing record_id in eval message: %s", body)
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        _call_odoo_method("preference.ranking", "eval_task", [record_id])

        elapsed = time.time() - start_time
        _logger.info("EVAL task completed for record_id=%s (%.1fs)", record_id, elapsed)

        with _stats_lock:
            _stats["eval_completed"] += 1
            _stats["total_eval_time"] += elapsed
            if _stats["eval_completed"] % 50 == 0:
                _log_stats()

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)

    except Exception as e:
        elapsed = time.time() - start_time
        _logger.error(
            "EVAL task failed for record_id=%s (attempt %d/%d, %.1fs): %s",
            record_id or "?",
            retry_count + 1,
            MAX_RETRIES,
            elapsed,
            e,
        )

        with _stats_lock:
            _stats["eval_failed"] += 1

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)

        if _is_permanent_failure(e):
            _logger.warning(
                "EVAL task DROPPED for record_id=%s (permanent failure, will not retry): %s",
                record_id or "?",
                e,
            )
        elif retry_count + 1 < MAX_RETRIES:
            delay = _retry_delay(retry_count)
            if _is_gateway_error(e):
                delay = max(delay, 60)
                _logger.warning(
                    "EVAL for record_id=%s hit gateway error — retrying in %ds (attempt %d/%d)",
                    record_id,
                    delay,
                    retry_count + 2,
                    MAX_RETRIES,
                )
            else:
                _logger.info(
                    "Re-queuing EVAL task for record_id=%s (retry in %ds)...",
                    record_id,
                    delay,
                )
            time.sleep(delay)
            cb = functools.partial(
                _republish_with_retry, connection, QUEUE_EVAL, body, retry_count
            )
            connection.add_callback_threadsafe(cb)
        else:
            _logger.error(
                "EVAL task PERMANENTLY FAILED for record_id=%s after %d attempts.",
                record_id or "?",
                MAX_RETRIES,
            )


def _process_qc(connection, channel, delivery_tag, properties, body):
    """Process a single QC message in a worker thread."""
    retry_count = _get_retry_count(properties)
    record_id = None
    start_time = time.time()
    try:
        message = json.loads(body)
        record_id = message.get("record_id")
        _logger.info(
            "Processing QC task for record_id=%s (attempt %d/%d)",
            record_id,
            retry_count + 1,
            MAX_RETRIES,
        )

        if not record_id:
            _logger.error("Missing record_id in QC message: %s", body)
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        _call_odoo_method("preference.ranking", "run_qc_checks", [record_id])

        elapsed = time.time() - start_time
        _logger.info("QC task completed for record_id=%s (%.1fs)", record_id, elapsed)

        with _stats_lock:
            _stats["qc_completed"] += 1
            _stats["total_qc_time"] += elapsed
            if _stats["qc_completed"] % 50 == 0:
                _log_stats()

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)

    except Exception as e:
        elapsed = time.time() - start_time
        _logger.error(
            "QC task failed for record_id=%s (attempt %d/%d, %.1fs): %s",
            record_id or "?",
            retry_count + 1,
            MAX_RETRIES,
            elapsed,
            e,
        )

        with _stats_lock:
            _stats["qc_failed"] += 1

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)

        if _is_permanent_failure(e):
            _logger.warning(
                "QC task DROPPED for record_id=%s (permanent failure, will not retry): %s",
                record_id or "?",
                e,
            )
        elif retry_count + 1 < MAX_RETRIES:
            delay = _retry_delay(retry_count)
            if _is_gateway_error(e):
                delay = max(delay, 60)
                _logger.warning(
                    "QC for record_id=%s hit gateway error — retrying in %ds (attempt %d/%d)",
                    record_id,
                    delay,
                    retry_count + 2,
                    MAX_RETRIES,
                )
            else:
                _logger.info(
                    "Re-queuing QC task for record_id=%s (retry in %ds)...",
                    record_id,
                    delay,
                )
            time.sleep(delay)
            cb = functools.partial(
                _republish_with_retry, connection, QUEUE_QC, body, retry_count
            )
            connection.add_callback_threadsafe(cb)
        else:
            _logger.error(
                "QC task PERMANENTLY FAILED for record_id=%s after %d attempts.",
                record_id or "?",
                MAX_RETRIES,
            )


def _ack_message(channel, delivery_tag):
    """Acknowledge a message. Called on pika's IO thread via add_callback_threadsafe."""
    if channel.is_open:
        channel.basic_ack(delivery_tag=delivery_tag)


def _process_reeval(connection, channel, delivery_tag, properties, body):
    retry_count = _get_retry_count(properties)
    record_id = None
    start_time = time.time()
    try:
        message = json.loads(body)
        record_id = message.get("record_id")
        _logger.info(
            "Processing REEVAL task for record_id=%s (attempt %d/%d)",
            record_id,
            retry_count + 1,
            MAX_RETRIES,
        )

        if not record_id:
            _logger.error("Missing record_id in reeval message: %s", body)
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        _call_odoo_method("preference.ranking", "reeval_task", [record_id])

        elapsed = time.time() - start_time
        _logger.info(
            "REEVAL task completed for record_id=%s (%.1fs)", record_id, elapsed
        )

        with _stats_lock:
            _stats["reeval_completed"] += 1
            _stats["total_reeval_time"] += elapsed
            if _stats["reeval_completed"] % 50 == 0:
                _log_stats()

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)

    except Exception as e:
        elapsed = time.time() - start_time
        _logger.error(
            "REEVAL task failed for record_id=%s (attempt %d/%d, %.1fs): %s",
            record_id or "?",
            retry_count + 1,
            MAX_RETRIES,
            elapsed,
            e,
        )

        with _stats_lock:
            _stats["reeval_failed"] += 1

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)

        if _is_permanent_failure(e):
            _logger.warning(
                "REEVAL task DROPPED for record_id=%s (permanent failure, will not retry): %s",
                record_id or "?",
                e,
            )
        elif retry_count + 1 < MAX_RETRIES:
            delay = _retry_delay(retry_count)
            if _is_gateway_error(e):
                delay = max(delay, 60)
                _logger.warning(
                    "REEVAL for record_id=%s hit gateway error — retrying in %ds (attempt %d/%d)",
                    record_id,
                    delay,
                    retry_count + 2,
                    MAX_RETRIES,
                )
            else:
                _logger.info(
                    "Re-queuing REEVAL task for record_id=%s (retry in %ds)...",
                    record_id,
                    delay,
                )
            time.sleep(delay)
            cb = functools.partial(
                _republish_with_retry, connection, QUEUE_REEVAL, body, retry_count
            )
            connection.add_callback_threadsafe(cb)
        else:
            _logger.error(
                "REEVAL task PERMANENTLY FAILED for record_id=%s after %d attempts.",
                record_id or "?",
                MAX_RETRIES,
            )


# ── Message Callbacks (dispatch to thread pool) ──────────────────────────────

def _make_eval_callback(connection, thread_pool):
    """Create a callback that dispatches eval messages to the thread pool."""
    def on_message(channel, method, properties, body):
        thread_pool.submit(
            _process_eval, connection, channel, method.delivery_tag, properties, body
        )

    return on_message


def _make_qc_callback(connection, thread_pool):
    def on_message(channel, method, properties, body):
        thread_pool.submit(
            _process_qc, connection, channel, method.delivery_tag, properties, body
        )

    return on_message


def _make_reeval_callback(connection, thread_pool):
    def on_message(channel, method, properties, body):
        thread_pool.submit(
            _process_reeval,
            connection,
            channel,
            method.delivery_tag,
            properties,
            body,
        )

    return on_message


# ── Consumer Setup ────────────────────────────────────────────────────────────

def start_consumer():
    """Connect to RabbitMQ and start consuming with threaded dispatch."""
    _logger.info("Starting consumer with %d worker threads", WORKER_THREADS)
    _logger.info("Connecting to RabbitMQ at %s:%s...", RABBITMQ_HOST, RABBITMQ_PORT)
    _logger.info("Odoo: %s (DB: %s, User: %s)", ODOO_URL, ODOO_DB, ODOO_USERNAME)

    # Verify Odoo is reachable before starting
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

    channel.queue_declare(queue=QUEUE_EVAL, durable=True)
    channel.queue_declare(queue=QUEUE_QC, durable=True)
    channel.queue_declare(queue=QUEUE_REEVAL, durable=True)

    # Allow up to WORKER_THREADS unacked messages so threads stay busy
    channel.basic_qos(prefetch_count=WORKER_THREADS)

    thread_pool = ThreadPoolExecutor(max_workers=WORKER_THREADS)

    channel.basic_consume(
        queue=QUEUE_EVAL,
        on_message_callback=_make_eval_callback(connection, thread_pool),
    )
    channel.basic_consume(
        queue=QUEUE_QC,
        on_message_callback=_make_qc_callback(connection, thread_pool),
    )
    channel.basic_consume(
        queue=QUEUE_REEVAL,
        on_message_callback=_make_reeval_callback(connection, thread_pool),
    )

    _logger.info(
        "Consumer started. Listening on queues: [%s, %s, %s]",
        QUEUE_EVAL,
        QUEUE_QC,
        QUEUE_REEVAL,
    )
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
