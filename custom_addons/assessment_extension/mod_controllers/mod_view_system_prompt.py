from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_request,
    validate_token,
)

from .common import (
    error_response,
    manager_payload,
    require_assessment_manager,
)


class ModViewSystemPromptController(http.Controller):

    @http.route(
        '/api/v1/assessment_extension/system_prompt/current',
        type='http', auth='none', methods=['GET'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def get_current_system_prompt(self, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        prompt = request.env['etp.assessment.system.prompt'].sudo().search(
            [('is_current', '=', True), ('is_active', '=', True)], limit=1,
        )
        if not prompt:
            return error_response('NOT_FOUND', 'No current system prompt is configured.', status=404)

        drawer = {
            'type': 'drawer',
            'modal_id': 'MOD-View-System-Prompt',
            'title': 'System prompt',
            'subtitle_chip': {
                'label': '%s (current)' % prompt.version_label,
                'token': 'neutral',
            },
            'label': 'GENERATION SYSTEM PROMPT',
            'body_text': prompt.body,
            'is_readonly': True,
            'locked_reason': 'Read-only \u00b7 platform-managed',
            'footer': {
                'lock_icon': True,
                'lock_text': 'Read-only \u00b7 platform-managed',
                'actions': [
                    {'key': 'close', 'label': 'Close', 'style': 'secondary'},
                ],
            },
            'meta': {
                'prompt_id': prompt.id,
                'version_label': prompt.version_label,
                'is_current': prompt.is_current,
            },
        }
        return return_Response(message='OK', status=200, data=manager_payload([drawer]))

    @http.route(
        '/api/v1/assessment_extension/system_prompt/current',
        type='http', auth='none', methods=['POST', 'PUT', 'PATCH', 'DELETE'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    def reject_system_prompt_writes(self, **kwargs):
        return error_response('READ_ONLY', 'The system prompt is read-only.', status=405)
