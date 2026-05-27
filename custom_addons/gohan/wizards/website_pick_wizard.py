from odoo import api, fields, models
from odoo.exceptions import UserError


class GohanWebsitePickWizard(models.TransientModel):
    _name = "gohan.website.pick.wizard"
    _description = "Pick Website by Category"

    category_id = fields.Many2one(
        "gohan.category",
        string="Category",
        required=True,
        help="Pick the category — a random unassigned URL in that category will be assigned to you as a new task.",
    )
    available_count = fields.Integer(
        string="Available", compute="_compute_available_count"
    )

    @api.depends("category_id")
    def _compute_available_count(self):
        Line = self.env["gohan.website.sheet.line"].sudo()
        for wiz in self:
            if not wiz.category_id:
                wiz.available_count = 0
                continue
            wiz.available_count = Line.search_count([
                ("category_id", "=", wiz.category_id.id),
                ("status", "=", "unassigned"),
                ("assigned_user_id", "=", False),
                ("sheet_id.state", "=", "active"),
            ])

    def action_pick(self):
        """Claim a pool URL for the tasker AND spawn a gohan.job for it.

        Steps:
          1. Bandwidth check (same gohan.max_jobs_per_user cap that
             action_start_task on gohan.job uses).
          2. Race-safe FCFS claim of a sheet line in the chosen category.
          3. Create a gohan.job in 'draft' state pre-filled with the URL,
             category and current user as tasker.
          4. Link the line back to the new job so admin can navigate either
             way (Upload Data view -> see job; All Tasks view -> see pool
             origin via the line's URL).
          5. Open the gohan.job form so the tasker can hit Run Pipeline.
        """
        self.ensure_one()
        if not self.category_id:
            raise UserError("Please pick a category.")

        user = self.env.user
        Job = self.env["gohan.job"]
        ICP = self.env["ir.config_parameter"].sudo()
        max_active = int(ICP.get_param("gohan.max_jobs_per_user", "5"))
        if max_active > 0:
            active_count = Job.sudo().search_count([
                ("user_id", "=", user.id),
                ("state", "in", Job._ACTIVE_STATES),
            ])
            if active_count >= max_active:
                raise UserError(
                    f"You already have {active_count} active task(s). "
                    f"Submit or complete existing tasks first "
                    f"(max: {max_active})."
                )

        line = self.env["gohan.website.sheet.line"].claim_for_user(
            self.category_id.id, user=user
        )
        if not line:
            raise UserError(
                "No unassigned website is available in this category."
            )

        # Create the gohan.job that the tasker will actually work on.
        # state='draft' + user_id set mirrors what action_start_task does
        # after a tasker grabs a pre-imported not_assigned job.
        job = Job.sudo().create({
            "url": line.url,
            "category_id": line.category_id.id,
            "user_id": user.id,
            "state": "draft",
        })

        line.sudo().write({"job_id": job.id})

        return {
            "type": "ir.actions.act_window",
            "name": "Your Task",
            "res_model": "gohan.job",
            "res_id": job.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }
