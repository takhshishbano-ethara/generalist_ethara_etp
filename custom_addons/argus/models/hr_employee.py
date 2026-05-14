# -*- coding: utf-8 -*-
"""Argus / hr.employee glue — role helper + auto group assignment.

Two responsibilities live on this extension:

1. ``_get_argus_role`` — thin delegate to the canonical
   ``task_forge_bridge._get_task_forge_role`` so Argus and Task Forge
   stay in agreement on who counts as what.  Returns one of
   ``'admin' | 'pl' | 'qr' | 'ql' | 'tasker'``.

2. ``_argus_sync_user_groups`` + lifecycle hooks — whenever an
   employee record is created or its Task Forge hierarchy is edited,
   Argus assigns the matching ``res.groups`` membership to the
   linked ``res.users`` automatically.

   Group mapping (spec)::

       Task Forge role               Argus group
       ─────────────────────────────────────────────────────
       tasker                        argus.group_argus_user
       pl                            argus.group_argus_pl
       ql                            argus.group_argus_ql
       qr + is_ql_type=True          argus.group_argus_ql
       everything else
           (plain qr, admin/CTO,
            missing classification)  argus.group_argus_manager   ← "privilege"

   Before applying the target group we strip ALL other Argus group
   memberships from the user so flipping someone from PL → tasker
   doesn't leave them carrying the elevated group.

Sudo note
---------
Granting / revoking ``res.groups`` membership requires write access
to ``res.users``, which most operators don't have.  We sudo the
whole sync block so the trigger fires regardless of who's editing
the employee.  The *decision* of which group to assign is still
based on the employee's authoritative Task Forge fields — there's
no privilege-escalation surface.
"""

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


# XML-IDs we manage.  Listed here once so both the "strip" pass and
# the "pick target" pass stay in sync — adding a new Argus group
# means adding one entry here and one branch in
# ``_argus_pick_target_group_xmlid``.
_ARGUS_GROUP_XMLIDS = (
    "argus.group_argus_user",
    "argus.group_argus_pl",
    "argus.group_argus_ql",
    "argus.group_argus_manager",
)

# Fields whose change should retrigger a group re-sync — anything
# that can flip the result of ``_get_task_forge_role`` or our local
# QL-type override.
_ARGUS_GROUP_SYNC_TRIGGERS = frozenset({
    "user_id",
    "is_ql_type",
    "task_forge_active",
    "task_forge_pl_id",
    "task_forge_qr_id",
})


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    # ==================================================================
    # Role helper — single source of truth shared with Task Forge
    # ==================================================================
    def _get_argus_role(self):
        """Return ``'admin' | 'pl' | 'qr' | 'ql' | 'tasker'``.

        Delegates to ``task_forge_bridge`` so Argus and Task Forge
        stay in agreement on who counts as what.  If the upstream
        helper is somehow missing (older bridge install) we fall
        back to ``'tasker'`` rather than crash.
        """
        self.ensure_one()
        getter = getattr(self, "_get_task_forge_role", None)
        if not getter:
            return "tasker"
        try:
            return getter() or "tasker"
        except Exception:  # pragma: no cover - defensive
            return "tasker"

    # ==================================================================
    # Lifecycle hooks — keep the user's Argus group in sync with the
    # employee's classification automatically.
    # ==================================================================
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._argus_sync_user_groups()
        return records

    def write(self, vals):
        res = super().write(vals)
        # Cheap pre-filter: only call the sync when a write touched a
        # field that can flip the role classification.  Saving an
        # employee's photo, birthday, or job title should NOT trigger
        # a group rewrite.
        if _ARGUS_GROUP_SYNC_TRIGGERS & set(vals.keys()):
            for rec in self:
                rec._argus_sync_user_groups()
        return res

    # ==================================================================
    # Group picker + sync
    # ==================================================================
    def _argus_pick_target_group_xmlid(self, role):
        """Map a Task Forge role string to an Argus group XML-ID.

        Spec (literal):
            tasker                    → group_argus_user
            pl                        → group_argus_pl
            ql OR (qr + is_ql_type)   → group_argus_ql
            anything else             → group_argus_manager  ("privilege")
        """
        self.ensure_one()
        if role == "tasker":
            return "argus.group_argus_user"
        if role == "pl":
            return "argus.group_argus_pl"
        if role == "ql" or (role == "qr" and self.is_ql_type):
            return "argus.group_argus_ql"
        # Catch-all: plain QR (without is_ql_type), admin / CTO,
        # missing classification.  These get the privileged
        # (manager) group per the spec.
        return "argus.group_argus_manager"

    def _argus_sync_user_groups(self):
        """Assign the right Argus group to ``self.user_id``.

        Safe to call repeatedly — when the user's Argus group is
        already correct we skip the write entirely.  Skips silently
        when the employee has no linked user (Argus groups live on
        ``res.users``, so there's nothing to assign to).

        Failures are logged but never raised — group sync is a
        convenience layer and must not block an employee save.
        """
        self.ensure_one()
        user = self.user_id
        if not user:
            return
        # Resolve the role via task_forge_bridge.  That helper reads
        # ``user.user_role`` + group memberships and dereferences
        # ``api_auth_gateway`` records, so route the call through
        # sudo so the trigger works regardless of who edited the
        # employee (HR Officer, the user themselves, a CTO promoting
        # someone, etc.).
        try:
            role = self.sudo()._get_argus_role() or "tasker"
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "Argus group sync: could not resolve role for %s "
                "(id=%s): %s",
                self.name, self.id, exc,
            )
            return

        # Build the set of all Argus groups we manage so we can
        # subtract them from the user's membership before adding the
        # target — flipping PL → tasker shouldn't leave the elevated
        # PL group behind.
        Groups = self.env["res.groups"].sudo()
        all_argus_groups = Groups
        for xmlid in _ARGUS_GROUP_XMLIDS:
            grp = self.env.ref(xmlid, raise_if_not_found=False)
            if grp:
                all_argus_groups |= grp
        if not all_argus_groups:
            _logger.warning(
                "Argus group sync: none of %s could be resolved — "
                "is the Argus security data file loaded?",
                _ARGUS_GROUP_XMLIDS,
            )
            return

        target_xmlid = self._argus_pick_target_group_xmlid(role)
        target = self.env.ref(target_xmlid, raise_if_not_found=False)
        if not target:
            _logger.warning(
                "Argus group sync: target group %s not found",
                target_xmlid,
            )
            return

        user_su = user.sudo()
        # New membership = (existing − all_argus_groups) ∪ {target}
        new_groups = (user_su.groups_id - all_argus_groups) | target
        if user_su.groups_id == new_groups:
            return  # no-op — nothing changed
        user_su.write({"groups_id": [(6, 0, new_groups.ids)]})
        _logger.info(
            "Argus group sync: user %s (login=%s) → %s "
            "(role=%s, is_ql_type=%s)",
            user_su.id, user_su.login, target_xmlid,
            role, self.is_ql_type,
        )
