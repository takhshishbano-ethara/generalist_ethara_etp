from odoo import fields, models

from .client_deliverable import STATUS_SELECTION


class ClientDeliverableIterationLog(models.Model):
    """An immutable log entry for one status transition.

    Each entry records the iteration it belonged to, the status change, who
    made it and when — so the full per-iteration history can be displayed.
    """

    _name = "client.deliverable.iteration.log"
    _description = "Client Deliverable Iteration Log"
    _order = "create_date desc, id desc"

    deliverable_id = fields.Many2one(
        "client.deliverable",
        string="Deliverable",
        required=True,
        ondelete="cascade",
        index=True,
    )
    iteration = fields.Integer(string="Iteration")
    from_status = fields.Selection(STATUS_SELECTION, string="From")
    to_status = fields.Selection(STATUS_SELECTION, string="To")
    user_id = fields.Many2one("res.users", string="Changed By")
    note = fields.Text(string="Note")
