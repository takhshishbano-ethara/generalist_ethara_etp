"""Tests for ``services/business_days.py`` (pure-Python weekday arithmetic).

Documented semantics (from the module docstring):

* iterates forward counting weekdays only; Sat/Sun never counted;
* ``n=0`` returns the start date unchanged — even on a weekend (no
  normalisation of the start date);
* the start date itself is never counted (Fri + 1 = Mon);
* negative ``n`` raises ``ValueError``.
"""

import datetime

from odoo.tests.common import tagged

from .common import IrisCase
from odoo.addons.iris.services.business_days import add_business_days

# 2026-06-01 is a Monday; 2026-06-05 a Friday; 2026-06-06/07 the weekend.
MONDAY = datetime.date(2026, 6, 1)
FRIDAY = datetime.date(2026, 6, 5)
SATURDAY = datetime.date(2026, 6, 6)
SUNDAY = datetime.date(2026, 6, 7)
NEXT_MONDAY = datetime.date(2026, 6, 8)
NEXT_FRIDAY = datetime.date(2026, 6, 12)


@tagged("post_install", "-at_install", "iris")
class TestBusinessDays(IrisCase):
    def test_friday_plus_five_is_next_friday(self):
        self.assertEqual(add_business_days(FRIDAY, 5), NEXT_FRIDAY)

    def test_monday_plus_five_is_next_monday(self):
        self.assertEqual(add_business_days(MONDAY, 5), NEXT_MONDAY)

    def test_n_zero_returns_start_unchanged(self):
        self.assertEqual(add_business_days(MONDAY, 0), MONDAY)

    def test_n_zero_on_weekend_is_not_normalised(self):
        # Documented behavior: n=0 returns d unchanged even on a weekend.
        self.assertEqual(add_business_days(SATURDAY, 0), SATURDAY)
        self.assertEqual(add_business_days(SUNDAY, 0), SUNDAY)

    def test_saturday_plus_one_is_monday(self):
        # Documented: "Saturday + 1 business day = the next Monday".
        self.assertEqual(add_business_days(SATURDAY, 1), NEXT_MONDAY)

    def test_sunday_plus_one_is_monday(self):
        self.assertEqual(add_business_days(SUNDAY, 1), NEXT_MONDAY)

    def test_saturday_plus_five_is_friday(self):
        self.assertEqual(add_business_days(SATURDAY, 5), NEXT_FRIDAY)

    def test_friday_plus_one_skips_weekend(self):
        # The start date is never counted: Fri + 1 = the following Monday.
        self.assertEqual(add_business_days(FRIDAY, 1), NEXT_MONDAY)

    def test_result_never_lands_on_weekend(self):
        for n in range(1, 15):
            result = add_business_days(MONDAY, n)
            self.assertLess(
                result.weekday(), 5,
                f"add_business_days(MONDAY, {n}) landed on a weekend: {result}",
            )

    def test_negative_n_raises(self):
        with self.assertRaises(ValueError):
            add_business_days(MONDAY, -1)
