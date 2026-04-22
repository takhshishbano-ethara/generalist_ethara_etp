"""
MinIO S3 Endpoint Patch for Jaeger Sandbox
===========================================

Problem:
  worker/s3_helpers.py hardcodes the endpoint_url in _get_client():

      endpoint_url=f"https://s3.{region}.amazonaws.com"

  boto3's AWS_ENDPOINT_URL env var (added in boto3 1.28+) is ONLY used when
  endpoint_url is NOT explicitly passed to the client constructor. Since
  s3_helpers.py passes it explicitly, the env var has no effect.

Solution (ONE-LINE change in s3_helpers.py):
  In _get_client(), change line 27 from:

      endpoint_url=f"https://s3.{region}.amazonaws.com",

  to:

      endpoint_url=os.environ.get("JAEGER_S3_ENDPOINT", f"https://s3.{region}.amazonaws.com"),

  This reads from the JAEGER_S3_ENDPOINT env var when set (e.g., in docker-compose)
  and falls back to the production AWS endpoint when unset. Zero impact on production.

  The docker-compose.yml already sets JAEGER_S3_ENDPOINT=http://minio:9000 on
  the Odoo container.

Alternatively, apply this file as a monkey-patch at sandbox startup:
  python sandbox/s3_patch.py

  But the one-line fix above is strongly recommended instead.
"""
import os
import sys


def patch():
    """Monkey-patch s3_helpers._get_client to respect JAEGER_S3_ENDPOINT."""
    endpoint = os.environ.get("JAEGER_S3_ENDPOINT")
    if not endpoint:
        return

    try:
        from odoo.addons.jaeger.worker import s3_helpers
    except ImportError:
        sys.path.insert(0, "/opt/ethara/app")
        sys.path.insert(0, "/opt/ethara/app/custom_addons")
        from jaeger.worker import s3_helpers

    _original = s3_helpers._get_client

    def _patched_get_client():
        import boto3
        from botocore.config import Config

        region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
        return boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            config=Config(
                retries={"mode": "standard", "max_attempts": 5},
                connect_timeout=30,
                read_timeout=60,
                max_pool_connections=10,
                s3={"addressing_style": "path"},
            ),
        )

    s3_helpers._get_client = _patched_get_client


if __name__ == "__main__":
    patch()
    print("s3_helpers._get_client patched to use JAEGER_S3_ENDPOINT=%s"
          % os.environ.get("JAEGER_S3_ENDPOINT"))
