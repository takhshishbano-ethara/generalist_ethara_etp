from odoo import _, api, fields, models


PROJECT_TYPE_SELECTION = [
    ("vs-1780675368-i2i-pixel-aligned-filter-image-edit-region", "vs-1780675368-i2i-pixel-aligned-filter-image-edit-region"),
    ("vs-1780658597-i2i-pixel-aligned-filter-motion-verify", "vs-1780658597-i2i-pixel-aligned-filter-motion-verify"),
    ("i2i-pixel-aligned-filter-flux2-nomask", "i2i-pixel-aligned-filter-flux2-nomask"),
    ("i2i-pixel-aligned-filter-flux2-region", "i2i-pixel-aligned-filter-flux2-region"),
    ("i2i-pixel-aligned-filter-flux2-comparison", "i2i-pixel-aligned-filter-flux2-comparison"),
]


class I2IProject(models.Model):
    _name = "i2i.project"
    _description = "I2I Project"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(string="Project Name", required=True, tracking=True)
    code = fields.Char(
        string="Code",
        copy=False,
        readonly=True,
        default=lambda self: self._default_code(),
    )
    description = fields.Text()
    state = fields.Selection(
        [("active", "Active"), ("archived", "Archived")],
        string="State",
        default="active",
        tracking=True,
    )
    default_project_type = fields.Selection(
        PROJECT_TYPE_SELECTION,
        string="Default Project Type",
        default="vs-1780675368-i2i-pixel-aligned-filter-image-edit-region",
        required=True,
    )
    manager_id = fields.Many2one(
        "res.users",
        string="Manager",
        default=lambda self: self.env.user,
        tracking=True,
    )
    member_ids = fields.Many2many(
        "res.users",
        relation="i2i_project_member_rel",
        column1="project_id",
        column2="user_id",
        string="Members",
        help="Users allocated to this project (RS members). Populate "
             "manually until the RS auto-allocation is wired.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        required=True,
    )

    item_ids = fields.One2many(
        "i2i.item",
        inverse_name="project_id",
        string="Items",
    )

    item_count = fields.Integer(compute="_compute_item_counts", store=False)
    items_pending_count = fields.Integer(compute="_compute_item_counts", store=False)
    items_approved_count = fields.Integer(compute="_compute_item_counts", store=False)
    items_rejected_count = fields.Integer(compute="_compute_item_counts", store=False)

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Project code must be unique."),
    ]

    def _default_code(self):
        return self.env["ir.sequence"].next_by_code("i2i.project") or _("New")

    @api.depends("item_ids", "item_ids.state")
    def _compute_item_counts(self):
        for rec in self:
            items = rec.item_ids
            rec.item_count = len(items)
            rec.items_pending_count = len(items.filtered(
                lambda i: i.state in ("draft", "llm_qc", "human_qc")
            ))
            rec.items_approved_count = len(items.filtered(
                lambda i: i.state == "approved"
            ))
            rec.items_rejected_count = len(items.filtered(
                lambda i: i.state == "rejected"
            ))

    def action_open_items(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Items - %s") % self.name,
            "res_model": "i2i.item",
            "view_mode": "list,form,kanban",
            "domain": [("project_id", "=", self.id)],
            "context": {
                "default_project_id": self.id,
                "default_project_type": self.default_project_type,
            },
        }
