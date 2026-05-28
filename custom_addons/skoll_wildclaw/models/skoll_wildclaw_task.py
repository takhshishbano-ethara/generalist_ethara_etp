from odoo import api, fields, models


class SkollWildclawTask(models.Model):
    _name = "skoll_wildclaw.task"
    _description = "Skoll WildClaw Task"
    _inherit = "wildclaw.task_base"

    wc_life_domain_ids = fields.Many2many("skoll_wildclaw.life_domain",
        "skoll_wildclaw_task_life_domain_rel", "task_id", "domain_id", string="Life Domains")
    wc_cluster_ids = fields.Many2many("skoll_wildclaw.cluster_tag",
        "skoll_wildclaw_task_cluster_rel", "task_id", "cluster_id", string="Clusters")
    wc_task_type_ids = fields.Many2many("skoll_wildclaw.task_type_tag",
        "skoll_wildclaw_task_type_rel", "task_id", "type_id", string="Task Types")
    wc_pattern_taxonomy_ids = fields.Many2many("skoll_wildclaw.pattern_taxonomy",
        "skoll_wildclaw_task_pattern_rel", "task_id", "pattern_id", string="Pattern Taxonomies")

    wc_user_id = fields.Many2one("res.users", string="User")
    wc_email = fields.Char(string="Email")
    wc_password = fields.Char(string="Google Password")
    wc_gog_auth = fields.Char(string="Google Auth")
    wc_gog_auth_token = fields.Text(string="Google Auth Token")
    wc_outlook_username = fields.Char(string="Outlook Username")
    wc_outlook_password = fields.Char(string="Outlook Password")
    wc_eventbrite_username = fields.Char(string="Eventbrite Username")
    wc_eventbrite_password = fields.Char(string="Eventbrite Password")
    wc_strava_username = fields.Char(string="Strava Username")
    wc_strava_password = fields.Char(string="Strava Password")
    wc_oura_username = fields.Char(string="Oura Username")
    wc_oura_password = fields.Char(string="Oura Password")
    wc_instagram_username = fields.Char(string="Instagram Username")
    wc_instagram_password = fields.Char(string="Instagram Password")
    wc_facebook_username = fields.Char(string="Facebook Username")
    wc_facebook_password = fields.Char(string="Facebook Password")
    wc_threads_username = fields.Char(string="Threads Username")
    wc_threads_password = fields.Char(string="Threads Password")

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
        default="none", string="Auto Process Status",
    )
    wc_auto_process_error = fields.Text(string="Auto Process Error")

    wc_claude_trajectory = fields.Text(string="Claude Trajectory")
    wc_glm_trajectory = fields.Text(string="GLM / Kimi Trajectory")
    wc_claude_session_status = fields.Selection(
        [("not_started", "Not Started"), ("in_progress", "In Progress"), ("completed", "Completed")],
        default="not_started",
    )
    wc_glm_session_status = fields.Selection(
        [("not_started", "Not Started"), ("in_progress", "In Progress"), ("completed", "Completed")],
        default="not_started",
    )

    wc_golden_input_mode = fields.Selection(
        [("ai", "AI Generated"), ("manual", "Manual")],
        default="ai", string="Golden Input Mode",
    )
    wc_golden_prompt = fields.Text(string="Golden Prompt")
    wc_golden_trajectory = fields.Text(string="Golden Trajectory")
    wc_golden_trajectory_manual = fields.Text(string="Manual Golden Trajectory")
    wc_golden_status = fields.Selection(
        [("pending", "Pending"), ("generating", "Generating"), ("ready", "Ready"), ("error", "Error")],
        default="pending",
    )
    wc_golden_error = fields.Text()
    wc_golden_started_at = fields.Datetime()
    wc_golden_spawn_tree = fields.Text(string="Golden Spawn Tree")
    wc_golden_qc_status = fields.Selection(
        [("pending", "Pending"), ("running", "Running"), ("passed", "Passed"), ("failed", "Failed")],
        default="pending",
    )
    wc_golden_structural_result = fields.Text(string="Structural Validation Result")
    wc_golden_qc_result = fields.Text(string="LLM QC Result")

    wc_claude_status = fields.Char(string="Claude Status", compute="_compute_model_statuses")
    wc_glm_status = fields.Char(string="GLM Status", compute="_compute_model_statuses")

    wc_is_admin = fields.Boolean(compute="_compute_is_admin", string="Is Admin")
    wc_is_ql_or_pl = fields.Boolean(compute="_compute_is_admin", string="Is QL/PL")

    wc_sandbox_ids = fields.One2many("skoll_wildclaw.sandbox", "wc_task_id", string="Sandboxes")
    wc_turn_ids = fields.One2many("skoll_wildclaw.turn", "wc_task_id", string="Turns")
    wc_generation_ids = fields.One2many("skoll_wildclaw.generation", "task_id", string="Generations")

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
        self.ensure_one()
        self.write({"wc_golden_status": "generating", "wc_golden_error": False})
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Golden Generation", "message": "Queued (deferred stub).",
                           "type": "info"}}

    def action_upload_trajectories(self):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Upload", "message": "Deferred stub.", "type": "info"}}

    def action_publish_auto_process(self):
        self.write({"wc_auto_process_status": "queued"})
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Auto-Process", "message": f"Queued {len(self)} task(s).",
                           "type": "info"}}
