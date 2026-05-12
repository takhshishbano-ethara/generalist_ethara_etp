from odoo import fields, models


class ArcBenchGameResult(models.Model):
    """Per-game analysis result with plot images."""

    _name = 'arc.bench.game.result'
    _description = 'ARC Bench Game Result'
    _order = 'game_id asc'

    session_id = fields.Many2one(
        comodel_name='arc.bench.session',
        string='Analysis Session',
        required=True,
        ondelete='cascade',
        index=True,
    )
    game_id = fields.Char(
        string='Game ID',
        required=True,
        index=True,
    )
    game_path = fields.Char(string='Game Path')
    model_count = fields.Integer(string='Models')

    # Plot images stored as Binary (base64-encoded PNG)
    plot1_image = fields.Binary(
        string='Score over Steps',
        attachment=True,
    )
    plot2_image = fields.Binary(
        string='Score vs Cost',
        attachment=True,
    )

    model_result_ids = fields.One2many(
        comodel_name='arc.bench.model.result',
        inverse_name='game_result_id',
        string='Model Results',
        readonly=True,
    )
