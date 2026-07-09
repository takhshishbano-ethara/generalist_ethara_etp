# -*- coding: utf-8 -*-
from odoo import _, fields, models


class DocumensoSyncWizard(models.TransientModel):
    _name = 'documenso.sync.wizard'
    _description = 'Documenso Sync Wizard'

    page_size = fields.Integer(string='Page Size', default=50)
    cache_binaries = fields.Boolean(
        string='Cache PDFs in Odoo',
        default=False,
        help='If enabled, downloads each document PDF and stores it on the record.',
    )

    def action_sync(self):
        self.ensure_one()
        Document = self.env['documenso.document']
        stats = Document.action_sync_all(page_size=self.page_size, cache_binaries=self.cache_binaries)
        message = _(
            'Synced %(total)s documents (created %(created)s, updated %(updated)s).'
        ) % {
            'total': stats.get('total', 0),
            'created': stats.get('created', 0),
            'updated': stats.get('updated', 0),
        }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Documenso Sync'),
                'message': message,
                'sticky': False,
                'type': 'success',
            },
        }
