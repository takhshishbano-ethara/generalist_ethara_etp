from odoo import fields, models


class ArcBenchModelResult(models.Model):
    """Per-model aggregated result within a game."""

    _name = 'arc.bench.model.result'
    _description = 'ARC Bench Model Result'
    _order = 'mean_score_pct desc'

    game_result_id = fields.Many2one(
        comodel_name='arc.bench.game.result',
        string='Game Result',
        required=True,
        ondelete='cascade',
        index=True,
    )
    session_id = fields.Many2one(
        related='game_result_id.session_id',
        store=True,
        index=True,
    )
    model_dir = fields.Char(
        string='Model Directory',
        required=True,
        help='e.g. Claude_Opus_4.7',
    )
    model_name = fields.Char(
        string='Model Name',
        help='Canonical model name, e.g. "Claude Opus 4.7"',
    )
    run_count = fields.Integer(string='Runs')
    mean_score_pct = fields.Float(string='Mean Score (%)', digits=(6, 2))
    mean_cost_usd = fields.Float(string='Mean Cost ($)', digits=(10, 2))
    total_steps = fields.Integer(string='Total Steps')
    solved_count = fields.Integer(string='Solved Runs')
    mean_elapsed_seconds = fields.Float(
        string='Mean Elapsed (s)', digits=(10, 1)
    )

    run_ids = fields.One2many(
        comodel_name='arc.bench.run',
        inverse_name='model_result_id',
        string='Runs',
        readonly=True,
    )
