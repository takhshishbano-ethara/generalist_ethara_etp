# -*- coding: utf-8 -*-
"""Remove stale top-level Kensei navbar menus that duplicate the Kensei Tracker
submenu.

An earlier layout exposed "Dashboard", "Task Allocation" (and "Analytics") as
direct children of the Kensei app root, so they appeared as top-level navbar
tabs. They were reorganised under the "Kensei Tracker" submenu, but the old
ir.ui.menu records can linger in existing databases and still show in the
navbar — appearing twice. The canonical copies now live under "Kensei Tracker";
this drops the orphaned top-level ones.

Safe by construction: it only deletes menus whose parent is the Kensei app root
AND whose name is one of the stale set. The Kensei Tracker copies have a
different parent (menu_kensei_tracker_root), so they are never matched. Remove a
name from _STALE_TOP_LEVEL if you actually want to keep it.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Names of the menus that used to sit directly under the Kensei app root and are
# no longer defined in the module's XML.
_STALE_TOP_LEVEL = ("Dashboard", "Task Allocation", "Analytics")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    root = env.ref("kensei.menu_kensei_root", raise_if_not_found=False)
    if not root:
        return
    stale = env["ir.ui.menu"].search([
        ("parent_id", "=", root.id),
        ("name", "in", list(_STALE_TOP_LEVEL)),
    ])
    count = len(stale)
    if stale:
        stale.unlink()
    _logger.info("kensei tracker: removed %s stale top-level navbar menu(s)", count)
