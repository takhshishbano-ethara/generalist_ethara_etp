"""Batch consistency Machine Summary parsing (v1.1) — and the FAIL-OPEN rule.

Two layers:

* ``verdict_parser.parse_batch_consistency`` (pure): anchors the LAST
  ``### Machine Summary`` heading, takes the next fenced ```json block,
  validates schema + shapes, returns a normalized dict — or ``None`` on
  ANY drift;
* ``iris.screening.batch._llm_on_success``: a ``None`` parse FAILS OPEN —
  the batch still reaches ``done`` with the report stored, a warning in
  the chatter, and NO advisory activities; a valid summary raises advisory
  activities + counters; unknown candidate references are counted but
  silently skipped when raising activities.
"""

import json

from odoo.tests.common import tagged

from .common import DEFAULT_LLM_RESULT, IrisCase, mock_llm
from odoo.addons.iris.services.verdict_parser import (
    BATCH_CONSISTENCY_SCHEMA,
    parse_batch_consistency,
)


def _result(content):
    return dict(DEFAULT_LLM_RESULT, content=content)


def _payload(**overrides):
    """A minimal valid Machine Summary payload (override to corrupt it)."""
    payload = {
        "schema": BATCH_CONSISTENCY_SCHEMA,
        "candidates": [
            {
                "reference": "IRC00001",
                "current_verdict": "ship",
                "revision_recommended": None,
                "inconsistent_flags": [],
                "fraud_signals": [],
            },
        ],
        "inconsistencies": [],
    }
    payload.update(overrides)
    return payload


def _summary_doc(payload, heading="### Machine Summary"):
    """A report-shaped markdown doc ending in a Machine Summary block."""
    return (
        "# Batch Screening Consistency Report\n\n"
        "## 6. Recommendations\n\nThese revisions are advisory.\n\n"
        f"{heading}\n\n"
        "```json\n"
        f"{json.dumps(payload)}\n"
        "```\n"
    )


@tagged("post_install", "-at_install", "iris")
class TestParseBatchConsistency(IrisCase):
    # ------------------------------------------------------------------
    # Pure parser
    # ------------------------------------------------------------------
    def test_valid_fixture_parses(self):
        report = self.VALID_BATCH_REPORT.format(ref1="IRC00001", ref2="IRC00002")
        parsed = parse_batch_consistency(report)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["schema"], BATCH_CONSISTENCY_SCHEMA)
        self.assertEqual(len(parsed["candidates"]), 2)
        self.assertIsNone(parsed["candidates"][0]["revision_recommended"])
        self.assertEqual(parsed["candidates"][1]["revision_recommended"], "hold")
        self.assertEqual(len(parsed["inconsistencies"]), 1)
        finding = parsed["inconsistencies"][0]
        self.assertEqual(finding["flag"], "H4")
        self.assertEqual(finding["fired_on"], ["IRC00001"])
        self.assertEqual(finding["should_fire_on"], ["IRC00002"])

    def test_minimal_valid_payload_parses(self):
        parsed = parse_batch_consistency(_summary_doc(_payload()))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["candidates"][0]["current_verdict"], "ship")

    def test_missing_machine_summary_returns_none(self):
        self.assertIsNone(parse_batch_consistency(self.UNPARSEABLE_BATCH_REPORT))

    def test_empty_input_returns_none(self):
        self.assertIsNone(parse_batch_consistency(""))

    def test_invalid_json_returns_none(self):
        doc = (
            "# Report\n\n### Machine Summary\n\n"
            "```json\n{not valid json,}\n```\n"
        )
        self.assertIsNone(parse_batch_consistency(doc))

    def test_missing_json_fence_returns_none(self):
        doc = "# Report\n\n### Machine Summary\n\nno fenced block here\n"
        self.assertIsNone(parse_batch_consistency(doc))

    def test_wrong_schema_returns_none(self):
        doc = _summary_doc(_payload(schema="iris.batch_consistency.v2"))
        self.assertIsNone(parse_batch_consistency(doc))

    def test_malformed_candidate_entries_return_none(self):
        bad_entries = [
            # missing reference
            {"current_verdict": "ship", "revision_recommended": None},
            # invalid current_verdict
            {"reference": "IRC1", "current_verdict": "approve"},
            # invalid revision value
            {
                "reference": "IRC1",
                "current_verdict": "ship",
                "revision_recommended": "maybe",
            },
            # non-list flags
            {
                "reference": "IRC1",
                "current_verdict": "ship",
                "revision_recommended": None,
                "inconsistent_flags": "H4",
            },
        ]
        for entry in bad_entries:
            doc = _summary_doc(_payload(candidates=[entry]))
            self.assertIsNone(
                parse_batch_consistency(doc),
                f"entry should not parse: {entry}",
            )

    def test_malformed_inconsistencies_return_none(self):
        doc = _summary_doc(_payload(inconsistencies=[
            {"flag": "H4", "fired_on": "IRC1", "should_fire_on": []},
        ]))
        self.assertIsNone(parse_batch_consistency(doc))

    def test_last_machine_summary_heading_wins(self):
        # A spoofed earlier heading (e.g. quoted from a member record) with
        # a bogus block must lose to the report's own trailing summary.
        spoof = _summary_doc(_payload(schema="spoofed"), heading="### Machine Summary")
        real = _summary_doc(_payload())
        parsed = parse_batch_consistency(spoof + "\n" + real)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["schema"], BATCH_CONSISTENCY_SCHEMA)

    # ------------------------------------------------------------------
    # Model integration helpers
    # ------------------------------------------------------------------
    def _batch_in_consistency(self, contents=None):
        batch = self._make_batch()
        batch.action_screen_batch()
        contents = contents or [self.VALID_HOLD_RECORD, self.VALID_SHIP_RECORD]
        with mock_llm(side_effect=[_result(content) for content in contents]):
            self._run_llm_queue()
        self.assertEqual(batch.state, "consistency")
        return batch

    def _advisory_activities(self, candidates):
        return self.env["mail.activity"].search([
            ("res_model", "=", "iris.candidate"),
            ("res_id", "in", candidates.ids),
            ("summary", "like", "Batch consistency advisory"),
        ])

    # ------------------------------------------------------------------
    # Fail-open on unparseable output
    # ------------------------------------------------------------------
    def test_unparseable_summary_fails_open(self):
        batch = self._batch_in_consistency()
        with mock_llm(self.UNPARSEABLE_BATCH_REPORT):
            self._run_llm_queue()

        self.assertEqual(batch.state, "done",
                         "an unparseable machine summary must NOT hold the "
                         "batch hostage")
        self.assertEqual(batch.llm_status, "done")
        self.assertEqual(
            batch.batch_report_markdown, self.UNPARSEABLE_BATCH_REPORT,
            "the human-readable report is still stored",
        )
        self.assertTrue(batch.report_attachment_id)
        self.assertFalse(batch.consistency_findings_json)
        self.assertEqual(batch.inconsistency_count, 0)
        self.assertEqual(batch.revision_advisory_count, 0)
        self.assertTrue(any(
            "unparseable" in body for body in self._chatter_bodies(batch)
        ))
        self.assertFalse(self._advisory_activities(batch.candidate_ids))

    # ------------------------------------------------------------------
    # Valid summary: counters + advisory activities (never auto-applied)
    # ------------------------------------------------------------------
    def test_valid_summary_raises_advisories_and_counters(self):
        batch = self._batch_in_consistency()
        hold_member = batch.candidate_ids.filtered(lambda c: c.state == "hold")
        ship_member = batch.candidate_ids.filtered(lambda c: c.state == "shipped")
        report = self.VALID_BATCH_REPORT.format(
            ref1=hold_member.reference, ref2=ship_member.reference,
        )
        with mock_llm(report):
            self._run_llm_queue()

        self.assertEqual(batch.state, "done")
        self.assertEqual(batch.inconsistency_count, 1)
        self.assertEqual(batch.revision_advisory_count, 1)
        parsed = json.loads(batch.consistency_findings_json)
        self.assertEqual(parsed["schema"], BATCH_CONSISTENCY_SCHEMA)

        # ship_member carries both the revision advisory and the H4 miss →
        # ONE activity with both reasons; hold_member (affirmed) gets none.
        activities = self._advisory_activities(batch.candidate_ids)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.res_id, ship_member.id)
        self.assertEqual(activities.user_id, batch.user_id)
        self.assertIn("Advisory revision", str(activities.note))
        self.assertIn("H4", str(activities.note))

        self.assertTrue(any(
            "Batch consistency advisory" in body
            for body in self._chatter_bodies(ship_member)
        ))
        # ADVISORY ONLY: nobody's verdict or state changed.
        self.assertEqual(ship_member.state, "shipped")
        self.assertEqual(hold_member.state, "hold")

    # ------------------------------------------------------------------
    # Unknown references: counted, but no activities, no crash
    # ------------------------------------------------------------------
    def test_unknown_references_are_skipped_silently(self):
        batch = self._batch_in_consistency(
            contents=[self.VALID_SHIP_RECORD, self.VALID_SHIP_RECORD],
        )
        report = _summary_doc(_payload(
            candidates=[{
                "reference": "IRC99999X",
                "current_verdict": "ship",
                "revision_recommended": "hold",
                "inconsistent_flags": ["H4"],
                "fraud_signals": [1],
            }],
            inconsistencies=[{
                "flag": "H4",
                "fired_on": ["IRC99999X"],
                "should_fire_on": ["IRC99999Y"],
                "evidence": "references nobody in this batch",
            }],
        ))
        with mock_llm(report):
            self._run_llm_queue()

        self.assertEqual(batch.state, "done")
        # The parse succeeded, so the counters reflect the summary...
        self.assertEqual(batch.inconsistency_count, 1)
        self.assertEqual(batch.revision_advisory_count, 1)
        self.assertTrue(batch.consistency_findings_json)
        # ...but unknown references never become activities or chatter.
        self.assertFalse(self._advisory_activities(batch.candidate_ids))
        self.assertEqual(set(batch.candidate_ids.mapped("state")), {"shipped"})
