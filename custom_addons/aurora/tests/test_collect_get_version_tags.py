# -*- coding: utf-8 -*-
from unittest import TestCase


class TestParseTag(TestCase):

    def _parse(self, name):
        from odoo.addons.aurora.tools.collect.get_version_tags import parse_tag
        return parse_tag(name)

    # semver
    def test_semver_basic(self):
        r = self._parse("v1.2.3")
        self.assertEqual(r["scheme"], "semver")
        self.assertEqual(r["major"], 1)
        self.assertEqual(r["minor"], 2)
        self.assertEqual(r["patch"], 3)

    def test_semver_no_v(self):
        r = self._parse("1.2.3")
        self.assertEqual(r["scheme"], "semver")

    def test_semver_zero(self):
        r = self._parse("v0.0.0")
        self.assertEqual(r["major"], 0)

    def test_semver_prerelease(self):
        r = self._parse("v1.0.0-rc.1")
        self.assertTrue(r["is_pre_release"])
        self.assertEqual(r["pre_release"], "rc.1")

    def test_semver_alpha(self):
        r = self._parse("v2.0.0-alpha.3")
        self.assertTrue(r["is_pre_release"])

    def test_semver_no_prerelease(self):
        r = self._parse("v3.1.0")
        self.assertFalse(r["is_pre_release"])
        self.assertIsNone(r["pre_release"])

    def test_semver_release_line(self):
        r = self._parse("v1.2.3")
        self.assertEqual(r["release_line"], "1.2")

    def test_semver_large_numbers(self):
        r = self._parse("v100.200.300")
        self.assertEqual(r["major"], 100)
        self.assertEqual(r["minor"], 200)
        self.assertEqual(r["patch"], 300)

    def test_semver_build_metadata(self):
        r = self._parse("v1.0.0+build.123")
        self.assertEqual(r["scheme"], "semver")
        self.assertEqual(r["major"], 1)

    def test_semver_pre_and_build(self):
        r = self._parse("v1.0.0-beta.1+build.42")
        self.assertTrue(r["is_pre_release"])

    # calver
    def test_calver_basic(self):
        r = self._parse("2024.01")
        self.assertEqual(r["scheme"], "calver")
        self.assertEqual(r["major"], 2024)
        self.assertEqual(r["minor"], 1)

    def test_calver_with_day(self):
        r = self._parse("2024.01.15")
        self.assertEqual(r["scheme"], "calver")

    def test_calver_short_year(self):
        r = self._parse("24.1")
        self.assertEqual(r["scheme"], "calver")
        self.assertEqual(r["major"], 2024)

    def test_calver_prerelease(self):
        r = self._parse("2024.01-rc.1")
        self.assertTrue(r["is_pre_release"])

    def test_calver_no_prerelease(self):
        r = self._parse("2024.06")
        self.assertFalse(r["is_pre_release"])

    def test_calver_release_line(self):
        r = self._parse("2024.03")
        self.assertEqual(r["release_line"], "2024.3")

    def test_calver_with_v(self):
        r = self._parse("v2024.01")
        self.assertEqual(r["scheme"], "calver")

    def test_calver_month_12(self):
        r = self._parse("2024.12")
        self.assertEqual(r["minor"], 12)

    def test_calver_day_31(self):
        r = self._parse("2024.01.31")
        self.assertEqual(r["scheme"], "calver")

    # unknown
    def test_unknown_random_string(self):
        r = self._parse("some-random-tag")
        self.assertEqual(r["scheme"], "unknown")

    def test_unknown_release_line(self):
        r = self._parse("garbage")
        self.assertEqual(r["release_line"], "unknown")

    def test_unknown_prerelease_detection(self):
        r = self._parse("my-project-beta.1")
        self.assertTrue(r["is_pre_release"])

    def test_unknown_no_prerelease(self):
        r = self._parse("stable-release")
        self.assertFalse(r["is_pre_release"])

    # prefix stripping
    def test_release_prefix_stripped(self):
        r = self._parse("release/v1.2.3")
        self.assertEqual(r["scheme"], "semver")
        self.assertEqual(r["major"], 1)

    def test_hotfix_prefix_stripped(self):
        r = self._parse("hotfix/v1.0.1")
        self.assertEqual(r["scheme"], "semver")

    def test_version_prefix_stripped(self):
        r = self._parse("version-1.2.3")
        self.assertEqual(r["scheme"], "semver")

    # sort_key
    def test_sort_key_is_tuple(self):
        r = self._parse("v1.0.0")
        self.assertIsInstance(r["sort_key"], tuple)

    def test_semver_sorts_before_calver(self):
        s = self._parse("v1.0.0")
        c = self._parse("2024.01")
        self.assertLess(s["sort_key"], c["sort_key"])

    def test_semver_ordering(self):
        a = self._parse("v1.0.0")
        b = self._parse("v1.1.0")
        c = self._parse("v2.0.0")
        self.assertLess(a["sort_key"], b["sort_key"])
        self.assertLess(b["sort_key"], c["sort_key"])

    # pre-release identifiers
    def test_all_prerelease_identifiers_detected(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _PRE_RELEASE_IDENTIFIERS
        for ident in _PRE_RELEASE_IDENTIFIERS:
            r = self._parse(f"tag-{ident}.1")
            self.assertTrue(
                r["is_pre_release"],
                f"Expected {ident} to be detected as pre-release"
            )

    # parametric
    def test_many_semver_tags(self):
        for major in range(5):
            for minor in range(5):
                for patch_v in range(3):
                    r = self._parse(f"v{major}.{minor}.{patch_v}")
                    self.assertEqual(r["scheme"], "semver")
                    self.assertEqual(r["major"], major)
                    self.assertEqual(r["minor"], minor)
                    self.assertEqual(r["patch"], patch_v)


class TestRegexPatterns(TestCase):

    def test_semver_re_matches(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _SEMVER_RE
        self.assertTrue(_SEMVER_RE.match("1.0.0"))
        self.assertTrue(_SEMVER_RE.match("v1.0.0"))
        self.assertTrue(_SEMVER_RE.match("v1.0.0-rc.1"))

    def test_semver_re_no_match(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _SEMVER_RE
        self.assertIsNone(_SEMVER_RE.match("abc"))
        self.assertIsNone(_SEMVER_RE.match("1.0"))

    def test_calver_re_matches(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _CALVER_RE
        self.assertTrue(_CALVER_RE.match("2024.01"))
        self.assertTrue(_CALVER_RE.match("2024.01.15"))
        self.assertTrue(_CALVER_RE.match("24.1"))

    def test_calver_re_no_match(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _CALVER_RE
        self.assertIsNone(_CALVER_RE.match("abc"))
        self.assertIsNone(_CALVER_RE.match("1.2.3"))

    def test_prefix_re_matches(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _PREFIX_RE
        self.assertTrue(_PREFIX_RE.match("release/v1.0"))
        self.assertTrue(_PREFIX_RE.match("hotfix/v1.0"))
        self.assertTrue(_PREFIX_RE.match("version-1.0"))

    def test_prefix_re_case_insensitive(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _PREFIX_RE
        self.assertTrue(_PREFIX_RE.match("Release/v1.0"))
        self.assertTrue(_PREFIX_RE.match("HOTFIX/v1.0"))

    def test_prerelease_identifiers_frozenset(self):
        from odoo.addons.aurora.tools.collect.get_version_tags import _PRE_RELEASE_IDENTIFIERS
        self.assertIsInstance(_PRE_RELEASE_IDENTIFIERS, frozenset)
        self.assertIn("alpha", _PRE_RELEASE_IDENTIFIERS)
        self.assertIn("beta", _PRE_RELEASE_IDENTIFIERS)
        self.assertIn("rc", _PRE_RELEASE_IDENTIFIERS)
        self.assertIn("dev", _PRE_RELEASE_IDENTIFIERS)
