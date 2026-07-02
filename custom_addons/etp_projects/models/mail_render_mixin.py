from datetime import date, datetime

from pytz import UTC, timezone

from odoo import api, models

IST = timezone("Asia/Kolkata")


def _format_ist(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        aware = value if value.tzinfo else UTC.localize(value)
        return aware.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _format_ist_date(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        aware = value if value.tzinfo else UTC.localize(value)
        return aware.astimezone(IST).strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


class MailRenderMixin(models.AbstractModel):
    _inherit = "mail.render.mixin"

    @api.model
    def _render_eval_context(self):
        ctx = super()._render_eval_context()
        ctx["format_ist"] = _format_ist
        ctx["format_ist_date"] = _format_ist_date
        return ctx
