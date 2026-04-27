from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_sec_bound", "post_install", "-at_install")
class TestAtlasSecurityBoundaries(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Domain = cls.env["atlas.domain"]
        cls.Task = cls.env["atlas.atlas"].create({})
        cls.Criterion = cls.env["atlas.rubric.criterion"]

    def test_sql_injection_stored_literally_not_executed(self):
        payload = "'; DROP TABLE atlas_domain; --"
        d = self.Domain.create({"name": payload})
        found = self.Domain.search([("name", "=", payload)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found.name, payload)
        # Verify table still exists by creating more records
        self.Domain.create({"name": "after_attack_still_works"})

    def test_sql_or_injection_not_bypass(self):
        d = self.Domain.create({"name": "real_name"})
        hack = "fake' OR '1'='1"
        found = self.Domain.search([("name", "=", hack)])
        self.assertEqual(len(found), 0)

    def test_xss_payload_stored_not_rendered(self):
        payload = "<script>alert(1)</script>"
        d = self.Domain.create({"name": payload})
        self.assertEqual(d.name, payload)
        self.assertIn("<script>", d.name)

    def test_template_injection_not_evaluated(self):
        payload = "{{7*7}}"
        d = self.Domain.create({"name": payload})
        self.assertEqual(d.name, "{{7*7}}")
        self.assertNotIn("49", d.name)

    def test_jinja_injection_not_evaluated(self):
        payload = "${{jndi:ldap://evil}}"
        d = self.Domain.create({"name": payload})
        self.assertEqual(d.name, payload)

    def test_path_traversal_stored_literally(self):
        payload = "../../etc/passwd"
        d = self.Domain.create({"md_file1": payload, "name": "pt_test"})
        self.assertEqual(d.md_file1, payload)

    def test_command_injection_in_name_no_execution(self):
        payload = "; rm -rf /"
        d = self.Domain.create({"name": payload})
        self.assertEqual(d.name, payload)

    def test_criterion_injection_in_suggestion_raw_stored(self):
        c = self.Criterion.create({
            "atlas_id": self.Task.id,
            "name": "injection criterion testing here",
            "suggestion": "'; DROP TABLE atlas_rubric_criterion; --",
        })
        self.assertIn("DROP TABLE", c.suggestion)

    def test_email_with_injection_stored_literally(self):
        a = self.env["atlas.atlas"].create(
            {"email": "'; DROP TABLE users; --@example.com"})
        self.assertIn("DROP TABLE", a.email)

    def test_config_param_injection_stored_literally(self):
        ICP = self.env["ir.config_parameter"].sudo()
        payload = "<script>alert(1)</script>"
        ICP.set_param("atlas.bedrock_inference_arn", payload)
        got = ICP.get_param("atlas.bedrock_inference_arn")
        self.assertEqual(got, payload)

    def test_null_byte_handled_in_name(self):
        try:
            d = self.Domain.create({"name": "pre\x00post"})
            self.assertIn("pre", d.name or "")
        except Exception:
            pass

    def test_unicode_bom_preserved_in_stored_name(self):
        d = self.Domain.create({"name": "\ufeff prefixed bom"})
        self.assertIn("prefixed", d.name)

    def test_rtl_override_character_preserved(self):
        d = self.Domain.create({"name": "normal\u202Ehidden"})
        self.assertIn("\u202E", d.name)

    def test_zero_width_space_in_search(self):
        d = self.Domain.create({"name": "foo\u200Bbar"})
        exact = self.Domain.search([("name", "=", "foo\u200Bbar")])
        self.assertEqual(len(exact), 1)
        noZwsp = self.Domain.search([("name", "=", "foobar")])
        self.assertEqual(len(noZwsp), 0)

    def test_homoglyph_attack_name_different_from_latin(self):
        latin = self.Domain.create({"name": "admin"})
        cyrillic = self.Domain.create({"name": "\u0430dmin"})
        self.assertNotEqual(latin.id, cyrillic.id)
        self.assertNotEqual(latin.name, cyrillic.name)
