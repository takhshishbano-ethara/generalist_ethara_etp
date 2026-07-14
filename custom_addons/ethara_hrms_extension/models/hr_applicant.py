import json
import logging
import re
import threading
from html import unescape
from io import BytesIO

import odoo
from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_RESUME_RECOMMENDATIONS = [
    ('shortlist', 'Shortlist'),
    ('reject', 'Reject'),
    ('maybe', 'Maybe'),
    ('needs_review', 'Needs Review'),
]
_RESUME_LLM_STATUSES = [
    ('pending', 'Pending'),
    ('processing', 'Processing'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
]


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'

    candidate_id = fields.Many2one(
        'ethara.candidate', string='Candidate',
        ondelete='set null', index=True,
    )
    portfolio_url = fields.Char(string='Portfolio URL')
    github_url = fields.Char(string='GitHub URL')
    resume_url = fields.Char(string='Resume URL')
    cancellation_reason = fields.Text(string='Cancellation Reason')
    status_updated_at = fields.Datetime(string='Status Updated At')

    job_history_ids = fields.One2many(
        'hr.applicant.job.history', 'applicant_id',
        string='Job Change History',
    )

    resume_score = fields.Float(string='Resume Score', digits=(4, 1))
    resume_recommendation = fields.Selection(
        _RESUME_RECOMMENDATIONS, string='LLM Recommendation',
    )
    resume_summary = fields.Text(string='Resume Summary')
    resume_strengths = fields.Text(string='Strengths')
    resume_gaps = fields.Text(string='Gaps')
    resume_screening_payload = fields.Text(string='Screening Payload (JSON)')
    resume_llm_status = fields.Selection(
        _RESUME_LLM_STATUSES, string='LLM Status', default='pending',
    )
    resume_llm_error = fields.Text(string='LLM Error')
    resume_llm_model_used = fields.Char(string='LLM Model Used')
    resume_llm_prompt_tokens = fields.Integer(string='Prompt Tokens')
    resume_llm_completion_tokens = fields.Integer(string='Completion Tokens')
    resume_llm_latency_ms = fields.Integer(string='LLM Latency (ms)')
    resume_screened_at = fields.Datetime(string='Last Screened At')

    def write(self, vals):
        if 'job_id' in vals:
            History = self.env['hr.applicant.job.history'].sudo()
            new_job_id = vals.get('job_id') or False
            for applicant in self:
                old_job_id = applicant.job_id.id if applicant.job_id else False
                if old_job_id != new_job_id:
                    History.create({
                        'applicant_id': applicant.id,
                        'previous_job_id': old_job_id,
                        'new_job_id': new_job_id,
                        'changed_by_id': self.env.uid,
                    })
        return super().write(vals)

    @api.onchange('candidate_id', 'active', 'stage_id')
    def _onchange_warn_active_application(self):
        if not self.candidate_id or not self.active:
            return
        if self.stage_id and self.stage_id.hired_stage:
            return
        if self.refuse_reason_id:
            return
        domain = [
            ('candidate_id', '=', self.candidate_id.id),
            ('active', '=', True),
            ('refuse_reason_id', '=', False),
        ]
        if isinstance(self.id, int):
            domain.append(('id', '!=', self.id))
        others = self.env['hr.applicant'].sudo().search(domain).filtered(
            lambda a: not (a.stage_id and a.stage_id.hired_stage)
        )
        if others:
            return {
                'warning': {
                    'title': "Existing Application",
                    'message': (
                        "Candidate '%s' already has an active application "
                        "(id %s) for job '%s'. Cancel it before applying to "
                        "another job."
                    ) % (
                        self.candidate_id.name,
                        others[0].id,
                        others[0].job_id.name or '?',
                    ),
                }
            }

    def action_screen_resume(self):
        self.ensure_one()
        from odoo.addons.ethara_hrms_extension.services import llm_client

        missing = []
        if not self.job_id:
            missing.append('Applied Job (job_id)')
        if not (self.resume_url or '').strip():
            missing.append('Resume URL (resume_url)')
        if missing:
            raise UserError(
                'Cannot screen resume — the following required field(s) are '
                'missing on this applicant:\n\n  • %s\n\n'
                'Please fill them in and try again.' % '\n  • '.join(missing)
            )

        ICP = self.env['ir.config_parameter'].sudo()
        api_key = (ICP.get_param('ethara_hrms.llm_api_key', '') or '').strip()
        if not api_key:
            raise UserError(
                'LLM API key not configured. Set the '
                '"ethara_hrms.llm_api_key" system parameter '
                '(Settings → Technical → System Parameters).'
            )
        base_url = ICP.get_param(
            'ethara_hrms.llm_base_url', llm_client.DEFAULT_BASE_URL,
        )
        model = ICP.get_param(
            'ethara_hrms.llm_model', llm_client.DEFAULT_LLM_MODEL,
        )
        timeout = int(ICP.get_param('ethara_hrms.llm_timeout', '60'))
        max_retries = int(ICP.get_param('ethara_hrms.llm_max_retries', '3'))

        resume_text = self._extract_resume_text()
        if not resume_text:
            raise UserError(
                'Could not extract text from resume_url. Check the Odoo log '
                'for the exact reason (HTTP error, timeout, scanned PDF, or '
                'encrypted PDF).'
            )

        system_prompt = (
            "You are an expert technical recruiter evaluating a candidate's "
            "resume for a job opening. Respond with a single JSON object "
            "containing exactly these keys: matchScore (integer 0-100), "
            "recommendation (one of: shortlist, reject, maybe, needs_review), "
            "summary (2-3 sentences), strengths (array of short strings), "
            "gaps (array of short strings). Do not include any prose "
            "outside the JSON object."
        )
        user_text = (
            "Job title: %s\n\nJob description:\n%s\n\n"
            "Candidate resume (first 6000 chars):\n%s"
        ) % (
            self._resume_job_title(),
            self._resume_job_description(),
            resume_text[:6000],
        )

        self.write({'resume_llm_status': 'processing', 'resume_llm_error': False})
        try:
            result = llm_client.chat_completion(
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_text=user_text,
                temperature=0.2,
                base_url=base_url,
                timeout=timeout,
                max_retries=max_retries,
                app_title='Ethara HRMS Resume Screening',
            )
        except Exception as exc:
            _logger.exception(
                'Resume screening failed for hr.applicant %s', self.id,
            )
            self.write({
                'resume_llm_status': 'failed',
                'resume_llm_error': str(exc),
            })
            raise UserError('Screening failed: %s' % exc) from exc

        payload = self._parse_screening_json(result.get('content') or '')
        strengths = payload.get('strengths') or []
        gaps = payload.get('gaps') or []
        self.write({
            'resume_score': self._clamp_score(payload.get('matchScore')),
            'resume_recommendation': self._normalize_recommendation(
                payload.get('recommendation'),
            ),
            'resume_summary': payload.get('summary') or '',
            'resume_strengths': '\n'.join(
                '- %s' % s for s in strengths if s
            ),
            'resume_gaps': '\n'.join('- %s' % s for s in gaps if s),
            'resume_screening_payload': json.dumps(payload, indent=2),
            'resume_llm_status': 'completed',
            'resume_llm_error': False,
            'resume_llm_model_used': result.get('model') or model,
            'resume_llm_prompt_tokens': int(result.get('prompt_tokens') or 0),
            'resume_llm_completion_tokens': int(
                result.get('completion_tokens') or 0,
            ),
            'resume_llm_latency_ms': int(result.get('latency_ms') or 0),
            'resume_screened_at': fields.Datetime.now(),
        })
        self._advance_stage_after_screening()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Resume screened',
                'message': 'Verdict: %s · Score: %s/100' % (
                    self.resume_recommendation or '?',
                    int(self.resume_score or 0),
                ),
                'type': 'success',
                'sticky': False,
            },
        }

    def _extract_resume_text(self):
        self.ensure_one()
        url = (self.resume_url or '').strip()
        if not url:
            return ''
        return self._extract_resume_text_from_url(url)

    def _extract_resume_text_from_url(self, url):
        pdf_bytes = self._http_get_pdf(url)
        if not pdf_bytes:
            return ''
        try:
            return self._pdf_bytes_to_text(pdf_bytes)
        except Exception as exc:
            _logger.warning(
                'Resume PDF parse failed for %s: %s', url, exc,
            )
            return ''

    @staticmethod
    def _http_get_pdf(url):
        try:
            import requests
            resp = requests.get(
                url,
                headers={'User-Agent': 'Ethara-HRMS-Screening/1.0'},
                timeout=30,
                allow_redirects=True,
            )
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            _logger.info(
                'Resume URL fetch via requests failed (%s); trying curl fallback',
                exc,
            )
        import shutil
        import subprocess
        import tempfile
        curl = shutil.which('curl')
        if not curl:
            _logger.warning('curl not on PATH — cannot fetch %s', url)
            return b''
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tf:
            tmp_path = tf.name
        try:
            result = subprocess.run(
                [
                    curl, '-sSL', '--max-time', '30',
                    '-A', 'Ethara-HRMS-Screening/1.0',
                    '-o', tmp_path, url,
                ],
                capture_output=True, timeout=35, check=False,
            )
            if result.returncode != 0:
                _logger.warning(
                    'curl fetch failed for %s: rc=%s stderr=%s',
                    url, result.returncode,
                    (result.stderr or b'').decode(errors='replace')[:200],
                )
                return b''
            with open(tmp_path, 'rb') as fh:
                return fh.read()
        except Exception as exc:
            _logger.warning('curl fetch exception for %s: %s', url, exc)
            return b''
        finally:
            try:
                import os
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _pdf_bytes_to_text(pdf_bytes):
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        text = '\n'.join((p.extract_text() or '') for p in reader.pages)
        return text[:20000] if text.strip() else ''

    def _resume_job_title(self):
        self.ensure_one()
        return (
            (self.job_id.name if self.job_id else None)
            or self.name
            or 'Unknown role'
        )

    def _resume_job_description(self):
        self.ensure_one()
        job = self.job_id
        pieces = []
        if job:
            if getattr(job, 'screening_prompt', None):
                pieces.append(job.screening_prompt)
            if job.description:
                pieces.append(
                    re.sub(
                        r'\s+', ' ',
                        unescape(re.sub(r'<[^>]+>', ' ', job.description)),
                    ).strip()
                )
        return '\n\n'.join(p for p in pieces if p) or (
            'Unspecified role — evaluate general fit.'
        )

    @staticmethod
    def _parse_screening_json(content):
        text = (content or '').strip()
        if text.startswith('```'):
            text = text.strip('`').strip()
            if text.lower().startswith('json'):
                text = text[4:].strip()
        try:
            return json.loads(text)
        except Exception:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    pass
            return {
                'summary': text[:500],
                'recommendation': 'needs_review',
                'matchScore': 0,
                'strengths': [],
                'gaps': [],
            }

    @staticmethod
    def _clamp_score(value):
        try:
            score = float(value or 0)
        except (TypeError, ValueError):
            score = 0.0
        return max(0.0, min(100.0, score))

    @staticmethod
    def _normalize_recommendation(value):
        v = (value or '').strip().lower()
        allowed = {'shortlist', 'reject', 'maybe', 'needs_review'}
        if v in allowed:
            return v
        if v in ('accept', 'yes', 'pass', 'proceed', 'hire', 'ship'):
            return 'shortlist'
        if v in ('no', 'fail', 'decline', 'block'):
            return 'reject'
        if v in ('hold', 'review', 'unsure'):
            return 'needs_review'
        return 'needs_review'

    def _advance_stage_after_screening(self):
        self.ensure_one()
        Stage = self.env['hr.recruitment.stage'].sudo()
        Reason = self.env['hr.applicant.refuse.reason'].sudo()
        rec = self.resume_recommendation

        if rec == 'reject':
            reason = Reason.search(
                [('name', '=', 'Rejected by AI resume screening')], limit=1,
            ) or Reason.create({'name': 'Rejected by AI resume screening'})
            self.sudo().write({'refuse_reason_id': reason.id, 'active': False})
            return

        if rec != 'shortlist':
            return

        current_seq = self.stage_id.sequence if self.stage_id else -1
        job_ids = self.job_id.ids or [0]
        next_stage = Stage.search(
            [
                ('sequence', '>', current_seq),
                '|', ('job_ids', '=', False), ('job_ids', 'in', job_ids),
            ],
            order='sequence asc, id asc',
            limit=1,
        )
        if next_stage:
            self.sudo().write({'stage_id': next_stage.id})

    def _schedule_resume_screening(self):
        self.ensure_one()
        if not self.id:
            return
        dbname = self.env.cr.dbname
        applicant_id = self.id

        def _run_in_thread():
            try:
                registry = odoo.modules.registry.Registry(dbname)
                with registry.cursor() as cr:
                    thread_env = api.Environment(cr, SUPERUSER_ID, {})
                    applicant = thread_env['hr.applicant'].browse(applicant_id).exists()
                    if not applicant:
                        return
                    applicant.action_screen_resume()
            except Exception:
                _logger.exception(
                    'Async resume screening failed for applicant %s',
                    applicant_id,
                )

        def _fire_thread():
            threading.Thread(
                target=_run_in_thread,
                name='ethara-resume-screen-%s' % applicant_id,
                daemon=True,
            ).start()

        self.env.cr.postcommit.add(_fire_thread)
