"""Regression suite for Ethara Project OS.

These are the invariants the system is *for*. Each test names a way the operation can
go wrong in real use, not a method signature — if one of these fails, somebody's day
gets worse, not just a build.

Run with:
    odoo-bin -d <db> -u ethara_project_os --test-enable --test-tags /ethara_project_os
"""

import base64
import json
from datetime import timedelta

import psycopg2

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class ProjectOsCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.context_today(cls.env['res.users'])
        cls.pod = cls.env['epo.pod'].create({
            'name': 'Alpha', 'code': 'POD-TEST-A', 'capacity': 20})
        cls.user_gpm, cls.emp_gpm = cls._person('Gita GPM', 'gpm@test.epo')
        cls.user_pl, cls.emp_pl = cls._person('Piyush PL', 'pl@test.epo')
        cls.user_pm, cls.emp_pm = cls._person('Mira PM', 'pm@test.epo')
        cls.user_pm2, cls.emp_pm2 = cls._person('Noor PM', 'pm2@test.epo')
        cls.pod.lead_employee_id = cls.emp_pl

        Role = cls.env['epo.role.assignment']
        Role.create({'employee_id': cls.emp_gpm.id, 'role': 'gpm'})
        Role.create({'employee_id': cls.emp_pl.id, 'role': 'pl',
                     'scope_pod_id': cls.pod.id})
        Role.create({'employee_id': cls.emp_pm.id, 'role': 'pm'})
        Role.create({'employee_id': cls.emp_pm2.id, 'role': 'pm'})

    @classmethod
    def _person(cls, name, login):
        user = cls.env['res.users'].create({'name': name, 'login': login})
        employee = cls.env['hr.employee'].create({
            'name': name, 'user_id': user.id, 'epo_pod_id': cls.pod.id,
            'work_email': login})
        return user, employee

    def _live_project(self, name='Test Project'):
        """A project that has passed the go-live gate: SOP + published stagelist."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': name})
        self.env['epo.document'].create({
            'folder_id': self.env['epo.folder']._get(project, 'sop').id,
            'name': 'SOP', 'doc_type': 'link', 'url': 'https://docs.test/sop'})
        template = self.env['epo.form.template'].create({
            'project_id': project.id, 'form_type': 'stagelist', 'name': 'Stagelist'})
        section = self.env['epo.form.section'].create({
            'template_id': template.id, 'name': 'Task'})
        field = self.env['epo.form.field'].create({
            'template_id': template.id, 'section_id': section.id, 'name': 'Task ID',
            'field_type': 'short_text', 'is_required': True})
        template.action_publish()
        return project, template, field

    def _cleared(self, project, employee):
        """Push somebody through onboarding so they may task."""
        onboarding = self.env['epo.onboarding']._get(employee.id, project.id)
        onboarding.action_mark_sop()
        onboarding.action_mark_training()
        return onboarding


@tagged('post_install', '-at_install')
class TestRoles(ProjectOsCommon):

    def test_grant_drives_the_login_level(self):
        """A role grant is the only way in — and it must actually open the door."""
        self.assertEqual(self.emp_gpm.epo_role, 'gpm')
        self.assertTrue(self.user_gpm.has_group('ethara_project_os.group_epo_manager'))
        self.assertEqual(self.user_gpm._epo_role(), 'gpm')

    def test_ladder_implies_lower_roles(self):
        """A GPM can do everything a Pod Lead can, without a second grant."""
        self.assertTrue(self.user_gpm.has_group('ethara_project_os.group_epo_pod_lead'))
        self.assertTrue(self.user_gpm.has_group('ethara_project_os.group_epo_member'))

    def test_revoking_is_a_date_not_a_delete(self):
        """Access ends, the record of having had it does not."""
        grant = self.emp_pm.epo_role_assignment_ids[0]
        grant.action_revoke()
        self.assertTrue(grant.exists())
        self.assertTrue(grant.date_to)

    def test_same_role_twice_over_overlapping_dates(self):
        """Ambiguous grants make "when was this given?" unanswerable."""
        with self.assertRaises(psycopg2.IntegrityError), self.cr.savepoint():
            self.env['epo.role.assignment'].create({
                'employee_id': self.emp_pm.id, 'role': 'pm'})

    def test_scope_by_role(self):
        """A pod member sees themselves; a lead sees their pod; a GPM sees everyone."""
        self.assertEqual(self.user_pm._epo_scope_employee_ids(), self.emp_pm.ids)
        self.assertIn(self.emp_pm.id, self.user_pl._epo_scope_employee_ids())
        self.assertIn(self.emp_pm.id, self.user_gpm._epo_scope_employee_ids())


@tagged('post_install', '-at_install')
class TestKickoffGate(ProjectOsCommon):

    def test_no_sop_no_go_live(self):
        """People cannot be sent to task on a project with nothing to read."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Ungated'})
        with self.assertRaises(UserError):
            project.action_activate()
        self.assertEqual(project.ethara_state, 'setup')

    def test_publishing_the_stagelist_takes_it_live(self):
        project, _template, _field = self._live_project('Auto Live')
        self.assertEqual(project.ethara_state, 'active')
        self.assertTrue(project.has_sop and project.has_stagelist)

    def test_removing_the_last_sop_of_a_live_project(self):
        """Deleting it would silently un-gate everybody already tasking."""
        project, _t, _f = self._live_project('Keeps Its SOP')
        sop = project.document_ids.filtered(lambda d: d.category == 'sop')
        with self.assertRaises(UserError):
            sop.unlink()

    def test_knowledge_link_must_be_http(self):
        """A javascript: URL rendered into a GPM's browser is an escalation."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Link Check'})
        with self.assertRaises(ValidationError):
            self.env['epo.document'].create({
                'folder_id': self.env['epo.folder']._get(project, 'other').id,
                'name': 'x', 'doc_type': 'link', 'url': 'javascript:alert(1)'})

    def test_archiving_with_people_still_on_it(self):
        project, _t, _f = self._live_project('Busy')
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        with self.assertRaises(UserError):
            project.action_archive_project()


@tagged('post_install', '-at_install')
class TestFormEngine(ProjectOsCommon):

    def test_published_form_is_immutable(self):
        """The prototype's worst defect: editing a live form rewrote submitted answers."""
        _project, template, field = self._live_project('Frozen')
        with self.assertRaises(UserError):
            field.write({'name': 'Renamed'})
        with self.assertRaises(UserError):
            template.section_ids.unlink()

    def test_new_version_leaves_the_old_one_alone(self):
        project, template, field = self._live_project('Versioned')
        action = template.action_new_version()
        clone = self.env['epo.form.template'].browse(action['res_id'])
        self.assertEqual(clone.state, 'draft')
        self.assertEqual(clone.version, template.version + 1)
        self.assertEqual(template.state, 'published')
        self.assertTrue(field.exists())

    def test_choice_field_needs_choices(self):
        """A dropdown with no options is an unfillable required field."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Choices'})
        template = self.env['epo.form.template'].create({
            'project_id': project.id, 'name': 'Draft'})
        with self.assertRaises(ValidationError):
            self.env['epo.form.field'].create({
                'template_id': template.id, 'name': 'Empty',
                'field_type': 'dropdown', 'options_json': '[]'})

    def test_publishing_an_empty_form(self):
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Empty Form'})
        template = self.env['epo.form.template'].create({
            'project_id': project.id, 'name': 'Nothing'})
        with self.assertRaises(UserError):
            template.action_publish()


@tagged('post_install', '-at_install')
class TestSubmissionGuard(ProjectOsCommon):

    def setUp(self):
        super().setUp()
        self.project, self.template, self.field = self._live_project('Guarded')
        self.allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})

    def test_cannot_submit_before_onboarding(self):
        with self.assertRaises(ValidationError):
            self.env['epo.form.entry'].submit_answers(
                self.template.id, self.emp_pm.id,
                [{'field_id': self.field.id, 'text': 'T-1'}])

    def test_cannot_submit_without_being_allocated(self):
        """Otherwise anybody with a template id can post against any project."""
        with self.assertRaises(ValidationError):
            self.env['epo.form.entry'].submit_answers(
                self.template.id, self.emp_pm2.id,
                [{'field_id': self.field.id, 'text': 'T-1'}])

    def test_required_field_blank(self):
        self._cleared(self.project, self.emp_pm)
        with self.assertRaises(UserError):
            self.env['epo.form.entry'].submit_answers(
                self.template.id, self.emp_pm.id, [])

    def test_retried_post_is_idempotent(self):
        """A flaky network must not inflate every count."""
        self._cleared(self.project, self.emp_pm)
        first = self.env['epo.form.entry'].submit_answers(
            self.template.id, self.emp_pm.id,
            [{'field_id': self.field.id, 'text': 'T-1'}], idempotency_key='k')
        second = self.env['epo.form.entry'].submit_answers(
            self.template.id, self.emp_pm.id,
            [{'field_id': self.field.id, 'text': 'T-1'}], idempotency_key='k')
        self.assertEqual(first, second)

    def test_submitted_entry_is_evidence(self):
        self._cleared(self.project, self.emp_pm)
        entry = self.env['epo.form.entry'].submit_answers(
            self.template.id, self.emp_pm.id,
            [{'field_id': self.field.id, 'text': 'T-1'}])
        with self.assertRaises(UserError):
            entry.write({'employee_id': self.emp_pm2.id})
        with self.assertRaises(UserError):
            entry.unlink()

    def test_choice_answer_must_be_a_declared_choice(self):
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Options'})
        template = self.env['epo.form.template'].create({
            'project_id': project.id, 'name': 'Draft'})
        choice = self.env['epo.form.field'].create({
            'template_id': template.id, 'name': 'Outcome', 'field_type': 'radio',
            'options_json': json.dumps(['Pass', 'Fail'])})
        self.env['epo.document'].create({
            'folder_id': self.env['epo.folder']._get(project, 'sop').id,
            'name': 'SOP', 'doc_type': 'link', 'url': 'https://docs.test/sop'})
        template.action_publish()
        # emp_pm is already on this class's project at 100%; use a second person so the
        # capacity guard is not what this test ends up exercising.
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm2.id})
        self._cleared(project, self.emp_pm2)
        entry = self.env['epo.form.entry'].create({
            'template_id': template.id, 'employee_id': self.emp_pm2.id})
        with self.assertRaises(ValidationError):
            self.env['epo.form.value'].create({
                'entry_id': entry.id, 'field_id': choice.id, 'value_text': 'Maybe'})


@tagged('post_install', '-at_install')
class TestAllocationAndPhases(ProjectOsCommon):

    def setUp(self):
        super().setUp()
        self.project, self.template, self.field = self._live_project('Staffed')

    def test_joining_opens_onboarding(self):
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self.assertEqual(allocation.current_phase, 'onboarding')
        self.assertFalse(allocation.onboarding_id.unlocked)

    def test_clearing_onboarding_moves_into_tasking(self):
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self._cleared(self.project, self.emp_pm)
        allocation.invalidate_recordset()
        self.assertEqual(allocation.current_phase, 'tasking')

    def test_double_allocation_over_overlapping_dates(self):
        """The classic double-count in every headcount report."""
        self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        with self.assertRaises(psycopg2.IntegrityError), self.cr.savepoint():
            self.env['epo.allocation'].create({
                'project_id': self.project.id, 'employee_id': self.emp_pm.id})

    def test_capacity_over_100_percent(self):
        """Somebody promised to two places at once."""
        other, _t, _f = self._live_project('Other')
        self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'allocation_pct': 60})
        with self.assertRaises(ValidationError):
            self.env['epo.allocation'].create({
                'project_id': other.id, 'employee_id': self.emp_pm.id,
                'allocation_pct': 60})

    def test_cannot_allocate_to_a_project_in_setup(self):
        draft = self.env['project.project'].create({'is_project_os': True, 'name': 'Not Ready'})
        with self.assertRaises(ValidationError):
            self.env['epo.allocation'].create({
                'project_id': draft.id, 'employee_id': self.emp_pm.id})

    def test_roster_accumulates_time_per_phase(self):
        """The whole point: nobody types the day counts in, they accumulate."""
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=6)})
        self._cleared(self.project, self.emp_pm)
        Roster = self.env['epo.roster.day']
        Roster.upsert(self.emp_pm.id, self.today - timedelta(days=2),
                      {'tasking_status': 'training', 'project_id': self.project.id})
        Roster.upsert(self.emp_pm.id, self.today,
                      {'tasking_status': 'tasking', 'project_id': self.project.id})
        allocation.invalidate_recordset()
        self.assertGreaterEqual(allocation.days_training, 1)
        self.assertGreaterEqual(allocation.days_tasking, 1)
        self.assertGreaterEqual(allocation.days_total, 7)

    def test_backfilling_a_past_day_does_not_rewrite_today(self):
        """A lead correcting last Tuesday must not move the phase they are in now."""
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=6)})
        self._cleared(self.project, self.emp_pm)
        allocation.invalidate_recordset()
        self.assertEqual(allocation.current_phase, 'tasking')
        self.env['epo.roster.day'].upsert(
            self.emp_pm.id, self.today - timedelta(days=3),
            {'tasking_status': 'training', 'project_id': self.project.id})
        allocation.invalidate_recordset()
        self.assertEqual(allocation.current_phase, 'tasking')
        self.assertGreaterEqual(allocation.days_training, 1)

    def test_release_keeps_everything(self):
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self._cleared(self.project, self.emp_pm)
        self.env['epo.form.entry'].submit_answers(
            self.template.id, self.emp_pm.id,
            [{'field_id': self.field.id, 'text': 'T-1'}])
        allocation.action_release()
        allocation.invalidate_recordset()
        self.assertFalse(allocation.is_open)
        self.assertFalse(allocation.phase_ids.filtered(lambda p: not p.date_to))
        self.assertEqual(allocation.submission_count, 1)


@tagged('post_install', '-at_install')
class TestRoster(ProjectOsCommon):

    def setUp(self):
        super().setUp()
        self.project, self.template, self.field = self._live_project('Rostered')
        self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})

    def test_one_row_per_person_per_day(self):
        Roster = self.env['epo.roster.day']
        first = Roster.upsert(self.emp_pm.id, self.today, {'tasking_status': 'onboarding'})
        second = Roster.upsert(self.emp_pm.id, self.today, {'tasking_status': 'training'})
        self.assertEqual(first, second)
        self.assertEqual(second.tasking_status, 'training')

    def test_unable_to_task_needs_a_reason(self):
        """An unexplained roster hole is unauditable."""
        with self.assertRaises(psycopg2.IntegrityError), self.cr.savepoint():
            self.env['epo.roster.day'].upsert(
                self.emp_pm.id, self.today, {'tasking_status': 'unable_to_task'})

    def test_no_rostering_beyond_tomorrow(self):
        """Planning is the allocation's job; the roster records what is happening."""
        with self.assertRaises(ValidationError):
            self.env['epo.roster.day'].upsert(
                self.emp_pm.id, self.today + timedelta(days=10),
                {'tasking_status': 'tasking', 'project_id': self.project.id})

    def test_no_rostering_onto_an_unallocated_project(self):
        """This is how phantom headcount gets into a delivery report."""
        with self.assertRaises(ValidationError):
            self.env['epo.roster.day'].upsert(
                self.emp_pm2.id, self.today,
                {'tasking_status': 'tasking', 'project_id': self.project.id})

    def test_locked_day_refuses_edits(self):
        row = self.env['epo.roster.day'].upsert(
            self.emp_pm.id, self.today, {'tasking_status': 'onboarding'})
        row.locked = True
        with self.assertRaises(UserError):
            row.write({'tasking_status': 'tasking'})


@tagged('post_install', '-at_install')
class TestHistory(ProjectOsCommon):

    def test_one_stream_two_readings(self):
        project, template, field = self._live_project('Historic')
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        self._cleared(project, self.emp_pm)
        self.env['epo.form.entry'].submit_answers(
            template.id, self.emp_pm.id, [{'field_id': field.id, 'text': 'T-1'}])

        Timeline = self.env['epo.timeline.event']
        by_person = Timeline.search([('employee_id', '=', self.emp_pm.id)])
        by_project = Timeline.search([('project_id', '=', project.id)])
        self.assertTrue(by_person)
        self.assertTrue(by_project)
        kinds = set(by_person.mapped('event_type'))
        self.assertLessEqual(
            {'allocated', 'phase_started', 'onboarding_unlocked', 'entry_submitted'},
            kinds)

    def test_timeline_is_append_only(self):
        project, _t, _f = self._live_project('Append Only')
        event = self.env['epo.timeline.event'].search(
            [('project_id', '=', project.id)], limit=1)
        with self.assertRaises(UserError):
            event.write({'summary': 'rewritten'})
        with self.assertRaises(UserError):
            event.unlink()


@tagged('post_install', '-at_install')
class TestAssessment(ProjectOsCommon):

    def test_no_question_bank_exists_here(self):
        """v2 4.6.2: the engine is gone, so there is no answer key to leak.

        Stronger than the old "the key is stripped on import" test, because nothing is
        imported at all — if either model ever comes back, this fails.
        """
        for gone in ('epo.assessment.link', 'epo.assessment.result',
                     'ethara.assessment.question', 'ethara.assessment.attempt'):
            self.assertNotIn(gone, self.env,
                             f'{gone} must not exist: v2 4.6.2 drops the quiz engine.')
        # What replaces it stores a link and nothing resembling a question.
        stored = set(self.env['ethara.assessment']._fields)
        for leaky in ('question_ids', 'options', 'correct_index', 'answer_key',
                      'attempt_ids', 'snapshot_json'):
            self.assertNotIn(leaky, stored)

    def test_mandatory_assessment_blocks_the_gate(self):
        project, _t, _f = self._live_project('Assessed')
        self.env['ethara.assessment'].create({'project_id': project.id, 'title': 'Paper', 'url': 'https://forms.test/p', 'pass_score': 60})
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        onboarding = self._cleared(project, self.emp_pm)
        self.assertFalse(onboarding.unlocked)
        self.assertIn('Assessment', onboarding.blockers)

    def test_a_stage_with_no_content_auto_passes(self):
        """The gate must never block on a stage the GPM never set up."""
        project, _t, _f = self._live_project('No Extras')
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        onboarding = self.env['epo.onboarding']._get(self.emp_pm.id, project.id)
        onboarding.action_mark_sop()
        self.assertTrue(onboarding.unlocked)

    def test_waiver_needs_a_reason_and_is_audited(self):
        project, _t, _f = self._live_project('Waived')
        self.env['ethara.assessment'].create({'project_id': project.id, 'title': 'Paper', 'url': 'https://forms.test/p', 'pass_score': 60})
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        onboarding = self.env['epo.onboarding']._get(self.emp_pm.id, project.id)
        as_gpm = onboarding.with_user(self.user_gpm)
        as_pm = onboarding.with_user(self.user_pm)
        with self.assertRaises(UserError):
            # No reason given.
            as_gpm.action_waive()
        with self.assertRaises(UserError):
            # A pod member cannot clear their own gate.
            as_pm.with_context(epo_reason='I am ready, honestly').action_waive()
        onboarding.with_context(epo_reason='Client deadline, senior hire').action_waive()
        self.assertTrue(onboarding.unlocked)
        self.assertTrue(self.env['epo.audit.log'].search([
            ('action', '=', 'onboarding_waived'), ('res_id', '=', onboarding.id)]))


@tagged('post_install', '-at_install')
class TestProjectCode(ProjectOsCommon):

    def test_code_is_generated_and_unique(self):
        """The code goes on the delivery report and in the channel name. Two GPMs
        creating projects on the same morning must not be able to collide."""
        first = self.env['project.project'].create({'is_project_os': True, 'name': 'Coded One'})
        second = self.env['project.project'].create({'is_project_os': True, 'name': 'Coded Two'})
        self.assertTrue(first.code)
        self.assertNotEqual(first.code, second.code)
        self.assertTrue(first.code.startswith('EXT-'))

    def test_every_ethara_project_gets_a_code(self):
        external = self.env['project.project'].create({'is_project_os': True, 'name': 'Client Work'})
        internal = self.env['project.project'].create(
            {'is_project_os': True, 'name': 'Tooling', 'ethara_project_type': 'internal'})
        # v2 §4.2: one EPR/YYYY/#### series for every Ethara project.
        self.assertTrue(external.code.startswith('EPR/'))
        self.assertTrue(internal.code.startswith('EPR/'))
        self.assertNotEqual(external.code, internal.code)

    def test_a_supplied_code_is_normalised(self):
        """A client-mandated code is honoured, but stored the way it will be read."""
        project = self.env['project.project'].create(
            {'is_project_os': True, 'name': 'Mandated', 'code': '  mm 2041 '})
        self.assertEqual(project.code, 'MM-2041')

    def test_duplicate_code_is_refused(self):
        self.env['project.project'].create({'is_project_os': True, 'name': 'First', 'code': 'DUP-1'})
        with self.assertRaises(psycopg2.IntegrityError), self.cr.savepoint():
            self.env['project.project'].create({'is_project_os': True, 'name': 'Second', 'code': 'dup-1'})

    def test_a_project_cannot_lose_its_code(self):
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Keeps Code'})
        with self.assertRaises(UserError):
            project.write({'code': ''})


@tagged('post_install', '-at_install')
class TestFolders(ProjectOsCommon):

    def test_every_project_gets_the_same_cabinet(self):
        """A GPM must never face an empty screen, and a pod member must always know
        where to look."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Filed'})
        Folder = self.env['epo.folder']
        for slug in ('knowledge', 'sop', 'common_errors', 'task_videos', 'other',
                     'management', 'client_documents'):
            folder = Folder._get(project, slug)
            self.assertTrue(folder, f'missing system folder {slug}')
            self.assertTrue(folder.is_system)
        self.assertEqual(Folder._get(project, 'sop').root, 'knowledge')
        self.assertEqual(Folder._get(project, 'client_documents').root, 'management')

    def test_system_folders_cannot_be_renamed_or_deleted(self):
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Fixed Shape'})
        sop = self.env['epo.folder']._get(project, 'sop')
        with self.assertRaises(UserError):
            sop.write({'name': 'Standard Operating Procedure'})
        with self.assertRaises(UserError):
            sop.unlink()

    def test_management_nests_freely(self):
        """One engagement has a single MSA, the next has forty files across six phases."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Nested'})
        client_docs = self.env['epo.folder']._get(project, 'client_documents')
        phase = self.env['epo.folder'].create({
            'project_id': project.id, 'parent_id': client_docs.id,
            'root': 'management', 'name': 'Phase 1'})
        deeper = self.env['epo.folder'].create({
            'project_id': project.id, 'parent_id': phase.id,
            'root': 'management', 'name': 'Sign-offs'})
        self.assertEqual(
            deeper.complete_name, 'Management / Client Documents / Phase 1 / Sign-offs')

    def test_knowledge_keeps_its_fixed_shape(self):
        """"Read the SOP" has to mean the same thing on every project."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Flat Knowledge'})
        sop = self.env['epo.folder']._get(project, 'sop')
        with self.assertRaises(ValidationError):
            self.env['epo.folder'].create({
                'project_id': project.id, 'parent_id': sop.id,
                'root': 'knowledge', 'name': 'Old versions'})

    def test_deleting_a_folder_never_takes_files_with_it(self):
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Careful Delete'})
        client_docs = self.env['epo.folder']._get(project, 'client_documents')
        folder = self.env['epo.folder'].create({
            'project_id': project.id, 'parent_id': client_docs.id,
            'root': 'management', 'name': 'Contracts'})
        self.env['epo.document'].create({
            'folder_id': folder.id, 'name': 'MSA',
            'doc_type': 'link', 'url': 'https://docs.test/msa'})
        with self.assertRaises(UserError):
            folder.unlink()

    def test_folder_cannot_cross_projects(self):
        one = self.env['project.project'].create({'is_project_os': True, 'name': 'Project One'})
        two = self.env['project.project'].create({'is_project_os': True, 'name': 'Project Two'})
        with self.assertRaises(ValidationError):
            self.env['epo.folder'].create({
                'project_id': one.id,
                'parent_id': self.env['epo.folder']._get(two, 'client_documents').id,
                'root': 'management', 'name': 'Sneaky'})

    def test_s3_prefix_mirrors_the_visible_path(self):
        """An operator browsing the bucket should see the same shape as a GPM browsing
        the UI."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Bucketed', 'code': 'BKT-1'})
        sop = self.env['epo.folder']._get(project, 'sop')
        self.assertEqual(sop.s3_prefix, 'project-os/bkt-1/knowledge/sop')

    def test_rebuilding_the_skeleton_is_idempotent(self):
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Rebuilt'})
        before = self.env['epo.folder'].search_count([('project_id', '=', project.id)])
        project.action_rebuild_folders()
        after = self.env['epo.folder'].search_count([('project_id', '=', project.id)])
        self.assertEqual(before, after)


@tagged('post_install', '-at_install')
class TestDocumentAccess(ProjectOsCommon):

    def test_management_is_invisible_to_a_pod_member(self):
        """Client contracts are not a pod member's business, and the honest way to say
        so is to leave them out of the tree — enforced by record rule, not by the API."""
        project, _t, _f = self._live_project('Confidential')
        client_docs = self.env['epo.folder']._get(project, 'client_documents')
        self.env['epo.document'].create({
            'folder_id': client_docs.id, 'name': 'MSA',
            'doc_type': 'link', 'url': 'https://docs.test/msa'})

        as_member = self.env['epo.document'].with_user(self.user_pm)
        self.assertFalse(as_member.search([('name', '=', 'MSA')]))
        as_lead = self.env['epo.document'].with_user(self.user_pl)
        self.assertFalse(as_lead.search([('name', '=', 'MSA')]))
        as_gpm = self.env['epo.document'].with_user(self.user_gpm)
        self.assertTrue(as_gpm.search([('name', '=', 'MSA')]))

    def test_knowledge_is_visible_to_everyone_on_the_project(self):
        """On the project — the allocation is what grants it.

        This test used to pass without allocating anybody, which is how the
        cross-project leak survived: it was asserting that Knowledge is world-readable.
        """
        project, _t, _f = self._live_project('Readable')
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        as_member = self.env['epo.document'].with_user(self.user_pm)
        self.assertTrue(as_member.search([
            ('project_id', '=', project.id), ('category', '=', 'sop')]))

    def test_folder_tree_hides_management_from_a_pod_member(self):
        project, _t, _f = self._live_project('Tree Scoped')
        tree = self.env['epo.folder'].tree_for(project, user=self.user_pm)
        self.assertEqual([node['slug'] for node in tree], ['knowledge'])
        full = self.env['epo.folder'].tree_for(project, user=self.user_gpm)
        self.assertEqual(sorted(node['slug'] for node in full),
                         ['knowledge', 'management'])

    def test_upload_is_refused_without_a_bucket(self):
        """The folders are an S3 layout: a file is in the bucket or it does not exist.

        Falling back to an Odoo attachment would put two documents in the same folder
        in two different places, and "where is this file?" would stop having one answer.
        """
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'No Bucket'})
        self.env['ir.config_parameter'].sudo().set_param('epo.s3.connector_id', '')
        self.env['s3.connector'].search([]).unlink()
        with self.assertRaises(UserError) as caught:
            self.env['epo.document'].create({
                'folder_id': self.env['epo.folder']._get(project, 'sop').id,
                'name': 'SOP', 'doc_type': 'file', 'file_name': 'sop.txt',
                'file_data': base64.b64encode(b'standard operating procedure'),
            })
        self.assertIn('bucket', str(caught.exception).lower())
        # Nothing was half-saved on the way out.
        self.assertFalse(self.env['epo.document'].search([
            ('project_id', '=', project.id), ('name', '=', 'SOP')]))

    def test_a_link_needs_no_bucket(self):
        """Links store nothing, so they keep working with no storage configured."""
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Links Only'})
        self.env['ir.config_parameter'].sudo().set_param('epo.s3.connector_id', '')
        self.env['s3.connector'].search([]).unlink()
        document = self.env['epo.document'].create({
            'folder_id': self.env['epo.folder']._get(project, 'sop').id,
            'name': 'SOP', 'doc_type': 'link', 'url': 'https://docs.test/sop'})
        self.assertEqual(document.storage, 'link')
        self.assertEqual(document.download_target()['kind'], 'link')

    def test_a_file_document_needs_a_file(self):
        project = self.env['project.project'].create({'is_project_os': True, 'name': 'Empty Doc'})
        with self.assertRaises(ValidationError):
            self.env['epo.document'].create({
                'folder_id': self.env['epo.folder']._get(project, 'other').id,
                'name': 'Nothing', 'doc_type': 'file'})


@tagged('post_install', '-at_install')
class TestCrossProjectIsolation(ProjectOsCommon):
    """A pod member must not be able to read a project they were never put on.

    Every one of these was a real leak found by auditing what the ORM actually returns
    rather than what the controllers check — the API guarded these paths, the record
    rules did not, and anything reaching the data another way (XML-RPC, a report, a
    studio view) would have walked straight through.
    """

    def setUp(self):
        super().setUp()
        # emp_pm is on `mine`; nobody has put them anywhere near `theirs`.
        self.mine, _t, _f = self._live_project('Mine')
        self.env['epo.allocation'].create({
            'project_id': self.mine.id, 'employee_id': self.emp_pm.id})
        self.theirs, self.their_template, _f2 = self._live_project('Theirs')

    def test_cannot_read_another_projects_knowledge(self):
        """A client's SOP and task videos describe how that client's work is done."""
        visible = self.env['epo.document'].with_user(self.user_pm).search([
            ('project_id', '=', self.theirs.id)])
        self.assertFalse(visible)

    def test_cannot_read_another_projects_folders(self):
        visible = self.env['epo.folder'].with_user(self.user_pm).search([
            ('project_id', '=', self.theirs.id)])
        self.assertFalse(visible)

    def test_cannot_read_another_projects_form(self):
        """The questions on a stagelist are project IP."""
        visible = self.env['epo.form.template'].with_user(self.user_pm).search([
            ('id', '=', self.their_template.id)])
        self.assertFalse(visible)

    def test_cannot_read_someone_elses_role_grant(self):
        """Who holds which role is HR-adjacent — who was demoted, and when."""
        grant = self.emp_gpm.epo_role_assignment_ids[0]
        visible = self.env['epo.role.assignment'].with_user(self.user_pm).search([
            ('id', '=', grant.id)])
        self.assertFalse(visible)

    def test_can_still_read_their_own_project(self):
        """The rules must narrow, not break: the day job has to keep working."""
        own_docs = self.env['epo.document'].with_user(self.user_pm).search([
            ('project_id', '=', self.mine.id)])
        self.assertTrue(own_docs, 'a pod member lost access to their own SOP')
        own_folders = self.env['epo.folder'].with_user(self.user_pm).search([
            ('project_id', '=', self.mine.id)])
        self.assertTrue(own_folders)
        own_grant = self.env['epo.role.assignment'].with_user(self.user_pm).search([
            ('employee_id', '=', self.emp_pm.id)])
        self.assertTrue(own_grant)


@tagged('post_install', '-at_install')
class TestPhaseWindow(ProjectOsCommon):
    """Phases must stay inside their allocation's window.

    Releases get back-dated all the time — "they came off the project last Friday",
    entered on Monday. Before this was guarded, that crashed outright when a phase had
    started after the release date, and left the day counts adding up to more than the
    time the person was actually on the project.
    """

    def setUp(self):
        super().setUp()
        self.project, self.template, self.field = self._live_project('Windowed')

    def test_back_dated_release_does_not_crash(self):
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=10)})
        # Clearing onboarding today opens a phase dated today...
        self._cleared(self.project, self.emp_pm)
        # ...and then the release is back-dated to before it.
        allocation.write({'date_to': self.today - timedelta(days=2)})
        allocation.invalidate_recordset()
        self.assertFalse(allocation.is_open)

    def test_phase_days_never_exceed_the_allocation_span(self):
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=10)})
        self._cleared(self.project, self.emp_pm)
        allocation.write({'date_to': self.today - timedelta(days=2)})
        allocation.invalidate_recordset()
        phase_days = sum(allocation.phase_ids.mapped('duration_days'))
        self.assertEqual(phase_days, allocation.days_total)

    def test_no_phase_survives_past_the_end_date(self):
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=10)})
        self._cleared(self.project, self.emp_pm)
        allocation.write({'date_to': self.today - timedelta(days=2)})
        allocation.invalidate_recordset()
        for phase in allocation.phase_ids:
            self.assertLessEqual(phase.date_to, allocation.date_to)
            self.assertTrue(phase.date_to, 'a phase was left open on a closed allocation')

    def test_a_day_before_the_allocation_starts_is_clamped(self):
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self.env['epo.allocation.phase']._transition(
            allocation, 'training', self.today - timedelta(days=30))
        allocation.invalidate_recordset()
        for phase in allocation.phase_ids:
            self.assertGreaterEqual(phase.date_from, allocation.date_from)

    def test_backfill_splits_rather_than_rewriting_the_present(self):
        """Correcting last Tuesday must not move the phase they are in now."""
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=8)})
        self._cleared(self.project, self.emp_pm)
        allocation.invalidate_recordset()
        self.assertEqual(allocation.current_phase, 'tasking')
        self.env['epo.roster.day'].upsert(
            self.emp_pm.id, self.today - timedelta(days=4),
            {'tasking_status': 'training', 'project_id': self.project.id})
        allocation.invalidate_recordset()
        self.assertEqual(allocation.current_phase, 'tasking')
        self.assertEqual(allocation.days_training, 1)
        # And the segments must still tile the window without overlapping.
        segments = allocation.phase_ids.sorted('date_from')
        for earlier, later in zip(segments, segments[1:]):
            self.assertLess(earlier.date_to, later.date_from)


@tagged('post_install', '-at_install')
class TestRequestParsing(ProjectOsCommon):
    """Malformed input is the caller's mistake and must read like one.

    Every one of these used to surface as "500 Server error", which tells a frontend
    developer nothing and buries real faults in the monitoring noise.
    """

    def test_missing_and_bad_parameters(self):
        from odoo.addons.ethara_project_os.controllers.utils import (
            BadRequest, need_int, need_str, opt_int)

        with self.assertRaises(BadRequest):
            need_int({}, 'project_id')
        with self.assertRaises(BadRequest):
            need_int({'project_id': 'abc'}, 'project_id')
        with self.assertRaises(BadRequest):
            need_int({'project_id': ''}, 'project_id')
        self.assertEqual(need_int({'project_id': '7'}, 'project_id'), 7)

        self.assertIsNone(opt_int({}, 'limit'))
        self.assertEqual(opt_int({}, 'limit', 100), 100)
        self.assertEqual(opt_int({'limit': '25'}, 'limit'), 25)
        with self.assertRaises(BadRequest):
            opt_int({'limit': 'abc'}, 'limit')

        with self.assertRaises(BadRequest):
            need_str({'name': '   '}, 'name')
        self.assertEqual(need_str({'name': ' Batch 7 '}, 'name'), 'Batch 7')

    def test_pagination_bounds(self):
        """`limit=-5` is a valid integer that Postgres refuses, so without a floor it
        came back as an opaque 500 instead of a sentence naming the parameter."""
        from odoo.addons.ethara_project_os.controllers.utils import BadRequest, opt_int

        with self.assertRaises(BadRequest):
            opt_int({'limit': '-5'}, 'limit', 200, minimum=1)
        with self.assertRaises(BadRequest):
            opt_int({'offset': '-1'}, 'offset', 0, minimum=0)
        # Over the ceiling is clamped rather than refused: asking for too much is not
        # a mistake, it just does not get all of it.
        self.assertEqual(opt_int({'limit': '99999'}, 'limit', 200, maximum=500), 500)
        self.assertEqual(opt_int({'limit': '10'}, 'limit', 200, minimum=1, maximum=500), 10)
        self.assertEqual(opt_int({}, 'limit', 200, minimum=1, maximum=500), 200)

    def test_the_message_names_the_parameter(self):
        from odoo.addons.ethara_project_os.controllers.utils import BadRequest, need_int
        try:
            need_int({}, 'project_id', 'The project')
        except BadRequest as exc:
            self.assertIn('The project', str(exc))
        else:
            self.fail('expected BadRequest')


@tagged('post_install', '-at_install')
class TestMinimumScore(ProjectOsCommon):
    """The GPM's staffing bar.

    A project can demand a higher score than the assessment paper's own pass mark —
    the paper says 60, this client's work needs 80. The bar is checked when somebody is
    put on the project, not later once they have already started reading the SOP.
    """

    def setUp(self):
        super().setUp()
        self.project, self.template, self.field = self._live_project('Choosy')
        self.project.min_assessment_score = 80.0

    def _grade(self, employee, score, project=None):
        """Record a verified external result for somebody on some other project.

        v2 4.6.3: Odoo does not grade, so a score exists only because a PM read it
        somewhere else and wrote it down.
        """
        project = project or self._live_project(f'Paper for {employee.name}')[0]
        record = self.env['epo.onboarding']._get(employee.id, project.id)
        record.write({
            'assessment_passed': True,
            'assessment_score': score,
            'assessment_verified_by': self.env.uid,
            'assessment_verified_at': fields.Datetime.now(),
        })
        return record

    def test_below_the_bar_is_refused(self):
        self._grade(self.emp_pm, 72.0)
        with self.assertRaises(ValidationError) as caught:
            self.env['epo.allocation'].create({
                'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self.assertIn('80', str(caught.exception))

    def test_at_or_above_the_bar_is_allowed(self):
        self._grade(self.emp_pm, 88.0)
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self.assertEqual(allocation.min_score_applied, 80.0)
        self.assertEqual(allocation.score_at_allocation, 88.0)

    def test_no_score_at_all_is_refused_but_says_so(self):
        """"Never sat an assessment" and "sat one and scored nothing" read differently."""
        with self.assertRaises(ValidationError) as caught:
            self.env['epo.allocation'].create({
                'project_id': self.project.id, 'employee_id': self.emp_pm2.id})
        self.assertIn('no assessment score', str(caught.exception).lower())

    def test_a_gpm_can_override_with_a_reason(self):
        """Somebody has to be first on a project, and a new joiner has no score."""
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm2.id,
            'override_reason': 'First cohort — nobody has sat this paper yet.'})
        self.assertTrue(allocation)
        self.assertTrue(self.env['epo.audit.log'].search([
            ('action', '=', 'allocation_below_minimum'), ('res_id', '=', allocation.id)]))

    def test_raising_the_bar_does_not_unseat_people_already_working(self):
        """The snapshot is the whole point of storing it on the allocation."""
        self._grade(self.emp_pm, 85.0)
        allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self.project.min_assessment_score = 95.0
        allocation.invalidate_recordset()
        self.assertEqual(allocation.min_score_applied, 80.0)
        self.assertTrue(allocation.is_open)

    def test_no_bar_means_anybody(self):
        open_project, _t, _f = self._live_project('Open Door')
        self.assertEqual(open_project.min_assessment_score, 0.0)
        self.assertTrue(self.env['epo.allocation'].create({
            'project_id': open_project.id, 'employee_id': self.emp_pm2.id}))

    def test_the_candidate_list_explains_every_exclusion(self):
        """A staffing screen that silently omits people cannot tell the GPM the
        difference between "nobody qualifies" and "the filter is broken"."""
        self._grade(self.emp_pm, 88.0)
        self._grade(self.emp_pm2, 40.0)
        rows = {r['employee_id']: r for r in self.project.candidates()}

        self.assertTrue(rows[self.emp_pm.id]['eligible'])
        self.assertEqual(rows[self.emp_pm.id]['best_score'], 88.0)

        self.assertFalse(rows[self.emp_pm2.id]['eligible'])
        self.assertIn('40', rows[self.emp_pm2.id]['reason'])
        self.assertIn('80', rows[self.emp_pm2.id]['reason'])

    def test_already_allocated_people_are_shown_as_such(self):
        self._grade(self.emp_pm, 90.0)
        self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        row = next(r for r in self.project.candidates()
                   if r['employee_id'] == self.emp_pm.id)
        self.assertFalse(row['eligible'])
        self.assertIn('already on this project', row['reason'])


@tagged('post_install', '-at_install')
class TestAllocationNotice(ProjectOsCommon):

    def test_joining_a_project_emails_the_pod_member(self):
        """They need to know at the moment they are added: the onboarding gate has just
        appeared in front of them and nobody has told them yet."""
        project, _t, _f = self._live_project('Notified')
        before = self.env['mail.mail'].search_count([])
        allocation = self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        self.assertGreater(self.env['mail.mail'].search_count([]), before,
                           'no allocation notice was queued')
        mail = self.env['mail.mail'].search(
            [('subject', 'like', project.name)], order='id desc', limit=1)
        self.assertTrue(mail, 'the allocation notice itself was not queued')
        self.assertIn(self.emp_pm.work_email, mail.email_to or '')
        self.assertIn(project.name, mail.subject or '')
        body = mail.body_html or ''
        self.assertIn(self.emp_pl.name, body, 'the pod lead is not named in the notice')
        self.assertIn('SOP', body, 'the notice does not say what to do next')
        self.assertTrue(allocation.exists())

    def test_a_missing_work_email_does_not_block_the_allocation(self):
        """A mail server having a bad afternoon must not roll back staffing."""
        project, _t, _f = self._live_project('No Email')
        self.emp_pm2.work_email = False
        allocation = self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm2.id})
        self.assertTrue(allocation.exists())


@tagged('post_install', '-at_install')
class TestEdgeCases(ProjectOsCommon):
    """Situations found by attacking the module rather than exercising it.

    Every test here stands for a bug that was real: publishing a second version of a
    form failed outright, a new version came back empty, one released person emptied
    the next morning's roster for the whole organisation, and one ineligible name in a
    bulk allocation threw away the other eleven.
    """

    def test_publishing_a_second_version_supersedes_the_first(self):
        """The documented way to change a live form. It used to fail on its own
        one-published index, because the archive of v1 was still queued behind it."""
        project, template, _f = self._live_project('Versioned')
        clone = self.env['epo.form.template'].browse(
            template.action_new_version()['res_id'])
        clone.action_publish()
        template.invalidate_recordset()
        self.assertEqual(clone.state, 'published')
        self.assertEqual(template.state, 'archived')
        self.assertTrue(project.has_stagelist, 'the go-live gate was lost on republish')
        self.assertEqual(project.stagelist_template_id, clone)

    def test_a_new_version_carries_the_whole_form(self):
        """Odoo does not copy one2many relations, so this used to hand the GPM an empty
        draft and ask them to rebuild the form from memory."""
        _project, template, _f = self._live_project('Structured')

        # v2: a draft carrying an extra dropdown, then published.
        v2 = self.env['epo.form.template'].browse(
            template.action_new_version()['res_id'])
        self.assertTrue(v2.field_ids, 'the new version came back empty')
        self.env['epo.form.field'].create({
            'template_id': v2.id, 'section_id': v2.section_ids[0].id,
            'name': 'Outcome', 'field_type': 'dropdown',
            'options_json': json.dumps(['Pass', 'Fail']), 'sequence': 20})
        v2.action_publish()

        # v3 must carry everything v2 had, options and section membership included.
        v3 = self.env['epo.form.template'].browse(v2.action_new_version()['res_id'])
        self.assertEqual(len(v3.section_ids), len(v2.section_ids))
        self.assertEqual(len(v3.field_ids), len(v2.field_ids))
        self.assertEqual(v3.field_ids.mapped('name'), v2.field_ids.mapped('name'))
        copied = v3.field_ids.filtered(lambda f: f.name == 'Outcome')
        self.assertEqual(json.loads(copied.options_json), ['Pass', 'Fail'])
        self.assertTrue(copied.section_id, 'fields lost their section')
        # New ids, so editing the draft cannot rewrite the meaning of an old answer.
        self.assertFalse(set(v3.field_ids.ids) & set(v2.field_ids.ids))

    def test_carry_forward_survives_somebody_being_released(self):
        """One person released last night used to abort the cron, leaving the whole
        organisation with an empty board in the morning."""
        project, _t, _f = self._live_project('Nightly')
        leaver = self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=5)})
        stayer = self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm2.id,
            'date_from': self.today - timedelta(days=5)})
        Roster = self.env['epo.roster.day']
        yesterday = self.today - timedelta(days=1)
        for employee in (self.emp_pm, self.emp_pm2):
            Roster.upsert(employee.id, yesterday,
                          {'tasking_status': 'tasking', 'project_id': project.id})
        leaver.write({'date_to': yesterday})

        Roster._cron_carry_forward()

        released_row = Roster.search([
            ('employee_id', '=', self.emp_pm.id), ('business_date', '=', self.today)])
        self.assertTrue(released_row, 'the released person got no row at all')
        self.assertFalse(released_row.project_id,
                         'a released person was carried onto the project they left')
        self.assertEqual(released_row.tasking_status, 'bench')

        kept_row = Roster.search([
            ('employee_id', '=', self.emp_pm2.id), ('business_date', '=', self.today)])
        self.assertTrue(kept_row, 'one leaver cost everybody else their roster')
        self.assertEqual(kept_row.project_id, project)

    def test_bulk_allocation_reports_who_it_skipped(self):
        """A GPM picking twelve people off a list that went stale while they read it
        should get eleven allocations and a note, not an error and nothing."""
        project, _t, _f = self._live_project('Batch')
        project.min_assessment_score = 80.0
        link = self.env['ethara.assessment'].create({'project_id': self._live_project('Prior Paper')[0].id, 'title': 'Paper', 'url': 'https://forms.test/p', 'pass_score': 60})
        self.env['epo.onboarding']._get(employee.id, project.id).write({'assessment_passed': True, 'assessment_score': 90.0, 'assessment_verified_by': self.env.uid, 'assessment_verified_at': fields.Datetime.now()})

        created, skipped = self.env['epo.allocation'].allocate_many(
            project.id, [self.emp_pm.id, self.emp_pm2.id])

        self.assertEqual(len(created), 1, 'the qualifying person was not allocated')
        self.assertEqual(created.employee_id, self.emp_pm)
        self.assertEqual(len(skipped), 1)
        self.assertIn(self.emp_pm2.name, skipped[0])

    def test_archiving_the_last_published_form_is_refused(self):
        """It would leave a live project with nothing for anyone to fill."""
        project, template, _f = self._live_project('Keeps Its Form')
        template.write({'state': 'archived'})
        with self.assertRaises(UserError):
            project._recompute_gate()

    def test_an_answer_for_another_forms_field_is_not_stored(self):
        project, template, field = self._live_project('Own Fields')
        other_project, _ot, other_field = self._live_project('Other Fields')
        self.env['epo.allocation'].create({
            'project_id': project.id, 'employee_id': self.emp_pm.id})
        self._cleared(project, self.emp_pm)
        entry = self.env['epo.form.entry'].submit_answers(
            template.id, self.emp_pm.id,
            [{'field_id': field.id, 'text': 'mine'},
             {'field_id': other_field.id, 'text': 'not mine'}])
        self.assertEqual(entry.value_ids.field_id, field)


@tagged('post_install', '-at_install')
class TestAssessmentOpensTheGate(ProjectOsCommon):
    """Passing the assessment has to actually let somebody work.

    This was broken and no test caught it: `assessment_passed` is stored and depends
    only on the employee/project pair, neither of which changes when a result is
    graded, so Odoo never recomputed it. Every test asserted the gate stayed *shut*;
    none asserted it opened. A pod member could pass and stay locked out for good.
    """

    def setUp(self):
        super().setUp()
        self.project, self.template, self.field = self._live_project('Gated')
        self.link = self.env['ethara.assessment'].create({'project_id': self.project.id, 'title': 'Paper', 'url': 'https://forms.test/p', 'pass_score': 60})
        self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id})
        self.onboarding = self._cleared(self.project, self.emp_pm)

    def _grade(self, score):
        return self.env['epo.onboarding']._get(employee.id, project.id).write({'assessment_passed': True, 'assessment_score': 90.0, 'assessment_verified_by': self.env.uid, 'assessment_verified_at': fields.Datetime.now()})

    def test_passing_opens_the_gate(self):
        self.assertFalse(self.onboarding.unlocked)
        self._grade(75.0)
        self.onboarding.invalidate_recordset()
        self.assertTrue(self.onboarding.assessment_passed)
        self.assertTrue(self.onboarding.unlocked, 'passing did not open the gate')
        self.assertTrue(self.onboarding.unlocked_at)

    def test_passing_lets_them_submit(self):
        """The gate opening has to be worth something."""
        self._grade(75.0)
        entry = self.env['epo.form.entry'].submit_answers(
            self.template.id, self.emp_pm.id,
            [{'field_id': self.field.id, 'text': 'T-1'}])
        self.assertEqual(entry.state, 'submitted')

    def test_failing_leaves_it_shut(self):
        self._grade(40.0)
        self.onboarding.invalidate_recordset()
        self.assertFalse(self.onboarding.unlocked)
        self.assertIn('Assessment', self.onboarding.blockers)

    def test_a_later_pass_after_a_fail_opens_it(self):
        """A retake has to count — the gate reads the best graded result."""
        self.link.max_attempts = 2
        self._grade(40.0)
        self.onboarding.invalidate_recordset()
        self.assertFalse(self.onboarding.unlocked)
        self.env['epo.onboarding']._get(employee.id, project.id).write({'assessment_passed': True, 'assessment_score': 90.0, 'assessment_verified_by': self.env.uid, 'assessment_verified_at': fields.Datetime.now()})
        self.onboarding.invalidate_recordset()
        self.assertTrue(self.onboarding.unlocked, 'the retake did not count')

    def test_the_gate_opening_moves_the_allocation_into_tasking(self):
        self._grade(75.0)
        allocation = self.emp_pm.epo_allocation_ids.filtered(
            lambda a: a.project_id == self.project)
        allocation.invalidate_recordset()
        self.assertEqual(allocation.current_phase, 'tasking')


@tagged('post_install', '-at_install')
class TestBackfillDoesNotEndTheCurrentStretch(ProjectOsCommon):
    """Catching up on last week must not end the phase somebody is in today.

    Found by building the demo: a lead filling in a past day of leave closed the
    stretch that had opened when onboarding cleared, so people who were plainly
    tasking reported no current phase at all and their history fragmented into a row
    per day.
    """

    def setUp(self):
        super().setUp()
        self.project, self.template, self.field = self._live_project('Catching Up')
        self.allocation = self.env['epo.allocation'].create({
            'project_id': self.project.id, 'employee_id': self.emp_pm.id,
            'date_from': self.today - timedelta(days=10)})
        self._cleared(self.project, self.emp_pm)      # opens a stretch dated today

    def test_a_past_leave_day_leaves_today_alone(self):
        self.env['epo.roster.day'].upsert(
            self.emp_pm.id, self.today - timedelta(days=6),
            {'tasking_status': 'leave', 'project_id': False})
        self.allocation.invalidate_recordset()
        self.assertEqual(self.allocation.current_phase, 'tasking',
                         'backfilling leave ended the stretch they are in now')

    def test_a_past_bench_day_leaves_today_alone(self):
        self.env['epo.roster.day'].upsert(
            self.emp_pm.id, self.today - timedelta(days=3),
            {'tasking_status': 'bench', 'project_id': False})
        self.allocation.invalidate_recordset()
        self.assertTrue(self.allocation.current_phase)

    def test_leave_today_does_end_todays_stretch(self):
        """The guard must not go too far the other way: leave *today* still counts."""
        self.env['epo.roster.day'].upsert(
            self.emp_pm.id, self.today,
            {'tasking_status': 'leave', 'project_id': False})
        self.allocation.invalidate_recordset()
        self.assertFalse(self.allocation.current_phase,
                         'somebody on leave today is still shown as tasking')

    def test_working_a_week_gives_contiguous_stretches(self):
        """One row per phase, not one row per day."""
        Roster = self.env['epo.roster.day']
        for offset in range(6, -1, -1):
            Roster.upsert(self.emp_pm.id, self.today - timedelta(days=offset),
                          {'tasking_status': 'tasking', 'project_id': self.project.id})
        self.allocation.invalidate_recordset()
        tasking = self.allocation.phase_ids.filtered(lambda p: p.phase == 'tasking')
        self.assertLessEqual(len(tasking), 2,
                             f'a week of tasking became {len(tasking)} segments')
        self.assertGreaterEqual(self.allocation.days_tasking, 7)
