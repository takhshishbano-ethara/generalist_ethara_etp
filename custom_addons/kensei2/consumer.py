#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RabbitMQ Consumer for Kensei2 batch auto-processing pipeline.

This script runs OUTSIDE the Odoo HTTP process as a standalone worker.
It connects to RabbitMQ, listens on the kensei2_auto_process queue, and
triggers batch execution: claim task -> start batch (16 pods) -> poll
until done -> mark complete.

All WS communication and sandbox orchestration is handled server-side
by the batch background workers in kensei2_sandbox.py.

Usage:
    python consumer.py

Environment variables required:
    RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQ_USERNAME, RABBITMQ_PASSWORD, RABBITMQ_VHOST
    ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD
    CONSUMER_WORKERS          -- concurrent worker threads (default 10)
    BATCH_POLL_INTERVAL       -- seconds between batch status polls (default 10)
    BATCH_POLL_TIMEOUT        -- max seconds to wait for batch completion (default 1800)
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

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("kensei2.consumer")

# -- RabbitMQ Config -----------------------------------------------------------
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "rabbit_akshat")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "rabbit_akshat")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

QUEUE_AUTO_PROCESS = "kensei2_auto_process"

WORKER_THREADS = int(os.getenv("CONSUMER_WORKERS", "10"))
BATCH_POLL_INTERVAL = int(os.getenv("BATCH_POLL_INTERVAL", "10"))
BATCH_POLL_TIMEOUT = int(os.getenv("BATCH_POLL_TIMEOUT", "1800"))

# -- Odoo XML-RPC Config ------------------------------------------------------
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "ethara_new")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")
XMLRPC_TIMEOUT = int(os.getenv("XMLRPC_TIMEOUT", "1800"))

# -- Monitoring ----------------------------------------------------------------
_stats_lock = threading.Lock()
_stats = {
    "tasks_completed": 0,
    "tasks_failed": 0,
    "tasks_skipped": 0,
    "total_process_time": 0.0,
}


def _log_stats():
    with _stats_lock:
        tc = _stats["tasks_completed"]
        tf = _stats["tasks_failed"]
        ts = _stats["tasks_skipped"]
        avg = (_stats["total_process_time"] / tc) if tc else 0
    _logger.info(
        "STATS | completed: %d, failed: %d, skipped: %d (avg %.1fs)",
        tc, tf, ts, avg,
    )


# -- Odoo XML-RPC Helpers -----------------------------------------------------
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


def _call_odoo(model, method, record_ids, args=None, kwargs=None):
    uid = _get_odoo_uid()
    models_proxy = _get_odoo_models()
    return models_proxy.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        model, method,
        [record_ids] + (args or []),
        kwargs or {},
    )


def _read_fields(model, record_ids, fields_list):
    uid = _get_odoo_uid()
    models_proxy = _get_odoo_models()
    return models_proxy.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        model, "read",
        [record_ids],
        {"fields": fields_list},
    )


# -- ACK Helpers ---------------------------------------------------------------
def _ack_message(channel, delivery_tag):
    if channel.is_open:
        channel.basic_ack(delivery_tag=delivery_tag)


# -- Batch Polling -------------------------------------------------------------
def _wait_for_batch_done(task_id, timeout=BATCH_POLL_TIMEOUT):
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        records = _read_fields(
            "kensei2.kensei2", [task_id],
            ["batch_status", "batch_error"],
        )
        if not records:
            raise RuntimeError("Task %s not found" % task_id)
        status = records[0].get("batch_status", "")
        if status != last_status:
            _logger.info(
                "Batch status for task %s: %s", task_id, status,
            )
            last_status = status
        if status == "done":
            return "done"
        if status == "error":
            error = records[0].get("batch_error", "unknown error")
            raise RuntimeError(
                "Batch failed for task %s: %s" % (task_id, error)
            )
        time.sleep(BATCH_POLL_INTERVAL)
    raise RuntimeError(
        "Batch timed out after %ds for task %s" % (timeout, task_id)
    )


# -- Worker Function -----------------------------------------------------------
def _process_task(connection, channel, delivery_tag, properties, body):
    task_id = None
    start_time = time.time()

    try:
        message = json.loads(body)
        task_id = message.get("task_id")
        _logger.info("Processing batch task_id=%s", task_id)

        if not task_id:
            _logger.error("Missing task_id in message: %s", body)
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        # 1. Claim task
        claim = _call_odoo(
            "kensei2.kensei2", "auto_process_claim_task", [task_id],
        )
        if claim.get("skip"):
            _logger.info(
                "Skipping task_id=%s: %s",
                task_id, claim.get("reason", "?"),
            )
            with _stats_lock:
                _stats["tasks_skipped"] += 1
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        initial_prompt = claim["initial_prompt"]
        _logger.info(
            "Claimed task_id=%s, prompt_len=%d",
            task_id, len(initial_prompt),
        )

        # 2. Start batch (16 pods: 8 Claude + 8 GPT)
        _logger.info(
            "Starting batch for task_id=%s: %.100s",
            task_id, initial_prompt,
        )
        _call_odoo(
            "kensei2.kensei2", "action_start_batch",
            [task_id], [initial_prompt],
        )

        # 3. Poll batch_status until done/error
        _wait_for_batch_done(task_id, timeout=BATCH_POLL_TIMEOUT)

        # 4. Mark done
        elapsed = time.time() - start_time
        _call_odoo(
            "kensei2.kensei2", "auto_process_mark_done",
            [task_id, "done", ""],
        )
        _logger.info(
            "Task completed: task_id=%s (%.1fs)", task_id, elapsed,
        )

        with _stats_lock:
            _stats["tasks_completed"] += 1
            _stats["total_process_time"] += elapsed
            if _stats["tasks_completed"] % 10 == 0:
                _log_stats()

    except Exception as e:
        elapsed = time.time() - start_time
        _logger.error(
            "Task failed: task_id=%s (%.1fs): %s\n%s",
            task_id or "?", elapsed, e, traceback.format_exc(),
        )
        with _stats_lock:
            _stats["tasks_failed"] += 1

        if task_id:
            try:
                _call_odoo(
                    "kensei2.kensei2", "auto_process_mark_done",
                    [task_id, "failed", str(e)[:2000]],
                )
            except Exception:
                _logger.exception(
                    "Failed to mark task %s as failed", task_id,
                )
    finally:
        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)


# -- Callback Factory ----------------------------------------------------------
def _make_callback(connection, thread_pool):
    def on_message(channel, method, properties, body):
        thread_pool.submit(
            _process_task,
            connection, channel,
            method.delivery_tag, properties, body,
        )

    return on_message


# -- Consumer Setup ------------------------------------------------------------
def start_consumer():
    _logger.info("Starting Kensei2 batch consumer with %d workers", WORKER_THREADS)
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
        heartbeat=1200,
        blocked_connection_timeout=300,
    )

    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.queue_declare(queue=QUEUE_AUTO_PROCESS, durable=True)
    channel.basic_qos(prefetch_count=WORKER_THREADS)

    thread_pool = ThreadPoolExecutor(max_workers=WORKER_THREADS)

    channel.basic_consume(
        queue=QUEUE_AUTO_PROCESS,
        on_message_callback=_make_callback(connection, thread_pool),
    )

    _logger.info(
        "Batch consumer started. Listening on queue: [%s]",
        QUEUE_AUTO_PROCESS,
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
            _logger.error(
                "RabbitMQ connection lost: %s. Reconnecting in 5s...", e,
            )
            time.sleep(5)
        except Exception as e:
            _logger.error(
                "Consumer crashed: %s\n%s", e, traceback.format_exc(),
            )
            _logger.info("Restarting consumer in 10s...")
            time.sleep(10)
