import base64
import csv
import io
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..models.hr_employee import (
    ROLE_DEFAULT_PARENT_ROLE,
    ROLE_GROUP_MAP,
    ROLE_SELECTION,
)

_logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmployeeRoleImportWizard(models.TransientModel):
    _name = "employee.role.import.wizard"
    _description = "Import Employees with Role Assignment"

    csv_file = fields.Binary(
        string="Employee File (CSV)",
        required=True,
        help="CSV file with the columns: name, employee_id, email (optional: role)",
    )
    csv_filename = fields.Char(string="Filename")
    csv_row_count = fields.Integer(string="Rows in File", readonly=True)

    role = fields.Selection(
        selection=ROLE_SELECTION,
        string="Default Role",
        help="Applied to every row whose role is still empty. "
        "Use the button next to it to push this value into the preview rows.",
    )

    line_ids = fields.One2many(
        "employee.role.import.line",
        "wizard_id",
        string="Preview Rows",
    )
    existing_row_count = fields.Integer(
        compute="_compute_row_counts", string="Rows Already In System"
    )
    issue_row_count = fields.Integer(
        compute="_compute_row_counts", string="Rows With Issues"
    )
    ready_row_count = fields.Integer(
        compute="_compute_row_counts", string="Rows Ready"
    )

    @api.depends("line_ids.status")
    def _compute_row_counts(self):
        for wiz in self:
            wiz.existing_row_count = len(
                wiz.line_ids.filtered(
                    lambda l: l.status in ("exists", "exists_archived")
                )
            )
            wiz.issue_row_count = len(
                wiz.line_ids.filtered(lambda l: l.status == "issue")
            )
            wiz.ready_row_count = len(
                wiz.line_ids.filtered(lambda l: l.status == "ready")
            )

    create_user = fields.Boolean(
        string="Create Login Users",
        default=True,
    )

    default_password = fields.Char(
        string="Default Password",
        default="Ethara@123",
    )

    import_count = fields.Integer(string="Imported", readonly=True)
    update_count = fields.Integer(string="Updated", readonly=True)
    error_count = fields.Integer(string="Errors", readonly=True)
    log_text = fields.Text(string="Import Log", readonly=True)
    imported_user_ids = fields.Many2many(
        "res.users",
        "employee_role_import_user_rel",
        "wizard_id",
        "user_id",
        string="Imported / Updated Users",
        readonly=True,
    )
    imported_employee_ids = fields.Many2many(
        "hr.employee",
        "employee_role_import_emp_rel",
        "wizard_id",
        "employee_id",
        string="Imported / Updated Employees",
        readonly=True,
    )

    state = fields.Selection(
        [("draft", "Upload"), ("done", "Done")],
        default="draft",
        readonly=True,
    )

    REQUIRED_COLUMNS = ("name", "employee_id", "email")

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

    @api.onchange("csv_file")
    def _onchange_csv_file(self):
        self.ensure_one()
        self.line_ids = [(5, 0, 0)]
        if not self.csv_file:
            self.csv_row_count = 0
            return None
        try:
            raw = base64.b64decode(self.csv_file).decode("utf-8-sig")
        except Exception as exc:
            self.csv_row_count = 0
            return {
                "warning": {
                    "title": _("Invalid file"),
                    "message": _("Cannot decode the uploaded file: %s") % exc,
                }
            }
        reader = csv.DictReader(io.StringIO(raw))
        headers = reader.fieldnames or []
        header_map = {h.lower().strip(): h for h in headers if h}
        missing = [c for c in self.REQUIRED_COLUMNS if c not in header_map]
        if missing:
            self.csv_row_count = 0
            return {
                "warning": {
                    "title": _("Missing required columns"),
                    "message": _(
                        "Expected columns: name, employee_id, email. "
                        "Missing: %s"
                    ) % ", ".join(missing),
                }
            }
        rows = list(reader)
        self.csv_row_count = len(rows)
        role_col = header_map.get("role")
        manager_col = (
            header_map.get("reports_to")
            or header_map.get("manager")
            or header_map.get("reports to")
            or header_map.get("manager_email")
        )
        Employee = self.env["hr.employee"].sudo().with_context(active_test=False)
        new_lines = []
        for idx, row in enumerate(rows, start=2):
            name = (row.get(header_map["name"]) or "").strip()
            code = (row.get(header_map["employee_id"]) or "").strip()
            email = (row.get(header_map["email"]) or "").strip().lower()
            if not name and not code and not email:
                continue
            row_role = ""
            if role_col:
                row_role = (row.get(role_col) or "").strip()
            role_key = self._resolve_role_key(row_role) or self.role or False
            manager_email = ""
            if manager_col:
                manager_email = (row.get(manager_col) or "").strip().lower()
            parent_id = False
            if manager_email:
                mgr = Employee.search(
                    [("work_email", "=", manager_email), ("active", "=", True)],
                    limit=1,
                )
                if mgr:
                    parent_id = mgr.id
            if not parent_id and role_key:
                auto = self._find_default_manager(role_key)
                if auto:
                    parent_id = auto.id
            new_lines.append((0, 0, {
                "sequence": idx,
                "name": name,
                "employee_code": code,
                "email": email,
                "role": role_key,
                "manager_email": manager_email or False,
                "parent_id": parent_id,
            }))
        self.line_ids = new_lines
        return None

    def _find_default_manager(self, employee_role, extra_candidates=None):
        Employee = self.env["hr.employee"].sudo()
        seen = set()
        target_role = ROLE_DEFAULT_PARENT_ROLE.get(employee_role)
        while target_role and target_role not in seen:
            seen.add(target_role)
            if extra_candidates:
                in_batch = extra_candidates.filtered(lambda e: e.role == target_role)
                if in_batch:
                    return in_batch[0]
            existing = Employee.search(
                [("role", "=", target_role), ("active", "=", True)], limit=1
            )
            if existing:
                return existing
            target_role = ROLE_DEFAULT_PARENT_ROLE.get(target_role)
        return False

    @api.onchange("role")
    def _onchange_default_role(self):
        if not self.role:
            return
        for line in self.line_ids:
            if not line.role:
                line.role = self.role
                if not line.parent_id:
                    auto = self._find_default_manager(self.role)
                    if auto:
                        line.parent_id = auto.id

    def action_apply_default_role(self):
        self.ensure_one()
        if not self.role:
            raise UserError(_("Pick a Default Role first."))
        empty = self.line_ids.filtered(lambda l: not l.role)
        empty.write({"role": self.role})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Default role applied"),
                "message": _("%d row(s) updated.") % len(empty),
                "type": "success",
                "sticky": False,
            },
        }

    def action_apply_default_role_all(self):
        self.ensure_one()
        if not self.role:
            raise UserError(_("Pick a Default Role first."))
        self.line_ids.write({"role": self.role})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Default role applied"),
                "message": _("%d row(s) updated.") % len(self.line_ids),
                "type": "success",
                "sticky": False,
            },
        }

    def _resolve_group(self, role_key):
        if not role_key:
            raise UserError(_("No role specified."))
        if role_key not in ROLE_GROUP_MAP:
            raise UserError(
                _("Unknown role: %(role)s. Allowed values: %(allowed)s.",
                  role=role_key,
                  allowed=", ".join(sorted(ROLE_GROUP_MAP)))
            )
        xml_id, label = ROLE_GROUP_MAP[role_key]
        group = self.env.ref(xml_id, raise_if_not_found=False)
        if not group:
            raise UserError(
                _("Role group %(xml_id)s is not installed. "
                  "Please install the `etp_user_roles` module first.",
                  xml_id=xml_id)
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

    def _should_auto_import(self):
        self.ensure_one()
        return (
            self.state == "draft"
            and self.line_ids
            and not self.import_count
            and not self.update_count
            and not self.error_count
            and not self.log_text
        )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if self.env.context.get("employee_role_import_no_auto"):
            return records
        for wiz in records:
            if wiz._should_auto_import():
                wiz.with_context(
                    employee_role_import_no_auto=True
                ).action_import()
        return records

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("employee_role_import_no_auto"):
            return res
        for wiz in self:
            if wiz._should_auto_import():
                wiz.with_context(
                    employee_role_import_no_auto=True
                ).action_import()
        return res

    def _notify_import_result(self, imported, updated, errors):
        try:
            parts = []
            if imported:
                parts.append(_("%d imported") % imported)
            if updated:
                parts.append(_("%d updated") % updated)
            if errors:
                parts.append(_("%d error(s)") % errors)
            message = ", ".join(parts) if parts else _("No rows processed.")
            ntype = "danger" if errors and not (imported or updated) else (
                "warning" if errors else "success"
            )
            self.env.user._bus_send("simple_notification", {
                "title": _("Employee import complete"),
                "message": message,
                "sticky": False,
                "type": ntype,
            })
        except Exception:  # noqa: BLE001
            _logger.info(
                "Employee role import bus toast skipped (%s imported / %s updated / "
                "%s errors)",
                imported, updated, errors,
            )

    def action_import(self):
        self.ensure_one()
        if self.state == "done":
            return {
                "type": "ir.actions.act_window",
                "res_model": self._name,
                "res_id": self.id,
                "view_mode": "form",
                "target": "new",
            }
        if not self.line_ids:
            raise UserError(_("Upload a CSV file first — no preview rows to import."))

        Employee = self.env["hr.employee"].sudo().with_context(active_test=False)
        Users = self.env["res.users"].sudo().with_context(active_test=False)

        imported = 0
        updated = 0
        errors = 0
        touched_user_ids = set()
        touched_emp_ids = set()
        email_to_emp_id = {}
        seen_emails_in_batch = {}
        seen_codes_in_batch = {}
        log = [
            f"Create login users: {'yes' if self.create_user else 'no'}",
            "Duplicate policy: match by Employee ID or Email — always update existing rows.",
            "",
        ]

        for line in self.line_ids:
            row_idx = line.sequence or line.id
            try:
                name = (line.name or "").strip()
                email = (line.email or "").strip().lower()
                code = (line.employee_code or "").strip()
                role_key = line.role

                if not name or not email:
                    log.append(f"Row {row_idx}: ERROR - 'name' and 'email' required")
                    errors += 1
                    continue
                if not EMAIL_RE.match(email):
                    log.append(f"Row {row_idx}: ERROR - invalid email '{email}'")
                    errors += 1
                    continue
                if not role_key:
                    log.append(f"Row {row_idx}: ERROR - no role set for this row")
                    errors += 1
                    continue
                if email in seen_emails_in_batch:
                    log.append(
                        f"Row {row_idx}: ERROR - duplicate email '{email}' in this CSV "
                        f"(also on row {seen_emails_in_batch[email]})"
                    )
                    errors += 1
                    continue
                if code and code.lower() in seen_codes_in_batch:
                    log.append(
                        f"Row {row_idx}: ERROR - duplicate Employee ID '{code}' in this CSV "
                        f"(also on row {seen_codes_in_batch[code.lower()]})"
                    )
                    errors += 1
                    continue
                seen_emails_in_batch[email] = row_idx
                if code:
                    seen_codes_in_batch[code.lower()] = row_idx

                group, role_label = self._resolve_group(role_key)

                match_by_code = (
                    Employee.search([("employee_code", "=ilike", code)], limit=1)
                    if code else Employee.browse()
                )
                match_by_email = (
                    Employee.search([("work_email", "=ilike", email)], limit=1)
                    if email else Employee.browse()
                )
                if match_by_code and match_by_email and match_by_code.id != match_by_email.id:
                    errors += 1
                    log.append(
                        f"Row {row_idx}: ERROR - conflicting duplicates "
                        f"(Employee ID '{code}' belongs to employee #{match_by_code.id} "
                        f"<{match_by_code.work_email or '-'}>, "
                        f"Email '{email}' belongs to employee #{match_by_email.id} "
                        f"<{match_by_email.employee_code or '-'}>). "
                        f"Reconcile the two records before re-importing."
                    )
                    continue
                existing_emp = match_by_code or match_by_email
                existing_user = existing_emp.user_id if existing_emp else Users.browse()
                archived = bool(existing_emp and not existing_emp.active)

                if existing_emp:
                    if not existing_emp.active:
                        existing_emp.write({"active": True})
                    if existing_user and not existing_user.active:
                        existing_user.write({"active": True})

                    if not existing_user and self.create_user:
                        orphan_user = Users.search([("login", "=ilike", email)], limit=1)
                        if orphan_user:
                            other_emp = Employee.search(
                                [("user_id", "=", orphan_user.id),
                                 ("id", "!=", existing_emp.id)],
                                limit=1,
                            )
                            if other_emp:
                                errors += 1
                                log.append(
                                    f"Row {row_idx}: ERROR - login '{email}' is already "
                                    f"linked to employee #{other_emp.id} ({other_emp.name}). "
                                    f"Reconcile the two records before re-importing."
                                )
                                continue
                            if not orphan_user.active:
                                orphan_user.write({"active": True})
                            existing_user = orphan_user
                        else:
                            user_vals = {
                                "name": name,
                                "login": email,
                                "email": email,
                                "group_ids": self._role_group_commands(group),
                            }
                            if self.default_password:
                                user_vals["password"] = self.default_password
                            existing_user = Users.create(user_vals)

                    if existing_user:
                        existing_user.write({"group_ids": self._role_group_commands(group)})
                        touched_user_ids.add(existing_user.id)

                    emp_update = {"name": name, "work_email": email}
                    if code:
                        emp_update["employee_code"] = code
                    if existing_user and not existing_emp.user_id:
                        emp_update["user_id"] = existing_user.id
                    existing_emp.write(emp_update)
                    email_to_emp_id[email] = existing_emp.id
                    touched_emp_ids.add(existing_emp.id)

                    updated += 1
                    archived_note = " (un-archived)" if archived else ""
                    role_note = role_label if existing_user else f"{role_label} (no user — role not applied)"
                    log.append(
                        f"Row {row_idx}: updated{archived_note} -> {name} <{email}> "
                        f"role={role_note} "
                        f"(matched employee #{existing_emp.id}, "
                        f"user_id={existing_user.id if existing_user else 'none'})"
                    )
                    continue

                user = False
                if self.create_user:
                    orphan_user = Users.search([("login", "=ilike", email)], limit=1)
                    if orphan_user:
                        other_emp = Employee.search(
                            [("user_id", "=", orphan_user.id)],
                            limit=1,
                        )
                        if other_emp:
                            errors += 1
                            log.append(
                                f"Row {row_idx}: ERROR - login '{email}' is already "
                                f"linked to employee #{other_emp.id} ({other_emp.name}). "
                                f"Reconcile the two records before re-importing."
                            )
                            continue
                        if not orphan_user.active:
                            orphan_user.write({"active": True})
                        user = orphan_user
                        user.write({
                            "name": name,
                            "group_ids": self._role_group_commands(group),
                        })
                    else:
                        user_vals = {
                            "name": name,
                            "login": email,
                            "email": email,
                            "group_ids": self._role_group_commands(group),
                        }
                        if self.default_password:
                            user_vals["password"] = self.default_password
                        user = Users.create(user_vals)
                    touched_user_ids.add(user.id)

                employee = False
                if user and "employee_id" in user._fields:
                    employee = user.employee_id or False
                emp_vals = {"name": name, "work_email": email}
                if code:
                    emp_vals["employee_code"] = code
                if user:
                    emp_vals["user_id"] = user.id

                if employee:
                    employee.write(emp_vals)
                else:
                    Employee.create(emp_vals)

                created_emp = employee or Employee.search([("work_email", "=ilike", email)], limit=1)
                if created_emp:
                    email_to_emp_id[email] = created_emp.id
                    touched_emp_ids.add(created_emp.id)
                imported += 1
                role_note = role_label if user else f"{role_label} (no user — role not applied)"
                log.append(
                    f"Row {row_idx}: imported -> {name} <{email}> role={role_note} "
                    f"(user_id={user.id if user else 'none'}, "
                    f"employee_id={created_emp.id if created_emp else 'none'})"
                )

            except (ValidationError, UserError) as exc:
                errors += 1
                message = exc.args[0] if exc.args else str(exc)
                log.append(f"Row {row_idx}: VALIDATION ERROR - {message}")
                _logger.warning("Employee role import row %s validation error: %s", row_idx, exc)
            except Exception as exc:
                errors += 1
                log.append(f"Row {row_idx}: ERROR - {exc}")
                _logger.exception("Employee role import row %s failed", row_idx)

        log.append("")
        log.append("--- Assigning Reports To ---")
        batch_emps = Employee.browse(list(touched_emp_ids))
        for line in self.line_ids:
            if not line.role:
                continue
            try:
                email = (line.email or "").strip().lower()
                emp_id = email_to_emp_id.get(email)
                if not emp_id:
                    continue
                emp = Employee.browse(emp_id)
                row_idx = line.sequence or line.id
                parent = line.parent_id
                if not parent and line.manager_email:
                    mgr_id = email_to_emp_id.get(line.manager_email)
                    parent = (
                        Employee.browse(mgr_id)
                        if mgr_id
                        else Employee.search(
                            [("work_email", "=", line.manager_email),
                             ("active", "=", True)],
                            limit=1,
                        )
                    )
                if not parent:
                    parent = self._find_default_manager(
                        line.role, extra_candidates=batch_emps
                    )
                if parent and parent.id != emp.id:
                    emp.write({"parent_id": parent.id})
                    log.append(
                        f"Row {row_idx}: reports_to -> {parent.name} "
                        f"({parent.role or '-'})"
                    )
                elif line.manager_email and not parent:
                    log.append(
                        f"Row {row_idx}: WARNING - manager '{line.manager_email}' "
                        f"not found, parent_id left unchanged"
                    )
            except Exception as exc:
                log.append(
                    f"Row {line.sequence or line.id}: reports_to "
                    f"assignment failed - {exc}"
                )
                _logger.exception(
                    "Manager assignment failed for row %s", line.sequence or line.id
                )

        summary = (
            f"Import complete: {imported} imported, "
            f"{updated} updated, {errors} errors."
        )
        log.insert(0, summary)

        self.with_context(employee_role_import_no_auto=True).write({
            "state": "done",
            "import_count": imported,
            "update_count": updated,
            "error_count": errors,
            "log_text": "\n".join(log),
            "imported_user_ids": [(6, 0, list(touched_user_ids))],
            "imported_employee_ids": [(6, 0, list(touched_emp_ids))],
        })

        self.env.cr.flush()
        self._notify_import_result(imported, updated, errors)

        if touched_emp_ids:
            return {
                "type": "ir.actions.act_window",
                "name": _("Imported Employees"),
                "res_model": "hr.employee",
                "view_mode": "list,form",
                "views": [
                    (self.env.ref("employee_role_import.view_imported_employees_list").id, "list"),
                    (False, "form"),
                ],
                "domain": [
                    ("id", "in", list(touched_emp_ids)),
                    "|", ("user_id", "=", False), ("user_id", "!=", self.env.uid),
                ],
                "context": {"active_test": False, "create": False},
                "target": "current",
            }

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_open_lines_search(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Preview Rows - %s") % (self.csv_filename or "Import"),
            "res_model": "employee.role.import.line",
            "view_mode": "list",
            "domain": [("wizard_id", "=", self.id)],
            "target": "new",
            "context": {"create": False},
        }

    def action_view_imported_users(self):
        self.ensure_one()
        user_ids = self.imported_user_ids.ids
        domain = [("id", "in", user_ids)] if user_ids else [(0, "=", 1)]
        return {
            "type": "ir.actions.act_window",
            "name": _("Imported / Updated Users"),
            "res_model": "res.users",
            "view_mode": "list,form",
            "domain": domain,
            "context": {"search_default_filter_no_share": 0},
            "target": "current",
        }

    def action_view_imported_employees(self):
        self.ensure_one()
        emp_ids = self.imported_employee_ids.ids
        if not emp_ids:
            return {
                "type": "ir.actions.act_window",
                "name": _("Imported Employees"),
                "res_model": "hr.employee",
                "view_mode": "list,form",
                "domain": [("id", "=", 0)],
                "context": {"active_test": False},
                "target": "current",
            }
        return {
            "type": "ir.actions.act_window",
            "name": _("Imported Employees"),
            "res_model": "hr.employee",
            "view_mode": "list,form",
            "views": [
                (self.env.ref("employee_role_import.view_imported_employees_list").id, "list"),
                (False, "form"),
            ],
            "domain": [
                ("id", "in", emp_ids),
                "|", ("user_id", "=", False), ("user_id", "!=", self.env.uid),
            ],
            "context": {"active_test": False},
            "target": "current",
        }

    def action_download_template(self):
        self.ensure_one()
        csv_template = (
            "name,employee_id,email,role,reports_to\n"
            "Mark Lead,EMP003,mark.lead@example.com,pl,\n"
            "Jane Smith,EMP002,jane.smith@example.com,ql,mark.lead@example.com\n"
            "John Doe,EMP001,john.doe@example.com,tasker,jane.smith@example.com\n"
        )
        data = base64.b64encode(csv_template.encode("utf-8")).decode("ascii")
        attachment = self.env["ir.attachment"].create({
            "name": "employee_role_import_template.csv",
            "datas": data,
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "text/csv",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    def action_reset(self):
        self.ensure_one()
        self.write({
            "state": "draft",
            "csv_file": False,
            "csv_filename": False,
            "csv_row_count": 0,
            "import_count": 0,
            "update_count": 0,
            "error_count": 0,
            "log_text": False,
            "line_ids": [(5, 0, 0)],
            "imported_user_ids": [(5, 0, 0)],
            "imported_employee_ids": [(5, 0, 0)],
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
