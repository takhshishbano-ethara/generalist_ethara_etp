from odoo import fields, models


class ArcQcModelResult(models.Model):
    """Per-model-directory QC result within a game."""

    _name = 'arc.qc.model.result'
    _description = 'ARC QC Model Result'
    _order = 'model_dir asc'

    game_result_id = fields.Many2one(
        comodel_name='arc.qc.game.result',
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

    has_runs = fields.Boolean(string='runs.jsonl Present')
    has_steps = fields.Boolean(string='steps.jsonl Present')
    run_count = fields.Integer(string='Run Count')
    step_count = fields.Integer(string='Step Count')
