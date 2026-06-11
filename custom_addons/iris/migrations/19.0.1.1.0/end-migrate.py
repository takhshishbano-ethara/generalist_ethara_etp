"""iris v1.0 -> v1.1 end-migration.

Runs AFTER all data files have loaded (the Head of Engineering seed from
``data/iris_role_profile_data.xml`` exists) and maps every legacy
free-text ``target_role_legacy`` string onto an ``iris.role.profile``:

* "Head of Engineering" (case-insensitive) -> the seeded role;
* every other distinct string -> an ARCHIVED ``is_legacy`` role with the
  label unchanged (created with the ``iris_role_migration`` context that
  bypasses the v1.1 role-creation lock).

The heavy lifting — including the final ``ALTER COLUMN role_id SET NOT
NULL`` — lives in the testable helper
``IrisRoleProfile._migrate_legacy_target_roles(cr)``, which is idempotent
and no-ops when the ``target_role_legacy`` column is absent.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        # Fresh install — no legacy data, nothing to migrate.
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    _logger.info(
        "iris end-migrate 19.0.1.1.0: mapping legacy target_role strings "
        "onto iris.role.profile records."
    )
    env["iris.role.profile"]._migrate_legacy_target_roles(cr)
