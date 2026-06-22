"""Export service: produce CSV / XLSX bytes from a queryset of employees.

The XLSX format follows the branded format used elsewhere in the codebase
(see ``task_forge_core/controllers/export_controllers.py``) so downloads
look consistent across modules.
"""

import csv
import io
import logging
from datetime import datetime
from typing import Iterable, Optional

from odoo import _
from odoo.exceptions import UserError

from ..models.hr_employee import ROLE_HIERARCHY_FIELDS, ROLE_SELECTION
from .employee_repository import EmployeeRepository
from .employee_service import EmployeeService

_logger = logging.getLogger(__name__)

# Brand colours - mirrors task_forge_core for visual consistency.
PRIMARY = "#1B2A4A"
ACCENT = "#2E86DE"
LIGHT_BG = "#F0F4F8"
WHITE = "#FFFFFF"
BORDER = "#CBD5E1"
TEXT_DARK = "#1E293B"

CSV_COLUMNS = [
    "employee_id",
    "name",
    "email",
    "role",
    "role_label",
    "job_title",
    "assigned_ql",
    "assigned_pl",
    "assigned_tpm",
    "status",
    "created_at",
    "updated_at",
]
ROLE_LABELS = dict(ROLE_SELECTION)


class ExportService:
    def __init__(self, env):
        self.env = env
        self.repo = EmployeeRepository(env)
        self.service = EmployeeService(env)

    # ── selection ────────────────────────────────────────────────────────────
    def _select(
        self,
        search: Optional[str] = None,
        role: Optional[str] = None,
        parent_id: Optional[int] = None,
        active: Optional[bool] = True,
        ids: Optional[Iterable[int]] = None,
        include_archived: bool = False,
        order: str = "employee_code asc, name asc",
    ):
        domain = self.repo.build_domain(
            search=search,
            role=role,
            parent_id=parent_id,
            active=None if include_archived else active,
            ids=ids,
        )
        return self.repo.list_employees(
            domain, offset=0, limit=None, order=order,
            include_archived=include_archived,
        )

    # ── CSV ──────────────────────────────────────────────────────────────────
    def to_csv(self, **filters) -> bytes:
        employees = self._select(**filters)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_COLUMNS)
        for emp in employees:
            writer.writerow(self._row(emp))
        return buf.getvalue().encode("utf-8-sig")

    # ── XLSX ─────────────────────────────────────────────────────────────────
    def to_xlsx(self, **filters) -> bytes:
        try:
            import xlsxwriter  # noqa: PLC0415 - heavy dep, lazy import
        except ImportError as exc:
            raise UserError(
                _("Excel export requires the 'xlsxwriter' Python package.")
            ) from exc

        employees = self._select(**filters)
        buf = io.BytesIO()
        workbook = xlsxwriter.Workbook(buf, {"in_memory": True})
        sheet = workbook.add_worksheet("Employees")
        fmt = self._formats(workbook)

        # Title banner
        title = "Employee Role Import - Employees"
        ts = datetime.now().strftime("%d %b %Y, %I:%M %p")
        sheet.merge_range(0, 0, 0, len(CSV_COLUMNS) - 1, title, fmt["title"])
        sheet.merge_range(
            1, 0, 1, len(CSV_COLUMNS) - 1,
            f"Downloaded on: {ts}  •  {len(employees)} record(s)",
            fmt["subtitle"],
        )
        sheet.set_row(0, 32)
        sheet.set_row(1, 20)

        # Headers
        header_row = 3
        for col, header in enumerate(CSV_COLUMNS):
            sheet.write(header_row, col, header.replace("_", " ").title(), fmt["header"])
        sheet.set_row(header_row, 24)

        # Data rows
        for r, emp in enumerate(employees, start=header_row + 1):
            row = self._row(emp)
            for col, value in enumerate(row):
                style = fmt["cell_alt"] if r % 2 == 0 else fmt["cell"]
                sheet.write(r, col, value if value is not None else "", style)

        widths = [16, 28, 32, 12, 18, 24, 24, 24, 24, 12, 22, 22]
        for col, width in enumerate(widths[: len(CSV_COLUMNS)]):
            sheet.set_column(col, col, width)

        sheet.freeze_panes(header_row + 1, 0)
        sheet.autofilter(header_row, 0, max(header_row, len(employees) + header_row), len(CSV_COLUMNS) - 1)
        workbook.close()
        return buf.getvalue()

    def _row(self, emp):
        applicable = ROLE_HIERARCHY_FIELDS.get(emp.role or "", ())
        assigned_ql = self.repo.resolve_assigned_ql(emp) if "ql" in applicable else None
        assigned_pl = self.repo.resolve_assigned_pl(emp) if "pl" in applicable else None
        assigned_tpm = self.repo.resolve_assigned_tpm(emp) if "tpm" in applicable else None
        return [
            emp.employee_code or "",
            emp.name or "",
            emp.work_email or "",
            emp.role or "",
            ROLE_LABELS.get(emp.role, "") if emp.role else "",
            emp.job_title or "",
            assigned_ql.name if assigned_ql else "",
            assigned_pl.name if assigned_pl else "",
            assigned_tpm.name if assigned_tpm else "",
            "Active" if emp.active else "Archived",
            emp.create_date.strftime("%Y-%m-%d %H:%M") if emp.create_date else "",
            emp.write_date.strftime("%Y-%m-%d %H:%M") if emp.write_date else "",
        ]

    @staticmethod
    def _formats(wb):
        return {
            "title": wb.add_format({
                "bold": True, "font_size": 16, "font_color": WHITE,
                "bg_color": PRIMARY, "align": "left", "valign": "vcenter",
                "font_name": "Calibri",
            }),
            "subtitle": wb.add_format({
                "italic": True, "font_size": 10, "font_color": LIGHT_BG,
                "bg_color": PRIMARY, "align": "left", "valign": "vcenter",
                "font_name": "Calibri",
            }),
            "header": wb.add_format({
                "bold": True, "font_size": 11, "font_color": WHITE,
                "bg_color": ACCENT, "align": "center", "valign": "vcenter",
                "border": 1, "border_color": BORDER, "font_name": "Calibri",
            }),
            "cell": wb.add_format({
                "font_size": 10, "font_color": TEXT_DARK,
                "align": "left", "valign": "vcenter",
                "border": 1, "border_color": BORDER, "font_name": "Calibri",
            }),
            "cell_alt": wb.add_format({
                "font_size": 10, "font_color": TEXT_DARK,
                "bg_color": LIGHT_BG, "align": "left", "valign": "vcenter",
                "border": 1, "border_color": BORDER, "font_name": "Calibri",
            }),
        }
