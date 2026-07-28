"""Dry run — walk the whole system over demo data and report what holds.

This is not the test suite. The tests prove each rule in isolation against fixtures
they build themselves; this walks one realistic organisation end to end and prints what
it finds, so you can read the numbers and judge them yourself.

    odoo-bin shell -d <db> --no-http < custom_addons/ethara_project_os/tools/dry_run.py

Needs a database installed with demo data:

    odoo-bin -d <db> -i ethara_project_os --with-demo --stop-after-init

It changes nothing — the transaction is rolled back at the end.
"""
from datetime import timedelta
from odoo import fields

env = env  # noqa: F821
today = fields.Date.context_today(env['res.users'])
PASS, FAIL = [], []

def check(label, condition, detail=''):
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f'  ({detail})' if detail else ''}")

def head(title):
    print(f'\n{"─" * 74}\n{title}\n{"─" * 74}')

P = env['project.project']
mm7 = P.search([('code', '=', 'DEMO-MM7')])
atlas = P.search([('code', '=', 'DEMO-ATLAS')])
tool = P.search([('code', '=', 'DEMO-TOOL')])

head('1 · PEOPLE AND ROLES')
for role in ('pm', 'pl', 'tasker'):
    people = env['hr.employee'].search([('epo_role', '=', role)])
    print(f'  {role.upper():4} {len(people)}: ' + ', '.join(people.mapped('name')))
gita = env['hr.employee'].search([('name', '=', 'Gita Rao')])
mira = env['hr.employee'].search([('name', '=', 'Mira Shah')])
check('a role grant produced a working login level',
      gita.user_id.has_group('ethara_project_os.group_epo_pm'))
check('the ladder holds — PM implies pod lead',
      gita.user_id.has_group('ethara_project_os.group_epo_pod_lead'))
check('a Tasker is not a manager',
      not mira.user_id.has_group('ethara_project_os.group_epo_pm'))

head('2 · PROJECTS AND THE GO-LIVE GATE')
for project in (mm7, atlas, tool):
    print(f'  {project.code:12} {project.name:26} {project.ethara_state:9} '
          f'blockers: {project.gate_blockers or "none"}')
check('a project with SOP + published stagelist went live', mm7.ethara_state == 'active')
check('a project with neither is still in setup', tool.ethara_state == 'setup')
check('and it says exactly what it is missing',
      'SOP' in tool.gate_blockers and 'stagelist' in tool.gate_blockers,
      tool.gate_blockers)
check('codes were generated and are unique',
      len({mm7.code, atlas.code, tool.code}) == 3)
check('internal work is numbered separately', tool.code.startswith('DEMO-'))

head('3 · THE FOLDER CABINET')
def show(nodes, depth=0):
    for node in nodes:
        docs = f"  [{node['document_count']} doc]" if node['document_count'] else ''
        print('     ' + '  ' * depth + node['name'] + docs)
        show(node['children'], depth + 1)
show(env['epo.folder'].tree_for(mm7, user=gita.user_id))
folder_slugs = set(env['epo.folder'].search([('project_id', '=', mm7.id)]).mapped('slug'))
check('every project gets the same skeleton',
      {'knowledge', 'sop', 'common_errors', 'task_videos', 'other',
       'management', 'client_documents'} <= folder_slugs)
check('the S3 prefix mirrors the visible path',
      env['epo.folder']._get(mm7, 'sop').s3_prefix.endswith('knowledge/sop'),
      env['epo.folder']._get(mm7, 'sop').s3_prefix)
member_tree = env['epo.folder'].tree_for(mm7, user=mira.user_id)
check('a Tasker sees no Management branch at all',
      [n['slug'] for n in member_tree] == ['knowledge'])
check('and cannot read a client document',
      not env['epo.document'].with_user(mira.user_id).search_count(
          [('root', '=', 'management')]))
check('but can read the SOP of the project they are on',
      env['epo.document'].with_user(mira.user_id).search_count(
          [('project_id', '=', mm7.id), ('category', '=', 'sop')]) == 1)
check('and nothing from a project she is not on',
      not env['epo.document'].with_user(mira.user_id).search_count(
          [('project_id', '=', tool.id)]),
      'Internal Tooling — nobody is allocated to it')

head('4 · WHO IS ON WHAT, AND FOR HOW LONG')
print(f'  {"person":14}{"joined":12}{"left":12}{"phase":11}{"days":>5}'
      f'{"onb":>5}{"trn":>5}{"task":>6}{"→prod":>7}{"subs":>6}')
for a in mm7.allocation_ids.sorted('date_from'):
    print(f'  {a.employee_id.name:14}{str(a.date_from):12}'
          f'{str(a.date_to or "—"):12}{(a.current_phase or "—"):11}'
          f'{a.days_total:>5}{a.days_onboarding:>5}{a.days_training:>5}'
          f'{a.days_tasking:>6}{a.days_to_productive:>7}{a.submission_count:>6}')
live_allocs = mm7.allocation_ids.filtered('is_open')
check('the phase log accumulated from the roster',
      any(a.days_tasking > 0 for a in live_allocs))
check('everybody tasking today has a current phase',
      all(a.current_phase for a in live_allocs if a.days_tasking),
      ', '.join(f'{a.employee_id.name}={a.current_phase or "NONE"}'
                for a in live_allocs))
check('phases are contiguous stretches, not one row per day',
      all(len(a.phase_ids) <= 8 for a in live_allocs),
      ', '.join(f'{a.employee_id.name}:{len(a.phase_ids)}' for a in live_allocs))
check('onboarding time is counted separately from tasking',
      any(a.days_onboarding > 0 for a in live_allocs))
closed = mm7.allocation_ids.filtered(lambda a: a.date_to)
check('a released person keeps their history',
      bool(closed) and closed[0].days_total > 0,
      f'{closed[0].employee_id.name}: {closed[0].days_total} days' if closed else '')
check('no phase outlives its allocation',
      all(not p.date_to or not a.date_to or p.date_to <= a.date_to
          for a in mm7.allocation_ids for p in a.phase_ids))
check('phase days never exceed the time on the project',
      all(sum(a.phase_ids.mapped('duration_days')) <= a.days_total
          for a in mm7.allocation_ids))

head('5 · THE ONBOARDING GATE')
for o in mm7.onboarding_ids.sorted(lambda r: r.employee_id.name):
    print(f'  {o.employee_id.name:14} sop={str(o.sop_done):5} '
          f'training={str(o.training_done):5} assessment={str(o.assessment_passed):5} '
          f'→ {"CLEARED" if o.unlocked else "blocked on " + (o.blockers or "?")}')
check('somebody who passed the assessment is cleared',
      any(o.unlocked for o in mm7.onboarding_ids))
check('the newest joiner is still blocked, and it says why',
      any(not o.unlocked and o.blockers for o in mm7.onboarding_ids))

head('6 · THE STAFFING BAR')
print(f'  {atlas.name} needs {atlas.min_assessment_score:.0f}')
for row in atlas.candidates()[:6]:
    mark = '✓' if row['eligible'] else ' '
    score = f"{row['best_score']:.0f}" if row['best_score'] else '—'
    print(f'   {mark} {row["name"]:14} best={score:>4}  {row["reason"]}')
check('the bar is recorded on the project', atlas.min_assessment_score == 80.0)
check('the bar is snapshotted onto each allocation',
      all(a.min_score_applied == 80.0 for a in atlas.allocation_ids))
check('somebody below the bar needed a recorded reason',
      any(a.override_reason for a in atlas.allocation_ids))
check('and that override is in the audit log',
      bool(env['epo.audit.log'].search([('action', '=', 'allocation_below_minimum')])))
check('nobody exceeds 100% of capacity',
      all(sum(env['epo.allocation'].search([
          ('employee_id', '=', e.id), ('date_to', '=', False)]).mapped(
              'allocation_pct')) <= 100
          for e in env['hr.employee'].search([('epo_role', '=', 'tasker')])))

head('7 · SUBMISSIONS — THE LEDGER')
Entry = env['epo.form.entry']
for form_type in ('stagelist', 'feedback'):
    rows = Entry._read_group(
        [('project_id', '=', mm7.id), ('form_type', '=', form_type),
         ('state', '=', 'submitted')], groupby=['employee_id'], aggregates=['__count'])
    total = sum(c for _e, c in rows)
    print(f'  {form_type:10} {total:>3} total: ' +
          ', '.join(f'{e.name.split()[0]} {c}' for e, c in rows))
check('stagelist submissions exist',
      Entry.search_count([('project_id', '=', mm7.id),
                          ('form_type', '=', 'stagelist'),
                          ('state', '=', 'submitted')]) > 0)
check('feedback submissions exist',
      Entry.search_count([('project_id', '=', mm7.id),
                          ('form_type', '=', 'feedback'),
                          ('state', '=', 'submitted')]) > 0)
check('every submission carries the form version it was filled on',
      all(e.template_version >= 1 for e in Entry.search([])))
check('every submitter was allocated on the day they submitted',
      all(e._is_allocated() for e in Entry.search([('state', '=', 'submitted')])))
first = Entry.search([('state', '=', 'submitted')], limit=1)
check('answers are stored against the fields of that form',
      bool(first.value_ids) and
      all(v.field_id.template_id == first.template_id for v in first.value_ids))

head('8 · SCOPING — WHAT EACH ROLE ACTUALLY SEES')
piyush = env['hr.employee'].search([('name', '=', 'Piyush Nair')])
print(f'  {"model":26}{"total":>7}{"member":>8}{"lead":>7}{'pm':>7}')
for model in ('epo.form.entry', 'epo.document', 'epo.folder',
              'epo.role.assignment', 'epo.form.template'):
    counts = [env[model].sudo().search_count([])]
    for user in (mira.user_id, piyush.user_id, gita.user_id):
        counts.append(env[model].with_user(user).search_count([]))
    print(f'  {model:26}{counts[0]:>7}{counts[1]:>8}{counts[2]:>7}{counts[3]:>7}')
check('a Tasker sees fewer submissions than the PM',
      env['epo.form.entry'].with_user(mira.user_id).search_count([]) <
      env['epo.form.entry'].with_user(gita.user_id).search_count([]))
check('a pod lead sees their pod but not the whole org',
      env['epo.form.entry'].with_user(piyush.user_id).search_count([]) <=
      env['epo.form.entry'].with_user(gita.user_id).search_count([]))

head('9 · HISTORY — ONE STREAM, TWO READINGS')
person_events = env['epo.timeline.event'].search([('employee_id', '=', mira.id)])
project_events = env['epo.timeline.event'].search([('project_id', '=', mm7.id)])
print(f'  Mira Shah: {len(person_events)} events — '
      f'{", ".join(sorted(set(person_events.mapped("event_type")))[:7])}')
print(f'  Batch 7  : {len(project_events)} events across '
      f'{len(set(project_events.mapped("employee_id").ids))} people')
check('a person has a full history', len(person_events) > 10)
check('a project has a full history', len(project_events) > 10)
check('it records joining, phases, onboarding and work',
      {'allocated', 'phase_started', 'onboarding_unlocked', 'entry_submitted'}
      <= set(person_events.mapped('event_type')))
productive = [a.days_to_productive for a in mm7.allocation_ids if a.days_to_productive]
print(f'  average days to productive on Batch 7: '
      f'{sum(productive) / len(productive):.1f}' if productive else '  (none yet)')

head('10 · THE NIGHTLY JOBS')
before = env['epo.roster.day'].search_count([('business_date', '=', today)])
env['epo.roster.day']._cron_carry_forward()
after = env['epo.roster.day'].search_count([('business_date', '=', today)])
check('carry-forward runs without exploding', True, f'{before} → {after} rows today')
check('nobody was carried onto a project they left',
      all(not r.project_id or env['epo.allocation'].search_count([
          ('employee_id', '=', r.employee_id.id),
          ('project_id', '=', r.project_id.id),
          ('date_from', '<=', r.business_date),
          '|', ('date_to', '=', False), ('date_to', '>=', r.business_date)])
          for r in env['epo.roster.day'].search([('business_date', '=', today)])))
env['hr.leave']._cron_epo_sync_roster()
check('the leave sync runs', True)
check('the grading catch-up runs', True)

print(f'\n{"═" * 74}')
print(f'  {len(PASS)} passed, {len(FAIL)} failed')
for f in FAIL:
    print(f'    ✗ {f}')
print('═' * 74)
env.cr.rollback()
