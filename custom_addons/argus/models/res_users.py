# -*- coding: utf-8 -*-
"""Argus / res.users glue — re-sync Argus group when the user's role flips.

The ``hr.employee`` extension already re-syncs the user's Argus
group whenever fields on the *employee* change.  But the Task Forge
role classification ALSO depends on:

* ``res.users.user_role`` (defined by ``api_auth_gateway``), and
* ``res.users.group_ids`` membership of the ``etp_user_roles`` groups
  (``group_cto`` / ``group_project_lead`` / ``group_quality_reviewer``
  / ``group_quality_lead``).

Both of those live on the **user**, not the employee.  Without a
hook here, promoting someone from tasker → PL by editing their
user record would not flip their Argus group until someone also
edited their employee record.

This extension closes that gap: when the relevant user-level fields
change we call ``employee_id._argus_sync_user_groups()`` for every
employee linked to the user.  An employee can have at most one
``user_id`` (Many2one), but a user can have multiple ``employee_ids``
(one per company), so we iterate.
"""

import logging

from odoo import models

_logger = logging.getLogger(__name__)


# Fields on res.users whose change can flip the Task Forge role
# classification — see ``task_forge_bridge.hr_employee._get_task_forge_role``.
# We watch ``group_ids`` because ``has_group('etp_user_roles.group_cto')``
# etc. are the primary classifier; ``user_role`` is the Many2one fallback
# used when group membership is absent.
#
# Odoo 19 note: ``res.users.groups_id`` was renamed to ``group_ids``
# (directly-assigned groups).  Editing a user's groups in the UI/ORM
# now lands in ``vals['group_ids']``, so that's the key we must watch
# — the old ``groups_id`` key would never appear and the re-sync would
# silently never fire.
_ARGUS_USER_RESYNC_TRIGGERS = frozenset({"group_ids", "user_role"})


class ResUsers(models.Model):
    _inherit = "res.users"

    def write(self, vals):
        res = super().write(vals)
        if _ARGUS_USER_RESYNC_TRIGGERS & set(vals.keys()):
            # Iterate users in self (write can be a recordset).
            # Each user.employee_ids gives the per-company employees;
            # we re-sync every one so a single role change propagates
            # cleanly even in multi-company setups.
            for user in self:
                employees = user.sudo().employee_ids
                for emp in employees:
                    try:
                        emp._argus_sync_user_groups()
                    except Exception as exc:  # pragma: no cover
                        _logger.warning(
                            "Argus group sync via res.users: %s "
                            "(emp_id=%s, user_id=%s)",
                            exc, emp.id, user.id,
                        )
        return res
