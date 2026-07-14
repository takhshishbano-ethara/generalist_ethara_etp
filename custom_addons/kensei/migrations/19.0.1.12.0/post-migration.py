# -*- coding: utf-8 -*-
"""Retire the Kensei Tracker from this module — it now lives in `kensei2`.

The Tracker's models, views, controller, security and assets have been moved to
the `kensei2` module. This tears down what they left behind in THIS one.

ORDERING — THE PART THAT MATTERS
--------------------------------
kensei2's own migration (kensei2/migrations/19.0.1.3.0/post-migration.py) COPIES the
live Tracker rows out of `kensei_tracker_allocation` / `kensei_tracker_team_member`
before they can be lost. This script must not run before that copy has happened, or
the data is gone.

It does not have to: this script does not drop the tables. It only removes the
ir.model / ir.model.fields / ir.model.data bookkeeping so Odoo stops treating the
Tracker as part of `kensei`, and deletes the module's own security records. The
PHYSICAL TABLES ARE DELIBERATELY LEFT IN PLACE — orphaned but intact — so that:

  * kensei2's migration can still read them whichever order the two modules are
    upgraded in, and
  * if anything goes wrong, the data is still on disk and recoverable.

Dropping them is a separate, explicit decision. See the note at the bottom.

SAFETY
------
A record rule with NO groups is GLOBAL in Odoo — it applies to every user. So the
Tracker's rules are DELETED, never orphaned. Same for its ACLs: a NULL group on an
ir.model.access means "everybody".
"""
import logging

_logger = logging.getLogger(__name__)

# Models the Tracker owned inside `kensei`. They are defined by `kensei2` now.
TRACKER_MODELS = [
    "kensei.tracker.allocation",
    "kensei.tracker.team.member",
    "kensei.tracker.bulk.allocation",
    "kensei.tracker.bulk.allocation.line",
    "kensei.tracker.team.import",
    "kensei.tracker.stage.handoff",
    "kensei.persona.import",
]

# Tracker-only fields that used to hang off the (still existing) kensei.persona.
# NB: l1_category / l2_category are NOT here — they are CORE kensei.persona fields
# (kensei/views/persona_views.xml renders them). Only the allocation-derived
# projections below were the Tracker's.
PERSONA_TRACKER_FIELDS = [
    "allocation_ids", "allocation_count", "assignment_status",
    "current_allocation_id", "current_task_ref", "current_tasker_id",
    "current_pl_id", "current_status", "current_stage",
]


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return cr.fetchone()[0] is not None


def migrate(cr, version):
    if not version:
        return

    # ---- 1. did kensei2 already take the data? Warn loudly if not. ----
    if _table_exists(cr, "kensei_tracker_allocation"):
        cr.execute("SELECT count(*) FROM kensei_tracker_allocation")
        src = cr.fetchone()[0]
        dst = 0
        if _table_exists(cr, "kensei2_tracker_allocation"):
            cr.execute("SELECT count(*) FROM kensei2_tracker_allocation")
            dst = cr.fetchone()[0]
        if src and not dst:
            _logger.warning(
                "kensei: %s Tracker allocation(s) are still in kensei_tracker_allocation "
                "and NONE are in kensei2_tracker_allocation yet. The tables are being "
                "LEFT IN PLACE, so nothing is lost — upgrade kensei2 to copy them across.",
                src)
        else:
            _logger.info(
                "kensei: Tracker data — %s row(s) in kensei, %s in kensei2.", src, dst)

    # ---- 2. record rules — DELETE, never orphan (a group-less rule is GLOBAL) ----
    cr.execute(
        """SELECT r.id FROM ir_rule r
            JOIN ir_model m ON m.id = r.model_id
            WHERE m.model = ANY(%s)""", (TRACKER_MODELS,))
    rule_ids = [r[0] for r in cr.fetchall()]
    if rule_ids:
        cr.execute("DELETE FROM rule_group_rel WHERE rule_group_id = ANY(%s)", (rule_ids,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.rule' AND res_id = ANY(%s)",
            (rule_ids,))
        cr.execute("DELETE FROM ir_rule WHERE id = ANY(%s)", (rule_ids,))
        _logger.info("kensei: removed %s Tracker record rule(s).", len(rule_ids))

    # ---- 3. ACLs — DELETE, never NULL the group (NULL = everybody) ----
    cr.execute(
        """DELETE FROM ir_model_access a
            USING ir_model m
            WHERE m.id = a.model_id AND m.model = ANY(%s)""", (TRACKER_MODELS,))

    # ---- 4. the Tracker's ir.model / ir.model.fields bookkeeping ----
    # Removing the ir_model row is what stops Odoo treating these as `kensei`
    # models. ON DELETE CASCADE takes the field rows with it.
    cr.execute(
        """DELETE FROM ir_model_data
            WHERE module = 'kensei'
              AND model = 'ir.model'
              AND res_id IN (SELECT id FROM ir_model WHERE model = ANY(%s))""",
        (TRACKER_MODELS,))
    cr.execute("DELETE FROM ir_model WHERE model = ANY(%s)", (TRACKER_MODELS,))
    _logger.info("kensei: unregistered %s Tracker model(s).", len(TRACKER_MODELS))

    # ---- 5. the tracker-only fields that lived on kensei.persona ----
    cr.execute(
        """DELETE FROM ir_model_fields f
            USING ir_model m
            WHERE m.id = f.model_id
              AND m.model = 'kensei.persona'
              AND f.name = ANY(%s)""", (PERSONA_TRACKER_FIELDS,))
    if _table_exists(cr, "kensei_persona"):
        for col in PERSONA_TRACKER_FIELDS:
            cr.execute(
                'ALTER TABLE kensei_persona DROP COLUMN IF EXISTS "%s"' % col)

    # ---- 6. the Tracker's ir.sequence (Task IDs are UUIDs in kensei2) ----
    cr.execute("DELETE FROM ir_sequence WHERE code = 'kensei.tracker.allocation'")

    # ---- 7. the Tracker's menus, actions and views ----
    #
    # DELETE THE RECORDS, NOT JUST THEIR ir_model_data.
    #
    # This is the whole trap. Odoo knows to remove a module's obsolete records by
    # looking them up in ir_model_data — so deleting those bookkeeping rows FIRST
    # does not clean anything up, it ORPHANS the records permanently: the menu, the
    # action and the view survive with nothing left to point Odoo at them. The menu
    # then still renders in the UI, its action still names `kensei.tracker.allocation`,
    # and clicking it 404s with
    #     KeyError: 'kensei.tracker.allocation'   ->   RPC_ERROR 404: Not Found
    # because the model is gone.
    #
    # So key the cleanup on the CONDITION (records referencing a model this module no
    # longer defines), not on ir_model_data, and delete in dependency order:
    # menus point at actions, actions and views point at models.
    tracker_ir_models = tuple(TRACKER_MODELS)

    # 7a. menus whose action targets a dead Tracker model
    cr.execute(
        """DELETE FROM ir_ui_menu m
            USING ir_act_window a
            WHERE m.action = 'ir.actions.act_window,' || a.id
              AND a.res_model = ANY(%s)""", (TRACKER_MODELS,))
    # 7b. the Tracker's client-action menus (Dashboard / Daily Tracker) and its
    #     now-childless root menu. Matched by xmlid rather than by label, so a menu
    #     from another module that merely says "Tracker" is never touched.
    cr.execute(
        """DELETE FROM ir_ui_menu
            WHERE id IN (
                SELECT res_id FROM ir_model_data
                 WHERE module = 'kensei' AND model = 'ir.ui.menu'
                   AND (name LIKE '%%tracker%%' OR name LIKE '%%persona_import%%'))""")
    cr.execute(
        """DELETE FROM ir_act_client
            WHERE tag IN ('kensei_dashboard', 'kensei_tracker_dashboard',
                          'kensei_tasker_dashboard', 'kensei_tracker_daily')""")

    # 7c. window actions + views bound to the dead models
    cr.execute("DELETE FROM ir_act_window WHERE res_model = ANY(%s)", (TRACKER_MODELS,))
    cr.execute("DELETE FROM ir_ui_view WHERE model = ANY(%s)", (TRACKER_MODELS,))

    # 7d. and ONLY NOW the bookkeeping, once the records themselves are gone
    cr.execute(
        """DELETE FROM ir_model_data
            WHERE module = 'kensei'
              AND (name LIKE '%%tracker%%' OR name LIKE '%%persona_import%%')""")
    _logger.info("kensei: removed the Tracker's menus, actions and views.")

    # NOTE — the physical tables kensei_tracker_allocation, kensei_tracker_team_member,
    # kensei_tracker_bulk_allocation(_line), kensei_tracker_team_import,
    # kensei_tracker_stage_handoff and kensei_persona_import are intentionally NOT
    # dropped. They are orphaned but intact, so kensei2's migration can read them in
    # either upgrade order and the data stays recoverable. Once you have verified the
    # Tracker in kensei2, drop them by hand:
    #
    #   DROP TABLE IF EXISTS kensei_tracker_allocation CASCADE;
    #   DROP TABLE IF EXISTS kensei_tracker_team_member CASCADE;
    #   DROP TABLE IF EXISTS kensei_tracker_bulk_allocation_line CASCADE;
    #   DROP TABLE IF EXISTS kensei_tracker_bulk_allocation CASCADE;
    #   DROP TABLE IF EXISTS kensei_tracker_team_import CASCADE;
    #   DROP TABLE IF EXISTS kensei_tracker_stage_handoff CASCADE;
    #   DROP TABLE IF EXISTS kensei_persona_import CASCADE;
