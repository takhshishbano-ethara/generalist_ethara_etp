import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ArcEvalGame(models.Model):
    """Local cache of games available in arc-explainer.

    Refreshed via cron (hourly by default) or manually by admins from the
    Games list view (``action_refresh_from_api``). The ``code`` field is the
    authoritative identifier sent to ``POST /api/eval/start``.
    """

    _name = "arc.eval.game"
    _description = "ARC Eval Game (cached from arc-explainer)"
    _order = "title, code"
    _rec_name = "code"

    code = fields.Char(
        string="Game ID",
        required=True,
        index=True,
        help="Canonical game identifier (e.g. 'ab12', 'eg08'). "
        "Sent as-is to the arc-explainer API.",
    )
    title = fields.Char(string="Title")
    game_type = fields.Char(string="Type", help="e.g. 'arc3', 'arc2'.")
    active = fields.Boolean(string="Active", default=True)
    last_synced_at = fields.Datetime(string="Last Synced")

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Game code must be unique."),
    ]

    @api.model
    def action_refresh_from_api(self):
        """Fetch game list from arc-explainer and upsert into the cache."""
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

        games_endpoint = (
            ICP.get_param("arc_eval.games_endpoint") or "/api/arc3/local-games"
        ).strip()
        if not games_endpoint.startswith("/"):
            games_endpoint = "/" + games_endpoint
        game_id_field = (
            ICP.get_param("arc_eval.game_id_field") or "game_id"
        ).strip()

        url = f"{api_base}{games_endpoint}"
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            _logger.warning("arc-explainer unreachable at %s: %s", url, exc)
            raise UserError(
                _("Cannot connect to arc-explainer at %s.\n"
                  "Check that the server is running and the "
                  "'arc_eval.api_base' system parameter is correct.") % api_base
            )
        except requests.exceptions.HTTPError as exc:
            body = (response.text or "")[:500]
            raise UserError(
                _("arc-explainer returned HTTP %s for %s.\nResponse: %s")
                % (response.status_code, url, body)
            )
        except requests.exceptions.RequestException as exc:
            raise UserError(_("Request to %s failed: %s") % (url, exc))

        try:
            payload = response.json()
        except ValueError:
            raise UserError(
                _("arc-explainer returned non-JSON for %s:\n%s")
                % (url, (response.text or "")[:500])
            )

        # Auto-detect response shape:
        # - { data: [...] }           → endpoints 2 & 3
        # - { data: { games: [...] } } → endpoint 1
        raw_data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(raw_data, dict):
            games = raw_data.get("games") or raw_data.get("data")
        elif isinstance(raw_data, list):
            games = raw_data
        else:
            games = None
        if not isinstance(games, list):
            raise UserError(
                _("Unexpected response shape from %s. Expected data={games:[...]}, got:\n%s")
                % (url, str(payload)[:500])
            )

        now = fields.Datetime.now()
        seen_codes = set()
        created = 0
        updated = 0
        for game in games:
            if not isinstance(game, dict):
                continue
            code = game.get(game_id_field)
            if not code:
                continue
            seen_codes.add(code)
            vals = {
                "title": game.get("title") or code,
                "game_type": game.get("type") or False,
                "active": True,
                "last_synced_at": now,
            }
            existing = self.search([("code", "=", code)], limit=1)
            if existing:
                existing.write(vals)
                updated += 1
            else:
                self.create(dict(vals, code=code))
                created += 1

        # Archive games that disappeared upstream (do NOT delete - preserves
        # history on session snapshots that reference them).
        missing = self.search([("code", "not in", list(seen_codes))])
        if missing:
            missing.write({"active": False})

        _logger.info(
            "arc.eval.game refresh: created=%s updated=%s archived=%s total_upstream=%s",
            created, updated, len(missing), len(seen_codes),
        )
        return True
