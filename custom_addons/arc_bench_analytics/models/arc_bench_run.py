from odoo import fields, models


class ArcBenchRun(models.Model):
    """Individual run data from runs.jsonl."""

    _name = 'arc.bench.run'
    _description = 'ARC Bench Run'
    _order = 'run_number asc'

    model_result_id = fields.Many2one(
        comodel_name='arc.bench.model.result',
        string='Model Result',
        required=True,
        ondelete='cascade',
        index=True,
    )
    run_id = fields.Char(string='Run ID')
    run_number = fields.Integer(string='Run #')
    final_score_pct = fields.Float(string='Score (%)', digits=(6, 2))
    cost_usd = fields.Float(string='Cost ($)', digits=(10, 2))
    total_steps = fields.Integer(string='Steps')
    solved = fields.Boolean(string='Solved')
    levels_completed = fields.Integer(string='Levels Completed')
    total_levels = fields.Integer(string='Total Levels')
    total_input_tokens = fields.Integer(string='Input Tokens')
    total_output_tokens = fields.Integer(string='Output Tokens')
    total_reasoning_tokens = fields.Integer(string='Reasoning Tokens')
    elapsed_seconds = fields.Float(string='Elapsed (s)', digits=(10, 1))
    error = fields.Text(string='Error')
