import html
import logging
import re
from string import Template

from odoo import fields, http
from odoo.http import request
from odoo.tools import html_sanitize, is_html_empty

from .job import _serialize_job

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Branded HTML page helpers (shown to the approver in the browser).
# ---------------------------------------------------------------------------
_PAGE = Template("""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>$title</title>
</head>
<body style="margin:0;padding:0;background-color:#f2f3f7;font-family:Arial,Helvetica,sans-serif;color:#33343f;">
  <div style="max-width:660px;margin:0 auto;padding:24px 16px;">
    <div style="background-color:#f7f8fb;border-radius:10px;padding:28px 32px;">
      <div style="margin-bottom:16px;">
        <span style="font-size:22px;font-weight:bold;color:#e6007e;">Ethara</span><span style="font-size:22px;font-weight:bold;color:#1b1b3a;">.AI</span>
      </div>
      <div style="border-top:1px solid #e3e5ec;margin-bottom:20px;"></div>
      $content
      <div style="border-top:1px solid #e3e5ec;margin-top:24px;padding-top:12px;color:#8a8b96;font-size:12px;line-height:1.6;">
        <div style="font-weight:bold;color:#33343f;">Ethara.AI</div>
        <div>5th Floor, Plot No. 273, Udyog Vihar Phase 1, Sector 20, Gurugram, Haryana 122016</div>
        <div style="margin-top:4px;"><a href="https://www.ethara.ai" style="color:#e6007e;text-decoration:none;">www.ethara.ai</a></div>
      </div>
    </div>
  </div>
</body>
</html>
""")


def _page(title, content_html, status=200):
    body = _PAGE.safe_substitute(title=html.escape(title), content=content_html)
    return request.make_response(
        body, headers=[('Content-Type', 'text/html; charset=utf-8')], status=status)


def _message(heading, message, color='#33343f', status=200, sub=None):
    content = (
        '<h2 style="margin:0 0 12px 0;color:%s;font-size:20px;">%s</h2>'
        '<p style="margin:0;color:#4b4c57;font-size:15px;line-height:1.6;">%s</p>'
    ) % (color, html.escape(heading), message)
    if sub:
        content += (
            '<p style="margin:14px 0 0 0;color:#6b6c78;font-size:13px;">%s</p>' % sub)
    return _page(heading, content, status=status)


def _btn(url, label, color):
    return (
        '<a href="%s" style="display:inline-block;background-color:%s;color:#ffffff;'
        'text-decoration:none;font-weight:bold;font-size:14px;padding:12px 24px;'
        'border-radius:8px;margin:6px 12px 6px 0;">%s</a>'
    ) % (url, color, html.escape(label))


def _detail_row(label, value):
    if value in (None, '', '—'):
        return ''
    return (
        '<tr>'
        '<td style="padding:6px 24px 6px 0;font-weight:bold;color:#33343f;'
        'vertical-align:top;white-space:nowrap;">%s</td>'
        '<td style="padding:6px 0;color:#4b4c57;">%s</td>'
        '</tr>'
    ) % (html.escape(label), html.escape(str(value)))


def _bullet_section(title, items):
    items = [i for i in (items or []) if i]
    if not items:
        return ''
    lis = ''.join(
        '<li style="margin:0 0 6px 0;color:#4b4c57;">%s</li>' % html.escape(i)
        for i in items)
    return (
        '<div style="margin-top:20px;">'
        '<div style="font-weight:bold;color:#33343f;font-size:15px;margin-bottom:8px;">%s</div>'
        '<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.5;">%s</ul>'
        '</div>'
    ) % (html.escape(title), lis)


def _text_section(title, text):
    if not text:
        return ''
    return (
        '<div style="margin-top:20px;">'
        '<div style="font-weight:bold;color:#33343f;font-size:15px;margin-bottom:6px;">%s</div>'
        '<div style="color:#4b4c57;font-size:14px;line-height:1.6;">%s</div>'
        '</div>'
    ) % (html.escape(title), text)


def _tags_section(title, tags):
    tags = [t for t in (tags or []) if t]
    if not tags:
        return ''
    chips = ''.join(
        '<span style="display:inline-block;background-color:#eceffb;color:#3b4a6b;'
        'font-size:12px;padding:4px 10px;border-radius:12px;margin:0 6px 6px 0;">%s</span>'
        % html.escape(t) for t in tags)
    return (
        '<div style="margin-top:20px;">'
        '<div style="font-weight:bold;color:#33343f;font-size:15px;margin-bottom:8px;">%s</div>'
        '<div>%s</div></div>'
    ) % (html.escape(title), chips)


# ---------------------------------------------------------------------------
# Free-text formatting.
# The JD description reaches us either as real rich HTML (authored in a
# WYSIWYG editor) or - the usual case for API-created JDs - as plain text that
# the Html field wrapped in a single tag, with all the structure living in
# literal newlines. Browsers collapse those newlines, so the JD reads as one
# big paragraph. These helpers rebuild proper HTML (paragraphs, bullet and
# numbered lists, sub-headings, **bold**) from that text.
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r'</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>')
# Tags that mean the HTML already carries its own layout and can be rendered
# as authored. A single <p>/<div>/<span> wrapper does NOT count.
_STRUCTURAL_TAGS = {
    'br', 'hr', 'li', 'ul', 'ol', 'table', 'tr', 'td', 'th', 'thead',
    'tbody', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'img',
}
_BLOCK_OPEN_RE = re.compile(r'<(?:p|div)\b', re.IGNORECASE)
_BULLET_LINE_RE = re.compile(r'^[-*•▪◦‣–—]\s+(.+)$')
_NUMBERED_LINE_RE = re.compile(r'^\(?\d{1,3}[.)]\s+(.+)$')
_MD_HEADING_RE = re.compile(r'^#{1,6}\s+(.+?)\s*#*$')
_MD_BOLD_RE = re.compile(r'\*\*([^*\n]+)\*\*')

_P_STYLE = 'margin:0 0 12px 0;'
_LIST_STYLE = 'margin:0 0 12px 0;padding-left:20px;'
_LI_STYLE = 'margin:0 0 6px 0;'
_SUBHEAD_STYLE = 'font-weight:bold;color:#33343f;margin:14px 0 6px 0;'


def _inline_html(text):
    """Escape a text fragment, then apply **bold** emphasis."""
    return _MD_BOLD_RE.sub(r'<strong>\1</strong>', html.escape(text))


def _is_subheading(line):
    """Short 'Section title:' lines act as sub-headings in plain-text JDs."""
    return line.endswith(':') and len(line) <= 60


def _plain_to_html(text):
    """Convert plain text to styled HTML.

    Blank lines split paragraphs, single newlines become explicit line
    breaks, and bullet/numbered/heading lines become real lists and
    sub-headings. All content is escaped, so the result is safe to inject.
    """
    if not text:
        return ''
    text = str(text).replace('\r\n', '\n').replace('\r', '\n')
    blocks = []      # rendered html blocks
    paragraph = []   # pending paragraph lines
    items = []       # pending list items
    list_tag = [None]  # 'ul' | 'ol' (list so the closures can rebind it)

    def flush_paragraph():
        if paragraph:
            blocks.append('<p style="%s">%s</p>'
                          % (_P_STYLE, '<br/>'.join(paragraph)))
            del paragraph[:]

    def flush_list():
        if items:
            lis = ''.join('<li style="%s">%s</li>' % (_LI_STYLE, i)
                          for i in items)
            blocks.append('<%s style="%s">%s</%s>'
                          % (list_tag[0], _LIST_STYLE, lis, list_tag[0]))
            del items[:]
        list_tag[0] = None

    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue
        bullet = _BULLET_LINE_RE.match(line)
        numbered = None if bullet else _NUMBERED_LINE_RE.match(line)
        if bullet or numbered:
            flush_paragraph()
            tag = 'ul' if bullet else 'ol'
            if list_tag[0] != tag:
                flush_list()
                list_tag[0] = tag
            items.append(_inline_html((bullet or numbered).group(1)))
            continue
        heading = _MD_HEADING_RE.match(line)
        if heading or _is_subheading(line):
            flush_paragraph()
            flush_list()
            blocks.append('<div style="%s">%s</div>'
                          % (_SUBHEAD_STYLE,
                             _inline_html(heading.group(1) if heading else line)))
            continue
        flush_list()
        paragraph.append(_inline_html(line))
    flush_paragraph()
    flush_list()
    return ''.join(blocks)


def _description_html(raw):
    """Render the JD description as clean HTML.

    Real rich HTML (several block tags, or structural tags such as lists,
    headings and explicit line breaks) is sanitized and rendered as authored.
    Otherwise - plain text wrapped in a single tag by the Html field - the
    wrapper is dropped and the text is rebuilt into paragraphs and lists
    from its newlines.
    """
    if not raw or is_html_empty(raw):
        return ''
    source = str(raw)
    tags = {t.lower() for t in _TAG_RE.findall(source)}
    block_count = len(_BLOCK_OPEN_RE.findall(source))
    if _STRUCTURAL_TAGS.intersection(tags) or block_count > 1:
        return html_sanitize(source)
    plain = html.unescape(_TAG_RE.sub(' ', source))
    return _plain_to_html(plain)


class EtharaJobApprovalController(http.Controller):

    # ------------------------------------------------------------------
    # Shared resolution: token -> job, uid -> user, authorization check.
    # Returns (job, user, error_code). error_code is None on success.
    # ------------------------------------------------------------------
    def _resolve(self, token, uid):
        Job = request.env['hr.job']
        job = Job.sudo()._resolve_approval_token(token)
        if not job:
            return None, None, 'invalid'
        if job.approval_token_expiry and job.approval_token_expiry < fields.Datetime.now():
            return job, None, 'expired'
        user = None
        if uid is not None and str(uid).strip().isdigit():
            user = request.env['res.users'].sudo().browse(int(uid)).exists()
        if not user:
            return job, None, 'no_user'
        approvers = Job.sudo()._get_job_approver_users()
        if user.id not in approvers.ids:
            return job, user, 'unauthorized'
        return job, user, None

    def _error_page(self, code):
        pages = {
            'invalid': ("Link no longer valid",
                        "This approval link is invalid or has already been used."),
            'expired': ("Link expired",
                        "This approval link has expired. Please ask the recruiter "
                        "to resend the approval request."),
            'no_user': ("Approver not recognised",
                        "We could not identify the approver for this link."),
            'unauthorized': ("Not authorised",
                             "You are not authorised to approve or reject this job "
                             "position. Please contact the recruiter."),
        }
        heading, message = pages.get(code, ("Something went wrong",
                                            "This request could not be processed."))
        return _message(heading, message, color='#dc3b3b', status=400)

    def _already_decided_page(self, job):
        status_label = {
            'posted': 'approved', 'rejected': 'rejected',
            'withdrawn': 'withdrawn', 'draft': 'reset to draft',
        }.get(job.approval_status, job.approval_status or 'processed')
        return _message(
            "Already %s" % status_label,
            "This job position has already been <strong>%s</strong>, so no further "
            "action is needed." % html.escape(status_label),
            color='#6b6c78')

    # ------------------------------------------------------------------
    # View full JD
    # ------------------------------------------------------------------
    @http.route('/api/v1/job/approval/view', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def view_jd(self, token=None, uid=None, **kw):
        try:
            job, user, err = self._resolve(token, uid)
            if err:
                return self._error_page(err)

            data = _serialize_job(job, detail=True)
            decided = job.approval_status != 'requested'

            grid = ''.join([
                _detail_row('Department', data.get('department')),
                _detail_row('Location', data.get('location')),
                _detail_row('Employment Type', data.get('employmentType')),
                _detail_row('Work Mode', data.get('workMode')),
                _detail_row('Experience Level', data.get('experienceLevel')),
                _detail_row('Experience (years)', data.get('experienceYears') or None),
                _detail_row('Openings', data.get('openings') or None),
                _detail_row('Salary Bracket', data.get('salaryBracket')),
                _detail_row('Urgency (P-level)', data.get('urgencyLevel')),
                _detail_row('Requested by', data.get('requestedBy')),
            ])

            sections = ''.join([
                _text_section('Summary', _plain_to_html(data.get('summary')) or None),
                _text_section('Description', _description_html(data.get('description')) or None),
                _bullet_section('Required Skill Set', data.get('requirements')),
                _bullet_section('Key Responsibilities', data.get('responsibilities')),
                _tags_section('Skill Keywords', data.get('skillKeywords')),
                _text_section('AI Screening Prompt',
                              _plain_to_html(data.get('screeningPrompt')) or None),
            ])

            actions = ''
            if not decided:
                urls = job._approval_urls(user.id)
                actions = (
                    '<div style="border-top:1px solid #e3e5ec;margin-top:24px;'
                    'padding-top:18px;">%s%s'
                    '<p style="margin:10px 0 0 0;color:#6b6c78;font-size:13px;">'
                    'A rejection reason is required before the JD is rejected.</p></div>'
                ) % (_btn(urls['approve'], 'Approve JD', '#1da25a'),
                     _btn(urls['reject'], 'Reject JD', '#dc3b3b'))
            else:
                actions = (
                    '<div style="border-top:1px solid #e3e5ec;margin-top:24px;'
                    'padding-top:18px;color:#6b6c78;font-size:13px;">This JD has already '
                    'been %s.</div>' % html.escape(job.approval_status or ''))

            content = (
                '<h1 style="margin:0 0 6px 0;font-size:22px;color:#1b1b3a;">%s</h1>'
                '<p style="margin:0 0 18px 0;color:#6b6c78;font-size:13px;">'
                'Job Description awaiting your review</p>'
                '<table role="presentation" cellpadding="0" cellspacing="0" '
                'style="width:100%%;">%s</table>%s%s'
            ) % (html.escape(data.get('title') or 'Job Position'), grid, sections, actions)

            return _page('View JD — %s' % (data.get('title') or ''), content)
        except Exception as exc:
            _logger.exception('view_jd failed')
            return _message("Something went wrong",
                            "We could not load this job description.",
                            color='#dc3b3b', status=500)

    # ------------------------------------------------------------------
    # Approve JD (one click)
    # ------------------------------------------------------------------
    @http.route('/api/v1/job/approval/approve', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def approve_jd(self, token=None, uid=None, **kw):
        try:
            job, user, err = self._resolve(token, uid)
            if err:
                return self._error_page(err)
            if job.approval_status != 'requested':
                return self._already_decided_page(job)

            job._approve_jd(user)
            _logger.info('JD %s approved by user %s', job.id, user.id)
            return _message(
                "JD Approved ✓",
                "<strong>%s</strong> has been approved and is now posted on the "
                "careers page." % html.escape(job.name or ''),
                color='#1da25a',
                sub="Approved by %s. This link is now closed." % html.escape(
                    user.name or user.login or ''))
        except Exception as exc:
            _logger.exception('approve_jd failed')
            return _message("Something went wrong",
                            "We could not approve this job position.",
                            color='#dc3b3b', status=500)

    # ------------------------------------------------------------------
    # Reject JD (GET shows the reason form, POST performs the rejection)
    # ------------------------------------------------------------------
    @http.route('/api/v1/job/approval/reject', type='http', auth='none',
                methods=['GET', 'POST'], csrf=False, cors='*')
    def reject_jd(self, token=None, uid=None, reason=None, **kw):
        try:
            job, user, err = self._resolve(token, uid)
            if err:
                return self._error_page(err)
            if job.approval_status != 'requested':
                return self._already_decided_page(job)

            if request.httprequest.method == 'POST':
                reason = (reason or '').strip()
                if not reason:
                    return self._reject_form(job, token, uid,
                                             error="Please enter a rejection reason.")
                job._reject_jd(user, reason)
                _logger.info('JD %s rejected by user %s', job.id, user.id)
                return _message(
                    "JD Rejected",
                    "<strong>%s</strong> has been rejected and will not be posted."
                    % html.escape(job.name or ''),
                    color='#dc3b3b',
                    sub="Rejected by %s. This link is now closed." % html.escape(
                        user.name or user.login or ''))

            return self._reject_form(job, token, uid)
        except Exception as exc:
            _logger.exception('reject_jd failed')
            return _message("Something went wrong",
                            "We could not process this rejection.",
                            color='#dc3b3b', status=500)

    def _reject_form(self, job, token, uid, error=None):
        err_html = ''
        if error:
            err_html = ('<p style="margin:0 0 12px 0;color:#dc3b3b;font-size:13px;">'
                        '%s</p>') % html.escape(error)
        content = (
            '<h2 style="margin:0 0 6px 0;font-size:20px;color:#1b1b3a;">Reject JD</h2>'
            '<p style="margin:0 0 16px 0;color:#4b4c57;font-size:15px;">'
            'You are rejecting <strong>%s</strong>. A reason is required and will be '
            'saved on the job position.</p>%s'
            '<form method="post" action="/api/v1/job/approval/reject">'
            '<input type="hidden" name="token" value="%s"/>'
            '<input type="hidden" name="uid" value="%s"/>'
            '<textarea name="reason" required rows="5" placeholder="Enter the reason for rejection..." '
            'style="width:100%%;box-sizing:border-box;padding:12px;border:1px solid #d3d6e0;'
            'border-radius:8px;font-size:14px;font-family:inherit;resize:vertical;"></textarea>'
            '<div style="margin-top:16px;">'
            '<button type="submit" style="background-color:#dc3b3b;color:#ffffff;border:none;'
            'font-weight:bold;font-size:14px;padding:12px 24px;border-radius:8px;cursor:pointer;">'
            'Confirm Rejection</button></div></form>'
        ) % (
            html.escape(job.name or ''),
            err_html,
            html.escape(token or ''),
            html.escape(str(uid or '')),
        )
        return _page('Reject JD — %s' % (job.name or ''), content)
