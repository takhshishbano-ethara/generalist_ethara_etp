#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RabbitMQ consumer for ETP Assessment per-question subjective scoring.

Runs OUTSIDE Odoo as a standalone worker. Listens on the score queue and,
for each message, calls etp.assessment.response.rmq_score_subjective(id)
over XML-RPC. One message == one response == one LLM call on its
justification (multimodal on Vertex so the model sees the images).

This mirrors preference_ranking/consumer.py (the house pattern): threaded
dispatch, retry-with-backoff, permanent-failure drop, manual ack.

Usage:
    python consumer.py

Env (with local-dev defaults):
    RABBITMQ_HOST / RABBITMQ_PORT / RABBITMQ_USERNAME / RABBITMQ_PASSWORD /
    RABBITMQ_VHOST / ETP_SCORE_QUEUE
    ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_PASSWORD
    CONSUMER_WORKERS (default 5), CONSUMER_MAX_RETRIES (default 5)
"""
import functools
import json
import logging
import os
import sys
import threading
import time
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor

import pika

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("etp_assessment.consumer")

# ── RabbitMQ ────────────────────────────────────────────────────────────────
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", 5672))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")
QUEUE_SCORE = os.getenv("ETP_SCORE_QUEUE", "etp_assessment_score")

MAX_RETRIES = int(os.getenv("CONSUMER_MAX_RETRIES", "5"))
WORKER_THREADS = int(os.getenv("CONSUMER_WORKERS", "5"))
RETRY_BACKOFF_BASE = int(os.getenv("CONSUMER_RETRY_BACKOFF", "30"))

# ── Odoo XML-RPC ────────────────────────────────────────────────────────────
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "ethara_dev")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")
XMLRPC_TIMEOUT = int(os.getenv("XMLRPC_TIMEOUT", "600"))

_cached_uid = None
_uid_lock = threading.Lock()


def _make_transport():
    transport = (
        xmlrpc.client.SafeTransport()
        if ODOO_URL.startswith("https")
        else xmlrpc.client.Transport()
    )
    orig = transport.make_connection

    def _patched(host):
        conn = orig(host)
        conn.timeout = XMLRPC_TIMEOUT
        return conn

    transport.make_connection = _patched
    return transport


def _get_uid():
    global _cached_uid
    with _uid_lock:
        if _cached_uid is not None:
            return _cached_uid
    common = xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/common", transport=_make_transport())
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError(
            f"Odoo auth failed for {ODOO_USERNAME} on {ODOO_DB}")
    with _uid_lock:
        _cached_uid = uid
    return uid


def _call_odoo(model, method, ids, args=None, kwargs=None):
    uid = _get_uid()
    proxy = xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/object", transport=_make_transport())
    return proxy.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, method,
        [ids] + (args or []), kwargs or {})


def _retry_count(properties):
    headers = (properties.headers or {}) if properties else {}
    return headers.get("x-retry-count", 0)


def _is_permanent(exc):
    msg = str(exc).lower()
    return any(p in msg for p in (
        "record does not exist", "has been deleted",
        "no justification", "access denied", "access error"))


def _retry_delay(n):
    return min(RETRY_BACKOFF_BASE * (2 ** n), 600)


def _ack(channel, tag):
    if channel.is_open:
        channel.basic_ack(delivery_tag=tag)


def _republish(connection, body, retry_count):
    headers = {"x-retry-count": retry_count + 1}
    ch = connection.channel()
    ch.basic_publish(
        exchange="", routing_key=QUEUE_SCORE, body=body,
        properties=pika.BasicProperties(delivery_mode=2, headers=headers))
    ch.close()


def _process(connection, channel, delivery_tag, properties, body):
    retry_count = _retry_count(properties)
    response_id = None
    start = time.time()
    try:
        msg = json.loads(body)
        response_id = msg.get("response_id")
        _logger.info("SCORE response_id=%s (attempt %d/%d)",
                     response_id, retry_count + 1, MAX_RETRIES)
        if not response_id:
            _logger.error("Missing response_id: %s", body)
            connection.add_callback_threadsafe(
                functools.partial(_ack, channel, delivery_tag))
            return

        _call_odoo("etp.assessment.response", "rmq_score_subjective",
                   [response_id])

        _logger.info("SCORE done response_id=%s (%.1fs)",
                     response_id, time.time() - start)
        connection.add_callback_threadsafe(
            functools.partial(_ack, channel, delivery_tag))

    except Exception as e:
        _logger.error("SCORE failed response_id=%s (attempt %d/%d): %s",
                      response_id or "?", retry_count + 1, MAX_RETRIES, e)
        connection.add_callback_threadsafe(
            functools.partial(_ack, channel, delivery_tag))

        if _is_permanent(e):
            _logger.warning("SCORE DROPPED response_id=%s (permanent): %s",
                            response_id or "?", e)
        elif retry_count + 1 < MAX_RETRIES:
            delay = _retry_delay(retry_count)
            _logger.info("Re-queue response_id=%s in %ds", response_id, delay)
            time.sleep(delay)
            connection.add_callback_threadsafe(
                functools.partial(_republish, connection, body, retry_count))
        else:
            _logger.error("SCORE PERMANENTLY FAILED response_id=%s after %d",
                          response_id or "?", MAX_RETRIES)


def main():
    executor = ThreadPoolExecutor(max_workers=WORKER_THREADS)
    credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
    params = pika.ConnectionParameters(
        host=RABBITMQ_HOST, port=RABBITMQ_PORT, virtual_host=RABBITMQ_VHOST,
        credentials=credentials, heartbeat=600,
        blocked_connection_timeout=300)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=QUEUE_SCORE, durable=True)
    channel.basic_qos(prefetch_count=WORKER_THREADS)

    def on_message(ch, method, properties, body):
        executor.submit(_process, connection, ch,
                        method.delivery_tag, properties, body)

    channel.basic_consume(queue=QUEUE_SCORE, on_message_callback=on_message)
    _logger.info(
        "ETP scoring consumer up — queue=%s workers=%d odoo=%s db=%s",
        QUEUE_SCORE, WORKER_THREADS, ODOO_URL, ODOO_DB)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        _logger.info("Shutting down…")
        channel.stop_consuming()
        executor.shutdown(wait=True)
        connection.close()


if __name__ == "__main__":
    main()
