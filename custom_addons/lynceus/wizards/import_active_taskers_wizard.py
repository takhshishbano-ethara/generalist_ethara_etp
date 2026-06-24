from __future__ import annotations

import base64
import csv
import io
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..services import allocator

_logger = logging.getLogger(__name__)


HEADER_MAP = {
    "email": "email",
    "name": "name",
}


class LynceusImportActiveTaskersWizard(models.TransientModel):
    _name = "lynceus.import.active.taskers.wizard"
    _description = "Lynceus Import Active Taskers (CSV)"

    file = fields.Binary(string="Upload CSV (email,name)", required=True)
    filename = fields.Char(string="Filename")
    quota_override = fields.Integer(
        string="Per-Tasker Quota Override",
        help="Leave 0 to use each user's lynceus_daily_quota or the system default.",
    )
    imported_count = fields.Integer(readonly=True)
    allocated_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True)
    allocation_summary = fields.Html(
        readonly=True,
        sanitize=False,
        help="Per-user allocation breakdown: who got how many prompts, and why.",
    )
    error_log = fields.Text(readonly=True)

    def action_import(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Please upload a CSV file."))

        raw = base64.b64decode(self.file)
        rows = self._read_csv(raw)
        if not rows:
            raise UserError(_("File contains no data rows."))

        header_raw = [str(c or "").strip().lower() for c in rows[0]]
        if "email" not in header_raw:
            raise UserError(_(
                "Missing required column 'email'. Headers found: %s"
            ) % ", ".join(header_raw))
        header_map = [HEADER_MAP.get(h) for h in header_raw]

        Users = self.env["res.users"].sudo()
        default_quota = int(
            self.env["ir.config_parameter"].sudo().get_param("lynceus.default_tasker_quota", "20") or "20"
        )

        imported = 0
        skipped = 0
        errors: list[str] = []
        user_quota_map: dict[int, int] = {}
        user_email_map: dict[int, str] = {}

        for idx, row in enumerate(rows[1:], start=2):
            try:
                email_val: str | None = None
                name_val: str | None = None
                for col_idx, field_name in enumerate(header_map):
                    if not field_name or col_idx >= len(row):
                        continue
                    cell = str(row[col_idx] or "").strip()
                    if field_name == "email":
                        email_val = cell or None
                    elif field_name == "name":
                        name_val = cell or None
                if not email_val:
                    continue

                user = Users.search([("login", "=ilike", email_val)], limit=1)
                lookup_method = "login"
                if not user and "@" in email_val:
                    user = Users.search([("email", "=ilike", email_val)], limit=1)
                    lookup_method = "email"
                if not user:
                    errors.append(
                        f"Row {idx}: '{email_val}' NOT FOUND (searched by login then by email). "
                        f"Verify the user exists with this exact login or email."
                    )
                    skipped += 1
                    continue

                if name_val and not user.name:
                    user.name = name_val

                quota = self.quota_override or user.lynceus_daily_quota or default_quota
                user.write({"lynceus_active_today": True})
                user_quota_map[user.id] = quota
                user_email_map[user.id] = email_val
                imported += 1
                _logger.info(
                    "Lynceus import row %d: matched '%s' to user id=%d (login='%s') by %s, quota=%d",
                    idx, email_val, user.id, user.login, lookup_method, quota,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Row {idx}: {exc}")
                skipped += 1
                _logger.warning("Lynceus active-taskers import row %d failed: %s", idx, exc)

        results: dict[int, dict] = {}
        if user_quota_map:
            results = allocator.allocate_to_users(self.env, user_quota_map)

        total_allocated = sum(r.get("allocated", 0) for r in results.values())
        any_pool_exhaustion = any(r.get("pool_was_empty") for r in results.values())

        self.env.cr.execute(
            "SELECT COUNT(*) FROM lynceus_prompt WHERE state = 'available'"
        )
        pool_remaining = self.env.cr.fetchone()[0] or 0

        BADGE = {
            "skip": '<span style="background:#e5e7eb;color:#4b5563;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:0.4px;">SKIP</span>',
            "ok":   '<span style="background:#dcfce7;color:#166534;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:0.4px;">OK</span>',
            "part": '<span style="background:#fef3c7;color:#92400e;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:0.4px;">PART</span>',
            "zero": '<span style="background:#fee2e2;color:#991b1b;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:0.4px;">ZERO</span>',
        }

        rows_html = []
        for uid, csv_email in user_email_map.items():
            res = results.get(uid, {})
            allocated = res.get("allocated", 0)
            had_before = res.get("had_before", 0)
            wanted_delta = res.get("wanted_delta", 0)
            quota = user_quota_map.get(uid, 0)
            after = had_before + allocated

            if wanted_delta == 0:
                badge = BADGE["skip"]
                allocation_cell = f"{had_before} / {quota}"
                note = "Already at or above quota - no top-up needed"
            elif allocated == wanted_delta:
                badge = BADGE["ok"]
                allocation_cell = f"<strong>+{allocated}</strong> &nbsp; {had_before} &rarr; {after} of {quota}"
                note = ""
            elif allocated > 0:
                badge = BADGE["part"]
                allocation_cell = f"<strong>+{allocated}</strong> of {wanted_delta} requested &nbsp; ({had_before} &rarr; {after} of {quota})"
                note = "Pool ran short during this user's turn"
            else:
                badge = BADGE["zero"]
                allocation_cell = f"0 of {wanted_delta} requested &nbsp; ({had_before} of quota {quota})"
                note = "Pool empty, or user already held every available prompt"

            rows_html.append(
                f"<tr>"
                f"<td style='padding:8px 12px;border-top:1px solid #f1f5f9;'>{csv_email}</td>"
                f"<td style='padding:8px 12px;border-top:1px solid #f1f5f9;text-align:center;'>{badge}</td>"
                f"<td style='padding:8px 12px;border-top:1px solid #f1f5f9;'>{allocation_cell}</td>"
                f"<td style='padding:8px 12px;border-top:1px solid #f1f5f9;color:#6b7280;font-size:12px;'>{note}</td>"
                f"</tr>"
            )

        warning_html = ""
        if any_pool_exhaustion:
            warning_html = (
                '<div style="margin-top:12px;padding:10px 14px;background:#fef3c7;color:#92400e;'
                'border-left:4px solid #f59e0b;border-radius:6px;font-size:13px;">'
                '<strong>POOL DEPLETED:</strong> One or more taskers got fewer prompts than their '
                "quota because the pool ran short. Run <em>Generate Batch</em> to refill the pool, "
                "then re-import."
                "</div>"
            )

        html_summary = (
            '<div>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            '<thead>'
            '<tr style="background:#f8fafc;text-align:left;">'
            '<th style="padding:8px 12px;font-weight:600;color:#475569;">Tasker</th>'
            '<th style="padding:8px 12px;font-weight:600;color:#475569;text-align:center;">Status</th>'
            '<th style="padding:8px 12px;font-weight:600;color:#475569;">Allocation</th>'
            '<th style="padding:8px 12px;font-weight:600;color:#475569;">Notes</th>'
            '</tr>'
            '</thead>'
            f'<tbody>{"".join(rows_html)}</tbody>'
            '</table>'
            f'<div style="margin-top:14px;padding:10px 14px;background:#f1f5f9;border-radius:6px;'
            f'font-size:13px;color:#1e293b;">'
            f'<strong>Total:</strong> {imported} taskers imported &middot; '
            f'<strong>{total_allocated}</strong> prompts allocated &middot; '
            f'{skipped} rows skipped &middot; '
            f'<strong>{pool_remaining}</strong> prompts remain available in the pool'
            f'</div>'
            f'{warning_html}'
            '</div>'
        )

        self.write({
            "imported_count": imported,
            "allocated_count": total_allocated,
            "skipped_count": skipped,
            "allocation_summary": html_summary,
            "error_log": "\n".join(errors[:100]) or False,
        })

        return {
            "type": "ir.actions.act_window",
            "name": _("Import Active Taskers - Results"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _read_csv(self, raw):
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        return [list(r) for r in reader]
