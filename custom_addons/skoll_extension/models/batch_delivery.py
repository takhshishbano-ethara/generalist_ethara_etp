# -*- coding: utf-8 -*-
import base64
import csv
import io

from odoo import _, api, fields, models
from odoo.exceptions import UserError


DEFAULT_CSV_FIELDS = [
    "task_id",
    "persona_id",
    "mode",
    "qc_status",
]


class SkollBatchDelivery(models.Model):
    _name = "skoll.batch.delivery"
    _description = "Skoll Batch Delivery"
    _order = "create_date desc"
    _rec_name = "name"

    name = fields.Char(
        string="Batch Reference",
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _("New"),
    )
    notes = fields.Text(string="Notes")
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("confirmed", "Confirmed"),
            ("delivered", "Delivered"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        default="draft",
        required=True,
        tracking=True,
        copy=False,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Owner",
        default=lambda self: self.env.user,
        required=True,
    )
    job_ids = fields.Many2many(
        "skoll.skoll",
        relation="skoll_batch_delivery_job_rel",
        column1="batch_id",
        column2="job_id",
        string="Skoll Tasks",
    )
    job_count = fields.Integer(
        string="Task Count", compute="_compute_job_count", store=True
    )
    column_ids = fields.Many2many(
        "ir.model.fields",
        relation="skoll_batch_delivery_column_rel",
        column1="batch_id",
        column2="field_id",
        string="CSV Columns",
        domain=[("model", "=", "skoll.skoll")],
        help="Fields to include in the CSV export. Leave empty for defaults.",
    )
    csv_file = fields.Binary(string="CSV File", readonly=True, attachment=True)
    csv_filename = fields.Char(string="CSV Filename", readonly=True)
    delivered_on = fields.Datetime(string="Delivered On", readonly=True, copy=False)

    @api.depends("job_ids")
    def _compute_job_count(self):
        for record in self:
            record.job_count = len(record.job_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "skoll.batch.delivery"
                ) or _("New")
        return super().create(vals_list)

    def action_confirm(self):
        for record in self:
            if not record.job_ids:
                raise UserError(_("Cannot confirm an empty batch."))
            if record.state != "draft":
                raise UserError(_("Only draft batches can be confirmed."))
            record.state = "confirmed"

    def action_mark_delivered(self):
        for record in self:
            if record.state != "confirmed":
                raise UserError(_("Only confirmed batches can be marked delivered."))
            record.state = "delivered"
            record.delivered_on = fields.Datetime.now()

    def action_reset_to_draft(self):
        for record in self:
            if record.state == "delivered":
                raise UserError(_("Delivered batches cannot be reset."))
            record.state = "draft"

    def action_cancel(self):
        for record in self:
            if record.state == "delivered":
                raise UserError(_("Delivered batches cannot be cancelled."))
            record.state = "cancelled"

    def _get_csv_field_names(self):
        self.ensure_one()
        if self.column_ids:
            return [field.name for field in self.column_ids]
        return list(DEFAULT_CSV_FIELDS)

    def _format_csv_value(self, record, field_name):
        if field_name not in record._fields:
            return ""
        field = record._fields[field_name]
        value = record[field_name]
        if value is False or value is None:
            return ""
        if field.type == "many2one":
            return value.display_name or ""
        if field.type in ("one2many", "many2many"):
            return ", ".join(value.mapped("display_name"))
        if field.type == "datetime":
            return fields.Datetime.to_string(value) or ""
        if field.type == "date":
            return fields.Date.to_string(value) or ""
        if field.type == "selection":
            selection = dict(field._description_selection(record.env))
            return selection.get(value, value)
        return str(value)

    def action_export_csv(self):
        self.ensure_one()
        if not self.job_ids:
            raise UserError(_("Nothing to export — batch has no tasks."))
        field_names = self._get_csv_field_names()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(field_names)
        for job in self.job_ids:
            writer.writerow(
                [self._format_csv_value(job, name) for name in field_names]
            )
        data = buffer.getvalue().encode("utf-8")
        self.write(
            {
                "csv_file": base64.b64encode(data),
                "csv_filename": "%s.csv" % (self.name or "batch"),
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/?model=%s&id=%s&field=csv_file&filename_field=csv_filename&download=true"
            % (self._name, self.id),
            "target": "self",
        }
