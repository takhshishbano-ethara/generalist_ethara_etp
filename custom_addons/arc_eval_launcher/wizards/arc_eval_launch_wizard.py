import json
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ArcEvalLaunchWizard(models.TransientModel):
    """Wizard to trigger an eval run on the arc-explainer backend."""

    _name = "arc.eval.launch.wizard"
    _description = "ARC Eval Launch Wizard"

    game_ids = fields.Many2many(
        comodel_name="arc.eval.game",
        string="Games",
        required=True,
        domain=[("active", "=", True)],
    )
    model_ids = fields.Many2many(
        comodel_name="arc.eval.model",
        string="Models",
        required=True,
        domain=[("active", "=", True)],
    )

    num_runs = fields.Integer(
        string="Runs per Model/Game",
        default=1,
        required=True,
        help="How many runs to execute per (game, model) pair.",
    )
    max_steps = fields.Integer(
        string="Max Steps",
        default=200,
        required=True,
        help="Maximum agent actions per run before truncation.",
    )
    context_window = fields.Integer(
        string="Context Window",
        default=50,
        required=True,
        help="Number of recent steps included in each LLM prompt.",
    )
    with_images = fields.Boolean(
        string="Include Grid Images",
        default=False,
        help="Send grid renders as images to vision-capable models.",
    )

    parallel_games = fields.Integer(
        string="Parallel Games",
        default=1,
        required=True,
        help="Number of games to run in parallel (1-25).",
    )
    parallel_runs = fields.Integer(
        string="Parallel Runs",
        default=1,
        required=True,
        help="Number of runs to execute in parallel per game (1-10).",
    )
    sequential_models = fields.Boolean(
        string="Sequential Models",
        default=False,
        help="Run selected models one after another instead of in parallel.",
    )

    budget_global_usd = fields.Float(
        string="Global Budget (USD)",
        default=0.0,
        help="Abort the session if total spend exceeds this value. 0 = no limit.",
    )
    budget_per_game_usd = fields.Float(
        string="Per-Game Budget (USD)",
        default=0.0,
        help="Abort a given game when its spend exceeds this value. 0 = no limit.",
    )

    @api.model
    def default_get(self, fields_list):
        """Pre-select all active games and models when wizard opens."""
        defaults = super().default_get(fields_list)
        # if "game_ids" in fields_list:
        #     games = self.env["arc.eval.game"].search([("active", "=", True)])
        #     defaults["game_ids"] = [(6, 0, games.ids)]
        if "model_ids" in fields_list:
            models = self.env["arc.eval.model"].search([("active", "=", True)])
            defaults["model_ids"] = [(6, 0, models.ids)]
        return defaults

    @api.constrains("num_runs", "max_steps", "context_window",
                    "parallel_games", "parallel_runs")
    def _check_numeric_bounds(self):
        for wiz in self:
            if wiz.num_runs < 1:
                raise UserError(_("Runs per Model/Game must be at least 1."))
            if not (1 <= wiz.max_steps <= 200):
                raise UserError(_("Max Steps must be between 1 and 200."))
            if wiz.context_window < 0:
                raise UserError(_("Context Window must be non-negative."))
            if not (1 <= wiz.parallel_games <= 25):
                raise UserError(_("Parallel Games must be between 1 and 25."))
            if not (1 <= wiz.parallel_runs <= 10):
                raise UserError(_("Parallel Runs must be between 1 and 10."))

    def action_launch(self):
        """Call POST /api/eval/start and create a session audit record."""
        self.ensure_one()

        if not self.game_ids:
            raise UserError(_("Select at least one game."))
        if not self.model_ids:
            raise UserError(_("Select at least one model."))

        ICP = self.env["ir.config_parameter"].sudo()
        api_base = (ICP.get_param("arc_eval.api_base") or "").rstrip("/")
        if not api_base:
            raise UserError(
                _("System parameter 'arc_eval.api_base' is not configured.")
            )
        try:
            timeout = int(ICP.get_param("arc_eval.request_timeout", default="30"))
        except (TypeError, ValueError):
            timeout = 30

        payload = {
            "gameIds": self.game_ids.mapped("code"),
            "modelKeys": self.model_ids.mapped("key"),
            "numRuns": self.num_runs,
            "maxSteps": self.max_steps,
            "contextWindow": self.context_window,
            "withImages": self.with_images,
            "parallelGames": self.parallel_games,
            "parallelRuns": self.parallel_runs,
            "sequentialModels": self.sequential_models,
        }
        if self.budget_global_usd > 0:
            payload["budgetGlobalUsd"] = self.budget_global_usd
        if self.budget_per_game_usd > 0:
            payload["budgetPerGameUsd"] = self.budget_per_game_usd

        url = f"{api_base}/api/eval/start"
        session_vals = {
            "game_ids": [(6, 0, self.game_ids.ids)],
            "model_ids": [(6, 0, self.model_ids.ids)],
            "num_runs": self.num_runs,
            "max_steps": self.max_steps,
            "context_window": self.context_window,
            "with_images": self.with_images,
            "parallel_games": self.parallel_games,
            "parallel_runs": self.parallel_runs,
            "sequential_models": self.sequential_models,
            "budget_global_usd": self.budget_global_usd,
            "budget_per_game_usd": self.budget_per_game_usd,
            "api_base": api_base,
            "request_payload": json.dumps(payload, indent=2),
        }

        try:
            response = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError as exc:
            _logger.warning("arc-explainer unreachable at %s: %s", url, exc)
            self.env["arc.eval.session"].create(dict(
                session_vals,
                state="failed",
                error_message=f"ConnectionError: {exc}",
            ))
            raise UserError(
                _("Cannot connect to arc-explainer at %s.\nSession marked as failed.")
                % api_base
            )
        except requests.exceptions.RequestException as exc:
            self.env["arc.eval.session"].create(dict(
                session_vals,
                state="failed",
                error_message=f"RequestException: {exc}",
            ))
            raise UserError(_("Request to %s failed: %s") % (url, exc))

        raw_body = response.text or ""
        session_vals["response_payload"] = raw_body[:8000]

        if not response.ok:
            self.env["arc.eval.session"].create(dict(
                session_vals,
                state="failed",
                error_message=f"HTTP {response.status_code}: {raw_body[:500]}",
            ))
            raise UserError(
                _("arc-explainer returned HTTP %s.\nResponse: %s")
                % (response.status_code, raw_body[:500])
            )

        try:
            data = response.json()
        except ValueError:
            self.env["arc.eval.session"].create(dict(
                session_vals,
                state="failed",
                error_message=f"Non-JSON response: {raw_body[:500]}",
            ))
            raise UserError(
                _("arc-explainer returned non-JSON:\n%s") % raw_body[:500]
            )

        session_id = None
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict):
                session_id = inner.get("sessionId")
        if not session_id:
            self.env["arc.eval.session"].create(dict(
                session_vals,
                state="failed",
                error_message=f"Missing sessionId in response: {raw_body[:500]}",
            ))
            raise UserError(
                _("arc-explainer response did not contain data.sessionId:\n%s")
                % raw_body[:500]
            )

        session = self.env["arc.eval.session"].create(dict(
            session_vals,
            state="launched",
            external_session_id=session_id,
        ))
        _logger.info(
            "arc-eval session launched id=%s external_id=%s games=%s models=%s",
            session.id, session_id,
            payload["gameIds"], payload["modelKeys"],
        )

        return {
            "type": "ir.actions.act_window",
            "name": _("Eval Session"),
            "res_model": "arc.eval.session",
            "view_mode": "form",
            "res_id": session.id,
            "target": "current",
        }
