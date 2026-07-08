import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class HrLeave(models.Model):
    _inherit = "hr.leave"

    greythr_request_ids = fields.One2many(
        "greythr.leave.request", "hr_leave_id", string="greytHR Requests"
    )

    @api.model_create_multi
    def create(self, vals_list):
        leaves = super().create(vals_list)
        leaves._greythr_push("create")
        return leaves

    def action_approve(self, *args, **kwargs):
        res = super().action_approve(*args, **kwargs)
        self._greythr_push("approve")
        return res

    def action_refuse(self, *args, **kwargs):
        res = super().action_refuse(*args, **kwargs)
        self._greythr_push("refuse")
        return res

    def _greythr_push(self, action):
        Instance = self.env["greythr.instance"].sudo()
        instances = Instance.search(
            [("active", "=", True), ("push_leave_on_state_change", "=", True)]
        )
        if not instances:
            return
        for leave in self:
            for instance in instances:
                try:
                    instance._push_leave_to_greythr(leave, action=action)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "greytHR push (%s) failed for leave %s: %s",
                        action,
                        leave.id,
                        exc,
                    )
