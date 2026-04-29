import json

from odoo.tests.common import TransactionCase


class TestJaegerInstance(TransactionCase):
    """Test jaeger.instance model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/testorg/testrepo",
            "language": "python",
            "pipeline_mode": "swe",
        })
        cls.instance = cls.env["jaeger.instance"].create({
            "name": "testorg__testrepo-42",
            "repository_id": cls.repo.id,
            "org": "testorg",
            "repo": "testrepo",
            "pr_number": 42,
            "base_sha": "abc123",
            "language": "python",
            "fix_patch": "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new",
            "test_patch": "--- a/test_foo.py\n+++ b/test_foo.py\n@@ -1 +1 @@\n-old\n+new",
        })

    def test_instance_creation(self):
        """Test instance record creation."""
        self.assertEqual(self.instance.name, "testorg__testrepo-42")
        self.assertEqual(self.instance.pr_number, 42)

    def test_compute_pr_url(self):
        """Test PR URL computation."""
        self.assertEqual(
            self.instance.pr_url,
            "https://github.com/testorg/testrepo/pull/42",
        )

    def test_compute_instance_id(self):
        """Test instance ID computation."""
        self.assertEqual(self.instance.instance_id, "testorg/testrepo:pr-42")

    def test_compute_test_counts(self):
        """Test f2p/p2p count computation from JSON."""
        self.instance.write({
            "f2p_tests_json": json.dumps({"test_a": {}, "test_b": {}}),
            "p2p_tests_json": json.dumps({"test_c": {}}),
        })
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.f2p_count, 2)
        self.assertEqual(self.instance.p2p_count, 1)

    def test_compute_test_counts_empty(self):
        """Test counts with empty/invalid JSON."""
        self.instance.write({
            "f2p_tests_json": "",
            "p2p_tests_json": "not-json",
        })
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.f2p_count, 0)
        self.assertEqual(self.instance.p2p_count, 0)

    def test_parse_test_log(self):
        """Test test log parsing."""
        log = """
test_foo.py::test_add PASSED
test_foo.py::test_sub FAILED
test_foo.py::test_mul SKIPPED
test_foo.py::test_div PASSED
"""
        result = self.instance._parse_test_log(log)
        self.assertEqual(result["passed_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("test_foo.py::test_add", result["passed_tests"])
        self.assertIn("test_foo.py::test_sub", result["failed_tests"])

    def test_parse_test_log_empty(self):
        """Test parsing empty log."""
        result = self.instance._parse_test_log("")
        self.assertEqual(result["passed_count"], 0)
        self.assertEqual(result["failed_count"], 0)

    def test_generate_test_report(self):
        """Test report generation with state transitions."""
        run_result = {
            "passed_tests": ["test_a"],
            "failed_tests": ["test_b"],
            "skipped_tests": [],
            "passed_count": 1,
            "failed_count": 1,
            "skipped_count": 0,
        }
        test_result = {
            "passed_tests": ["test_a"],
            "failed_tests": ["test_b", "test_c"],
            "skipped_tests": ["test_d"],
            "passed_count": 1,
            "failed_count": 2,
            "skipped_count": 1,
        }
        fix_result = {
            "passed_tests": ["test_a", "test_b", "test_c", "test_d"],
            "failed_tests": [],
            "skipped_tests": [],
            "passed_count": 4,
            "failed_count": 0,
            "skipped_count": 0,
        }

        self.instance._generate_test_report(run_result, test_result, fix_result)

        # test_b: FAIL -> PASS (f2p)
        f2p = json.loads(self.instance.f2p_tests_json)
        self.assertIn("test_b", f2p)
        self.assertIn("test_c", f2p)

        # test_a: PASS -> PASS (p2p)
        p2p = json.loads(self.instance.p2p_tests_json)
        self.assertIn("test_a", p2p)

        # test_d: SKIP -> PASS (s2p)
        s2p = json.loads(self.instance.s2p_tests_json)
        self.assertIn("test_d", s2p)

        self.assertTrue(self.instance.is_valid)
        self.assertTrue(self.instance.has_fix_signal)
        self.assertFalse(self.instance.has_regressions)

    def test_generate_test_report_regression(self):
        """Test report detects regressions."""
        test_result = {
            "passed_tests": ["test_a", "test_b"],
            "failed_tests": ["test_c"],
            "skipped_tests": [],
            "passed_count": 2,
            "failed_count": 1,
            "skipped_count": 0,
        }
        fix_result = {
            "passed_tests": ["test_c"],
            "failed_tests": ["test_a"],  # regression
            "skipped_tests": [],
            "passed_count": 1,
            "failed_count": 1,
            "skipped_count": 0,
        }

        self.instance._generate_test_report({}, test_result, fix_result)

        self.assertTrue(self.instance.has_regressions)
        self.assertFalse(self.instance.is_valid)
        self.assertIn("test_a", self.instance.validation_error)

    def test_default_docker_status(self):
        """Test default docker build status."""
        self.assertEqual(self.instance.docker_build_status, "pending")
        self.assertEqual(self.instance.delivery_status, "pending")
