import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models",
    "import_dataset_normalize.py",
)
_spec = importlib.util.spec_from_file_location("aurora_import_dataset_normalize", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_normalize = _mod.normalize_record


class TestNormalizeRecord(unittest.TestCase):

    def test_teammate_format_with_window_label(self):
        rec = {
            "org": "abi",
            "repo": "screenshot-to-code",
            "number": 562,
            "base": {
                "label": "window:2026-02-26..window:2026-03-10",
                "ref": "main",
                "sha": "1aeb50785eba1de5ec1fc3754d30f6675391e5fc",
            },
            "fix_patch": "diff ...",
            "test_patch": "diff ...",
        }
        _normalize(rec)
        self.assertEqual(rec["tag_start"], "window:2026-02-26")
        self.assertEqual(rec["tag_end"], "window:2026-03-10")
        self.assertEqual(
            rec["instance_id"],
            "abi__screenshot-to-code-window:2026-02-26..window:2026-03-10",
        )
        self.assertEqual(rec["pr_numbers"], [562])
        self.assertEqual(
            rec["pr_url"],
            "https://github.com/abi/screenshot-to-code/pull/562",
        )

    def test_aurora_lht_format_is_idempotent(self):
        rec = {
            "instance_id": "huggingface__lerobot-v0.4.2..v0.4.3",
            "org": "huggingface",
            "repo": "lerobot",
            "number": 2496,
            "tag_start": "v0.4.2",
            "tag_end": "v0.4.3",
            "base": {"label": "main", "ref": "main", "sha": "abc"},
            "pr_numbers": [2496, 2493, 2492],
            "pr_url": "https://github.com/huggingface/lerobot/pull/2496",
            "head": {"sha": "def", "ref": "branch", "label": "branch"},
            "release_line": "0.4",
            "version_scheme": "semver",
        }
        snapshot = dict(rec)
        _normalize(rec)
        for key in ("instance_id", "tag_start", "tag_end", "pr_numbers",
                    "pr_url", "head", "release_line", "version_scheme"):
            self.assertEqual(rec[key], snapshot[key],
                             f"existing {key} should not be overwritten")

    def test_swe_bench_id_overwritten_when_tags_available(self):
        rec = {
            "instance_id": "astropy__astropy-17739",
            "org": "astropy",
            "repo": "astropy",
            "number": 17739,
            "base": {
                "label": "v7.0.1..v7.0.2",
                "ref": "v7.0.x",
                "sha": "c1bf7267",
            },
        }
        _normalize(rec)
        self.assertEqual(rec["instance_id"], "astropy__astropy-v7.0.1..v7.0.2",
                         "tag-derived id should override SWE-bench style")
        self.assertEqual(rec["tag_start"], "v7.0.1")
        self.assertEqual(rec["tag_end"], "v7.0.2")

    def test_swe_bench_single_pr_falls_back_to_number(self):
        rec = {
            "org": "pallets",
            "repo": "flask",
            "number": 1234,
            "base": {"label": "main", "ref": "main", "sha": "abc"},
        }
        _normalize(rec)
        self.assertEqual(rec["instance_id"], "pallets__flask-1234")
        self.assertEqual(rec["pr_numbers"], [1234])

    def test_no_label_no_number_no_instance_id(self):
        rec = {"org": "foo", "repo": "bar"}
        _normalize(rec)
        self.assertNotIn("instance_id", rec)

    def test_base_label_without_double_dot_skipped(self):
        rec = {
            "org": "foo", "repo": "bar", "number": 1,
            "base": {"label": "just-a-branch-name", "ref": "main", "sha": "x"},
        }
        _normalize(rec)
        self.assertNotIn("tag_start", rec)
        self.assertNotIn("tag_end", rec)
        self.assertEqual(rec["instance_id"], "foo__bar-1")

    def test_missing_base_entirely(self):
        rec = {"org": "foo", "repo": "bar", "number": 7}
        _normalize(rec)
        self.assertNotIn("tag_start", rec)
        self.assertEqual(rec["instance_id"], "foo__bar-7")

    def test_existing_tag_start_preserved(self):
        rec = {
            "org": "foo", "repo": "bar", "number": 1,
            "tag_start": "v1.0",
            "tag_end": "v2.0",
            "base": {"label": "window:a..window:b"},
        }
        _normalize(rec)
        self.assertEqual(rec["tag_start"], "v1.0")
        self.assertEqual(rec["tag_end"], "v2.0")
        self.assertEqual(rec["instance_id"], "foo__bar-v1.0..v2.0")

    def test_defaults_populated(self):
        rec = {"org": "foo", "repo": "bar", "number": 1}
        _normalize(rec)
        self.assertEqual(rec["hints"], "")
        self.assertEqual(rec["release_line"], "")
        self.assertEqual(rec["version_scheme"], "")
        self.assertEqual(rec["pr_attribution_method"], "")
        self.assertEqual(rec["number_interval"], "")
        self.assertEqual(rec["head"], {"sha": "", "ref": "", "label": ""})

    def test_prs_in_bundle_takes_precedence_over_number(self):
        rec = {"org": "o", "repo": "r", "number": 1,
               "prs_in_bundle": [10, 11, 12]}
        _normalize(rec)
        self.assertEqual(rec["pr_numbers"], [10, 11, 12])

    def test_empty_prs_in_bundle_falls_back_to_number(self):
        rec = {"org": "o", "repo": "r", "number": 5, "prs_in_bundle": []}
        _normalize(rec)
        self.assertEqual(rec["pr_numbers"], [5])

    def test_hints_text_used_when_hints_missing(self):
        rec = {"org": "o", "repo": "r", "number": 1,
               "hints_text": "be careful"}
        _normalize(rec)
        self.assertEqual(rec["hints"], "be careful")

    def test_existing_hints_preserved_over_hints_text(self):
        rec = {"org": "o", "repo": "r", "number": 1,
               "hints": "primary", "hints_text": "ignored"}
        _normalize(rec)
        self.assertEqual(rec["hints"], "primary")

    def test_existing_pr_numbers_preserved_over_bundle(self):
        rec = {"org": "o", "repo": "r", "number": 1,
               "pr_numbers": [99],
               "prs_in_bundle": [10, 11, 12]}
        _normalize(rec)
        self.assertEqual(rec["pr_numbers"], [99])

    def test_existing_pr_numbers_preserved(self):
        rec = {"org": "o", "repo": "r", "number": 1, "pr_numbers": [10, 20, 30]}
        _normalize(rec)
        self.assertEqual(rec["pr_numbers"], [10, 20, 30])

    def test_existing_head_preserved(self):
        rec = {
            "org": "o", "repo": "r", "number": 1,
            "head": {"sha": "real-sha", "ref": "feature", "label": "feature-branch"},
        }
        _normalize(rec)
        self.assertEqual(rec["head"]["sha"], "real-sha")

    def test_empty_strings_treated_as_missing(self):
        rec = {
            "org": "o", "repo": "r", "number": 99,
            "instance_id": "",
            "tag_start": "",
            "tag_end": "",
            "base": {"label": "v1..v2"},
        }
        _normalize(rec)
        self.assertEqual(rec["tag_start"], "v1")
        self.assertEqual(rec["tag_end"], "v2")
        self.assertEqual(rec["instance_id"], "o__r-v1..v2")

    def test_none_number_no_pr_numbers_filled(self):
        rec = {"org": "o", "repo": "r"}
        _normalize(rec)
        self.assertEqual(rec.get("pr_numbers"), [])
        self.assertEqual(rec.get("pr_url"), "")


if __name__ == "__main__":
    unittest.main()
