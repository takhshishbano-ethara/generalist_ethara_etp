# -*- coding: utf-8 -*-
import base64
import csv
import io
import random

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class KenseiTrackerPersonaAssign(models.TransientModel):
    """Bulk-assign personas (from the existing kensei.persona master) to
    Task Allocations.

    The uploaded file is a list of Persona names. Names are validated against
    the master; personas are then randomly distributed to the chosen taskers,
    with no persona reused within a batch until the list is exhausted.
    """
    _name = 'kensei.tracker.persona.assign'
    _description = 'Kensei Tracker — Bulk Persona Assignment'

    csv_file = fields.Binary(string='Persona Names File', required=True)
    filename = fields.Char(string='Filename')
    allocation_ids = fields.Many2many(
        'kensei.tracker.allocation', string='Taskers',
        help='Task Allocations to assign personas to.',
    )
    only_without_persona = fields.Boolean(
        string='Only taskers without a Persona', default=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        if ctx.get('active_model') == 'kensei.tracker.allocation' and ctx.get('active_ids'):
            res['allocation_ids'] = [(6, 0, ctx['active_ids'])]
        return res

    # accepted header spellings (normalised) -> logical column
    _COL_ALIASES = {
        'name': ['persona name', 'persona', 'name'],
        'l1': ['l1', 'l1 category', 'l1category'],
        'l2': ['l2', 'l2 category', 'l2category'],
    }

    @staticmethod
    def _norm(v):
        return ' '.join((v or '').strip().lower().replace('_', ' ').replace('-', ' ').split())

    def _parse_rows(self):
        """Return ordered [{'name', 'l1', 'l2'}] from the uploaded file.

        Supports an optional header row with L1 / L2 columns; without a header
        the first column is treated as the Persona name.
        """
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
            # no recognisable header -> first column is the name
            colmap = {'name': 0, 'l1': None, 'l2': None}
            data = rows
        else:
            data = rows[1:]

        def cell(row, idx):
            return row[idx].strip() if idx is not None and idx < len(row) else ''

        result = []
        for row in data:
            name = cell(row, colmap['name'])
            if name:
                result.append({
                    'name': name,
                    'l1': cell(row, colmap['l1']),
                    'l2': cell(row, colmap['l2']),
                })
        return result

    def action_assign(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Please upload a file with the Persona names."))

        rows = self._parse_rows()
        if not rows:
            raise UserError(_("The file contains no Persona names."))

        # ---- find or create each persona in the master ----
        Persona = self.env['kensei.persona']
        pool, created = [], 0
        for r in rows:
            sanitized = r['name'].strip().lower().replace(' ', '-')
            persona = Persona.search([('name', '=', sanitized)], limit=1)
            if not persona:
                vals = {'name': r['name']}
                if r['l1']:
                    vals['l1_category'] = r['l1']
                if r['l2']:
                    vals['l2_category'] = r['l2']
                persona = Persona.create(vals)
                created += 1
            else:
                # fill L1/L2 from the file only when currently empty
                upd = {}
                if r['l1'] and not persona.l1_category:
                    upd['l1_category'] = r['l1']
                if r['l2'] and not persona.l2_category:
                    upd['l2_category'] = r['l2']
                if upd:
                    persona.write(upd)
            pool.append(persona)

        # ---- unique persona pool ----
        unique = list({p.id: p for p in pool}.values())
        if not unique:
            raise UserError(_("No valid personas were found in the file."))

        # ---- targets ----
        targets = self.allocation_ids
        if self.only_without_persona:
            targets = targets.filtered(lambda a: not a.persona_id)
        if not targets:
            raise UserError(_(
                "No taskers to assign. Select Task Allocations to assign to "
                "(or untick 'Only taskers without a Persona')."))

        # ---- random assignment, unique until the pool is exhausted ----
        bag = list(unique)
        random.shuffle(bag)
        reused = False
        assigned = 0
        for alloc in targets:
            if not bag:
                bag = list(unique)
                random.shuffle(bag)
                reused = True
            alloc.persona_id = bag.pop().id
            assigned += 1

        msg = _("Assigned personas to %(count)s tasker(s) from %(pool)s persona(s).",
                count=assigned, pool=len(unique))
        if created:
            msg += _("\n%s new persona(s) were created in the master.", created)
        if reused:
            msg += _("\n\nThere were more taskers than personas, so some "
                     "personas were reused after the list was exhausted.")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Persona Assignment"),
                'message': msg,
                'type': 'warning' if reused else 'success',
                'sticky': reused,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
