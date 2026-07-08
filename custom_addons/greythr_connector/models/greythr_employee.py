from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class GreythrEmployee(models.Model):
    _name = "greythr.employee"
    _description = "greytHR Employee"
    _order = "name"
    _rec_name = "name"

    instance_id = fields.Many2one(
        "greythr.instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    external_employee_id = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    first_name = fields.Char()
    middle_name = fields.Char()
    last_name = fields.Char()
    email = fields.Char()
    mobile = fields.Char()
    department = fields.Char()
    designation = fields.Char()
    date_of_joining = fields.Date()
    employment_status = fields.Char()

    employee_id = fields.Many2one(
        "hr.employee",
        string="Odoo Employee",
        ondelete="set null",
        index=True,
    )
    hr_employee_code = fields.Char(
        related="employee_id.employee_code",
        store=False,
        readonly=True,
    )

    leave_balance_ids = fields.One2many(
        "greythr.leave.balance", "greythr_employee_id", string="Leave Balances"
    )
    leave_transaction_ids = fields.One2many(
        "greythr.leave.transaction", "greythr_employee_id", string="Leave Transactions"
    )
    leave_balance_count = fields.Integer(compute="_compute_counts")
    leave_transaction_count = fields.Integer(compute="_compute_counts")

    last_sync_at = fields.Datetime(readonly=True)
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        (
            "uniq_instance_external",
            "UNIQUE(instance_id, external_employee_id)",
            "greytHR employee id must be unique per instance.",
        ),
    ]

    def _compute_counts(self):
        Bal = self.env["greythr.leave.balance"]
        Tx = self.env["greythr.leave.transaction"]
        for rec in self:
            rec.leave_balance_count = Bal.search_count(
                [("greythr_employee_id", "=", rec.id)]
            )
            rec.leave_transaction_count = Tx.search_count(
                [("greythr_employee_id", "=", rec.id)]
            )

    def action_link_hr_employee(self):
        HrEmployee = self.env["hr.employee"].sudo()
        linked = 0
        for rec in self:
            if rec.employee_id:
                continue
            emp = HrEmployee.search(
                [("employee_code", "=", rec.external_employee_id)], limit=1
            )
            if emp:
                rec.employee_id = emp.id
                linked += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "greytHR",
                "message": "Linked %s employee(s) to hr.employee." % linked,
                "sticky": False,
                "type": "success",
            },
        }

    def action_open_hr_employee(self):
        self.ensure_one()
        if not self.employee_id:
            return False
        return {
            "type": "ir.actions.act_window",
            "res_model": "hr.employee",
            "res_id": self.employee_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_sync_my_balance(self):
        self.ensure_one()
        if not self.external_employee_id:
            raise UserError(_("Missing greytHR external employee id."))
        instance = self.instance_id
        instance._sync_leave_types()
        Balance = self.env["greythr.leave.balance"].sudo()
        year = instance.default_leave_year or fields.Date.today().year
        path = "/leave/v2/employee/%s/years/%s/balance" % (
            self.external_employee_id,
            int(year),
        )
        created = updated = 0
        now = fields.Datetime.now()
        response = instance._request("GET", path)
        rows = []
        if isinstance(response, list):
            rows = response
        elif isinstance(response, dict):
            for key in ("data", "content", "items", "results"):
                v = response.get(key)
                if isinstance(v, list):
                    rows = v
                    break
            if not rows and isinstance(response.get("list"), list):
                rows = response["list"]
            if not rows and response:
                rows = [response]
        raw_dump = instance._dump_raw(response if isinstance(response, dict) else {"list": response})
        if not rows:
            raise UserError(
                _(
                    "greytHR returned no balance rows for employee %(id)s year %(y)s.\n\n"
                    "Raw response:\n%(r)s"
                )
                % {"id": self.external_employee_id, "y": year, "r": raw_dump[:2000]}
            )
        for raw in rows:
            code_raw = raw.get("leaveTypeCategory") or raw.get("leaveType")
            if isinstance(code_raw, dict):
                code = str(
                    code_raw.get("code")
                    or code_raw.get("id")
                    or code_raw.get("category")
                    or ""
                ).strip()
                name_source = code_raw
            else:
                code = str(code_raw or "").strip()
                name_source = raw
            if not code:
                continue
            leave_type = instance._get_leave_type(code) or instance._ensure_leave_type(code, name_source)
            if not leave_type:
                continue
            vals = {
                "greythr_employee_id": self.id,
                "leave_type_id": leave_type.id or False,
                "year": int(year),
                "opening_balance": instance._to_float(raw.get("openingBalance")),
                "granted": instance._to_float(raw.get("granted")),
                "availed": instance._to_float(raw.get("availed")),
                "applied": instance._to_float(raw.get("applied")),
                "lapsed": instance._to_float(raw.get("lapsed")),
                "deducted": instance._to_float(raw.get("deducted")),
                "encashed": instance._to_float(raw.get("encashed")),
                "current_balance": instance._to_float(
                    raw.get("currentBalance") or raw.get("balance")
                ),
                "last_sync_at": now,
                "raw_payload": instance._dump_raw(raw),
            }
            existing = Balance.search(
                [
                    ("greythr_employee_id", "=", self.id),
                    ("year", "=", int(year)),
                    ("leave_type_id", "=", leave_type.id or False),
                ],
                limit=1,
            )
            if existing:
                existing.write(vals)
                updated += 1
            else:
                Balance.create(vals)
                created += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("greytHR Leave Balance"),
                "message": _(
                    "%(n)s: created %(c)s, updated %(u)s.",
                    n=self.name,
                    c=created,
                    u=updated,
                ),
                "sticky": False,
                "type": "success",
            },
        }

    def action_push_to_greythr(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("No greytHR instance linked."))
        if not self.external_employee_id:
            raise UserError(
                _(
                    "greytHR requires an Employee Number. "
                    "Fill 'External Employee ID' with the employee number "
                    "you want to assign in greytHR before pushing."
                )
            )
        instance = self.instance_id
        hr_emp = self.employee_id
        parts = (self.name or (hr_emp.name if hr_emp else "") or "").split(" ", 1)
        first = self.first_name or (parts[0] if parts else "")
        last = self.last_name or (parts[1] if len(parts) > 1 else "")
        full_name = self.name or (hr_emp.name if hr_emp else "")
        dob = "2000-01-01"
        if hr_emp and getattr(hr_emp, "birthday", False):
            dob = fields.Date.to_string(hr_emp.birthday)
        doj = None
        if self.date_of_joining:
            doj = fields.Date.to_string(self.date_of_joining)
        elif hr_emp and getattr(hr_emp, "joining_date", False):
            doj = fields.Date.to_string(hr_emp.joining_date)
        email = self.email or (hr_emp.work_email if hr_emp else False) or None
        mobile = (
            self.mobile
            or (hr_emp.mobile_phone if hr_emp else False)
            or (hr_emp.work_phone if hr_emp else False)
            or None
        )
        designation = (
            self.designation
            or (hr_emp.job_title if hr_emp else False)
            or None
        )
        department = (
            self.department
            or (hr_emp.department_id.name if hr_emp and hr_emp.department_id else False)
            or None
        )
        if not full_name:
            raise UserError(_("Employee name is required by greytHR."))
        payload = {
            "name": full_name,
            "firstName": first or None,
            "lastName": last or None,
            "dateOfBirth": dob,
            "dateOfJoin": doj,
            "email": email,
            "mobile": mobile,
            "employeeNo": self.external_employee_id or None,
            "department": department,
            "designation": designation,
        }
        payload = {k: v for k, v in payload.items() if v}
        result = instance._request("POST", "/employee/v2/employees", json=payload)
        external_id = ""
        if isinstance(result, dict):
            external_id = str(
                result.get("employeeId")
                or result.get("id")
                or result.get("employeeNumber")
                or ""
            ).strip()
        write_vals = {
            "last_sync_at": fields.Datetime.now(),
            "raw_payload": instance._dump_raw(
                result if isinstance(result, dict) else {}
            ),
        }
        if external_id and external_id != self.external_employee_id:
            write_vals["external_employee_id"] = external_id
        self.sudo().write(write_vals)
        if self.employee_id and "employee_code" in self.employee_id._fields:
            if external_id and not self.employee_id.employee_code:
                self.employee_id.sudo().write({"employee_code": external_id})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("greytHR"),
                "message": _(
                    "%(n)s pushed to greytHR (id=%(id)s).",
                    n=self.name,
                    id=external_id or "?",
                ),
                "sticky": False,
                "type": "success",
            },
        }

    def action_create_hr_employee(self):
        self.ensure_one()
        if self.employee_id:
            raise UserError(_("This record is already linked to an Odoo employee."))
        HrEmployee = self.env["hr.employee"].sudo()
        vals = {
            "name": self.name or self.external_employee_id,
            "work_email": self.email or False,
            "mobile_phone": self.mobile or False,
            "job_title": self.designation or False,
        }
        if "employee_code" in HrEmployee._fields and self.external_employee_id:
            vals["employee_code"] = self.external_employee_id
        emp = HrEmployee.create(vals)
        self.sudo().write({"employee_id": emp.id})
        return {
            "type": "ir.actions.act_window",
            "res_model": "hr.employee",
            "res_id": emp.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_sync_my_transactions(self):
        self.ensure_one()
        if not self.external_employee_id:
            raise UserError(_("Missing greytHR external employee id."))
        instance = self.instance_id
        instance._sync_leave_types()
        Transaction = self.env["greythr.leave.transaction"].sudo()
        end = fields.Date.today()
        start = end - timedelta(
            days=max(1, instance.transactions_lookback_days or 30)
        )
        created = updated = 0
        now = fields.Datetime.now()
        for chunk_start, chunk_end in instance._chunk_date_range(start, end, days=30):
            params = {
                "start": fields.Date.to_string(chunk_start),
                "end": fields.Date.to_string(chunk_end),
            }
            path = "/leave/v2/employee/%s/transactions" % self.external_employee_id
            for raw in instance._paginated(path, params=params):
                child_list = (
                    raw.get("list") if isinstance(raw.get("list"), list) else [raw]
                )
                for child in child_list:
                    external_txn_id = str(
                        child.get("id") or child.get("transactionId") or ""
                    ).strip()
                    if not external_txn_id:
                        continue
                    code_raw = child.get("leaveTypeCategory") or child.get("leaveType")
                    if isinstance(code_raw, dict):
                        code = str(
                            code_raw.get("code")
                            or code_raw.get("id")
                            or code_raw.get("category")
                            or ""
                        ).strip()
                        name_source = code_raw
                    else:
                        code = str(code_raw or "").strip()
                        name_source = child
                    leave_type = instance._get_leave_type(code) or instance._ensure_leave_type(code, name_source)
                    if not leave_type:
                        continue
                    vals = {
                        "external_transaction_id": external_txn_id,
                        "greythr_employee_id": self.id,
                        "leave_type_id": leave_type.id or False,
                        "transaction_type": instance._map_transaction_type(
                            child.get("leaveTransactionType")
                            or child.get("transactionType")
                        ),
                        "from_date": instance._parse_date(child.get("fromDate")),
                        "to_date": instance._parse_date(child.get("toDate")),
                        "number_of_days": instance._to_float(
                            child.get("days") or child.get("numberOfDays")
                        ),
                        "sessions": instance._format_sessions(child),
                        "status": (
                            "cancelled"
                            if child.get("cancelled")
                            else instance._map_status(child.get("status"))
                        ),
                        "remarks": child.get("remarks") or False,
                        "reason": child.get("reason") or False,
                        "last_sync_at": now,
                        "raw_payload": instance._dump_raw(child),
                    }
                    existing = Transaction.search(
                        [("external_transaction_id", "=", external_txn_id)],
                        limit=1,
                    )
                    if existing:
                        existing.write(vals)
                        updated += 1
                    else:
                        Transaction.create(vals)
                        created += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("greytHR Leave Transactions"),
                "message": _(
                    "%(n)s: created %(c)s, updated %(u)s.",
                    n=self.name,
                    c=created,
                    u=updated,
                ),
                "sticky": False,
                "type": "success",
            },
        }

    def action_sync_my_requests(self):
        self.ensure_one()
        if not self.external_employee_id:
            raise UserError(_("Missing greytHR external employee id."))
        instance = self.instance_id
        try:
            instance._sync_leave_types()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("greytHR leave-types pre-sync failed: %s", exc)
        Request = self.env["greythr.leave.request"].sudo()
        end = fields.Date.today()
        start = end - timedelta(
            days=max(1, instance.transactions_lookback_days or 30)
        )
        created = updated = 0
        now = fields.Datetime.now()
        total_rows_seen = 0
        last_raw = None
        for chunk_start, chunk_end in instance._chunk_date_range(start, end, days=30):
            params = {
                "start": fields.Date.to_string(chunk_start),
                "end": fields.Date.to_string(chunk_end),
            }
            path = "/leave/v2/employee/%s/transactions" % self.external_employee_id
            try:
                response = instance._request("GET", path, params=params)
            except Exception as exc:  # noqa: BLE001
                raise UserError(
                    _("greytHR requests fetch failed for %s: %s") % (self.name, exc)
                )
            last_raw = response
            rows = []
            if isinstance(response, list):
                rows = response
            elif isinstance(response, dict):
                for key in ("data", "content", "items", "results", "list"):
                    v = response.get(key)
                    if isinstance(v, list):
                        rows = v
                        break
                if not rows and response:
                    rows = [response]
            total_rows_seen += len(rows)
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                child_list = (
                    raw.get("list") if isinstance(raw.get("list"), list) else [raw]
                )
                for child in child_list:
                    if not isinstance(child, dict):
                        continue
                    external_id = str(
                        child.get("requestId") or child.get("id") or ""
                    ).strip()
                    if not external_id:
                        continue
                    code_raw = child.get("leaveTypeCategory") or child.get("leaveType")
                    if isinstance(code_raw, dict):
                        code = str(
                            code_raw.get("code")
                            or code_raw.get("id")
                            or code_raw.get("category")
                            or ""
                        ).strip()
                        name_source = code_raw
                    else:
                        code = str(code_raw or "").strip()
                        name_source = child
                    leave_type = instance._get_leave_type(code) or instance._ensure_leave_type(code, name_source)
                    vals = {
                        "external_request_id": external_id,
                        "greythr_employee_id": self.id,
                        "instance_id": instance.id,
                        "leave_type_id": leave_type.id if leave_type else False,
                        "from_date": instance._parse_date(child.get("fromDate")),
                        "to_date": instance._parse_date(child.get("toDate")),
                        "number_of_days": instance._to_float(
                            child.get("numberOfDays") or child.get("days")
                        ),
                        "sessions": instance._format_sessions(child),
                        "reason": child.get("reason") or False,
                        "remarks": child.get("remarks") or False,
                        "status": (
                            "cancelled"
                            if child.get("cancelled")
                            else instance._map_request_status(child.get("status") or "approved")
                        ),
                        "direction": "inbound",
                        "push_state": "sent",
                        "last_sync_at": now,
                        "raw_payload": instance._dump_raw(child),
                    }
                    existing = Request.search(
                        [
                            ("instance_id", "=", instance.id),
                            ("external_request_id", "=", external_id),
                        ],
                        limit=1,
                    )
                    if existing:
                        existing.write(vals)
                        updated += 1
                    else:
                        Request.create(vals)
                        created += 1
        if (created + updated) == 0:
            raw_dump = instance._dump_raw(
                last_raw if isinstance(last_raw, dict) else {"body": last_raw}
            )
            raise UserError(
                _(
                    "greytHR returned no leave request rows for employee %(id)s "
                    "in window %(s)s to %(e)s.\n\n"
                    "Rows seen in envelope: %(r)s\n"
                    "Last raw response:\n%(dump)s"
                )
                % {
                    "id": self.external_employee_id,
                    "s": start,
                    "e": end,
                    "r": total_rows_seen,
                    "dump": raw_dump[:3000],
                }
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("greytHR Leave Requests"),
                "message": _(
                    "%(n)s: created %(c)s, updated %(u)s.",
                    n=self.name,
                    c=created,
                    u=updated,
                ),
                "sticky": False,
                "type": "success",
            },
        }
