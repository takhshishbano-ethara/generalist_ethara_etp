import json
import logging
import os
from typing import Optional

_logger = logging.getLogger(__name__)


def _pika_connection_params():
    try:
        import pika
    except ImportError:
        raise RuntimeError("pika required for RabbitMQ; pip install pika")
    return pika.ConnectionParameters(
        host=os.environ.get("RABBITMQ_HOST", "localhost"),
        port=int(os.environ.get("RABBITMQ_PORT", "5672")),
        virtual_host=os.environ.get("RABBITMQ_VHOST", "/"),
        credentials=pika.PlainCredentials(
            os.environ.get("RABBITMQ_USERNAME", "guest"),
            os.environ.get("RABBITMQ_PASSWORD", "guest"),
        ),
        heartbeat=600,
        blocked_connection_timeout=300,
    )


def publish(queue: str, payload: dict) -> bool:
    try:
        import pika
        params = _pika_connection_params()
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        channel.queue_declare(queue=queue, durable=True)
        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=json.dumps(payload).encode("utf-8"),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        connection.close()
        return True
    except Exception as exc:
        _logger.warning("RabbitMQ publish to %s failed: %s", queue, exc)
        return False
