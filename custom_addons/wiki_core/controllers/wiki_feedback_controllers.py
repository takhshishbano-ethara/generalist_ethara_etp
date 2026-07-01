from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

# Shared with the rest of the wiki_core API (merged from wiki_feedback).
from .wiki_controllers import _read_params


class WikiFeedbackController(http.Controller):
    """Submit 'Feedback on this page' from the Employee Portal wiki."""

    @http.route('/api/v1/wiki/feedback', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def submit_feedback(self, **kwargs):
        params = _read_params()
        message = (params.get('message') or '').strip()
        helpful = params.get('helpful') or False
        if helpful not in ('up', 'down'):
            helpful = False
        # A submission must carry either a written message or a helpful vote.
        if not message and not helpful:
            return return_Response(
                message="'message' or 'helpful' is required.", status=400)
        employee = request.env.user.employee_id
        feedback = request.env['wiki.feedback'].sudo().create({
            'page': params.get('page') or '',
            'page_label': params.get('page_label') or '',
            'message': message,
            'helpful': helpful,
            'employee_id': employee.id if employee else False,
            'user_id': request.env.user.id,
        })
        return return_Response(message='Success', status=200, data={'data': {
            'reference': feedback.reference,
            'status': feedback.state,
        }})
