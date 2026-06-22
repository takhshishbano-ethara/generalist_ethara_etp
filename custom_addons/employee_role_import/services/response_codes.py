"""HTTP and business response code constants for the employee_role_import API.

Centralising these here keeps controller code uniform and tests robust
against accidental renumbering.
"""

# ── HTTP status codes ────────────────────────────────────────────────────────
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_INTERNAL_ERROR = 500

# ── Business error codes (machine-readable, stable) ──────────────────────────
ERR_VALIDATION = "validation_error"
ERR_DUPLICATE = "duplicate_employee"
ERR_NOT_FOUND = "not_found"
ERR_FORBIDDEN = "forbidden"
ERR_CSV_PARSE = "csv_parse_error"
ERR_CSV_SCHEMA = "csv_schema_error"
ERR_SESSION_EXPIRED = "session_expired"
ERR_SESSION_ALREADY_IMPORTED = "session_already_imported"
ERR_UNKNOWN_ROLE = "unknown_role"
ERR_EXPORT_FAILED = "export_failed"
ERR_DEPENDENCY_MISSING = "missing_dependency"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200
MAX_CSV_BYTES = 5 * 1024 * 1024  # 5 MB hard cap on uploads
