import io
import csv
import base64
import logging
from datetime import timedelta

from odoo import http, fields
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, generate_s3_link,
)
from .main import _apply_filters, _is_hr, CATEGORY_LABELS, STATE_LABELS

_logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

# ── Brand palette (kept consistent with task_forge_core exports) ─────────────
PRIMARY = '#1B2A4A'
ACCENT = '#2E86DE'
LIGHT_BG = '#F0F4F8'
WHITE = '#FFFFFF'
BORDER = '#CBD5E1'
SUCCESS = '#27AE60'
DANGER = '#E74C3C'
WARNING = '#F39C12'
TEXT_DARK = '#1E293B'
TEXT_LIGHT = '#94A3B8'

EXPORT_HEADERS = [
    '#', 'Claim Ref', 'Employee', 'Department', 'Request Date',
    'Category', 'Description', '# Rides', 'Total Amount', 'Currency',
    'Status', 'Submitted On', 'Approved/Rejected On',
    'Reviewed By', 'Rejection Reason',
]

LINE_HEADERS = [
    '#', 'Claim Ref', 'Employee', 'Line Date', 'Category',
    'Description', 'Amount', 'Currency', 'Receipt URL',
]


def _ist_date(d):
    if not d:
        return ''
    if hasattr(d, 'hour'):
        return (d + IST_OFFSET).strftime('%Y-%m-%d %H:%M')
    return str(d)


def _summarize_lines(rec):
    """Collapse line-level fields into single strings for the claim row."""
    if not rec.line_ids:
        return '', '', 0
    categories = []
    for line in rec.line_ids:
        label = CATEGORY_LABELS.get(line.category, line.category or '')
        if label and label not in categories:
            categories.append(label)
    category = ', '.join(categories)
    first_desc = (rec.line_ids[0].description or '').strip()
    extra = len(rec.line_ids) - 1
    description = first_desc + (f' (+{extra} more)' if extra else '')
    return category, description, len(rec.line_ids)


def _claim_rows(records):
    """One row per claim, with line fields summarized."""
    rows = []
    for idx, rec in enumerate(records, 1):
        category, description, line_count = _summarize_lines(rec)
        rows.append({
            'idx': idx,
            'name': rec.name or '',
            'employee': rec.employee_id.name or '',
            'department': rec.department_id.name or '',
            'request_date': rec.request_date.isoformat() if rec.request_date else '',
            'category': category,
            'description': description,
            'line_count': line_count,
            'amount': float(rec.total_amount or 0),
            'currency': rec.currency_id.name or '',
            'state': STATE_LABELS.get(rec.state, rec.state or ''),
            'submitted': _ist_date(rec.submitted_date),
            'decided': _ist_date(rec.approved_date or rec.rejected_date or rec.reimbursed_date),
            'reviewed_by': (rec.approved_by.name if rec.approved_by else '')
                           or (rec.rejected_by.name if rec.rejected_by else '')
                           or (rec.reimbursed_by.name if rec.reimbursed_by else ''),
            'reason': rec.rejection_reason or '',
        })
    return rows


def _line_rows(records):
    """One row per line item, for the optional 'Line Items' sheet."""
    rows = []
    idx = 0
    for rec in records:
        for line in rec.line_ids:
            idx += 1
            rows.append({
                'idx': idx,
                'name': rec.name or '',
                'employee': rec.employee_id.name or '',
                'date': line.date.isoformat() if line.date else '',
                'category': CATEGORY_LABELS.get(line.category, line.category or ''),
                'description': line.description or '',
                'amount': float(line.amount or 0),
                'currency': rec.currency_id.name or '',
                'receipt_url': line.receipt_url or '',
            })
    return rows


def _build_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(EXPORT_HEADERS)
    for r in rows:
        writer.writerow([
            r['idx'], r['name'], r['employee'], r['department'], r['request_date'],
            r['category'], r['description'], r['line_count'], r['amount'], r['currency'],
            r['state'], r['submitted'], r['decided'], r['reviewed_by'], r['reason'],
        ])
    return buf.getvalue().encode('utf-8')


def _build_xlsx(rows, line_rows=None):
    import xlsxwriter
    line_rows = line_rows or []
    output = io.BytesIO()
    wb = xlsxwriter.Workbook(output, {'in_memory': True})
    ws = wb.add_worksheet('Reimbursements')

    title_fmt = wb.add_format({
        'bold': True, 'font_size': 16, 'font_color': WHITE,
        'bg_color': PRIMARY, 'align': 'left', 'valign': 'vcenter',
    })
    sub_fmt = wb.add_format({
        'italic': True, 'font_size': 10, 'font_color': TEXT_LIGHT,
        'bg_color': PRIMARY, 'align': 'left', 'valign': 'vcenter',
    })
    header_fmt = wb.add_format({
        'bold': True, 'font_size': 11, 'font_color': WHITE,
        'bg_color': ACCENT, 'align': 'center', 'valign': 'vcenter',
        'border': 1, 'border_color': BORDER, 'text_wrap': True,
    })
    cell = wb.add_format({
        'font_size': 10, 'font_color': TEXT_DARK,
        'align': 'left', 'valign': 'vcenter',
        'border': 1, 'border_color': BORDER, 'text_wrap': True,
    })
    cell_alt = wb.add_format({
        'font_size': 10, 'font_color': TEXT_DARK, 'bg_color': LIGHT_BG,
        'align': 'left', 'valign': 'vcenter',
        'border': 1, 'border_color': BORDER, 'text_wrap': True,
    })
    num = wb.add_format({
        'font_size': 10, 'align': 'right', 'valign': 'vcenter',
        'border': 1, 'border_color': BORDER, 'num_format': '#,##0.00',
    })
    num_alt = wb.add_format({
        'font_size': 10, 'align': 'right', 'valign': 'vcenter',
        'border': 1, 'border_color': BORDER, 'num_format': '#,##0.00',
        'bg_color': LIGHT_BG,
    })
    badge = {
        'Pending': wb.add_format({'bold': True, 'font_color': WHITE, 'bg_color': WARNING, 'align': 'center', 'border': 1, 'border_color': BORDER}),
        'Approved': wb.add_format({'bold': True, 'font_color': WHITE, 'bg_color': SUCCESS, 'align': 'center', 'border': 1, 'border_color': BORDER}),
        'Reimbursed': wb.add_format({'bold': True, 'font_color': WHITE, 'bg_color': SUCCESS, 'align': 'center', 'border': 1, 'border_color': BORDER}),
        'Rejected': wb.add_format({'bold': True, 'font_color': WHITE, 'bg_color': DANGER, 'align': 'center', 'border': 1, 'border_color': BORDER}),
    }

    widths = [5, 14, 22, 18, 13, 18, 35, 9, 13, 10, 12, 18, 18, 18, 30]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)

    ws.merge_range(0, 0, 0, len(EXPORT_HEADERS) - 1, 'Reimbursement Report', title_fmt)
    ws.set_row(0, 30)
    now_str = (fields.Datetime.now() + IST_OFFSET).strftime('%d %b %Y, %I:%M %p')
    ws.merge_range(1, 0, 1, len(EXPORT_HEADERS) - 1, 'Generated on: %s' % now_str, sub_fmt)
    ws.set_row(1, 18)

    ws.set_row(3, 26)
    for col, h in enumerate(EXPORT_HEADERS):
        ws.write(3, col, h, header_fmt)

    for ridx, r in enumerate(rows):
        row = ridx + 4
        is_alt = ridx % 2 == 1
        text = cell_alt if is_alt else cell
        n_fmt = num_alt if is_alt else num
        ws.write(row, 0, r['idx'], text)
        ws.write(row, 1, r['name'], text)
        ws.write(row, 2, r['employee'], text)
        ws.write(row, 3, r['department'], text)
        ws.write(row, 4, r['request_date'], text)
        ws.write(row, 5, r['category'], text)
        ws.write(row, 6, r['description'], text)
        ws.write_number(row, 7, r['line_count'] or 0, n_fmt)
        ws.write_number(row, 8, r['amount'] or 0, n_fmt)
        ws.write(row, 9, r['currency'], text)
        ws.write(row, 10, r['state'], badge.get(r['state'], text))
        ws.write(row, 11, r['submitted'], text)
        ws.write(row, 12, r['decided'], text)
        ws.write(row, 13, r['reviewed_by'], text)
        ws.write(row, 14, r['reason'], text)

    if rows:
        ws.autofilter(3, 0, 3 + len(rows), len(EXPORT_HEADERS) - 1)
    ws.freeze_panes(4, 2)

    # ── Secondary sheet: per-line detail ──────────────────────────────────
    if line_rows:
        ws2 = wb.add_worksheet('Line Items')
        widths2 = [5, 14, 22, 13, 18, 40, 13, 10, 50]
        for i, w in enumerate(widths2):
            ws2.set_column(i, i, w)
        ws2.merge_range(0, 0, 0, len(LINE_HEADERS) - 1, 'Reimbursement — Line Items', title_fmt)
        ws2.set_row(0, 30)
        ws2.merge_range(1, 0, 1, len(LINE_HEADERS) - 1, 'Generated on: %s' % now_str, sub_fmt)
        ws2.set_row(1, 18)
        ws2.set_row(3, 26)
        for col, h in enumerate(LINE_HEADERS):
            ws2.write(3, col, h, header_fmt)
        for ridx, r in enumerate(line_rows):
            row = ridx + 4
            is_alt = ridx % 2 == 1
            text = cell_alt if is_alt else cell
            n_fmt = num_alt if is_alt else num
            ws2.write(row, 0, r['idx'], text)
            ws2.write(row, 1, r['name'], text)
            ws2.write(row, 2, r['employee'], text)
            ws2.write(row, 3, r['date'], text)
            ws2.write(row, 4, r['category'], text)
            ws2.write(row, 5, r['description'], text)
            ws2.write_number(row, 6, r['amount'] or 0, n_fmt)
            ws2.write(row, 7, r['currency'], text)
            if r['receipt_url']:
                ws2.write_url(row, 8, r['receipt_url'], text, string='View')
            else:
                ws2.write(row, 8, '', text)
        ws2.autofilter(3, 0, 3 + len(line_rows), len(LINE_HEADERS) - 1)
        ws2.freeze_panes(4, 2)

    wb.close()
    return output.getvalue()


class ReimbursementExportController(http.Controller):

    @http.route('/api/v1/reimbursement/export', methods=['GET', 'POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_reimbursement(self, **kwargs):
        try:
            user = request.env.user
            Reimbursement = request.env['etp.reimbursement'].sudo()

            domain = []
            if not _is_hr(user):
                if not user.employee_id:
                    return return_Response(message='No employee profile linked.', status=400)
                domain.append(('employee_id', '=', user.employee_id.id))
            domain = _apply_filters(domain, kwargs)

            records = Reimbursement.search(domain, order='submitted_date desc, id desc')
            rows = _claim_rows(records)
            line_rows = _line_rows(records)

            fmt = (kwargs.get('format') or 'xlsx').lower()
            timestamp = (fields.Datetime.now() + IST_OFFSET).strftime('%Y%m%d_%H%M%S')

            if fmt == 'csv':
                file_bytes = _build_csv(rows)
                filename = 'reimbursement_report_%s.csv' % timestamp
            else:
                file_bytes = _build_xlsx(rows, line_rows=line_rows)
                filename = 'reimbursement_report_%s.xlsx' % timestamp

            try:
                file_b64 = base64.b64encode(file_bytes).decode('utf-8')
                s3_url = generate_s3_link(file_b64, prefix='reimbursement/exports', filename=filename)
            except Exception as e:
                _logger.error('S3 upload failed for reimbursement export: %s', e)
                return return_Response(message='Export upload failed.', status=400)

            return return_Response(
                message='Export generated.',
                status=200,
                data={
                    'download_url': s3_url or '',
                    'format': 'csv' if fmt == 'csv' else 'xlsx',
                    'row_count': len(rows),
                    'claim_count': len(records),
                },
            )
        except Exception as e:
            _logger.exception('Reimbursement export failed: %s', e)
            return return_Response(message=str(e), status=400)
