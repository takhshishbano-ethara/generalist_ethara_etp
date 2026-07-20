from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'ethara_project', 'ethara_project_threading')
class TestEmailThreadRoot(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Project = cls.env['ethara.project']
        cls.project = cls.Project.create({
            'name': 'Thread Test Project',
            'client_project_name': 'Client Thread Test',
            'internal_project_name': 'Internal Thread Test',
        })

    def _post(self, project, body):
        return project.message_post(
            body=body,
            subtype_xmlid='mail.mt_comment',
            message_type='comment',
        )

    def test_capture_root_sets_only_when_empty(self):
        self.assertFalse(self.project.email_thread_root_message_id)
        first = self._post(self.project, 'first')
        self.project._ethara_capture_root(first)
        self.assertEqual(self.project.email_thread_root_message_id, first)

        second = self._post(self.project, 'second')
        self.project._ethara_capture_root(second)
        self.assertEqual(
            self.project.email_thread_root_message_id, first,
            'Root must NOT be overwritten by later messages.',
        )

    def test_thread_post_kwargs_injects_parent_id(self):
        first = self._post(self.project, 'anchor')
        self.project._ethara_capture_root(first)

        base = {'body': 'reply', 'subtype_xmlid': 'mail.mt_comment'}
        kwargs = self.project._ethara_thread_post_kwargs(base)
        self.assertEqual(kwargs['parent_id'], first.id)

    def test_thread_post_kwargs_noop_when_root_unset(self):
        base = {'body': 'reply', 'subtype_xmlid': 'mail.mt_comment'}
        kwargs = self.project._ethara_thread_post_kwargs(base)
        self.assertNotIn('parent_id', kwargs)

    def test_second_project_has_isolated_root(self):
        first_a = self._post(self.project, 'A1')
        self.project._ethara_capture_root(first_a)

        project_b = self.Project.create({
            'name': 'Second Project',
            'client_project_name': 'Client B',
            'internal_project_name': 'Internal B',
        })
        first_b = self._post(project_b, 'B1')
        project_b._ethara_capture_root(first_b)

        self.assertNotEqual(
            self.project.email_thread_root_message_id,
            project_b.email_thread_root_message_id,
            'Two projects must anchor on their own root message.',
        )
        self.assertEqual(self.project.email_thread_root_message_id, first_a)
        self.assertEqual(project_b.email_thread_root_message_id, first_b)

    def test_reply_chain_parent_id_persists_across_posts(self):
        first = self._post(self.project, 'root msg')
        self.project._ethara_capture_root(first)

        base = {
            'body': 'reply n',
            'subtype_xmlid': 'mail.mt_comment',
            'message_type': 'comment',
        }
        for _ in range(3):
            k = self.project._ethara_thread_post_kwargs(dict(base))
            msg = self.project.message_post(**k)
            self.assertEqual(msg.parent_id, first)

    def test_notify_headers_include_in_reply_to(self):
        first = self._post(self.project, 'root')
        self.project._ethara_capture_root(first)
        self.assertTrue(first.message_id, 'First message must have Message-Id')

        second = self.project.message_post(
            body='reply',
            subtype_xmlid='mail.mt_comment',
            message_type='comment',
            parent_id=first.id,
        )
        base_vals = self.project._notify_by_email_get_base_mail_values(
            second, [], additional_values=None,
        )
        self.assertIn(
            first.message_id, base_vals.get('references', ''),
            'Root Message-Id must appear in References header.',
        )
        self.assertIn('In-Reply-To', base_vals.get('headers') or '')
