import importlib
import logging

_logger = logging.getLogger(__name__)

_LANG_MODULES = [
    "odoo.addons.aurora.tools.harness.repos.c",
    "odoo.addons.aurora.tools.harness.repos.cpp",
    "odoo.addons.aurora.tools.harness.repos.golang",
    "odoo.addons.aurora.tools.harness.repos.java",
    "odoo.addons.aurora.tools.harness.repos.javascript",
    "odoo.addons.aurora.tools.harness.repos.python",
    "odoo.addons.aurora.tools.harness.repos.rust",
    "odoo.addons.aurora.tools.harness.repos.typescript",
    "odoo.addons.aurora.tools.harness.repos.ruby",
    "odoo.addons.aurora.tools.harness.repos.php",
    "odoo.addons.aurora.tools.harness.repos.swift",
    "odoo.addons.aurora.tools.harness.repos.kotlin",
    "odoo.addons.aurora.tools.harness.repos.scala",
    "odoo.addons.aurora.tools.harness.repos.csharp",
    "odoo.addons.aurora.tools.harness.repos.html",
]

for _mod in _LANG_MODULES:
    try:
        importlib.import_module(_mod)
    except Exception:
        _logger.warning("Failed to import harness language module %s", _mod, exc_info=True)
