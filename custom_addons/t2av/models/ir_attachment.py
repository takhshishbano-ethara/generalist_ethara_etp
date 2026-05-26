"""T2AV extension of ``ir.attachment``.

Adds two backref fields so generated video attachments can be queried by
job or attempt. Used by ``t2av.attempt._run_download`` (Wave 4D) to
eagerly create one ir.attachment per done video so it appears in the
job's chatter with standard Odoo attachment UI (download, audit, etc).
"""

from odoo import fields, models


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    t2av_job_id = fields.Many2one(
        "t2av.generation",
        string="T2AV Job",
        index=True,
        ondelete="set null",
        help="Job that produced this video attachment.",
    )
    t2av_attempt_id = fields.Many2one(
        "t2av.attempt",
        string="T2AV Attempt",
        index=True,
        ondelete="set null",
        help="Specific attempt that produced this video attachment.",
    )
