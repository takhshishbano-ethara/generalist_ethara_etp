from odoo import api, fields, models


class KenseiWildclawTask(models.Model):
    _name = "kensei_wildclaw.task"
    _description = "Kensei WildClaw Task"
    _inherit = "wildclaw.task_base"

    wc_user_id = fields.Many2one("res.users", string="Assigned User")
    wc_email = fields.Char(string="Email")
    wc_password = fields.Char(string="Password")
    wc_gog_auth = fields.Char(string="Google Auth Cookie")
    wc_gog_auth_token = fields.Char(string="Google Auth Token")

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
        [("pending", "Pending"), ("draft", "Draft"), ("qc", "Under QC"),
         ("completed", "Completed"), ("failed", "Failed")],
        string="Task Status", default="pending",
    )
    wc_auto_process_status = fields.Selection(
        [("none", "None"), ("queued", "Queued"), ("processing", "Processing"),
         ("done", "Done"), ("failed", "Failed")],
        string="Auto-Process Status", default="none",
    )
    wc_auto_process_error = fields.Text(string="Auto-Process Error")

    wc_batch_status = fields.Selection(
        [("idle", "Idle"), ("starting", "Starting"), ("running", "Running"),
         ("stopping", "Stopping"), ("done", "Done"), ("error", "Error")],
        string="Batch Status", default="idle",
    )
    wc_batch_prompt = fields.Text(string="Batch Prompt")
    wc_batch_size = fields.Integer(string="Batch Size", default=8)
    wc_batch_started_at = fields.Datetime(string="Batch Started At")
    wc_batch_completed_at = fields.Datetime(string="Batch Completed At")

    wc_claude_trajectory = fields.Text(string="Claude Trajectory")
    wc_gpt_trajectory = fields.Text(string="GPT Trajectory")
    wc_golden_trajectory = fields.Text(string="Golden Trajectory")
    wc_golden_status = fields.Selection(
        [("pending", "Pending"), ("generating", "Generating"),
         ("ready", "Ready"), ("error", "Error")],
        string="Golden Status", default="pending",
    )
    wc_golden_error = fields.Text(string="Golden Error")
    wc_golden_started_at = fields.Datetime(string="Golden Started At")

    wc_onePA_trajectory = fields.Text(string="1PA Trajectory")
    wc_onePB_trajectory = fields.Text(string="1PB Trajectory")
    wc_onePC_trajectory = fields.Text(string="1PC Trajectory")
    wc_onePD_trajectory = fields.Text(string="1PD Trajectory")

    wc_claude_status = fields.Char(string="Claude Status", compute="_compute_model_statuses")
    wc_gpt_status = fields.Char(string="GPT Status", compute="_compute_model_statuses")

    wc_is_admin = fields.Boolean(string="Is Admin", compute="_compute_role_flags")
    wc_is_ql_or_pl = fields.Boolean(string="Is QL/PL", compute="_compute_role_flags")

    wc_file_attachment_ids = fields.Many2many(
        "wildclaw.media.attachment",
        "kensei_wildclaw_task_attachment_rel",
        "wc_task_id",
        "wc_attachment_id",
        string="File Attachments",
    )

    wc_intent_test_jsonl = fields.Text(string="Intent Test JSONL")
    wc_intent_test_status = fields.Selection(
        [("pending", "Pending"), ("generating", "Generating"),
         ("ready", "Ready"), ("error", "Error")],
        string="Intent Test Status", default="pending",
    )

    wc_sandbox_ids = fields.One2many("kensei_wildclaw.sandbox", "wc_task_id", string="Sandboxes")
    wc_turn_ids = fields.One2many("kensei_wildclaw.turn", "wc_task_id", string="Turns")

    @api.depends("wc_sandbox_ids", "wc_sandbox_ids.model_type", "wc_sandbox_ids.session_status")
    def _compute_model_statuses(self):
        for rec in self:
            claude = rec.wc_sandbox_ids.filtered(lambda s: s.model_type == "claude")[:1]
            gpt = rec.wc_sandbox_ids.filtered(lambda s: s.model_type == "gpt")[:1]
            rec.wc_claude_status = claude.session_status if claude else "not_started"
            rec.wc_gpt_status = gpt.session_status if gpt else "not_started"

    def _compute_role_flags(self):
        admin = self.env.user.has_group("wildclaw_core.group_wildclaw_pl")
        ql_or_pl = (admin or self.env.user.has_group("wildclaw_core.group_wildclaw_ql"))
        for rec in self:
            rec.wc_is_admin = admin
            rec.wc_is_ql_or_pl = ql_or_pl

    def action_generate_golden_trajectory(self):
        self.ensure_one()
        self.write({"wc_golden_status": "generating", "wc_golden_started_at": fields.Datetime.now(),
                    "wc_golden_error": False})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Golden Generation", "message": f"Queued for {self.task_id}", "sticky": False, "type": "info"},
        }

    def action_export_to_harbor(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Harbor Export", "message": "Export pending (stub)", "sticky": False, "type": "info"},
        }

    def action_publish_auto_process(self):
        for rec in self:
            rec.write({"wc_auto_process_status": "queued"})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Auto-Process", "message": f"{len(self)} task(s) queued", "sticky": False, "type": "success"},
        }

    def action_mass_export_to_harbor(self):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Harbor Export", "message": f"{len(self)} task(s) export pending (stub)", "sticky": False, "type": "info"},
        }

    def action_download_harbor_bundle(self):
        return {
            "type": "ir.actions.act_url",
            "url": "/kensei_wildclaw/harbor/download",
            "target": "self",
        }
