from odoo.tests.common import TransactionCase


class TestJaegerRepository(TransactionCase):
    """Test jaeger.repository model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/testorg/testrepo",
            "language": "python",
            "pipeline_mode": "swe",
        })

    def test_create_repo(self):
        """Test repo creation and sequence assignment."""
        self.assertTrue(self.repo.name)
        self.assertNotEqual(self.repo.name, "New")

    def test_compute_org_repo(self):
        """Test org/repo name extraction from URL."""
        self.assertEqual(self.repo.org, "testorg")
        self.assertEqual(self.repo.repo_name, "testrepo")

    def test_compute_org_repo_git_suffix(self):
        """Test URL parsing with .git suffix."""
        repo = self.env["jaeger.repository"].create({
            "repo_url": "https://github.com/myorg/myrepo.git",
            "language": "java",
        })
        self.assertEqual(repo.org, "myorg")
        self.assertEqual(repo.repo_name, "myrepo")

    def test_default_stage(self):
        """Test default stage is stage1."""
        self.assertEqual(self.repo.current_stage, "stage1")
        self.assertEqual(self.repo.terminal_state, "none")

    def test_gate_check_stage1(self):
        """Test stage1 gate requires crawl_status done."""
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("validation", msg.lower())

    def test_next_stage_mapping(self):
        """Test stage progression mapping."""
        self.assertEqual(self.repo._next_stage(), "stage2")
        self.repo.write({"current_stage": "stage3"})
        self.assertEqual(self.repo._next_stage(), "stage4")
        self.repo.write({"current_stage": "stage7"})
        self.assertEqual(self.repo._next_stage(), "done")

    def test_append_log(self):
        """Test log output appending."""
        self.repo._append_log("Test message 1")
        self.assertIn("Test message 1", self.repo.log_output)
        self.repo._append_log("Test message 2")
        self.assertIn("Test message 2", self.repo.log_output)

    def test_append_log_truncation(self):
        """Test log truncation over 500 lines."""
        for i in range(600):
            self.repo._append_log(f"Line {i}")
        lines = self.repo.log_output.strip().split("\n")
        self.assertLess(len(lines), 510)

    def test_gate_blocks_advance(self):
        """Test that action_advance_stage raises when gate fails."""
        from odoo.exceptions import UserError

        with self.assertRaises(UserError):
            self.repo.action_advance_stage()

    def test_collect_prs_wrong_stage(self):
        """Test collect PRs requires stage2."""
        from odoo.exceptions import UserError

        with self.assertRaises(UserError):
            self.repo.action_collect_prs()
