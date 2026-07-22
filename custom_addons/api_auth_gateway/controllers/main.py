from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value, generate_s3_link, is_valid_email, is_valid_mobile
from datetime import datetime, timedelta
import json
import logging
import re

IST_OFFSET = timedelta(hours=5, minutes=30)
_logger = logging.getLogger(__name__)

class ApiAuthController(http.Controller):

    def get_the_menuitem_list(self, domain=[]):
        # 1. Fetch all records once
        menu_lines = request.env['api.role.line'].sudo().search(domain)
        user_line_ids = request.env.user.user_role.line_ids.ids if request.env.user.user_role else []

        menu_map = {}
        roots = []

        # 2. First Pass: Create the data objects
        for line in menu_lines:
            menu_map[line.id] = {
                'id': line.menu_name or "",
                'is_visible': line.id in user_line_ids,
                'order': line.sequence or 0,
                'read': line.can_read or False,
                'write': line.can_write or False,
                'create': line.can_create or False,
                'delete': line.can_delete or False,
                'parent_id': line.parent_id.id if line.parent_id else None,
                'child_list': []
            }

        # 3. Second Pass: The "Same Code" logic for any level
        for line_id, item in menu_map.items():
            parent_id = item['parent_id']

            if parent_id and parent_id in menu_map:
                menu_map[parent_id]['child_list'].append(item)
            else:
                # If no parent, it's a top-level root
                roots.append(item)

        # 4. Recursive Helper: Sorts and ensures Parents are visible if Children are
        def finalize_tree(items):
            items.sort(key=lambda x: x['order'])
            for item in items:
                if item['child_list']:
                    finalize_tree(item['child_list'])
                    # Logic: If any child is visible, the parent must be visible too
                    if any(child['is_visible'] for child in item['child_list']):
                        item['is_visible'] = True
            return items

        role_data = finalize_tree(roots)
        return role_data

    @http.route('/api/v1/job/apply', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({'job_id': {'type': 'int', 'required': True}})
    def api_apply_job(self, **kwargs):
        jdata = kwargs.get('jdata') or {}
        expected_ctc = jdata.get('expected_ctc')
        if expected_ctc is None:
            expected_ctc = jdata.get('expectedCTC')
        return self.apply_job_position(
            job_id=jdata.get('job_id'), expected_ctc=expected_ctc,
        )

    def apply_job_position(self, job_id=None, expected_ctc=None):
        Applicant = request.env['hr.applicant'].sudo()
        Job = request.env['hr.job'].sudo()
        user = request.env.user

        if not job_id:
            return return_Response(message="job_id is required.", status=400)
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            return return_Response(
                message="job_id must be an integer.", status=400,
            )
        if expected_ctc in (None, ''):
            expected_ctc = None
        else:
            try:
                expected_ctc = float(expected_ctc)
            except (TypeError, ValueError):
                return return_Response(
                    message="expected_ctc must be a number.", status=400,
                )
            if expected_ctc < 0:
                return return_Response(
                    message="expected_ctc must be a non-negative number.",
                    status=400,
                )
        job = Job.browse(job_id).exists()
        if not job:
            return return_Response(
                message="Job posting not found.", status=404,
            )

        active_app = Applicant.search(
            [
                ('candidate_user_id', '=', user.id),
                ('active', '=', True),
                ('refuse_reason_id', '=', False),
                ('pipeline_status', '!=', 'rejected'),
            ],
            limit=1, order='id desc',
        )
        if active_app:
            if active_app.job_id and active_app.job_id.id == job.id:
                return return_Response(
                    message="You have already applied for this job.",
                    status=400,
                )
            if active_app.job_id:
                return return_Response(
                    message=(
                        "You have an active application for '%s'. "
                        "Withdraw it before applying to a new job."
                    ) % active_app.job_id.name,
                    status=400,
                )
            vals = {'job_id': job.id}
            if expected_ctc is not None:
                vals['expected_ctc'] = expected_ctc
            if job.department_id:
                vals['department_id'] = job.department_id.id
            active_app.write(vals)
            self._trigger_resume_screen(active_app)
            return return_Response(
                message="Application submitted for %s." % job.name,
                status=200,
                data={'applicant_id': active_app.id},
            )

        source_app = Applicant.with_context(active_test=False).search(
            [('candidate_user_id', '=', user.id)],
            limit=1, order='id desc',
        )
        if not source_app:
            return return_Response(
                message=(
                    "No candidate profile found for this user. "
                    "Please complete registration first."
                ),
                status=404,
            )

        new_vals = {
            'partner_name': source_app.partner_name or user.name,
            'email_from': source_app.email_from or user.email,
            'partner_phone': source_app.partner_phone,
            'candidate_user_id': user.id,
            'resume_url': source_app.resume_url,
            'job_id': job.id,
        }
        for optional in (
            'partner_id', 'aadhaar_number', 'aadhaar_card_url',
            'company_id', 'gender', 'birthday', 'experience',
            'experience_years', 'college_id', 'current_company',
            'current_ctc', 'expected_ctc', 'notice_period_days',
        ):
            if optional not in source_app._fields:
                continue
            value = source_app[optional]
            if not value:
                continue
            new_vals[optional] = value.id if hasattr(value, 'id') else value
        if expected_ctc is not None:
            new_vals['expected_ctc'] = expected_ctc
        if job.department_id:
            new_vals['department_id'] = job.department_id.id

        try:
            new_app = Applicant.create(new_vals)
        except Exception as exc:
            _logger.exception(
                "apply_job_position: failed to create applicant "
                "for user %s job %s", user.id, job.id,
            )
            return return_Response(
                message="Could not create application record.",
                status=500, errors=[str(exc)],
            )
        self._trigger_resume_screen(new_app)
        return return_Response(
            message="Application submitted for %s." % job.name,
            status=200,
            data={'applicant_id': new_app.id, 'is_reapplication': True},
        )

    def _trigger_resume_screen(self, applicant):
        try:
            applicant.sudo()._schedule_resume_screening()
        except Exception:
            _logger.exception(
                "Failed to schedule resume screening for applicant %s",
                applicant.id,
            )

    @http.route('/api/v1/auth_token', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({'login': {'type': 'str', 'required': True}, 'password': {'type': 'str', 'required': True}})
    def auth_token(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            login = jdata.get('login').lower().strip()
            password = jdata.get('password').strip()
            user = request.env['res.users'].sudo().search([('login', '=', login), ('active', 'in', [True, False])], limit=1, order='id desc')
            if user:
                if user.active:
                    credential = {'login': user.login, 'password': password, 'type': 'password'}
                    uid = request.session.authenticate(request.env, credential)
                else:
                    return return_Response(message="Your account has been deactivated. To reactivate it, please contact to the Administrator.", status=400)
            else:
                return return_Response(message="No user exists for the provided login credentials.", status=400)
        except Exception as e:
            return return_Response(message="Login failed. Please check your credentials.", status=400, errors=[str(e)])
        uid = uid['uid']
        if not uid:
            return return_Response(message="Login failed. Please check your credentials.", status=400)
        else:
            access_token, refresh  = request.env['api.access_token'].sudo().find_one_or_create_token(user_id=uid, create=True)
            if jdata.get('browser_name') or jdata.get('os_name') or jdata.get('location'):
                token_dict = {
                    'browser_name': jdata.get('browser_name'),
                    'os_name': jdata.get('os_name'),
                    'location': jdata.get('location')
                }
                if access_token:
                    token = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], limit=1)
                    if token:
                        token.sudo().write(token_dict)

            address = ""
            if request.env.user.partner_id.street:
                address += f"{request.env.user.partner_id.street}"
            if request.env.user.partner_id.city:
                address += f", {request.env.user.partner_id.city}"
            if request.env.user.partner_id.zip:
                address += f", {request.env.user.partner_id.zip}"
            if request.env.user.partner_id.state_id:
                address += f", {request.env.user.partner_id.state_id.name}"
            if request.env.user.partner_id.country_id:
                address += f", {request.env.user.partner_id.country_id.name}"
            role_data = self.get_the_menuitem_list(domain=[])
            role_candidate = request.env.ref(
                'api_auth_gateway.role_candidate_technical',
                raise_if_not_found=False,
            )
            user_role = request.env.user.user_role
            apply_result = None
            if (
                jdata.get('job_id')
                and role_candidate
                and user_role
                and user_role.id == role_candidate.id
            ):
                apply_resp = self.apply_job_position(job_id=jdata.get('job_id'))
                try:
                    body = json.loads(apply_resp.data)
                    data_block = body.get('data') or {}
                    apply_result = {
                        'status': body.get('status_code'),
                        'message': body.get('message'),
                        'applicant_id': data_block.get('applicant_id'),
                        'is_reapplication': data_block.get('is_reapplication', False),
                    }
                except Exception:
                    _logger.exception(
                        "Failed to parse apply_job_position response for user %s job_id %s",
                        uid, jdata.get('job_id'),
                    )
                    apply_result = {
                        'status': 500,
                        'message': 'Failed to process job application.',
                        'applicant_id': None,
                        'is_reapplication': False,
                    }
            res = {
                "data": {
                    'uid': uid,
                    'email': safe_get_value(request.env.user, 'login', 'str'),
                    'name': safe_get_value(request.env.user, 'name', 'str'),
                    'mobile': safe_get_value(request.env.user, 'phone', 'str'),
                    'access_token': access_token or "",
                    'refresh_token': refresh or "",
                    'address': address,
                    'user_role': safe_get_value(request.env.user, 'user_role.name', 'str'),
                    'user_type': safe_get_value(request.env.user, 'user_role.user_type', 'str'),
                    'role_department': safe_get_value(request.env.user, 'user_role.department_id.url_key', 'str'),
                    'profile_pic': "",
                    'permissions': role_data,
                    'apply_result': apply_result,
                }
            }
            return return_Response(message="Success", status=200, data=res)

    @http.route('/api/v1/refresh_token', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({'refresh_token': {'type': 'str', 'required': True}})
    def refresh_token(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            token_rec = request.env['api.access_token'].sudo().search([('refresh_token', '=', jdata.get('refresh_token'))], limit=1)

            if not token_rec:
                return return_Response(message="Invalid Refresh Token", status=401)
            access_token, refresh = token_rec.update_access_token()
            res = {
                'access_token': access_token or "",
                'refresh_token': refresh or ""
            }
            return return_Response(message="Success", status=200, data=res)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @http.route('/api/v1/auth_token_unlink', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def auth_token_unlink(self, **params):
        try:
            access_token = request.httprequest.headers.get('access_token')
            if not access_token:
                return return_Response(message="missing access token in request header", status=401)
            if access_token:
                access_token_data = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], order='id DESC', limit=1)
                if access_token_data:
                    access_token_data.sudo().unlink()
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Access Token Deleted Successfully", status=200)

    @http.route('/api/v1/sign_out_all_session', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def sign_out_all_session(self, **params):
        try:
            try:
                user_id = request.env['res.users'].sudo().browse(self.env.uid)
            except:
                user_id = request.env['res.users'].sudo().browse(request.env.uid)
            access_token = request.env['api.access_token'].sudo().search([('user_id', '=', user_id.id)])
            if access_token:
                for token in access_token:
                    token.sudo().unlink()
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Access Token Deleted Successfully", status=200)

    @http.route('/api/v1/get_menu_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_menu_list(self, **params):
        role_data = []
        try:
            try:
                user_id = request.env['res.users'].sudo().browse(self.env.uid)
            except:
                user_id = request.env['res.users'].sudo().browse(request.env.uid)
            domain = []
            if params.get('id'):
                domain = [('menu_name', '=', params.get('id')), ('parent_id.menu_name', '=', params.get('id'))]
            role_data = self.get_the_menuitem_list(domain=domain)

        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Access Token Deleted Successfully", status=200, data={"permissions": role_data})

    @validate_token
    @http.route('/api/v1/update_profile_information', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({})
    def update_profile_information(self, **params):
        try:
            jdata = params.get('jdata')
            s3_connector_id = request.env['s3.connector'].sudo().search([], limit=1)
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            user_dict = {}
            partner_dict = {}

            if jdata.get('profile_pic'):
                partner_dict['profile_url'] = generate_s3_link(jdata.get('profile_pic'), uid=user_id.id) if f"{s3_connector_id.cdn_url}" not in jdata.get('profile_pic') else jdata.get('profile_pic')
            if jdata.get('name'):
                user_dict['name'] = jdata.get('name')
            if jdata.get('bio'):
                partner_dict['bio_data'] = jdata.get('bio')
            if jdata.get('location'):
                partner_dict['location'] = jdata.get('location')
            if jdata.get('email'):
                if not is_valid_email(jdata.get('email')):
                    return return_Response(message="Please enter a valid email address.", status=400, errors=[])
                else:
                    user_dict['email'] = jdata.get('email')
            if jdata.get('mobile'):
                if not is_valid_mobile(jdata.get('mobile')):
                    return return_Response(message="Please enter a valid mobile number.", status=400, errors=[])
                else:
                    user_dict['phone'] = jdata.get('mobile')

            # if jdata.get('new_password'):
            #     if not jdata.get('confirm_password') or not jdata.get('current_password'):
            #         return return_Response(message="Missing required parameter.", status=400, errors=[])
            #
            #     if jdata.get('new_password') != jdata.get('confirm_password'):
            #         return return_Response(message="The password and confirm password do not match.", status=400, errors=[])
            #
            #     credential = {'login': user_id.login, 'password': jdata.get('current_password'), 'type': 'password'}
            #     uid = request.session.authenticate(
            #         request.session.db,
            #         credential
            #     )
            #     if 'uid' not in uid:
            #         return return_Response(message="Incorrect Password.", status=400, errors=[])
            #     user_dict['password'] = jdata.get('new_password')

            if user_dict:
                user_id.sudo().write(user_dict)
            if partner_dict:
                user_id.partner_id.sudo().write(partner_dict)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Profile Updated Successfully", status=200)

    @validate_token
    @http.route('/api/v1/change_users_password', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({'old_password': {'type': 'str', 'required': True}, 'new_password': {'type': 'str', 'required': True}, 'confirm_password': {'type': 'str', 'required': True}})
    def change_users_password(self, **params):
        try:
            jdata = params.get('jdata')
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if jdata.get('new_password') != jdata.get('confirm_password'):
                return return_Response(message="The password and confirm password do not match.", status=400, errors=[])
            credential = {'login': user_id.login, 'password': jdata.get('old_password'), 'type': 'password'}
            uid = request.session.authenticate(request.env, credential)
            if 'uid' not in uid:
                return return_Response(message="Incorrect Password.", status=400, errors=[])
            user_id.sudo().password = jdata.get('new_password')
            access_token = request.env['api.access_token'].sudo().search([('user_id', '=', user_id.id)])
            if access_token:
                for token in access_token:
                    token.sudo().unlink()
            # Notify the user that their password changed. Runs only after the
            # DB update succeeds and never blocks/breaks the API response.
            self._send_password_changed_email(user_id)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Profile Updated Successfully", status=200)

    # Basic RFC-5322-ish sanity check; intentionally permissive.
    _EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

    def _send_password_changed_email(self, user):
        """Send a 'password changed' confirmation email to ``user``.

        Fired only after the password has been updated successfully. Every step
        of the flow is logged (prefix ``PWD-CHANGE-EMAIL``) so that delivery can
        be audited per-user and any failure is attributable to an exact stage
        (recipient lookup, template lookup, mail creation, SMTP send). Every
        failure is logged and swallowed so it can never affect the password
        update outcome.

        Delivery model: the mail record is persisted (``auto_delete=False``) as
        a security audit trail, then an immediate send is attempted so problems
        surface at request time instead of silently sitting in the hourly mail
        queue. The send is isolated with ``raise_exception=False`` and a
        surrounding ``try`` so SMTP issues never bubble up.
        """
        lp = 'PWD-CHANGE-EMAIL [user_id=%s login=%s]' % (user.id, user.login)
        try:
            _logger.info('%s step=START', lp)

            # -- Step 1: resolve the recipient --
            # `login` is the email in this system, so it is a safe fallback when
            # the dedicated `email` field was never populated.
            recipient = (user.email or '').strip() or (user.login or '').strip()
            _logger.info(
                '%s step=RECIPIENT_LOOKUP email_field=%r login=%r resolved=%r',
                lp, user.email, user.login, recipient)
            if not recipient:
                _logger.error(
                    '%s step=ABORT reason=NO_RECIPIENT (no email and no login)',
                    lp)
                return
            if not self._EMAIL_RE.match(recipient):
                _logger.error(
                    '%s step=ABORT reason=INVALID_EMAIL_FORMAT recipient=%r',
                    lp, recipient)
                return

            # -- Step 2: collect request metadata (device / IP / time) --
            httprequest = getattr(request, 'httprequest', None)
            ip_address = 'Unknown'
            device_info = 'Unknown'
            if httprequest is not None:
                ip_address = (
                    httprequest.headers.get('X-Forwarded-For')
                    or httprequest.remote_addr
                    or 'Unknown'
                )
                device_info = httprequest.headers.get('User-Agent') or 'Unknown'
            change_time = (datetime.utcnow() + IST_OFFSET).strftime(
                '%d %b %Y, %I:%M %p IST')
            _logger.info(
                '%s step=METADATA ip=%s device=%s when=%s',
                lp, ip_address, device_info, change_time)

            # -- Step 3: outgoing-mail-server sanity check --
            # Explains "queued but never delivered" before we even try to send.
            mail_server = request.env['ir.mail_server'].sudo().search(
                [], limit=1)
            if not mail_server:
                _logger.warning(
                    '%s step=NO_MAIL_SERVER no ir.mail_server configured; the '
                    'mail will be queued but CANNOT be delivered until an '
                    'outgoing SMTP server is set up.', lp)
            else:
                _logger.info(
                    '%s step=MAIL_SERVER name=%s host=%s:%s',
                    lp, mail_server.name, mail_server.smtp_host,
                    mail_server.smtp_port)

            ctx = {
                'user_name': user.name,
                'change_time': change_time,
                'ip_address': ip_address,
                'device_info': device_info,
            }
            email_from = request.env['ir.config_parameter'].sudo().get_param(
                'mail.catchall.email', 'no-reply@kuberha.ai')

            # -- Step 4: locate the template --
            template = request.env.ref(
                'api_auth_gateway.email_template_password_changed',
                raise_if_not_found=False,
            )
            _logger.info('%s step=TEMPLATE_LOOKUP found=%s', lp, bool(template))

            # -- Step 5: create + queue the mail (persisted for audit) --
            if template:
                # Force a single recipient via ``email_to`` and explicitly clear
                # ``recipient_ids``. Otherwise the template also resolves the
                # user's partner, and a mail carrying BOTH email_to and a
                # partner recipient is delivered twice (once per channel).
                mail_id = template.sudo().with_context(**ctx).send_mail(
                    user.id,
                    force_send=False,
                    email_values={
                        'email_to': recipient,
                        'recipient_ids': [(5, 0, 0)],
                        'auto_delete': False,
                    },
                )
            else:
                # Fallback if the template record is missing (e.g. not upgraded).
                _logger.warning(
                    '%s step=TEMPLATE_FALLBACK building inline body', lp)
                body_html = '''
                    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                        <h2 style="color: #333;">Password Changed Successfully</h2>
                        <p>Hi %s,</p>
                        <p>This is a confirmation that the password for your Ethara account was changed successfully.</p>
                        <p><strong>Date &amp; Time:</strong> %s<br/>
                           <strong>IP Address:</strong> %s<br/>
                           <strong>Device / Browser:</strong> %s</p>
                        <p style="background-color: #fff4f4; border-left: 4px solid #d9534f; padding: 12px 16px; color: #a94442;">
                            <strong>If you did not make this change, please contact support immediately.</strong>
                        </p>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;"/>
                        <p style="color: #999; font-size: 11px;">Ethara Team</p>
                    </div>
                ''' % (user.name, change_time, ip_address, device_info)
                mail = request.env['mail.mail'].sudo().create({
                    'subject': 'Your Ethara password was changed',
                    'email_from': email_from,
                    'email_to': recipient,
                    'body_html': body_html,
                    'auto_delete': False,
                })
                mail_id = mail.id
            _logger.info(
                '%s step=MAIL_QUEUED mail_id=%s recipient=%s via=%s',
                lp, mail_id, recipient, 'template' if template else 'fallback')

            # -- Step 6: attempt immediate delivery (isolated) --
            mail_rec = request.env['mail.mail'].sudo().browse(mail_id)
            try:
                mail_rec.send(raise_exception=False)
                _logger.info(
                    '%s step=SEND_ATTEMPT mail_id=%s state=%s failure=%s',
                    lp, mail_id, mail_rec.state,
                    (mail_rec.failure_reason or '')[:200])
                if mail_rec.state == 'sent':
                    _logger.info(
                        '%s step=SUCCESS delivered to %s', lp, recipient)
                else:
                    _logger.error(
                        '%s step=NOT_SENT mail_id=%s state=%s reason=%s '
                        '(will be retried by the mail queue cron)',
                        lp, mail_id, mail_rec.state,
                        (mail_rec.failure_reason or 'unknown')[:200])
            except Exception as send_err:
                _logger.error(
                    '%s step=SEND_ERROR mail_id=%s err=%s',
                    lp, mail_id, send_err, exc_info=True)

            _logger.info('%s step=DONE', lp)
        except Exception as e:
            # Never let an email failure break the password change flow.
            _logger.error(
                '%s step=EXCEPTION err=%s', lp, str(e), exc_info=True)

    def _send_password_reset_email(self, user, recipient, reset_link):
        """Send the password-reset link email.

        Returns ``True`` only if the mail was actually accepted by the SMTP
        server, ``False`` otherwise. Mirrors the recipient/logging hardening of
        :meth:`_send_password_changed_email` (prefix ``PWD-RESET-EMAIL``).
        Unlike the change notification, the reset link IS the deliverable, so
        the caller surfaces failures to the user based on this boolean.

        ``recipient`` is forced via ``email_to`` (with partner
        ``recipient_ids`` cleared) so the link reaches the validated address
        even when the user's partner ``email`` field is blank, and is never
        delivered twice.
        """
        lp = 'PWD-RESET-EMAIL [user_id=%s login=%s]' % (user.id, user.login)
        try:
            _logger.info('%s step=START recipient=%s', lp, recipient)

            mail_server = request.env['ir.mail_server'].sudo().search(
                [('active', '=', True)], limit=1)
            if not mail_server:
                _logger.warning(
                    '%s step=NO_MAIL_SERVER no active ir.mail_server; reset '
                    'email CANNOT be delivered until an outgoing SMTP server '
                    'is configured.', lp)
            else:
                _logger.info(
                    '%s step=MAIL_SERVER name=%s host=%s:%s', lp,
                    mail_server.name, mail_server.smtp_host,
                    mail_server.smtp_port)

            email_from = request.env['ir.config_parameter'].sudo().get_param(
                'mail.catchall.email', 'no-reply@kuberha.ai')

            template = request.env.ref(
                'api_auth_gateway.email_template_password_reset',
                raise_if_not_found=False)
            _logger.info('%s step=TEMPLATE_LOOKUP found=%s', lp, bool(template))

            if template:
                mail_id = template.sudo().with_context(
                    reset_link=reset_link,
                    user_name=user.name,
                ).send_mail(
                    user.id,
                    force_send=False,
                    email_values={
                        'email_to': recipient,
                        'recipient_ids': [(5, 0, 0)],
                        'auto_delete': False,
                    },
                )
            else:
                _logger.warning(
                    '%s step=TEMPLATE_FALLBACK building inline body', lp)
                body_html = '''
                    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                        <h2 style="color: #333;">Password Reset Request</h2>
                        <p>Hi %s,</p>
                        <p>We received a request to reset your password. Click the button below to set a new password:</p>
                        <p style="text-align: center; margin: 30px 0;">
                            <a href="%s" style="background-color: #007bff; color: white; padding: 12px 30px;
                               text-decoration: none; border-radius: 5px; font-size: 16px;">
                               Reset Password
                            </a>
                        </p>
                        <p style="color: #666; font-size: 13px;">This link will expire in <strong>1 hour</strong>.</p>
                        <p style="color: #666; font-size: 13px;">If you did not request a password reset, please ignore this email.</p>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                        <p style="color: #999; font-size: 11px;">Ethara Team</p>
                    </div>
                ''' % (user.name, reset_link)
                mail = request.env['mail.mail'].sudo().create({
                    'subject': 'Password Reset Request - Ethara',
                    'email_from': email_from,
                    'email_to': recipient,
                    'body_html': body_html,
                    'auto_delete': False,
                })
                mail_id = mail.id
            _logger.info(
                '%s step=MAIL_QUEUED mail_id=%s recipient=%s via=%s',
                lp, mail_id, recipient, 'template' if template else 'fallback')

            mail_rec = request.env['mail.mail'].sudo().browse(mail_id)
            mail_rec.send(raise_exception=False)
            _logger.info(
                '%s step=SEND_ATTEMPT mail_id=%s state=%s failure=%s',
                lp, mail_id, mail_rec.state,
                (mail_rec.failure_reason or '')[:200])
            if mail_rec.state == 'sent':
                _logger.info('%s step=SUCCESS delivered to %s', lp, recipient)
                return True
            _logger.error(
                '%s step=NOT_SENT mail_id=%s state=%s reason=%s',
                lp, mail_id, mail_rec.state,
                (mail_rec.failure_reason or 'unknown')[:200])
            return False
        except Exception as e:
            _logger.error('%s step=EXCEPTION err=%s', lp, str(e), exc_info=True)
            return False

    @validate_token
    @http.route('/api/v1/get_logged_user_details', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_logged_user_details(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            projects = request.env['project.project'].sudo().search(['|', '|', '|', '|', ('project_lead', 'in', [user_id.employee_id.id]), ('project_aire', 'in', [user_id.employee_id.id]), ('project_swe', 'in', [user_id.employee_id.id]), ('project_qc_reviewer', 'in', [user_id.employee_id.id]), ('project_tasker', 'in', [user_id.employee_id.id])])

            data = {
                'id': safe_get_value(user_id, 'id', 'int'),
                'login': safe_get_value(user_id, 'login', 'str'),
                'name': safe_get_value(user_id, 'name', 'str'),
                'mobile': safe_get_value(user_id, 'phone', 'str'),
                'email': safe_get_value(user_id, 'email', 'str'),
                'employee_id': safe_get_value(user_id, 'employee_id.id', 'int'),
                'employee_name': safe_get_value(user_id, 'employee_id.name', 'str'),
                'department_id': safe_get_value(user_id, 'employee_id.department_id.id', 'int'),
                'department_name': safe_get_value(user_id, 'employee_id.department_id.name', 'str'),
                'education': f"{safe_get_value(user_id, 'employee_id.certificate', 'str')} {safe_get_value(user_id, 'employee_id.study_field', 'str')}",
                'experience_years': safe_get_value(user_id, 'employee_id.experience_years', 'float'),
                'profile_url': safe_get_value(user_id, 'partner_id.profile_url', 'str'),
                'bio_data': safe_get_value(user_id, 'partner_id.bio_data', 'str'),
                'location': safe_get_value(user_id, 'partner_id.street', 'str'),
                'in_app_notification': safe_get_value(user_id, 'employee_id.in_app_notification', 'bool'),
                'email_notification': safe_get_value(user_id, 'employee_id.email_notification', 'bool'),
                'push_notification': safe_get_value(user_id, 'employee_id.push_notification', 'bool'),
                'join_date': safe_get_value(user_id, 'employee_id.joining_date', 'str'),
                'project_count': len(projects),
                'team_size': 0,
                'blocked_resolved': 0,
                'avg_resolution': "",
                'skills': [{"id": skill.skill_id.id, "name": skill.skill_id.name, "is_verified": True} for skill in request.env['hr.employee.skill'].sudo().search([('employee_id', '=', user_id.employee_id.id)])],
                'project_list': [{'id': i.id, 'name': i.name, 'status': i.stage_id.name, "since": str(i.create_date + IST_OFFSET) if i.create_date else ""} for i in projects],
                'notification_line': []
            }
            for notification in user_id.employee_id.notification_line:
                data['notification_line'].append({
                    'name': safe_get_value(notification, 'name.name', 'str'),
                    'in_app_notification': safe_get_value(notification, 'in_app_notification', 'bool'),
                    'email_notification': safe_get_value(notification, 'email_notification', 'bool'),
                    'push_notification': safe_get_value(notification, 'push_notification', 'bool')
                })
            # Org Approval Rate
            data['approval_target'] = 95.0
            data['approval_graph'] = {"Jan": 65.5, "Feb": 67.0, "March": 90.2}
            access_token = request.httprequest.headers.get('access_token')
            if access_token:
                access_token_data = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], order='id DESC', limit=1)
                if access_token_data:
                    data['browser_name'] = safe_get_value(access_token_data, 'browser_name', 'str')
                    data['os_name'] = safe_get_value(access_token_data, 'os_name', 'str')
                    data['location'] = safe_get_value(access_token_data, 'location', 'str')
                    data['theme'] = safe_get_value(access_token_data, 'theme', 'str')
                    data['table_density'] = safe_get_value(access_token_data, 'table_density', 'str')
                    data['collapse_sidebar'] = safe_get_value(access_token_data, 'collapse_sidebar', 'bool')
            return return_Response(message="Success", status=200, data={"record": data})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    # =====================================================
    # Forgot Password / Reset Password
    # =====================================================

    @http.route('/api/v1/forgot_password', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({'email': {'type': 'str', 'required': True}})
    def forgot_password(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            email = jdata.get('email', '').lower().strip()

            if not is_valid_email(email):
                return return_Response(message="Please enter a valid email address.", status=400)

            user = request.env['res.users'].sudo().search([
                ('login', '=', email),
                ('active', '=', True),
            ], limit=1)

            # Prevent email enumeration: always return success
            if not user:
                return return_Response(
                    message="If an account exists with this email, a password reset link has been sent.",
                    status=200
                )

            ResetToken = request.env['api.password_reset_token'].sudo()
            token = ResetToken.generate_reset_token(user.id)

            base_url = request.env['ir.config_parameter'].sudo().get_param(
                'api_auth_gateway.password_reset_url',
                default='http://localhost:3000/reset-password'
            )
            reset_link = '%s?token=%s' % (base_url, token)

            # `email` is the validated login; force it as the recipient so the
            # link is delivered even if the partner email field is blank.
            sent = self._send_password_reset_email(user, email, reset_link)
            if not sent:
                return return_Response(
                    message="Failed to send reset email. Please try again later.",
                    status=400)

            return return_Response(
                message="If an account exists with this email, a password reset link has been sent.",
                status=200
            )
        except Exception as e:
            _logger.error('Forgot password error: %s', str(e))
            return return_Response(message="Something went wrong. Please try again.", status=400, errors=[str(e)])

    @http.route('/api/v1/reset_password', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({
        'token': {'type': 'str', 'required': True},
        'new_password': {'type': 'str', 'required': True},
        'confirm_password': {'type': 'str', 'required': True},
    })
    def reset_password(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            token = jdata.get('token', '').strip()
            new_password = jdata.get('new_password', '').strip()
            confirm_password = jdata.get('confirm_password', '').strip()

            if new_password != confirm_password:
                return return_Response(message="The password and confirm password do not match.", status=400)

            if len(new_password) < 6:
                return return_Response(message="Password must be at least 6 characters long.", status=400)

            ResetToken = request.env['api.password_reset_token'].sudo()
            token_record = ResetToken.validate_reset_token(token)

            if not token_record:
                return return_Response(
                    message="Invalid or expired reset link. Please request a new password reset.",
                    status=400
                )

            user = token_record.user_id

            user.sudo().password = new_password

            token_record.sudo().write({'used': True})

            ResetToken.search([
                ('user_id', '=', user.id),
                ('used', '=', False),
            ]).write({'used': True})
            access_token = request.env['api.access_token'].sudo().search([('user_id', '=', user.id)])
            if access_token:
                for token in access_token:
                    token.sudo().unlink()
            return return_Response(
                message="Password has been reset successfully. Please login with your new password.",
                status=200
            )
        except Exception as e:
            _logger.error('Reset password error: %s', str(e))
            return return_Response(message="Something went wrong. Please try again.", status=400, errors=[str(e)])

