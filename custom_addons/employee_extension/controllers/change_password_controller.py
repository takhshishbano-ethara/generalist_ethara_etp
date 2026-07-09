import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
    is_valid_email,
)

_logger = logging.getLogger(__name__)


class ChangePasswordController(http.Controller):

    @validate_token
    @http.route('/api/v1/change_password/send_otp', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    def change_password_send_otp(self, **params):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            email = (user_id.login or '').strip().lower()
            if not is_valid_email(email):
                return return_Response(message="Your account does not have a valid email address.", status=400, errors=[])
            otp_record, err = request.env['employee.registration.otp'].sudo().generate_for_email(email)
            if not otp_record:
                return return_Response(message=err or "Unable to generate OTP. Please try again later.", status=400, errors=[])
            template = request.env.ref('employee_extension.email_template_change_password_otp', raise_if_not_found=False)
            if not template:
                return return_Response(message="Email template is not configured.", status=400, errors=[])
            try:
                template.sudo().with_context(
                    ttl_minutes=10,
                    user_name=user_id.name,
                ).send_mail(otp_record.id, force_send=True)
            except Exception as e:
                _logger.error('Failed to send password-change OTP to %s: %s', email, str(e))
                return return_Response(message="Failed to send OTP. Please try again later.", status=400, errors=[str(e)])
            return return_Response(message="OTP has been sent to your registered email.", status=200)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/change_password/verify_otp', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({
        'otp': {'type': 'str', 'required': True},
        'new_password': {'type': 'str', 'required': True},
        'confirm_password': {'type': 'str', 'required': True},
    })
    def change_password_verify_otp(self, **params):
        try:
            jdata = params.get('jdata')
            otp = (jdata.get('otp') or '').strip()
            new_password = (jdata.get('new_password') or '').strip()
            confirm_password = (jdata.get('confirm_password') or '').strip()
            if new_password != confirm_password:
                return return_Response(message="The password and confirm password do not match.", status=400, errors=[])
            if len(new_password) < 6:
                return return_Response(message="Password must be at least 6 characters long.", status=400, errors=[])
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            email = (user_id.login or '').strip().lower()
            ok, msg = request.env['employee.registration.otp'].sudo().verify_for_email(email, otp)
            if not ok:
                return return_Response(message=msg or "Invalid OTP.", status=400, errors=[])
            user_id.sudo().password = new_password
            request.env['employee.registration.otp'].sudo().search([('email', '=', email)]).unlink()
            access_token = request.env['api.access_token'].sudo().search([('user_id', '=', user_id.id)])
            if access_token:
                for token in access_token:
                    token.sudo().unlink()
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Password has been updated successfully. Please login with your new password.", status=200)
