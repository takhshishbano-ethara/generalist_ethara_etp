"""CSV import service: parse, validate, preview, commit.

This is the API-facing analogue of ``EmployeeRoleImportWizard``.

Design notes
------------
* The wizard owns the form-driven flow (transient model + onchange).  This
  service owns the REST flow and persists state in
  ``employee.import.session``.  They share *constants* (ROLE_GROUP_MAP,
  ROLE_DEFAULT_PARENT_ROLE) from ``models.hr_employee`` but not code -
  the wizard stays untouched so the UI cannot regress when the API grows.
* All validation happens in :meth:`build_preview` so the client gets
  ALL row-level errors in one pass and can surface them in the UI.
* :meth:`commit` re-validates and is idempotent on
  ``state in ('imported', 'failed')``.

Validation rules (matches requirements)
---------------------------------------
* ``employee_id`` and ``email`` are mandatory.
* ``employee_id`` is unique (case-insensitive) - duplicates within the
  CSV are flagged, duplicates against the DB trigger an UPDATE flow
  (no second row is created).
* Dedup match priority: ``employee_id`` first, then ``email`` fallback.
"""

import base64
import csv
import io
import json
import logging
import re

from odoo import _
from odoo.exceptions import UserError

from ..models.hr_employee import (
    ROLE_DEFAULT_PARENT_ROLE,
    ROLE_GROUP_MAP,
    ROLE_HIERARCHY_FIELDS,
    ROLE_LEVEL,
    ROLE_SELECTION,
)
from .employee_repository import EmployeeRepository
from .response_codes import MAX_CSV_BYTES

_logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

EMPLOYEE_CODE_RE = re.compile(r"^[A-Z]{2,6}\d+$")

REQUIRED_COLUMNS = ("name", "employee_id", "email")
OPTIONAL_COLUMNS = (
    "role",
    "job_title",
    "assigned_ql",
    "assigned_pl",
    "assigned_tpm",
    "assigned_ql_email",
    "assigned_pl_email",
    "assigned_tpm_email",
    "reports_to",
    "manager",
    "manager_email",
)

ROLE_LABELS = dict(ROLE_SELECTION)


class CsvParseError(UserError):
    """Raised when the CSV cannot even be opened (bad encoding, missing cols)."""


class CsvImportService:
    """Service orchestrating CSV parse → preview → commit."""

    def __init__(self, env):
        self.env = env
        self.repo = EmployeeRepository(env)

    # ── public entry points ──────────────────────────────────────────────────
    def create_session(
        self,
        raw_bytes: bytes,
        filename: str = "",
        default_role: str = "",
        create_user: bool = True,
        default_password: str = "Ethara@123",
    ):
        """Persist the upload and return the session record."""
        if not raw_bytes:
            raise CsvParseError(_("Uploaded file is empty."))
        if len(raw_bytes) > MAX_CSV_BYTES:
            raise CsvParseError(
                _("Uploaded file exceeds the %d KB limit.") % (MAX_CSV_BYTES // 1024)
            )
        if default_role:
            resolved = self._resolve_role_key(default_role)
            if resolved is None:
                raise CsvParseError(_("Unknown default role '%s'.") % default_role)
            default_role = resolved
            if default_role not in self.env["hr.employee"]._current_user_assignable_roles():
                raise CsvParseError(
                    _("You are not allowed to assign the '%s' role.") % default_role
                )
        else:
            default_role = False

        session = self.env["employee.import.session"].sudo().create({
            "csv_filename": filename or "import.csv",
            "raw_csv": base64.b64encode(raw_bytes),
            "default_role": default_role or False,
            "create_user": bool(create_user),
            "default_password": default_password or "Ethara@123",
            "user_id": self.env.uid,
        })
        return session

    def build_preview(self, session) -> dict:
        """Parse the session's CSV and return a preview payload.

        The payload caches the rows so ``commit`` doesn't need to re-parse.
        Refuses to re-preview a session in a terminal state - callers
        should use the cached ``preview_payload`` instead.
        """
        session.assert_owner(self.env.user)
        if session.state in ("imported", "discarded", "failed"):
            raise UserError(
                _("Session is in state '%s' and cannot be re-previewed.")
                % session.state
            )
        raw = self._decode_raw(session.raw_csv)
        rows, headers = self._parse_csv(raw)
        header_map = {h.lower().strip(): h for h in headers if h}
        missing = [c for c in REQUIRED_COLUMNS if c not in header_map]
        if missing:
            session.sudo().write({"state": "failed"})
            raise CsvParseError(
                _("Missing required column(s): %s.  Required: name, employee_id, email.")
                % ", ".join(missing)
            )

        role_col = header_map.get("role")
        job_title_col = (
            header_map.get("job_title")
            or header_map.get("job title")
            or header_map.get("title")
        )
        assigned_ql_col = (
            header_map.get("assigned_ql")
            or header_map.get("assigned_ql_email")
            or header_map.get("ql_email")
        )
        assigned_pl_col = (
            header_map.get("assigned_pl")
            or header_map.get("assigned_pl_email")
            or header_map.get("pl_email")
        )
        assigned_tpm_col = (
            header_map.get("assigned_tpm")
            or header_map.get("assigned_tpm_email")
            or header_map.get("tpm_email")
        )
        # Legacy QC alias - silently mapped to QL since the new structure
        # treats Quality Lead (not Quality Reviewer) as the hierarchy slot.
        if not assigned_ql_col:
            assigned_ql_col = (
                header_map.get("assigned_qc")
                or header_map.get("assigned_qc_email")
                or header_map.get("qc_email")
            )
        manager_col = (
            header_map.get("reports_to")
            or header_map.get("manager")
            or header_map.get("manager_email")
            or header_map.get("reports to")
        )

        seen_codes = {}
        seen_emails = {}
        preview_rows = []
        valid = invalid = dup = 0

        for idx, raw_row in enumerate(rows, start=2):
            row_data = self._extract_row(
                raw_row, header_map, role_col, manager_col,
                assigned_ql_col, assigned_pl_col, assigned_tpm_col,
                job_title_col, session.default_role,
            )
            row_data["row_index"] = idx

            issues, status, existing = self._validate_row(
                row_data, seen_codes, seen_emails
            )
            row_data["issues"] = issues
            row_data["status"] = status
            row_data["can_import"] = not issues
            row_data["existing_employee_id"] = existing.id if existing else None
            row_data["existing_employee_archived"] = bool(
                existing and not existing.active
            ) if existing else False

            code_lower = (row_data["employee_id"] or "").strip().lower()
            email_lower = (row_data["email"] or "").strip().lower()
            if code_lower:
                seen_codes.setdefault(code_lower, idx)
            if email_lower:
                seen_emails.setdefault(email_lower, idx)

            if issues:
                invalid += 1
            elif status == "exists":
                valid += 1
            else:
                valid += 1

            preview_rows.append(row_data)

        self._enrich_preview_rows(preview_rows)

        payload = {
            "session_token": session.session_token,
            "csv_filename": session.csv_filename,
            "default_role": session.default_role,
            "create_user": session.create_user,
            "headers_detected": list(header_map.keys()),
            "totals": {
                "rows": len(preview_rows),
                "ready": sum(
                    1 for r in preview_rows
                    if r["status"] == "ready" and not r["issues"]
                ),
                "exists": sum(
                    1 for r in preview_rows
                    if r["status"] == "exists" and not r["issues"]
                ),
                "with_issues": sum(1 for r in preview_rows if r["issues"]),
                "to_create": sum(
                    1 for r in preview_rows
                    if r["status"] == "ready" and not r["issues"]
                ),
                "to_update": sum(
                    1 for r in preview_rows
                    if r["status"] == "exists" and not r["issues"]
                ),
            },
            "rows": preview_rows,
        }
        session.sudo().write({
            "state": "previewed",
            "preview_payload": json.dumps(payload, default=str),
            "total_rows": len(preview_rows),
            "valid_rows": valid,
            "invalid_rows": invalid,
            "duplicate_rows": sum(
                1 for r in preview_rows
                if any("duplicate" in issue.lower() for issue in r["issues"])
            ),
        })
        return payload

    def commit(self, session, row_overrides=None) -> dict:
        """Persist the validated rows.

        Returns a summary payload identical in shape to
        ``EmployeeRoleImportWizard.action_import``'s log/counts.
        Idempotent: re-committing a session that already imported returns
        the cached summary.
        ``row_overrides`` lets the caller mutate per-row fields between
        preview and commit (e.g. user picked a different role / PL / QC
        on the preview screen).  Shape:
            [{"row_index": 2, "role": "ql",
              "assigned_pl_employee_id": 30, "assigned_qc_employee_id": 41,
              "name": "...", "email": "...", "employee_id": "..."}]
        """
        session.assert_owner(self.env.user)
        if session.state == "imported" and session.summary_payload:
            return json.loads(session.summary_payload)
        if session.state in ("failed", "discarded"):
            raise UserError(
                _("This import session is in state '%s' and cannot be committed.")
                % session.state
            )
        # The import wires the reporting hierarchy in a second pass (a member's
        # manager may be a later row), so defer the hard mandatory-hierarchy
        # constraint for every write made during this commit. The wizard's
        # row-level flagging already blocks rows missing a required manager.
        self.env = self.env(context=dict(self.env.context, etp_importing=True))
        # Rebuild the repository on the import-context env too. Otherwise records
        # it creates/writes (employees, role) carry the original context WITHOUT
        # `etp_importing`, so the mandatory-hierarchy constraint fires mid-persist
        # — before the second pass can wire each row's manager — instead of being
        # deferred. The genuine "missing required manager" case is still caught,
        # cleanly, by the explicit post-wiring validation below.
        self.repo = EmployeeRepository(self.env)

        # Always rebuild preview right before commit to catch DB drift
        # between upload and commit.
        preview = self.build_preview(session)
        if row_overrides:
            self._apply_row_overrides(preview, row_overrides)

        Employee = self.env["hr.employee"].sudo().with_context(active_test=False)
        Users = self.env["res.users"].sudo().with_context(active_test=False)

        imported = updated = failed = 0
        log = [
            f"Create login users: {'yes' if session.create_user else 'no'}",
            "Duplicate policy: match by Employee ID, fallback to Email - update in place.",
            "",
        ]
        touched_emp_ids = []
        email_to_emp_id = {}

        for row in preview["rows"]:
            idx = row["row_index"]
            if row.get("issues"):
                failed += 1
                log.append(f"Row {idx}: ERROR - {'; '.join(row['issues'])}")
                continue

            try:
                result = self._persist_row(
                    Employee, Users, row,
                    create_user=session.create_user,
                    default_password=session.default_password,
                )
            except Exception as exc:  # noqa: BLE001 - log and continue per-row
                failed += 1
                log.append(f"Row {idx}: ERROR - {exc}")
                _logger.exception(
                    "employee.import.session %s row %s persistence failed",
                    session.session_token, idx,
                )
                continue

            if result["action"] == "created":
                imported += 1
            else:
                updated += 1
            log.append(
                f"Row {idx}: {result['action']} -> {row['name']} <{row['email']}> "
                f"role={result['role_label']} "
                f"(employee_id={result['employee_id']}, user_id={result['user_id'] or 'none'})"
            )
            touched_emp_ids.append(result["employee_id"])
            if row["email"]:
                email_to_emp_id[row["email"].lower()] = result["employee_id"]

        # second pass: assign reports_to now that everyone exists
        log.append("")
        log.append("--- Assigning Reports To ---")
        batch_employees = Employee.browse(touched_emp_ids)
        for row in preview["rows"]:
            if row.get("issues"):
                continue
            self._assign_parent(row, email_to_emp_id, batch_employees, Employee, log)

        # Force a recompute of the denormalized task_forge_* hierarchy now that
        # ALL parents are wired. The per-row parent writes above don't reliably
        # cascade inherited tiers (e.g. a QL's TPM from its PL's chain) through
        # multi-level chains within the same commit, so trigger it explicitly.
        if batch_employees:
            batch_employees.invalidate_recordset([
                "task_forge_ql_id", "task_forge_qr_id",
                "task_forge_pl_id", "task_forge_tpm_id",
            ])
            batch_employees.modified(["parent_id"])
            batch_employees.flush_recordset()

            # The mandatory-hierarchy constraint (_check_required_hierarchy) is
            # deferred during the multi-step import via the `etp_importing`
            # context, because a row's manager may be a later row. Now that
            # every parent is wired and the task_forge_* tiers are recomputed,
            # validate explicitly (in a non-import context) so a genuinely
            # missing required manager — e.g. a QL with no PL — raises HERE,
            # inside the controller's try/except, and is returned as a clean
            # JSON error (with CORS headers + the human message). Previously
            # this violation only surfaced at the request-teardown flush, which
            # bypasses the controller's error handling and reaches web clients
            # as an opaque "network connection" error with no actionable text.
            batch_employees.with_context(
                etp_importing=False
            )._check_required_hierarchy()

        summary = {
            "session_token": session.session_token,
            "totals": preview["totals"],
            "results": {
                "imported": imported,
                "updated": updated,
                "failed": failed,
                "touched_employee_ids": sorted(set(touched_emp_ids)),
            },
            "log": log,
        }
        session.sudo().write({
            "state": "imported" if failed == 0 else (
                "imported" if (imported + updated) > 0 else "failed"
            ),
            "imported_count": imported,
            "updated_count": updated,
            "failed_count": failed,
            "log_text": "\n".join(log),
            "summary_payload": json.dumps(summary, default=str),
        })
        return summary

    def _apply_row_overrides(self, preview: dict, row_overrides) -> None:
        """Merge per-row overrides from the client into the preview payload.

        The Flutter / web preview lets the user edit each row inline (name,
        employee_id, email, role, job_title, assigned_*). Those edits are
        sent at commit time as a list of dicts; this method applies them to
        the corresponding preview row and re-runs row validation so any
        change in role / email / employee_id is reflected in `issues` /
        `status` / `existing_employee_id` before the rows are persisted.

        Override shape (every key is optional except `row_index`):

            {
              "row_index": 2,
              "name": "Kajall",
              "employee_id": "GRT5674",
              "email": "kajall@ethara.ai",
              "role": "hr_admin",
              "job_title": "HR Lead",
              "assigned_ql_email": "...",
              "assigned_pl_email": "...",
              "assigned_tpm_email": "...",
              "exclude_employee_id": 42  // skip dedup against this id
            }

        Rows are matched by ``row_index`` first, then by ``email`` as a
        fallback so clients that don't track the original CSV row number
        can still mutate by stable identity. Unknown rows are appended to
        the preview as new entries so a freshly-typed row in the UI is
        actually imported.
        """
        if not row_overrides:
            return
        if isinstance(row_overrides, dict):
            row_overrides = [row_overrides]
        if not isinstance(row_overrides, list):
            raise UserError(_("row_overrides must be a list of objects."))

        rows = preview.get("rows") or []
        by_index = {r.get("row_index"): r for r in rows if r.get("row_index")}
        by_email = {(r.get("email") or "").lower(): r for r in rows if r.get("email")}

        seen_codes, seen_emails = {}, {}
        for r in rows:
            code_lower = (r.get("employee_id") or "").strip().lower()
            email_lower = (r.get("email") or "").strip().lower()
            if code_lower:
                seen_codes.setdefault(code_lower, r.get("row_index"))
            if email_lower:
                seen_emails.setdefault(email_lower, r.get("row_index"))

        next_index = (max((r.get("row_index") or 0) for r in rows) + 1) if rows else 2

        for raw in row_overrides:
            if not isinstance(raw, dict):
                continue
            target = None
            idx = raw.get("row_index")
            if idx is not None:
                try:
                    target = by_index.get(int(idx))
                except (TypeError, ValueError):
                    target = None
            if target is None:
                email_lookup = (raw.get("email") or "").strip().lower()
                if email_lookup:
                    target = by_email.get(email_lookup)
            if target is None:
                target = {"row_index": next_index}
                next_index += 1
                rows.append(target)

            for key in (
                "name", "employee_id", "email", "role", "job_title",
                "manager_email", "assigned_ql_email", "assigned_pl_email",
                "assigned_tpm_email",
            ):
                if key not in raw:
                    continue
                value = raw[key]
                if value is None:
                    value = ""
                if key in ("email", "assigned_ql_email", "assigned_pl_email",
                          "assigned_tpm_email", "manager_email"):
                    value = str(value).strip().lower()
                elif key == "role":
                    resolved = self._resolve_role_key(str(value).strip())
                    target["role_invalid_value"] = (
                        str(value).strip() if value and resolved is None else None
                    )
                    value = resolved or ""
                    target["role_label"] = ROLE_LABELS.get(value, "") if value else ""
                else:
                    value = str(value).strip()
                target[key] = value

            # Re-run validation on the mutated row so issues/status reflect
            # the new field values before _persist_row is called.
            exclude_id = raw.get("exclude_employee_id")
            try:
                exclude_id = int(exclude_id) if exclude_id else None
            except (TypeError, ValueError):
                exclude_id = None

            code_lower = (target.get("employee_id") or "").strip().lower()
            email_lower = (target.get("email") or "").strip().lower()
            local_seen_codes = {k: v for k, v in seen_codes.items()
                                if v != target.get("row_index")}
            local_seen_emails = {k: v for k, v in seen_emails.items()
                                 if v != target.get("row_index")}

            issues, status, existing = self._validate_row(
                target, local_seen_codes, local_seen_emails,
            )
            if existing and exclude_id and existing.id == exclude_id:
                existing = None
                status = "ready"

            target["issues"] = issues
            target["status"] = status
            target["can_import"] = not issues
            target["existing_employee_id"] = existing.id if existing else None
            target["existing_employee_archived"] = bool(
                existing and not existing.active
            ) if existing else False

            if code_lower:
                seen_codes.setdefault(code_lower, target.get("row_index"))
            if email_lower:
                seen_emails.setdefault(email_lower, target.get("row_index"))

        self._enrich_preview_rows(rows)

        # Recompute totals so the summary returned to the client reflects
        # the post-override state, not the original CSV.
        totals = {
            "rows": len(rows),
            "ready": sum(1 for r in rows
                         if r.get("status") == "ready" and not r.get("issues")),
            "exists": sum(1 for r in rows
                          if r.get("status") == "exists" and not r.get("issues")),
            "with_issues": sum(1 for r in rows if r.get("issues")),
            "to_create": sum(1 for r in rows
                             if r.get("status") == "ready" and not r.get("issues")),
            "to_update": sum(1 for r in rows
                             if r.get("status") == "exists" and not r.get("issues")),
        }
        preview["totals"] = totals
        preview["rows"] = rows

    def validate_row(self, payload: dict) -> dict:
        """Re-validate a single import row.

        Used by the Flutter import preview when the user edits a row
        inline. Returns the same row shape the upload/preview endpoints
        emit, so the client can drop the result straight back into its
        table row state.

        ``payload`` accepts the same keys a CSV row produces:
        ``name, employee_id, email, role, job_title, assigned_ql_email,
        assigned_pl_email, assigned_tpm_email``. ``exclude_employee_id``
        (optional) lets the caller scope the duplicate search away from
        the record currently being edited.
        """
        if not isinstance(payload, dict):
            raise UserError(_("validate_row payload must be a JSON object."))

        name = (payload.get("name") or "").strip()
        code = (payload.get("employee_id") or "").strip()
        email = (payload.get("email") or "").strip().lower()
        role_raw = (payload.get("role") or "").strip()
        role_key = self._resolve_role_key(role_raw) if role_raw else None
        role_invalid = role_raw if (role_raw and role_key is None) else None
        job_title = (payload.get("job_title") or "").strip()
        exclude_id = payload.get("exclude_employee_id")
        try:
            exclude_id = int(exclude_id) if exclude_id else None
        except (TypeError, ValueError):
            exclude_id = None

        row = {
            "name": name,
            "employee_id": code,
            "email": email,
            "role": role_key or "",
            "role_label": ROLE_LABELS.get(role_key, "") if role_key else "",
            "role_invalid_value": role_invalid,
            "job_title": job_title,
            "manager_email": "",
            "assigned_ql_email": (payload.get("assigned_ql_email") or "").strip().lower(),
            "assigned_pl_email": (payload.get("assigned_pl_email") or "").strip().lower(),
            "assigned_tpm_email": (payload.get("assigned_tpm_email") or "").strip().lower(),
        }

        issues, status, existing = self._validate_row(row, {}, {})

        if existing and exclude_id and existing.id == exclude_id:
            existing = None
            status = "ready"

        row["issues"] = issues
        row["status"] = status
        row["can_import"] = not issues
        row["existing_employee_id"] = existing.id if existing else None
        row["existing_employee_archived"] = bool(
            existing and not existing.active
        ) if existing else False

        self._enrich_preview_rows([row])
        return row

    def discard(self, session):
        session.assert_owner(self.env.user)
        if session.state == "imported":
            raise UserError(_("Cannot discard an already-imported session."))
        session.sudo().write({"state": "discarded"})
        return True

    # ── parsing ──────────────────────────────────────────────────────────────
    @staticmethod
    def _decode_raw(b64: str) -> str:
        try:
            return base64.b64decode(b64).decode("utf-8-sig")
        except Exception as exc:  # noqa: BLE001
            raise CsvParseError(
                _("Cannot decode the uploaded file: %s") % exc
            ) from exc

    @staticmethod
    def _parse_csv(raw_text: str):
        try:
            reader = csv.DictReader(io.StringIO(raw_text))
            headers = reader.fieldnames or []
            rows = list(reader)
            return rows, headers
        except csv.Error as exc:
            raise CsvParseError(_("Invalid CSV: %s") % exc) from exc

    def _extract_row(
        self, raw_row, header_map, role_col, manager_col,
        assigned_ql_col, assigned_pl_col, assigned_tpm_col,
        job_title_col, default_role,
    ):
        def get(col):
            real = header_map.get(col)
            return (raw_row.get(real) or "").strip() if real else ""

        name = get("name")
        code = get("employee_id")
        email = get("email").lower()

        row_role_raw = (raw_row.get(role_col) or "").strip() if role_col else ""
        resolved_role = self._resolve_role_key(row_role_raw) if row_role_raw else None
        if row_role_raw and resolved_role is None:
            role_key = False
            role_invalid_label = row_role_raw
        else:
            role_key = resolved_role or default_role or False
            role_invalid_label = None

        manager_email = ""
        if manager_col:
            manager_email = (raw_row.get(manager_col) or "").strip().lower()

        assigned_ql_email = ""
        if assigned_ql_col:
            assigned_ql_email = (raw_row.get(assigned_ql_col) or "").strip().lower()
        assigned_pl_email = ""
        if assigned_pl_col:
            assigned_pl_email = (raw_row.get(assigned_pl_col) or "").strip().lower()
        assigned_tpm_email = ""
        if assigned_tpm_col:
            assigned_tpm_email = (raw_row.get(assigned_tpm_col) or "").strip().lower()

        job_title = ""
        if job_title_col:
            job_title = (raw_row.get(job_title_col) or "").strip()

        return {
            "name": name,
            "employee_id": code,
            "email": email,
            "role": role_key or "",
            "role_label": ROLE_LABELS.get(role_key, "") if role_key else "",
            "role_invalid_value": role_invalid_label,
            "job_title": job_title,
            "manager_email": manager_email,
            "assigned_ql_email": assigned_ql_email,
            "assigned_pl_email": assigned_pl_email,
            "assigned_tpm_email": assigned_tpm_email,
        }

    # ── validation ───────────────────────────────────────────────────────────
    def _validate_row(self, row, seen_codes, seen_emails):
        issues = []
        if not row["name"]:
            issues.append("name is required")
        if not row["employee_id"]:
            issues.append("employee_id is required")
        elif not EMPLOYEE_CODE_RE.match(row["employee_id"]):
            issues.append(
                "employee_id format invalid (expected 2-6 uppercase letters "
                "followed by digits, e.g. GRT1137 or GRTP6789)"
            )
        if not row["email"]:
            issues.append("email is required")
        elif not EMAIL_RE.match(row["email"]):
            issues.append("email is not a valid address")

        if row.get("role_invalid_value"):
            issues.append(
                "unknown role '%s' (allowed: %s)"
                % (row["role_invalid_value"], ", ".join(sorted(ROLE_GROUP_MAP.keys())))
            )
        elif not row["role"]:
            issues.append("no role specified for this row")
        elif row["role"] not in self.env["hr.employee"]._current_user_assignable_roles():
            issues.append(
                "Role '%s' cannot be assigned by you." % row["role"]
            )

        # Mandatory hierarchy: the immediate higher tier must be provided
        # (Tasker->QL/QR, QL/QR->PL, PL->TPM). The model constraint is deferred
        # during import, so enforce it here -> the row is flagged and skipped
        # rather than saved without its required manager.
        _req = {
            "tasker": ("ql", "QL/QR"),
            "qr": ("pl", "PL"),
            "ql": ("pl", "PL"),
            "pl": ("tpm", "TPM"),
        }.get(row.get("role"))
        if _req and not (
            row.get("assigned_%s" % _req[0]) or row.get("assigned_%s_email" % _req[0])
        ):
            issues.append(
                "a %s must be assigned for role '%s'" % (_req[1], row["role"])
            )

        code_lower = (row["employee_id"] or "").lower()
        email_lower = (row["email"] or "").lower()
        if code_lower and code_lower in seen_codes:
            issues.append(
                f"duplicate employee_id within CSV (also on row {seen_codes[code_lower]})"
            )
        if email_lower and email_lower in seen_emails:
            issues.append(
                f"duplicate email within CSV (also on row {seen_emails[email_lower]})"
            )

        existing = self.repo.find_by_employee_code(row["employee_id"]) \
            or self.repo.find_by_email(row["email"])
        status = "exists" if existing else "ready"
        return issues, status, existing

    # ── persistence ──────────────────────────────────────────────────────────
    def _resolve_group(self, role_key):
        if not role_key:
            raise UserError(_("No role specified."))
        if role_key not in ROLE_GROUP_MAP:
            raise UserError(_("Unknown role: %s") % role_key)
        xml_id, label = ROLE_GROUP_MAP[role_key]
        group = self.env.ref(xml_id, raise_if_not_found=False)
        if not group:
            raise UserError(
                _("Role group %s is not installed (install etp_user_roles).")
                % xml_id
            )
        return group, label

    def _role_group_commands(self, group):
        internal = self.env.ref("base.group_user", raise_if_not_found=False)
        commands = [(4, group.id)]
        if internal:
            commands.append((4, internal.id))
        for _key, (xml_id, _label) in ROLE_GROUP_MAP.items():
            other = self.env.ref(xml_id, raise_if_not_found=False)
            if other and other.id != group.id:
                commands.append((3, other.id))
        return commands

    def _persist_row(self, Employee, Users, row, create_user, default_password):
        """Upsert a single employee + its login user with a two-way link.

        HR rule: exactly one employee record and one login user per person,
        always linked.  Dedup resolves the existing person, then the user is
        resolved/linked, then the employee is written.  No duplicate employee
        or user is ever created.
        """
        role_key = row["role"]
        group, label = self._resolve_group(role_key)

        # ── 1. Resolve the existing employee.  Match by Employee ID first,
        #    then fall back to work email (the requested email-based check).
        emp_by_code = self.repo.find_by_employee_code(row["employee_id"]) \
            if row["employee_id"] else Employee.browse()
        emp_by_email = self.repo.find_by_email(row["email"]) \
            if row["email"] else Employee.browse()

        # Req: "If Employee ID exists, verify using Email it is the same
        # employee."  When the ID points at one person and the email at
        # another, they are not the same record - refuse rather than silently
        # overwrite one with the other's data.
        if emp_by_code and emp_by_email and emp_by_code.id != emp_by_email.id:
            raise UserError(_(
                "Conflicting records: Employee ID '%(code)s' belongs to "
                "'%(code_name)s', but email '%(email)s' belongs to a different "
                "employee '%(email_name)s'. Reconcile them before importing."
            ) % {
                "code": row["employee_id"],
                "code_name": emp_by_code.name,
                "email": row["email"],
                "email_name": emp_by_email.name,
            })

        existing_emp = emp_by_code or emp_by_email
        if existing_emp and not existing_emp.active:
            self.repo.reactivate(existing_emp)

        # ── 2. Resolve the single login user for this person (maps an existing
        #    user even when create_user is off; only *creation* is gated).
        user = self._resolve_user(
            Users, Employee, row, group, default_password,
            existing_emp=existing_emp, create_user=create_user,
        )

        # ── 3. Upsert the employee and ensure the employee->user link.
        vals = {"name": row["name"], "work_email": row["email"]}
        if row["employee_id"]:
            vals["employee_code"] = row["employee_id"]
        if row.get("job_title"):
            vals["job_title"] = row["job_title"]

        if existing_emp:
            # Link the (existing or newly created) user when the employee has
            # none yet - this also surfaces the employee on the user record.
            if user and not existing_emp.user_id:
                vals["user_id"] = user.id
            existing_emp.sudo().write(vals)
            employee = existing_emp
            action = "updated"
        else:
            # A user found by email may already own an employee record - reuse
            # it instead of creating a duplicate.
            employee = (
                user.employee_id
                if user and "employee_id" in user._fields and user.employee_id
                else False
            )
            if user:
                vals["user_id"] = user.id
            if employee:
                employee.sudo().write(vals)
                action = "updated"
            else:
                employee = self.repo.create_employee(vals)
                action = "created"

        # ── 4. Apply role groups on the linked user (role lives on the user).
        if user:
            user.sudo().write({"group_ids": self._role_group_commands(group)})

        # ── 5. Make the employee's stored `role` reflect the imported choice.
        #
        # `hr.employee.role` is a stored compute derived from the linked user's
        # security groups. Two gaps left imported employees showing a blank
        # role in the View List:
        #   * With a user: the groups were written in step 4 AFTER the employee
        #     was created, and the compute didn't re-fire for the group change,
        #     so the stored role stayed empty. Force a recompute now.
        #   * Without a user: there are no groups to derive from at all, so the
        #     role could never be stored. Persist the selected role directly
        #     (the relaxed inverse allows it; the compute guard preserves it).
        # Keep the mandatory-hierarchy constraint deferred for these writes —
        # the row's manager is wired in the second pass below; the genuine
        # missing-manager case is validated cleanly after that.
        emp_importing = employee.with_context(etp_importing=True)
        if role_key:
            if user:
                emp_importing.invalidate_recordset(["role"])
                emp_importing._compute_role()
                emp_importing.flush_recordset(["role"])
            else:
                emp_importing.sudo().write({"role": role_key})

        return {
            "action": action,
            "employee_id": employee.id,
            "user_id": user.id if user else None,
            "role_label": label,
        }

    def _resolve_user(self, Users, Employee, row, group, default_password,
                      existing_emp, create_user):
        """Return the single login user for this person, mapping or creating.

        Preference order (never creates a duplicate user):

        1. The user already linked to the matched employee.
        2. An existing user whose login matches the row email - mapped
           automatically *even when create_user is off*, because mapping an
           existing account is not the same as creating one.
        3. A brand-new user, only when ``create_user`` is enabled.
        """
        # 1. Employee already has a user.
        linked = existing_emp.user_id if existing_emp else Users.browse()
        if linked:
            if not linked.active:
                linked.sudo().write({"active": True})
            return linked

        # 2. An existing login matches this email - map it automatically.
        match = self.repo.find_user_by_login(row["email"]) if row["email"] \
            else Users.browse()
        if match:
            owner = Employee.search([("user_id", "=", match.id)], limit=1)
            if owner and (not existing_emp or owner.id != existing_emp.id):
                raise UserError(_(
                    "Login '%(login)s' is already linked to a different "
                    "employee '%(name)s' (#%(id)d). Reconcile before importing."
                ) % {"login": row["email"], "name": owner.name, "id": owner.id})
            if not match.active:
                match.sudo().write({"active": True})
            match.sudo().write({"name": row["name"]})
            return match

        # 3. Create a new user only when allowed.
        if not create_user:
            return Users.browse()
        vals = {
            "name": row["name"],
            "login": row["email"],
            "email": row["email"],
            "group_ids": self._role_group_commands(group),
        }
        if default_password:
            vals["password"] = default_password
        return Users.create(vals)

    def _assign_parent(self, row, email_to_emp_id, batch_employees, Employee, log):
        if row.get("issues"):
            return
        if not row["role"]:
            return
        idx = row["row_index"]
        emp_id = email_to_emp_id.get(row["email"].lower())
        if not emp_id:
            return
        emp = Employee.browse(emp_id)

        parent = self._lookup_legacy_manager(row, email_to_emp_id, Employee)
        if not parent:
            parent = self._derive_parent_from_direct(row, email_to_emp_id, Employee)
        if not parent:
            parent = self.repo.find_default_manager(
                row["role"], ROLE_DEFAULT_PARENT_ROLE,
                extra_candidates=batch_employees,
            )

        if parent and parent.id != emp.id:
            if parent.role and ROLE_LEVEL.get(parent.role, 0) <= ROLE_LEVEL.get(
                row["role"], 0
            ):
                log.append(
                    f"Row {idx}: WARNING - manager '{parent.name}' is not senior "
                    f"to role '{row['role']}', skipping reports_to assignment."
                )
            else:
                emp.sudo().write({"parent_id": parent.id})
                log.append(
                    f"Row {idx}: reports_to -> {parent.name} ({parent.role or '-'})"
                )
        elif row.get("manager_email") and not parent:
            log.append(
                f"Row {idx}: WARNING - manager_email '{row['manager_email']}' "
                f"not found - parent_id left unchanged."
            )

        self.repo.propagate_task_forge_chain(emp)
        self._apply_direct_hierarchy_assignment(
            emp, row, email_to_emp_id, Employee, log,
        )

    @staticmethod
    def _lookup_legacy_manager(row, email_to_emp_id, Employee):
        # Honours older CSVs (column name 'reports_to' / 'manager_email') and
        # the wizard, both of which still send a single manager email.
        manager_email = row.get("manager_email")
        if not manager_email:
            return Employee.browse()
        mgr_id = email_to_emp_id.get(manager_email)
        if mgr_id:
            return Employee.browse(mgr_id)
        return Employee.search(
            [("work_email", "=ilike", manager_email)], limit=1
        )

    def _apply_direct_hierarchy_assignment(self, emp, row, email_to_emp_id, Employee, log):
        fields_present = emp._fields
        idx = row["row_index"]
        applicable = set(ROLE_HIERARCHY_FIELDS.get(row.get("role") or "", ()))
        vals = {}
        used_any = False

        for csv_key, field, label, role_key in (
            ("assigned_ql_email", "task_forge_ql_id", "assigned_ql", "ql"),
            ("assigned_pl_email", "task_forge_pl_id", "assigned_pl", "pl"),
            ("assigned_tpm_email", "task_forge_tpm_id", "assigned_tpm", "tpm"),
        ):
            if field not in fields_present:
                continue
            if role_key not in applicable:
                continue
            email = (row.get(csv_key) or "").lower()
            if not email:
                continue
            target = self._resolve_employee_by_email(email, email_to_emp_id, Employee)
            if target:
                vals[field] = target.id
                used_any = True
                log.append(f"Row {idx}: {label} -> {target.name} <{email}>")
            else:
                log.append(
                    f"Row {idx}: WARNING - {label} '{email}' not found, skipped."
                )

        if vals:
            emp.sudo().write(vals)
        return used_any

    def _enrich_preview_rows(self, preview_rows):
        emails_to_resolve = set()
        for row in preview_rows:
            for key in ("assigned_ql_email", "assigned_pl_email", "assigned_tpm_email"):
                value = (row.get(key) or "").strip().lower()
                if value:
                    emails_to_resolve.add(value)
        resolved = {}
        if emails_to_resolve:
            Employee = self.env["hr.employee"].sudo().with_context(active_test=False)
            for emp in Employee.search([("work_email", "in", list(emails_to_resolve))]):
                resolved[(emp.work_email or "").lower()] = emp.name or ""

        for row in preview_rows:
            applicable = set(ROLE_HIERARCHY_FIELDS.get(row.get("role") or "", ()))
            row["hierarchy_fields"] = sorted(applicable)
            for role_key, email_key, name_key in (
                ("ql", "assigned_ql_email", "assigned_ql_name"),
                ("pl", "assigned_pl_email", "assigned_pl_name"),
                ("tpm", "assigned_tpm_email", "assigned_tpm_name"),
            ):
                email = (row.get(email_key) or "").strip().lower()
                if role_key in applicable and email:
                    row[name_key] = resolved.get(email, "")
                else:
                    row[name_key] = ""
                    if role_key not in applicable:
                        row[email_key] = ""

    @staticmethod
    def _resolve_employee_by_email(email, email_to_emp_id, Employee):
        emp_id = email_to_emp_id.get(email)
        if emp_id:
            return Employee.browse(emp_id)
        found = Employee.search(
            [("work_email", "=ilike", email), ("active", "=", True)], limit=1
        )
        return found or None

    @staticmethod
    def _derive_parent_from_direct(row, email_to_emp_id, Employee):
        role = row.get("role") or ""
        candidates = []
        if role in ("tasker", "qr"):
            candidates = [
                row.get("assigned_ql_email"),
                row.get("assigned_pl_email"),
                row.get("assigned_tpm_email"),
            ]
        elif role == "ql":
            candidates = [row.get("assigned_pl_email"), row.get("assigned_tpm_email")]
        elif role == "pl":
            candidates = [row.get("assigned_tpm_email")]
        for raw in candidates:
            target_email = (raw or "").lower()
            if not target_email:
                continue
            emp_id = email_to_emp_id.get(target_email)
            if emp_id:
                return Employee.browse(emp_id)
            found = Employee.search(
                [("work_email", "=ilike", target_email), ("active", "=", True)], limit=1
            )
            if found:
                return found
        return Employee.browse()

    # ── lookups ──────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_role_key(value):
        if not value:
            return None
        v = value.strip().lower()
        if not v:
            return None
        if v in ROLE_GROUP_MAP:
            return v
        for key, (_xml, label) in ROLE_GROUP_MAP.items():
            if label.lower() == v:
                return key
        return None
