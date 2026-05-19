from odoo import api, fields, models
from odoo.exceptions import UserError


class VegetaRerunWizard(models.TransientModel):
    _name = "vegeta.rerun.wizard"
    _description = "Rerun Pipeline Wizard"

    job_id = fields.Many2one("vegeta.job", string="Job", required=True)

    # Display fields (read from job)
    job_score = fields.Float(related="job_id.score", readonly=True)
    job_score_display = fields.Char(
        string="Score", compute="_compute_extraction_info",
    )
    job_grade = fields.Char(related="job_id.grade", readonly=True)
    job_qc_verdict = fields.Char(
        string="QC Verdict", compute="_compute_extraction_info",
    )
    has_screenshots = fields.Boolean(compute="_compute_extraction_info")
    has_assets = fields.Boolean(compute="_compute_extraction_info")
    asset_summary = fields.Char(compute="_compute_extraction_info")

    @api.depends("job_id")
    def _compute_extraction_info(self):
        for rec in self:
            job = rec.job_id
            ss = job.screenshot_keys or []
            assets = job.asset_keys or []
            rec.has_screenshots = bool(ss)
            rec.has_assets = bool(assets)
            parts = []
            if ss:
                parts.append(f"{len(ss)} screenshot(s)")
            if assets:
                parts.append(f"{len(assets)} asset(s)")
            rec.asset_summary = ", ".join(parts) if parts else "No extraction data"
            if job.score:
                rec.job_score_display = str(int(job.score))
            else:
                rec.job_score_display = "Not Scored"
            rec.job_qc_verdict = (
                "Shippable" if job.qc_verdict == "shippable"
                else "Not Shippable" if job.qc_verdict == "not_shippable"
                else "Not evaluated"
            )

    def action_rerun_generate_only(self):
        """Regenerate PRD + QC using existing extraction data."""
        self.ensure_one()
        self.job_id.action_rerun_without_extract()

    def action_rerun_full(self):
        """Re-extract website from scratch, then regenerate PRD + QC."""
        self.ensure_one()
        self.job_id.action_rerun_with_extract()
