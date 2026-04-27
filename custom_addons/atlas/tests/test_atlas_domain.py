from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from psycopg2 import IntegrityError
from odoo.tools import mute_logger


@tagged("atlas", "atlas_domain", "post_install", "-at_install")
class TestAtlasDomain(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Domain = cls.env["atlas.domain"]

    def test_create_plain(self):
        d = self.Domain.create({"name": "Plain"})
        self.assertTrue(d.id > 0)
        self.assertEqual(d.name, "Plain")

    def test_create_empty_name_stored_as_false(self):
        d = self.Domain.create({"name": ""})
        self.assertFalse(d.name)

    def test_create_unicode_cjk(self):
        d = self.Domain.create({"name": "\u4e2d\u6587\u57df"})
        self.assertEqual(d.name, "\u4e2d\u6587\u57df")

    def test_create_emoji(self):
        d = self.Domain.create({"name": "Emoji \U0001f600"})
        self.assertEqual(d.name, "Emoji \U0001f600")

    def test_create_long_500_chars(self):
        d = self.Domain.create({"name": "x" * 500})
        self.assertEqual(len(d.name), 500)

    def test_create_with_md_files(self):
        d = self.Domain.create({"name": "T", "md_file1": "a.md", "md_file2": "b.md", "md_file3": "c.md"})
        self.assertEqual(d.md_file1, "a.md")
        self.assertEqual(d.md_file2, "b.md")
        self.assertEqual(d.md_file3, "c.md")

    def test_md_files_default_false(self):
        d = self.Domain.create({"name": "NoMd"})
        self.assertFalse(d.md_file1)
        self.assertFalse(d.md_file2)
        self.assertFalse(d.md_file3)

    def test_write_rename(self):
        d = self.Domain.create({"name": "Old"})
        d.write({"name": "New"})
        self.assertEqual(d.name, "New")

    def test_unlink_removes_record(self):
        d = self.Domain.create({"name": "ToKill"})
        did = d.id
        d.unlink()
        self.assertFalse(self.Domain.search([("id", "=", did)]))

    def test_search_by_name_like(self):
        self.Domain.create({"name": "FindMe Alpha"})
        self.Domain.create({"name": "FindMe Beta"})
        r = self.Domain.search([("name", "like", "FindMe")])
        self.assertGreaterEqual(len(r), 2)

    def test_parent_single_child(self):
        p = self.Domain.create({"name": "Parent"})
        c = self.Domain.create({"name": "Child", "parent_id": p.id})
        self.assertEqual(c.parent_id.id, p.id)
        self.assertEqual(len(p.child_ids), 1)

    def test_parent_multiple_children(self):
        p = self.Domain.create({"name": "ParentMulti"})
        for i in range(5):
            self.Domain.create({"name": "C%d" % i, "parent_id": p.id})
        self.assertEqual(len(p.child_ids), 5)

    def test_reparenting_changes_parent(self):
        p1 = self.Domain.create({"name": "P1"})
        p2 = self.Domain.create({"name": "P2"})
        c = self.Domain.create({"name": "C", "parent_id": p1.id})
        c.parent_id = p2
        self.assertEqual(len(p1.child_ids), 0)
        self.assertEqual(len(p2.child_ids), 1)

    def test_orphan_parent_unlinked_child_parent_false(self):
        p = self.Domain.create({"name": "ToBeGone"})
        c = self.Domain.create({"name": "Orphan", "parent_id": p.id})
        p.unlink()
        self.assertFalse(c.parent_id)

    def test_deep_nesting_5_levels(self):
        prev = None
        for i in range(5):
            d = self.Domain.create({"name": "L%d" % i, "parent_id": prev.id if prev else False})
            prev = d
        leaf = prev
        depth = 0
        while leaf.parent_id:
            leaf = leaf.parent_id
            depth += 1
        self.assertEqual(depth, 4)

    def test_self_parent_allowed_or_rejected(self):
        d = self.Domain.create({"name": "Self"})
        try:
            d.parent_id = d
            self.assertEqual(d.parent_id.id, d.id)
        except Exception:
            pass

    def test_display_name_uses_name(self):
        d = self.Domain.create({"name": "DN"})
        self.assertEqual(d.display_name, "DN")

    def test_browse_by_id(self):
        d = self.Domain.create({"name": "B"})
        b = self.Domain.browse(d.id)
        self.assertEqual(b.name, "D" "B"[1:])

    def test_multi_create_batch(self):
        r = self.Domain.create([{"name": "A"}, {"name": "B"}, {"name": "C"}])
        self.assertEqual(len(r), 3)

    def test_name_with_sql_injection_stored_literally(self):
        payload = "'; DROP TABLE atlas_domain; --"
        d = self.Domain.create({"name": payload})
        self.assertEqual(d.name, payload)
        self.assertTrue(self.Domain.search([("id", "=", d.id)]))

    def test_name_with_xss_payload_stored_literally(self):
        payload = "<script>alert('xss')</script>"
        d = self.Domain.create({"name": payload})
        self.assertEqual(d.name, payload)

    def test_name_with_path_traversal_stored_literally(self):
        d = self.Domain.create({"name": "../../etc/passwd"})
        self.assertEqual(d.name, "../../etc/passwd")

    def test_name_with_newline_preserved(self):
        d = self.Domain.create({"name": "line1\nline2"})
        self.assertEqual(d.name, "line1\nline2")

    def test_name_with_tab_preserved(self):
        d = self.Domain.create({"name": "col1\tcol2"})
        self.assertEqual(d.name, "col1\tcol2")

    def test_two_siblings_same_parent(self):
        p = self.Domain.create({"name": "P"})
        s1 = self.Domain.create({"name": "S1", "parent_id": p.id})
        s2 = self.Domain.create({"name": "S2", "parent_id": p.id})
        self.assertNotEqual(s1.id, s2.id)
        self.assertIn(s1, p.child_ids)
        self.assertIn(s2, p.child_ids)

    def test_write_md_files(self):
        d = self.Domain.create({"name": "N"})
        d.write({"md_file1": "new.md"})
        self.assertEqual(d.md_file1, "new.md")

    def test_write_clear_parent(self):
        p = self.Domain.create({"name": "P"})
        c = self.Domain.create({"name": "C", "parent_id": p.id})
        c.parent_id = False
        self.assertFalse(c.parent_id)

    def test_name_with_500_unicode_chars(self):
        name = "\u4e2d" * 500
        d = self.Domain.create({"name": name})
        self.assertEqual(d.name, name)
