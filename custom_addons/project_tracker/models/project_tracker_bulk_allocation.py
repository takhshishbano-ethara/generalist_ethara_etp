# -*- coding: utf-8 -*-
"""Bulk Allocation wizard.

Uploads a list of Persona names, reuses/creates the personas, then distributes
them across selected taskers — each persona to exactly one tasker, respecting an
optional per-tasker limit — and writes every resulting Task Allocation in one
batch. Preview (summary before saving) and a final report are provided.
"""
import base64
import csv
import heapq
import io
import random
from collections import deque

from markupsafe import escape

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class Kensei2TrackerBulkAllocation(models.TransientModel):
    _name = 'project.tracker.bulk.allocation'
    _description = 'Project Tracker — Bulk Persona Allocation'

    # ------------------------------------------------------------------ #
    #  Inputs
    # ------------------------------------------------------------------ #
    source_mode = fields.Selection(
        [('unassigned', 'Existing Personas'),
         ('file', 'Upload File')],
        string='Persona Source', default='unassigned', required=True,
        help='Existing Personas: allocate personas that already exist in the '
             'pool but are not yet assigned to anyone.\n'
             'Upload File: read persona names from a CSV/XLSX (reuse/create).')
    csv_file = fields.Binary(string='Persona Names File')
    filename = fields.Char(string='Filename')
    # Optional: restrict the existing-persona pool to one domain, so a run can
    # hand out only (e.g.) the "coding" personas. Empty = all domains.
    domain_filter = fields.Selection(
        selection='_domain_selection', string='Domain',
        help='Allocate only personas in this domain. Leave empty for all domains.')
    unassigned_available = fields.Integer(
        string='Unassigned Personas', compute='_compute_unassigned_available')

    @api.model
    def _domain_selection(self):
        """Distinct domains across personas, for the Domain filter dropdown."""
        groups = self.env['kensei2.persona']._read_group(
            [('pt_domain', '!=', False)], ['pt_domain'])
        domains = sorted({g[0] for g in groups if g[0]})
        return [(d, d) for d in domains]
    state = fields.Selection(
        [('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Done')],
        default='draft')

    # Required in the Bulk Allocation form (see the view), but NOT at the model
    # level: the Persona Import wizard builds a throwaway record of this model to
    # reuse the CSV parser, and that import needs no project.
    project_id = fields.Many2one(
        'project.tracker.project', string='Project',
        help="Allocate within this project: its active taskers are loaded below, "
             "and every allocation created is stamped with it.")
    allocation_method = fields.Selection(
        [('sequential', 'Sequential'), ('random', 'Random')],
        string='Allocation Method', default='sequential', required=True)
    limit_mode = fields.Selection(
        [('no_limit', 'No Limit'), ('limited', 'Limited')],
        string='Allocation Limit', default='no_limit', required=True)
    allocation_limit = fields.Integer(
        string='Max Personas per Tasker', default=1,
        help='Maximum total personas a tasker may hold (current + newly '
             'allocated). Used only when Allocation Limit is "Limited".')

    tasker_line_ids = fields.One2many(
        'project.tracker.bulk.allocation.line', 'wizard_id', string='Taskers')

    # ------------------------------------------------------------------ #
    #  Preview summary (computed on Preview, read-only)
    # ------------------------------------------------------------------ #
    total_personas = fields.Integer(readonly=True)
    existing_personas = fields.Integer(readonly=True)
    new_personas = fields.Integer(readonly=True)
    skipped_personas = fields.Integer(readonly=True)
    selected_taskers = fields.Integer(readonly=True)
    expected_assignments = fields.Integer(readonly=True)
    unallocated_expected = fields.Integer(readonly=True)

    # ------------------------------------------------------------------ #
    #  Result report (computed on Start, read-only)
    # ------------------------------------------------------------------ #
    assigned_count = fields.Integer(string='Total Personas Assigned', readonly=True)
    created_count = fields.Integer(string='Newly Created Personas', readonly=True)
    skipped_count = fields.Integer(string='Skipped Records', readonly=True)
    unallocated_count = fields.Integer(string='Unallocated Personas', readonly=True)
    result_html = fields.Html(string='Report', readonly=True, sanitize=False)

    # ------------------------------------------------------------------ #
    #  Taskers — the project's OWN active taskers, each with their current load
    #  and all ticked. Loaded on open (if a project is passed in) and refreshed
    #  whenever the Project is changed.
    # ------------------------------------------------------------------ #
    def _tasker_line_commands(self, project_id):
        """One2many create-commands for every active tasker on ``project_id``."""
        Member = self.env['project.tracker.team.member']
        Alloc = self.env['project.tracker.allocation']
        taskers = Member.search([
            ('project_ids', 'in', project_id),
            ('role', '=', 'tasker'),
            ('status', '=', 'active'),
        ])
        counts = {}
        if taskers:
            for m, c in Alloc._read_group(
                    [('tasker_member_id', 'in', taskers.ids)],
                    ['tasker_member_id'], ['__count']):
                counts[m.id] = c
        return [
            (0, 0, {'member_id': m.id, 'selected': True,
                    'current_count': counts.get(m.id, 0)})
            for m in taskers
        ]

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        project_id = res.get('project_id') or self.env.context.get('default_project_id')
        if project_id:
            res['project_id'] = project_id
            res['tasker_line_ids'] = self._tasker_line_commands(project_id)
        return res

    @api.onchange('project_id')
    def _onchange_project_id(self):
        """Reload the tasker roster for the chosen project — only its members can
        be allocated to."""
        cmds = [(5, 0, 0)]                     # drop the previous project's taskers
        if self.project_id:
            cmds += self._tasker_line_commands(self.project_id.id)
        self.tasker_line_ids = cmds

    # ------------------------------------------------------------------ #
    #  CSV / XLSX parsing (Persona name + optional L1 / L2)
    # ------------------------------------------------------------------ #
    # Import carries the persona's own attributes: NAME, optional DOMAIN and
    # optional SOURCE URL. Task taxonomy (L1/L2, Agent Type) is filled by the
    # tasker per allocation, never stored on the persona — so it is NOT a column.
    _COL_ALIASES = {
        'name': ['persona name', 'persona', 'name'],
        'domain': ['domain'],
        'source_url': ['source url', 'source', 'url'],
    }

    @staticmethod
    def _norm(v):
        return ' '.join((v or '').strip().lower().replace('_', ' ').replace('-', ' ').split())

    @staticmethod
    def _sanitize(name):
        return (name or '').strip().lower().replace(' ', '-')

    def _parse_rows(self):
        """Ordered [{'name', 'source_url'}] from the uploaded file (header
        optional). Taxonomy columns, if any, are ignored — taxonomy is per task."""
        try:
            text = base64.b64decode(self.csv_file).decode('utf-8-sig')
        except Exception:
            raise UserError(_(
                "The file could not be read. Please upload a valid, UTF-8 "
                "encoded CSV/text file."))
        rows = [r for r in csv.reader(io.StringIO(text)) if any((c or '').strip() for c in r)]
        if not rows:
            return []

        header = [self._norm(h) for h in rows[0]]
        colmap = {}
        for key, aliases in self._COL_ALIASES.items():
            colmap[key] = next((i for i, h in enumerate(header)
                                if h in [self._norm(a) for a in aliases]), None)
        if colmap['name'] is None:
            colmap = {'name': 0, 'domain': None, 'source_url': None}  # col 0 = name
            data = rows
        else:
            data = rows[1:]

        def cell(row, idx):
            return row[idx].strip() if idx is not None and idx < len(row) else ''

        result = []
        for row in data:
            name = cell(row, colmap['name'])
            if name:
                result.append({'name': name,
                               'domain': cell(row, colmap['domain']),
                               'source_url': cell(row, colmap['source_url'])})
        return result

    # ------------------------------------------------------------------ #
    #  Reusable business logic
    # ------------------------------------------------------------------ #
    def _unassigned_persona_domain(self):
        """Domain for personas not referenced by any Task Allocation (active
        personas only), optionally narrowed to a single domain.

        ``kensei2.persona.pt_assignment_status`` is a STORED, INDEXED compute kept in
        sync by ``_compute_tracker_allocation`` — the model already answers this
        question. The previous implementation read every allocated persona id back
        into Python and shipped it to Postgres as a literal ``NOT IN (...)``: a
        five-figure IN-list at the target scale of 50k personas, rebuilt on every
        wizard open and on every ``source_mode`` change (see
        ``_compute_unassigned_available``). This is one indexed equality instead.
        """
        domain = [('pt_assignment_status', '=', 'unassigned')]
        if self.domain_filter:
            domain.append(('pt_domain', '=', self.domain_filter))
        return domain

    @api.depends('source_mode', 'domain_filter')
    def _compute_unassigned_available(self):
        Persona = self.env['kensei2.persona']
        for wiz in self:
            wiz.unassigned_available = Persona.search_count(
                wiz._unassigned_persona_domain())

    def _persona_entries(self, create_missing):
        """De-duplicated persona list. Each entry: {sanitized, name, l1, l2,
        is_new, persona}. When ``create_missing`` is True the new personas are
        created (one batch) and every entry carries a persona record.

        Two sources: an uploaded file, or the pool of existing personas that are
        not yet assigned to any tasker (``source_mode``)."""
        if self.source_mode == 'unassigned':
            personas = self.env['kensei2.persona'].search(
                self._unassigned_persona_domain())
            if not personas:
                raise UserError(_("There are no unassigned personas to allocate."))
            return [{'sanitized': p.name, 'name': p.name, 'domain': '',
                     'source_url': '', 'is_new': False, 'persona': p}
                    for p in personas]

        rows = self._parse_rows()
        if not rows:
            raise UserError(_("The file contains no Persona names."))

        Persona = self.env['kensei2.persona']
        seen, entries = set(), []
        for r in rows:
            sanitized = self._sanitize(r['name'])
            if not sanitized or sanitized in seen:
                continue
            seen.add(sanitized)
            entries.append({'sanitized': sanitized, 'name': r['name'],
                            'domain': r.get('domain', ''),
                            'source_url': r.get('source_url', ''),
                            'is_new': True, 'persona': Persona.browse()})

        # one query resolves all existing personas (archived included -> reuse)
        existing = {
            p.name: p
            for p in Persona.with_context(active_test=False).search(
                [('name', 'in', list(seen))])
        }
        for e in entries:
            p = existing.get(e['sanitized'])
            if p:
                e['is_new'] = False
                e['persona'] = p

        if create_missing:
            # Personas are global — no project, no taxonomy. The project is applied
            # per allocation (self.project_id below); taxonomy is filled by the
            # tasker on the task. Only the optional Source URL rides along.
            new_vals = []
            for e in entries:
                if e['is_new']:
                    vals = {'name': e['name']}
                    if e.get('domain'):
                        vals['pt_domain'] = e['domain']
                    if e.get('source_url'):
                        vals['pt_source_url'] = e['source_url']
                    new_vals.append((e, vals))
            if new_vals:
                created = Persona.create([v for _e, v in new_vals])
                for (e, _v), rec in zip(new_vals, created):
                    e['persona'] = rec
        return entries

    def _selected_lines(self):
        return self.tasker_line_ids.filtered('selected')

    def _capacity(self, line):
        """Remaining capacity for a tasker line; None means unlimited."""
        if self.limit_mode == 'no_limit':
            return None
        return max(0, self.allocation_limit - line.current_count)

    def _plan(self, create_missing):
        """Shared planning used by both Preview and Start.

        Returns a dict describing what will happen. Only the counts are needed
        for the preview; ``create_missing=True`` (used by Start) also creates
        the new personas and returns the ordered persona records to allocate.
        """
        self.ensure_one()
        if not self.project_id:
            raise UserError(_("Pick a Project to allocate within."))
        if self.limit_mode == 'limited' and self.allocation_limit < 1:
            raise UserError(_("Allocation Limit must be at least 1 (or choose No Limit)."))

        lines = self._selected_lines()
        if not lines:
            raise UserError(_("Select at least one Tasker to allocate to."))

        entries = self._persona_entries(create_missing)

        # skip personas already assigned to an allocation (no duplicate personas)
        existing_ids = [e['persona'].id for e in entries if e['persona']]
        allocated_ids = set()
        if existing_ids:
            Alloc = self.env['project.tracker.allocation']
            for p, in Alloc._read_group(
                    [('persona_id', 'in', existing_ids)], ['persona_id']):
                if p:
                    allocated_ids.add(p.id)
        for e in entries:
            e['skipped'] = bool(e['persona'] and e['persona'].id in allocated_ids)

        to_allocate = [e for e in entries if not e['skipped']]

        # capacity
        caps = {ln.id: self._capacity(ln) for ln in lines}
        unlimited = any(c is None for c in caps.values())
        total_capacity = None if unlimited else sum(caps.values())

        n = len(to_allocate)
        if total_capacity is None:
            expected = n
        else:
            expected = min(n, total_capacity)

        return {
            'entries': entries,
            'to_allocate': to_allocate,
            'lines': lines,
            'caps': caps,
            'total': len(entries),
            'existing': sum(1 for e in entries if not e['is_new']),
            'new': sum(1 for e in entries if e['is_new']),
            'skipped': sum(1 for e in entries if e['skipped']),
            'selected_taskers': len(lines),
            'expected': expected,
            'unallocated': n - expected,
        }

    # ------------------------------------------------------------------ #
    #  Steps
    # ------------------------------------------------------------------ #
    def _reopen(self, name):
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name,
            'res_id': self.id, 'view_mode': 'form', 'target': 'new', 'name': name,
        }

    # ------------------------------------------------------------------ #
    #  Distribution strategies
    # ------------------------------------------------------------------ #
    def _distribute_least_loaded(self, personas, lines, caps):
        """Give each persona to the tasker with the FEWEST total allocations so
        far (existing ``current_count`` + already assigned in this run), so the
        least-loaded people fill up first and the load evens out. Respects the
        per-tasker cap (Limited mode): a tasker drops out once full. ``cap is
        None`` means No Limit.

        A min-heap keyed on the running load makes each placement O(log m).
        """
        heap = []
        for ln in lines:
            cap = caps[ln.id]
            if cap is None or cap > 0:
                # (load, name, id) is a total order — id is unique so the member
                # recordset that follows is never compared.
                heapq.heappush(
                    heap,
                    (ln.current_count, (ln.member_name or '').lower(), ln.id,
                     ln.member_id, cap))
        assignments, unallocated = [], []
        for persona in personas:
            if not heap:
                unallocated.append(persona)
                continue
            load, name, lid, member, cap = heapq.heappop(heap)
            assignments.append((member, persona))
            new_cap = None if cap is None else cap - 1
            if new_cap is None or new_cap > 0:
                heapq.heappush(heap, (load + 1, name, lid, member, new_cap))
            # else capacity exhausted -> leave the tasker out of the heap
        return assignments, unallocated

    def _distribute_round_robin(self, personas, lines, caps):
        """Even round-robin over a shuffled tasker order (used by Random). Each
        persona is O(1): assign to the tasker at the front, then either drop it
        (capacity exhausted) or rotate it to the back for an even spread."""
        ordered = list(lines)
        random.shuffle(ordered)
        queue = deque()
        for ln in ordered:
            cap = caps[ln.id]
            if cap is None or cap > 0:
                queue.append([ln.member_id, cap])
        assignments, unallocated = [], []
        for persona in personas:
            if not queue:
                unallocated.append(persona)
                continue
            member, cap = queue[0]
            assignments.append((member, persona))
            if cap is None:
                queue.rotate(-1)                 # unlimited -> keep in rotation
            elif cap - 1 > 0:
                queue[0][1] = cap - 1
                queue.rotate(-1)
            else:
                queue.popleft()                  # capacity exhausted -> drop
        return assignments, unallocated

    def action_preview(self):
        self.ensure_one()
        if self.source_mode == 'file' and not self.csv_file:
            raise UserError(_("Please upload a file with the Persona names."))
        plan = self._plan(create_missing=False)
        self.write({
            'total_personas': plan['total'],
            'existing_personas': plan['existing'],
            'new_personas': plan['new'],
            'skipped_personas': plan['skipped'],
            'selected_taskers': plan['selected_taskers'],
            'expected_assignments': plan['expected'],
            'unallocated_expected': plan['unallocated'],
            'state': 'preview',
        })
        return self._reopen(_('Allocation Summary'))

    def action_back(self):
        self.ensure_one()
        self.state = 'draft'
        return self._reopen(_('Bulk Allocation'))

    def action_start(self):
        self.ensure_one()
        if self.source_mode == 'file' and not self.csv_file:
            raise UserError(_("Please upload a file with the Persona names."))

        # _plan already created every new persona, so they are persisted in the
        # DB regardless of whether they get allocated below. We deliberately do
        # NOT raise here (which would roll the personas back) — instead anything
        # that can't be placed is reported as "unallocated".
        plan = self._plan(create_missing=True)
        lines, caps = plan['lines'], plan['caps']
        entries = plan['to_allocate']

        personas = [e['persona'] for e in entries]
        if self.allocation_method == 'random':
            random.shuffle(personas)
            assignments, unallocated = self._distribute_round_robin(
                personas, lines, caps)
        else:
            assignments, unallocated = self._distribute_least_loaded(
                personas, lines, caps)

        # single optimized batch write
        Alloc = self.env['project.tracker.allocation'].with_context(project_tracker_skip_toast=True)
        if assignments:
            Alloc.create([
                {'tasker_member_id': member.id, 'persona_id': persona.id,
                 'project_id': self.project_id.id}
                for member, persona in assignments
            ])

        self.write({
            'assigned_count': len(assignments),
            'created_count': plan['new'],
            'skipped_count': plan['skipped'],
            'unallocated_count': len(unallocated),
            'result_html': self._render_report(plan, assignments, unallocated),
            'state': 'done',
        })
        return self._reopen(_('Allocation Report'))

    # ------------------------------------------------------------------ #
    #  Report rendering
    # ------------------------------------------------------------------ #
    def _render_report(self, plan, assignments, unallocated):
        # per-tasker tally
        tally = {}
        for member, _persona in assignments:
            tally[member] = tally.get(member, 0) + 1

        def row(label, value, color):
            return ("<tr><td style='padding:4px 8px;'>%s</td>"
                    "<td style='padding:4px 8px;text-align:right;font-weight:600;"
                    "color:%s;'>%s</td></tr>" % (escape(label), color, value))

        head = ("<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
                "<tbody>")
        summary = (
            row(_("Total Personas Assigned"), len(assignments), "#10b981")
            + row(_("Newly Created Personas"), plan['new'], "#3b82f6")
            + row(_("Skipped (already allocated)"), plan['skipped'], "#6b7280")
            + row(_("Unallocated Personas"), len(unallocated),
                  "#ef4444" if unallocated else "#6b7280")
            + "</tbody></table>"
        )

        # per-tasker breakdown
        by_tasker = ""
        if tally:
            rows = "".join(
                "<tr style='border-bottom:1px solid #eee;'>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;text-align:right;'>%s</td></tr>"
                % (escape(member.member_name or member.email or ''), n)
                for member, n in sorted(tally.items(),
                                        key=lambda kv: -kv[1]))
            by_tasker = (
                "<div style='margin-top:10px;font-weight:600;'>Per-tasker</div>"
                "<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
                "<thead><tr style='text-align:left;border-bottom:2px solid #d0d0d0;'>"
                "<th style='padding:4px 8px;'>Tasker</th>"
                "<th style='padding:4px 8px;text-align:right;'>Assigned</th></tr>"
                "</thead><tbody>" + rows + "</tbody></table>")

        # unallocated reasons
        unalloc = ""
        if unallocated:
            reason = _("No remaining tasker capacity (allocation limit reached).")
            items = "".join(
                "<tr style='border-bottom:1px solid #eee;'>"
                "<td style='padding:4px 8px;'>%s</td>"
                "<td style='padding:4px 8px;color:#b45309;'>%s</td></tr>"
                % (escape(p.name), escape(reason)) for p in unallocated)
            unalloc = (
                "<div style='margin-top:10px;font-weight:600;color:#b45309;'>"
                "Unallocated personas</div>"
                "<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
                "<thead><tr style='text-align:left;border-bottom:2px solid #d0d0d0;'>"
                "<th style='padding:4px 8px;'>Persona</th>"
                "<th style='padding:4px 8px;'>Reason</th></tr></thead><tbody>"
                + items + "</tbody></table>")

        return head + summary + by_tasker + unalloc


class Kensei2TrackerBulkAllocationLine(models.TransientModel):
    _name = 'project.tracker.bulk.allocation.line'
    _description = 'Bulk Allocation — Tasker Line'
    _order = 'current_count, id'

    wizard_id = fields.Many2one(
        'project.tracker.bulk.allocation', required=True, ondelete='cascade')
    member_id = fields.Many2one(
        'project.tracker.team.member', string='Tasker', required=True,
        ondelete='cascade')
    member_name = fields.Char(related='member_id.member_name', string='Name', readonly=True)
    email = fields.Char(related='member_id.email', string='Email', readonly=True)
    selected = fields.Boolean(string='Allocate?', default=True)
    current_count = fields.Integer(string='Current Personas', readonly=True)
