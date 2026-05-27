import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Advisory-lock namespace for claiming a sheet line. A single namespace shared
# across all sheets is fine — the second key (sheet_id) scopes the lock per
# sheet so two taskers picking from different sheets do not block each other,
# while two taskers picking from the same sheet at the same instant serialise
# and only one of them grabs each unassigned line.
_SHEET_CLAIM_LOCK_NS = 0x60134B01


class GohanWebsiteSheet(models.Model):
    _name = "gohan.website.sheet"
    _description = "Upload Data"
    _order = "create_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        string="Reference",
        required=True,
        default=lambda self: self.env["ir.sequence"].next_by_code(
            "gohan.website.sheet"
        ) or "New Sheet",
        copy=False,
        tracking=True,
    )
    csv_filename = fields.Char(string="Source File")
    csv_file = fields.Binary(string="Source CSV", attachment=True)
    uploaded_by = fields.Many2one(
        "res.users",
        string="Uploaded By",
        default=lambda self: self.env.user,
        readonly=True,
    )
    state = fields.Selection(
        [
            ("active", "Active"),
            ("closed", "Closed"),
        ],
        default="active",
        tracking=True,
        help=(
            "Active sheets are open for taskers to pick from. Closed sheets "
            "stop appearing in the pick wizard but remain visible to admins."
        ),
    )

    line_ids = fields.One2many(
        "gohan.website.sheet.line",
        "sheet_id",
        string="Websites",
    )

    total_count = fields.Integer(
        string="Total", compute="_compute_counts", store=False
    )
    unassigned_count = fields.Integer(
        string="Unassigned", compute="_compute_counts", store=False
    )
    in_progress_count = fields.Integer(
        string="In Progress", compute="_compute_counts", store=False
    )
    done_count = fields.Integer(
        string="Done", compute="_compute_counts", store=False
    )

    @api.depends("line_ids.status")
    def _compute_counts(self):
        for sheet in self:
            lines = sheet.line_ids
            sheet.total_count = len(lines)
            sheet.unassigned_count = len(
                lines.filtered(lambda l: l.status == "unassigned")
            )
            sheet.in_progress_count = len(
                lines.filtered(lambda l: l.status == "in_progress")
            )
            sheet.done_count = len(
                lines.filtered(lambda l: l.status == "done")
            )

    def action_close(self):
        for sheet in self:
            sheet.state = "closed"

    def action_reopen(self):
        for sheet in self:
            sheet.state = "active"


class GohanWebsiteSheetLine(models.Model):
    _name = "gohan.website.sheet.line"
    _description = "Upload Data Line"
    _order = "sheet_id, id"
    _inherit = ["mail.thread"]
    _rec_name = "url"

    sheet_id = fields.Many2one(
        "gohan.website.sheet",
        string="Sheet",
        required=True,
        ondelete="cascade",
        index=True,
    )
    category_id = fields.Many2one(
        "gohan.category",
        string="Category",
        required=True,
        index=True,
        tracking=True,
    )
    url = fields.Char(string="Website URL", required=True, tracking=True)
    assigned_user_id = fields.Many2one(
        "res.users",
        string="Assigned To",
        readonly=True,
        index=True,
        tracking=True,
        help="Tasker who currently holds this URL.",
    )
    assigned_date = fields.Datetime(string="Assigned On", readonly=True)
    status = fields.Selection(
        [
            ("unassigned", "Unassigned"),
            ("in_progress", "In Progress"),
            ("on_hold", "On Hold"),
            ("done", "Done"),
            ("cancelled", "Cancelled"),
        ],
        default="unassigned",
        required=True,
        tracking=True,
    )
    notes = fields.Text(string="Notes")
    sheet_state = fields.Selection(
        related="sheet_id.state", store=False, string="Sheet State"
    )
    job_id = fields.Many2one(
        "gohan.job",
        string="Pipeline Job",
        ondelete="set null",
        help=(
            "The gohan.job created when this URL was claimed. Lets admin "
            "jump straight to the pipeline work for this pool entry."
        ),
    )
    job_state = fields.Selection(
        related="job_id.state", store=False, string="Job State"
    )
    is_admin = fields.Boolean(compute="_compute_is_admin")

    @api.depends_context("uid")
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group("gohan.group_gohan_admin")
        for rec in self:
            rec.is_admin = is_admin

    _sql_constraints = [
        (
            "url_sheet_unique",
            "UNIQUE(sheet_id, url)",
            "The same URL cannot appear twice in the same sheet.",
        ),
    ]

    @api.model
    def claim_for_user(self, category_id, user=None):
        """Atomically claim the next unassigned line in `category_id` for `user`.

        First-come-first-served — admins do not pre-allocate. Any tasker who
        picks a matching category gets the next free URL.

        Returns the claimed line (or an empty recordset if none available).
        Uses a per-sheet advisory lock so two concurrent calls do not hand
        out the same line.
        """
        user = user or self.env.user
        if not category_id:
            raise UserError("Please pick a category.")

        candidates = self.sudo().search(
            [
                ("category_id", "=", category_id),
                ("status", "=", "unassigned"),
                ("assigned_user_id", "=", False),
                ("sheet_id.state", "=", "active"),
            ],
            order="sheet_id, id",
            limit=1,
        )

        if not candidates:
            return self.browse()

        # Take a transaction-scoped advisory lock keyed on the sheet so a
        # second concurrent claim against the same sheet serialises here.
        line = candidates
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_SHEET_CLAIM_LOCK_NS, line.sheet_id.id),
        )

        # Re-read with FOR UPDATE inside the lock to verify the line is
        # still unassigned (a parallel transaction that beat us to the lock
        # may have already claimed it).
        self.env.cr.execute(
            """
            SELECT id FROM gohan_website_sheet_line
             WHERE id = %s
               AND status = 'unassigned'
               AND assigned_user_id IS NULL
             FOR UPDATE
            """,
            (line.id,),
        )
        row = self.env.cr.fetchone()
        if not row:
            # Lost the race — recurse once to retry with the next candidate.
            return self.claim_for_user(category_id, user=user)

        line.sudo().write({
            "assigned_user_id": user.id,
            "assigned_date": fields.Datetime.now(),
            "status": "in_progress",
        })
        return line

    def action_open_url(self):
        """Open the URL in a new browser tab."""
        self.ensure_one()
        if not self.url:
            raise UserError("This line has no URL.")
        return {
            "type": "ir.actions.act_url",
            "url": self.url,
            "target": "new",
        }

    def action_release(self):
        """Admin action: unassign and return the line to the pool."""
        for line in self:
            line.write({
                "assigned_user_id": False,
                "assigned_date": False,
                "status": "unassigned",
            })

    def action_mark_done(self):
        for line in self:
            line.status = "done"

    def action_mark_in_progress(self):
        for line in self:
            line.status = "in_progress"
