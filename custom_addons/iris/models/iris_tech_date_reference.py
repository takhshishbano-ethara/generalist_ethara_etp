"""Versioned central Tech Date Reference table (iris v1.1, P1-4).

Replaces the per-candidate free-text ``tech_date_reference`` field as the
authoritative source of technology release/GA dates consumed by the
screening prompt (rules B2/H5 — temporal impossibility checks).

Versioning model: at most ONE active row per technology
(case-insensitive). Updating a date means archiving the old row and
creating a new one — archived rows are the version history, and
``mail.thread`` tracking on every payload field gives a full audit trail.

``get_reference_markdown()`` renders the active rows as the trusted
(unfenced) ``| Technology | GA Date | Source |`` table injected into
``_llm_build_messages`` by ``iris.screening`` and
``iris.calibration.task``. The quarterly cron schedules a stock To-Do
review activity so dates never silently rot.
"""

import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class IrisTechDateReference(models.Model):
    _name = "iris.tech.date.reference"
    _description = "Iris Tech Date Reference"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "technology asc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    technology = fields.Char(
        string="Technology", required=True, tracking=True,
        help="Technology / product name as candidates claim it, "
             "e.g. \"MCP (Model Context Protocol)\" or \"ChatGPT\".",
    )
    ga_date = fields.Date(
        string="GA Date", required=True, tracking=True,
        help="General-availability (public release) date. Screeners may "
             "not assert dates from memory — this table is the only "
             "authoritative source fed to the screening LLM.",
    )
    source_url = fields.Char(
        string="Source URL", tracking=True,
        help="Link backing the GA date (announcement, changelog, ...).",
    )
    note = fields.Char(string="Note")
    active = fields.Boolean(
        default=True, tracking=True,
        help="Archive a row instead of editing its date: archived rows "
             "are the version history of this table.",
    )

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("technology", "active")
    def _check_unique_active_technology(self):
        """At most one ACTIVE row per technology (case-insensitive).

        Archived duplicates are explicitly allowed — that is how the
        table versions itself.
        """
        for rec in self:
            if not rec.active or not (rec.technology or "").strip():
                continue
            tech = rec.technology.strip().lower()
            others = self.search([
                ("id", "!=", rec.id),
                ("active", "=", True),
            ])
            if any((o.technology or "").strip().lower() == tech for o in others):
                raise ValidationError(_(
                    "An active Tech Date Reference row for '%s' already "
                    "exists (case-insensitive match). Archive the existing "
                    "row first — archived rows keep the version history."
                ) % rec.technology)

    # ------------------------------------------------------------------
    # Rendering (consumed by LLM prompt builders as TRUSTED text)
    # ------------------------------------------------------------------
    @api.model
    def get_reference_markdown(self):
        """Render the active rows as a markdown reference table.

        :return: ``| Technology | GA Date | Source |`` markdown table with
            ISO dates, or ``""`` when the table has no active rows (the
            caller then falls back to the legacy fenced free-text field).
        """
        rows = self.search([("active", "=", True)])
        if not rows:
            return ""

        def cell(value):
            return (value or "").replace("|", "\\|").replace("\n", " ").strip()

        lines = [
            "| Technology | GA Date | Source |",
            "| --- | --- | --- |",
        ]
        for row in rows:
            lines.append("| %s | %s | %s |" % (
                cell(row.technology),
                row.ga_date.isoformat() if row.ga_date else "",
                cell(row.source_url),
            ))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cron — quarterly review (v2.0 governance: "review quarterly")
    # ------------------------------------------------------------------
    @api.model
    def _cron_schedule_quarterly_review(self):
        """Schedule a stock To-Do review activity on the latest row.

        Skips (with a log line) when the table has no active rows.
        Otherwise schedules a ``mail.mail_activity_data_todo`` activity on
        the most recently written active row, assigned to the first Iris
        manager (superuser excluded; falls back to the admin user).
        """
        rows = self.search([("active", "=", True)])
        if not rows:
            _logger.info(
                "Iris: quarterly tech-date review skipped — the reference "
                "table has no active rows."
            )
            return False

        latest = rows.sorted(
            key=lambda r: (r.write_date or r.create_date, r.id)
        )[-1]

        assignee = self.env["res.users"]
        manager_group = self.env.ref(
            "iris.group_iris_manager", raise_if_not_found=False,
        )
        if manager_group:
            assignee = manager_group.user_ids.filtered(
                lambda u: u.active and u.id != SUPERUSER_ID
            ).sorted("id")[:1]
        if not assignee:
            admin = self.env.ref("base.user_admin", raise_if_not_found=False)
            if admin and admin.active:
                assignee = admin
        if not assignee:
            _logger.warning(
                "Iris: quarterly tech-date review skipped — no Iris manager "
                "or admin user found to assign the review activity to."
            )
            return False

        latest.activity_schedule(
            "mail.mail_activity_data_todo",
            summary=_("Quarterly review: Iris Tech Date Reference"),
            note=_(
                "Review every active Tech Date Reference row: confirm each "
                "GA date against its source, add rows for newly claimed "
                "technologies, and archive rows that are no longer correct "
                "(archiving preserves the version history)."
            ),
            user_id=assignee.id,
        )
        _logger.info(
            "Iris: quarterly tech-date review activity scheduled on row %s "
            "for user %s.", latest.id, assignee.id,
        )
        return True
