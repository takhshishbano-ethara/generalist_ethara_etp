import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    is_valid_email,
    safe_get_value,
)
from odoo.addons.api_auth_gateway.controllers.main import validate_request

_logger = logging.getLogger(__name__)


def _iso_utc(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'


def _serialize_lead(lead):
    return {
        'id': safe_get_value(lead, 'id', 'int'),
        'firstName': safe_get_value(lead, 'first_name', 'str'),
        'lastName': safe_get_value(lead, 'last_name', 'str'),
        'email': safe_get_value(lead, 'email', 'str'),
        'company': safe_get_value(lead, 'company', 'str'),
        'queryType': safe_get_value(lead, 'query_type', 'str'),
        'message': safe_get_value(lead, 'message', 'str'),
        'createdAt': _iso_utc(lead.create_date),
        'updatedAt': _iso_utc(lead.write_date),
    }


class EtharaLeadController(http.Controller):

    @http.route('/api/v1/lead/create', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    @validate_request({})
    def create_lead(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            params = {**kwargs, **jdata}

            first_name = (params.get('first_name') or '').strip()
            last_name = (params.get('last_name') or '').strip()
            email = (params.get('email') or '').strip()
            company = (params.get('company') or '').strip()
            query_type = (params.get('query_type') or '').strip()
            message = (params.get('message') or '').strip()

            errors = []
            if not first_name:
                errors.append('first_name is required')
            if not last_name:
                errors.append('last_name is required')
            if not email or not is_valid_email(email):
                errors.append('valid email is required')
            if errors:
                return return_Response('Validation failed', 400, errors=errors)

            vals = {
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
                'company': company or False,
                'query_type': query_type or False,
                'message': message or False,
            }

            lead = request.env['ethara.lead'].sudo().create(vals)
            return return_Response(
                'Lead created', 200,
                data={'record': _serialize_lead(lead)},
            )
        except Exception as exc:
            _logger.exception('create_lead failed')
            return return_Response('Internal error', 500, errors=[str(exc)])
