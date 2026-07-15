from odoo import fields, models


class EtpApplicantAssessmentSnapshot(models.Model):
    _name = "etp.applicant.assessment.snapshot"
    _description = "Applicant Assessment Snapshot (proctoring evidence)"
    _order = "captured_at desc, id desc"

    assessment_id = fields.Many2one(
        "etp.applicant.assessment",
        required=True, ondelete="cascade", index=True,
    )
    reason = fields.Char(
        required=True, index=True,
        help="'periodic', 'signal', or one of the warning kinds.",
    )
    url = fields.Char(
        required=True,
        help="Public URL of the stored image (S3 or local ir.attachment).",
    )
    s3_key = fields.Char(help="S3 object key when the snapshot lives in S3.")
    attachment_id = fields.Many2one(
        "ir.attachment", ondelete="set null",
        help="Local ir.attachment when S3 is not configured.",
    )
    captured_at = fields.Datetime(required=True, index=True)
    signed_url = fields.Char(
        compute="_compute_signed_url",
        help="Short-lived presigned GET for reviewer playback (S3 only).",
    )

    def _compute_signed_url(self):
        from ..services import s3_service
        env = self.env
        s3_ready = s3_service.is_configured(env)
        client = s3_service._client(env) if s3_ready else None
        for rec in self:
            if s3_ready and rec.s3_key:
                rec.signed_url = s3_service.presigned_get_url(
                    env, rec.s3_key, expires=3600, client=client,
                ) or rec.url
            else:
                rec.signed_url = rec.url
