import logging
from datetime import datetime, date, timedelta

from odoo import http, fields
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request,
    generate_s3_link, safe_get_value,
)

_logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

CATEGORY_LABELS = {
    'cab_taxi': 'Cab/Taxi',
    'meals': 'Meals',
    'travel': 'Travel',
    'other': 'Other',
}

STATE_LABELS = {
    'draft': 'Draft',
    'submitted': 'Pending',
    'approved': 'Approved',
    'rejected': 'Rejected',
    'reimbursed': 'Reimbursed',
}


def _ist(dt, fmt='%Y-%m-%d %H:%M:%S'):
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        return (dt + IST_OFFSET).strftime(fmt)
    if isinstance(dt, date):
        return dt.isoformat()
    return ''


HR_ROLE_NAMES = {'hr', 'hr admin'}


def _is_hr(user):
    role_name = (user.user_role.name or '').strip().lower() if user.user_role else ''
    return role_name in HR_ROLE_NAMES \
        or user.has_group('etp_user_roles.group_hr_admin') \
        or user.has_group('etp_reimbursement.group_reimbursement_manager') \
        or user.has_group('base.group_system')


def _serialize_line(line):
    return {
        'id': line.id,
        'date': safe_get_value(line, 'date', 'date'),
        'category': line.category or '',
        'category_label': CATEGORY_LABELS.get(line.category, ''),
        'description': line.description or '',
        'amount': float(line.amount or 0),
        'receipt_url': line.receipt_url or '',
    }


def _serialize_claim(rec, include_lines=True):
    data = {
        'id': rec.id,
        'name': rec.name or '',
        'employee_id': rec.employee_id.id if rec.employee_id else 0,
        'employee_name': rec.employee_id.name if rec.employee_id else '',
        'employee_code': safe_get_value(rec, 'employee_id.employee_id_seq', 'str') or safe_get_value(rec, 'employee_id.identification_id', 'str') or '',
        'department': rec.department_id.name if rec.department_id else '',
        'request_date': safe_get_value(rec, 'request_date', 'date'),
        'total_amount': float(rec.total_amount or 0),
        'currency_symbol': rec.currency_id.symbol or '',
        'currency_code': rec.currency_id.name or '',
        'line_count': len(rec.line_ids),
        'state': rec.state or '',
        'state_label': STATE_LABELS.get(rec.state, ''),
        'submitted_at': _ist(rec.submitted_date),
        'approved_at': _ist(rec.approved_date),
        'rejected_at': _ist(rec.rejected_date),
        'reimbursed_at': _ist(rec.reimbursed_date),
        'approved_by': rec.approved_by.name if rec.approved_by else '',
        'rejected_by': rec.rejected_by.name if rec.rejected_by else '',
        'rejection_reason': rec.rejection_reason or '',
        'hr_comment': rec.hr_comment or '',
    }
    if include_lines:
        data['lines'] = [_serialize_line(l) for l in rec.line_ids]
    return data


def _apply_filters(domain, kwargs):
    """Mutates and returns domain based on common filter kwargs."""
    state = kwargs.get('status') or kwargs.get('state')
    if state and state.lower() != 'all':
        if state.lower() == 'pending':
            domain.append(('state', '=', 'submitted'))
        else:
            domain.append(('state', '=', state.lower()))

    if kwargs.get('employee_id'):
        try:
            domain.append(('employee_id', '=', int(kwargs.get('employee_id'))))
        except (TypeError, ValueError):
            pass

    if kwargs.get('category'):
        domain.append(('line_ids.category', '=', kwargs.get('category')))

    if kwargs.get('search'):
        term = kwargs.get('search')
        domain += ['|', '|',
                   ('name', 'ilike', term),
                   ('employee_id.name', 'ilike', term),
                   ('line_ids.description', 'ilike', term)]

    date_from = kwargs.get('date_from') or kwargs.get('start_date')
    date_to = kwargs.get('date_to') or kwargs.get('end_date')
    if date_from:
        domain.append(('request_date', '>=', date_from))
    if date_to:
        domain.append(('request_date', '<=', date_to))
    return domain


class ReimbursementController(http.Controller):

    # ── 1. Create a reimbursement claim ──────────────────────────────────────
    @http.route('/api/v1/reimbursement/create', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'request_date': {'type': 'date', 'required': False},
        'employee_id': {'type': 'int', 'required': False},
        'lines': {
            'type': 'list', 'required': True,
            'items': {
                'date': {'type': 'date', 'required': True},
                'category': {'type': 'string', 'required': True},
                'description': {'type': 'string', 'required': True},
                'amount': {'required': True},
                'receipt': {'type': 'string', 'required': False},
                'receipt_filename': {'type': 'string', 'required': False},
            },
        },
    })
    def create_reimbursement(self, **params):
        try:
            jdata = params.get('jdata') or {}
            user = request.env.user

            # HR can raise on behalf of any employee; non-HR always raises for self.
            target_employee_id = jdata.get('employee_id')
            if target_employee_id and _is_hr(user):
                employee = request.env['hr.employee'].sudo().browse(int(target_employee_id))
                if not employee.exists():
                    return return_Response(
                        message='Employee with id %s not found.' % target_employee_id,
                        status=400,
                    )
            else:
                employee = user.employee_id
                if not employee:
                    return return_Response(message='No employee profile linked to this user.', status=400)

            req_date = jdata.get('request_date') or fields.Date.context_today(request.env.user).isoformat()

            # Enforce one-per-day rule before creating (HR users are exempt)
            if not _is_hr(user):
                existing = request.env['etp.reimbursement'].sudo().search_count([
                    ('employee_id', '=', employee.id),
                    ('request_date', '=', req_date),
                    ('state', '!=', 'draft'),
                ])
                if existing:
                    return return_Response(
                        message='You have already raised a reimbursement request today. Only one request per day is allowed.',
                        status=400,
                    )

            lines = jdata.get('lines') or []
            if not lines:
                return return_Response(message='Add at least one ride/expense line.', status=400)

            valid_categories = set(CATEGORY_LABELS.keys())
            line_vals = []
            for idx, ln in enumerate(lines, 1):
                category = (ln.get('category') or '').strip()
                if category not in valid_categories:
                    return return_Response(
                        message='Invalid category "%s" on line %s. Allowed: %s' % (category, idx, ', '.join(valid_categories)),
                        status=400,
                    )
                amount = float(ln.get('amount') or 0)
                if amount <= 0:
                    return return_Response(message='Line %s: amount must be greater than zero.' % idx, status=400)

                receipt_url = ''
                receipt_b64 = ln.get('receipt')
                if receipt_b64:
                    try:
                        receipt_url = generate_s3_link(
                            receipt_b64,
                            prefix='reimbursement/receipts',
                            uid=user.id,
                            filename=ln.get('receipt_filename') or None,
                        ) or ''
                    except Exception as e:
                        _logger.error('Receipt upload failed for line %s: %s', idx, e)
                        return return_Response(message='Failed to upload receipt for line %s.' % idx, status=400)

                line_vals.append((0, 0, {
                    'date': ln.get('date'),
                    'category': category,
                    'description': ln.get('description') or '',
                    'amount': amount,
                    'receipt_url': receipt_url,
                }))

            Reimbursement = request.env['etp.reimbursement'].sudo()
            rec = Reimbursement.create({
                'employee_id': employee.id,
                'request_date': req_date,
                'line_ids': line_vals,
            })
            rec.action_submit()
            return return_Response(
                message='Reimbursement raised successfully.',
                status=200,
                data={'record': _serialize_claim(rec)},
            )
        except Exception as e:
            _logger.exception('Reimbursement create failed: %s', e)
            return return_Response(message=str(e) or 'Failed to create reimbursement.', status=400)

    # ── 2. List with filter + pagination ─────────────────────────────────────
    @http.route('/api/v1/reimbursement/list', methods=['GET', 'POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_reimbursement(self, **kwargs):
        try:
            user = request.env.user
            Reimbursement = request.env['etp.reimbursement'].sudo()

            domain = []
            if not _is_hr(user):
                if not user.employee_id:
                    return return_Response(message='No employee profile linked.', status=400)
                domain.append(('employee_id', '=', user.employee_id.id))
            domain = _apply_filters(domain, kwargs)

            # Pagination
            try:
                page = max(int(kwargs.get('page') or 1), 1)
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = int(kwargs.get('page_size') or kwargs.get('limit') or 20)
            except (TypeError, ValueError):
                page_size = 20
            page_size = max(1, min(page_size, 200))
            offset = (page - 1) * page_size

            order = kwargs.get('order') or 'submitted_date desc, id desc'

            total = Reimbursement.search_count(domain)
            records = Reimbursement.search(domain, offset=offset, limit=page_size, order=order)

            # Summary tiles (HR sees all, employee sees own)
            tile_domain = [] if _is_hr(user) else [('employee_id', '=', user.employee_id.id)]
            total_claims = Reimbursement.search_count(tile_domain)
            pending_recs = Reimbursement.search(tile_domain + [('state', '=', 'submitted')])
            pending_amount = sum(pending_recs.mapped('total_amount'))

            today = fields.Date.context_today(request.env.user)
            month_start = today.replace(day=1)
            approved_recs = Reimbursement.search(
                tile_domain + [('state', '=', 'approved'), ('approved_date', '>=', month_start.isoformat())])
            reimbursed_recs = Reimbursement.search(
                tile_domain + [('state', '=', 'reimbursed'), ('reimbursed_date', '>=', month_start.isoformat())])

            return return_Response(
                message='Success',
                status=200,
                data={
                    'records': [_serialize_claim(r, include_lines=False) for r in records],
                    'pagination': {
                        'page': page,
                        'page_size': page_size,
                        'total': total,
                        'total_pages': (total + page_size - 1) // page_size,
                    },
                    'summary': {
                        'total_claims': total_claims,
                        'pending_amount': float(pending_amount),
                        'approved_this_month': float(sum(approved_recs.mapped('total_amount'))),
                        'reimbursed_this_month': float(sum(reimbursed_recs.mapped('total_amount'))),
                        'pending_count': len(pending_recs),
                    },
                    'is_hr': _is_hr(user),
                },
            )
        except Exception as e:
            _logger.exception('Reimbursement list failed: %s', e)
            return return_Response(message=str(e) or 'Failed to fetch list.', status=400)

    # ── 3. Detail ────────────────────────────────────────────────────────────
    @http.route('/api/v1/reimbursement/detail', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def detail_reimbursement(self, **kwargs):
        try:
            claim_id = kwargs.get('id') or kwargs.get('claim_id')
            if not claim_id:
                return return_Response(message='id is required', status=400)
            rec = request.env['etp.reimbursement'].sudo().browse(int(claim_id))
            if not rec.exists():
                return return_Response(message='Claim not found', status=404)
            user = request.env.user
            if not _is_hr(user) and rec.user_id.id != user.id:
                return return_Response(message='You are not allowed to view this claim.', status=403)
            return return_Response(message='Success', status=200, data={'record': _serialize_claim(rec)})
        except Exception as e:
            _logger.exception('Reimbursement detail failed: %s', e)
            return return_Response(message=str(e), status=400)

    # ── 4. Approve ───────────────────────────────────────────────────────────
    @http.route('/api/v1/reimbursement/approve', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'id': {'type': 'int', 'required': True},
        'comment': {'type': 'string', 'required': False},
    })
    def approve_reimbursement(self, **params):
        try:
            user = request.env.user
            if not _is_hr(user):
                return return_Response(message='Only HR can approve claims.', status=403)
            jdata = params.get('jdata') or {}
            rec = request.env['etp.reimbursement'].sudo().browse(int(jdata.get('id')))
            if not rec.exists():
                return return_Response(message='Claim not found', status=404)
            rec.with_user(user.id).sudo().action_approve(comment=jdata.get('comment'))
            return return_Response(message='Claim approved.', status=200, data={'record': _serialize_claim(rec)})
        except Exception as e:
            _logger.exception('Reimbursement approve failed: %s', e)
            return return_Response(message=str(e), status=400)

    # ── 5. Reject ────────────────────────────────────────────────────────────
    @http.route('/api/v1/reimbursement/reject', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'id': {'type': 'int', 'required': True},
        'reason': {'type': 'string', 'required': True},
    })
    def reject_reimbursement(self, **params):
        try:
            user = request.env.user
            if not _is_hr(user):
                return return_Response(message='Only HR can reject claims.', status=403)
            jdata = params.get('jdata') or {}
            rec = request.env['etp.reimbursement'].sudo().browse(int(jdata.get('id')))
            if not rec.exists():
                return return_Response(message='Claim not found', status=404)
            rec.with_user(user.id).sudo().action_reject(reason=jdata.get('reason'))
            return return_Response(message='Claim rejected.', status=200, data={'record': _serialize_claim(rec)})
        except Exception as e:
            _logger.exception('Reimbursement reject failed: %s', e)
            return return_Response(message=str(e), status=400)

    # ── 6. Mark Reimbursed (HR) ──────────────────────────────────────────────
    @http.route('/api/v1/reimbursement/mark_reimbursed', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({'id': {'type': 'int', 'required': True}})
    def mark_reimbursed(self, **params):
        try:
            user = request.env.user
            if not _is_hr(user):
                return return_Response(message='Only HR can mark a claim as reimbursed.', status=403)
            jdata = params.get('jdata') or {}
            rec = request.env['etp.reimbursement'].sudo().browse(int(jdata.get('id')))
            if not rec.exists():
                return return_Response(message='Claim not found', status=404)
            rec.with_user(user.id).sudo().action_mark_reimbursed()
            return return_Response(message='Claim marked as reimbursed.', status=200, data={'record': _serialize_claim(rec)})
        except Exception as e:
            _logger.exception('Reimbursement mark_reimbursed failed: %s', e)
            return return_Response(message=str(e), status=400)

    # ── 7. Delete own draft / pending claim ──────────────────────────────────
    @http.route('/api/v1/reimbursement/delete', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({'id': {'type': 'int', 'required': True}})
    def delete_reimbursement(self, **params):
        try:
            jdata = params.get('jdata') or {}
            user = request.env.user
            rec = request.env['etp.reimbursement'].sudo().browse(int(jdata.get('id')))
            if not rec.exists():
                return return_Response(message='Claim not found', status=404)
            if rec.user_id.id != user.id and not _is_hr(user):
                return return_Response(message='Not allowed to delete this claim.', status=403)
            if rec.state not in ('draft', 'submitted'):
                return return_Response(message='Only draft / pending claims can be deleted.', status=400)
            rec.unlink()
            return return_Response(message='Claim deleted.', status=200)
        except Exception as e:
            _logger.exception('Reimbursement delete failed: %s', e)
            return return_Response(message=str(e), status=400)
