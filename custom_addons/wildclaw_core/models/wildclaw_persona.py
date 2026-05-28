from odoo import api, fields, models
from odoo.exceptions import ValidationError


class WildclawPersona(models.Model):
    _name = "wildclaw.persona"
    _description = "WildClaw Persona (shared base — soul/memory/agents.md injection into sandbox containers)"
    _order = "name"

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)

    soul_md = fields.Text(string="SOUL.md")
    memory_md = fields.Text(string="MEMORY.md")
    agents_md = fields.Text(string="AGENTS.md")

    litellm_config_yaml = fields.Text(
        string="LiteLLM Config (YAML)",
        help="Per-persona litellm_config.yaml content. Empty = use global default.",
    )
    docker_compose_yaml = fields.Text(
        string="Docker Compose (YAML)",
        help="Per-persona docker-compose.yml content. Empty = use bundled default.",
    )

    is_wildclaw_admin = fields.Boolean(
        compute="_compute_is_wildclaw_admin",
        search="_search_is_wildclaw_admin",
    )

    @api.depends_context("uid")
    def _compute_is_wildclaw_admin(self):
        is_admin = self.env.user.has_group("wildclaw_core.group_wildclaw_ql")
        for rec in self:
            rec.is_wildclaw_admin = is_admin

    def _search_is_wildclaw_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("wildclaw_core.group_wildclaw_ql")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

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
