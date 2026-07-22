"""Backfill calendar.event.candidate_id for legacy events.

Run via: odoo-bin shell -c odoo.conf -d <db> --no-http < scripts/backfill_candidate_id.py

Match strategy:
1. Parse event.name for 'Interview - <name>' pattern → hr.applicant.partner_name
2. Fallback: partner_ids emails → hr.applicant.email_from
"""
import re

Event = env['calendar.event'].sudo().with_context(active_test=False)
Applicant = env['hr.applicant'].sudo()

orphans = Event.search([
    ('candidate_id', '=', False),
    ('is_google_meet', '=', True),
])
print(f'Orphaned Meet events without candidate_id: {len(orphans)}')

by_title = by_email = 0
unmatched = []
for e in orphans:
    m = re.search(r'Interview[^-]*-\s*(.+?)(?:\s*[|(-]|$)', e.name or '', re.I)
    if m:
        cand = Applicant.search([('partner_name', '=ilike', m.group(1).strip())], limit=1)
        if cand:
            e.candidate_id = cand.id
            by_title += 1
            continue
    for em in (p.email for p in e.partner_ids if p.email):
        cand = Applicant.search([('email_from', '=ilike', em)], limit=1)
        if cand:
            e.candidate_id = cand.id
            by_email += 1
            break
    else:
        unmatched.append((e.id, e.name))

env.cr.commit()
print(f'  by title: {by_title}, by email: {by_email}, unmatched: {len(unmatched)}')
for e in unmatched[:10]:
    print(f'    event#{e[0]} name={e[1]!r}')
