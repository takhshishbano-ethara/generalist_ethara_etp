from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Re-seed the Erza sample bundle on module upgrade.

    ``seed_from_bundle`` is otherwise only invoked by ``post_init_hook`` at
    install time, so a plain ``-u erza_dashboard`` would not pick up an edited
    ``data/erza_dataset.json``. Running it here (post-migrate, after the ORM is
    loaded) upserts the current payload and prunes any task/model no longer in
    it, so an upgrade fully replaces the prior sample delivery.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["erza.run"].seed_from_bundle()
