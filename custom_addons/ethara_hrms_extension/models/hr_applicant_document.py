import logging

from odoo import api, fields, models

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
    ('rejected', 'Rejected'),
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
        return super().create(vals_list)

    def action_verify_document(self):
        return True

    def _schedule_verification(self):
        return

    def _run_verification_sync(self):
        return
