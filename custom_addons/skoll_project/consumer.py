#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RabbitMQ Consumer for Skoll auto-processing pipeline.

This script runs OUTSIDE the Odoo HTTP process as a standalone worker.
It connects to RabbitMQ, listens on the skoll_auto_process queue, and
orchestrates: sandbox start → WS connect → send prompt → receive response
→ save → auto-hint QC loop.

Usage:
    python consumer.py

Environment variables required:
    RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQ_USERNAME, RABBITMQ_PASSWORD, RABBITMQ_VHOST
    ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD
    CONSUMER_WORKERS          -- concurrent worker threads (default 10)
    CONSUMER_MAX_RETRIES      -- max retries per message (default 3)
    SANDBOX_START_TIMEOUT     -- seconds to wait for sandbox start (default 300)
    WS_RESPONSE_TIMEOUT       -- seconds to wait for LLM response (default 600)
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
_logger = logging.getLogger("skoll.consumer")

# ── RabbitMQ Config ───────────────────────────────────────────────────────────
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "rabbit_akshat")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "rabbit_akshat")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

QUEUE_AUTO_PROCESS = "skoll_auto_process"

WORKER_THREADS = int(os.getenv("CONSUMER_WORKERS", "10"))
MAX_RETRIES = int(os.getenv("CONSUMER_MAX_RETRIES", "3"))
SANDBOX_START_TIMEOUT = int(os.getenv("SANDBOX_START_TIMEOUT", "300"))
WS_RESPONSE_TIMEOUT = int(os.getenv("WS_RESPONSE_TIMEOUT", "600"))
# ── Odoo XML-RPC Config ──────────────────────────────────────────────────────
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "ethara_new")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")
XMLRPC_TIMEOUT = int(os.getenv("XMLRPC_TIMEOUT", "1800"))

# ── Monitoring ────────────────────────────────────────────────────────────────
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
        tc,
        tf,
        ts,
        avg,
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
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", transport=transport)


def _call_odoo(model, method, record_ids, args=None, kwargs=None):
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


def _read_fields(model, record_ids, fields_list):
    uid = _get_odoo_uid()
    models_proxy = _get_odoo_models()
    return models_proxy.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        model,
        "read",
        [record_ids],
        {"fields": fields_list},
    )


# ── ACK Helpers ───────────────────────────────────────────────────────────────
def _ack_message(channel, delivery_tag):
    if channel.is_open:
        channel.basic_ack(delivery_tag=delivery_tag)


# ── Sandbox Polling ───────────────────────────────────────────────────────────
def _wait_for_sandbox_running(sandbox_id, timeout=SANDBOX_START_TIMEOUT):
    """Poll docker_status until 'running' or 'error'. Returns status string."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        records = _read_fields(
            "skoll.sandbox",
            [sandbox_id],
            ["docker_status", "docker_error"],
        )
        if not records:
            raise RuntimeError("Sandbox %s not found" % sandbox_id)
        status = records[0].get("docker_status", "")
        if status == "running":
            _logger.info("Sandbox %s is running", sandbox_id)
            return "running"
        if status == "error":
            error = records[0].get("docker_error", "")
            raise RuntimeError("Sandbox %s failed to start: %s" % (sandbox_id, error))
        _logger.debug("Sandbox %s status: %s — polling...", sandbox_id, status)
        time.sleep(5)
    raise RuntimeError("Sandbox %s start timed out after %ds" % (sandbox_id, timeout))


# ── Worker Function ───────────────────────────────────────────────────────────
def _process_task(connection, channel, delivery_tag, properties, body):
    """Full orchestration for one task."""
    from ws_client import OpenClawClient, OpenClawError, OpenClawTimeoutError

    task_id = None
    start_time = time.time()
    ws_client = None

    try:
        message = json.loads(body)
        task_id = message.get("task_id")
        _logger.info("Processing auto_process task_id=%s", task_id)

        if not task_id:
            _logger.error("Missing task_id in message: %s", body)
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        # 1. Claim task
        claim = _call_odoo("skoll.skoll", "auto_process_claim_task", [task_id])
        if claim.get("skip"):
            _logger.info("Skipping task_id=%s: %s", task_id, claim.get("reason", "?"))
            with _stats_lock:
                _stats["tasks_skipped"] += 1
            cb = functools.partial(_ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return

        sandbox_id = claim["sandbox_id"]
        initial_prompt = claim["initial_prompt"]
        docker_status = claim.get("docker_status", "stopped")

        _logger.info(
            "Claimed task_id=%s sandbox_id=%s docker_status=%s",
            task_id,
            sandbox_id,
            docker_status,
        )

        # 2. Start sandbox if needed
        if docker_status != "running":
            _logger.info("Starting sandbox %s...", sandbox_id)
            _call_odoo("skoll.sandbox", "action_start_sandbox", [sandbox_id])
            _wait_for_sandbox_running(sandbox_id, timeout=SANDBOX_START_TIMEOUT)

        # 3. Get WS info
        ws_info = _call_odoo("skoll.sandbox", "auto_process_get_ws_info", [sandbox_id])
        if ws_info.get("error"):
            raise RuntimeError("WS info error: %s" % ws_info["error"])

        ws_url = ws_info["ws_url"]
        gateway_token = ws_info["gateway_token"]

        # 4. Connect to OpenClaw
        _logger.info("Connecting to OpenClaw: %s (sandbox=%s)", ws_url, sandbox_id)
        ws_client = OpenClawClient(ws_url, gateway_token, sandbox_id)

        connect_retries = 3
        for attempt in range(connect_retries):
            try:
                ws_client.connect(timeout=30)
                break
            except (OpenClawError, OpenClawTimeoutError) as e:
                if attempt < connect_retries - 1:
                    _logger.warning(
                        "WS connect failed (attempt %d/%d): %s",
                        attempt + 1,
                        connect_retries,
                        e,
                    )
                    time.sleep(5)
                else:
                    raise

        # 5. Create turn + send prompt
        _logger.info(
            "Sending initial_prompt (task=%s, sandbox=%s): %.100s",
            task_id,
            sandbox_id,
            initial_prompt,
        )
        turn_result = _call_odoo(
            "skoll.sandbox",
            "auto_process_create_turn",
            [sandbox_id, initial_prompt],
        )
        if turn_result.get("error"):
            raise RuntimeError("create_turn failed: %s" % turn_result["error"])
        turn_id = turn_result["turn_id"]

        ws_client.send_message(initial_prompt)

        # 6. Wait for response
        _logger.info(
            "Waiting for response (sandbox=%s, turn=%s)...", sandbox_id, turn_id
        )
        response = ws_client.wait_for_response(timeout=WS_RESPONSE_TIMEOUT)
        _logger.info(
            "Response received (sandbox=%s, turn=%s): %d chars",
            sandbox_id,
            turn_id,
            len(response.text),
        )

        # 7. Save response
        _call_odoo(
            "skoll.sandbox",
            "auto_process_save_response",
            [turn_id, response.text, response.tool_calls_json, False],
        )

        # 8. Fetch and save trajectory
        try:
            history = ws_client.fetch_history()
            if history:
                _call_odoo(
                    "skoll.sandbox",
                    "auto_process_save_trajectory",
                    [sandbox_id, turn_id, json.dumps(history)],
                )
        except Exception as e:
            _logger.warning("fetch_history failed (non-fatal): %s", e)

        # 8b. Save sub-agent messages + spawn tree (real-time capture + child history fallback)
        try:
            time.sleep(2)

            child_history_msgs = []
            for child_key in list(ws_client.get_child_sessions().keys()):
                try:
                    child_history = ws_client.fetch_child_history(child_key)
                    if child_history:
                        _logger.info(
                            "Fetched %d child history messages for %s (sandbox=%s)",
                            len(child_history),
                            child_key,
                            sandbox_id,
                        )
                        for msg in child_history:
                            if not isinstance(msg, dict):
                                continue
                            child_history_msgs.append({
                                "sessionKey": child_key,
                                "role": msg.get("role", "unknown"),
                                "text": msg.get("text", "") or (
                                    msg.get("content", [{}])[0].get("text", "")
                                    if isinstance(msg.get("content"), list) and msg.get("content")
                                    else ""
                                ),
                                "provenance": msg.get("provenance", {}),
                                "timestamp": msg.get("timestamp"),
                                "kind": "child_history",
                            })
                except Exception as e:
                    _logger.warning("fetch_child_history(%s) failed: %s", child_key, e)

            sub_agent_msgs = ws_client.get_sub_agent_messages()
            if child_history_msgs:
                seen_texts = {(m.get("sessionKey", ""), m.get("text", "")[:200]) for m in sub_agent_msgs}
                for chm in child_history_msgs:
                    dedup_key = (chm.get("sessionKey", ""), chm.get("text", "")[:200])
                    if dedup_key not in seen_texts:
                        sub_agent_msgs.append(chm)
                        seen_texts.add(dedup_key)
                _logger.info(
                    "Merged child history: %d new messages added (sandbox=%s)",
                    len(child_history_msgs),
                    sandbox_id,
                )
            spawn_tree = ws_client.get_spawn_tree()

            if sub_agent_msgs or spawn_tree:
                _call_odoo(
                    "skoll.sandbox",
                    "auto_process_save_sub_agent_messages",
                    [
                        sandbox_id,
                        turn_id,
                        json.dumps(sub_agent_msgs) if sub_agent_msgs else "[]",
                        json.dumps(spawn_tree) if spawn_tree else "[]",
                    ],
                )
                _logger.info(
                    "Saved %d sub-agent messages + spawn tree with %d roots (sandbox=%s)",
                    len(sub_agent_msgs),
                    len(spawn_tree),
                    sandbox_id,
                )
        except Exception as e:
            _logger.warning("save_sub_agent_messages failed (non-fatal): %s", e)

        # 9. Mark done (sandbox stays running)
        elapsed = time.time() - start_time
        _call_odoo("skoll.skoll", "auto_process_mark_done", [task_id, "done", ""])
        _logger.info(
            "Task completed: task_id=%s sandbox=%s (%.1fs)",
            task_id,
            sandbox_id,
            elapsed,
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
            task_id or "?",
            elapsed,
            e,
            traceback.format_exc(),
        )
        with _stats_lock:
            _stats["tasks_failed"] += 1

        if task_id:
            try:
                _call_odoo(
                    "skoll.skoll",
                    "auto_process_mark_done",
                    [task_id, "failed", str(e)[:2000]],
                )
            except Exception:
                _logger.exception("Failed to mark task %s as failed", task_id)

    finally:
        if ws_client:
            try:
                ws_client.disconnect()
            except Exception:
                pass

        cb = functools.partial(_ack_message, channel, delivery_tag)
        connection.add_callback_threadsafe(cb)


# ── Callback Factory ─────────────────────────────────────────────────────────
def _make_callback(connection, thread_pool):
    def on_message(channel, method, properties, body):
        thread_pool.submit(
            _process_task,
            connection,
            channel,
            method.delivery_tag,
            properties,
            body,
        )

    return on_message


# ── Consumer Setup ────────────────────────────────────────────────────────────
def start_consumer():
    _logger.info("Starting Skoll consumer with %d worker threads", WORKER_THREADS)
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

    _logger.info("Consumer started. Listening on queue: [%s]", QUEUE_AUTO_PROCESS)
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
