# -*- coding: utf-8 -*-
import base64
import csv
import io

from odoo import _, api, fields, models
from odoo.exceptions import UserError

DEFAULT_CSV_FIELDS = [
    "code",
    "title",
    "category_id",
    "subcategory",
    "status",
    "lead_user_id",
    "reviewer_id",
    "estimated_completion_time_hours",
    "pricing",
    "create_date",
]


class FenrirBatchDelivery(models.Model):
    _name = "fenrir.batch.delivery"
    _description = "Fenrir Batch Delivery"
    _order = "create_date desc"
    _rec_name = "name"

    name = fields.Char(
        "Batch Reference",
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _("New"),
    )
    notes = fields.Text("Notes")
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
        copy=False,
    )
    user_id = fields.Many2one(
        "res.users",
        "Owner",
        default=lambda self: self.env.user,
        required=True,
    )
    job_ids = fields.Many2many(
        "fenrir.task",
        relation="fenrir_batch_delivery_rel",
        column1="batch_id",
        column2="job_id",
        string="Tasks",
    )
    job_count = fields.Integer(compute="_compute_job_count", store=True)
    column_ids = fields.Many2many(
        "ir.model.fields",
        relation="fenrir_batch_delivery_col_rel",
        column1="batch_id",
        column2="field_id",
        string="CSV Columns",
        domain=[("model", "=", "fenrir.task")],
    )
    csv_file = fields.Binary(readonly=True, attachment=True)
    csv_filename = fields.Char(readonly=True)
    delivered_on = fields.Datetime(readonly=True, copy=False)

    @api.depends("job_ids")
    def _compute_job_count(self):
        for record in self:
            record.job_count = len(record.job_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("fenrir.batch.delivery")
                    or _("New")
                )
        return super().create(vals_list)

    def action_confirm(self):
        for batch in self:
            if batch.state != "draft":
                raise UserError(_("Only draft batches can be confirmed."))
            if not batch.job_ids:
                raise UserError(_("Add at least one task before confirming."))
            batch.state = "confirmed"
        return True

    def action_mark_delivered(self):
        for batch in self:
            if batch.state != "confirmed":
                raise UserError(_("Only confirmed batches can be delivered."))
            batch.write({"state": "delivered", "delivered_on": fields.Datetime.now()})
        return True

    def action_reset_to_draft(self):
        for batch in self:
            if batch.state == "delivered":
                raise UserError(_("Delivered batches cannot be reset."))
            batch.state = "draft"
        return True

    def action_cancel(self):
        for batch in self:
            if batch.state in ("delivered", "cancelled"):
                raise UserError(_("Delivered or cancelled batches cannot be cancelled."))
            batch.state = "cancelled"
        return True

    def _get_csv_field_names(self):
        self.ensure_one()
        if self.column_ids:
            return self.column_ids.mapped("name")
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
            return selection.get(value, value) or ""
        return str(value)

    def action_export_csv(self):
        self.ensure_one()
        if not self.job_ids:
            raise UserError(_("This batch has no tasks to export."))
        field_names = self._get_csv_field_names()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(field_names)
        for job in self.job_ids:
            writer.writerow(
                [self._format_csv_value(job, name) for name in field_names]
            )
        data = buffer.getvalue().encode("utf-8")
        filename = "%s.csv" % (self.name or "batch_delivery").replace("/", "_")
        self.write(
            {
                "csv_file": base64.b64encode(data),
                "csv_filename": filename,
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/?model=%s&id=%s&field=csv_file&filename_field=csv_filename&download=true"
            % (self._name, self.id),
            "target": "self",
        }
