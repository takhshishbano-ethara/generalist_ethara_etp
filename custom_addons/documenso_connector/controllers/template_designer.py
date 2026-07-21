# -*- coding: utf-8 -*-
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


class DocumensoTemplateDesignerController(http.Controller):

    @http.route('/documenso/template/<int:template_id>/pdf',
                type='http', auth='user', csrf=False)
    def template_pdf(self, template_id, **kwargs):
        template = request.env['documenso.template'].browse(template_id)
        try:
            template.check_access_rights('read')
            template.check_access_rule('read')
        except AccessError:
            return request.not_found()
        if not template.exists() or not template.documenso_id:
            return request.not_found()
        client = request.env['res.config.settings'].sudo().get_client()
        try:
            pdf_bytes = client.download_template_pdf(template.documenso_id)
        except UserError as exc:
            _logger.warning("Template PDF fetch failed for %s: %s", template_id, exc)
            return request.make_response(str(exc), status=502,
                                         headers=[('Content-Type', 'text/plain')])
        except Exception:
            _logger.exception("Template PDF fetch crashed for %s", template_id)
            return request.make_response('Internal error', status=500,
                                         headers=[('Content-Type', 'text/plain')])
        return request.make_response(pdf_bytes, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Length', str(len(pdf_bytes))),
            ('Cache-Control', 'private, max-age=0, no-cache'),
        ])
