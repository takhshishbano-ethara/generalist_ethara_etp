from datetime import date, datetime
from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.crowley_extension.controllers.analytics_dashboard import (
    COLOR_TOKENS,
    HEATMAP_INTENSITY_LEVELS,
    _attempt_scope,
    _color,
    _create_date_domain,
    _diff_pct,
    _fmt_duration,
    _initials,
    _intensity,
    _money,
    _pct as analytics_pct,
    _pct1,
    _period_windows,
)
from odoo.addons.crowley_extension.controllers.dashboard_overview import (
    _kpi_item,
    _pct as overview_pct,
    _week_start,
)
from odoo.addons.crowley_extension.controllers.task_view_dashboard import (
    CATEGORY_SLUG_TO_LABEL,
    _coerce_int,
    _derive_stage,
    _derive_status,
    _iso,
    _or_join,
    _prompts,
    _resolve_category_param,
    _spec_string,
    _status_domain_for,
)


@tagged("post_install", "-at_install", "crowley_extension")
class TestAnalyticsHelpers(TransactionCase):

    def test_pct_zero_whole_returns_zero(self):
        self.assertEqual(analytics_pct(5, 0), 0.0)
        self.assertEqual(analytics_pct(0, 0), 0.0)

    def test_pct_basic(self):
        self.assertEqual(analytics_pct(1, 4), 25.0)
        self.assertEqual(analytics_pct(1, 3), 33.33)
        self.assertEqual(analytics_pct(2, 3), 66.67)

    def test_diff_pct_no_previous_with_current(self):
        self.assertEqual(_diff_pct(10, 0), 100.0)

    def test_diff_pct_no_previous_no_current(self):
        self.assertEqual(_diff_pct(0, 0), 0.0)

    def test_diff_pct_increase(self):
        self.assertEqual(_diff_pct(120, 100), 20.0)

    def test_diff_pct_decrease(self):
        self.assertEqual(_diff_pct(80, 100), -20.0)

    def test_fmt_duration_zero(self):
        self.assertEqual(_fmt_duration(0), "0m 00s")

    def test_fmt_duration_none(self):
        self.assertEqual(_fmt_duration(None), "0m 00s")

    def test_fmt_duration_seconds_only(self):
        self.assertEqual(_fmt_duration(5), "0m 05s")

    def test_fmt_duration_minutes(self):
        self.assertEqual(_fmt_duration(65), "1m 05s")
        self.assertEqual(_fmt_duration(125), "2m 05s")

    def test_fmt_duration_rounds_floats(self):
        self.assertEqual(_fmt_duration(65.4), "1m 05s")
        self.assertEqual(_fmt_duration(65.6), "1m 06s")

    def test_intensity_zero_count(self):
        self.assertEqual(_intensity(0, 10), 0)

    def test_intensity_zero_max(self):
        self.assertEqual(_intensity(5, 0), 0)

    def test_intensity_max_is_full_level(self):
        self.assertEqual(_intensity(10, 10), HEATMAP_INTENSITY_LEVELS)

    def test_intensity_mid_range(self):
        self.assertEqual(_intensity(5, 10), 2)
        self.assertEqual(_intensity(3, 10), 2)

    def test_create_date_domain_both_none(self):
        self.assertEqual(_create_date_domain(None, None), [])

    def test_create_date_domain_start_only(self):
        domain = _create_date_domain(date(2026, 1, 1), None)
        self.assertEqual(len(domain), 1)
        self.assertEqual(domain[0][0], "create_date")
        self.assertEqual(domain[0][1], ">=")
        self.assertEqual(domain[0][2], datetime(2026, 1, 1, 0, 0))

    def test_create_date_domain_both(self):
        domain = _create_date_domain(date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(len(domain), 2)
        self.assertEqual(domain[0][2], datetime(2026, 1, 1, 0, 0))
        self.assertEqual(domain[1][1], "<")
        self.assertEqual(domain[1][2], datetime(2026, 2, 1, 0, 0))

    def test_period_windows_explicit_range(self):
        cur_start, cur_end, prev_start, prev_end = _period_windows(
            date(2026, 1, 10), date(2026, 1, 16)
        )
        self.assertEqual(cur_start, date(2026, 1, 10))
        self.assertEqual(cur_end, date(2026, 1, 16))
        self.assertEqual(prev_end, date(2026, 1, 9))
        self.assertEqual(prev_start, date(2026, 1, 3))
        self.assertEqual((cur_end - cur_start).days, (prev_end - prev_start).days)

    def test_money_zero(self):
        self.assertEqual(_money(0), "$0.00")
        self.assertEqual(_money(None), "$0.00")

    def test_money_thousands(self):
        self.assertEqual(_money(1234.5), "$1,234.50")

    def test_pct1_zero_whole(self):
        self.assertEqual(_pct1(5, 0), 0.0)

    def test_pct1_one_decimal(self):
        self.assertEqual(_pct1(1, 3), 33.3)

    def test_initials_empty(self):
        self.assertEqual(_initials(""), "?")
        self.assertEqual(_initials(None), "?")

    def test_initials_single_word(self):
        self.assertEqual(_initials("Alice"), "AL")

    def test_initials_two_words(self):
        self.assertEqual(_initials("Alice Smith"), "AS")

    def test_initials_three_words_uses_first_and_last(self):
        self.assertEqual(_initials("alice bob carol"), "AC")

    def test_color_cycles(self):
        self.assertEqual(_color(0), COLOR_TOKENS[0])
        self.assertEqual(_color(len(COLOR_TOKENS)), COLOR_TOKENS[0])
        self.assertEqual(_color(len(COLOR_TOKENS) + 2), COLOR_TOKENS[2])

    def test_attempt_scope_rewrites_leaves(self):
        scope = [("user_id", "=", 5), ("state", "in", ["done"])]
        self.assertEqual(
            _attempt_scope(scope),
            [("job_id.user_id", "=", 5), ("job_id.state", "in", ["done"])],
        )

    def test_attempt_scope_preserves_operators(self):
        scope = ["&", ("user_id", "=", 5), "!", ("state", "=", "failed")]
        result = _attempt_scope(scope)
        self.assertEqual(result[0], "&")
        self.assertEqual(result[2], "!")
        self.assertEqual(result[1], ("job_id.user_id", "=", 5))
        self.assertEqual(result[3], ("job_id.state", "=", "failed"))


@tagged("post_install", "-at_install", "crowley_extension")
class TestOverviewHelpers(TransactionCase):

    def test_pct_zero(self):
        self.assertEqual(overview_pct(1, 0), 0.0)

    def test_pct_value(self):
        self.assertEqual(overview_pct(1, 2), 50.0)

    def test_week_start_on_monday(self):
        self.assertEqual(_week_start(date(2026, 1, 5)), date(2026, 1, 5))

    def test_week_start_midweek(self):
        self.assertEqual(_week_start(date(2026, 1, 7)), date(2026, 1, 5))

    def test_week_start_sunday(self):
        self.assertEqual(_week_start(date(2026, 1, 11)), date(2026, 1, 5))

    def test_kpi_item_defaults(self):
        self.assertEqual(
            _kpi_item("k", "L", 5),
            {
                "key": "k",
                "label": "L",
                "value": 5,
                "sub_string": "",
                "pattern": "",
                "sign": "",
            },
        )

    def test_kpi_item_with_overrides(self):
        item = _kpi_item("k", "L", 5, sub_string="sub", pattern="up", sign="+")
        self.assertEqual(item["sub_string"], "sub")
        self.assertEqual(item["pattern"], "up")
        self.assertEqual(item["sign"], "+")


@tagged("post_install", "-at_install", "crowley_extension")
class TestTaskViewHelpers(TransactionCase):

    def test_coerce_int_valid(self):
        self.assertEqual(_coerce_int("5", 0), 5)

    def test_coerce_int_invalid(self):
        self.assertEqual(_coerce_int("x", 7), 7)

    def test_coerce_int_none(self):
        self.assertEqual(_coerce_int(None, 7), 7)

    def test_iso_none(self):
        self.assertEqual(_iso(None), "")

    def test_iso_datetime(self):
        self.assertEqual(_iso(datetime(2026, 1, 1, 12, 30)), "2026-01-01T12:30:00")

    def test_or_join_empty_list(self):
        self.assertEqual(_or_join([]), [])

    def test_or_join_empty_subdomains_are_dropped(self):
        self.assertEqual(_or_join([[], []]), [])

    def test_or_join_single_passthrough(self):
        d = [("state", "=", "done")]
        self.assertEqual(_or_join([d]), d)

    def test_or_join_two_domains(self):
        d1 = [("state", "=", "done")]
        d2 = [("state", "=", "failed")]
        self.assertEqual(
            _or_join([d1, d2]),
            ["|", ("state", "=", "done"), ("state", "=", "failed")],
        )

    def test_or_join_three_domains_has_two_or_operators(self):
        result = _or_join(
            [
                [("a", "=", 1)],
                [("b", "=", 2)],
                [("c", "=", 3)],
            ]
        )
        self.assertEqual(result[:2], ["|", "|"])

    def test_status_domain_unstarted(self):
        self.assertEqual(_status_domain_for("unstarted"), [("state", "=", "draft")])

    def test_status_domain_generating_uses_in(self):
        d = _status_domain_for("generating")
        self.assertEqual(d[0][0], "state")
        self.assertEqual(d[0][1], "in")
        self.assertIn("queued", d[0][2])
        self.assertIn("processing", d[0][2])

    def test_status_domain_pending_review_polish(self):
        d = _status_domain_for("pending_review")
        self.assertEqual(d[0], "&")
        self.assertIn(("state", "=", "done"), d)

    def test_status_domain_approved(self):
        self.assertEqual(
            _status_domain_for("approved"),
            ["&", ("state", "=", "done"), ("review_state", "=", "approved")],
        )

    def test_status_domain_failed_qc_polish(self):
        d = _status_domain_for("failed_qc")
        self.assertEqual(d[0], "|")

    def test_status_domain_unknown(self):
        self.assertEqual(_status_domain_for("__nope__"), [])

    def test_derive_stage_draft(self):
        self.assertEqual(_derive_stage("draft"), ("s1_draft", "S1 Draft"))

    def test_derive_stage_inflight(self):
        for s in ("queued", "submitting", "processing", "downloading"):
            self.assertEqual(_derive_stage(s), ("s2_enriching", "S2 Enriching"))

    def test_derive_stage_done(self):
        self.assertEqual(_derive_stage("done"), ("s2_qc", "S2 QC"))

    def test_derive_stage_failed(self):
        self.assertEqual(_derive_stage("failed"), ("failed", "Failed"))
        self.assertEqual(_derive_stage("cancelled"), ("failed", "Failed"))

    def test_derive_status_matrix(self):
        self.assertEqual(_derive_status("draft", ""), "unstarted")
        self.assertEqual(_derive_status("queued", ""), "generating")
        self.assertEqual(_derive_status("processing", ""), "generating")
        self.assertEqual(_derive_status("done", "approved"), "approved")
        self.assertEqual(_derive_status("done", "rejected"), "failed_qc")
        self.assertEqual(_derive_status("done", "pending"), "pending_review")
        self.assertEqual(_derive_status("done", ""), "pending_review")
        self.assertEqual(_derive_status("failed", ""), "failed_qc")
        self.assertEqual(_derive_status("cancelled", ""), "failed_qc")

    def test_resolve_category_empty(self):
        slugs, err = _resolve_category_param("")
        self.assertIsNone(err)
        self.assertEqual(slugs, [])

    def test_resolve_category_by_slug(self):
        slug = next(iter(CATEGORY_SLUG_TO_LABEL))
        slugs, err = _resolve_category_param(slug)
        self.assertIsNone(err)
        self.assertEqual(slugs, [slug])

    def test_resolve_category_by_label(self):
        slug, label = next(iter(CATEGORY_SLUG_TO_LABEL.items()))
        slugs, err = _resolve_category_param(label)
        self.assertIsNone(err)
        self.assertEqual(slugs, [slug])

    def test_resolve_category_label_case_insensitive(self):
        slug, label = next(iter(CATEGORY_SLUG_TO_LABEL.items()))
        slugs, err = _resolve_category_param(label.upper())
        self.assertIsNone(err)
        self.assertEqual(slugs, [slug])

    def test_resolve_category_invalid(self):
        slugs, err = _resolve_category_param("__no_such_category__")
        self.assertIsNone(slugs)
        self.assertIn("Invalid category", err)

    def test_resolve_category_csv_partial_invalid_rejects(self):
        slug = next(iter(CATEGORY_SLUG_TO_LABEL))
        slugs, err = _resolve_category_param(f"{slug},__bogus__")
        self.assertIsNone(slugs)
        self.assertIn("__bogus__", err)

    def test_spec_string_complete(self):
        m = MagicMock()
        m.resolution = "720p"
        m.aspect_ratio = "16:9"
        m.duration = "5"
        self.assertEqual(_spec_string(m), "720p · 16:9 · 5s")

    def test_spec_string_missing(self):
        m = MagicMock()
        m.resolution = ""
        m.aspect_ratio = ""
        m.duration = ""
        self.assertEqual(_spec_string(m), "- · - · -")

    def test_prompts_only_golden_demoted_to_raw(self):
        m = MagicMock()
        m.original_prompt = ""
        m.prompt = "golden"
        raw, golden = _prompts(m)
        self.assertEqual(raw, "golden")
        self.assertEqual(golden, "")

    def test_prompts_identical_drops_golden(self):
        m = MagicMock()
        m.original_prompt = "x"
        m.prompt = "x"
        raw, golden = _prompts(m)
        self.assertEqual(raw, "x")
        self.assertEqual(golden, "")

    def test_prompts_different_keeps_both(self):
        m = MagicMock()
        m.original_prompt = "raw"
        m.prompt = "golden"
        raw, golden = _prompts(m)
        self.assertEqual(raw, "raw")
        self.assertEqual(golden, "golden")
