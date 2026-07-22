import json
import logging
import threading

import odoo
from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

JOB_STATES = [
    ('queued', 'Queued'),
    ('running', 'Running'),
    ('done', 'Done'),
    ('failed', 'Failed'),
]


class DocumensoBulkSendJob(models.Model):
    _name = 'documenso.bulk.send.job'
    _description = 'Documenso Bulk Send Job'
    _order = 'create_date desc, id desc'

    name = fields.Char(required=True, default='Bulk Send')
    state = fields.Selection(JOB_STATES, default='queued', required=True, index=True)
    template_ids_json = fields.Text(required=True)
    applicant_ids_json = fields.Text(required=True)
    job_id = fields.Many2one('hr.job')
    distribute = fields.Boolean(default=True)
    title_override = fields.Char()
    extra_prefill_json = fields.Text()

    total = fields.Integer(default=0)
    sent_count = fields.Integer(default=0)
    failed_count = fields.Integer(default=0)
    skipped_count = fields.Integer(default=0)
    results_json = fields.Text(default='{"sent":[],"failed":[],"skipped_no_email":[]}')

    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    error = fields.Text()

    def _serialize_state(self):
        self.ensure_one()
        try:
            results = json.loads(self.results_json or '{}')
        except (TypeError, ValueError):
            results = {}
        return {
            'id': self.id,
            'state': self.state,
            'total': self.total,
            'sent_count': self.sent_count,
            'failed_count': self.failed_count,
            'skipped_no_email_count': self.skipped_count,
            'progress_percent': round(
                (self.sent_count + self.failed_count + self.skipped_count) * 100.0 /
                max(1, self.total), 1),
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'error': self.error or None,
            'sent': results.get('sent', []),
            'failed': results.get('failed', []),
            'skipped_no_email': results.get('skipped_no_email', []),
        }

    def _launch_background(self):
        self.ensure_one()
        job_id = self.id
        dbname = self.env.cr.dbname
        uid = self.env.uid
        thread = threading.Thread(
            target=self._background_worker, args=(dbname, uid, job_id))
        thread.daemon = True
        thread.start()

    @staticmethod
    def _background_worker(dbname, uid, job_id):
        try:
            with odoo.registry(dbname).cursor() as cr:
                env = api.Environment(cr, uid, {})
                job = env['documenso.bulk.send.job'].browse(job_id)
                if not job.exists():
                    return
                job._run()
        except Exception:
            _logger.exception("Documenso bulk send background worker crashed")

    def _run(self):
        self.ensure_one()
        env = self.env
        self.write({'state': 'running', 'started_at': fields.Datetime.now()})
        env.cr.commit()

        try:
            template_ids = json.loads(self.template_ids_json)
            applicant_ids = json.loads(self.applicant_ids_json)
            extra_prefill = json.loads(self.extra_prefill_json or '{}')
        except (TypeError, ValueError) as exc:
            self.write({
                'state': 'failed', 'error': 'Invalid job payload: %s' % exc,
                'finished_at': fields.Datetime.now(),
            })
            env.cr.commit()
            return

        Template = env['documenso.template'].sudo()
        Applicant = env['hr.applicant'].sudo()
        Contract = env['documenso.contract'].sudo()

        templates = Template.browse(template_ids).exists()
        applicants = Applicant.browse(applicant_ids).exists()
        sendable = applicants.filtered(lambda a: a.email_from)
        skipped_no_email = [
            {'id': a.id, 'name': a.partner_name or a.display_name}
            for a in (applicants - sendable)
        ]

        self.write({'total': len(applicant_ids), 'skipped_count': len(skipped_no_email)})
        env.cr.commit()

        results = {'sent': [], 'failed': [], 'skipped_no_email': skipped_no_email}
        first_doc_class = templates[:1].doc_class or 'contract'

        for index, applicant in enumerate(sendable, start=1):
            try:
                contract = Contract.create({
                    'applicant_id': applicant.id,
                    'job_id': (self.job_id.id or applicant.job_id.id) or False,
                    'doc_class': first_doc_class,
                    'template_ids': [(6, 0, templates.ids)],
                    'template_id': templates[:1].id,
                })
                contract.action_send(
                    template_ids=templates.ids,
                    distribute=self.distribute,
                    title_override=self.title_override or False,
                    extra_prefill=extra_prefill,
                )
                env.cr.commit()
                results['sent'].append({
                    'applicant_id': applicant.id,
                    'contract_id': contract.id,
                    'documenso_id': contract.documenso_id,
                    'email': applicant.email_from,
                })
                self.write({
                    'sent_count': len(results['sent']),
                    'results_json': json.dumps(results),
                })
                env.cr.commit()
            except Exception as exc:
                env.cr.rollback()
                _logger.exception("Bulk send failed for applicant %s", applicant.id)
                results['failed'].append({
                    'applicant_id': applicant.id,
                    'email': applicant.email_from,
                    'error': str(exc),
                })
                self.write({
                    'failed_count': len(results['failed']),
                    'results_json': json.dumps(results),
                })
                env.cr.commit()
            if index % 25 == 0:
                _logger.info(
                    "Bulk send job %s progress: %s / %s", self.id, index, len(sendable))

        self.write({'state': 'done', 'finished_at': fields.Datetime.now()})
        env.cr.commit()
