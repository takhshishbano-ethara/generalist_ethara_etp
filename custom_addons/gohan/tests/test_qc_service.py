"""Tests for ``services.qc_service`` — structural checks, parsers, summaries."""
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services.qc_service import (
    DEFAULT_QC_SYSTEM_PROMPT,
    _build_report,
    _parse_llm_issues,
    _parse_qc_verdict,
    _run_structural_checks,
    _strip_code_fence,
    _summarize_extraction,
    run_qc,
)


def _valid_prd():
    return (
        "Project: Demo\nCategory: Normal Website\nTarget resolution: 1440x900\n\n"
        "## Product Overview\nIntro text.\n"
        "## Visual & Brand Direction\nColors #111111 #222222.\n"
        "## Technical Ambition\nNext.js 14.\n"
        "## Site Architecture\n### Home\n### About\n"
        "## Motion Language\n240ms cubic-bezier.\n"
        "## Application Logic\nNextAuth.\n"
        "## Accessibility & Quality\nWCAG AA.\n"
        "## Content & SEO\nLighthouse 95.\n"
    ) + (" specification" * 900)


@tagged("post_install", "-at_install", "gohan")
class TestQcStructuralChecks(TransactionCase):

    def test_wordcount_too_short_high(self):
        issues = _run_structural_checks("short prd " * 10, "Normal Website")
        codes = [i["code"] for i in issues]
        self.assertIn("S-WORDCOUNT", codes)
        wc = next(i for i in issues if i["code"] == "S-WORDCOUNT")
        self.assertEqual(wc["severity"], "high")

    def test_wordcount_over_ceiling_high(self):
        text = ("word " * 1800) + _valid_prd()
        issues = _run_structural_checks(text, "Normal Website")
        wc = next(i for i in issues if i["code"] == "S-WORDCOUNT")
        self.assertEqual(wc["severity"], "high")

    def test_wordcount_in_band_no_issue(self):
        # A PRD inside the 800-1500 band raises no S-WORDCOUNT issue.
        issues = _run_structural_checks(_valid_prd(), "Normal Website")
        self.assertFalse([i for i in issues if i["code"] == "S-WORDCOUNT"])

    def test_non_ascii_flagged(self):
        text = _valid_prd() + "\nSpecial chars: \u2013 \u2022 \u2192"
        issues = _run_structural_checks(text, "Normal Website")
        self.assertTrue(any(i["code"] == "S-CHARS" for i in issues))

    def test_missing_sections_critical_when_many(self):
        text = "Word " * 4500 + " Category: Normal\nTarget resolution: 1440px\n"
        issues = _run_structural_checks(text, "Normal Website")
        sec_issues = [i for i in issues if i["code"] == "S-SECTIONS"]
        self.assertTrue(sec_issues)
        self.assertEqual(sec_issues[0]["severity"], "critical")

    def test_banned_phrases_medium(self):
        text = _valid_prd() + (
            "\nThe site has smooth animation, modern UX and sleek design."
        )
        issues = _run_structural_checks(text, "Normal Website")
        self.assertTrue(any(i["code"] == "S-SLOP" for i in issues))

    def test_tables_are_low_severity(self):
        text = _valid_prd() + "\n| col | col |\n| col2 | col2 |\n"
        issues = _run_structural_checks(text, "Normal Website")
        table_issues = [i for i in issues if i["code"] == "S-TABLES"]
        if table_issues:
            self.assertEqual(table_issues[0]["severity"], "low")

    def test_clean_prd_minimal_issues(self):
        issues = _run_structural_checks(_valid_prd(), "Normal Website")
        # Should not have critical/high (apart from S-CHARS quirk none here)
        critical_high = [i for i in issues if i["severity"] in ("critical", "high")]
        self.assertEqual(critical_high, [])


@tagged("post_install", "-at_install", "gohan")
class TestQcLlmParser(TransactionCase):

    def test_parses_fail_rows_from_check_table(self):
        response = (
            "VERDICT: FAIL\n\n"
            "CHECK TABLE:\n"
            "| ID | Check | Status | Evidence |\n"
            "|----|-------|--------|----------|\n"
            "| A1 | No arrow glyph | FAIL | found 'a -> b' |\n"
            "| B5 | Word count band | PASS | 1200 words |\n"
            "| D3 | Target users bullets | FAIL | only one bullet |\n"
            "| G8 | Real-time | N/A | omitted |\n"
        )
        issues = _parse_llm_issues(response)
        # Only the two FAIL rows become issues; PASS / N/A / header skipped.
        self.assertEqual(sorted(i["code"] for i in issues), ["QC-A1", "QC-D3"])

    def test_critical_vs_soft_severity(self):
        response = (
            "CHECK TABLE:\n"
            "| A1 | Arrow | FAIL | x |\n"
            "| D3 | Bullets | FAIL | x |\n"
        )
        sev = {i["code"]: i["severity"] for i in _parse_llm_issues(response)}
        self.assertEqual(sev["QC-A1"], "critical")  # A-class auto-fail
        self.assertEqual(sev["QC-D3"], "medium")    # soft warning

    def test_parser_skips_header_pass_and_alignment_rows(self):
        response = (
            "| ID | Check | Status | Evidence |\n"
            "|----|-------|--------|----------|\n"
            "| A1 | Arrow | PASS | no match |\n"
        )
        self.assertEqual(_parse_llm_issues(response), [])

    def test_verdict_pass(self):
        self.assertEqual(_parse_qc_verdict("VERDICT: PASS\n\nSUMMARY:"), "shippable")

    def test_verdict_fail(self):
        self.assertEqual(
            _parse_qc_verdict("VERDICT: FAIL\n\nSUMMARY:"), "not_shippable"
        )

    def test_verdict_missing(self):
        self.assertEqual(_parse_qc_verdict("no verdict line here"), "")

    def test_strip_code_fence_unwraps(self):
        wrapped = "```\nVERDICT: PASS\nCHECK TABLE:\n```"
        self.assertEqual(
            _strip_code_fence(wrapped), "VERDICT: PASS\nCHECK TABLE:"
        )

    def test_strip_code_fence_passthrough(self):
        plain = "VERDICT: PASS\nno fence here"
        self.assertEqual(_strip_code_fence(plain), plain)


@tagged("post_install", "-at_install", "gohan")
class TestQcSummarize(TransactionCase):

    def test_summarize_with_full_discovery(self):
        sd = {
            "title": "Example",
            "category": "Normal",
            "pages": ["/", "/about"],
            "tech_stack": {"react": {"version": "18"}, "tailwind": {}},
        }
        out = _summarize_extraction({}, sd)
        self.assertIn("Example", out)
        self.assertIn("/about", out)
        self.assertIn("react", out)

    def test_summarize_with_style_data_dict(self):
        data = {
            "style_data": {
                "colors": ["#111", "#222"],
                "fonts": ["Inter"],
            },
        }
        out = _summarize_extraction(data, {})
        self.assertIn("#111", out)
        self.assertIn("Inter", out)

    def test_summarize_with_json_string_payload(self):
        data = {
            "style_data": '{"colors": ["#abc"], "fonts": ["Roboto"]}',
        }
        out = _summarize_extraction(data, {})
        self.assertIn("#abc", out)
        self.assertIn("Roboto", out)

    def test_summarize_empty(self):
        out = _summarize_extraction({}, {})
        self.assertIn("No structured extraction data", out)


@tagged("post_install", "-at_install", "gohan")
class TestQcReportBuilder(TransactionCase):

    def test_build_report_shippable(self):
        report = _build_report(
            verdict="shippable",
            url="https://example.com",
            category="Normal",
            prd_text="word " * 1500,
            structural_issues=[],
            llm_report="All good.",
            all_issues=[],
        )
        self.assertIn("SHIPPABLE", report)
        self.assertIn("https://example.com", report)
        self.assertIn("No issues found", report)

    def test_build_report_not_shippable(self):
        issues = [
            {"severity": "critical", "code": "S-X", "message": "Bad"},
            {"severity": "high", "code": "S-Y", "message": "Worse"},
        ]
        report = _build_report(
            verdict="not_shippable",
            url="https://example.com",
            category="Normal",
            prd_text="word " * 1500,
            structural_issues=issues[:1],
            llm_report="Stuff.",
            all_issues=issues,
        )
        self.assertIn("NOT SHIPPABLE", report)
        self.assertIn("Bad", report)
        self.assertIn("Worse", report)


@tagged("post_install", "-at_install", "gohan")
class TestRunQc(TransactionCase):

    def _patch_llm(self, response):
        return patch(
            "odoo.addons.gohan.services.bedrock_service.generate_prd",
            return_value=response,
        )

    def test_run_qc_shippable_path(self):
        response = (
            "VERDICT: PASS\n\n"
            "SUMMARY:\n- Soft warnings: 0\n\n"
            "CHECK TABLE:\n"
            "| A1 | No arrow glyph | PASS | no match |\n"
        )
        with self._patch_llm(response):
            result = run_qc(
                prd_text=_valid_prd(),
                extraction_data={},
                site_discovery={"title": "Demo"},
                url="https://example.com",
                category="Normal Website",
                inference_arn="arn:test",
                region="us-east-1",
            )
        self.assertEqual(result["verdict"], "shippable")
        self.assertEqual(result["issues_critical"], 0)

    def test_run_qc_fail_verdict_not_shippable(self):
        response = (
            "VERDICT: FAIL\n\n"
            "CHECK TABLE:\n"
            "| A1 | No arrow glyph | FAIL | found 'a -> b' |\n"
        )
        with self._patch_llm(response):
            result = run_qc(
                prd_text=_valid_prd(),
                extraction_data={},
                site_discovery={"title": "Demo"},
                url="https://example.com",
                category="Normal Website",
                inference_arn="arn:test",
                region="us-east-1",
            )
        self.assertEqual(result["verdict"], "not_shippable")
        self.assertGreaterEqual(result["issues_critical"], 1)

    def test_run_qc_llm_failure_fail_closed(self):
        with patch(
            "odoo.addons.gohan.services.bedrock_service.generate_prd",
            side_effect=RuntimeError("Bedrock down"),
        ):
            result = run_qc(
                prd_text=_valid_prd(),
                extraction_data={},
                site_discovery={},
                url="https://example.com",
                category="Normal Website",
                inference_arn="arn:test",
                region="us-east-1",
            )
        self.assertEqual(result["verdict"], "not_shippable")
        self.assertGreaterEqual(result["issues_critical"], 1)

    def test_run_qc_uses_custom_prompt(self):
        captured = {}

        def _fake(inference_arn, region, system_prompt, messages, **_):
            captured["system_prompt"] = system_prompt
            return "VERDICT: PASS\n\nCHECK TABLE:\n| A1 | Arrow | PASS | ok |\n"

        with patch(
            "odoo.addons.gohan.services.bedrock_service.generate_prd",
            side_effect=_fake,
        ):
            run_qc(
                prd_text=_valid_prd(),
                extraction_data={},
                site_discovery={},
                url="https://example.com",
                category="Normal Website",
                inference_arn="arn:test",
                region="us-east-1",
                qc_system_prompt="CUSTOM PROMPT",
            )
        self.assertEqual(captured["system_prompt"], "CUSTOM PROMPT")

    def test_run_qc_falls_back_to_default_prompt(self):
        captured = {}

        def _fake(inference_arn, region, system_prompt, messages, **_):
            captured["system_prompt"] = system_prompt
            return "VERDICT: PASS\n\nCHECK TABLE:\n| A1 | Arrow | PASS | ok |\n"

        with patch(
            "odoo.addons.gohan.services.bedrock_service.generate_prd",
            side_effect=_fake,
        ):
            run_qc(
                prd_text=_valid_prd(),
                extraction_data={},
                site_discovery={},
                url="https://example.com",
                category="Normal Website",
                inference_arn="arn:test",
                region="us-east-1",
                qc_system_prompt="",
            )
        self.assertEqual(captured["system_prompt"], DEFAULT_QC_SYSTEM_PROMPT)
