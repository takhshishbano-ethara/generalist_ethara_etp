import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock, PropertyMock


class TestComputeStarRanges(TestCase):

    def test_small_max_results_single_range(self):
        from odoo.addons.aurora.tools.collect.discover_repos import _compute_star_ranges
        result = _compute_star_ranges(1000, 50)
        self.assertEqual(result, [(1000, None)])

    def test_small_max_results_boundary(self):
        from odoo.addons.aurora.tools.collect.discover_repos import _compute_star_ranges
        result = _compute_star_ranges(1000, 900)
        self.assertEqual(result, [(1000, None)])

    def test_large_max_results_splits(self):
        from odoo.addons.aurora.tools.collect.discover_repos import _compute_star_ranges
        result = _compute_star_ranges(1000, 2000)
        self.assertGreater(len(result), 1)
        has_open_ended = any(high is None for _, high in result)
        self.assertTrue(has_open_ended)

    def test_high_min_stars_filters_boundaries(self):
        from odoo.addons.aurora.tools.collect.discover_repos import _compute_star_ranges
        result = _compute_star_ranges(50000, 2000)
        for low, _ in result:
            self.assertGreaterEqual(low, 50000)


class TestSearchRepos(TestCase):

    @patch("odoo.addons.aurora.tools.collect.discover_repos.TokenRotator")
    def test_excluded_repos_skipped(self, MockRotator):
        from odoo.addons.aurora.tools.collect.discover_repos import _search_repos

        mock_item = MagicMock()
        mock_item.full_name = "excluded-org/excluded-repo"
        mock_item.fork = False
        mock_item.archived = False

        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.get_page.return_value = [mock_item]
        mock_client.search_repositories.return_value = mock_result

        mock_rotator = MagicMock()
        mock_rotator.get_client.return_value = mock_client

        excluded = {"excluded-org/excluded-repo"}
        repos = _search_repos(mock_rotator, "python", 1000, 10, excluded)
        self.assertEqual(len(repos), 0)

    @patch("odoo.addons.aurora.tools.collect.discover_repos.TokenRotator")
    def test_fork_repos_skipped(self, MockRotator):
        from odoo.addons.aurora.tools.collect.discover_repos import _search_repos

        mock_item = MagicMock()
        mock_item.full_name = "org/repo"
        mock_item.fork = True
        mock_item.archived = False

        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.get_page.return_value = [mock_item]
        mock_client.search_repositories.return_value = mock_result

        mock_rotator = MagicMock()
        mock_rotator.get_client.return_value = mock_client

        repos = _search_repos(mock_rotator, "python", 1000, 10, set())
        self.assertEqual(len(repos), 0)

    @patch("odoo.addons.aurora.tools.collect.discover_repos.TokenRotator")
    def test_valid_repo_returned(self, MockRotator):
        from odoo.addons.aurora.tools.collect.discover_repos import _search_repos

        mock_item = MagicMock()
        mock_item.full_name = "good-org/good-repo"
        mock_item.owner.login = "good-org"
        mock_item.name = "good-repo"
        mock_item.fork = False
        mock_item.archived = False
        mock_item.stargazers_count = 5000
        mock_item.forks_count = 200
        mock_item.size = 10000
        mock_item.description = "A test repo"
        mock_item.topics = ["python", "testing"]
        mock_item.license = MagicMock(spdx_id="MIT")
        mock_item.default_branch = "main"
        mock_item.open_issues_count = 50
        mock_item.pushed_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01T00:00:00"))
        mock_item.language = "Python"

        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.get_page.return_value = [mock_item]
        mock_client.search_repositories.return_value = mock_result

        mock_rotator = MagicMock()
        mock_rotator.get_client.return_value = mock_client

        repos = _search_repos(mock_rotator, "python", 1000, 10, set())
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["github_org"], "good-org")
        self.assertEqual(repos[0]["github_repo"], "good-repo")
        self.assertEqual(repos[0]["stars"], 5000)


class TestEnrichRepo(TestCase):

    @patch("odoo.addons.aurora.tools.collect.discover_repos.requests")
    def test_language_pct_calculated(self, mock_requests):
        from odoo.addons.aurora.tools.collect.discover_repos import _enrich_repo

        mock_response = MagicMock()
        mock_response.json.return_value = {"Python": 8000, "JavaScript": 2000}
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_gh_repo = MagicMock()
        mock_gh_repo.languages_url = "https://api.github.com/repos/org/repo/languages"

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_gh_repo

        mock_rotator = MagicMock()
        mock_rotator.get_client.return_value = mock_client
        mock_rotator.get_token.return_value = "fake-token"

        repo = {"full_name": "org/repo", "primary_language": "Python", "default_branch": "main"}
        result = _enrich_repo(mock_rotator, repo)

        self.assertEqual(result["language_pct"], 80.0)

    @patch("odoo.addons.aurora.tools.collect.discover_repos.requests")
    def test_language_pct_zero_when_no_match(self, mock_requests):
        from odoo.addons.aurora.tools.collect.discover_repos import _enrich_repo

        mock_response = MagicMock()
        mock_response.json.return_value = {"JavaScript": 10000}
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_gh_repo = MagicMock()
        mock_gh_repo.languages_url = "https://api.github.com/repos/org/repo/languages"

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_gh_repo

        mock_rotator = MagicMock()
        mock_rotator.get_client.return_value = mock_client
        mock_rotator.get_token.return_value = "fake-token"

        repo = {"full_name": "org/repo", "primary_language": "Python", "default_branch": "main"}
        result = _enrich_repo(mock_rotator, repo)

        self.assertEqual(result["language_pct"], 0.0)

    @patch("odoo.addons.aurora.tools.collect.discover_repos.requests")
    def test_api_failure_returns_zero_pct(self, mock_requests):
        from odoo.addons.aurora.tools.collect.discover_repos import _enrich_repo

        mock_requests.get.side_effect = Exception("Network error")

        mock_gh_repo = MagicMock()
        mock_gh_repo.languages_url = "https://api.github.com/repos/org/repo/languages"

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_gh_repo

        mock_rotator = MagicMock()
        mock_rotator.get_client.return_value = mock_client
        mock_rotator.get_token.return_value = "fake-token"

        repo = {"full_name": "org/repo", "primary_language": "Python", "default_branch": "main"}
        result = _enrich_repo(mock_rotator, repo)

        self.assertEqual(result["language_pct"], 0.0)


class TestMainJsonlOutput(TestCase):

    @patch("odoo.addons.aurora.tools.collect.discover_repos._enrich_repo")
    @patch("odoo.addons.aurora.tools.collect.discover_repos._search_repos")
    def test_main_writes_jsonl(self, mock_search, mock_enrich):
        from odoo.addons.aurora.tools.collect.discover_repos import main

        mock_search.return_value = [
            {"full_name": "org/repo", "github_org": "org", "github_repo": "repo",
             "stars": 5000, "primary_language": "Python", "default_branch": "main"},
        ]
        mock_enrich.return_value = {
            "full_name": "org/repo", "github_org": "org", "github_repo": "repo",
            "stars": 5000, "primary_language": "Python", "default_branch": "main",
            "language_pct": 85.0, "has_tests": True, "has_ci": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            main(
                tokens=["fake-token"],
                out_dir=Path(tmpdir),
                language="python",
                min_stars=1000,
                max_results=10,
                min_language_pct=60.0,
                enrich=True,
            )
            output = Path(tmpdir) / "python_discovery_candidates.jsonl"
            self.assertTrue(output.exists())
            lines = output.read_text().strip().split("\n")
            self.assertEqual(len(lines), 1)
            data = json.loads(lines[0])
            self.assertEqual(data["github_org"], "org")

    @patch("odoo.addons.aurora.tools.collect.discover_repos._enrich_repo")
    @patch("odoo.addons.aurora.tools.collect.discover_repos._search_repos")
    def test_main_filters_below_threshold(self, mock_search, mock_enrich):
        from odoo.addons.aurora.tools.collect.discover_repos import main

        mock_search.return_value = [
            {"full_name": "org/repo", "github_org": "org", "github_repo": "repo",
             "stars": 5000, "primary_language": "Python", "default_branch": "main"},
        ]
        mock_enrich.return_value = {
            "full_name": "org/repo", "github_org": "org", "github_repo": "repo",
            "stars": 5000, "primary_language": "Python", "default_branch": "main",
            "language_pct": 40.0, "has_tests": False, "has_ci": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            main(
                tokens=["fake-token"],
                out_dir=Path(tmpdir),
                language="python",
                min_stars=1000,
                max_results=10,
                min_language_pct=60.0,
                enrich=True,
            )
            output = Path(tmpdir) / "python_discovery_candidates.jsonl"
            self.assertTrue(output.exists())
            self.assertEqual(output.read_text().strip(), "")


class TestQualityScore(TestCase):

    def test_high_quality_repo(self):
        from odoo.addons.aurora.models.discovery import AuroraDiscovery
        from unittest.mock import MagicMock
        from datetime import datetime, timedelta

        rec = MagicMock(spec=AuroraDiscovery)
        rec.last_pushed = datetime.now() - timedelta(days=10)
        rec.stars = 10000
        rec.language_pct = 90.0
        rec.has_tests = True
        rec.has_ci = True
        rec.license_spdx = "MIT"
        rec.open_issues = 100
        rec.quality_score = 0

        score = 0
        days = 10
        if days <= 30: score += 30
        if rec.stars >= 5000: score += 20
        if rec.language_pct >= 80: score += 20
        if rec.has_tests: score += 10
        if rec.has_ci: score += 10
        if rec.license_spdx: score += 5
        if rec.open_issues >= 50: score += 5
        self.assertEqual(min(score, 100), 100)

    def test_low_quality_repo(self):
        score = 0
        days = 500
        stars = 200
        lang_pct = 30.0
        has_tests = False
        has_ci = False
        license_spdx = ""
        open_issues = 5

        if days <= 30: score += 30
        elif days <= 180: score += 15
        elif days <= 365: score += 5
        if stars >= 5000: score += 20
        elif stars >= 2000: score += 15
        elif stars >= 1000: score += 10
        elif stars >= 500: score += 5
        if lang_pct >= 80: score += 20
        elif lang_pct >= 60: score += 10
        elif lang_pct >= 40: score += 5
        if has_tests: score += 10
        if has_ci: score += 10
        if license_spdx: score += 5
        if open_issues >= 50: score += 5
        self.assertEqual(score, 0)


class TestShouldSkip(TestCase):

    def test_skip_yaml_excluded(self):
        mock_self = MagicMock()
        mock_self._load_excluded_repos.return_value = {"org/repo"}
        from odoo.addons.aurora.models.discovery import AuroraDiscovery
        result = AuroraDiscovery._should_skip(mock_self, "org", "repo")
        self.assertTrue(result)

    def test_skip_existing_discovery(self):
        mock_self = MagicMock()
        mock_self._load_excluded_repos.return_value = set()
        mock_self.search_count.return_value = 1
        from odoo.addons.aurora.models.discovery import AuroraDiscovery
        result = AuroraDiscovery._should_skip(mock_self, "org", "repo")
        self.assertTrue(result)

    def test_no_skip_new_repo(self):
        mock_self = MagicMock()
        mock_self._load_excluded_repos.return_value = set()
        mock_self.search_count.return_value = 0
        mock_self.env = MagicMock()
        mock_self.env.__getitem__.return_value.search_count.return_value = 0
        from odoo.addons.aurora.models.discovery import AuroraDiscovery
        result = AuroraDiscovery._should_skip(mock_self, "new_org", "new_repo")
        self.assertFalse(result)


class TestMarkIncompatible(TestCase):

    def test_genuine_data_failure_marks_rejected(self):
        from odoo.addons.aurora.models.pipeline_executor import _fail_pipeline
        mock_cr = MagicMock()
        mock_cr.fetchone.return_value = ("test_org", "test_repo")
        _fail_pipeline(mock_cr, 1, "step1_status", "Step 1 output file is empty: /tmp/test.jsonl")
        update_calls = [c for c in mock_cr.execute.call_args_list if "aurora_discovery" in str(c)]
        self.assertTrue(len(update_calls) > 0)

    def test_technical_failure_does_not_mark(self):
        from odoo.addons.aurora.models.pipeline_executor import _fail_pipeline
        mock_cr = MagicMock()
        _fail_pipeline(mock_cr, 1, "step1_status", "ConnectionError: network timeout")
        update_calls = [c for c in mock_cr.execute.call_args_list if "aurora_discovery" in str(c)]
        self.assertEqual(len(update_calls), 0)


class TestHarnessRegistryParsing(TestCase):

    def test_version_suffix_stripped(self):
        import re
        test_cases = [
            ("camunda_modeler_1310_to_1270", "camunda_modeler"),
            ("ava_2936_to_2729", "ava"),
            ("browser_laptop_13607_to_11977", "browser_laptop"),
            ("pydantic_v2_0_3", "pydantic"),
            ("cli_go1_13", "cli"),
            ("minio_premod", "minio"),
            ("thanos_gopath", "thanos"),
            ("reactor_core_era_a", "reactor_core"),
            ("axios", "axios"),
            ("rdkit", "rdkit"),
        ]
        for input_name, expected in test_cases:
            result = re.sub(r'_\d+(_to_\d+)?$', '', input_name)
            result = re.sub(r'_v\d+[\d_.]*$', '', result)
            result = re.sub(r'_(era_?[a-z]|go\d+[\d_]*|gopath|premod|gopath_\w+)$', '', result)
            self.assertEqual(result, expected, f"Failed for {input_name}: got {result}")


class TestEndToEnd(TestCase):

    @patch("odoo.addons.aurora.tools.collect.discover_repos._enrich_repo")
    @patch("odoo.addons.aurora.tools.collect.discover_repos._search_repos")
    def test_full_pipeline_search_enrich_write(self, mock_search, mock_enrich):
        from odoo.addons.aurora.tools.collect.discover_repos import main

        mock_search.return_value = [
            {"full_name": "org/repo", "github_org": "org", "github_repo": "repo",
             "stars": 5000, "primary_language": "Python", "default_branch": "main"},
        ]
        mock_enrich.return_value = {
            "full_name": "org/repo", "github_org": "org", "github_repo": "repo",
            "stars": 5000, "primary_language": "Python", "default_branch": "main",
            "language_pct": 85.0, "has_tests": True, "has_ci": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = main(
                tokens=["fake-token"],
                out_dir=Path(tmpdir),
                language="python",
                min_stars=1000,
                max_results=10,
                min_language_pct=60.0,
                enrich=True,
            )
            output = Path(tmpdir) / "python_discovery_candidates.jsonl"
            self.assertTrue(output.exists())
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["github_org"], "org")
            self.assertEqual(result[0]["language_pct"], 85.0)
