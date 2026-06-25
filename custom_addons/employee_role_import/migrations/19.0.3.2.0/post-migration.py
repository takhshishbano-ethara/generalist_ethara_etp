"""Backfill the Task Forge hierarchy fields after they became computed+stored.

The TPM / PL / QL / QR fields were plain stored Many2one until this version.
Converting them to computed+stored does not recompute existing rows, so we
force a one-time recompute here.  New / edited employees populate
automatically via the field dependencies; this only fixes pre-existing data.
"""

import logging

_logger = logging.getLogger(__name__)

HIERARCHY_FIELDS = [
    "task_forge_tpm_id",
    "task_forge_pl_id",
    "task_forge_ql_id",
    "task_forge_qr_id",
]


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    Employee = env["hr.employee"].with_context(active_test=False)
    employees = Employee.search([])
    if not employees:
        return

    # Force a recompute (in dependency order) of the now-computed fields, then
    # persist.  The compute walks each employee's reporting chain directly, so
    # the result is correct regardless of order.
    for field_name in HIERARCHY_FIELDS:
        field = Employee._fields.get(field_name)
        if field:
            env.add_to_compute(field, employees)
    env.flush_all()
    _logger.info(
        "employee_role_import: backfilled Task Forge hierarchy for %d employees",
        len(employees),
    )
