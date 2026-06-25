import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_AUTO_CLOSE_HOURS = 14


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    @api.model
    def _cron_close_orphan_attendances(self):
        cutoff = fields.Datetime.now() - timedelta(hours=_AUTO_CLOSE_HOURS)
        orphans = self.search([("check_out", "=", False), ("check_in", "<=", cutoff)])
        if not orphans:
            return
        _logger.info("ESSL: auto-closing %d orphan attendance(s)", len(orphans))
        for att in orphans:
            auto_checkout = att.check_in + timedelta(hours=_AUTO_CLOSE_HOURS)
            att.write({"check_out": auto_checkout})
            att._message_log(
                body="Auto-closed by ESSL after %d hours: no check-out recorded. "
                     "Please review and correct this attendance record." % _AUTO_CLOSE_HOURS
            )
