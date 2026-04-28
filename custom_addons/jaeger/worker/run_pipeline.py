#!/usr/bin/env python3
"""K8s pod entrypoint for Jaeger pipeline.

Bootstraps Odoo and delegates to _run_scrape_pipeline_standalone.
Handles SIGTERM for graceful pod termination.

Environment variables:
    REPO_ID             - jaeger.repository record ID
    ODOO_DB             - PostgreSQL database name
"""
import logging
import os
import signal
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("jaeger.worker")

REPO_ID = int(os.environ["REPO_ID"])
DB_NAME = os.environ["ODOO_DB"]

sys.path.insert(0, "/opt/ethara/app/src")
sys.path.insert(0, "/opt/ethara/app")
sys.path.insert(0, "/opt/ethara/app/custom_addons")

import odoo  # noqa: E402
import odoo.tools.config  # noqa: E402

odoo.tools.config.parse_config([
    "--config", os.environ.get("ODOO_CONF", "/etc/odoo/odoo.conf"),
    "--no-http",
])

for env_key, conf_key in {
    "DB_HOST": "db_host", "DB_PORT": "db_port",
    "DB_USER": "db_user", "DB_PASSWORD": "db_password",
}.items():
    val = os.environ.get(env_key)
    if val:
        odoo.tools.config[conf_key] = val

from odoo.addons.jaeger.worker.pipeline_helpers import PipelineCancelled  # noqa: E402

# ── SIGTERM handling ─────────────────────────────────────────────────────
_cancelled = False


def _sigterm_handler(signum, frame):
    global _cancelled
    _cancelled = True
    _logger.warning("Received SIGTERM — will stop after current step.")


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _sigterm_handler)


def check_cancelled():
    if _cancelled:
        raise PipelineCancelled("Pipeline cancelled (SIGTERM received)")


# ── Chatter helper ───────────────────────────────────────────────────────
def post_chatter(registry, repo_id, body):
    cr = None
    try:
        from odoo import SUPERUSER_ID, api
        cr = registry.cursor()
        env = api.Environment(cr, SUPERUSER_ID, {})
        env["jaeger.repository"].browse(repo_id).message_post(
            body=body, message_type="comment", subtype_xmlid="mail.mt_note",
        )
        cr.commit()
    except Exception:
        _logger.debug("Failed to post chatter for repo=%s", repo_id, exc_info=True)
    finally:
        if cr:
            cr.close()


def main():
    _logger.info("Starting pipeline for repo_id=%s, db=%s", REPO_ID, DB_NAME)

    from odoo.modules.registry import Registry
    registry = Registry(DB_NAME)

    post_chatter(registry, REPO_ID, f"Pipeline started (repo_id={REPO_ID})")

    from odoo.addons.jaeger.worker.pipeline_helpers import (
        _run_scrape_pipeline_standalone,
    )
    _run_scrape_pipeline_standalone(DB_NAME, REPO_ID)

    post_chatter(registry, REPO_ID, "Pipeline completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except PipelineCancelled:
        _logger.warning("Pipeline cancelled for repo %s (SIGTERM)", REPO_ID)
        try:
            from odoo.modules.registry import Registry
            registry = Registry(DB_NAME)
            post_chatter(registry, REPO_ID, "Pipeline cancelled (SIGTERM received).")
        except Exception:
            pass
        sys.exit(0)
    except Exception:
        _logger.exception("Pipeline failed for repo %s", REPO_ID)
        try:
            from odoo.modules.registry import Registry
            registry = Registry(DB_NAME)
            import traceback
            err = traceback.format_exc()[-500:]
            post_chatter(registry, REPO_ID, f"Pipeline failed:\n<pre>{err}</pre>")
        except Exception:
            pass
        sys.exit(1)
    sys.exit(0)
