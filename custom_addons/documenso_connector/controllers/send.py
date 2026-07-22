import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 1000


def _parse_body():
    raw = request.httprequest.get_data() or b''
    if not raw:
        return {}
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


class DocumensoBulkSend(http.Controller):

    @http.route('/api/v1/documenso/send', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def bulk_send(self, **_kwargs):
        body = _parse_body()

        template_ids = body.get('template_ids') or ([body['template_id']] if body.get('template_id') else [])
        applicant_ids = body.get('applicant_ids') or []
        job_id = body.get('job_id')
        distribute = body.get('distribute', True)
        title_override = body.get('title_override') or False
        extra_prefill = body.get('extra_prefill') or {}

        if not template_ids:
            return return_Response(message="template_id or template_ids is required.", status=400)
        if not applicant_ids or not isinstance(applicant_ids, list):
            return return_Response(message="applicant_ids must be a non-empty list.", status=400)
        if len(applicant_ids) > MAX_BATCH_SIZE:
            return return_Response(
                message="Batch too large: %s applicants, max is %s." % (
                    len(applicant_ids), MAX_BATCH_SIZE),
                status=400,
            )
        if not isinstance(extra_prefill, dict):
            return return_Response(message="extra_prefill must be a JSON object.", status=400)

        env = request.env
        Template = env['documenso.template'].sudo()
        Applicant = env['hr.applicant'].sudo()

        templates = Template.browse(template_ids).exists()
        if len(templates) != len(set(template_ids)):
            return return_Response(message="One or more template_ids do not exist.", status=404)
        unpublished = templates.filtered(lambda t: not t.is_published)
        if unpublished:
            return return_Response(
                message="Templates must be published before sending: %s" % (
                    unpublished.mapped('title')),
                status=400,
            )

        applicants = Applicant.browse(applicant_ids).exists()
        if len(applicants) != len(set(applicant_ids)):
            missing = set(applicant_ids) - set(applicants.ids)
            return return_Response(
                message="Applicant IDs not found: %s" % sorted(missing), status=404)

        job = env['hr.job'].sudo().browse(job_id).exists() if job_id else False

        Job = env['documenso.bulk.send.job'].sudo()
        record = Job.create({
            'name': 'Bulk Send %s applicants' % len(applicant_ids),
            'template_ids_json': json.dumps(templates.ids),
            'applicant_ids_json': json.dumps(applicants.ids),
            'job_id': job.id if job else False,
            'distribute': bool(distribute),
            'title_override': title_override or False,
            'extra_prefill_json': json.dumps(extra_prefill),
            'total': len(applicant_ids),
        })
        env.cr.commit()
        record._launch_background()

        return return_Response(
            message="Bulk send queued.",
            status=202,
            data={
                'job_id': record.id,
                'state': record.state,
                'total': record.total,
                'poll_url': '/api/v1/documenso/send/%s' % record.id,
            },
        )

    @http.route('/api/v1/documenso/send/<int:job_id>', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def bulk_send_status(self, job_id, **_kwargs):
        record = request.env['documenso.bulk.send.job'].sudo().browse(job_id)
        if not record.exists():
            return return_Response(message="Job not found.", status=404)
        return return_Response(
            message="Success", status=200, data=record._serialize_state())
