"""Cross-candidate duplicate-resume detection (P2-10): advisory only.

Contract under test (services/duplicate_detector.py + the
``_detect_duplicate_resumes`` hook at the end of ``_process_resume``):

* exact duplicates are caught via the indexed SHA-256 of the NORMALIZED
  resume text and flagged on BOTH sides (M2M + warning + chatter);
* near duplicates (name-swapped resume) hit at the default 0.90 ratio;
  genuinely different resumes never hit;
* ``iris.dup_similarity_threshold`` and ``iris.dup_scan_limit`` ICPs bound
  the near scan (0 disables it entirely — exact matching stays on);
* the fingerprint lives and dies with the resume (cleared on removal);
* detection is best-effort: a crashing detector NEVER breaks the upload;
* advisory only: candidate ``state`` is never touched.
"""

import base64
from unittest.mock import patch

from odoo.tests.common import tagged

from .common import IrisCase, make_pdf_bytes
from odoo.addons.iris.services import duplicate_detector

#: Long shared body so a name swap stays ≥ 0.90 similar (measured ≈ 0.994).
RESUME_BODY = (
    "Senior ML Engineer with nine years of production experience. Built the "
    "retrieval pipeline serving forty million queries per day at Acme AI. "
    "Cut p99 latency from nine hundred milliseconds to two hundred ten "
    "milliseconds. Trained ranking models at Globex and owned the offline "
    "evaluation harness end to end. Led a team of six engineers across two "
    "sites. Designed the feature store and the nightly batch scoring jobs. "
    "Mentored four junior engineers and ran the hiring loop for the platform "
    "team."
)
TEXT_A = "Jane Doe\n" + RESUME_BODY
TEXT_B = "John Roe\n" + RESUME_BODY
TEXT_OTHER = (
    "Sam Park\nJunior data analyst. Two years of spreadsheet work and "
    "dashboard maintenance at a regional logistics firm."
)


@tagged("post_install", "-at_install", "iris")
class TestDuplicateDetection(IrisCase):
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _upload_pdf(self, candidate, text, filename="cv.pdf"):
        candidate.write({
            "resume_file": base64.b64encode(make_pdf_bytes(text)).decode("ascii"),
            "resume_filename": filename,
        })

    def _candidate_with_text(self, name, text):
        """Candidate with injected resume_text + the detection hook run."""
        candidate = self._make_candidate(name=name, resume_text=text)
        candidate._detect_duplicate_resumes()
        return candidate

    # ------------------------------------------------------------------
    # Service-level invariants
    # ------------------------------------------------------------------
    def test_normalization_defeats_cosmetic_differences(self):
        a = duplicate_detector.normalize_resume_text(
            "[Page 1]\nJane DOE,\nSenior ML-Engineer!",
        )
        b = duplicate_detector.normalize_resume_text(
            "jane doe senior ml engineer",
        )
        self.assertEqual(a, b)
        self.assertEqual(
            duplicate_detector.sha256_of(a), duplicate_detector.sha256_of(b),
        )

    def test_find_near_duplicates_ratio_and_ordering(self):
        norm_a = duplicate_detector.normalize_resume_text(TEXT_A)
        norm_b = duplicate_detector.normalize_resume_text(TEXT_B)
        norm_other = duplicate_detector.normalize_resume_text(TEXT_OTHER)

        hits = duplicate_detector.find_near_duplicates(
            norm_b, [("a", norm_a), ("other", norm_other)],
        )
        self.assertEqual([key for key, _ratio in hits], ["a"])
        self.assertGreaterEqual(hits[0][1], 0.90)

    # ------------------------------------------------------------------
    # Exact duplicate via real PDF upload — flags BOTH sides
    # ------------------------------------------------------------------
    def test_exact_duplicate_flags_both_sides(self):
        pdf_b64 = base64.b64encode(
            make_pdf_bytes("Jane Doe — Senior ML Engineer — Acme AI"),
        ).decode("ascii")
        first = self._make_candidate(
            name="Jane Doe", resume_text=False,
            resume_file=pdf_b64, resume_filename="cv.pdf",
        )
        self.assertTrue(first.resume_sha256)
        self.assertFalse(first.duplicate_candidate_ids)
        self.assertFalse(first.duplicate_warning)

        second = self._make_candidate(
            name="Jane Doe Clone", resume_text=False,
            resume_file=pdf_b64, resume_filename="cv-copy.pdf",
        )

        # Same bytes → same extracted text → same normalized fingerprint.
        self.assertEqual(second.resume_sha256, first.resume_sha256)

        # Both sides carry the M2M link + the warning banner text.
        self.assertEqual(second.duplicate_candidate_ids, first)
        self.assertEqual(first.duplicate_candidate_ids, second)
        self.assertIn("exact match", second.duplicate_warning)
        self.assertIn(first.reference, second.duplicate_warning)
        self.assertIn("exact match", first.duplicate_warning)
        self.assertIn(second.reference, first.duplicate_warning)

        # Chatter on BOTH sides.
        for candidate in (first, second):
            bodies = self._chatter_bodies(candidate)
            self.assertTrue(
                any("Possible duplicate resume" in body for body in bodies),
                f"no duplicate chatter on {candidate.name}: {bodies}",
            )

        # Advisory only: nobody's state moved.
        self.assertEqual(first.state, "draft")
        self.assertEqual(second.state, "draft")

    # ------------------------------------------------------------------
    # Near duplicate (name-swapped) at the default 0.90 threshold
    # ------------------------------------------------------------------
    def test_near_duplicate_name_swapped_resume_detected(self):
        first = self._candidate_with_text("Jane Doe", TEXT_A)
        second = self._candidate_with_text("John Roe", TEXT_B)

        # Different fingerprints — this is the difflib path, not the hash.
        self.assertNotEqual(second.resume_sha256, first.resume_sha256)
        self.assertIn(first, second.duplicate_candidate_ids)
        self.assertIn(second, first.duplicate_candidate_ids)
        self.assertIn("% similar", second.duplicate_warning)
        self.assertEqual(first.state, "draft")
        self.assertEqual(second.state, "draft")

    def test_different_resumes_no_hit(self):
        first = self._candidate_with_text("Jane Doe", TEXT_A)
        second = self._candidate_with_text("Sam Park", TEXT_OTHER)

        self.assertFalse(second.duplicate_candidate_ids)
        self.assertFalse(second.duplicate_warning)
        self.assertFalse(first.duplicate_candidate_ids)
        self.assertFalse(first.duplicate_warning)

    # ------------------------------------------------------------------
    # ICP knobs
    # ------------------------------------------------------------------
    def test_similarity_threshold_icp_respected(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("iris.dup_similarity_threshold", "0.999")

        first = self._candidate_with_text("Jane Doe", TEXT_A)
        second = self._candidate_with_text("John Roe", TEXT_B)
        # ≈0.994 similar — below the tightened 0.999 bar.
        self.assertFalse(second.duplicate_candidate_ids)

        # Back at the default 0.90 the same pair hits.
        ICP.set_param("iris.dup_similarity_threshold", "0.90")
        second._detect_duplicate_resumes()
        self.assertIn(first, second.duplicate_candidate_ids)

    def test_scan_limit_icp_bounds_the_near_scan(self):
        ICP = self.env["ir.config_parameter"].sudo()
        older = self._candidate_with_text("Jane Doe", TEXT_A)
        self._candidate_with_text("Sam Park", TEXT_OTHER)

        # Window of 1 only reaches the (different) middle candidate.
        ICP.set_param("iris.dup_scan_limit", "1")
        newest = self._candidate_with_text("John Roe", TEXT_B)
        self.assertFalse(newest.duplicate_candidate_ids)

        # Widening the window brings the older near-duplicate into range.
        ICP.set_param("iris.dup_scan_limit", "5")
        newest._detect_duplicate_resumes()
        self.assertIn(older, newest.duplicate_candidate_ids)

    def test_scan_limit_zero_disables_near_scan_but_not_exact(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("iris.dup_scan_limit", "0")

        first = self._candidate_with_text("Jane Doe", TEXT_A)
        near = self._candidate_with_text("John Roe", TEXT_B)
        # The near-duplicate scan is off entirely.
        self.assertFalse(near.duplicate_candidate_ids)

        # The indexed exact-hash path still works.
        exact = self._candidate_with_text("Jane Clone", TEXT_A)
        self.assertEqual(exact.duplicate_candidate_ids, first)
        self.assertIn("exact match", exact.duplicate_warning)

    # ------------------------------------------------------------------
    # Hash lifecycle
    # ------------------------------------------------------------------
    def test_hash_lifecycle_on_upload_and_clear(self):
        candidate = self._make_candidate(name="Hash Holder", resume_text=False)
        self.assertFalse(candidate.resume_sha256)

        self._upload_pdf(candidate, "Hash Holder — ML Engineer — Hooli")
        expected = duplicate_detector.sha256_of(
            duplicate_detector.normalize_resume_text(candidate.resume_text),
        )
        self.assertEqual(candidate.resume_sha256, expected)

        # Removing the resume clears the fingerprint + duplicate fields.
        candidate.write({"resume_file": False})
        self.assertFalse(candidate.resume_sha256)
        self.assertFalse(candidate.resume_text)
        self.assertFalse(candidate.duplicate_candidate_ids)
        self.assertFalse(candidate.duplicate_warning)

    # ------------------------------------------------------------------
    # Best-effort: detector crashes never break the upload
    # ------------------------------------------------------------------
    def test_detector_exception_never_breaks_the_upload(self):
        pdf_b64 = base64.b64encode(
            make_pdf_bytes("Crash Test — resilient upload"),
        ).decode("ascii")
        with patch(
            "odoo.addons.iris.services.duplicate_detector.normalize_resume_text",
            side_effect=RuntimeError("detector exploded"),
        ):
            candidate = self._make_candidate(
                name="Crash Test", resume_text=False,
                resume_file=pdf_b64, resume_filename="cv.pdf",
            )

        # The upload itself fully succeeded.
        self.assertIn("resilient upload", candidate.resume_text)
        self.assertTrue(candidate.resume_uploaded_at)
        self.assertEqual(candidate.state, "draft")
        # The detector never got far enough to fingerprint or warn.
        self.assertFalse(candidate.resume_sha256)
        self.assertFalse(candidate.duplicate_candidate_ids)
        self.assertFalse(candidate.duplicate_warning)

        # A later clean upload recovers the fingerprint.
        self._upload_pdf(candidate, "Crash Test — resilient upload v2")
        self.assertTrue(candidate.resume_sha256)
