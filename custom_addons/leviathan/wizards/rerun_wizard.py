from odoo import fields, models
from odoo.exceptions import UserError


class LeviathanRerunWizard(models.TransientModel):
    _name = "leviathan.rerun.wizard"
    _description = "Rerun Pipeline Wizard"

    job_id = fields.Many2one("leviathan.job", string="Job", required=True)

    def action_rerun_generate_only(self):
        """Regenerate PRD + QC using existing extraction data."""
        self.ensure_one()
        self.job_id.action_rerun_without_extract()

    def action_rerun_full(self):
        """Re-extract website from scratch, then regenerate PRD + QC."""
        self.ensure_one()
        self.job_id.action_rerun_with_extract()
