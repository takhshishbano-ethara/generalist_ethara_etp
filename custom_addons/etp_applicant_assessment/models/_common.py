import pytz

# All datetimes are stored in UTC (Odoo convention). API responses convert
# them to IST at serialization time only — never at write time.
IST_TZ = pytz.timezone("Asia/Kolkata")


def to_ist(dt):
    """Return the given naive-UTC datetime as an IST-aware datetime.

    Falsy input (None/False) is returned unchanged so callers can keep
    their existing ``if value`` guards.
    """
    if not dt:
        return dt
    return pytz.utc.localize(dt).astimezone(IST_TZ)


QUESTION_TYPE_SELECTION = [
    ("mcq_single", "Single-choice"),
    ("mcq_multi", "Multi-choice"),
    ("true_false", "True / False"),
    ("dropdown", "Dropdown"),
    ("text", "Short Text"),
    ("long_text", "Long Text"),
    ("fill_blank", "Fill in the Blank"),
    ("numeric", "Numeric Answer"),
    ("code", "Code"),
    ("file_upload", "File Upload"),
    ("url", "URL Submission"),
    ("matching", "Matching"),
    ("ordering", "Ordering / Ranking"),
    ("rating", "Rating (Likert)"),
    ("date", "Date"),
    ("consent", "Consent Checkbox"),
]

MCQ_TYPES = ("mcq_single", "mcq_multi", "dropdown", "true_false")

OPTION_BEARING_TYPES = (
    "mcq_single", "mcq_multi", "dropdown", "true_false",
    "matching", "ordering", "rating",
)

OPTION_STORAGE_TYPES = MCQ_TYPES + ("rating",)

MANUAL_REVIEW_TYPES = ("text", "long_text", "url")

VALID_QUESTION_TYPE_KEYS = {k for k, _ in QUESTION_TYPE_SELECTION}
