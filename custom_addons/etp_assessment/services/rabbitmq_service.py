# -*- coding: utf-8 -*-
"""RabbitMQ publisher for batched per-evaluator subjective scoring.

One message per CANDIDATE (etp.assessment.evaluator), not per response.
The consumer (consumer.py) pulls each message and calls
etp.assessment.evaluator.action_llm_score(), which scores all of that
candidate's needs_llm responses in a single Vertex call.

Concurrency cap = worker count: 10 workers on the consumer means at most
10 Vertex calls in flight. With 400-500 concurrent candidate submits, the
queue absorbs the burst and workers drain it at a controlled rate — no
LLM rate-limit blow-out, no system thrash.

Topology (declared idempotently on first use):
    etp_assessment_score          main queue, dead-letters to .dead
    etp_assessment_score.dlx      direct exchange for dead letters
    etp_assessment_score.dead     audit queue for permanently-failed msgs

Connection params:
    Required env vars (refuses to publish if any missing):
        RABBITMQ_HOST, RABBITMQ_USERNAME, RABBITMQ_PASSWORD
    Optional:
        RABBITMQ_PORT (5672), RABBITMQ_VHOST (/), ETP_SCORE_QUEUE
        ETP_SCORE_DLQ, ETP_SCORE_DLX, RABBITMQ_BATCH_CHUNK (50)

Publishing is best-effort: callers catch the exception and fall back to
llm_state='pending' so the in-Odoo cron drainer scores it inline. Local
dev with no broker continues to work via the cron path.
"""
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

QUEUE_SCORE = os.getenv("ETP_SCORE_QUEUE", "etp_assessment_score")
DLX_EXCHANGE = os.getenv("ETP_SCORE_DLX", "etp_assessment_score.dlx")
DLQ_QUEUE = os.getenv("ETP_SCORE_DLQ", "etp_assessment_score.dead")

BATCH_CHUNK_SIZE = int(os.getenv("RABBITMQ_BATCH_CHUNK", "50"))
BATCH_CHUNK_DELAY = float(os.getenv("RABBITMQ_CHUNK_DELAY", "0.1"))

_conn_lock = threading.Lock()
_connection: Optional[pika.BlockingConnection] = None
_channel = None


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            "Environment variable %s is required for the ETP Assessment "
            "RabbitMQ pipeline. Configure it on the Odoo host or in the "
            "consumer .env file." % name
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
    """Idempotent: DLX + DLQ + main queue bound to DLX on permanent failure."""
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
        queue=QUEUE_SCORE,
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
            "etp_assessment RabbitMQ connected (queue=%s, dlq=%s)",
            QUEUE_SCORE, DLQ_QUEUE)
        return _channel


def _reset_connection() -> None:
    global _connection, _channel
    with _conn_lock:
        _connection = None
        _channel = None


def _basic_properties(retry_count: int = 0) -> pika.BasicProperties:
    return pika.BasicProperties(
        delivery_mode=2,
        headers={"x-retry-count": retry_count},
        content_type="application/json",
    )


def _serialize(evaluator_id: int) -> bytes:
    payload = {
        "evaluator_id": int(evaluator_id),
        "action": "score",
        "published_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    return json.dumps(payload).encode("utf-8")


def queue_stats(queue_name: Optional[str] = None) -> dict:
    """Return {ready, consumers} for the score queue (and DLQ) — best-effort.

    Uses passive=True queue_declare which returns counts without altering
    topology, so it's safe to call from a monitoring endpoint or a cron.
    Returns {'error': '...'} when the broker is unreachable — never raises,
    so a stats poll can't bring down the publisher.
    """
    try:
        ch = _get_channel()
        main = ch.queue_declare(
            queue=queue_name or QUEUE_SCORE, durable=True, passive=True,
        )
        dead = ch.queue_declare(queue=DLQ_QUEUE, durable=True, passive=True)
        return {
            "queue": main.method.queue,
            "ready": main.method.message_count,
            "consumers": main.method.consumer_count,
            "dlq_ready": dead.method.message_count,
            "dlq_consumers": dead.method.consumer_count,
        }
    except Exception as exc:
        _logger.warning("queue_stats failed: %s", exc)
        return {"error": str(exc)}


def publish_evaluator_score_task(
    evaluator_id: int, retry_count: int = 0,
) -> None:
    """Publish ONE batched-scoring task for an evaluator (whole candidate).

    One reconnect attempt on AMQP connection/channel error; further failure
    propagates so the caller can fall back to llm_state='pending'.
    """
    body = _serialize(evaluator_id)
    try:
        ch = _get_channel()
        ch.basic_publish(
            exchange="",
            routing_key=QUEUE_SCORE,
            body=body,
            properties=_basic_properties(retry_count),
        )
    except (
        pika.exceptions.AMQPConnectionError,
        pika.exceptions.AMQPChannelError,
    ) as exc:
        _logger.warning(
            "etp_assessment publish lost connection (eval=%s); "
            "reconnecting: %s", evaluator_id, exc,
        )
        _reset_connection()
        ch = _get_channel()
        ch.basic_publish(
            exchange="",
            routing_key=QUEUE_SCORE,
            body=body,
            properties=_basic_properties(retry_count),
        )
    # Production observability: include queue depth + consumer count so a
    # `grep "etp_assessment published"` over the prod log answers both
    # "is the broker reachable?" and "is anything draining the queue?"
    try:
        stats = ch.queue_declare(
            queue=QUEUE_SCORE, durable=True, passive=True,
        )
        size, consumers = stats.method.message_count, stats.method.consumer_count
    except Exception:
        size, consumers = -1, -1
    _logger.info(
        "etp_assessment published evaluator=%s queue=%s size=%d consumers=%d "
        "retry=%d",
        evaluator_id, QUEUE_SCORE, size, consumers, retry_count,
    )


def batch_publish_evaluator_score_tasks(
    evaluator_ids: Iterable[int],
) -> int:
    """Chunked publish (one msg per evaluator). Returns count published.

    Splits into chunks of BATCH_CHUNK_SIZE with a small inter-chunk sleep
    so a burst of 500 submits doesn't slam the broker. Per-chunk reconnect.
    """
    ids: List[int] = [int(eid) for eid in evaluator_ids if eid]
    total = len(ids)
    published = 0
    if total == 0:
        return 0
    for chunk_start in range(0, total, BATCH_CHUNK_SIZE):
        chunk = ids[chunk_start: chunk_start + BATCH_CHUNK_SIZE]
        try:
            ch = _get_channel()
            for eid in chunk:
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_SCORE,
                    body=_serialize(eid),
                    properties=_basic_properties(0),
                )
            published += len(chunk)
            _logger.info(
                "etp_assessment batch-published chunk %d-%d / %d",
                chunk_start + 1, chunk_start + len(chunk), total,
            )
        except (
            pika.exceptions.AMQPConnectionError,
            pika.exceptions.AMQPChannelError,
        ) as exc:
            _logger.warning(
                "etp_assessment connection lost at chunk %d; "
                "reconnecting: %s", chunk_start, exc,
            )
            _reset_connection()
            ch = _get_channel()
            for eid in chunk:
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE_SCORE,
                    body=_serialize(eid),
                    properties=_basic_properties(0),
                )
            published += len(chunk)
        if chunk_start + BATCH_CHUNK_SIZE < total:
            time.sleep(BATCH_CHUNK_DELAY)
    _logger.info(
        "etp_assessment batch-published %d/%d evaluator-score tasks",
        published, total,
    )
    return published


def republish_with_retry(evaluator_id: int, retry_count: int) -> None:
    """Called from consumer worker via add_callback_threadsafe; IO thread."""
    publish_evaluator_score_task(evaluator_id, retry_count=retry_count)
