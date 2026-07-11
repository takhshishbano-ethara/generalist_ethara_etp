# -*- coding: utf-8 -*-
import base64
import csv
import io
import re

from markupsafe import escape

from odoo import models, fields, api, _
from odoo.exceptions import UserError

try:  # optional dependency — CSV always works, XLSX only if available
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# accepted header spellings (normalised) that identify each optional column
_EMAIL_ALIASES = {'email', 'e mail', 'mail', 'login', 'user email',
                  'email id', 'emailid', 'email address'}
_PL_ALIASES = {'pl', 'assigned pl', 'project lead', 'pl email', 'pl name',
               'assigned pl email', 'assigned project lead'}
_QL_ALIASES = {'ql', 'assigned ql', 'quality lead', 'ql email', 'ql name',
               'assigned ql email', 'assigned quality lead'}
_STATUS_ALIASES = {'status', 'state', 'member status'}
# normalised status spellings -> stored value (see kensei.tracker.team.member)
_STATUS_MAP = {
    'active': 'active',
    'on hold': 'on_hold', 'onhold': 'on_hold', 'hold': 'on_hold',
    'inactive': 'inactive', 'in active': 'inactive',
}


class KenseiTrackerTeamImport(models.TransientModel):
    """Bulk-add existing Odoo users to the Kensei Tracker team roster.

    Never creates users: each email is resolved against ``res.users`` and only
    matches are linked. Non-matches, invalid addresses and duplicates are
    reported back in a summary plus a downloadable error report.
    """
    _name = 'kensei.tracker.team.import'
    _description = 'Kensei Tracker — Team Bulk Import'

    upload_file = fields.Binary(string='File (CSV / XLSX)', required=True)
    filename = fields.Char(string='Filename')
    state = fields.Selection(
        [('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Done')],
        default='draft')

    imported_count = fields.Integer(string='Imported', readonly=True)
    already_count = fields.Integer(string='Already Added', readonly=True)
    notfound_count = fields.Integer(string='User Not Found', readonly=True)
    invalid_count = fields.Integer(string='Invalid Email', readonly=True)
    failed_count = fields.Integer(string='Import Failed', readonly=True)
    warning_count = fields.Integer(string='Rows With Warnings', readonly=True)
    preview_html = fields.Html(string='Preview', readonly=True, sanitize=False)
    result_html = fields.Html(string='Summary', readonly=True, sanitize=False)
    error_report = fields.Binary(string='Error Report', readonly=True)
    error_filename = fields.Char(readonly=True)

    # classification key -> (label, colour) for preview / summary rendering
    _STATE_LABELS = {
        'import': ('To Import', '#10b981'),
        'already': ('Already Added', '#6b7280'),
        'notfound': ('User Not Found', '#f59e0b'),
        'invalid': ('Invalid Email', '#ef4444'),
        'error': ('Import Failed', '#ef4444'),
    }

    # ------------------------------------------------------------------ #
    #  Parsing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _norm(v):
        return ' '.join((v or '').strip().lower().replace('_', ' ').replace('-', ' ').split())

    def _map_columns(self, header):
        """Locate each supported column by header name (all optional but email)."""
        norm = [self._norm(h) for h in header]

        def find(aliases):
            return next((i for i, h in enumerate(norm) if h in aliases), None)

        return {
            'email': find(_EMAIL_ALIASES),
            'pl': find(_PL_ALIASES),
            'ql': find(_QL_ALIASES),
            'status': find(_STATUS_ALIASES),
        }

    def _parse_rows(self):
        """Return ordered dicts ``{'row', 'email', 'pl', 'ql', 'status'}``.

        ``email`` is the only required column. ``pl`` / ``ql`` / ``status`` are
        read only when a recognisable header names them; a plain single-column
        (headerless) file still works as an email-only list.
        """
        data = base64.b64decode(self.upload_file)
        name = (self.filename or '').lower()
        if name.endswith(('.xlsx', '.xlsm', '.xls')):
            table = self._read_xlsx(data)
        else:
            table = self._read_csv(data)
        table = [r for r in table if any((str(c) or '').strip() for c in r)]
        if not table:
            return []

        cols = self._map_columns(table[0])
        if cols['email'] is None:
            # no recognisable header -> first column holds the email
            cols = {'email': 0, 'pl': None, 'ql': None, 'status': None}
            body, offset = table, 1
        else:
            body, offset = table[1:], 2  # +1 header, +1 human 1-based

        def cell(row, idx):
            if idx is None or idx >= len(row):
                return ''
            c = row[idx]
            return str(c).strip() if c is not None else ''

        return [{
            'row': i + offset,
            'email': cell(row, cols['email']),
            'pl': cell(row, cols['pl']),
            'ql': cell(row, cols['ql']),
            'status': cell(row, cols['status']),
        } for i, row in enumerate(body)]

    # ------------------------------------------------------------------ #
    #  Resolution helpers (PL / QL / status)
    # ------------------------------------------------------------------ #
    def _resolve_people(self, tokens):
        """Resolve each raw token (an email/login OR a full name) to a single
        ``res.users``. Returns ``{token_lower: (user, note)}`` where ``user`` is
        an empty recordset when unresolved/ambiguous and ``note`` explains why."""
        Users = self.env['res.users'].sudo()
        empty = Users.browse()
        tokens = {t.strip() for t in tokens if t and t.strip()}
        if not tokens:
            return {}

        # 1) login / email (case-insensitive)
        terms = list(tokens | {t.lower() for t in tokens})
        le = {}
        for u in Users.search(['|', ('login', 'in', terms), ('email', 'in', terms)]):
            if u.login:
                le.setdefault(u.login.strip().lower(), u)
            if u.email:
                le.setdefault(u.email.strip().lower(), u)

        # 2) name (only tokens not already resolved and not email-shaped)
        name_tokens = [t for t in tokens
                       if t.lower() not in le and not _EMAIL_RE.match(t)]
        name_map = {}
        if name_tokens:
            domain = [('name', '=ilike', name_tokens[0])]
            for t in name_tokens[1:]:
                domain = ['|'] + domain + [('name', '=ilike', t)]
            for u in Users.search(domain):
                name_map.setdefault(self._norm(u.name), empty)
                name_map[self._norm(u.name)] |= u

        result = {}
        for t in tokens:
            tl = t.lower()
            if tl in le:
                result[tl] = (le[tl], '')
                continue
            cand = name_map.get(self._norm(t), empty)
            if len(cand) == 1:
                result[tl] = (cand, '')
            elif len(cand) > 1:
                result[tl] = (empty, _("“%s” matches multiple users — left unset.", t))
            else:
                result[tl] = (empty, _("“%s” not found — left unset.", t))
        return result

    def _person_for(self, raw, people):
        """Look up a resolved (user, note) for a raw PL/QL cell."""
        empty = self.env['res.users'].browse()
        raw = (raw or '').strip()
        if not raw:
            return empty, ''
        return people.get(raw.lower(), (empty, ''))

    def _status_for(self, raw):
        """Normalise a status cell to a stored value; unknown -> Active + note."""
        raw = (raw or '').strip()
        if not raw:
            return 'active', ''
        st = _STATUS_MAP.get(self._norm(raw))
        if st:
            return st, ''
        return 'active', _("Unknown status “%s” — defaulted to Active.", raw)

    def _read_csv(self, data):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise UserError(_("The CSV could not be read. Please upload a valid "
                              "UTF-8 encoded file."))
        return list(csv.reader(io.StringIO(text)))

    def _read_xlsx(self, data):
        if openpyxl is None:
            raise UserError(_("XLSX support is not available on this server. "
                              "Please upload a CSV file instead."))
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise UserError(_("The XLSX file could not be read. Please check the "
                              "file and try again."))
        ws = wb.active
        return [[c if c is not None else '' for c in row]
                for row in ws.iter_rows(values_only=True)]

    # ------------------------------------------------------------------ #
    #  Import
    # ------------------------------------------------------------------ #
    def _classify(self):
        """Parse + classify every row without writing anything.

        Returns an ordered list of entry dicts (email state + resolved PL / QL /
        status and any per-row ``warnings``). Shared by the preview and the
        actual import so both agree exactly.
        """
        if not self.upload_file:
            raise UserError(_("Please upload a CSV or XLSX file."))
        rows = self._parse_rows()
        if not rows:
            raise UserError(_("The file contains no email addresses."))

        Users = self.env['res.users'].sudo()
        Member = self.env['kensei.tracker.team.member']
        status_labels = dict(Member._fields['status'].selection)

        # validity + in-file de-duplication (first occurrence wins)
        prelim, seen = [], set()
        for r in rows:
            email = (r['email'] or '').strip()
            if not email:
                continue
            key = email.lower()
            if not _EMAIL_RE.match(email):
                prelim.append((r, email, 'invalid'))
            elif key in seen:
                prelim.append((r, email, 'dupe'))
            else:
                seen.add(key)
                prelim.append((r, email, 'valid'))

        # one query to resolve member users, one to find existing members
        emails = [e for _r, e, k in prelim if k in ('valid', 'dupe')]
        terms = list({e for e in emails} | {e.lower() for e in emails})
        by_email = {}
        if terms:
            for u in Users.search(['|', ('login', 'in', terms), ('email', 'in', terms)]):
                if u.login:
                    by_email.setdefault(u.login.strip().lower(), u)
                if u.email:
                    by_email.setdefault(u.email.strip().lower(), u)
        # include archived members so the unique(user_id) constraint can't trip
        existing_uids = set(Member.with_context(active_test=False).search(
            [('user_id', 'in', [u.id for u in by_email.values()])]).mapped('user_id').ids)

        # batch-resolve every PL / QL token referenced on importable rows
        people_tokens = set()
        for r, _e, kind in prelim:
            if kind in ('valid', 'dupe'):
                people_tokens.update(t for t in (r['pl'], r['ql']) if t)
        people = self._resolve_people(people_tokens)

        entries, batch_uids = [], set()
        for r, email, kind in prelim:
            pl_user, pl_note = self._person_for(r['pl'], people)
            ql_user, ql_note = self._person_for(r['ql'], people)
            status, st_note = self._status_for(r['status'])
            warnings = [n for n in (
                _("PL %s", pl_note) if pl_note else '',
                _("QL %s", ql_note) if ql_note else '',
                st_note,
            ) if n]
            entry = {
                'row': r['row'], 'email': email, 'name': '', 'user_id': False,
                'pl_id': pl_user.id, 'pl_label': pl_user.name or (r['pl'] or ''),
                'ql_id': ql_user.id, 'ql_label': ql_user.name or (r['ql'] or ''),
                'status': status, 'status_label': status_labels.get(status, status),
                'warnings': warnings,
            }
            if kind == 'invalid':
                entry['state'] = 'invalid'
                entry['warnings'] = []  # a rejected row's assignments don't matter
                entries.append(entry)
                continue
            user = by_email.get(email.lower())
            if not user:
                entry['state'] = 'notfound'
                entry['warnings'] = []
                entries.append(entry)
                continue
            entry['name'] = user.name
            entry['user_id'] = user.id  # carry the resolved user so the create
            #                             skips team.member's email->user search
            if kind == 'dupe' or user.id in existing_uids or user.id in batch_uids:
                entry['state'] = 'already'
                entry['warnings'] = []  # not imported -> no assignment applied
            else:
                batch_uids.add(user.id)
                entry['state'] = 'import'
            entries.append(entry)
        return entries

    def _apply_counts(self, entries):
        self.imported_count = sum(1 for e in entries if e['state'] == 'import')
        self.already_count = sum(1 for e in entries if e['state'] == 'already')
        self.notfound_count = sum(1 for e in entries if e['state'] == 'notfound')
        self.invalid_count = sum(1 for e in entries if e['state'] == 'invalid')
        self.failed_count = sum(1 for e in entries if e['state'] == 'error')
        self.warning_count = sum(1 for e in entries if e.get('warnings'))

    def _reopen(self, name):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'name': name,
        }

    def action_preview(self):
        """Step 1 — parse the file and show the user what will happen."""
        self.ensure_one()
        entries = self._classify()
        self._apply_counts(entries)
        self.preview_html = self._render_preview(entries)
        self.state = 'preview'
        return self._reopen(_('Preview Import'))

    def action_import(self):
        """Step 2 — commit: add only the rows classified as 'To Import'."""
        self.ensure_one()
        entries = self._classify()
        Member = self.env['kensei.tracker.team.member'].with_context(
            kensei_skip_toast=True)
        # Per-row create: each member is written inside its own savepoint, so an
        # unexpected failure on one row (e.g. a race-condition duplicate) only
        # skips that row and is reported — the rest of the batch still imports.
        for e in entries:
            if e['state'] != 'import':
                continue
            # Pass the already-resolved user_id (team.member's user_id is the
            # canonical link) so create() does not re-run an email->user search
            # per row; email is derived from user_id on the model.
            vals = {'user_id': e['user_id'], 'status': e['status']}
            if e['pl_id']:
                vals['assigned_pl_id'] = e['pl_id']
            if e['ql_id']:
                vals['assigned_ql_id'] = e['ql_id']
            try:
                with self.env.cr.savepoint():
                    Member.create(vals)
            except Exception as ex:  # noqa: BLE001 — any failure is per-row isolated
                e['state'] = 'error'
                first = (str(ex) or '').splitlines()
                e['error'] = first[0] if first else _("create failed")
        self._apply_counts(entries)
        self._build_error_report(entries)
        self.result_html = self._render_summary()
        self.state = 'done'
        return self._reopen(_('Import Summary'))

    def action_back(self):
        """Return from the preview to re-pick a file."""
        self.ensure_one()
        self.state = 'draft'
        return self._reopen(_('Bulk Upload Team'))

    def _render_preview(self, entries):
        cap = 500
        head = (
            "<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
            "<thead><tr style='text-align:left;border-bottom:2px solid #d0d0d0;'>"
            "<th style='padding:4px 8px;'>Row</th>"
            "<th style='padding:4px 8px;'>Email</th>"
            "<th style='padding:4px 8px;'>Name</th>"
            "<th style='padding:4px 8px;'>Assigned PL</th>"
            "<th style='padding:4px 8px;'>Assigned QL</th>"
            "<th style='padding:4px 8px;'>Status</th>"
            "<th style='padding:4px 8px;'>Result</th></tr></thead><tbody>"
        )
        body = []
        for e in entries[:cap]:
            label, color = self._STATE_LABELS.get(e['state'], (e['state'], '#000'))
            imported = e['state'] == 'import'
            # only show PL/QL/status for rows that will actually be imported
            pl = escape(e['pl_label']) if imported else ''
            ql = escape(e['ql_label']) if imported else ''
            st = escape(e['status_label']) if imported else ''
            result = label
            if imported and e.get('warnings'):
                result += ("<div style='color:#f59e0b;font-weight:400;font-size:12px;'>%s</div>"
                           % escape(' '.join(e['warnings'])))
            body.append(
                "<tr style='border-bottom:1px solid #eee;'>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;color:%s;font-weight:600;'>%s</td></tr>"
                % (e['row'], escape(e['email']), escape(e['name'] or ''),
                   pl, ql, st, color, result)
            )
        tail = "</tbody></table>"
        if len(entries) > cap:
            tail += ("<div class='text-muted mt-1'>… and %s more row(s)</div>"
                     % (len(entries) - cap))
        return head + "".join(body) + tail

    def _build_error_report(self, entries):
        rows = [('Row', 'Email', 'Reason')]
        for e in entries:
            if e['state'] == 'notfound':
                rows.append((e['row'], e['email'], 'User not found in Odoo'))
            elif e['state'] == 'invalid':
                rows.append((e['row'], e['email'], 'Invalid email format'))
            elif e['state'] == 'error':
                rows.append((e['row'], e['email'],
                             'Import failed: %s' % (e.get('error') or 'unknown error')))
        # non-blocking warnings on imported rows (unresolved PL/QL, bad status)
        for e in entries:
            if e['state'] == 'import' and e.get('warnings'):
                for w in e['warnings']:
                    rows.append((e['row'], e['email'], w))
        if len(rows) == 1:
            self.error_report = False
            self.error_filename = False
            return
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        self.error_report = base64.b64encode(buf.getvalue().encode('utf-8-sig'))
        self.error_filename = 'team_import_errors.csv'

    def _render_summary(self):
        def line(label, count, color):
            return (
                "<div style='display:flex;justify-content:space-between;"
                "padding:6px 0;border-bottom:1px solid #eee;'>"
                "<span>%s</span><b style='color:%s'>%s</b></div>"
                % (label, color, count)
            )
        html = (
            "<div>"
            + line(_("Imported"), self.imported_count, "#10b981")
            + line(_("Already Added"), self.already_count, "#6b7280")
            + line(_("User Not Found"), self.notfound_count, "#f59e0b")
            + line(_("Invalid Email"), self.invalid_count, "#ef4444")
        )
        if self.failed_count:
            html += line(_("Import Failed"), self.failed_count, "#ef4444")
        if self.warning_count:
            html += line(_("Imported with warnings (PL/QL/status)"),
                         self.warning_count, "#f59e0b")
        return html + "</div>"
