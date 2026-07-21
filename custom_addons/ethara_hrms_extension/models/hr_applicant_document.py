import logging
import threading

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)


SELECTION_FORM_DOCUMENT_TYPES = [
    ('passport_size_photo', 'Passport Size Photo'),
    ('marksheet_10th', '10th Marksheet'),
    ('marksheet_12th', '12th Marksheet'),
    ('graduation', 'Graduation Certificate'),
    ('post_graduation', 'Post Graduation Certificate'),
    ('certifications', 'Certifications'),
    ('experience_letter_1', 'Experience Letter (Previous Employer)'),
    ('experience_letter_2', 'Experience Letter (Additional)'),
    ('relieving_letter', 'Relieving Letter'),
    ('payslips', 'Payslips (Last 3 Months)'),
    ('pan_doc', 'PAN Card'),
    ('aadhaar_doc', 'Aadhaar Card'),
    ('permanent_address_proof', 'Permanent Address Proof'),
    ('current_address_proof', 'Current Address Proof'),
    ('cancelled_cheque', 'Cancelled Cheque'),
]

VERIFICATION_STATUS = [
    ('pending', 'Pending'),
    ('verified', 'Verified'),
    ('mismatch', 'Mismatch'),
    ('skipped', 'Skipped'),
    ('failed', 'Failed'),
]


class HrApplicantDocument(models.Model):
    _name = 'hr.applicant.document'
    _description = 'Applicant Selection Form Document'
    _inherit = ['mail.thread']
    _order = 'document_type'

    applicant_id = fields.Many2one(
        'hr.applicant', string='Applicant',
        required=True, ondelete='cascade', index=True,
    )
    document_type = fields.Selection(
        SELECTION_FORM_DOCUMENT_TYPES,
        string='Document Type', required=True,
    )
    s3_url = fields.Char(string='S3 URL', required=True)
    file_name = fields.Char(string='File Name')
    mime_type = fields.Char(string='MIME Type')
    file_size = fields.Integer(string='File Size (bytes)')
    uploaded_at = fields.Datetime(
        string='Uploaded At', default=fields.Datetime.now,
    )
    uploaded_by_id = fields.Many2one(
        'res.users', string='Uploaded By',
        default=lambda self: self.env.user,
    )
    verification_status = fields.Selection(
        VERIFICATION_STATUS,
        string='Verification', default='pending',
        tracking=True, index=True,
    )
    verification_confidence = fields.Float(
        string='Confidence', digits=(3, 2),
    )
    ocr_text = fields.Text(string='OCR Text')
    ocr_matched_keywords = fields.Char(string='Matched Keywords')
    verification_error = fields.Text(string='Verification Error')
    verified_at = fields.Datetime(string='Verified At')

    _sql_constraints = [
        (
            'uniq_applicant_doc_type',
            'unique(applicant_id, document_type)',
            'Only one document per document type per applicant is allowed.',
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._schedule_verification()
        return records

    def write(self, vals):
        reschedule = 's3_url' in vals or 'document_type' in vals
        if reschedule and 'verification_status' not in vals:
            vals = dict(vals, verification_status='pending')
        res = super().write(vals)
        if reschedule:
            self._schedule_verification()
        return res

    def action_verify_document(self):
        for rec in self:
            rec._run_verification_sync()
        return True

    def _schedule_verification(self):
        if self.env.context.get('skip_verify_schedule'):
            return
        to_verify = self.filtered(lambda r: r.s3_url)
        if not to_verify:
            return
        record_ids = tuple(to_verify.ids)
        registry = self.env.registry
        dbname = self.env.cr.dbname

        def _fire_thread():
            threading.Thread(
                target=_verify_worker,
                args=(registry, record_ids),
                name=f'hr-applicant-doc-verify-{dbname}',
                daemon=True,
            ).start()

        self.env.cr.postcommit.add(_fire_thread)

    def _run_verification_sync(self):
        self.ensure_one()
        from odoo.addons.ethara_hrms_extension.services import document_verifier
        result = document_verifier.verify_document(
            url=self.s3_url,
            document_type=self.document_type,
            file_name=self.file_name,
        )
        self.sudo().write({
            'verification_status': result.status,
            'verification_confidence': result.confidence,
            'ocr_text': (result.text or '')[:20000] or False,
            'ocr_matched_keywords': (
                ', '.join(result.matched) if result.matched else False
            ),
            'verification_error': result.error or False,
            'verified_at': fields.Datetime.now(),
        })


def _verify_worker(registry, record_ids):
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            docs = env['hr.applicant.document'].browse(record_ids).exists()
            for doc in docs:
                try:
                    doc._run_verification_sync()
                except Exception:
                    _logger.exception(
                        'document_verifier: verification failed for doc %s',
                        doc.id,
                    )
            cr.commit()
    except Exception:
        _logger.exception('document_verifier: worker crashed')
