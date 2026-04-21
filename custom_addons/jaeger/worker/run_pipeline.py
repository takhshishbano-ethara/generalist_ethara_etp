#!/usr/bin/env python3
"""K8s pod entrypoint for Jaeger Phase 1 pipeline.

Bootstraps Odoo and delegates to _run_scrape_pipeline_standalone.

Environment variables:
    REPO_ID             - jaeger.repository record ID
    ODOO_DB             - PostgreSQL database name
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("jaeger.worker")

REPO_ID = int(os.environ["REPO_ID"])
DB_NAME = os.environ["ODOO_DB"]

sys.path.insert(0, "/opt/ethara/app")
sys.path.insert(0, "/opt/ethara/app/custom_addons")

import odoo  # noqa: E402
import odoo.tools.config  # noqa: E402

odoo.tools.config.parse_config([
    "--config", os.environ.get("ODOO_CONF", "/opt/ethara/app/odoo.conf"),
    "--no-http",
])


def main():
    _logger.info("Starting pipeline for repo_id=%s, db=%s", REPO_ID, DB_NAME)
    from odoo.addons.jaeger.models.jaeger_repository import (
        _run_scrape_pipeline_standalone,
    )
    _run_scrape_pipeline_standalone(DB_NAME, REPO_ID)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _logger.exception("Pipeline failed for repo %s", REPO_ID)
        sys.exit(1)
    sys.exit(0)
