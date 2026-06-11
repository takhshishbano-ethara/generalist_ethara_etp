"""Business-day arithmetic for Iris HOLD deadlines.

Pure-Python module (no Odoo imports). Weekends are Saturday/Sunday; public
holidays are NOT considered in v1.

Future hook: when holiday awareness is needed, replace the weekend check
with a lookup against an Odoo ``resource.calendar`` (company working
schedule + ``resource.calendar.leaves``), keeping this signature so callers
(``iris.candidate`` hold-deadline computation, the auto-block cron) are
untouched.
"""

from __future__ import annotations

import datetime

#: ``date.weekday()`` values for Saturday (5) and Sunday (6).
_WEEKEND = (5, 6)


def add_business_days(d: datetime.date, n: int) -> datetime.date:
    """Return the date ``n`` business days (Mon-Fri) after ``d``.

    Semantics (exact):

    - Iterates strictly FORWARD one calendar day at a time, counting only
      weekdays (Mon-Fri); Saturdays and Sundays are skipped and never
      counted.
    - ``n=0`` returns ``d`` unchanged — including when ``d`` itself falls
      on a weekend (no normalisation of the start date is performed).
    - The start date ``d`` itself is never counted: ``add_business_days``
      of a Friday with ``n=1`` is the following Monday.
    - Example: Friday + 5 business days = the Friday of the following week;
      Saturday + 1 business day = the next Monday.

    Args:
        d: Start date.
        n: Number of business days to add (must be >= 0).

    Returns:
        datetime.date: The resulting date.

    Raises:
        ValueError: If ``n`` is negative.
    """
    if n < 0:
        raise ValueError(f"add_business_days requires n >= 0, got {n}")

    current = d
    remaining = n
    one_day = datetime.timedelta(days=1)
    while remaining > 0:
        current += one_day
        if current.weekday() not in _WEEKEND:
            remaining -= 1
    return current
