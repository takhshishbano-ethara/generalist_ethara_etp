"""
RabbitMQ Service for Jaeger pipeline job dispatch.

Provides helpers to publish messages with persistent connection pooling
and batch-publish support. Follows the preference_ranking pattern.
"""
import json
import logging
import os
import threading
import time

try:
    import pika
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pika = None

_logger = logging.getLogger(__name__)

# -- Connection parameters (from env or Odoo ir.config_parameter) -----------
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

# Queue names ---------------------------------------------------------------
QUEUE_SCRAPE = "jaeger_scrape"
QUEUE_DOCKER = "jaeger_docker"
QUEUE_TEST = "jaeger_test"
QUEUE_FINALIZE = "jaeger_finalize"
QUEUE_TRAJECTORY = "jaeger_trajectory"
QUEUE_EXPORT = "jaeger_export"

ALL_QUEUES = [
    QUEUE_SCRAPE, QUEUE_DOCKER, QUEUE_TEST,
    QUEUE_FINALIZE, QUEUE_TRAJECTORY, QUEUE_EXPORT,
]

# -- Thread-safe persistent connection pool ----------------------------------
_conn_lock = threading.Lock()
_connection = None
_channel = None


def _get_channel():
    """Return a reusable channel, reconnecting if the connection dropped."""
    if pika is None:
        raise RuntimeError(
            "pika is not installed. RabbitMQ dispatch requires: pip install pika"
        )
    global _connection, _channel
    with _conn_lock:
        if _connection and _connection.is_open and _channel and _channel.is_open:
            return _channel

        # Close stale connection if it exists
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

        # Declare all queues as durable on (re)connect
        for q in ALL_QUEUES:
            _channel.queue_declare(queue=q, durable=True)

        _logger.info(
            "RabbitMQ connection established (%s:%s), declared %d queues",
            RABBITMQ_HOST, RABBITMQ_PORT, len(ALL_QUEUES),
        )
        return _channel


def _publish(queue, message_body):
    """Publish a single message to a queue using the persistent connection.

    Uses delivery_mode=2 for persistent messages that survive broker restarts.
    """
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


# -- Per-queue publish functions ---------------------------------------------

def publish_scrape_task(repo_id):
    try:
        message = json.dumps({
            "record_id": repo_id,
            "action": "scrape",
        })
        _publish(QUEUE_SCRAPE, message)
        _logger.info("Published scrape task for repo_id=%s", repo_id)
    except Exception as e:
        _logger.error("Failed to publish scrape task for repo_id=%s: %s", repo_id, e)
        raise


def publish_docker_task(repo_id):
    """Publish a single repo to the Docker build queue."""
    try:
        message = json.dumps({"record_id": repo_id, "action": "docker"})
        _publish(QUEUE_DOCKER, message)
        _logger.info("Published docker task for repo_id=%s", repo_id)
    except Exception as e:
        _logger.error("Failed to publish docker task for repo_id=%s: %s", repo_id, e)
        raise


def publish_test_task(instance_id):
    """Publish a single instance to the test execution queue.

    Note: this takes an instance_id (jaeger.instance), not a repo_id.
    """
    try:
        message = json.dumps({"record_id": instance_id, "action": "test"})
        _publish(QUEUE_TEST, message)
        _logger.info("Published test task for instance_id=%s", instance_id)
    except Exception as e:
        _logger.error("Failed to publish test task for instance_id=%s: %s", instance_id, e)
        raise


def publish_finalize_task(repo_id):
    """Publish a single repo to the dataset finalization queue."""
    try:
        message = json.dumps({"record_id": repo_id, "action": "finalize"})
        _publish(QUEUE_FINALIZE, message)
        _logger.info("Published finalize task for repo_id=%s", repo_id)
    except Exception as e:
        _logger.error("Failed to publish finalize task for repo_id=%s: %s", repo_id, e)
        raise


def publish_trajectory_task(repo_id):
    """Publish a single repo to the trajectory dispatch queue."""
    try:
        message = json.dumps({"record_id": repo_id, "action": "trajectory"})
        _publish(QUEUE_TRAJECTORY, message)
        _logger.info("Published trajectory task for repo_id=%s", repo_id)
    except Exception as e:
        _logger.error("Failed to publish trajectory task for repo_id=%s: %s", repo_id, e)
        raise


def publish_export_task(repo_id):
    """Publish a single repo to the Meta export queue."""
    try:
        message = json.dumps({"record_id": repo_id, "action": "export"})
        _publish(QUEUE_EXPORT, message)
        _logger.info("Published export task for repo_id=%s", repo_id)
    except Exception as e:
        _logger.error("Failed to publish export task for repo_id=%s: %s", repo_id, e)
        raise


# -- Batch publishing --------------------------------------------------------

BATCH_CHUNK_SIZE = int(os.getenv("RABBITMQ_BATCH_CHUNK", "50"))
BATCH_CHUNK_DELAY = float(os.getenv("RABBITMQ_CHUNK_DELAY", "0.1"))


def _batch_publish(queue, record_ids, action):
    """Generic batch publish with chunking and backpressure.

    Publishes BATCH_CHUNK_SIZE messages at a time, pausing BATCH_CHUNK_DELAY
    seconds between chunks so RabbitMQ can flush to disk.
    """
    total = len(record_ids)
    published = 0
    for chunk_start in range(0, total, BATCH_CHUNK_SIZE):
        chunk = record_ids[chunk_start : chunk_start + BATCH_CHUNK_SIZE]
        try:
            ch = _get_channel()
            for rid in chunk:
                message = json.dumps({"record_id": rid, "action": action})
                ch.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)
            _logger.info(
                "Batch-published chunk %d-%d / %d %s tasks",
                chunk_start + 1,
                chunk_start + len(chunk),
                total,
                action,
            )
        except (
            pika.exceptions.AMQPConnectionError,
            pika.exceptions.AMQPChannelError,
        ) as e:
            _logger.warning(
                "Connection lost at chunk %d, reconnecting: %s", chunk_start, e,
            )
            global _connection, _channel
            with _conn_lock:
                _connection = None
                _channel = None
            ch = _get_channel()
            for rid in chunk:
                message = json.dumps({"record_id": rid, "action": action})
                ch.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=message,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            published += len(chunk)

        if chunk_start + BATCH_CHUNK_SIZE < total:
            time.sleep(BATCH_CHUNK_DELAY)

    _logger.info("Batch-published %d/%d %s tasks", published, total, action)


def batch_publish_scrape_tasks(repo_ids):
    """Batch-publish repos to the scrape queue."""
    _batch_publish(QUEUE_SCRAPE, repo_ids, "scrape")


def batch_publish_docker_tasks(repo_ids):
    """Batch-publish repos to the Docker build queue."""
    _batch_publish(QUEUE_DOCKER, repo_ids, "docker")


def batch_publish_test_tasks(instance_ids):
    """Batch-publish instances to the test execution queue."""
    _batch_publish(QUEUE_TEST, instance_ids, "test")


def batch_publish_finalize_tasks(repo_ids):
    """Batch-publish repos to the dataset finalization queue."""
    _batch_publish(QUEUE_FINALIZE, repo_ids, "finalize")


def batch_publish_trajectory_tasks(repo_ids):
    """Batch-publish repos to the trajectory dispatch queue."""
    _batch_publish(QUEUE_TRAJECTORY, repo_ids, "trajectory")


def batch_publish_export_tasks(repo_ids):
    """Batch-publish repos to the Meta export queue."""
    _batch_publish(QUEUE_EXPORT, repo_ids, "export")
