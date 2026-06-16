from odoo import _, api, fields, models
from odoo.exceptions import UserError

S3_FEEDBACK_PREFIX = "client_deliverable_feedback"


class ClientDeliverableFeedback(models.Model):
    """A single dated feedback/comment entry on a deliverable.

    Multiple entries per deliverable; each carries its own date and comment so
    clients can log feedback over time. An optional file can be uploaded — it is
    pushed straight to S3 and only the resulting link is kept; the bytes are
    never stored in our database.
    """

    _name = "client.deliverable.feedback"
    _description = "Client Deliverable Feedback Entry"
    _order = "feedback_date desc, id desc"

    deliverable_id = fields.Many2one(
        "client.deliverable",
        string="Deliverable",
        required=True,
        ondelete="cascade",
        index=True,
    )
    feedback_date = fields.Date(
        string="Feedback Date",
        required=True,
        default=fields.Date.context_today,
    )
    comment = fields.Text(string="Comment", required=True)
    author_id = fields.Many2one(
        "res.users",
        string="Author",
        default=lambda self: self.env.user,
    )
    author_name = fields.Char(string="Author Name")

    # Transient upload field: receives the picked file, gets pushed to S3 on
    # save, then cleared so the bytes never persist in the DB.
    upload_file = fields.Binary(string="Upload File", attachment=False)
    upload_filename = fields.Char(string="Upload Filename")

    # Only the S3 link + name are kept.
    file_url = fields.Char(string="File URL", readonly=True)
    file_name = fields.Char(string="File Name", readonly=True)

    def _upload_to_s3(self, b64data, filename):
        connector = self.env["s3.connector"].sudo().search([], limit=1)
        if not connector:
            raise UserError(
                _("No S3 connector is configured; cannot upload the file.")
            )
        wizard = self.env["s3.upload.wizard"].sudo().create(
            {
                "s3_connector_id": connector.id,
                "upload_file": b64data,
                "prefix": S3_FEEDBACK_PREFIX,
                "file_name": filename or "upload",
            }
        )
        return wizard.upload_images_in_s3_get_url()

    def _handle_upload(self, vals):
        """Push any uploaded bytes to S3 and keep only the link."""
        if vals.get("upload_file"):
            filename = vals.get("upload_filename") or "upload"
            vals["file_url"] = self._upload_to_s3(vals["upload_file"], filename)
            vals["file_name"] = filename
        # Never persist the raw bytes.
        vals["upload_file"] = False
        vals["upload_filename"] = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._handle_upload(vals)
            if not vals.get("author_name"):
                if vals.get("author_id"):
                    user = self.env["res.users"].browse(vals["author_id"])
                else:
                    user = self.env.user
                vals["author_name"] = user.name
        return super().create(vals_list)

    def write(self, vals):
        if "upload_file" in vals:
            self._handle_upload(vals)
        return super().write(vals)
