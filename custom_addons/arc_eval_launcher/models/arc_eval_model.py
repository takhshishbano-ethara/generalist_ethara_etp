import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ArcEvalModel(models.Model):
    """Local cache of LLM models available in arc-explainer.

    Refreshed via cron or manually. The ``key`` field is the authoritative
    identifier sent to ``POST /api/eval/start`` (e.g. ``claude-opus-4.7``).
    """

    _name = "arc.eval.model"
    _description = "ARC Eval Model (cached from arc-explainer)"
    _order = "name, key"

    key = fields.Char(
        string="Model Key",
        required=True,
        index=True,
        help="Canonical model key (e.g. 'claude-opus-4.7', 'kimi-k2.5'). "
        "Sent as-is to the arc-explainer API.",
    )
    name = fields.Char(string="Display Name")
    provider = fields.Char(string="Provider")
    supports_vision = fields.Boolean(string="Vision Support", default=False)
    active = fields.Boolean(string="Active", default=True)
    last_synced_at = fields.Datetime(string="Last Synced")

    _sql_constraints = [
        ("key_uniq", "unique(key)", "Model key must be unique."),
    ]

    def name_get(self):
        result = []
        for rec in self:
            label = rec.name and f"{rec.name} ({rec.key})" or rec.key
            result.append((rec.id, label))
        return result

    @api.model
    def action_refresh_from_api(self):
        """Fetch model list from arc-explainer and upsert into the cache."""
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

        url = f"{api_base}/api/eval/models"
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
        except requests.exceptions.HTTPError:
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

        # Endpoint returns { success: true, data: { models: [{key, name, ...}, ...] } }
        raw_data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(raw_data, dict):
            items = raw_data.get("models") or raw_data.get("data")
        elif isinstance(raw_data, list):
            items = raw_data
        else:
            items = None
        if not isinstance(items, list):
            raise UserError(
                _("Unexpected response shape from %s. Expected data={models:[...]}, got:\n%s")
                % (url, str(payload)[:500])
            )

        now = fields.Datetime.now()
        seen_keys = set()
        created = 0
        updated = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not key:
                continue
            seen_keys.add(key)
            vals = {
                "name": item.get("name") or key,
                "provider": item.get("provider") or False,
                "supports_vision": bool(item.get("supportsVision")),
                "active": True,
                "last_synced_at": now,
            }
            existing = self.search([("key", "=", key)], limit=1)
            if existing:
                existing.write(vals)
                updated += 1
            else:
                self.create(dict(vals, key=key))
                created += 1

        missing = self.search([("key", "not in", list(seen_keys))])
        if missing:
            missing.write({"active": False})

        _logger.info(
            "arc.eval.model refresh: created=%s updated=%s archived=%s total_upstream=%s",
            created, updated, len(missing), len(seen_keys),
        )
        return True
