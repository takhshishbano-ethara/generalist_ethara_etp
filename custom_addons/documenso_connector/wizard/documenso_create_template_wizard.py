import base64
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DocumensoCreateTemplateWizard(models.TransientModel):
    _name = 'documenso.create.template.wizard'
    _description = 'Documenso Create Template Wizard'

    title = fields.Char(string='Title', required=True)
    file_data = fields.Binary(string='PDF File', required=True, attachment=False)
    filename = fields.Char(string='Filename')
    job_id = fields.Many2one('hr.job', string='Applicable Job', required=True, ondelete='cascade')
    doc_class = fields.Selection([
        ('contract', 'Contract'),
        ('compliance', 'Compliance'),
    ], string='Document Class', default='contract', required=True)
    visibility = fields.Selection([
        ('EVERYONE', 'Everyone'),
        ('MANAGER_AND_ABOVE', 'Manager and above'),
        ('ADMIN', 'Admin only'),
    ], string='Visibility', default='EVERYONE')
    template_type = fields.Selection([
        ('PRIVATE', 'Private'),
        ('PUBLIC', 'Public'),
    ], string='Type', default='PRIVATE')

    def action_create(self):
        self.ensure_one()
        client = self.env['res.config.settings'].sudo().get_client()
        pdf_bytes = base64.b64decode(self.file_data)
        filename = self.filename or (self.title + '.pdf')
        created = client.create_template(
            title=self.title,
            pdf_bytes=pdf_bytes,
            filename=filename,
            visibility=self.visibility or None,
            template_type=self.template_type or None,
        )
        _logger.info("Documenso create_template response: %s", created)
        template_id = None
        if isinstance(created, dict):
            template_id = created.get('id') or created.get('templateId')
        if not template_id:
            raise UserError(_(
                "Documenso did not return a template id. Raw response: %s"
            ) % created)
        Template = self.env['documenso.template']
        record, _was_created = Template._upsert_from_list_payload(created)
        if not record:
            raise UserError(_(
                "Failed to mirror Documenso template (id=%s) locally."
            ) % template_id)
        record.write({
            'job_id': self.job_id.id,
            'doc_class': self.doc_class,
            'salary_bracket': getattr(self.job_id, 'salary_bracket', '') or record.salary_bracket,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Documenso Template'),
            'res_model': 'documenso.template',
            'view_mode': 'form',
            'res_id': record.id,
            'target': 'current',
        }
