import base64
import csv
import io

from odoo import _, api, fields, models

# Lifecycle stages. Iteration tracking keys off "rework".
STATUS_SELECTION = [
    ("draft", "Draft"),
    ("delivered", "Delivered"),
    ("commented", "Commented"),
    ("rework", "Rework"),
    ("approved", "Approved"),
]

# Columns produced by `action_export_csv` (feedback log export).
CSV_FEEDBACK_HEADERS = ["Date", "Author", "Comment", "File"]


class ClientDeliverable(models.Model):
    """One deliverable record per project.

    Holds the shareable links (git repo, excel sheet, Google Drive, plus any
    number of S3 file links), the lifecycle ``status`` with iteration tracking,
    the per-iteration status log, and the client feedback log. The links and
    status are editable from the frontend via the REST API in
    ``controllers/deliverables.py``.
    """

    _name = "client.deliverable"
    _description = "Client Deliverable (links + feedback hub)"
    _inherit = ["mail.thread"]
    _order = "create_date desc"

    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _("New"),
    )
    project_id = fields.Many2one(
        "project.project",
        string="Project",
        ondelete="cascade",
        index=True,
        tracking=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Owner",
        default=lambda self: self.env.user,
        required=True,
    )

    status = fields.Selection(
        STATUS_SELECTION,
        string="Status",
        default="draft",
        required=True,
        tracking=True,
        copy=False,
    )
    iteration = fields.Integer(
        string="Iteration",
        default=1,
        readonly=True,
        copy=False,
        help="Current iteration number. Increments by one each time the "
        "deliverable enters the 'Rework' status.",
    )

    git_repo_url = fields.Char(string="Git Repository URL", tracking=True)
    excel_sheet_url = fields.Char(string="Excel Sheet URL", tracking=True)
    gdrive_url = fields.Char(string="Google Drive URL", tracking=True)
    s3_link_ids = fields.One2many(
        "client.deliverable.s3.link",
        "deliverable_id",
        string="S3 File Links",
        help="Links to files in S3. Only the links are stored/shown — never "
        "the files themselves.",
    )

    active = fields.Boolean(default=True)
    notes = fields.Text(string="Notes")

    feedback_ids = fields.One2many(
        "client.deliverable.feedback",
        "deliverable_id",
        string="Feedback",
    )
    feedback_count = fields.Integer(
        string="Feedback Count",
        compute="_compute_feedback_count",
        store=True,
    )
    iteration_log_ids = fields.One2many(
        "client.deliverable.iteration.log",
        "deliverable_id",
        string="Iteration Logs",
    )

    csv_file = fields.Binary(string="Feedback CSV", readonly=True, attachment=True)
    csv_filename = fields.Char(string="CSV Filename", readonly=True)

    @api.depends("feedback_ids")
    def _compute_feedback_count(self):
        for record in self:
            record.feedback_count = len(record.feedback_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "client.deliverable"
                ) or _("New")
        return super().create(vals_list)

    def write(self, vals):
        """Log every status transition and bump the iteration on 'Rework'."""
        new_status = vals.get("status")
        logs = []
        bump = self.browse()
        if new_status:
            for record in self:
                if new_status != record.status:
                    logs.append(
                        {
                            "deliverable_id": record.id,
                            "iteration": record.iteration,
                            "from_status": record.status or False,
                            "to_status": new_status,
                            "user_id": self.env.user.id,
                            "note": vals.get("status_note") or False,
                        }
                    )
                    if new_status == "rework":
                        bump |= record
        # `status_note` is a transient hint for the log only, not a stored field.
        vals.pop("status_note", None)
        result = super().write(vals)
        if logs:
            self.env["client.deliverable.iteration.log"].create(logs)
        for record in bump:
            super(ClientDeliverable, record).write(
                {"iteration": record.iteration + 1}
            )
        return result

    def action_export_csv(self):
        """Export this deliverable's feedback log to a downloadable CSV."""
        self.ensure_one()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_FEEDBACK_HEADERS)
        feedbacks = self.feedback_ids.sorted(
            key=lambda f: (f.feedback_date or fields.Date.today(), f.id)
        )
        for feedback in feedbacks:
            writer.writerow(
                [
                    fields.Date.to_string(feedback.feedback_date) or "",
                    feedback.author_name or (feedback.author_id.name or ""),
                    (feedback.comment or "").replace("\n", " ").strip(),
                    feedback.file_url or "",
                ]
            )
        data = buffer.getvalue().encode("utf-8")
        self.write(
            {
                "csv_file": base64.b64encode(data),
                "csv_filename": "%s-feedback.csv" % (self.name or "deliverable"),
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/?model=%s&id=%s&field=csv_file&filename_field=csv_filename&download=true"
            % (self._name, self.id),
            "target": "self",
        }
