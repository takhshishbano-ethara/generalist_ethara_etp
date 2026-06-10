import base64
import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


DEFAULT_CSV_FIELDS = (
    "name",
    "user_id",
    "state",
    "qc_verdict",
    "completed_at",
    "duration_seconds",
)


class LeviathanBatchDelivery(models.Model):
    _name = "leviathan.batch.delivery"
    _description = "Leviathan Batch Delivery"
    _order = "create_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        index=True,
        default=lambda self: _("New"),
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("confirmed", "Confirmed"),
            ("delivered", "Delivered"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        tracking=True,
    )
    date_from = fields.Date(string="Date From")
    date_to = fields.Date(string="Date To")
    notes = fields.Text(string="Notes")

    job_ids = fields.Many2many(
        "leviathan.job",
        "leviathan_batch_delivery_job_rel",
        "batch_id",
        "job_id",
        string="Jobs",
    )
    job_count = fields.Integer(compute="_compute_job_count", store=False)

    column_ids = fields.Many2many(
        "ir.model.fields",
        "leviathan_batch_delivery_column_rel",
        "batch_id",
        "field_id",
        string="Columns to Export",
        domain="[('model', '=', 'leviathan.job')]",
        help="Fields of leviathan.job to include in the exported CSV. Defaults are used when empty.",
    )

    csv_file = fields.Binary(string="CSV File", readonly=True, attachment=True)
    csv_filename = fields.Char(string="CSV Filename", readonly=True)
    delivered_at = fields.Datetime(string="Delivered At", readonly=True)
    user_id = fields.Many2one(
        "res.users",
        string="Created By",
        default=lambda self: self.env.user,
        readonly=True,
    )

    @api.depends("job_ids")
    def _compute_job_count(self):
        for rec in self:
            rec.job_count = len(rec.job_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == _("New"):
                seq = self.env["ir.sequence"].next_by_code("leviathan.batch.delivery")
                vals["name"] = seq or _("New")
        return super().create(vals_list)

    def action_confirm(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Only draft batches can be confirmed."))
            if not rec.job_ids:
                raise UserError(_("Cannot confirm an empty batch."))
            rec.state = "confirmed"

    def action_set_to_draft(self):
        for rec in self:
            if rec.state == "delivered":
                raise UserError(_("Delivered batches cannot be reset to draft."))
            rec.state = "draft"

    def action_cancel(self):
        for rec in self:
            rec.state = "cancelled"

    def action_mark_delivered(self):
        for rec in self:
            if rec.state not in ("draft", "confirmed"):
                raise UserError(_("Batch must be draft or confirmed to mark as delivered."))
            rec.state = "delivered"
            rec.delivered_at = fields.Datetime.now()

    def _csv_field_names(self):
        self.ensure_one()
        if self.column_ids:
            return list(self.column_ids.mapped("name"))
        return list(DEFAULT_CSV_FIELDS)

    def _format_csv_value(self, record, field_name):
        if field_name not in record._fields:
            return ""
        value = record[field_name]
        if value is False or value is None:
            return ""
        f = record._fields[field_name]
        if f.type == "many2one":
            return value.display_name or ""
        if f.type in ("one2many", "many2many"):
            return ", ".join(value.mapped("display_name"))
        if f.type == "datetime":
            return fields.Datetime.to_string(value) or ""
        if f.type == "date":
            return fields.Date.to_string(value) or ""
        if f.type == "selection":
            selection = dict(f._description_selection(self.env))
            return selection.get(value, str(value))
        return str(value)

    def action_generate_csv(self):
        self.ensure_one()
        if not self.job_ids:
            raise UserError(_("Cannot export an empty batch."))
        column_names = self._csv_field_names()
        buf = io.StringIO()
        writer = csv.writer(buf)
        headers = ["Batch"]
        for field_name in column_names:
            f = self.env["leviathan.job"]._fields.get(field_name)
            headers.append(f.string if f else field_name)
        writer.writerow(headers)
        for job in self.job_ids:
            row = [self.name]
            for field_name in column_names:
                row.append(self._format_csv_value(job, field_name))
            writer.writerow(row)
        data = buf.getvalue().encode("utf-8")
        self.csv_file = base64.b64encode(data)
        self.csv_filename = f"{self.name.replace('/', '_')}.csv"
        return {
            "type": "ir.actions.act_url",
            "url": (
                "/web/content?model=leviathan.batch.delivery"
                f"&id={self.id}&field=csv_file&filename_field=csv_filename&download=true"
            ),
            "target": "self",
        }

    def action_view_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Jobs in %s") % self.name,
            "res_model": "leviathan.job",
            "view_mode": "list,form",
            "domain": [("id", "in", self.job_ids.ids)],
        }
