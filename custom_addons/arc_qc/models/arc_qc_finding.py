from odoo import api, fields, models


class ArcQcFinding(models.Model):

    _name = 'arc.qc.finding'
    _description = 'ARC QC Finding'
    _order = 'severity_order asc, phase asc, id asc'

    session_id = fields.Many2one(
        comodel_name='arc.qc.session',
        string='QC Session',
        required=True,
        ondelete='cascade',
        index=True,
    )
    game_result_id = fields.Many2one(
        comodel_name='arc.qc.game.result',
        string='Game Result',
        ondelete='cascade',
        index=True,
    )
    severity = fields.Selection(
        selection=[
            ('critical', 'CRITICAL'),
            ('high', 'HIGH'),
            ('medium', 'MEDIUM'),
            ('low', 'LOW'),
        ],
        required=True,
        index=True,
    )
    severity_order = fields.Integer(
        compute='_compute_severity_order',
        store=True,
        index=True,
    )
    phase = fields.Char(required=True, index=True)
    code = fields.Char(required=True, index=True)
    game_id = fields.Char(string='Game', index=True)
    message = fields.Text(required=True)

    file_path = fields.Char()
    line_number = fields.Integer()
    field_name = fields.Char()
    expected = fields.Char()
    actual = fields.Char()
    spec_ref = fields.Char(string='Spec Reference')

    @api.depends('severity')
    def _compute_severity_order(self):
        order_map = {'critical': 1, 'high': 2, 'medium': 3, 'low': 4}
        for rec in self:
            rec.severity_order = order_map.get(rec.severity, 99)
