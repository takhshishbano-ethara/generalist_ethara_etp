# -*- coding: utf-8 -*-
"""Remove the Kensei Tracker's orphaned menus, actions and views.

THE BUG THIS FIXES
------------------
19.0.1.12.0 moved the Tracker to `kensei2` and unregistered its models. It also
deleted the Tracker's rows from `ir_model_data` — and that was exactly backwards.

Odoo removes a module's obsolete records by looking them up in `ir_model_data`.
Deleting those bookkeeping rows first does not clean the records up; it ORPHANS
them permanently. The menu, its action and its views all survived with nothing left
to point Odoo at them. The "Kensei Tracker" menu therefore still rendered, its
action still named `kensei.tracker.allocation`, and clicking it blew up:

    KeyError: 'kensei.tracker.allocation'
    werkzeug.exceptions.NotFound: 404 Not Found
    RPC_ERROR: 404: Not Found   (OwlError in the client)

...because the MODEL is gone but the MENU is not.

WHY A NEW VERSION AND NOT A FIX TO 1.12.0
-----------------------------------------
Migrations only run when the installed version is lower than the manifest version.
Any database that already reached 19.0.1.12.0 is stuck with the orphans and would
never see a corrected 1.12.0 script. So the cleanup lands here, at a version those
databases have not reached yet.

Keyed entirely on the CONDITION — records that reference a model this module no
longer defines — never on ir_model_data, which by now is empty. That makes it work
on a database in ANY state: mid-move, fully moved, or never moved.

Idempotent: on a healthy database it finds nothing and does nothing.
"""
import logging

_logger = logging.getLogger(__name__)

# Models the Tracker used to define inside `kensei`. They live in `kensei2` now.
TRACKER_MODELS = [
    "kensei.tracker.allocation",
    "kensei.tracker.team.member",
    "kensei.tracker.bulk.allocation",
    "kensei.tracker.bulk.allocation.line",
    "kensei.tracker.team.import",
    "kensei.tracker.stage.handoff",
    "kensei.persona.import",
]

# The Tracker's OWL client actions (Dashboard / Daily Tracker / per-tasker view).
# They carry no res_model, so they can only be matched by tag.
TRACKER_CLIENT_TAGS = [
    "kensei_dashboard",
    "kensei_tracker_dashboard",
    "kensei_tasker_dashboard",
    "kensei_tracker_daily",
]


def migrate(cr, version):
    if not version:
        return

    # ---- 1. client actions (Dashboard, Daily Tracker, ...) ----
    cr.execute("DELETE FROM ir_act_client WHERE tag = ANY(%s) RETURNING id",
               (TRACKER_CLIENT_TAGS,))
    client_ids = [r[0] for r in cr.fetchall()]

    # ---- 2. window actions + views bound to the dead models ----
    cr.execute("DELETE FROM ir_act_window WHERE res_model = ANY(%s) RETURNING id",
               (TRACKER_MODELS,))
    window_ids = [r[0] for r in cr.fetchall()]

    cr.execute("DELETE FROM ir_ui_view WHERE model = ANY(%s) RETURNING id",
               (TRACKER_MODELS,))
    view_ids = [r[0] for r in cr.fetchall()]

    # ---- 3. menus whose action we just deleted ----
    # Includes both the window-action menus (Task Allocation, Team Management, …)
    # and the client-action ones (Dashboard, Daily Tracker).
    dangling = (["ir.actions.act_window,%s" % i for i in window_ids]
                + ["ir.actions.client,%s" % i for i in client_ids])
    menu_ids = []
    if dangling:
        cr.execute("DELETE FROM ir_ui_menu WHERE action = ANY(%s) RETURNING id",
                   (dangling,))
        menu_ids = [r[0] for r in cr.fetchall()]

    # ---- 4. the now-childless "Kensei Tracker" root menu ----
    # It has no action of its own, so it can only be found structurally: a kensei
    # menu with no action and no surviving children. Looped, so a deeper tree
    # collapses from the leaves up rather than leaving a stranded middle level.
    while True:
        cr.execute(
            """DELETE FROM ir_ui_menu m
                WHERE m.action IS NULL
                  AND m.name->>'en_US' IN ('Kensei Tracker', 'Tracker')
                  AND NOT EXISTS (
                      SELECT 1 FROM ir_ui_menu c WHERE c.parent_id = m.id)
                RETURNING m.id""")
        gone = [r[0] for r in cr.fetchall()]
        if not gone:
            break
        menu_ids += gone

    # ---- 5. and only now the bookkeeping ----
    for table, ids in (("ir.ui.menu", menu_ids),
                       ("ir.actions.act_window", window_ids),
                       ("ir.actions.client", client_ids),
                       ("ir.ui.view", view_ids)):
        if ids:
            cr.execute(
                "DELETE FROM ir_model_data WHERE model = %s AND res_id = ANY(%s)",
                (table, ids))
    cr.execute(
        """DELETE FROM ir_model_data
            WHERE module = 'kensei'
              AND (name LIKE '%%tracker%%' OR name LIKE '%%persona_import%%')""")

    if menu_ids or window_ids or view_ids or client_ids:
        _logger.warning(
            "kensei: removed the Tracker's orphaned UI — %s menu(s), %s window "
            "action(s), %s client action(s), %s view(s). They referenced models "
            "that now live in kensei2, so opening them 404'd.",
            len(menu_ids), len(window_ids), len(client_ids), len(view_ids))
    else:
        _logger.info("kensei: no orphaned Tracker UI found — nothing to do.")
