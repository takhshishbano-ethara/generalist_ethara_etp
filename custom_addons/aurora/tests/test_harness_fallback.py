import json
import os
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from odoo.addons.aurora.tools.harness_bridge import phase2_docker_build as p2db


class TestTranslatePhase1JsonlFallback(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.in_path = os.path.join(self.tmpdir, "phase1.jsonl")
        self.out_path = os.path.join(self.tmpdir, "harness.jsonl")
        self.summary_path = self.out_path + ".fallback_summary.json"

    def tearDown(self):
        for p in (self.in_path, self.out_path, self.summary_path):
            if os.path.isfile(p):
                os.unlink(p)
        os.rmdir(self.tmpdir)

    def _write_input(self, records):
        with open(self.in_path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def _read_output(self):
        with open(self.out_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_pr_in_interval_keeps_interval(self):
        self._write_input([
            {
                "instance_id": "org__repo-100",
                "tag_start": "v1.0.0", "tag_end": "v1.1.0",
                "pr_numbers": [100, 101],
            },
        ])
        with mock_patch.object(
            p2db, "_build_number_interval_map",
            return_value=[("repo_200_to_50", 50, 200)],
        ):
            n = p2db._translate_phase1_jsonl(self.in_path, "org", "repo", "python", self.out_path)
        self.assertEqual(n, 1)
        out = self._read_output()
        self.assertEqual(out[0]["number_interval"], "repo_200_to_50")
        self.assertFalse(os.path.isfile(self.summary_path))

    def test_pr_outside_intervals_falls_back_to_base(self):
        self._write_input([
            {
                "instance_id": "org__repo-500",
                "tag_start": "v2.0.0", "tag_end": "v2.1.0",
                "pr_numbers": [500],
            },
        ])
        with mock_patch.object(
            p2db, "_build_number_interval_map",
            return_value=[("repo_200_to_50", 50, 200)],
        ):
            n = p2db._translate_phase1_jsonl(self.in_path, "org", "repo", "python", self.out_path)
        self.assertEqual(n, 1, "fallback PR should be WRITTEN, not skipped")
        out = self._read_output()
        self.assertEqual(out[0]["number_interval"], "")
        self.assertTrue(os.path.isfile(self.summary_path))
        with open(self.summary_path) as sf:
            summary = json.load(sf)
        self.assertEqual(summary["org"], "org")
        self.assertEqual(summary["repo"], "repo")
        self.assertEqual(summary["base_fallback_prs"], [500])

    def test_no_intervals_at_all_uses_base(self):
        self._write_input([
            {
                "instance_id": "org__repo-42",
                "tag_start": "v1.0.0", "tag_end": "v1.1.0",
                "pr_numbers": [42],
            },
        ])
        with mock_patch.object(
            p2db, "_build_number_interval_map", return_value=[],
        ):
            n = p2db._translate_phase1_jsonl(self.in_path, "org", "repo", "python", self.out_path)
        self.assertEqual(n, 1)
        out = self._read_output()
        self.assertEqual(out[0]["number_interval"], "")
        self.assertFalse(
            os.path.isfile(self.summary_path),
            "no summary when there are no intervals to fall back FROM",
        )

    def test_mixed_in_and_out_of_interval(self):
        self._write_input([
            {"instance_id": "org__repo-100", "tag_start": "v1", "tag_end": "v2", "pr_numbers": [100]},
            {"instance_id": "org__repo-500", "tag_start": "v3", "tag_end": "v4", "pr_numbers": [500]},
            {"instance_id": "org__repo-150", "tag_start": "v2", "tag_end": "v3", "pr_numbers": [150]},
        ])
        with mock_patch.object(
            p2db, "_build_number_interval_map",
            return_value=[("repo_200_to_50", 50, 200)],
        ):
            n = p2db._translate_phase1_jsonl(self.in_path, "org", "repo", "python", self.out_path)
        self.assertEqual(n, 3)
        out = self._read_output()
        intervals = [r["number_interval"] for r in out]
        self.assertEqual(intervals.count("repo_200_to_50"), 2)
        self.assertEqual(intervals.count(""), 1)
        with open(self.summary_path) as sf:
            summary = json.load(sf)
        self.assertEqual(summary["base_fallback_prs"], [500])

    def test_summary_deduplicates_repeated_prs(self):
        self._write_input([
            {"instance_id": "org__repo-500", "tag_start": "a", "tag_end": "b", "pr_numbers": [500]},
            {"instance_id": "org__repo-500-2", "tag_start": "c", "tag_end": "d", "pr_numbers": [500, 501]},
        ])
        with mock_patch.object(
            p2db, "_build_number_interval_map",
            return_value=[("repo_200_to_50", 50, 200)],
        ):
            p2db._translate_phase1_jsonl(self.in_path, "org", "repo", "python", self.out_path)
        with open(self.summary_path) as sf:
            summary = json.load(sf)
        self.assertEqual(summary["base_fallback_prs"], [500])

    def test_empty_pr_numbers_still_skipped(self):
        self._write_input([
            {"instance_id": "org__repo-empty", "tag_start": "a", "tag_end": "b", "pr_numbers": []},
            {"instance_id": "org__repo-valid", "tag_start": "c", "tag_end": "d", "pr_numbers": [100]},
        ])
        with mock_patch.object(
            p2db, "_build_number_interval_map", return_value=[],
        ):
            n = p2db._translate_phase1_jsonl(self.in_path, "org", "repo", "python", self.out_path)
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
