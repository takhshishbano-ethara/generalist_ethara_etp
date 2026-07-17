# -*- coding: utf-8 -*-
from odoo import models, fields, _
from odoo.exceptions import UserError


class Kensei2PersonaImport(models.TransientModel):
    """Bulk-import Personas from a CSV — WITHOUT allocating them to anyone.

    This is the "load the data now, assign it later" half of the workflow:
    imported personas sit unassigned, and the Bulk Allocation wizard then picks
    them up via its "Load Unassigned Personas" source. (Bulk Allocation can also
    take a CSV directly, which imports *and* distributes in one shot — use that
    when you already know who gets what.)

    The parsing / de-dupe / reuse-or-create logic is NOT reimplemented here: it is
    reused verbatim from project.tracker.bulk.allocation._persona_entries(), so the
    two CSV paths can never drift apart (same column aliases, same name
    sanitisation, same reuse of archived personas).
    """
    _name = 'project.tracker.persona.import'
    _description = 'Kensei — Bulk Import Personas (CSV)'

    csv_file = fields.Binary(string='Persona CSV', required=True)
    filename = fields.Char(string='Filename')

    # The project this upload feeds — defaulted from the launching context
    # (default_pt_project_id). Imported personas are stamped with it, and its
    # type decides which taxonomy columns the dialog documents.
    pt_project_id = fields.Many2one(
        'project.tracker.project', string='Project')
    project_type = fields.Selection(
        related='pt_project_id.project_type', string='Type')

    state = fields.Selection(
        [('choose', 'Choose'), ('done', 'Done')],
        default='choose', readonly=True,
    )
    total_count = fields.Integer(string='Rows in File', readonly=True)
    created_count = fields.Integer(string='Newly Created', readonly=True)
    reused_count = fields.Integer(string='Already Existed', readonly=True)
    result_html = fields.Html(readonly=True, sanitize=False)

    def action_import(self):
        """Create/reuse the personas in the uploaded CSV. No allocation happens."""
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Please upload a CSV file first."))

        # Reuse the Bulk Allocation wizard's parser + persona resolver. It is a
        # TransientModel and every required field has a default, so building one
        # in memory is cheap and safe. create_missing=True does exactly the import
        # (parse -> de-dupe -> reuse existing/archived -> batch-create the rest);
        # distributing to taskers is a separate step there, so nothing is assigned.
        helper = self.env['project.tracker.bulk.allocation'].with_context(
            default_pt_project_id=self.pt_project_id.id,
        ).create({
            'source_mode': 'file',
            'csv_file': self.csv_file,
            'filename': self.filename,
        })
        entries = helper._persona_entries(create_missing=True)

        created = [e for e in entries if e['is_new']]
        reused = [e for e in entries if not e['is_new']]

        self.write({
            'state': 'done',
            'total_count': len(entries),
            'created_count': len(created),
            'reused_count': len(reused),
            'result_html': self._build_report(created, reused),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Import Personas'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _build_report(self, created, reused):
        def _names(entries, limit=15):
            shown = ', '.join(e['name'] for e in entries[:limit])
            if len(entries) > limit:
                shown += _(' … and %s more', len(entries) - limit)
            return shown or '—'

        return _(
            '<p><b>%(created)s</b> persona(s) created, '
            '<b>%(reused)s</b> already existed (reused, not duplicated).</p>'
            '<p class="text-muted mb-1"><b>Created:</b> %(created_names)s</p>'
            '<p class="text-muted mb-0"><b>Reused:</b> %(reused_names)s</p>'
            '<p class="mt-2 mb-0">These personas are <b>unassigned</b>. Use '
            '<b>Bulk Allocation → Load Unassigned Personas</b> to distribute them '
            'to taskers.</p>',
            created=len(created), reused=len(reused),
            created_names=_names(created), reused_names=_names(reused),
        )

    def action_open_bulk_allocation(self):
        """Shortcut from the result screen straight into distributing them."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Bulk Allocation'),
            'res_model': 'project.tracker.bulk.allocation',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_source_mode': 'unassigned'},
        }
