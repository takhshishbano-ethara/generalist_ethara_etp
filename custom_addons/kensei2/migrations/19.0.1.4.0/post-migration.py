"""Re-derive the stored status_label after the per-stage rename.

`status_label` is a STORED compute that depends on (status, stage_no). This upgrade
changes neither -- it changes the *label tables* the compute reads:

    manual_qc  ->  'Stage 1 Manual QC'   (stage 1)
                   'Stage 2 Manual QC'   (stage 2)

so nothing marks the field dirty and every existing row keeps the string it was
written with ('Manual QC'). The list, the kanban columns and the Group-by all read
status_label, so without this they would keep showing the old name indefinitely --
and the kanban would grow a stale extra column for it.

Through the ORM, not raw SQL: status_label is a stored compute, and writing its table
behind the ORM's back is exactly what left assignment_status stale on personas before.
"""

from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Alloc = env["kensei2.tracker.allocation"].with_context(active_test=False)
    allocs = Alloc.search([])
    if not allocs:
        return

    # Queue the field for recomputation, then force it to be written back.
    # modified() would be the wrong direction here -- it invalidates the fields that
    # DEPEND ON status_label, not status_label itself.
    env.add_to_compute(Alloc._fields["status_label"], allocs)
    allocs.flush_recordset(["status_label"])
