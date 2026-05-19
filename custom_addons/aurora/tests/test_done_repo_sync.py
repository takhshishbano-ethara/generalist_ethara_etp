import time
import unittest
from unittest.mock import patch as mock_patch

from odoo.addons.aurora.models import done_repo_sync as drs


class TestNormalizeRepoName(unittest.TestCase):

    def test_standard_slash(self):
        self.assertEqual(drs._normalize_repo_name("pallets/flask"), "pallets/flask")

    def test_lowercases(self):
        self.assertEqual(drs._normalize_repo_name("Pallets/Flask"), "pallets/flask")

    def test_double_underscore(self):
        self.assertEqual(drs._normalize_repo_name("pallets__flask"), "pallets/flask")

    def test_triple_underscore(self):
        self.assertEqual(drs._normalize_repo_name("jackc___pgx"), "jackc/pgx")

    def test_lang_status_tail_stripped(self):
        self.assertEqual(
            drs._normalize_repo_name("pallets/flask:python:permanent_failure"),
            "pallets/flask",
        )

    def test_lht_final_suffix_NOT_stripped(self):
        self.assertEqual(
            drs._normalize_repo_name("pallets__flask_lht_final"),
            "pallets/flask_lht_final",
        )

    def test_bare_name(self):
        self.assertEqual(drs._normalize_repo_name("flask"), "flask")

    def test_blank(self):
        self.assertIsNone(drs._normalize_repo_name(""))
        self.assertIsNone(drs._normalize_repo_name("   "))

    def test_comment(self):
        self.assertIsNone(drs._normalize_repo_name("# this is a comment"))

    def test_whitespace_trimmed(self):
        self.assertEqual(drs._normalize_repo_name("  pallets/flask  "), "pallets/flask")


class TestStripForFuzzy(unittest.TestCase):

    def test_removes_separators(self):
        self.assertEqual(drs._strip_for_fuzzy("vercel/next.js"), "vercel/nextjs")
        self.assertEqual(drs._strip_for_fuzzy("aws/aws-sdk-js"), "aws/awssdkjs")

    def test_lowercases(self):
        self.assertEqual(drs._strip_for_fuzzy("Pallets-Flask"), "palletsflask")


class TestBuildMatchIndex(unittest.TestCase):

    def test_separates_exact_and_bare(self):
        idx = drs._build_match_index({"pallets/flask", "flask", "vercel/next.js"})
        self.assertIn("pallets/flask", idx.exact)
        self.assertIn("vercel/next.js", idx.exact)
        self.assertIn("flask", idx.bare)
        self.assertNotIn("flask", idx.exact)

    def test_fuzzy_table(self):
        idx = drs._build_match_index({"vercel/next.js"})
        self.assertEqual(idx.fuzzy["vercel/nextjs"], "vercel/next.js")

    def test_empty(self):
        idx = drs._build_match_index(set())
        self.assertEqual(idx.exact, frozenset())
        self.assertEqual(idx.bare, frozenset())
        self.assertEqual(idx.fuzzy, {})


class TestMatchRepo(unittest.TestCase):

    def setUp(self):
        self.idx = drs._build_match_index({
            "pallets/flask",
            "vercel/next.js",
            "celery",
        })

    def test_exact_match(self):
        matched, tier = drs._match_repo("pallets", "flask", self.idx)
        self.assertTrue(matched)
        self.assertEqual(tier, "exact")

    def test_exact_case_insensitive(self):
        matched, tier = drs._match_repo("Pallets", "Flask", self.idx)
        self.assertTrue(matched)
        self.assertEqual(tier, "exact")

    def test_bare_match_by_repo(self):
        matched, tier = drs._match_repo("anyorg", "celery", self.idx)
        self.assertTrue(matched)
        self.assertEqual(tier, "bare")

    def test_bare_match_by_org(self):
        matched, tier = drs._match_repo("celery", "anything", self.idx)
        self.assertTrue(matched)
        self.assertEqual(tier, "bare")

    def test_fuzzy_match(self):
        matched, tier = drs._match_repo("vercel", "nextjs", self.idx)
        self.assertTrue(matched)
        self.assertEqual(tier, "fuzzy")

    def test_no_match(self):
        matched, tier = drs._match_repo("unknown", "repo", self.idx)
        self.assertFalse(matched)
        self.assertEqual(tier, "none")


class TestWrappers(unittest.TestCase):

    def test_is_repo_done(self):
        idx = drs._build_match_index({"pallets/flask"})
        self.assertEqual(drs.is_repo_done("pallets", "flask", idx), (True, "exact"))
        self.assertEqual(drs.is_repo_done("unknown", "repo", idx), (False, "none"))

    def test_is_repo_no_pr(self):
        idx = drs._build_match_index({"torvalds/linux"})
        self.assertEqual(drs.is_repo_no_pr("torvalds", "linux", idx), (True, "exact"))


class TestPickToken(unittest.TestCase):

    def test_single_token(self):
        self.assertEqual(drs._pick_token("ghp_abc123"), "ghp_abc123")

    def test_strips_whitespace(self):
        self.assertEqual(drs._pick_token("  ghp_abc123  "), "ghp_abc123")

    def test_picks_from_pool(self):
        result = drs._pick_token("ghp_a, ghp_b, ghp_c")
        self.assertIn(result, ("ghp_a", "ghp_b", "ghp_c"))

    def test_empty_pool_raises(self):
        with self.assertRaises(ValueError):
            drs._pick_token(", , ,")


class TestCache(unittest.TestCase):

    def setUp(self):
        drs.invalidate_cache()

    def tearDown(self):
        drs.invalidate_cache()

    def test_cache_hit_within_ttl(self):
        gh_repo_mock = unittest.mock.MagicMock()
        gh_repo_mock.get_contents.side_effect = self._fake_contents
        with mock_patch.object(drs, "_get_repo_handle", return_value=gh_repo_mock):
            drs.sync_done_repos("token", "org/repo", "main")
            calls_first = gh_repo_mock.get_contents.call_count
            drs.sync_done_repos("token", "org/repo", "main")
            calls_second = gh_repo_mock.get_contents.call_count
        self.assertEqual(calls_first, calls_second,
                         "second call within TTL should not hit GitHub")

    def test_force_refresh_bypasses_cache(self):
        gh_repo_mock = unittest.mock.MagicMock()
        gh_repo_mock.get_contents.side_effect = self._fake_contents
        with mock_patch.object(drs, "_get_repo_handle", return_value=gh_repo_mock):
            drs.sync_done_repos("token", "org/repo", "main")
            calls_first = gh_repo_mock.get_contents.call_count
            drs.sync_done_repos("token", "org/repo", "main", force_refresh=True)
            calls_second = gh_repo_mock.get_contents.call_count
        self.assertGreater(calls_second, calls_first)

    def test_ttl_expiry(self):
        gh_repo_mock = unittest.mock.MagicMock()
        gh_repo_mock.get_contents.side_effect = self._fake_contents
        with mock_patch.object(drs, "_get_repo_handle", return_value=gh_repo_mock):
            drs.sync_done_repos("token", "org/repo", "main")
            with mock_patch.object(time, "time", return_value=time.time() + drs._CACHE_TTL_SECONDS + 1):
                drs.sync_done_repos("token", "org/repo", "main")
        self.assertGreaterEqual(gh_repo_mock.get_contents.call_count, 4)

    def test_invalidate_cache_specific(self):
        with drs._cache_lock:
            drs._cache["org/repo@main"] = (time.time(), ("dummy", "dummy"))
            drs._cache["other/repo@main"] = (time.time(), ("dummy", "dummy"))
        drs.invalidate_cache("org/repo", "main")
        with drs._cache_lock:
            self.assertNotIn("org/repo@main", drs._cache)
            self.assertIn("other/repo@main", drs._cache)

    @staticmethod
    def _fake_contents(path, ref=None):
        cf = unittest.mock.MagicMock()
        if path == "done_repo.txt":
            cf.decoded_content = b"pallets/flask\nvercel/next.js\n"
        else:
            cf.decoded_content = b"torvalds/linux\n"
        cf.sha = "abc123"
        return cf


if __name__ == "__main__":
    unittest.main()
