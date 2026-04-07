from odoo import models, fields, api
from odoo.exceptions import ValidationError


class TalosPersona(models.Model):
    _name = "talos.persona"
    _description = "Talos Persona"
    _order = "name"

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)

    soul_md = fields.Text(string="SOUL.md")
    memory_md = fields.Text(string="MEMORY.md")
    agents_md = fields.Text(string="AGENTS.md")

    litellm_config_yaml = fields.Text(
        string="LiteLLM Config (YAML)",
        help="Per-persona litellm_config.yaml content. "
        "If empty, the global default from Talos settings is used.",
    )
    docker_compose_yaml = fields.Text(
        string="Docker Compose (YAML)",
        help="Per-persona docker-compose.yml content. "
        "If empty, the bundled default from the module is used.",
    )

    task_ids = fields.One2many("talos.talos", "persona_id", string="Tasks")
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
