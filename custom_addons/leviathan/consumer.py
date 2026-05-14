#!/usr/bin/env python3
"""DEPRECATED standalone consumer — REMOVED in 19.0.2.0.0.

This file is intentionally NON-FUNCTIONAL. The Leviathan pipeline no longer
uses RabbitMQ. Batch runs are dispatched directly from Odoo by issuing
``boto3 lambda:Invoke(InvocationType='Event')`` against the extraction
Lambda — up to ``ReservedConcurrentExecutions`` concurrent runs (default 250).

WHAT TO DO INSTEAD
==================

There is no consumer process to run any more. To launch a batch:

1. In Odoo, select N rows in the Leviathan list view.
2. Gear menu → "Run Batch (Parallel)".
3. Odoo fans out N async ``lambda:Invoke`` calls via a 250-wide thread pool.
4. Each Lambda runs the extraction and posts back to the webhook at
   ``/api/v1/leviathan/webhook/extraction-complete``.

EKS DEPLOYMENT NOTES
====================

The old ``consumer.py`` EKS Deployment YAML must be **deleted**. Specifically:

    kubectl -n ethara delete deployment etp-leviathan-consumer  # if it exists
    kubectl -n ethara delete configmap leviathan-consumer-env  # if it exists

The Odoo pod must have IAM permission ``lambda:InvokeFunction`` on the
extraction Lambda's ARN — granted via IRSA (preferred on EKS) or explicit
AWS keys in Odoo settings. See ``docs/EKS_DEPLOYMENT.md``.
"""

import sys


def main() -> int:
    sys.stderr.write(
        "consumer.py was removed in leviathan 19.0.2.0.0.\n"
        "Batches are now dispatched directly from Odoo via async lambda:Invoke.\n"
        "See docs/EKS_DEPLOYMENT.md for the new architecture.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
