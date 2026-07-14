from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class KenseiPersona(models.Model):
    _name = "kensei.persona"
    _description = "Kensei Persona"
    _order = "name"

    is_kensei_admin = fields.Boolean(
        compute="_compute_is_kensei_admin",
        search="_search_is_kensei_admin",
    )

    @api.depends_context("uid")
    def _compute_is_kensei_admin(self):
        is_admin = self.env.user.has_group("kensei.group_kensei_ql")
        for rec in self:
            rec.is_kensei_admin = is_admin

    def _search_is_kensei_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("kensei.group_kensei_ql")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)


    l1_category = fields.Char(string="L1 Category")
    l2_category = fields.Char(string="L2 Category")

    soul_md = fields.Text(string="SOUL.md")
    memory_md = fields.Text(string="MEMORY.md")
    agents_md = fields.Text(string="AGENTS.md")

    litellm_config_yaml = fields.Text(
        string="LiteLLM Config (YAML)",
        help="Per-persona litellm_config.yaml content. "
        "If empty, the global default from Kensei settings is used.",
    )
    docker_compose_yaml = fields.Text(
        string="Docker Compose (YAML)",
        help="Per-persona docker-compose.yml content. "
        "If empty, the bundled default from the module is used.",
    )

    task_ids = fields.One2many("kensei.kensei", "persona_id", string="Tasks")
    task_count = fields.Integer(compute="_compute_task_count")

    @api.depends("task_ids")
    def _compute_task_count(self):
        for rec in self:
            rec.task_count = len(rec.task_ids)

    @api.constrains("name")
    def _check_name(self):
        for rec in self:
            if not rec.name or not rec.name.strip():
                raise ValidationError("Persona name cannot be empty.")
            sanitized = rec.name.strip().lower().replace(" ", "-")
            existing = self.search(
                [("name", "=ilike", sanitized), ("id", "!=", rec.id)], limit=1
            )
            if existing:
                raise ValidationError(
                    "A persona with name '%s' already exists." % sanitized
                )

    def write(self, vals):
        if "name" in vals:
            vals["name"] = vals["name"].strip().lower().replace(" ", "-")
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "name" in vals:
                vals["name"] = vals["name"].strip().lower().replace(" ", "-")
        return super().create(vals_list)
