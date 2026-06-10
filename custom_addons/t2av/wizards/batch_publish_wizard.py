from odoo import _, api, fields, models
from odoo.exceptions import UserError


_ELIGIBLE_STATE = "draft"


class T2avBatchPublishWizard(models.TransientModel):
    _name = "t2av.batch_publish.wizard"
    _description = "T2AV - Batch Publish to RabbitMQ (FIFO or Selected)"

    mode = fields.Selection(
        [("manual", "Use Selected Records"), ("fifo", "Auto-pick FIFO")],
        string="Selection Mode",
        required=True,
        default="fifo",
    )

    requested_count = fields.Integer(
        string="Number to Publish",
        default=10,
        help="FIFO mode only. How many eligible records to auto-select in "
        "oldest-first order.",
    )
    category_filter = fields.Selection(
        selection=lambda self: self.env["t2av.generation"]._fields["category"].selection,
        string="Category (optional)",
        help="FIFO mode only. Pick a category to restrict the FIFO selection "
        "to that category. Leave empty to publish across all categories.",
    )
    include_failed = fields.Boolean(
        string="Include Previously Failed",
        default=True,
        help="When checked, records with pipeline_status='failed' are also "
        "eligible (republish). Uncheck to only consider fresh drafts.",
    )
    date_from = fields.Datetime(
        string="Created After (optional)",
        help="FIFO mode only. Only consider records whose create_date is on "
        "or after this datetime.",
    )
    date_to = fields.Datetime(
        string="Created Before (optional)",
        help="FIFO mode only. Only consider records whose create_date is on "
        "or before this datetime.",
    )

    manual_ids = fields.Many2many(
        "t2av.generation",
        relation="t2av_batch_pub_wiz_manual_rel",
        column1="wizard_id",
        column2="generation_id",
        string="Records You Selected",
    )

    eligible_count = fields.Integer(
        string="Total Eligible Now",
        compute="_compute_eligible",
    )
    will_publish_count = fields.Integer(
        string="Will Publish",
        compute="_compute_preview",
    )
    skipped_count = fields.Integer(
        string="Will Skip (ineligible)",
        compute="_compute_preview",
    )
    info_message = fields.Char(
        string="Info",
        compute="_compute_eligible",
    )
    preview_ids = fields.Many2many(
        "t2av.generation",
        relation="t2av_batch_pub_wiz_preview_rel",
        column1="wizard_id",
        column2="generation_id",
        string="Records this batch will publish",
        compute="_compute_preview",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context or {}
        if ctx.get("active_model") == "t2av.generation":
            active_ids = ctx.get("active_ids") or []
            if active_ids:
                res["mode"] = "manual"
                res["manual_ids"] = [(6, 0, list(active_ids))]
        return res

    def _allowed_pipeline_statuses(self):
        self.ensure_one()
        if self.include_failed:
            return ("not_published", "failed")
        return ("not_published",)

    def _allowed_states(self):
        self.ensure_one()
        if self.include_failed:
            return (_ELIGIBLE_STATE, "failed")
        return (_ELIGIBLE_STATE,)

    def _eligible_domain(self):
        self.ensure_one()
        domain = [
            ("state", "in", list(self._allowed_states())),
            ("pipeline_status", "in", list(self._allowed_pipeline_statuses())),
        ]
        cat = (self.category_filter or "").strip()
        if cat:
            domain.append(("category", "=", cat))
        if self.date_from:
            domain.append(("create_date", ">=", self.date_from))
        if self.date_to:
            domain.append(("create_date", "<=", self.date_to))
        return domain

    def _filter_eligible(self, records):
        allowed_states = self._allowed_states()
        allowed_statuses = self._allowed_pipeline_statuses()
        return records.filtered(
            lambda r: r.state in allowed_states
            and (r.pipeline_status or "not_published") in allowed_statuses
        )

    @api.depends("mode", "category_filter", "include_failed", "date_from", "date_to", "manual_ids", "manual_ids.state", "manual_ids.pipeline_status")
    def _compute_eligible(self):
        Gen = self.env["t2av.generation"]
        for rec in self:
            if rec.mode == "manual":
                eligible = rec._filter_eligible(rec.manual_ids)
                rec.eligible_count = len(eligible)
                total = len(rec.manual_ids)
                if total == 0:
                    rec.info_message = _("No records selected.")
                elif len(eligible) == total:
                    rec.info_message = _(
                        "%(n)d selected record(s) are all eligible."
                    ) % {"n": total}
                else:
                    rec.info_message = _(
                        "%(eligible)d of %(total)d eligible. %(skipped)d "
                        "already in progress or done."
                    ) % {
                        "eligible": len(eligible),
                        "total": total,
                        "skipped": total - len(eligible),
                    }
            else:
                count = Gen.search_count(rec._eligible_domain())
                rec.eligible_count = count
                if count == 0:
                    rec.info_message = _(
                        "No eligible records. Anything already published or "
                        "done is excluded."
                    )
                else:
                    rec.info_message = _(
                        "%(n)d eligible record(s)."
                    ) % {"n": count}

    @api.depends("mode", "requested_count", "category_filter", "include_failed", "date_from", "date_to", "manual_ids", "manual_ids.state", "manual_ids.pipeline_status")
    def _compute_preview(self):
        Gen = self.env["t2av.generation"]
        for rec in self:
            if rec.mode == "manual":
                eligible = rec._filter_eligible(rec.manual_ids)
                rec.preview_ids = [(6, 0, eligible.ids)]
                rec.will_publish_count = len(eligible)
                rec.skipped_count = len(rec.manual_ids) - len(eligible)
            else:
                requested = max(0, int(rec.requested_count or 0))
                rec.skipped_count = 0
                if requested == 0:
                    rec.preview_ids = [(5,)]
                    rec.will_publish_count = 0
                    continue
                picked = Gen.search(
                    rec._eligible_domain(),
                    order="create_date asc, id asc",
                    limit=requested,
                )
                rec.preview_ids = [(6, 0, picked.ids)]
                rec.will_publish_count = len(picked)

    @api.constrains("mode", "requested_count")
    def _check_requested_count(self):
        for rec in self:
            if rec.mode == "fifo" and (rec.requested_count is None or rec.requested_count <= 0):
                raise UserError(_("Number to Publish must be at least 1 in FIFO mode."))

    def action_publish(self):
        self.ensure_one()
        Gen = self.env["t2av.generation"]
        if self.mode == "manual":
            if not self.manual_ids:
                raise UserError(_("No records selected."))
            picked = self._filter_eligible(self.manual_ids)
            if not picked:
                raise UserError(_(
                    "None of your %d selected record(s) are publishable right "
                    "now. They may already be queued, processing, or done."
                ) % len(self.manual_ids))
            failed_picks = picked.filtered(lambda r: r.state == "failed")
            if failed_picks:
                failed_picks.write({"state": _ELIGIBLE_STATE})
        else:
            if self.requested_count <= 0:
                raise UserError(_("Number to Publish must be at least 1."))
            eligible_total = Gen.search_count(self._eligible_domain())
            if eligible_total == 0:
                raise UserError(_(
                    "No eligible records to publish right now. Another user "
                    "may have just queued them, or the filter excluded "
                    "everything."
                ))
            clamped = min(int(self.requested_count), eligible_total)
            if clamped < self.requested_count:
                self.requested_count = clamped
            picked = Gen.search(
                self._eligible_domain(),
                order="create_date asc, id asc",
                limit=clamped,
            )
            if not picked:
                raise UserError(_(
                    "Race condition: another user just took the records you "
                    "were about to publish. Try again."
                ))
            failed_picks = picked.filtered(lambda r: r.state == "failed")
            if failed_picks:
                failed_picks.write({"state": _ELIGIBLE_STATE})
        return picked.action_batch_publish_pipeline()
