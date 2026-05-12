from odoo import _, api, fields, models


class ArcEvalSession(models.Model):
    """Audit record for an eval session launched from Odoo.

    One record is created each time a user submits the launch wizard. The
    ``external_session_id`` is the identifier returned by
    ``POST /api/eval/start`` on arc-explainer; use it to cross-reference
    steps.jsonl, DB rows, and SSE streams on the arc-explainer side.
    """

    _name = "arc.eval.session"
    _description = "ARC Eval Session (launched via Odoo)"
    _order = "launched_at desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        string="Reference",
        compute="_compute_name",
        store=True,
    )
    external_session_id = fields.Char(
        string="External Session ID",
        index=True,
        readonly=True,
        tracking=True,
        help="Session identifier returned by arc-explainer "
        "(POST /api/eval/start -> data.sessionId).",
    )
    state = fields.Selection(
        selection=[
            ("launched", "Launched"),
            ("failed", "Failed to Launch"),
        ],
        default="launched",
        required=True,
        tracking=True,
    )
    launched_at = fields.Datetime(
        string="Launched At",
        default=fields.Datetime.now,
        readonly=True,
    )
    launched_by = fields.Many2one(
        comodel_name="res.users",
        string="Launched By",
        default=lambda self: self.env.user,
        readonly=True,
    )

    game_ids = fields.Many2many(
        comodel_name="arc.eval.game",
        string="Games",
        readonly=True,
    )
    model_ids = fields.Many2many(
        comodel_name="arc.eval.model",
        string="Models",
        readonly=True,
    )

    num_runs = fields.Integer(string="Runs per Model/Game", readonly=True)
    max_steps = fields.Integer(string="Max Steps", readonly=True)
    context_window = fields.Integer(string="Context Window", readonly=True)
    with_images = fields.Boolean(string="With Images", readonly=True)
    parallel_games = fields.Integer(string="Parallel Games", readonly=True)
    parallel_runs = fields.Integer(string="Parallel Runs", readonly=True)
    sequential_models = fields.Boolean(string="Sequential Models", readonly=True)
    budget_global_usd = fields.Float(string="Global Budget (USD)", readonly=True)
    budget_per_game_usd = fields.Float(string="Per-Game Budget (USD)", readonly=True)

    api_base = fields.Char(
        string="API Base",
        readonly=True,
        help="arc-explainer URL the session was launched against.",
    )
    request_payload = fields.Text(
        string="Request Payload",
        readonly=True,
        help="JSON payload sent to POST /api/eval/start.",
    )
    response_payload = fields.Text(
        string="Response Payload",
        readonly=True,
        help="Raw JSON response from arc-explainer.",
    )
    error_message = fields.Text(string="Error", readonly=True)

    @api.depends("external_session_id", "launched_at")
    def _compute_name(self):
        for rec in self:
            if rec.external_session_id:
                rec.name = rec.external_session_id
            elif rec.launched_at:
                rec.name = _("Eval @ %s") % fields.Datetime.to_string(rec.launched_at)
            else:
                rec.name = _("New Eval Session")
