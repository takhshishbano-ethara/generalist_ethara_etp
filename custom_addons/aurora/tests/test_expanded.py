# -*- coding: utf-8 -*-
"""Massive parametric expansion tests for all Aurora modules."""
import hashlib
import json
import os
import re
import string
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPipelineExpanded(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("aurora.output_dir", "/tmp/aurora_test")
        cls.env["ir.config_parameter"].sudo().set_param("aurora.max_active_tasks", "10")
        cls.env["ir.config_parameter"].sudo().set_param("aurora.lang_detection_mode", "manual")
        cls.env["ir.config_parameter"].sudo().set_param("aurora.lang", "python")
        cls.env["ir.config_parameter"].sudo().set_param("aurora.s3_bucket", "")
        cls.env["ir.config_parameter"].sudo().set_param("aurora.s3_access_key", "")
        cls.env["ir.config_parameter"].sudo().set_param("aurora.s3_secret_key", "")
        cls.env["aurora.github.token"].sudo().create({
            "name": "exp-tok", "token": "ghp_exp1",
            "token_hash": "exp_hash_1", "state": "active",
        })

    def _create(self, **kw):
        v = {"github_org": "org", "github_repo": "repo"}
        v.update(kw)
        return self.env["aurora.pipeline"].create(v)

    def test_batch_create_50(self):
        recs = self.env["aurora.pipeline"].create([
            {"github_org": f"org{i}", "github_repo": f"repo{i}"}
            for i in range(50)
        ])
        self.assertEqual(len(recs), 50)
        names = [r.name for r in recs]
        self.assertEqual(len(set(names)), 50)

    def test_all_step_status_combinations(self):
        from odoo.addons.aurora.models.pipeline import AUTOMATION_STATUS
        rec = self._create()
        for step in range(1, 7):
            for status_key, _ in AUTOMATION_STATUS:
                rec.write({f"step{step}_status": status_key})
                self.assertEqual(getattr(rec, f"step{step}_status"), status_key)

    def test_valid_org_names_parametric(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        valid_names = [
            "a", "Z", "org", "my-org", "my_org", "my.org", "org123",
            "A123", "test-org-name", "a.b.c", "x_y_z", "aBcDeF",
        ] + [f"org{i}" for i in range(100)]
        for name in valid_names:
            self.assertTrue(_SAFE_GITHUB_NAME.match(name), f"{name!r} should be valid")

    def test_invalid_org_names_parametric(self):
        from odoo.addons.aurora.models.pipeline import _SAFE_GITHUB_NAME
        invalid = [
            "", " ", "a b", "a/b", "a;b", "a&b", "a|b",
            "a$(cmd)", "a`cmd`", "a\nb", "a\tb", "a<b", "a>b",
            "a{b}", "a[b]", "a(b)", "a!b", "a@b", "a#b",
            "a$b", "a%b", "a^b", "a*b", "a+b", "a=b",
            "a'b", 'a"b', "a\\b", "a,b", "a?b",
        ]
        for name in invalid:
            self.assertFalse(_SAFE_GITHUB_NAME.match(name), f"{name!r} should be invalid")

    def test_config_type_conversions(self):
        rec = self._create()
        self.env["ir.config_parameter"].sudo().set_param("aurora.delay_on_error", "123")
        self.env["ir.config_parameter"].sudo().set_param("aurora.retry_attempts", "5")
        self.env["ir.config_parameter"].sudo().set_param("aurora.max_tags", "50")
        self.env["ir.config_parameter"].sudo().set_param("aurora.window_days", "7")
        cfg = rec._get_config()
        self.assertIsInstance(cfg["delay_on_error"], int)
        self.assertIsInstance(cfg["retry_attempts"], int)
        self.assertIsInstance(cfg["max_tags"], int)
        self.assertIsInstance(cfg["window_days"], int)
        self.assertEqual(cfg["delay_on_error"], 123)
        self.assertEqual(cfg["retry_attempts"], 5)

    def test_use_s3_parametric(self):
        rec = self._create()
        s3_paths = [
            ("s3://bucket/key", True),
            ("s3://a", True),
            ("/tmp/local", False),
            ("", False),
            (False, False),
        ]
        for path, expected in s3_paths:
            rec.write({"output_dir": path})
            self.assertEqual(rec.use_s3, expected, f"use_s3 for {path!r}")

    def test_language_map_completeness(self):
        from odoo.addons.aurora.models.pipeline_config import LANGUAGE_SELECTION, GITHUB_LANG_MAP
        sel_keys = {k for k, _ in LANGUAGE_SELECTION}
        map_values = set(GITHUB_LANG_MAP.values())
        self.assertEqual(sel_keys, map_values)
        self.assertEqual(len(sel_keys), 15)

    def test_dashboard_data_structure(self):
        rec = self._create()
        data = self.env["aurora.pipeline"].get_dashboard_data()
        self.assertIn("pipelines", data)
        self.assertIn("evaluations", data)
        self.assertIn("recent_pipelines", data)
        self.assertIn("recent_evaluations", data)
        self.assertIn("tokens", data)
        for key in ["total", "running", "done", "failed", "draft"]:
            self.assertIn(key, data["pipelines"])
            self.assertIn(key, data["evaluations"])

    def test_counter_fields_writable(self):
        rec = self._create()
        counters = {
            "pr_count": 100, "filtered_pr_count": 50, "tag_count": 25,
            "group_count": 10, "issue_count": 30, "dataset_count": 8,
        }
        rec.write(counters)
        for field, val in counters.items():
            self.assertEqual(getattr(rec, field), val)


@tagged("post_install", "-at_install")
class TestEvaluationExpanded(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("aurora.output_dir", "/tmp/aurora_test")
        cls.pipeline = cls.env["aurora.pipeline"].create({
            "github_org": "testorg", "github_repo": "testrepo",
        })
        cls.pipeline.write({"stage": "done", "step6_file": "/tmp/ds.jsonl"})

    def test_batch_create_50_evals(self):
        recs = self.env["aurora.evaluation"].create([{} for _ in range(50)])
        self.assertEqual(len(recs), 50)
        names = [r.name for r in recs]
        self.assertEqual(len(set(names)), 50)

    def test_all_status_combinations(self):
        from odoo.addons.aurora.models.evaluation import EVAL_STATUS
        rec = self.env["aurora.evaluation"].create({})
        for field in ["build_status", "run_status", "report_status"]:
            for key, _ in EVAL_STATUS:
                rec.write({field: key})
                self.assertEqual(getattr(rec, field), key)

    def test_reset_multiple_times(self):
        rec = self.env["aurora.evaluation"].create({})
        for _ in range(10):
            rec.write({"stage": "failed", "build_status": "done"})
            rec.action_reset_to_draft()
            self.assertEqual(rec.stage, "draft")
            self.assertEqual(rec.build_status, "idle")


class TestParseTagExpanded(TestCase):

    def _parse(self, name):
        from odoo.addons.aurora.tools.collect.get_version_tags import parse_tag
        return parse_tag(name)

    def test_semver_200_versions(self):
        for major in range(10):
            for minor in range(10):
                for p in range(2):
                    r = self._parse(f"v{major}.{minor}.{p}")
                    self.assertEqual(r["scheme"], "semver")

    def test_calver_all_months(self):
        for month in range(1, 13):
            r = self._parse(f"2024.{month:02d}")
            self.assertEqual(r["scheme"], "calver")
            self.assertEqual(r["minor"], month)

    def test_calver_all_days(self):
        for day in [1, 10, 15, 28, 30, 31]:
            r = self._parse(f"2024.01.{day:02d}")
            self.assertEqual(r["scheme"], "calver")

    def test_prerelease_variants(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _PRE_RELEASE_IDENTIFIERS
        for ident in _PRE_RELEASE_IDENTIFIERS:
            for suffix in ["", ".1", ".2", "1", "2"]:
                tag = f"v1.0.0-{ident}{suffix}"
                r = self._parse(tag)
                self.assertEqual(r["scheme"], "semver")
                self.assertTrue(r["is_pre_release"], f"{tag} should be pre-release")

    def test_sort_key_monotonic(self):
        tags = [f"v{i}.0.0" for i in range(20)]
        parsed = [self._parse(t) for t in tags]
        for i in range(len(parsed) - 1):
            self.assertLess(parsed[i]["sort_key"], parsed[i + 1]["sort_key"])

    def test_prefix_variants(self):
        prefixes = ["release/", "hotfix/", "rel/", "version/", "ver/",
                     "Release/", "HOTFIX/", "release-", "version-"]
        for prefix in prefixes:
            r = self._parse(f"{prefix}v1.2.3")
            self.assertEqual(r["scheme"], "semver", f"Failed for prefix {prefix}")


class TestExtractResolvedIssuesExpanded(TestCase):

    def _pull(self, title="", body="", commits=None):
        return {"title": title or "", "body": body or "", "commits": commits or []}

    def test_all_keyword_variants(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        keywords = ["close", "closes", "closed", "fix", "fixes", "fixed",
                     "resolve", "resolves", "resolved"]
        for kw in keywords:
            result = extract_resolved_issues(self._pull(body=f"{kw} #100"))
            self.assertIn(100, result, f"Keyword '{kw}' should match")

    def test_many_issues_in_one_body(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        body = " ".join(f"fix #{i}" for i in range(1, 51))
        result = extract_resolved_issues(self._pull(body=body))
        self.assertEqual(len(result), 50)

    def test_issue_numbers_1_to_100(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        for n in range(1, 101):
            result = extract_resolved_issues(self._pull(body=f"fix #{n}"))
            self.assertIn(n, result)


class TestGroupPrsExpanded(TestCase):

    def test_extract_pr_numbers_parametric(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        for n in range(1, 51):
            result = _extract_pr_numbers(f"Merge pull request #{n} from user/br")
            self.assertIn(n, result)

    def test_parse_date_various_formats(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        valid_dates = [
            "2024-01-01T00:00:00Z",
            "2024-06-15T12:30:45+00:00",
            "2023-12-31T23:59:59Z",
            "2020-01-01T00:00:00+05:30",
        ]
        for d in valid_dates:
            self.assertIsNotNone(_parse_date(d), f"Should parse: {d}")

    def test_filter_pre_releases_parametric(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _filter_pre_releases
        tags = [{"is_pre_release": i % 3 == 0, "name": f"t{i}"} for i in range(30)]
        result = _filter_pre_releases(tags)
        self.assertTrue(all(not t["is_pre_release"] for t in result))


class TestBuildDatasetExpanded(TestCase):

    def test_split_patches_test_keywords(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches, TEST_PATH_KEYWORDS
        for kw in TEST_PATH_KEYWORDS:
            diff = f"diff --git a/{kw}/foo.py b/{kw}/foo.py\n--- a/{kw}/foo.py\n+++ b/{kw}/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
            fix, test = split_patches(diff)
            self.assertTrue(len(test) > 0, f"Keyword '{kw}' should classify as test")

    def test_extract_issue_urls_parametric(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        for n in [1, 10, 100, 1000, 9999]:
            body = f"https://github.com/org/repo/issues/{n}"
            result = extract_issue_numbers_from_body(body)
            self.assertIn(n, result)

    def test_aggregate_issues_many_prs(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": i, "body": f"fixes #{i+100}", "title": f"PR{i}", "resolved_issues": [i+100]}
               for i in range(50)]
        issues = {i+100: {"number": i+100, "title": f"Bug{i}", "body": f"Desc{i}"}
                  for i in range(50)}
        result = aggregate_issues(prs, issues)
        self.assertEqual(len(result), 50)


class TestHarnessExpanded(TestCase):

    def test_test_status_all_values(self):
        from odoo.addons.aurora.tools.harness.test_result import TestStatus
        self.assertEqual(len(TestStatus), 9)

    def test_test_result_many_tests(self):
        from odoo.addons.aurora.tools.harness.test_result import TestResult
        passed = {f"test_{i}" for i in range(100)}
        failed = {f"fail_{i}" for i in range(50)}
        skipped = {f"skip_{i}" for i in range(25)}
        r = TestResult(
            passed_count=100, failed_count=50, skipped_count=25,
            passed_tests=passed, failed_tests=failed, skipped_tests=skipped,
        )
        self.assertEqual(r.all_count, 175)

    def test_mapping_to_testresult_large(self):
        from odoo.addons.aurora.tools.harness.test_result import mapping_to_testresult
        m = {}
        for i in range(100):
            m[f"test_{i}"] = "PASSED"
        for i in range(50):
            m[f"fail_{i}"] = "FAILED"
        for i in range(25):
            m[f"skip_{i}"] = "SKIPPED"
        r = mapping_to_testresult(m)
        self.assertEqual(r.passed_count, 100)
        self.assertEqual(r.failed_count, 50)
        self.assertEqual(r.skipped_count, 25)

    def test_repository_ordering_100(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        repos = [Repository(org=f"org{i:03d}", repo="r") for i in range(100)]
        sorted_repos = sorted(repos)
        for i in range(99):
            self.assertLess(sorted_repos[i], sorted_repos[i + 1])

    def test_pull_request_base_ordering_100(self):
        from odoo.addons.aurora.tools.harness.pull_request import PullRequestBase
        prs = [PullRequestBase(org="o", repo="r", number=i) for i in range(100)]
        sorted_prs = sorted(prs)
        for i in range(99):
            self.assertLess(sorted_prs[i], sorted_prs[i + 1])

    def test_base_validation_types(self):
        from odoo.addons.aurora.tools.harness.pull_request import Base
        valid = [
            ("label", "ref", "sha"),
            ("", "", ""),
            ("a" * 1000, "b" * 1000, "c" * 1000),
        ]
        for l, r, s in valid:
            b = Base(label=l, ref=r, sha=s)
            self.assertEqual(b.label, l)

    def test_resolved_issue_various(self):
        from odoo.addons.aurora.tools.harness.pull_request import ResolvedIssue
        for n in range(1, 51):
            ri = ResolvedIssue(number=n, title=f"Bug {n}", body=f"Desc {n}")
            self.assertEqual(ri.number, n)


@tagged("post_install", "-at_install")
class TestTokenExpanded(TransactionCase):

    def test_batch_create_tokens(self):
        tokens = self.env["aurora.github.token"].sudo().create([
            {"name": f"Tok{i}", "token": f"ghp_{i:04d}",
             "token_hash": hashlib.sha256(f"ghp_{i:04d}".encode()).hexdigest(),
             "state": "draft"}
            for i in range(100)
        ])
        self.assertEqual(len(tokens), 100)

    def test_all_states_writable(self):
        from odoo.addons.aurora.models.github_token import TOKEN_STATES
        tok = self.env["aurora.github.token"].sudo().create({
            "name": "StateTest", "token": "ghp_sttest",
            "token_hash": hashlib.sha256(b"ghp_sttest").hexdigest(),
        })
        for state_key, _ in TOKEN_STATES:
            tok.write({"state": state_key})
            self.assertEqual(tok.state, state_key)

    def test_pool_metrics_repeated(self):
        for _ in range(10):
            self.env["aurora.github.token"]._cron_pool_metrics()
        count = self.env["aurora.pool.metrics"].search_count([])
        self.assertGreaterEqual(count, 10)


@tagged("post_install", "-at_install")
class TestCredentialExpanded(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ICP = cls.env["ir.config_parameter"].sudo()

    def test_encrypt_decrypt_100_values(self):
        from ..models.credential_manager import encrypt_value, decrypt_value
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            for i in range(100):
                val = f"secret-{i}-{'x' * (i % 50)}"
                enc = encrypt_value(self.ICP, val)
                dec = decrypt_value(self.ICP, enc)
                self.assertEqual(dec, val)

    def test_encrypt_all_single_chars(self):
        from ..models.credential_manager import encrypt_value, decrypt_value
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            for c in string.printable:
                enc = encrypt_value(self.ICP, c)
                dec = decrypt_value(self.ICP, enc)
                self.assertEqual(dec, c)

    def test_set_get_param_round_trip_50(self):
        from ..models.credential_manager import set_encrypted_param, get_encrypted_param
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            for i in range(50):
                set_encrypted_param(self.env, "aurora.s3_access_key", f"KEY_{i}")
                result = get_encrypted_param(self.env, "aurora.s3_access_key")
                self.assertEqual(result, f"KEY_{i}")


@tagged("post_install", "-at_install")
class TestS3Expanded(TransactionCase):

    def test_build_s3_key_100_runs(self):
        from ..models.s3_storage import build_s3_key
        for run_num in range(1, 101):
            key = build_s3_key("org", "repo", run_num, "file.jsonl")
            self.assertIn(f"run_{run_num}", key)

    def test_build_base_prefix_various_folders(self):
        from ..models.s3_storage import _build_base_prefix
        folders = ["", "prod", "prod/sub", "/leading/", "trailing/", "/both/"]
        for folder in folders:
            prefix = _build_base_prefix("org", "repo", folder)
            self.assertIn("org__repo", prefix)
            self.assertTrue(prefix.endswith("/"))

    def test_is_configured_all_combos(self):
        from ..models.s3_storage import is_configured
        combos = [
            ({"bucket": "b", "access_key": "a", "secret_key": "s"}, True),
            ({"bucket": "", "access_key": "a", "secret_key": "s"}, False),
            ({"bucket": "b", "access_key": "", "secret_key": "s"}, False),
            ({"bucket": "b", "access_key": "a", "secret_key": ""}, False),
            ({}, False),
            ({"bucket": None, "access_key": "a", "secret_key": "s"}, False),
        ]
        for cfg, expected in combos:
            self.assertEqual(is_configured(cfg), expected, f"Config: {cfg}")


@tagged("post_install", "-at_install")
class TestPipelineExecutorExpanded(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("aurora.output_dir", "/tmp/aurora_test")

    def _create(self, **kw):
        v = {"github_org": "org", "github_repo": "repo"}
        v.update(kw)
        return self.env["aurora.pipeline"].create(v)

    def test_update_pipeline_all_allowed_columns(self):
        from ..models.pipeline_executor import _update_pipeline, _ALLOWED_COLUMNS
        rec = self._create()
        for col in _ALLOWED_COLUMNS:
            if "status" in col:
                _update_pipeline(self.env.cr, rec.id, {col: "done"})
            elif "count" in col:
                _update_pipeline(self.env.cr, rec.id, {col: 42})
            elif col in ("stage",):
                _update_pipeline(self.env.cr, rec.id, {col: "done"})
            elif col in ("last_heartbeat",):
                _update_pipeline(self.env.cr, rec.id, {col: datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            else:
                _update_pipeline(self.env.cr, rec.id, {col: "test_value"})

    def test_append_log_100_entries(self):
        from ..models.pipeline_executor import _append_log
        rec = self._create()
        for i in range(100):
            _append_log(self.env.cr, rec.id, f"Log entry {i}")
        self.env.cr.flush()
        rec.invalidate_recordset()
        self.assertIn("Log entry 99", rec.log)

    def test_validate_step_output_all_steps(self):
        from ..models.pipeline_executor import _validate_step_output
        for step in range(1, 7):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
                f.write('{"key": "value"}\n')
                path = f.name
            try:
                _validate_step_output(path, step)
            finally:
                os.unlink(path)

    def test_count_jsonl_various_sizes(self):
        from ..models.pipeline_executor import _count_jsonl_lines
        for size in [0, 1, 5, 10, 50, 100]:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
                for i in range(size):
                    f.write(f'{{"n": {i}}}\n')
                path = f.name
            try:
                self.assertEqual(_count_jsonl_lines(path), size)
            finally:
                os.unlink(path)
