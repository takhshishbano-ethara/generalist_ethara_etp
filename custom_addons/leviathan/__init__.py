from . import models
from . import controllers
from . import wizards
from . import log_handler


def post_load():
    # Install the [job=N]-scraping logging handler exactly once per
    # Python process. Idempotent — guarded inside install_handler().
    # Fires in EVERY process that loads this addon: Odoo HTTP workers,
    # Odoo cron workers, AND the standalone PRD worker process (whose
    # boot path imports `odoo.addons.leviathan.*`). That single hook
    # is why one tagged `_logger.info("[leviathan][job=%s] ...")` call
    # anywhere in the addon shows up on the Logs tab without per-site
    # changes.
    log_handler.install_handler()
