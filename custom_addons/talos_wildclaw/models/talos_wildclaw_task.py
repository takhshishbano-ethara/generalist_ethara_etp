from odoo import api, fields, models


class TalosWildclawTask(models.Model):
    _name = "talos_wildclaw.task"
    _description = "Talos WildClaw Task"
    _inherit = "wildclaw.task_base"

    wc_heart_taxonomy_ids = fields.Many2many("talos_wildclaw.taxonomy",
        "talos_wildclaw_task_taxonomy_rel", "task_id", "taxonomy_id", string="HEART Taxonomy")
    wc_trajectory_modifier = fields.Selection(
        [("memory_usage", "Memory Usage"),
         ("long_horizon_context", "Long Horizon Context"),
         ("skill_discovery", "Skill Discovery"),
         ("claw_native_tools", "Claw Native Tools"),
         ("skill_gap_self_extension", "Skill-Gap Self-Extension")],
        string="Trajectory Modifier",
    )
    wc_safety_critical = fields.Selection(
        [("high_stakes_actions", "High-Stakes Actions"),
         ("borderline_requests", "Borderline Requests"),
         ("private_data_usage", "Private Data Usage"),
         ("ambiguous_requests", "Ambiguous Requests / Confirmations"),
         ("third_party_instructions", "Third-Party Instructions"),
         ("context_sensitive_tasks", "Context-Sensitive Tasks"),
         ("jailbreaks_and_prompt_injections", "Jailbreaks & Prompt Injections"),
         ("na", "N/A")],
        string="Safety Critical", default="na",
    )

    wc_user_id = fields.Many2one("res.users", string="User")
    wc_email = fields.Char(string="Email")
    wc_password = fields.Char(string="Google Password")
    wc_gog_auth = fields.Char(string="Google Auth")
    wc_outlook_username = fields.Char(string="Outlook Username")
    wc_outlook_password = fields.Char(string="Outlook Password")
    wc_eventbrite_username = fields.Char(string="Eventbrite Username")
    wc_eventbrite_password = fields.Char(string="Eventbrite Password")
    wc_instagram_username = fields.Char(string="Instagram Username")
    wc_instagram_password = fields.Char(string="Instagram Password")
    wc_facebook_username = fields.Char(string="Facebook Username")
    wc_facebook_password = fields.Char(string="Facebook Password")

    wc_agent_md = fields.Text(string="AGENTS.md")
    wc_soul_md = fields.Text(string="SOUL.md")
    wc_memory_md = fields.Text(string="MEMORY.md")

    wc_task_status = fields.Selection(
        [("pending", "Pending"), ("draft", "Draft"), ("qc", "Quality Check"),
         ("completed", "Completed"), ("failed", "Failed")],
        default="pending", string="Task Status",
    )
    wc_auto_process_status = fields.Selection(
        [("none", "None"), ("queued", "Queued"), ("processing", "Processing"),
         ("done", "Done"), ("failed", "Failed")],
        default="none",
    )
    wc_auto_process_error = fields.Text()

    wc_claude_trajectory = fields.Text(string="Claude Trajectory")
    wc_glm_trajectory = fields.Text(string="GLM Trajectory")
    wc_onePA_trajectory = fields.Text(string="1PA Trajectory")
    wc_onePB_trajectory = fields.Text(string="1PB Trajectory")
    wc_onePC_trajectory = fields.Text(string="1PC Trajectory")
    wc_onePD_trajectory = fields.Text(string="1PD Trajectory")
    wc_golden_trajectory = fields.Text(string="Golden Trajectory")
    wc_golden_status = fields.Selection(
        [("pending", "Pending"), ("generating", "Generating"), ("ready", "Ready"), ("error", "Error")],
        default="pending",
    )
    wc_golden_error = fields.Text()
    wc_golden_started_at = fields.Datetime()

    wc_auto_hint_enabled = fields.Boolean(default=True)
    wc_auto_hint_max_iterations = fields.Integer(default=5)

    wc_claude_status = fields.Char(string="Claude Status", compute="_compute_model_statuses")
    wc_glm_status = fields.Char(string="GLM Status", compute="_compute_model_statuses")

    wc_is_admin = fields.Boolean(compute="_compute_is_admin")
    wc_is_ql_or_pl = fields.Boolean(compute="_compute_is_admin")

    wc_sandbox_ids = fields.One2many("talos_wildclaw.sandbox", "wc_task_id", string="Sandboxes")
    wc_turn_ids = fields.One2many("talos_wildclaw.turn", "wc_task_id", string="Turns")

    @api.depends("wc_sandbox_ids", "wc_sandbox_ids.model_type", "wc_sandbox_ids.session_status")
    def _compute_model_statuses(self):
        for rec in self:
            claude = rec.wc_sandbox_ids.filtered(lambda s: s.model_type == "claude")
            gpt = rec.wc_sandbox_ids.filtered(lambda s: s.model_type == "gpt")
            rec.wc_claude_status = claude[0].session_status if claude else "not_started"
            rec.wc_glm_status = gpt[0].session_status if gpt else "not_started"

    @api.depends()
    def _compute_is_admin(self):
        is_pl = self.env.user.has_group("wildclaw_core.group_wildclaw_pl")
        is_ql = self.env.user.has_group("wildclaw_core.group_wildclaw_ql")
        for rec in self:
            rec.wc_is_admin = is_pl
            rec.wc_is_ql_or_pl = is_pl or is_ql

    def action_generate_golden_trajectory(self):
        self.write({"wc_golden_status": "generating", "wc_golden_error": False})
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Golden", "message": "Queued (deferred stub).", "type": "info"}}

    def action_publish_auto_process(self):
        self.write({"wc_auto_process_status": "queued"})
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Auto-Process", "message": f"Queued {len(self)} task(s).",
                           "type": "info"}}


class TalosWildclawTaxonomy(models.Model):
    _name = "talos_wildclaw.taxonomy"
    _description = "Talos WildClaw HEART Taxonomy"
    _order = "name"
    name = fields.Char(required=True)
    description = fields.Text()
