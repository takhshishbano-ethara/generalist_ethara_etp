# -*- coding: utf-8 -*-
import functools
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor

import pika

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger('t2av.consumer')


def _require_env(name):
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            "Environment variable %s is required for the T2AV consumer." % name
        )
    return val


QUEUE_PIPELINE = os.getenv("RABBITMQ_QUEUE", "t2av_pipeline")
DLX_EXCHANGE = os.getenv("RABBITMQ_DLX", "t2av_pipeline.dlx")
DLQ_QUEUE = os.getenv("RABBITMQ_DLQ", "t2av_pipeline.dead")

CONSUMER_WORKERS = int(os.getenv("CONSUMER_WORKERS", "15"))
MAX_RETRIES = int(os.getenv("CONSUMER_MAX_RETRIES", "5"))
RETRY_BACKOFF_BASE = int(os.getenv("CONSUMER_RETRY_BACKOFF", "30"))
RETRY_BACKOFF_CAP = int(os.getenv("CONSUMER_RETRY_BACKOFF_CAP", "600"))
XMLRPC_TIMEOUT = int(os.getenv("XMLRPC_TIMEOUT", "2700"))

ODOO_URL = _require_env('ODOO_URL')
ODOO_DB = _require_env('ODOO_DB')
ODOO_USERNAME = _require_env('ODOO_USERNAME')
ODOO_PASSWORD = _require_env('ODOO_PASSWORD')


_stats_lock = threading.Lock()
_stats = {
    "pipeline_completed": 0,
    "pipeline_failed_permanent": 0,
    "pipeline_failed_transient": 0,
    "total_pipeline_time": 0.0,
}
_shutdown = threading.Event()


def _log_stats():
    with _stats_lock:
        completed = _stats["pipeline_completed"]
        permanent = _stats["pipeline_failed_permanent"]
        transient = _stats["pipeline_failed_transient"]
        avg = (_stats["total_pipeline_time"] / completed) if completed else 0
    _logger.info(
        "STATS | completed=%d permanent_fail=%d transient_fail=%d avg=%.1fs",
        completed, permanent, transient, avg,
    )


_cached_uid = None
_uid_lock = threading.Lock()


def _make_transport():
    transport = (
        xmlrpc.client.SafeTransport()
        if ODOO_URL.startswith("https")
        else xmlrpc.client.Transport()
    )
    original_make_connection = transport.make_connection

    def _patched(host):
        conn = original_make_connection(host)
        conn.timeout = XMLRPC_TIMEOUT
        return conn

    transport.make_connection = _patched
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
    proxy = _get_odoo_models()
    if record_ids:
        method_args = [record_ids] + (args or [])
    else:
        method_args = list(args or [])
    return proxy.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        model,
        method,
        method_args,
        kwargs or {},
    )


def _get_retry_count(properties):
    headers = (properties.headers or {}) if properties else {}
    return int(headers.get("x-retry-count", 0))


def _is_permanent_failure(exc):
    """Substring heuristics for errors that won't succeed on retry."""
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in (
            "permanent failure",
            "record does not exist",
            "has been deleted",
            "missing required fields",
            "access denied",
            "access error",
            "validation error",
            "do not have enough rights",
            "not allowed to access",
        )
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
    return min(RETRY_BACKOFF_BASE * (2 ** retry_count), RETRY_BACKOFF_CAP)


def _ack_message(channel, delivery_tag):
    if channel.is_open:
        channel.basic_ack(delivery_tag=delivery_tag)


def _nack_message_to_dlq(channel, delivery_tag):
    """Reject without requeue so RabbitMQ routes the message to the DLX/DLQ."""
    if channel.is_open:
        channel.basic_nack(delivery_tag=delivery_tag, requeue=False)


def _republish_with_retry(connection, channel, body, retry_count):
    """Must run on the IO thread (called via add_callback_threadsafe)."""
    headers = {"x-retry-count": retry_count + 1}
    publish_channel = connection.channel()
    publish_channel.basic_publish(
        exchange="",
        routing_key=QUEUE_PIPELINE,
        body=body,
        properties=pika.BasicProperties(
            delivery_mode=2,
            headers=headers,
            content_type="application/json",
        ),
    )
    publish_channel.close()


def _process_pipeline(connection, channel, delivery_tag, properties, body):
    retry_count = _get_retry_count(properties)
    record_id = None
    start_time = time.time()
    try:
        message = json.loads(body)
        record_id = message.get("record_id")
        _logger.info(
            "Processing PIPELINE rid=%s (attempt %d/%d, source=%s)",
            record_id, retry_count + 1, MAX_RETRIES, message.get("source", "?"),
        )

        if not record_id:
            _logger.error("Missing record_id in pipeline message: %s", body)
            connection.add_callback_threadsafe(
                functools.partial(_ack_message, channel, delivery_tag)
            )
            return

        _call_odoo_method(
            "t2av.generation", "run_pipeline_sync", [], args=[int(record_id)],
        )

        elapsed = time.time() - start_time
        _logger.info("PIPELINE done rid=%s (%.1fs)", record_id, elapsed)

        with _stats_lock:
            _stats["pipeline_completed"] += 1
            _stats["total_pipeline_time"] += elapsed
            if _stats["pipeline_completed"] % 50 == 0:
                _log_stats()

        connection.add_callback_threadsafe(
            functools.partial(_ack_message, channel, delivery_tag)
        )

    except Exception as exc:
        elapsed = time.time() - start_time
        _logger.error(
            "PIPELINE failed rid=%s (attempt %d/%d, %.1fs): %s",
            record_id or "?", retry_count + 1, MAX_RETRIES, elapsed, exc,
        )

        if _is_permanent_failure(exc):
            _logger.warning(
                "PIPELINE DROP-TO-DLQ rid=%s (permanent): %s",
                record_id or "?", exc,
            )
            with _stats_lock:
                _stats["pipeline_failed_permanent"] += 1
            connection.add_callback_threadsafe(
                functools.partial(_nack_message_to_dlq, channel, delivery_tag)
            )
            return

        with _stats_lock:
            _stats["pipeline_failed_transient"] += 1

        if retry_count + 1 < MAX_RETRIES:
            delay = _retry_delay(retry_count)
            if _is_gateway_error(exc):
                delay = max(delay, 60)
            _logger.info(
                "Re-queuing PIPELINE rid=%s (retry in %ds, attempt %d/%d)",
                record_id, delay, retry_count + 2, MAX_RETRIES,
            )
            time.sleep(delay)
            connection.add_callback_threadsafe(
                functools.partial(_ack_message, channel, delivery_tag)
            )
            connection.add_callback_threadsafe(
                functools.partial(
                    _republish_with_retry, connection, channel, body, retry_count
                )
            )
        else:
            _logger.error(
                "PIPELINE EXHAUSTED rid=%s after %d attempts; routing to DLQ.",
                record_id or "?", MAX_RETRIES,
            )
            connection.add_callback_threadsafe(
                functools.partial(_nack_message_to_dlq, channel, delivery_tag)
            )


def _make_pipeline_callback(connection, thread_pool):
    def on_message(channel, method, properties, body):
        thread_pool.submit(
            _process_pipeline,
            connection, channel, method.delivery_tag, properties, body,
        )
    return on_message


def _declare_topology(channel):
    """Idempotent setup of DLX exchange, DLQ, and main queue bound to DLX."""
    channel.exchange_declare(
        exchange=DLX_EXCHANGE, exchange_type="direct", durable=True,
    )
    channel.queue_declare(queue=DLQ_QUEUE, durable=True)
    channel.queue_bind(
        queue=DLQ_QUEUE, exchange=DLX_EXCHANGE, routing_key=DLQ_QUEUE,
    )
    channel.queue_declare(
        queue=QUEUE_PIPELINE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": DLQ_QUEUE,
        },
    )


def _install_signal_handlers():
    def _handler(signum, _frame):
        _logger.info("Received signal %d; initiating graceful shutdown.", signum)
        _shutdown.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def start_consumer():
    _logger.info(
        "Starting T2AV consumer (workers=%d, max_retries=%d, xmlrpc_timeout=%ds)",
        CONSUMER_WORKERS, MAX_RETRIES, XMLRPC_TIMEOUT,
    )
    _logger.info("Connecting to RabbitMQ at %s...", os.getenv("RABBITMQ_HOST"))
    _logger.info("Odoo: %s (DB=%s, user=%s)", ODOO_URL, ODOO_DB, ODOO_USERNAME)

    uid = _get_odoo_uid()
    _logger.info("Odoo authenticated (uid=%s)", uid)

    credentials = pika.PlainCredentials(
        _require_env("RABBITMQ_USERNAME"),
        _require_env("RABBITMQ_PASSWORD"),
    )
    params = pika.ConnectionParameters(
        host=_require_env("RABBITMQ_HOST"),
        port=int(os.getenv("RABBITMQ_PORT", "5672")),
        virtual_host=os.getenv("RABBITMQ_VHOST", "/"),
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    _declare_topology(channel)
    channel.basic_qos(prefetch_count=CONSUMER_WORKERS)

    thread_pool = ThreadPoolExecutor(max_workers=CONSUMER_WORKERS)
    channel.basic_consume(
        queue=QUEUE_PIPELINE,
        on_message_callback=_make_pipeline_callback(connection, thread_pool),
    )

    _logger.info("Consumer started. Listening on queue '%s'.", QUEUE_PIPELINE)

    try:
        while not _shutdown.is_set():
            connection.process_data_events(time_limit=1)
    except KeyboardInterrupt:
        _logger.info("Consumer interrupted.")
    finally:
        _logger.info("Stopping consumer; draining in-flight workers...")
        try:
            channel.stop_consuming()
        except Exception:  # noqa: BLE001
            pass
        thread_pool.shutdown(wait=True)
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
        _log_stats()
        _logger.info("Consumer stopped.")


if __name__ == "__main__":
    _install_signal_handlers()
    while not _shutdown.is_set():
        try:
            start_consumer()
            break
        except pika.exceptions.AMQPConnectionError as exc:
            _logger.error(
                "RabbitMQ connection lost: %s. Reconnecting in 5s...", exc,
            )
            time.sleep(5)
        except Exception as exc:
            _logger.error(
                "Consumer crashed: %s\n%s", exc, traceback.format_exc(),
            )
            _logger.info("Restarting consumer in 10s...")
            time.sleep(10)
