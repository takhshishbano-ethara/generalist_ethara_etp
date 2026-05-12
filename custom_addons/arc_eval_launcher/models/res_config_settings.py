from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    arc_eval_api_base = fields.Char(
        string="API Base URL",
        config_parameter="arc_eval.api_base",
        help="Base URL for the arc-explainer server (e.g. http://localhost:5000).",
    )
    arc_eval_request_timeout = fields.Integer(
        string="Request Timeout (s)",
        config_parameter="arc_eval.request_timeout",
        help="HTTP timeout in seconds for arc-explainer API calls.",
    )
    arc_eval_games_endpoint = fields.Char(
        string="Games Endpoint",
        config_parameter="arc_eval.games_endpoint",
        help="API path appended to base URL to fetch game list "
        "(e.g. /api/arc3/local-games, /api/arc3/games, /api/eval/games).",
    )
    arc_eval_game_id_field = fields.Char(
        string="Game ID Field",
        config_parameter="arc_eval.game_id_field",
        help="JSON field name that holds the game identifier in the API response "
        "(e.g. 'game_id' or 'id').",
    )
