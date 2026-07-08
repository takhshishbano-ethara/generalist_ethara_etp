import logging
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.greythr.com"
DEFAULT_TIMEOUT = 60
DEFAULT_PAGE_SIZE = 100
TOKEN_LEEWAY_SECONDS = 60


class GreythrInstance(models.Model):
    _name = "greythr.instance"
    _description = "greytHR Instance"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    base_url = fields.Char(
        string="Base URL",
        required=True,
        default=DEFAULT_BASE_URL,
        help="greytHR API base URL, e.g. https://api.greythr.com",
    )
    domain = fields.Char(
        string="Company Domain",
        required=True,
        help="Value sent in the x-greythr-domain header (your greytHR company domain).",
    )
    client_id = fields.Char(string="Client ID / API Username", required=True)
    client_secret = fields.Char(string="Client Secret / API Password", required=True)
    auth_mode = fields.Selection(
        [
            ("basic", "OAuth2 Basic (client_credentials)"),
            ("json", "Admin Token (JSON username/password)"),
        ],
        default="basic",
        required=True,
        help="Basic: standard OAuth2 client_credentials with Basic auth. "
             "JSON: admin-generated API user with JSON body {username, password}.",
    )
    auth_base_url = fields.Char(
        string="Auth Base URL",
        help="Base URL used ONLY for the token request. Leave empty to reuse "
             "Base URL. greytHR typically issues tokens from the tenant "
             "subdomain (e.g. https://yourcompany.greythr.com) while serving "
             "data from https://api.greythr.com.",
    )
    auth_endpoint = fields.Char(
        default="/uas/v1/oauth2/client-token",
        help="Path appended to Auth Base URL (or Base URL) when requesting a token.",
    )
    timeout = fields.Integer(default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds.")
    page_size = fields.Integer(
        default=DEFAULT_PAGE_SIZE,
        help="Page size used when paginating list endpoints.",
    )

    push_employee_on_create = fields.Boolean(
        string="Push New Employees to greytHR",
        default=False,
        help="When enabled, creating an hr.employee will automatically create "
             "the corresponding employee in greytHR via this instance.",
    )
    push_leave_on_state_change = fields.Boolean(
        string="Push Leave Approvals/Refusals to greytHR",
        default=False,
        help="When enabled, creating/approving/refusing an hr.leave will push "
             "the change to greytHR via this instance.",
    )
    enable_payroll_sync = fields.Boolean(
        string="Enable Payroll Sync",
        default=False,
        help="Enables payroll sync buttons and cron for this instance.",
    )
    default_payroll_year = fields.Integer(
        default=lambda self: fields.Date.today().year,
    )
    default_payroll_month = fields.Integer(
        default=lambda self: fields.Date.today().month,
    )

    access_token = fields.Char(readonly=True, copy=False)
    access_token_expiry = fields.Datetime(readonly=True, copy=False)

    default_leave_year = fields.Integer(
        default=lambda self: fields.Date.today().year,
        help="Year used when syncing leave balances if none provided.",
    )
    transactions_lookback_days = fields.Integer(
        default=30,
        help="How many days to look back when auto-syncing leave transactions.",
    )

    employee_ids = fields.One2many(
        "greythr.employee", "instance_id", string="Employees"
    )
    leave_type_ids = fields.One2many(
        "greythr.leave.type", "instance_id", string="Leave Types"
    )
    leave_request_ids = fields.One2many(
        "greythr.leave.request", "instance_id", string="Leave Requests"
    )
    payroll_ids = fields.One2many(
        "greythr.payroll", "instance_id", string="Payroll Records"
    )
    employee_count = fields.Integer(compute="_compute_counts")
    leave_type_count = fields.Integer(compute="_compute_counts")
    leave_balance_count = fields.Integer(compute="_compute_counts")
    leave_transaction_count = fields.Integer(compute="_compute_counts")
    leave_request_count = fields.Integer(compute="_compute_counts")
    payroll_count = fields.Integer(compute="_compute_counts")

    last_employee_sync_at = fields.Datetime(readonly=True, copy=False)
    last_employee_sync_count = fields.Integer(readonly=True, copy=False)
    last_employee_sync_error = fields.Text(readonly=True, copy=False)

    last_leave_type_sync_at = fields.Datetime(readonly=True, copy=False)
    last_leave_type_sync_count = fields.Integer(readonly=True, copy=False)
    last_leave_type_sync_error = fields.Text(readonly=True, copy=False)

    last_leave_balance_sync_at = fields.Datetime(readonly=True, copy=False)
    last_leave_balance_sync_count = fields.Integer(readonly=True, copy=False)
    last_leave_balance_sync_error = fields.Text(readonly=True, copy=False)

    last_leave_transaction_sync_at = fields.Datetime(readonly=True, copy=False)
    last_leave_transaction_sync_count = fields.Integer(readonly=True, copy=False)
    last_leave_transaction_sync_error = fields.Text(readonly=True, copy=False)

    last_leave_request_sync_at = fields.Datetime(readonly=True, copy=False)
    last_leave_request_sync_count = fields.Integer(readonly=True, copy=False)
    last_leave_request_sync_error = fields.Text(readonly=True, copy=False)

    last_payroll_sync_at = fields.Datetime(readonly=True, copy=False)
    last_payroll_sync_count = fields.Integer(readonly=True, copy=False)
    last_payroll_sync_error = fields.Text(readonly=True, copy=False)

    def _compute_counts(self):
        Emp = self.env["greythr.employee"]
        Lt = self.env["greythr.leave.type"]
        Lb = self.env["greythr.leave.balance"]
        Ltx = self.env["greythr.leave.transaction"]
        Req = self.env["greythr.leave.request"]
        Pay = self.env["greythr.payroll"]
        for rec in self:
            rec.employee_count = Emp.search_count([("instance_id", "=", rec.id)])
            rec.leave_type_count = Lt.search_count([("instance_id", "=", rec.id)])
            rec.leave_balance_count = Lb.search_count(
                [("greythr_employee_id.instance_id", "=", rec.id)]
            )
            rec.leave_transaction_count = Ltx.search_count(
                [("greythr_employee_id.instance_id", "=", rec.id)]
            )
            rec.leave_request_count = Req.search_count(
                [("instance_id", "=", rec.id)]
            )
            rec.payroll_count = Pay.search_count(
                [("instance_id", "=", rec.id)]
            )

    def _get_requests(self):
        try:
            import requests  # noqa: F401
        except ImportError as exc:
            raise UserError(
                _("Python package 'requests' is not installed on the Odoo host.")
            ) from exc
        return requests

    def _token_valid(self):
        self.ensure_one()
        if not self.access_token or not self.access_token_expiry:
            return False
        return self.access_token_expiry > (
            fields.Datetime.now() + timedelta(seconds=TOKEN_LEEWAY_SECONDS)
        )

    def _get_token(self, force_refresh=False):
        self.ensure_one()
        if not force_refresh and self._token_valid():
            return self.access_token
        requests = self._get_requests()
        endpoint = (self.auth_endpoint or "/uas/v1/oauth2/client-token").strip()
        host = (self.auth_base_url or self.base_url or "").rstrip("/")
        url = host + "/" + endpoint.lstrip("/")
        try:
            if self.auth_mode == "json":
                response = requests.post(
                    url,
                    json={
                        "username": self.client_id,
                        "password": self.client_secret,
                    },
                    headers={"Accept": "application/json"},
                    timeout=self.timeout or DEFAULT_TIMEOUT,
                )
            else:
                response = requests.post(
                    url,
                    auth=(self.client_id, self.client_secret),
                    headers={"Accept": "application/json"},
                    timeout=self.timeout or DEFAULT_TIMEOUT,
                )
        except Exception as exc:  # noqa: BLE001
            raise UserError(
                _("Failed to reach greytHR token endpoint: %s") % exc
            ) from exc
        if response.status_code != 200:
            raise UserError(
                _(
                    "greytHR token request failed (HTTP %(code)s): %(body)s",
                    code=response.status_code,
                    body=(response.text or "")[:500],
                )
            )
        payload = response.json() or {}
        token = (
            payload.get("access_token")
            or payload.get("token")
            or payload.get("accessToken")
        )
        if not token:
            raise UserError(_("greytHR token response did not include access_token."))
        expires_in = int(payload.get("expires_in") or payload.get("expiresIn") or 3600)
        self.sudo().write(
            {
                "access_token": token,
                "access_token_expiry": fields.Datetime.now()
                + timedelta(seconds=expires_in),
            }
        )
        return token

    def _request(self, method, path, params=None, json=None, retry_on_auth=True):
        self.ensure_one()
        requests = self._get_requests()
        token = self._get_token()
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        headers = {
            "ACCESS-TOKEN": token,
            "x-greythr-domain": self.domain,
            "Accept": "application/json",
        }
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                timeout=self.timeout or DEFAULT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            raise UserError(
                _("greytHR request %(m)s %(p)s failed: %(e)s", m=method, p=path, e=exc)
            ) from exc
        if response.status_code == 401 and retry_on_auth:
            self._get_token(force_refresh=True)
            return self._request(method, path, params=params, json=json, retry_on_auth=False)
        if response.status_code >= 400:
            raise UserError(
                _(
                    "greytHR %(m)s %(p)s failed (HTTP %(c)s): %(b)s",
                    m=method,
                    p=path,
                    c=response.status_code,
                    b=(response.text or "")[:500],
                )
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise UserError(
                _("greytHR response was not valid JSON: %s") % (response.text or "")[:500]
            ) from exc

    def _paginated(self, path, params=None):
        self.ensure_one()
        params = dict(params or {})
        page = 0
        size = self.page_size or DEFAULT_PAGE_SIZE
        while True:
            params["page"] = page
            params["size"] = size
            payload = self._request("GET", path, params=params)
            if isinstance(payload, list):
                _logger.info(
                    "greytHR %s page=%s -> list of %s", path, page, len(payload)
                )
                for item in payload:
                    yield item
                return
            if not isinstance(payload, dict):
                _logger.info(
                    "greytHR %s page=%s -> non-dict %s", path, page, type(payload).__name__
                )
                return
            items = None
            matched_key = None
            for key in ("data", "content", "items", "results"):
                v = payload.get(key)
                if isinstance(v, list):
                    items = v
                    matched_key = key
                    break
            if items is None:
                _logger.info(
                    "greytHR %s page=%s -> dict keys=%s, no list envelope",
                    path,
                    page,
                    list(payload.keys()),
                )
                if payload:
                    yield payload
                return
            _logger.info(
                "greytHR %s page=%s -> envelope=%s, items=%s",
                path,
                page,
                matched_key,
                len(items),
            )
            for item in items:
                yield item
            pages_info = (
                payload.get("pages")
                if isinstance(payload.get("pages"), dict)
                else payload
            )
            last = pages_info.get("last")
            total_pages = pages_info.get("totalPages")
            if last is True:
                return
            if total_pages is not None and page + 1 >= int(total_pages):
                return
            if not items:
                return
            page += 1

    def action_debug_balance(self):
        self.ensure_one()
        year = self.default_leave_year or fields.Date.today().year
        path = "/leave/v2/employee/years/%s/balance" % int(year)
        response = self._request("GET", path, params={"page": 0, "size": 5})
        try:
            import json

            body = json.dumps(response, default=str, indent=2)[:4000]
        except Exception:  # noqa: BLE001
            body = str(response)[:4000]
        raise UserError(
            _("Raw balance response (first page):\n\n%s") % body
        )

    def action_test_connection(self):
        self.ensure_one()
        try:
            self._get_token(force_refresh=True)
            self._request("GET", "/employee/v2/employees", params={"page": 0, "size": 1})
        except UserError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UserError(_("Connection failed: %s") % exc) from exc
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("greytHR"),
                "message": _("Connection to %s succeeded.") % self.name,
                "sticky": False,
                "type": "success",
            },
        }

    def _notification(self, title, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "sticky": False,
                "type": kind,
            },
        }

    def action_sync_employees(self):
        self.ensure_one()
        try:
            created, updated = self._sync_employees()
        except UserError as exc:
            self.sudo().write({"last_employee_sync_error": str(exc)[:1000]})
            raise
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({"last_employee_sync_error": str(exc)[:1000]})
            raise UserError(_("Employee sync failed: %s") % exc) from exc
        return self._notification(
            _("greytHR Employees"),
            _("Created: %(c)s, Updated: %(u)s", c=created, u=updated),
        )

    def action_sync_leave_types(self):
        self.ensure_one()
        try:
            created, updated = self._sync_leave_types()
        except UserError as exc:
            self.sudo().write({"last_leave_type_sync_error": str(exc)[:1000]})
            raise
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({"last_leave_type_sync_error": str(exc)[:1000]})
            raise UserError(_("Leave type sync failed: %s") % exc) from exc
        return self._notification(
            _("greytHR Leave Types"),
            _("Created: %(c)s, Updated: %(u)s", c=created, u=updated),
        )

    def action_sync_leave_balances(self):
        self.ensure_one()
        year = self.default_leave_year or fields.Date.today().year
        try:
            created, updated = self._sync_leave_balances(year)
        except UserError as exc:
            self.sudo().write({"last_leave_balance_sync_error": str(exc)[:1000]})
            raise
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({"last_leave_balance_sync_error": str(exc)[:1000]})
            raise UserError(_("Leave balance sync failed: %s") % exc) from exc
        return self._notification(
            _("greytHR Leave Balances (%s)") % year,
            _("Created: %(c)s, Updated: %(u)s", c=created, u=updated),
        )

    def action_sync_leave_transactions(self):
        self.ensure_one()
        end = fields.Date.today()
        start = end - timedelta(days=max(1, self.transactions_lookback_days or 30))
        try:
            created, updated = self._sync_leave_transactions(start, end)
        except UserError as exc:
            self.sudo().write({"last_leave_transaction_sync_error": str(exc)[:1000]})
            raise
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({"last_leave_transaction_sync_error": str(exc)[:1000]})
            raise UserError(_("Leave transaction sync failed: %s") % exc) from exc
        return self._notification(
            _("greytHR Leave Transactions"),
            _(
                "Range %(s)s → %(e)s. Created: %(c)s, Updated: %(u)s",
                s=start,
                e=end,
                c=created,
                u=updated,
            ),
        )

    def action_sync_all(self):
        self.ensure_one()
        self.action_sync_employees()
        self.action_sync_leave_types()
        self.action_sync_leave_balances()
        self.action_sync_leave_transactions()
        self.action_sync_leave_requests()
        if self.enable_payroll_sync:
            self.action_sync_payroll()
        return self._notification(
            _("greytHR"), _("Full sync completed for %s.") % self.name
        )

    def action_sync_leave_requests(self):
        self.ensure_one()
        try:
            created, updated = self._sync_leave_requests()
        except UserError as exc:
            self.sudo().write({"last_leave_request_sync_error": str(exc)[:1000]})
            raise
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({"last_leave_request_sync_error": str(exc)[:1000]})
            raise UserError(_("Leave request sync failed: %s") % exc) from exc
        return self._notification(
            _("greytHR Leave Requests"),
            _("Created: %(c)s, Updated: %(u)s", c=created, u=updated),
        )

    def action_sync_payroll(self):
        self.ensure_one()
        if not self.enable_payroll_sync:
            raise UserError(_("Payroll sync is disabled on this instance."))
        year = self.default_payroll_year or fields.Date.today().year
        month = self.default_payroll_month or fields.Date.today().month
        try:
            created, updated = self._sync_payroll(year, month)
        except UserError as exc:
            self.sudo().write({"last_payroll_sync_error": str(exc)[:1000]})
            raise
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({"last_payroll_sync_error": str(exc)[:1000]})
            raise UserError(_("Payroll sync failed: %s") % exc) from exc
        return self._notification(
            _("greytHR Payroll (%04d-%02d)") % (year, month),
            _("Created: %(c)s, Updated: %(u)s", c=created, u=updated),
        )

    def _sync_employees(self):
        self.ensure_one()
        Employee = self.env["greythr.employee"].sudo()
        HrEmployee = self.env["hr.employee"].sudo()
        created = updated = 0
        now = fields.Datetime.now()
        for raw in self._paginated("/employee/v2/employees"):
            external_id = str(
                raw.get("employeeId") or raw.get("id") or raw.get("employeeNumber") or ""
            ).strip()
            if not external_id:
                _logger.warning("greytHR employee without id: %s", raw)
                continue
            vals = self._employee_vals(raw, external_id, HrEmployee)
            vals["last_sync_at"] = now
            existing = Employee.search(
                [("instance_id", "=", self.id), ("external_employee_id", "=", external_id)],
                limit=1,
            )
            if existing:
                existing.write(vals)
                updated += 1
            else:
                vals.update(
                    {"instance_id": self.id, "external_employee_id": external_id}
                )
                Employee.create(vals)
                created += 1
        self.sudo().write(
            {
                "last_employee_sync_at": now,
                "last_employee_sync_count": created + updated,
                "last_employee_sync_error": False,
            }
        )
        return created, updated

    def _employee_vals(self, raw, external_id, HrEmployee):
        def first(*keys):
            for k in keys:
                v = raw.get(k)
                if v:
                    return v
            return False

        first_name = first("firstName", "first_name") or ""
        middle_name = first("middleName", "middle_name") or ""
        last_name = first("lastName", "last_name") or ""
        full_name = first("name", "fullName") or " ".join(
            p for p in [first_name, middle_name, last_name] if p
        )
        hr_emp = HrEmployee.search([("employee_code", "=", external_id)], limit=1)
        status_val = raw.get("status")
        left = raw.get("leftorg") or raw.get("leftOrg")
        if left is True:
            employment_status = "Left"
        elif status_val is not None:
            employment_status = str(status_val)
        else:
            employment_status = first("employmentStatus") or False
        return {
            "name": full_name or external_id,
            "first_name": first_name or False,
            "middle_name": middle_name or False,
            "last_name": last_name or False,
            "email": first("email", "workEmail", "personalEmail") or False,
            "mobile": first("mobile", "phone") or False,
            "department": first("department", "departmentName") or False,
            "designation": first("designation", "designationName", "title") or False,
            "employment_status": employment_status,
            "date_of_joining": self._parse_date(
                first("dateOfJoin", "dateOfJoining", "doj")
            ),
            "employee_id": hr_emp.id or False,
            "raw_payload": self._dump_raw(raw),
        }

    def _sync_leave_types(self):
        self.ensure_one()
        LeaveType = self.env["greythr.leave.type"].sudo()
        Employee = self.env["greythr.employee"].sudo()
        created = updated = 0
        now = fields.Datetime.now()
        year = self.default_leave_year or fields.Date.today().year
        employees = Employee.search(
            [
                ("instance_id", "=", self.id),
                ("external_employee_id", "!=", False),
            ],
            limit=15,
        )
        if not employees:
            _logger.warning(
                "greytHR leave-types: no employees loaded on instance %s; "
                "skipping types sync. Run Sync Employees first.",
                self.id,
            )
            return 0, 0
        seen_codes = set()
        for emp in employees:
            path = "/leave/v2/employee/%s/years/%s/balance" % (
                emp.external_employee_id,
                int(year),
            )
            try:
                response = self._request("GET", path)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "greytHR leave-types: balance fetch failed for employee %s: %s",
                    emp.external_employee_id,
                    exc,
                )
                continue
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
            flat_rows = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                sub = r.get("list")
                if isinstance(sub, list):
                    for item in sub:
                        if isinstance(item, dict):
                            flat_rows.append(item)
                else:
                    flat_rows.append(r)
            for child in flat_rows:
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
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                existing = LeaveType.search(
                    [
                        ("instance_id", "=", self.id),
                        ("external_code", "=", code),
                    ],
                    limit=1,
                )
                self._ensure_leave_type(code, name_source)
                if existing:
                    updated += 1
                else:
                    created += 1
        self.sudo().write(
            {
                "last_leave_type_sync_at": now,
                "last_leave_type_sync_count": created + updated,
                "last_leave_type_sync_error": False,
            }
        )
        return created, updated

    def _sync_leave_types_legacy(self):
        self.ensure_one()
        LeaveType = self.env["greythr.leave.type"].sudo()
        HrLeaveType = self.env["hr.leave.type"].sudo()
        created = updated = 0
        now = fields.Datetime.now()
        seen_codes = set()
        try:
            iterator = list(self._paginated("/leave/v2/leave-types"))
        except UserError as exc:
            msg = str(exc)
            if "404" in msg or "no Route" in msg:
                return 0, 0
            raise
        for raw in iterator:
            code = str(
                raw.get("leaveTypeCategory")
                or raw.get("code")
                or raw.get("leaveTypeCode")
                or ""
            ).strip()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            name = raw.get("name") or raw.get("leaveTypeName") or code
            hr_type = HrLeaveType.search(
                [("ethara_leave_code", "=", code.lower())], limit=1
            )
            vals = {
                "name": name,
                "external_code": code,
                "holiday_status_id": hr_type.id or False,
                "last_sync_at": now,
                "raw_payload": self._dump_raw(raw),
            }
            existing = LeaveType.search(
                [("instance_id", "=", self.id), ("external_code", "=", code)],
                limit=1,
            )
            if existing:
                existing.write(vals)
                updated += 1
            else:
                vals.update({"instance_id": self.id})
                LeaveType.create(vals)
                created += 1
        self.sudo().write(
            {
                "last_leave_type_sync_at": now,
                "last_leave_type_sync_count": created + updated,
                "last_leave_type_sync_error": False,
            }
        )
        return created, updated

    def _get_leave_type(self, code):
        self.ensure_one()
        if isinstance(code, dict):
            code = (
                code.get("code")
                or code.get("id")
                or code.get("category")
                or ""
            )
        if not code:
            return self.env["greythr.leave.type"]
        code = str(code).strip()
        if not code:
            return self.env["greythr.leave.type"]
        return (
            self.env["greythr.leave.type"]
            .sudo()
            .search(
                [("instance_id", "=", self.id), ("external_code", "=", code)],
                limit=1,
            )
        )

    def _ensure_leave_type(self, code, name=None):
        self.ensure_one()
        if not code:
            return self.env["greythr.leave.type"]
        code = str(code).strip()
        raw_dict = name if isinstance(name, dict) else None
        if isinstance(name, dict):
            name = (
                name.get("description")
                or name.get("name")
                or name.get("label")
                or name.get("value")
                or name.get("categoryDesc")
                or name.get("leaveTypeName")
                or name.get("leaveTypeDesc")
                or ""
            )
        if name is not None and not isinstance(name, str):
            name = str(name)
        clean_name = (name or "").strip() or code
        write_vals = {
            "name": clean_name,
            "last_sync_at": fields.Datetime.now(),
        }
        if raw_dict is not None:
            write_vals["raw_payload"] = self._dump_raw(raw_dict)
        LeaveType = self.env["greythr.leave.type"].sudo()
        lt = LeaveType.search(
            [("instance_id", "=", self.id), ("external_code", "=", code)], limit=1
        )
        if lt:
            lt.write(write_vals)
            return lt
        if clean_name and clean_name != code:
            lt = LeaveType.search(
                [
                    ("instance_id", "=", self.id),
                    ("name", "=ilike", clean_name),
                ],
                limit=1,
            )
            if lt:
                write_vals["external_code"] = code
                lt.write(write_vals)
                return lt
        HrLeaveType = self.env["hr.leave.type"].sudo()
        hr_type = HrLeaveType.search(
            [("ethara_leave_code", "=", code.lower())], limit=1
        )
        write_vals.update(
            {
                "instance_id": self.id,
                "external_code": code,
                "holiday_status_id": hr_type.id or False,
            }
        )
        return LeaveType.create(write_vals)

    def _sync_leave_balances(self, year):
        self.ensure_one()
        self._sync_leave_types()
        Employee = self.env["greythr.employee"].sudo()
        Balance = self.env["greythr.leave.balance"].sudo()
        created = updated = 0
        now = fields.Datetime.now()
        employees = Employee.search(
            [
                ("instance_id", "=", self.id),
                ("external_employee_id", "!=", False),
            ]
        )
        for emp in employees:
            path = "/leave/v2/employee/%s/years/%s/balance" % (
                emp.external_employee_id,
                int(year),
            )
            try:
                response = self._request("GET", path)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "greytHR balance fetch failed for %s: %s",
                    emp.external_employee_id,
                    exc,
                )
                continue
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
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
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
                leave_type = self._get_leave_type(code) or self._ensure_leave_type(code, name_source)
                if not leave_type:
                    continue
                vals = {
                    "greythr_employee_id": emp.id,
                    "leave_type_id": leave_type.id,
                    "year": int(year),
                    "opening_balance": self._to_float(raw.get("openingBalance")),
                    "granted": self._to_float(raw.get("granted")),
                    "availed": self._to_float(raw.get("availed")),
                    "applied": self._to_float(raw.get("applied")),
                    "lapsed": self._to_float(raw.get("lapsed")),
                    "deducted": self._to_float(raw.get("deducted")),
                    "encashed": self._to_float(raw.get("encashed")),
                    "current_balance": self._to_float(
                        raw.get("currentBalance") or raw.get("balance")
                    ),
                    "last_sync_at": now,
                    "raw_payload": self._dump_raw(raw),
                }
                existing = Balance.search(
                    [
                        ("greythr_employee_id", "=", emp.id),
                        ("year", "=", int(year)),
                        ("leave_type_id", "=", leave_type.id),
                    ],
                    limit=1,
                )
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Balance.create(vals)
                    created += 1
        self.sudo().write(
            {
                "last_leave_balance_sync_at": now,
                "last_leave_balance_sync_count": created + updated,
                "last_leave_balance_sync_error": False,
            }
        )
        return created, updated

    def _sync_leave_transactions(self, start, end):
        self.ensure_one()
        self._sync_leave_types()
        Employee = self.env["greythr.employee"].sudo()
        Transaction = self.env["greythr.leave.transaction"].sudo()
        created = updated = 0
        now = fields.Datetime.now()
        employees = Employee.search(
            [
                ("instance_id", "=", self.id),
                ("external_employee_id", "!=", False),
            ]
        )
        for emp in employees:
            for chunk_start, chunk_end in self._chunk_date_range(start, end, days=30):
                params = {
                    "start": fields.Date.to_string(chunk_start),
                    "end": fields.Date.to_string(chunk_end),
                }
                path = "/leave/v2/employee/%s/transactions" % emp.external_employee_id
                try:
                    response = self._request("GET", path, params=params)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "greytHR transactions fetch failed for %s: %s",
                        emp.external_employee_id,
                        exc,
                    )
                    continue
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
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    child_list = raw.get("list") if isinstance(raw.get("list"), list) else [raw]
                    for child in child_list:
                        if not isinstance(child, dict):
                            continue
                        external_txn_id = str(
                            child.get("id")
                            or child.get("transactionId")
                            or child.get("leaveTransactionId")
                            or ""
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
                        leave_type = self._get_leave_type(code) or self._ensure_leave_type(code, name_source)
                        if not leave_type:
                            continue
                        vals = {
                            "external_transaction_id": external_txn_id,
                            "greythr_employee_id": emp.id,
                            "leave_type_id": leave_type.id,
                            "transaction_type": self._map_transaction_type(
                                child.get("leaveTransactionType")
                                or child.get("transactionType")
                            ),
                            "from_date": self._parse_date(child.get("fromDate")),
                            "to_date": self._parse_date(child.get("toDate")),
                            "number_of_days": self._to_float(
                                child.get("days") or child.get("numberOfDays")
                            ),
                            "sessions": self._format_sessions(child),
                            "status": (
                                "cancelled"
                                if child.get("cancelled")
                                else self._map_status(child.get("status"))
                            ),
                            "remarks": child.get("remarks") or False,
                            "reason": child.get("reason") or False,
                            "last_sync_at": now,
                            "raw_payload": self._dump_raw(child),
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
        self.sudo().write(
            {
                "last_leave_transaction_sync_at": now,
                "last_leave_transaction_sync_count": created + updated,
                "last_leave_transaction_sync_error": False,
            }
        )
        return created, updated

    def _push_employee_to_greythr(self, hr_employee):
        self.ensure_one()
        if not hr_employee:
            return False
        payload = self._employee_push_payload(hr_employee)
        result = self._request("POST", "/employee/v2/employees", json=payload)
        external_id = ""
        if isinstance(result, dict):
            external_id = str(
                result.get("employeeId")
                or result.get("id")
                or result.get("employeeNumber")
                or ""
            ).strip()
        Employee = self.env["greythr.employee"].sudo()
        vals = {
            "name": hr_employee.name or (external_id or "Unnamed"),
            "email": (
                hr_employee.work_email
                or getattr(hr_employee, "private_email", False)
                or False
            ),
            "mobile": (
                getattr(hr_employee, "mobile_phone", False)
                or getattr(hr_employee, "work_phone", False)
                or False
            ),
            "department": hr_employee.department_id.name if hr_employee.department_id else False,
            "designation": hr_employee.job_title or False,
            "employee_id": hr_employee.id,
            "last_sync_at": fields.Datetime.now(),
            "raw_payload": self._dump_raw(result if isinstance(result, dict) else {}),
        }
        if external_id:
            existing = Employee.search(
                [
                    ("instance_id", "=", self.id),
                    ("external_employee_id", "=", external_id),
                ],
                limit=1,
            )
            if existing:
                existing.write(vals)
            else:
                vals.update(
                    {
                        "instance_id": self.id,
                        "external_employee_id": external_id,
                    }
                )
                Employee.create(vals)
            if (
                "employee_code" in hr_employee._fields
                and not hr_employee.employee_code
            ):
                hr_employee.sudo().write({"employee_code": external_id})
        return external_id or True

    def _employee_push_payload(self, hr_employee):
        name = hr_employee.name or ""
        parts = name.split(" ", 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        payload = {
            "firstName": first,
            "lastName": last,
            "email": hr_employee.work_email or None,
            "mobile": getattr(hr_employee, "mobile_phone", False) or None,
            "employeeNo": getattr(hr_employee, "employee_code", False) or None,
            "department": hr_employee.department_id.name if hr_employee.department_id else None,
            "designation": hr_employee.job_title or None,
        }
        return {k: v for k, v in payload.items() if v}

    def _push_leave_to_greythr(self, hr_leave, action="create"):
        self.ensure_one()
        if not hr_leave:
            return False
        Request = self.env["greythr.leave.request"].sudo()
        Employee = self.env["greythr.employee"].sudo()
        LeaveType = self.env["greythr.leave.type"].sudo()
        gr_emp = False
        if hr_leave.employee_id:
            gr_emp = Employee.search(
                [
                    ("instance_id", "=", self.id),
                    ("employee_id", "=", hr_leave.employee_id.id),
                ],
                limit=1,
            )
        gr_lt = False
        if hr_leave.holiday_status_id:
            gr_lt = LeaveType.search(
                [
                    ("instance_id", "=", self.id),
                    ("holiday_status_id", "=", hr_leave.holiday_status_id.id),
                ],
                limit=1,
            )
        req = Request.search(
            [
                ("instance_id", "=", self.id),
                ("hr_leave_id", "=", hr_leave.id),
            ],
            limit=1,
        )
        common_vals = {
            "hr_leave_id": hr_leave.id,
            "instance_id": self.id,
            "greythr_employee_id": gr_emp.id if gr_emp else False,
            "leave_type_id": gr_lt.id if gr_lt else False,
            "from_date": hr_leave.date_from and hr_leave.date_from.date() or False,
            "to_date": hr_leave.date_to and hr_leave.date_to.date() or False,
            "number_of_days": hr_leave.number_of_days or 0.0,
            "reason": hr_leave.name or False,
            "direction": "outbound",
        }
        if not req:
            req = Request.create(common_vals)
        else:
            req.write(common_vals)
        try:
            if action == "create":
                payload = self._leave_push_payload(hr_leave, gr_emp, gr_lt)
                result = self._request(
                    "POST", "/leave/v2/employee/requests", json=payload
                )
                external_id = ""
                if isinstance(result, dict):
                    external_id = str(
                        result.get("requestId") or result.get("id") or ""
                    ).strip()
                req.write(
                    {
                        "external_request_id": external_id or req.external_request_id,
                        "status": "pending",
                        "push_state": "sent",
                        "push_error": False,
                        "last_sync_at": fields.Datetime.now(),
                        "raw_payload": self._dump_raw(
                            result if isinstance(result, dict) else {}
                        ),
                    }
                )
            elif action == "approve":
                ext = req.external_request_id
                if not ext:
                    req.write(
                        {
                            "status": "approved",
                            "push_error": "Missing external_request_id; approval not pushed.",
                        }
                    )
                    return False
                path = "/leave/v2/employee/requests/%s/approve" % ext
                result = self._request("POST", path)
                req.write(
                    {
                        "status": "approved",
                        "push_state": "sent",
                        "push_error": False,
                        "last_sync_at": fields.Datetime.now(),
                        "raw_payload": self._dump_raw(
                            result if isinstance(result, dict) else {}
                        ),
                    }
                )
            elif action == "refuse":
                ext = req.external_request_id
                if not ext:
                    req.write(
                        {
                            "status": "refused",
                            "push_error": "Missing external_request_id; refusal not pushed.",
                        }
                    )
                    return False
                path = "/leave/v2/employee/requests/%s/reject" % ext
                result = self._request("POST", path)
                req.write(
                    {
                        "status": "refused",
                        "push_state": "sent",
                        "push_error": False,
                        "last_sync_at": fields.Datetime.now(),
                        "raw_payload": self._dump_raw(
                            result if isinstance(result, dict) else {}
                        ),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            req.write(
                {
                    "push_state": "failed",
                    "push_error": str(exc)[:1000],
                    "last_sync_at": fields.Datetime.now(),
                }
            )
            raise
        return True

    def _leave_push_payload(self, hr_leave, gr_emp, gr_lt):
        return {
            "employeeId": gr_emp.external_employee_id if gr_emp else None,
            "leaveTypeCategory": gr_lt.external_code if gr_lt else None,
            "fromDate": (
                fields.Date.to_string(hr_leave.date_from.date())
                if hr_leave.date_from
                else None
            ),
            "toDate": (
                fields.Date.to_string(hr_leave.date_to.date())
                if hr_leave.date_to
                else None
            ),
            "numberOfDays": hr_leave.number_of_days or 0.0,
            "reason": hr_leave.name or "",
        }

    def _sync_payroll(self, year, month):
        self.ensure_one()
        Employee = self.env["greythr.employee"].sudo()
        Payroll = self.env["greythr.payroll"].sudo()
        created = updated = 0
        now = fields.Datetime.now()
        params = {"year": int(year), "month": int(month)}
        for raw in self._paginated("/payroll/v2/employees/payslips", params=params):
            external_emp_id = str(
                raw.get("employeeId") or raw.get("employee_id") or ""
            ).strip()
            if not external_emp_id:
                continue
            emp = Employee.search(
                [
                    ("instance_id", "=", self.id),
                    ("external_employee_id", "=", external_emp_id),
                ],
                limit=1,
            )
            if not emp:
                emp = Employee.create(
                    {
                        "instance_id": self.id,
                        "external_employee_id": external_emp_id,
                        "name": raw.get("employeeName") or external_emp_id,
                        "last_sync_at": now,
                    }
                )
            external_payslip_id = (
                str(raw.get("payslipId") or raw.get("id") or "").strip() or False
            )
            vals = {
                "greythr_employee_id": emp.id,
                "instance_id": self.id,
                "external_payslip_id": external_payslip_id,
                "period_year": int(year),
                "period_month": int(month),
                "payslip_date": self._parse_date(
                    raw.get("payslipDate") or raw.get("paidOn")
                ),
                "currency": raw.get("currency") or False,
                "gross_salary": self._to_float(
                    raw.get("grossSalary") or raw.get("gross")
                ),
                "net_salary": self._to_float(
                    raw.get("netSalary")
                    or raw.get("net")
                    or raw.get("takeHome")
                ),
                "total_earnings": self._to_float(
                    raw.get("totalEarnings") or raw.get("earnings")
                ),
                "total_deductions": self._to_float(
                    raw.get("totalDeductions") or raw.get("deductions")
                ),
                "tax": self._to_float(raw.get("tax") or raw.get("tds")),
                "paid_days": self._to_float(raw.get("paidDays")),
                "lop_days": self._to_float(raw.get("lopDays")),
                "status": raw.get("status") or False,
                "last_sync_at": now,
                "raw_payload": self._dump_raw(raw),
            }
            existing = Payroll.search(
                [
                    ("greythr_employee_id", "=", emp.id),
                    ("period_year", "=", int(year)),
                    ("period_month", "=", int(month)),
                ],
                limit=1,
            )
            if existing:
                existing.write(vals)
                updated += 1
            else:
                Payroll.create(vals)
                created += 1
        self.sudo().write(
            {
                "last_payroll_sync_at": now,
                "last_payroll_sync_count": created + updated,
                "last_payroll_sync_error": False,
            }
        )
        return created, updated

    def _sync_leave_requests(self):
        self.ensure_one()
        self._sync_leave_types()
        Employee = self.env["greythr.employee"].sudo()
        Request = self.env["greythr.leave.request"].sudo()
        created = updated = 0
        now = fields.Datetime.now()
        end = fields.Date.today()
        start = end - timedelta(days=max(1, self.transactions_lookback_days or 30))
        for chunk_start, chunk_end in self._chunk_date_range(start, end, days=30):
            params = {
                "start": fields.Date.to_string(chunk_start),
                "end": fields.Date.to_string(chunk_end),
            }
            try:
                iterator = list(
                    self._paginated("/leave/v2/employee/requests", params=params)
                )
            except UserError as exc:
                msg = str(exc)
                if "404" in msg or "no Route" in msg:
                    _logger.warning(
                        "greytHR /leave/v2/employee/requests not available on tenant. "
                        "Inbound leave-request sync skipped. "
                        "Outbound push (approvals/refusals) still works."
                    )
                    self.sudo().write(
                        {
                            "last_leave_request_sync_at": now,
                            "last_leave_request_sync_count": 0,
                            "last_leave_request_sync_error": (
                                "Inbound endpoint not available on this tenant. "
                                "Use Sync Leave Transactions for historical leave data. "
                                "Outbound push to greytHR still works."
                            ),
                        }
                    )
                    return 0, 0
                raise
            for raw in iterator:
                external_id = str(
                    raw.get("requestId") or raw.get("id") or ""
                ).strip()
                if not external_id:
                    continue
                external_emp_id = str(raw.get("employeeId") or "").strip()
                emp = False
                if external_emp_id:
                    emp = Employee.search(
                        [
                            ("instance_id", "=", self.id),
                            ("external_employee_id", "=", external_emp_id),
                        ],
                        limit=1,
                    )
                    if not emp:
                        emp = Employee.create(
                            {
                                "instance_id": self.id,
                                "external_employee_id": external_emp_id,
                                "name": raw.get("employeeName") or external_emp_id,
                                "last_sync_at": now,
                            }
                        )
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
                leave_type = self._get_leave_type(code) or self._ensure_leave_type(code, name_source)
                if not leave_type:
                    continue
                vals = {
                    "greythr_employee_id": emp.id if emp else False,
                    "instance_id": self.id,
                    "leave_type_id": leave_type.id or False,
                    "from_date": self._parse_date(raw.get("fromDate")),
                    "to_date": self._parse_date(raw.get("toDate")),
                    "number_of_days": self._to_float(raw.get("numberOfDays")),
                    "sessions": raw.get("sessions") or False,
                    "reason": raw.get("reason") or False,
                    "remarks": raw.get("remarks") or False,
                    "status": self._map_request_status(raw.get("status")),
                    "direction": "inbound",
                    "push_state": "sent",
                    "last_sync_at": now,
                    "raw_payload": self._dump_raw(raw),
                }
                existing = Request.search(
                    [
                        ("instance_id", "=", self.id),
                        ("external_request_id", "=", external_id),
                    ],
                    limit=1,
                )
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    vals["external_request_id"] = external_id
                    Request.create(vals)
                    created += 1
        self.sudo().write(
            {
                "last_leave_request_sync_at": now,
                "last_leave_request_sync_count": created + updated,
                "last_leave_request_sync_error": False,
            }
        )
        return created, updated

    @staticmethod
    def _dump_raw(raw):
        try:
            import json

            return json.dumps(raw, default=str)[:8000]
        except Exception:  # noqa: BLE001
            return str(raw)[:8000]

    @staticmethod
    def _to_float(value):
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_date(value):
        if not value:
            return False
        if isinstance(value, str):
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%d-%m-%Y"):
                try:
                    return datetime.strptime(value[: len(fmt) + 6], fmt).date()
                except ValueError:
                    continue
        return False

    @staticmethod
    def _chunk_date_range(start, end, days=30):
        cur = start
        step = timedelta(days=days)
        while cur <= end:
            chunk_end = min(cur + step - timedelta(days=1), end)
            yield cur, chunk_end
            cur = chunk_end + timedelta(days=1)

    @staticmethod
    def _format_sessions(child):
        fs = child.get("fromSession")
        ts = child.get("toSession")
        if fs is not None or ts is not None:
            return "%s-%s" % (fs if fs is not None else "?", ts if ts is not None else "?")
        return child.get("sessions") or False

    @staticmethod
    def _map_transaction_type(value):
        if not value:
            return False
        v = str(value).strip().lower()
        mapping = {
            "credit": "credit",
            "debit": "debit",
            "grant": "credit",
            "avail": "debit",
            "apply": "debit",
            "opening": "opening",
            "closing": "closing",
            "encash": "encash",
            "lapse": "lapse",
        }
        return mapping.get(v, "other")

    @staticmethod
    def _map_status(value):
        if not value:
            return False
        v = str(value).strip().lower()
        mapping = {
            "applied": "applied",
            "approved": "approved",
            "rejected": "rejected",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "pending": "applied",
            "withdrawn": "cancelled",
        }
        return mapping.get(v, "other")

    @staticmethod
    def _map_request_status(value):
        if not value:
            return False
        v = str(value).strip().lower()
        mapping = {
            "applied": "pending",
            "pending": "pending",
            "submitted": "pending",
            "approved": "approved",
            "rejected": "refused",
            "refused": "refused",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "withdrawn": "cancelled",
        }
        return mapping.get(v, "other")

    @api.model
    def _cron_sync_all_instances(self):
        for instance in self.sudo().search([("active", "=", True)]):
            try:
                instance._sync_employees()
            except Exception as exc:  # noqa: BLE001
                _logger.exception("greytHR employee sync failed for %s", instance.name)
                instance.sudo().write(
                    {"last_employee_sync_error": str(exc)[:1000]}
                )
            try:
                instance._sync_leave_types()
            except Exception as exc:  # noqa: BLE001
                _logger.exception("greytHR leave type sync failed for %s", instance.name)
                instance.sudo().write(
                    {"last_leave_type_sync_error": str(exc)[:1000]}
                )
            try:
                instance._sync_leave_balances(
                    instance.default_leave_year or fields.Date.today().year
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "greytHR leave balance sync failed for %s", instance.name
                )
                instance.sudo().write(
                    {"last_leave_balance_sync_error": str(exc)[:1000]}
                )
            try:
                end = fields.Date.today()
                start = end - timedelta(
                    days=max(1, instance.transactions_lookback_days or 30)
                )
                instance._sync_leave_transactions(start, end)
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "greytHR leave transaction sync failed for %s", instance.name
                )
                instance.sudo().write(
                    {"last_leave_transaction_sync_error": str(exc)[:1000]}
                )
            try:
                instance._sync_leave_requests()
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "greytHR leave request sync failed for %s", instance.name
                )
                instance.sudo().write(
                    {"last_leave_request_sync_error": str(exc)[:1000]}
                )
            if instance.enable_payroll_sync:
                try:
                    instance._sync_payroll(
                        instance.default_payroll_year or fields.Date.today().year,
                        instance.default_payroll_month or fields.Date.today().month,
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.exception(
                        "greytHR payroll sync failed for %s", instance.name
                    )
                    instance.sudo().write(
                        {"last_payroll_sync_error": str(exc)[:1000]}
                    )
