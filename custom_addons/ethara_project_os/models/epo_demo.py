"""Demo data — a small but complete organisation.

Loaded only when the database is created with demo data. Install with
``--without-demo=all`` and none of this exists; there is no other trace of it in the
module, so removing it is deleting one folder and one manifest line.

It is built by **calling the real business logic** rather than inserting rows: projects
go through the go-live gate, people go through the onboarding gate, the roster drives
the phase log, submissions go through the admission guard. That means if any of it is
broken the demo fails loudly at install time, and it means the numbers on the demo
screens are the numbers the system actually computes.

What it sets up
---------------
* two pods with leads, one PM, six Taskers;
* **Multimango Batch 7** — live, fully staffed, ten days of roster history, submissions;
* **Atlas Annotation** — live with a minimum score of 80, so the candidate list has
  people on both sides of the bar;
* **Internal Tooling** — deliberately stuck in setup with its gate blockers showing.

Every demo record is findable: projects are coded ``DEMO-…`` and people have
``@demo.ethara`` emails, so purging by hand is a two-line search.
"""

import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

DEMO_EMAIL_DOMAIN = 'demo.ethara'
DEMO_PASSWORD = 'demo1234'


class EpoDemo(models.AbstractModel):
    _name = 'epo.demo'
    _description = 'Project OS Demo Data Builder'

    # ------------------------------------------------------------------
    @api.model
    def build(self):
        """Entry point called from demo/epo_demo.xml."""
        if self.env['project.project'].search_count([('code', 'like', 'DEMO-%'), ('is_project_os', '=', True)]):
            _logger.info('Project OS: demo data already present, skipping.')
            return True
        try:
            self._build()
        except Exception:  # noqa: BLE001
            # Demo data must never be the reason an install fails. Log the whole
            # traceback so it is obvious what broke, and let the module finish.
            _logger.exception('Project OS: demo data could not be built.')
            return False
        _logger.info('Project OS: demo data ready.')
        return True

    def _build(self):
        today = fields.Date.context_today(self)
        people = self._people()
        self._project_in_flight(people, today)
        self._project_with_a_bar(people, today)
        self._project_in_setup(people)

    # ------------------------------------------------------------------
    # people
    # ------------------------------------------------------------------
    def _person(self, name, role, pod=None, seat=None):
        login = f"{name.split()[0].lower()}@{DEMO_EMAIL_DOMAIN}"
        user = self.env['res.users'].create({
            'name': name, 'login': login, 'password': DEMO_PASSWORD})
        employee = self.env['hr.employee'].create({
            'name': name, 'user_id': user.id, 'work_email': login,
            'epo_pod_id': pod.id if pod else False, 'epo_seat_no': seat,
            'epo_job_status': 'fte'})
        self.env['epo.role.assignment'].create({
            'employee_id': employee.id, 'role': role,
            'scope_pod_id': pod.id if (role == 'pl' and pod) else False,
            'reason': 'Demo data'})
        return employee

    def _people(self):
        Pod = self.env['epo.pod']
        alpha = Pod.create({'name': 'Alpha', 'code': 'POD-A', 'floor': '3',
                            'zone': 'North', 'seat_range': 'A01-A20', 'capacity': 20})
        bravo = Pod.create({'name': 'Bravo', 'code': 'POD-B', 'floor': '3',
                            'zone': 'South', 'seat_range': 'B01-B20', 'capacity': 20})

        pm = self._person('Gita Rao', 'pm')
        lead_a = self._person('Piyush Nair', 'pl', alpha, 'A01')
        lead_b = self._person('Anjali Menon', 'pl', bravo, 'B01')
        alpha.lead_employee_id = lead_a
        bravo.lead_employee_id = lead_b

        members = [
            self._person('Mira Shah', 'tasker', alpha, 'A02'),
            self._person('Noor Iqbal', 'tasker', alpha, 'A03'),
            self._person('Ravi Kumar', 'tasker', alpha, 'A04'),
            self._person('Sana Desai', 'tasker', bravo, 'B02'),
            self._person('Tarun Bose', 'tasker', bravo, 'B03'),
            self._person('Uma Pillai', 'tasker', bravo, 'B04'),
        ]
        for member in members:
            member.parent_id = lead_a if member.epo_pod_id == alpha else lead_b
        return {'pm': pm, 'lead_a': lead_a, 'lead_b': lead_b,
                'members': members, 'alpha': alpha, 'bravo': bravo}

    # ------------------------------------------------------------------
    # content helpers
    # ------------------------------------------------------------------
    def _doc(self, project, slug, name, url, mandatory=False):
        """Demo documents are links, never uploads: the demo must work on a database
        with no S3 bucket configured, and uploads are refused without one."""
        return self.env['epo.document'].create({
            'folder_id': self.env['epo.folder']._get(project, slug).id,
            'name': name, 'doc_type': 'link', 'url': url,
            'is_mandatory': mandatory})

    def _stagelist(self, project):
        template = self.env['epo.form.template'].create({
            'project_id': project.id, 'form_type': 'stagelist',
            'name': 'Task stagelist',
            'description': 'One response per task completed on the client platform.'})
        section = self.env['epo.form.section'].create({
            'template_id': template.id, 'name': 'The task', 'sequence': 10,
            'help_text': 'Fill this after the task is done on the platform.'})
        Field = self.env['epo.form.field']
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'Task ID', 'field_type': 'short_text',
                      'is_required': True, 'sequence': 10,
                      'help_text': 'The identifier shown on the client platform.'})
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'Full task prompt', 'field_type': 'paragraph',
                      'is_required': True, 'sequence': 20})
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'Outcome', 'field_type': 'dropdown', 'is_required': True,
                      'options_json': json.dumps(['Completed', 'Blocked', 'Escalated']),
                      'sequence': 30})
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'Minutes spent', 'field_type': 'number', 'sequence': 40})
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'Notes for the reviewer', 'field_type': 'paragraph',
                      'sequence': 50})
        return template

    def _feedback_form(self, project):
        template = self.env['epo.form.template'].create({
            'project_id': project.id, 'form_type': 'feedback',
            'name': 'Client feedback',
            'description': 'Log the feedback the client left on their platform.'})
        section = self.env['epo.form.section'].create({
            'template_id': template.id, 'name': 'Client feedback', 'sequence': 10})
        Field = self.env['epo.form.field']
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'Task ID', 'field_type': 'short_text',
                      'is_required': True, 'sequence': 10})
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'Rating', 'field_type': 'radio', 'is_required': True,
                      'options_json': json.dumps(['Accepted', 'Revision asked',
                                                  'Rejected']),
                      'sequence': 20})
        Field.create({'template_id': template.id, 'section_id': section.id,
                      'name': 'What the client said', 'field_type': 'paragraph',
                      'sequence': 30})
        return template

    def _verified(self, employee, project, score, days_ago=20):
        """A Tasker read the external result and recorded it (v2 §4.6.3)."""
        when = fields.Datetime.now() - timedelta(days=days_ago)
        record = self.env['epo.onboarding'].sudo()._get(employee.id, project.id)
        record.sudo().write({
            'assessment_passed': True,
            'assessment_score': score,
            'assessment_verified_by': self.env.ref('base.user_admin').id,
            'assessment_verified_at': when,
        })
        return record

    # ------------------------------------------------------------------
    # project 1 — live, staffed, with history behind it
    # ------------------------------------------------------------------
    def _project_in_flight(self, people, today):
        project = self.env['project.project'].create({
            'name': 'Multimango Batch 7', 'code': 'DEMO-MM7',
            'ethara_project_type': 'external', 'platform': 'Multimango',
            'is_project_os': True,             'pm_id': people['pm'].id, 'date_start': today - timedelta(days=30),
            'description': 'Annotation work on the Multimango platform, batch 7.'})

        self._doc(project, 'sop', 'SOP v3 — annotation',
                  'https://docs.ethara.test/mm7/sop-v3', mandatory=True)
        self._doc(project, 'common_errors', 'Top 10 rejections this month',
                  'https://docs.ethara.test/mm7/common-errors')
        self._doc(project, 'task_videos', 'Walkthrough: a full task end to end',
                  'https://videos.ethara.test/mm7/walkthrough')
        self._doc(project, 'client_documents', 'MSA — Multimango 2026',
                  'https://docs.ethara.test/mm7/msa-2026')
        self._doc(project, 'client_documents', 'Batch 7 statement of work',
                  'https://docs.ethara.test/mm7/sow-b7')

        self.env['epo.training'].create({
            'project_id': project.id, 'name': 'Batch 7 kickoff walkthrough',
            'mode': 'recorded', 'url': 'https://videos.ethara.test/mm7/kickoff',
            'duration_mins': 45, 'is_mandatory': True,
            'trainer_id': people['pm'].id,
            'notes': 'Watch before your first task.'})

        self.env['ethara.assessment'].create({
            'project_id': project.id, 'title': 'Batch 7 induction',
            'url': 'https://forms.ethara.test/mm7-induction',
            'provider': 'Google Form', 'pass_score': 60, 'is_mandatory': True,
            'notes': 'Sit it once you have read the SOP and watched the walkthrough.'})

        self._stagelist(project).action_publish()   # ← takes the project live
        self._feedback_form(project).action_publish()

        # Four people, joined at different times so the history is not uniform.
        joiners = [(people['members'][0], 21, 88.0),
                   (people['members'][1], 14, 72.0),
                   (people['members'][2], 7, 64.0),
                   (people['members'][3], 2, None)]
        Allocation = self.env['epo.allocation']
        Roster = self.env['epo.roster.day']
        for member, days_ago, score in joiners:
            allocation = Allocation.create({
                'project_id': project.id, 'employee_id': member.id,
                'date_from': today - timedelta(days=days_ago)})
            onboarding = self.env['epo.onboarding']._get(member.id, project.id)
            if score is None:
                continue      # newest joiner: still in onboarding, nothing done yet
            # Days first, then the gate — the order it happens in real life, and the
            # order that leaves one contiguous stretch per phase rather than a row per
            # day of backfill.
            self._roster_history(Roster, member, project, allocation, today, days_ago)
            onboarding.action_mark_sop()
            onboarding.action_mark_training()
            self._verified(member, project, score, days_ago=days_ago - 1)
            self.env['epo.onboarding']._refresh(member.id, project.id)

        # Somebody who did the work and has since moved on.
        alumnus = people['members'][4]
        past = Allocation.create({
            'project_id': project.id, 'employee_id': alumnus.id,
            'date_from': today - timedelta(days=28)})
        self._roster_history(Roster, alumnus, project, past, today - timedelta(days=9), 19)
        past_onboarding = self.env['epo.onboarding']._get(alumnus.id, project.id)
        past_onboarding.action_mark_sop()
        past_onboarding.action_mark_training()
        self._verified(alumnus, project, 91.0, days_ago=27)
        self.env['epo.onboarding']._refresh(alumnus.id, project.id)
        past.write({'date_to': today - timedelta(days=9)})

        self._submissions(project, [j[0] for j in joiners[:3]], today)
        return project

    def _roster_history(self, Roster, member, project, allocation, today, days_ago):
        """Walk the person day by day, exactly as a pod lead would each morning.

        This is what fills the phase log: onboarding for the first stretch, then
        training, then tasking — with a day of leave in the middle for one person so the
        "time off the project does not count" rule is visible in the numbers.
        """
        for offset in range(days_ago, -1, -1):
            day = today - timedelta(days=offset)
            if day > today:
                continue
            elapsed = days_ago - offset
            if elapsed == 0:
                status = 'onboarding'
            elif elapsed == 1:
                status = 'training'
            elif elapsed == 2:
                status = 'assessment'
            elif offset == 4 and days_ago > 10:
                status = 'leave'          # a day away, mid-project
            else:
                status = 'tasking'
            values = {'tasking_status': status,
                      'project_id': False if status == 'leave' else project.id,
                      'present': status != 'leave'}
            try:
                with self.env.cr.savepoint():
                    Roster.upsert(member.id, day, values, source='bulk')
            except Exception as exc:  # noqa: BLE001
                _logger.info('Project OS demo: skipped roster %s for %s (%s)',
                             day, member.display_name, exc)

    def _submissions(self, project, members, today):
        Entry = self.env['epo.form.entry']
        stagelist = project.stagelist_template_id
        feedback = project.feedback_template_id
        fields_by_name = {f.name: f for f in stagelist.field_ids}
        fb_by_name = {f.name: f for f in feedback.field_ids}
        outcomes = ['Completed', 'Completed', 'Completed', 'Blocked', 'Completed']
        ratings = ['Accepted', 'Accepted', 'Revision asked']

        counter = 1000
        for index, member in enumerate(members):
            for n in range((index + 2) * 3):        # 6, 9, 12 tasks each
                counter += 1
                day = today - timedelta(days=n % 5)
                try:
                    with self.env.cr.savepoint():
                        Entry.submit_answers(
                            stagelist.id, member.id,
                            [{'field_id': fields_by_name['Task ID'].id,
                              'text': f'MM7-{counter}'},
                             {'field_id': fields_by_name['Full task prompt'].id,
                              'text': 'Label every entity in the transcript and mark '
                                      'the ones the client flagged last week.'},
                             {'field_id': fields_by_name['Outcome'].id,
                              'text': outcomes[n % len(outcomes)]},
                             {'field_id': fields_by_name['Minutes spent'].id,
                              'number': 12 + (n % 7) * 3}],
                            idempotency_key=f'demo-sl-{member.id}-{counter}',
                            business_date=day)
                except Exception as exc:  # noqa: BLE001
                    _logger.info('Project OS demo: skipped a stagelist entry (%s)', exc)

            for n in range(index + 1):
                counter += 1
                try:
                    with self.env.cr.savepoint():
                        Entry.submit_answers(
                            feedback.id, member.id,
                            [{'field_id': fb_by_name['Task ID'].id,
                              'text': f'MM7-{counter}'},
                             {'field_id': fb_by_name['Rating'].id,
                              'text': ratings[n % len(ratings)]},
                             {'field_id': fb_by_name['What the client said'].id,
                              'text': 'Client asked for tighter spans on the second '
                                      'half of the transcript.'}],
                            idempotency_key=f'demo-fb-{member.id}-{counter}',
                            business_date=today - timedelta(days=n))
                except Exception as exc:  # noqa: BLE001
                    _logger.info('Project OS demo: skipped a feedback entry (%s)', exc)

    # ------------------------------------------------------------------
    # project 2 — live, but only takes people who scored 80
    # ------------------------------------------------------------------
    def _project_with_a_bar(self, people, today):
        project = self.env['project.project'].create({
            'name': 'Atlas Annotation', 'code': 'DEMO-ATLAS',
            'ethara_project_type': 'external', 'platform': 'Atlas',
            'is_project_os': True,             'pm_id': people['pm'].id, 'min_assessment_score': 80.0,
            'date_start': today - timedelta(days=5),
            'description': 'Specialist work — only takes people who have scored 80.'})
        self._doc(project, 'sop', 'SOP — Atlas span labelling',
                  'https://docs.ethara.test/atlas/sop', mandatory=True)
        self._doc(project, 'common_errors', 'What gets rejected on Atlas',
                  'https://docs.ethara.test/atlas/rejections')
        self._doc(project, 'client_documents', 'Atlas NDA',
                  'https://docs.ethara.test/atlas/nda')
        self.env['epo.training'].create({
            'project_id': project.id, 'name': 'Atlas span labelling — live session',
            'mode': 'online', 'scheduled_at': fields.Datetime.now() + timedelta(days=2),
            'url': 'https://meet.ethara.test/atlas-intro', 'duration_mins': 60,
            'is_mandatory': True, 'trainer_id': people['pm'].id})
        self._stagelist(project).action_publish()

        # Mira cleared the bar on Batch 7 and splits her time across the two. Her
        # Batch 7 share drops first — nobody can be over 100% of capacity.
        mira = people['members'][0]
        mira.epo_allocation_ids.filtered(lambda a: not a.date_to).write(
            {'allocation_pct': 50.0})
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': mira.id,
            'date_from': today - timedelta(days=3), 'allocation_pct': 50.0,
            'note': 'Split with Batch 7 while Atlas ramps up.'})
        # Uma has no score at all, so she needs a deliberate decision.
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': people['members'][5].id,
            'date_from': today - timedelta(days=1),
            'override_reason': 'First Atlas cohort — nobody has sat this paper yet.'})
        return project

    # ------------------------------------------------------------------
    # project 3 — deliberately stuck before the gate
    # ------------------------------------------------------------------
    def _project_in_setup(self, people):
        project = self.env['project.project'].create({
            'name': 'Internal Tooling Revamp', 'code': 'DEMO-TOOL',
            'ethara_project_type': 'internal', 'platform': 'Internal',
            'is_project_os': True,
            'pm_id': people['pm'].id,
            'description': 'Left in setup on purpose: it shows what the go-live gate '
                           'blocks on before a project can take anybody.'})
        self._doc(project, 'task_videos', 'Old walkthrough (pre-revamp)',
                  'https://videos.ethara.test/tooling/old')
        return project
