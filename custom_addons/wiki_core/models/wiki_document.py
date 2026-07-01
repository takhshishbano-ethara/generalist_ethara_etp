from odoo import models, fields


class WikiDocumentPage(models.Model):
    """A simple Wiki info page that carries only a description and an
    optional uploaded document.

    Used by the Leave Policy and Holidays wiki pages. The live data they show
    (leave balances, public holidays) is sourced directly from the Time Off
    module — this model only holds the editable narrative / reference
    document, so there is no duplicate holiday or balance data here.
    """
    _name = 'wiki.document.page'
    _description = 'Wiki Document Page'

    page_key = fields.Selection(
        [('leave_policy', 'Leave Policy'), ('holidays', 'Holidays')],
        string='Page', required=True,
        help='Which wiki page this content backs.')
    name = fields.Char(string='Title', required=True)
    description = fields.Text(string='Description')
    document = fields.Binary(string='Document', attachment=True)
    document_filename = fields.Char(string='Document Filename')

    # Odoo 19: _sql_constraints is ignored; declare model.Constraint instead.
    _page_key_uniq = models.Constraint(
        'unique(page_key)',
        'Each wiki page can only have one document page.',
    )
