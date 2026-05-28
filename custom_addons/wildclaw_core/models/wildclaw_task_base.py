from odoo import api, fields, models


class WildclawTaskBase(models.AbstractModel):
    """Abstract base model that wrapper-addon task models inherit.

    Wrappers (kensei_wildclaw.task, skoll_wildclaw.task, talos_wildclaw.task)
    define `_inherit = "wildclaw.task_base"` plus `_name = "..._wildclaw.task"`
    plus their own concrete `_table`. They add only their differentiated fields
    (e.g. file_attachment_ids for kensei_wildclaw, golden_trajectory for
    skoll_wildclaw). All shared fields live here.
    """

    _name = "wildclaw.task_base"
    _description = "WildClaw Abstract Task Base (inherited by wrapper addons)"
    _order = "create_date desc, id desc"

    task_id = fields.Char(string="Task ID", required=True, index=True)
    name = fields.Char(string="Display Name", compute="_compute_name", store=True)

    persona_id = fields.Many2one("wildclaw.persona", string="Persona", required=True, ondelete="restrict")
    domain_l1_id = fields.Many2one("wildclaw.domain", string="L1 Domain")
    domain_l2_id = fields.Many2one("wildclaw.domain", string="L2 Domain")

    employee_ids = fields.Many2many("hr.employee", string="Assigned Employees")

    task_type = fields.Selection(
        [
            ("home_and_organization", "Home & Organization"),
            ("customer_service", "Customer Service"),
            ("research_and_analysis", "Research & Analysis"),
            ("creative_writing", "Creative Writing"),
            ("technical_support", "Technical Support"),
            ("education_and_learning", "Education & Learning"),
            ("health_and_wellness", "Health & Wellness"),
            ("finance_and_budgeting", "Finance & Budgeting"),
            ("sustainable_planning", "Sustainable Planning"),
            ("historical_archiving", "Historical Archiving"),
        ],
        string="Task Type",
    )
    difficulty = fields.Selection(
        [
            ("single_app", "Single App"),
            ("multi_app_light", "Multi-App (Light)"),
            ("multi_app_complex", "Multi-App (Complex)"),
        ],
        string="Difficulty",
    )

    system_prompt = fields.Text(string="System Prompt")
    seed_prompt = fields.Text(string="Seed Prompt")
    initial_prompt = fields.Text(string="Initial Prompt")
    task_description = fields.Text(string="Task Description (auto-generated)")

    qc_status = fields.Selection(
        [("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
        string="QC Status",
        default="pending",
    )

    rubrics = fields.Text(string="Rubrics (JSON)")
    test_weights = fields.Text(string="Test Weights (JSON)")

    media_attachment_ids = fields.Many2many(
        "wildclaw.media.attachment",
        string="Multimedia Attachments",
        help="Image/video/PDF attachments uploaded for this task (S3-backed).",
    )

    @api.depends("task_id")
    def _compute_name(self):
        for rec in self:
            rec.name = rec.task_id or "(unnamed task)"
