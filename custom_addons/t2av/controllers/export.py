from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import content_disposition, request

_logger = logging.getLogger(__name__)

HEADERS = [
    "No",
    "Date",
    "item_id",
    "category",
    "sub_category",
    "style",
    "priority",
    "complexity",
    "language",
    "topic",
    "prompt",
    "golden_prompt",
    "meta_prompt",
    "video_file",
    "duration_seconds",
    "resolution",
    "fps",
    "contains_dialogue",
    "speaker_count",
]

_DEFAULT_FPS = 24


def _parse_ids(raw) -> list[int]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        candidates = raw
    else:
        candidates = str(raw).split(",")
    result = []
    for item in candidates:
        text = str(item).strip()
        if not text:
            continue
        try:
            result.append(int(text))
        except ValueError:
            raise UserError(_("Invalid record id: %s") % text)
    return result


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _human_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return f"{value.day} {value.strftime('%B %Y')}"


def _item_id(rec) -> str:
    attempt = rec.active_attempt_id
    if attempt and attempt.video_file:
        return attempt.video_file
    return ""


def _row(idx: int, rec) -> list:
    completed = rec.completed_at or rec.write_date
    return [
        idx,
        _human_date(completed),
        _item_id(rec),
        rec.category or "",
        rec.sub_category or "",
        rec.style or "",
        rec.priority or "",
        rec.complexity or "",
        rec.language or "",
        rec.topic or "",
        rec.prompt or "",
        rec.golden_prompt or "",
        rec.meta_prompt or "",
        rec.video_s3_url or "",
        rec.duration_seconds or 0,
        rec.resolution or "",
        _DEFAULT_FPS,
        bool(rec.dialogue_transcript),
        rec.speaker_count or 0,
    ]


def _filename(fmt: str, count: int) -> str:
    today = date.today()
    iso = today.strftime("%Y%m%d")
    ord_day = _ordinal(today.day)
    month = today.strftime("%B").lower()
    return f"[INT] T2AV-delivery-{iso} - {ord_day} {month} {count} tasks.{fmt}"


class T2AVExportController(http.Controller):

    @http.route(
        "/t2av/export/complete",
        type="http",
        auth="user",
        methods=["GET", "POST"],
        csrf=False,
    )
    def export_complete(self, format="csv", ids=None, **_kwargs):
        fmt = (format or "csv").lower()
        if fmt not in ("csv", "xlsx"):
            raise UserError(_("Unsupported export format: %s") % format)

        Generation = request.env["t2av.generation"]
        if not Generation.check_access_rights("read", raise_exception=False):
            raise UserError(_("You are not allowed to export t2av.generation records."))

        domain = [("state", "=", "done")]
        selected_ids = _parse_ids(ids)
        if selected_ids:
            domain.append(("id", "in", selected_ids))

        records = Generation.search(
            domain,
            order="completed_at desc, id desc",
        )
        rows = [_row(i + 1, rec) for i, rec in enumerate(records)]
        filename = _filename(fmt, len(rows))

        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(HEADERS)
            writer.writerows(rows)
            content = buf.getvalue().encode("utf-8-sig")
            content_type = "text/csv; charset=utf-8"
        else:
            try:
                from openpyxl import Workbook
            except ImportError as exc:
                raise UserError(_(
                    "openpyxl is required for xlsx export. "
                    "Install it with: pip install openpyxl"
                )) from exc
            wb = Workbook(write_only=True)
            ws = wb.create_sheet(title="Complete")
            ws.append(HEADERS)
            for row in rows:
                ws.append(row)
            bio = io.BytesIO()
            wb.save(bio)
            content = bio.getvalue()
            content_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        return request.make_response(
            content,
            headers=[
                ("Content-Type", content_type),
                ("Content-Length", len(content)),
                ("Content-Disposition", content_disposition(filename)),
            ],
        )
