import base64
import json
import logging
from datetime import datetime

from odoo import http, SUPERUSER_ID
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

def _ok(data):
    """Standard success envelope: {message, errors, status_code, data}."""
    return return_Response(message='Success', status=200, data={'data': data})


def _read_params():
    params = dict(request.params or {})
    try:
        raw = request.httprequest.get_data(as_text=True) or ''
        if raw:
            body = json.loads(raw)
            if isinstance(body, dict):
                params.update(body)
    except (json.JSONDecodeError, ValueError):
        pass
    return params


def _employee():
    """The authenticated user's employee record, or empty recordset."""
    return request.env.user.employee_id


def _initials(name):
    parts = [p for p in (name or '').split() if p]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _date_label(value):
    return value.strftime('%d %b %Y') if value else ''


def _section_key(heading):
    return (heading or '').strip().lower().replace(' & ', '_').replace(
        ' ', '_').replace('&', '')


def _leave_policy_sections():
    """Leave-policy prose — fully editable in Odoo, no hardcoded copy. Prefers
    the multi-section 'leave_policy' Wiki Article (heading/body per section);
    falls back to the single Leave Policy document-page description. `key` lets
    the app map each section to its place in the page."""
    env = request.env
    article = env['wiki.article'].sudo().search(
        [('slug', '=', 'leave_policy')], limit=1)
    if article and article.section_ids:
        return [{'key': _section_key(s.heading), 'heading': s.heading or '',
                 'body': s.body or ''}
                for s in article.section_ids.sorted('sequence')]
    page = env['wiki.document.page'].sudo().search(
        [('page_key', '=', 'leave_policy')], limit=1)
    if page and page.description:
        return [{'key': 'overview', 'heading': page.name or 'Leave Policy',
                 'body': page.description}]
    return []


# ── Org-chart helpers (role hierarchy: CTO → TPM → PL → QL/QR → Tasker) ──
# Flattened role-manager links each employee carries, lowest tier first.
_TIER_FIELDS = ['task_forge_ql_id', 'task_forge_pl_id', 'task_forge_tpm_id']


def _org_use_roles(env):
    """True when the ETP role system is installed on hr.employee."""
    fields = env['hr.employee']._fields
    return 'role' in fields and 'task_forge_ql_id' in fields


def _emp_code(emp):
    return 'EMP-%s' % emp.id


def _role_label(emp):
    """Human role label (CTO/TPM/Project Lead/…), falling back to job title."""
    if 'role' in emp._fields and emp.role:
        return dict(emp._fields['role'].selection).get(emp.role, emp.role)
    return emp.job_title or (emp.job_id.name if emp.job_id else '')


def _upward_managers(emp):
    """(field, manager) pairs from the immediate tier up to the CFO."""
    Emp = emp.env['hr.employee'].sudo()
    out = []
    for field in _TIER_FIELDS:
        mgr = emp[field]
        if mgr and mgr.id != emp.id and all(mgr.id != m.id for _f, m in out):
            out.append((field, mgr))
    # Roll up to the CTO for anyone in the role tree, or a TPM/DM under it.
    if out or emp.role in ('tpm', 'dm'):
        cto = Emp.search([('role', '=', 'cto')], limit=1)
        if cto and cto.id != emp.id and all(cto.id != m.id for _f, m in out):
            out.append(('role', cto))
    # Roll up to the CFO above the CTO (top of the hierarchy).
    if out or emp.role == 'cto':
        cfo = Emp.search([('role', '=', 'cfo')], limit=1)
        if cfo and cfo.id != emp.id and all(cfo.id != m.id for _f, m in out):
            out.append(('role', cfo))
    return out


def _direct_reports(emp):
    """Employees whose immediate role-manager is `emp` (one tier down)."""
    Emp = emp.env['hr.employee'].sudo()
    reports = Emp.search([('task_forge_ql_id', '=', emp.id)])
    reports |= Emp.search([('task_forge_pl_id', '=', emp.id),
                           ('task_forge_ql_id', '=', False)])
    reports |= Emp.search([('task_forge_tpm_id', '=', emp.id),
                           ('task_forge_ql_id', '=', False),
                           ('task_forge_pl_id', '=', False)])
    if emp.role == 'cto':
        reports |= Emp.search([('role', 'in', ('tpm', 'dm'))])
    if emp.role == 'cfo':
        reports |= Emp.search([('role', '=', 'cto')])
    return (reports - emp).sorted('name')


class WikiController(http.Controller):

    # ── Company Wiki dashboard ────────────────────────────
    @http.route('/api/v1/wiki/dashboard', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def dashboard(self, **kwargs):
        env = request.env
        categories = env['wiki.category'].sudo().search([])
        updates = env['wiki.update'].sudo().search([], limit=8)
        return _ok({
            'categories': [{
                'id': c.id,
                'title': c.name,
                'description': c.description or '',
                'icon': c.icon or '',
                'route_key': c.route_key or '',
            } for c in categories],
            'recent_updates': [{
                'id': u.id,
                'title': u.name,
                'meta': '%s · %s' % (u.owner or '', _date_label(u.date)),
            } for u in updates],
        })

    # ── FAQs ──────────────────────────────────────────────
    @http.route('/api/v1/wiki/faqs', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def faqs(self, **kwargs):
        faqs = request.env['wiki.faq'].sudo().search([])
        groups = []
        index = {}
        for faq in faqs:
            label = faq.group or 'General'
            if label not in index:
                index[label] = {'label': label, 'items': []}
                groups.append(index[label])
            index[label]['items'].append({
                'id': faq.id,
                'question': faq.name,
                'answer': faq.answer,
            })
        # Address behind the "Didn't find your answer? Contact HR" action.
        # Admin-configurable; falls back to the company email.
        env = request.env
        hr_email = (env['ir.config_parameter'].sudo().get_param(
            'wiki.hr_contact_email') or env.company.email or 'hr@ethara.ai')
        return _ok({'groups': groups, 'hr_contact_email': hr_email})

    # ── Holidays ──────────────────────────────────────────
    @http.route('/api/v1/wiki/holidays', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def holidays(self, **kwargs):
        params = _read_params()
        year = params.get('year')
        year = int(year) if (year and str(year).isdigit()) else None
        # Single source of truth: Time Off → Configuration → Public Holidays
        # (resource.calendar.leaves with no resource). The Wiki keeps no
        # holiday records of its own.
        domain = [('resource_id', '=', False)]
        if year:
            domain += [('date_from', '>=', '%s-01-01 00:00:00' % year),
                       ('date_from', '<=', '%s-12-31 23:59:59' % year)]
        all_records = request.env['resource.calendar.leaves'].sudo().search(
            domain, order='date_from')
        today = request.env.cr.now().date() if hasattr(
            request.env.cr, 'now') else datetime.now().date()

        def _hdate(h):
            return h.date_from.date() if h.date_from else None

        # Public holidays may exist once per working calendar; collapse them
        # to one entry per (name, date) so the app never shows duplicates.
        records = request.env['resource.calendar.leaves']
        seen = set()
        for h in all_records:
            key = (h.name, _hdate(h))
            if key in seen:
                continue
            seen.add(key)
            records |= h

        def _htype(h):
            return h.holiday_classification or 'gazetted'

        upcoming = records.filtered(
            lambda h: _hdate(h) and _hdate(h) >= today)
        nxt = upcoming[:1]
        return _ok({
            'year': year,
            'total': len(records),
            'gazetted': len(records.filtered(
                lambda h: _htype(h) == 'gazetted')),
            'restricted': len(records.filtered(
                lambda h: _htype(h) == 'restricted')),
            'next': {
                'date_label': _hdate(nxt).strftime('%d %b') if nxt else '',
                'name': nxt.name if nxt else '',
            } if nxt else None,
            'holidays': [{
                'id': h.id,
                'name': h.name,
                'date_label': _date_label(_hdate(h)),
                'type': _htype(h),
            } for h in records],
        })

    # ── Training ──────────────────────────────────────────
    @http.route('/api/v1/wiki/training', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def training(self, **kwargs):
        env = request.env
        groups = env['wiki.training.group'].sudo().search([])
        employee = _employee()
        # Map doc_id -> this employee's progress record.
        progress_by_doc = {}
        if employee:
            for p in env['wiki.training.progress'].sudo().search(
                    [('employee_id', '=', employee.id)]):
                progress_by_doc[p.doc_id.id] = p

        standing = {'required_pending': 0, 'in_progress': 0,
                    'completed': 0, 'total': 0}

        def serialize(doc):
            prog = progress_by_doc.get(doc.id)
            state = prog.state if prog else 'not_started'
            percent = prog.progress if prog else 0
            if state == 'completed':
                percent = 100
            standing['total'] += 1
            if state == 'completed':
                standing['completed'] += 1
            elif state == 'in_progress':
                standing['in_progress'] += 1
            elif doc.required:
                standing['required_pending'] += 1
            return {
                'id': doc.id,
                'title': doc.name,
                'meta': doc.meta or '',
                'description': doc.description or '',
                'body': doc.body or '',
                'required': doc.required,
                'pages': doc.pages or 0,
                'updated': _date_label(doc.write_date.date())
                if doc.write_date else '',
                'has_document': bool(doc.document),
                'status': state,
                'progress': percent,
            }

        payload = {
            'groups': [{
                'label': g.name,
                'docs': [serialize(d) for d in g.doc_ids],
            } for g in groups],
        }
        payload['standing'] = standing
        payload['total_documents'] = standing['total']
        return _ok(payload)

    # ── Training: update the current user's progress on a document ──
    @http.route('/api/v1/wiki/training/progress', methods=['POST'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def training_progress(self, **kwargs):
        params = _read_params()
        env = request.env
        employee = _employee()
        if not employee:
            return return_Response(message='Employee profile not found.',
                                   status=404)
        raw_id = params.get('doc_id')
        doc = (env['wiki.training.doc'].sudo().browse(int(raw_id)).exists()
               if raw_id and str(raw_id).isdigit()
               else env['wiki.training.doc'])
        if not doc:
            return return_Response(message='Document not found.', status=404)
        state = params.get('state')
        if state not in ('not_started', 'in_progress', 'completed'):
            state = 'in_progress'
        percent = params.get('progress')
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            percent = None
        if state == 'completed':
            percent = 100
        elif percent is None:
            percent = 0
        percent = max(0, min(100, percent))

        Progress = env['wiki.training.progress'].sudo()
        record = Progress.search(
            [('employee_id', '=', employee.id), ('doc_id', '=', doc.id)],
            limit=1)
        vals = {'state': state, 'progress': percent}
        if record:
            record.write(vals)
        else:
            record = Progress.create(dict(
                vals, employee_id=employee.id, doc_id=doc.id))
        return _ok({
            'doc_id': doc.id,
            'status': record.state,
            'progress': record.progress,
        })

    # ── Training: download a document's file ──
    @http.route('/api/v1/wiki/training/document', methods=['GET'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def training_document(self, **kwargs):
        params = _read_params()
        raw_id = params.get('doc_id')
        doc = (request.env['wiki.training.doc'].sudo().browse(
            int(raw_id)).exists()
            if raw_id and str(raw_id).isdigit()
            else request.env['wiki.training.doc'])
        if not doc or not doc.document:
            return return_Response(message='Document not found.', status=404)
        content = base64.b64decode(doc.document)
        filename = doc.document_filename or ('%s.pdf' % (doc.name or 'document'))
        return request.make_response(content, headers=[
            ('Content-Type', 'application/octet-stream'),
            ('Content-Disposition', 'attachment; filename="%s"' % filename),
        ])

    # ── Grievances ────────────────────────────────────────
    @http.route('/api/v1/wiki/grievances', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def grievances_list(self, **kwargs):
        employee = _employee()
        Grievance = request.env['wiki.grievance'].sudo()
        items = Grievance.search(
            [('employee_id', '=', employee.id), ('is_anonymous', '=', False)]
        ) if employee else Grievance.browse()
        labels = dict(request.env['wiki.grievance'].CATEGORIES)
        return _ok({
            'categories': [{'value': v, 'label': lbl}
                           for v, lbl in request.env['wiki.grievance'].CATEGORIES],
            'items': [{
                'id': g.id,
                'reference': g.reference,
                'category': labels.get(g.category, g.category),
                'status': dict(g._fields['state'].selection).get(g.state),
                'created': _date_label(g.create_date.date()
                                       if g.create_date else False),
            } for g in items],
        })

    @http.route('/api/v1/wiki/grievances', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def grievances_create(self, **kwargs):
        params = _read_params()
        category = params.get('category')
        description = (params.get('description') or '').strip()
        if not category or not description:
            return return_Response(
                message="'category' and 'description' are required.",
                status=400)
        _anon = params.get('is_anonymous')
        is_anonymous = _anon is True or str(_anon).strip().lower() in (
            '1', 'true', 'yes')
        submit = params.get('state') != 'draft'
        employee = _employee()
        # Anonymous reports MUST NOT be traceable: create under SUPERUSER so
        # create_uid/write_uid don't fingerprint the reporter. Named reports
        # keep the real uid for accountability. Plain .sudo() would leave the
        # caller's uid on the audit columns, defeating anonymity.
        grievance_env = (
            request.env['wiki.grievance'].with_user(SUPERUSER_ID)
            if is_anonymous
            else request.env['wiki.grievance'].sudo()
        )
        grievance = grievance_env.create({
            'category': category,
            'description': description,
            'is_anonymous': is_anonymous,
            'employee_id': False if is_anonymous else (employee.id or False),
            'state': 'submitted' if submit else 'draft',
        })
        # Persist any uploaded evidence as ir.attachment linked to the
        # grievance. getlist() handles 0, 1 or many files transparently.
        attachments = []
        for upload in request.httprequest.files.getlist('evidence'):
            if not upload or not upload.filename:
                continue
            content = upload.read()
            if not content:
                continue
            # Evidence attachments inherit the same anonymity requirement:
            # create_uid on ir.attachment would otherwise deanonymize the
            # reporter, so route anonymous uploads through SUPERUSER too.
            attach_env = (
                request.env['ir.attachment'].with_user(SUPERUSER_ID)
                if is_anonymous
                else request.env['ir.attachment'].sudo()
            )
            attach_env.create({
                'name': upload.filename,
                'datas': base64.b64encode(content),
                'mimetype': upload.content_type or 'application/octet-stream',
                'res_model': 'wiki.grievance',
                'res_id': grievance.id,
            })
            attachments.append(upload.filename)
        return _ok({
            'id': grievance.id,
            'reference': grievance.reference,
            'status': dict(grievance._fields['state'].selection).get(
                grievance.state),
            'attachments': attachments,
        })

    # ── Leave summary + apply ─────────────────────────────
    @http.route('/api/v1/wiki/leave/summary', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def leave_summary(self, **kwargs):
        env = request.env
        employee = _employee()
        if not employee:
            return return_Response(message='Employee profile not found.',
                                   status=404)
        buckets = env['hr.leave.bucket'].sudo().search(
            [('employee_id', '=', employee.id)])
        # Pending (submitted, not yet finally approved) days per leave type —
        # the bucket view only counts approved leave, so derive this from
        # hr.leave directly. Single source of truth: Time Off.
        pending_by_type = {}
        for lv in env['hr.leave'].sudo().search([
                ('employee_id', '=', employee.id),
                ('state', 'in', ('confirm', 'validate1'))]):
            tid = lv.holiday_status_id.id
            pending_by_type[tid] = pending_by_type.get(tid, 0) + lv.number_of_days
        balances = [{
            'id': b.leave_type_id.id,
            'code': b.code or '',
            'label': b.leave_type_id.name,
            'value': int(round(b.entitlement)),
            'remaining': int(round(b.remaining)),
            # My-balances table: Opening / Availed / Pending / Balance.
            'opening': int(round(b.allocated)),
            'availed': int(round(b.taken)),
            'pending': int(round(pending_by_type.get(b.leave_type_id.id, 0))),
            'balance': int(round(b.remaining)),
            'unit': 'accrued' if (b.code or '') == 'el' else 'days / year',
        } for b in buckets]
        leaves = env['hr.leave'].sudo().search(
            [('employee_id', '=', employee.id)], limit=20)
        requests = [{
            'reference': 'LEV-%04d' % lv.id,
            'type': lv.holiday_status_id.name,
            'dates': _leave_dates(lv),
            'days': int(round(lv.number_of_days)),
            'status': dict(lv._fields['state'].selection).get(lv.state, lv.state),
        } for lv in leaves]
        return _ok({
            'balances': balances,
            'requests': requests,
            'policy': _leave_policy_sections(),
        })

    @http.route('/api/v1/wiki/leave/apply', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def leave_apply(self, **kwargs):
        params = _read_params()
        employee = _employee()
        if not employee:
            return return_Response(message='Employee profile not found.',
                                   status=404)
        type_id = params.get('leave_type_id')
        date_from = params.get('date_from')
        date_to = params.get('date_to') or date_from
        if not type_id or not date_from:
            return return_Response(
                message="'leave_type_id' and 'date_from' are required.",
                status=400)
        try:
            leave = request.env['hr.leave'].sudo().create({
                'employee_id': employee.id,
                'holiday_status_id': int(type_id),
                'request_date_from': date_from,
                'request_date_to': date_to,
                'name': params.get('reason') or 'Leave request',
            })
        except Exception as exc:  # noqa: BLE001 - surface ORM validation cleanly
            _logger.warning('Leave apply failed: %s', exc)
            return return_Response(message=str(exc), status=400)
        return _ok({
            'reference': 'LEV-%04d' % leave.id,
            'status': dict(leave._fields['state'].selection).get(leave.state),
        })

    # ── Org chart ─────────────────────────────────────────
    @http.route('/api/v1/wiki/org_chart', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def org_chart(self, **kwargs):
        employee = _employee()
        if not employee:
            return return_Response(message='Employee profile not found.',
                                   status=404)

        Emp = request.env['hr.employee'].sudo()
        # Build the chart from the ETP role hierarchy (CTO → TPM → PL → QL/QR
        # → Tasker) when that system is installed; otherwise fall back to the
        # plain reporting line (parent_id) + job title.
        use_roles = _org_use_roles(request.env)

        if use_roles:
            managers = _upward_managers(employee)
            # chain top-down: highest manager first, employee last.
            chain_records = [m for _f, m in reversed(managers)] + [employee]
            if managers:
                imm_field, manager = managers[0]
                if imm_field in _TIER_FIELDS:
                    # Peers share the same immediate manager *at the same tier*.
                    # Lower-tier links must be empty so the whole subtree below
                    # a manager isn't pulled in (the flattened links are shared).
                    lower = _TIER_FIELDS[:_TIER_FIELDS.index(imm_field)]
                    domain = [(imm_field, '=', manager.id)]
                    domain += [(lf, '=', False) for lf in lower]
                    team_records = Emp.search(domain)
                else:
                    # Immediate manager is the CTO (employee is a TPM/DM).
                    team_records = (Emp.search([('role', '=', employee.role)])
                                    if employee.role else employee)
            else:
                manager = Emp.browse()
                team_records = employee
        else:
            chain_records = []
            node = employee
            seen = set()
            while node and node.id not in seen:
                seen.add(node.id)
                chain_records.append(node)
                node = node.parent_id
            chain_records.reverse()
            manager = employee.parent_id
            team_records = manager.child_ids if manager else employee
        if not team_records:
            team_records = employee

        def serialize(emp):
            return {
                'id': emp.id,
                'initials': _initials(emp.name),
                'name': emp.name,
                'role': _role_label(emp),
                'emp_code': _emp_code(emp),
                'is_you': emp.id == employee.id,
            }

        return _ok({
            'reports_to': manager.name if manager else '',
            'chain': [serialize(e) for e in chain_records],
            'team': [serialize(e) for e in team_records],
        })

    # ── Org chart: single person detail (for the tap-to-open drawer) ──
    @http.route('/api/v1/wiki/org_chart/person', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def org_chart_person(self, **kwargs):
        params = _read_params()
        raw_id = params.get('employee_id') or params.get('id')
        Emp = request.env['hr.employee'].sudo()
        person = (Emp.browse(int(raw_id)).exists()
                  if raw_id and str(raw_id).isdigit() else Emp.browse())
        if not person:
            return return_Response(message='Employee not found.', status=404)

        me = _employee()
        use_roles = _org_use_roles(request.env)
        if use_roles:
            managers = _upward_managers(person)
            manager = managers[0][1] if managers else Emp.browse()
            reports = _direct_reports(person)
        else:
            manager = person.parent_id
            reports = person.child_ids

        def serialize(emp):
            return {
                'id': emp.id,
                'initials': _initials(emp.name),
                'name': emp.name,
                'role': _role_label(emp),
                'emp_code': _emp_code(emp),
                'is_you': bool(me) and emp.id == me.id,
            }

        data = serialize(person)
        data['reports_to'] = ({'id': manager.id, 'name': manager.name}
                              if manager else None)
        data['direct_reports'] = [serialize(r) for r in reports]
        return _ok(data)

    @http.route('/api/v1/wiki/articles', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def articles(self):
        # The 'leave_policy' article backs the Leave Policy page's prose, not a
        # Foundation card — keep it out of the Foundation hub.
        records = request.env['wiki.article'].sudo().search(
            [('slug', '!=', 'leave_policy')])
        return _ok({
            'articles': [{
                'slug': a.slug or '',
                'title': a.name or '',
                'description': a.description or '',
                'icon': a.icon or '',
                'route_key': a.route_key or '',
                'meta': a.meta or '',
                'sections': [{
                    'heading': s.heading or '',
                    'body': s.body or '',
                } for s in a.section_ids],
            } for a in records],
        })

    @http.route('/api/v1/wiki/process_flows', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def process_flows(self):
        records = request.env['wiki.process.flow'].sudo().search([])
        return _ok({
            'flows': [{
                'title': f.name or '',
                'meta': f.meta or '',
                'stages': [{
                    'label': s.name or '',
                    'kind': s.kind or 'step',
                } for s in f.stage_ids],
            } for f in records],
        })


def _leave_dates(leave):
    start = leave.request_date_from
    end = leave.request_date_to
    if start and end and start != end:
        return '%s – %s' % (start.strftime('%d %b'), end.strftime('%d %b %Y'))
    if start:
        return start.strftime('%d %b %Y')
    return ''
