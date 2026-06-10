from odoo import fields, models


class EtpExternalProjectApiMap(models.Model):
    _name = 'etp.external.project.api.map'
    _description = 'External Project API Mapping'
    _rec_name = 'table_name'
    # Tab order in the Flutter project detail is the order of project.api_map_ids,
    # which follows this _order. Ordering by `sequence` (then id) lets the tab
    # order be controlled explicitly from the seed data instead of being frozen
    # to row-creation order — so a project can be given new tabs in any position.
    _order = 'sequence, id'

    sequence = fields.Integer(
        string='Sequence',
        default=10,
        help="Order of this tab in the project detail tab strip "
             "(lower numbers first).",
    )
    project_id = fields.Many2one(
        comodel_name='project.project',
        string='External Project',
        required=True,
        ondelete='cascade',
        domain=[('project_classification', '=', 'external')],
        help="External project this API mapping is connected to.",
    )
    table_name = fields.Char(
        string='Table Name',
        required=True,
        help="Name of the table to map.",
    )
    field_name = fields.Char(
        string='Field',
        required=True,
        help="Name of the field to map.",
    )
    api_prefix = fields.Char(
        string='API Prefix',
        required=True,
        help="API prefix defined for this table/field mapping.",
    )
