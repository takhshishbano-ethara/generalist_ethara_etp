"""Server-side parse state for the two-step REST import flow.

The REST API splits ``POST /upload`` (parse + preview) from ``POST /commit``
(persist the validated rows).  Between the two calls the caller holds a
``session_token`` that points at one of these rows.

We use a real (non-transient) model so the session survives the request
that created it.  Stale sessions are reaped by ``_gc_old_sessions``
through Odoo's autovacuum cron - no extra cron registration required.
"""

import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SESSION_TTL_HOURS = 24


class EmployeeImportSession(models.Model):
    _name = "employee.import.session"
    _description = "Employee Role Import Session (REST API)"
    _order = "create_date desc"
    _rec_name = "session_token"

    session_token = fields.Char(
        string="Session Token",
        required=True,
        index=True,
        copy=False,
        default=lambda self: self._default_token(),
        help="Opaque identifier returned by the upload endpoint; "
        "the commit endpoint dereferences the upload via this token.",
    )
    state = fields.Selection(
        [
            ("uploaded", "Uploaded"),
            ("previewed", "Previewed"),
            ("imported", "Imported"),
            ("failed", "Failed"),
            ("discarded", "Discarded"),
        ],
        default="uploaded",
        required=True,
        index=True,
    )
    csv_filename = fields.Char(string="Original Filename")
    raw_csv = fields.Binary(string="Raw CSV", attachment=False)

    default_role = fields.Char(
        string="Default Role Key",
        help="ROLE_GROUP_MAP key applied to rows that omit a role.",
    )
    create_user = fields.Boolean(string="Create Login Users", default=True)
    default_password = fields.Char(string="Default Password", default="Ethara@123")

    preview_payload = fields.Text(
        string="Preview JSON",
        help="Cached JSON payload returned by the preview endpoint - "
        "kept around so the caller can re-fetch the preview cheaply.",
    )
    summary_payload = fields.Text(
        string="Summary JSON",
        help="Cached JSON summary populated after the commit endpoint runs.",
    )

    total_rows = fields.Integer(string="Rows Parsed")
    valid_rows = fields.Integer(string="Valid Rows")
    invalid_rows = fields.Integer(string="Invalid Rows")
    duplicate_rows = fields.Integer(string="Duplicate Rows")

    imported_count = fields.Integer(string="Imported")
    updated_count = fields.Integer(string="Updated")
    failed_count = fields.Integer(string="Failed")

    log_text = fields.Text(string="Import Log")

    user_id = fields.Many2one(
        "res.users",
        string="Owner",
        default=lambda self: self.env.user,
        required=True,
        ondelete="cascade",
        index=True,
    )

    _sql_constraints = [
        (
            "session_token_unique",
            "unique(session_token)",
            "Session token must be unique.",
        ),
    ]

    @staticmethod
    def _default_token():
        return secrets.token_urlsafe(24)

    def assert_owner(self, user):
        """Raise UserError unless ``user`` owns this session.

        HR managers can also see other people's sessions.
        """
        self.ensure_one()
        if user.id == self.user_id.id:
            return
        hr_manager = self.env.ref("hr.group_hr_manager", raise_if_not_found=False)
        if hr_manager and hr_manager in user.group_ids:
            return
        raise UserError(_("You are not allowed to access this import session."))

    @api.autovacuum
    def _gc_old_sessions(self):
        """Reap sessions older than ``SESSION_TTL_HOURS`` hours.

        Runs via Odoo's autovacuum cron (`base.autovacuum_job`).
        """
        cutoff = fields.Datetime.now() - timedelta(hours=SESSION_TTL_HOURS)
        stale = self.sudo().search([("create_date", "<", cutoff)])
        if stale:
            _logger.info(
                "employee.import.session: GC removing %s stale session(s) (older than %sh)",
                len(stale),
                SESSION_TTL_HOURS,
            )
            stale.unlink()
        return True
