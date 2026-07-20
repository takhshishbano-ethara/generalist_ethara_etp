from odoo import api, fields, models


_WARNING_KINDS = [
    ("other_person", "Other Person in Frame"),
    ("mobile_phone", "Mobile Phone Detected"),
    ("lip_movement", "Lip Movement / Speaking"),
    ("no_face", "Candidate Not Visible"),
    ("look_away", "Looking Away From Screen"),
    ("window_change", "Window/Tab Change"),
    ("tab_switch", "Tab Switched"),
    ("fullscreen_exit", "Fullscreen Exited"),
    ("copy_paste", "Copy / Paste Attempt"),
]


class EtpApplicantAssessmentWarning(models.Model):
    _name = "etp.applicant.assessment.warning"
    _description = "Applicant Assessment Warning"
    _order = "create_date desc"

    assessment_id = fields.Many2one(
        "etp.applicant.assessment",
        required=True, ondelete="cascade", index=True,
    )
    kind = fields.Selection(_WARNING_KINDS, required=True, index=True)
    chunk_start_at = fields.Datetime()
    chunk_end_at = fields.Datetime()

    s3_url = fields.Char(string="Clip URL")
    s3_key = fields.Char(string="S3 Key")
    attachment_id = fields.Many2one(
        "ir.attachment", ondelete="set null",
        help="Local ir.attachment holding the clip when S3 is not "
             "configured or the S3 upload failed.",
    )
    signed_url = fields.Char(
        compute="_compute_signed_urls",
        help="Short-lived presigned GET for reviewer playback (S3 only).",
    )
    raw_meta_json = fields.Text(string="Detector Meta")

    penalty_percent = fields.Float(
        string="Penalty (%)",
        help="Snapshot of template penalty for this warning kind at creation time.",
    )
    detector_confidence = fields.Float()

    snapshot_url = fields.Char(
        string="Snapshot URL",
        help="320px JPEG evidence thumbnail captured at the signal moment.",
    )
    snapshot_key = fields.Char(string="Snapshot S3 Key")
    snapshot_signed_url = fields.Char(
        compute="_compute_signed_urls",
        help="Short-lived presigned GET for the snapshot (S3 only).",
    )

    def _compute_signed_urls(self):
        from ..services import s3_service
        env = self.env
        s3_ready = s3_service.is_configured(env)
        client = s3_service._client(env) if s3_ready else None
        for rec in self:
            if s3_ready and rec.s3_key:
                rec.signed_url = s3_service.presigned_get_url(
                    env, rec.s3_key, expires=3600, client=client,
                ) or rec.s3_url
            else:
                rec.signed_url = rec.s3_url or ""
            if s3_ready and rec.snapshot_key:
                rec.snapshot_signed_url = s3_service.presigned_get_url(
                    env, rec.snapshot_key, expires=3600, client=client,
                ) or rec.snapshot_url
            else:
                rec.snapshot_signed_url = rec.snapshot_url or ""

    _PENALTY_FIELD = {
        "other_person": "penalty_other_person",
        "mobile_phone": "penalty_mobile_phone",
        "lip_movement": "penalty_lip_movement",
        "no_face": "penalty_no_face",
        "look_away": "penalty_look_away",
        "window_change": "penalty_window_change",
        "tab_switch": "penalty_window_change",
        "fullscreen_exit": "penalty_window_change",
        "copy_paste": "penalty_window_change",
    }

    @api.model_create_multi
    def create(self, vals_list):
        Assessment = self.env["etp.applicant.assessment"]
        for vals in vals_list:
            if vals.get("penalty_percent") is not None:
                continue
            kind = vals.get("kind")
            assessment_id = vals.get("assessment_id")
            if not (kind and assessment_id):
                continue
            template = Assessment.browse(assessment_id).template_id
            attr = self._PENALTY_FIELD.get(kind)
            vals["penalty_percent"] = (
                getattr(template, attr, 0.0) if attr else 0.0
            )
        records = super().create(vals_list)
        records.mapped("assessment_id")._maybe_auto_submit()
        return records
