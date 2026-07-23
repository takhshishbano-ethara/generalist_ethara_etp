import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "mm_performance_pipeline.py",
)

# Use the same Python interpreter that Odoo is running under (the venv python)
PYTHON_PATH = sys.executable

# Window keys matching WINDOW_PRESETS in the pipeline script
WINDOW_KEYS = ["all", "mtd", "ytd", "last_3d", "last_1w", "last_2w", "last_3w"]


class NonStemRun(models.Model):
    _name = "non.stem.run"
    _description = "Non Stem Dashboard Run"
    _order = "create_date desc"

    name = fields.Char(string="Run Name", required=True, default="New Run")
    consolidated_csv = fields.Binary(
        string="Consolidated Performance CSV", required=True, attachment=True,
    )
    consolidated_filename = fields.Char(string="Consolidated Filename")
    resource_sheet_csv = fields.Binary(
        string="Resource Sheet CSV", required=True, attachment=True,
    )
    resource_sheet_filename = fields.Char(string="Resource Sheet Filename")
    state = fields.Selection(
        [("draft", "Draft"), ("running", "Running"),
         ("done", "Done"), ("error", "Error")],
        default="draft", string="Status", readonly=True,
    )
    log_output = fields.Text(string="Pipeline Log", readonly=True)
    output_dir = fields.Char(string="Output Directory", readonly=True)

    tasker_dashboard_html = fields.Text(
        string="Tasker Dashboard HTML", readonly=True,
    )
    management_dashboard_html = fields.Text(
        string="Management Dashboard HTML", readonly=True,
    )
    window_dashboards_json = fields.Text(
        string="Window Dashboards JSON", readonly=True,
    )
    public_token = fields.Char(
        string="Public Token", readonly=True, copy=False,
    )
    public_url = fields.Char(
        string="Public Dashboard Link", compute="_compute_public_url",
    )
    is_current_user_manager = fields.Boolean(
        compute="_compute_is_current_user_manager",
    )
    current_user_role = fields.Selection(
        [("viewer", "Viewer"), ("user", "User"), ("admin", "Admin")],
        compute="_compute_current_user_role",
    )

    @api.depends("public_token")
    def _compute_public_url(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for rec in self:
            if rec.public_token:
                rec.public_url = f"{base_url}/non_stem_dashboard/public/{rec.public_token}"
            else:
                rec.public_url = False

    def _compute_is_current_user_manager(self):
        user = self.env.user
        is_mgr = user._is_admin() or user.has_group("non_stem_dashboard.group_admin")
        for rec in self:
            rec.is_current_user_manager = is_mgr

    def _compute_current_user_role(self):
        user = self.env.user
        if user._is_admin() or user.has_group("non_stem_dashboard.group_admin"):
            role = "admin"
        elif user.has_group("non_stem_dashboard.group_user"):
            role = "user"
        else:
            role = "viewer"
        for rec in self:
            rec.current_user_role = role

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("public_token"):
                vals["public_token"] = uuid.uuid4().hex
        return super().create(vals_list)

    def action_run_pipeline(self):
        self.ensure_one()
        if not self.consolidated_csv:
            raise UserError("Please upload the Consolidated Performance CSV.")

        self.write({"state": "running", "log_output": ""})

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write consolidated CSV to temp
            cons_path = os.path.join(tmpdir, self.consolidated_filename or "consolidated.csv")
            with open(cons_path, "wb") as f:
                f.write(base64.b64decode(self.consolidated_csv))

            # Build command
            cmd = [PYTHON_PATH, SCRIPT_PATH, cons_path]

            # Always pass --rs (required by pipeline)
            if not self.resource_sheet_csv:
                self.write({"state": "draft"})
                raise UserError("Please upload the Resource Sheet CSV.")
            rs_path = os.path.join(tmpdir, self.resource_sheet_filename or "resource_sheet.csv")
            with open(rs_path, "wb") as f:
                f.write(base64.b64decode(self.resource_sheet_csv))
            cmd += ["--rs", rs_path]

            try:
                # Clean up old pipeline output dirs so only fresh output remains
                script_dir = os.path.dirname(SCRIPT_PATH)
                for d in os.listdir(script_dir):
                    if d.startswith("mm_performance_") and os.path.isdir(os.path.join(script_dir, d)):
                        shutil.rmtree(os.path.join(script_dir, d))

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=tmpdir,
                )
                log = result.stdout + "\n" + result.stderr
                self.write({"log_output": log})

                if result.returncode != 0:
                    self.write({"state": "error"})
                    raise UserError(
                        f"Pipeline failed (exit code {result.returncode}):\n{log}"
                    )

                # The pipeline creates output next to the script file
                output_dirs = [
                    d for d in os.listdir(script_dir)
                    if d.startswith("mm_performance_") and os.path.isdir(os.path.join(script_dir, d))
                ]
                if not output_dirs:
                    self.write({"state": "error"})
                    raise UserError("Pipeline ran but no output folder was created.")
                out_dir = os.path.join(script_dir, output_dirs[0])

                # Read all window HTML dashboards
                window_data = {}  # {window_key: {tasker: html, management: html}}
                for wkey in WINDOW_KEYS:
                    if wkey == "all":
                        w_dir = out_dir
                    else:
                        w_dir = os.path.join(out_dir, "windows", wkey)
                    tasker_path = os.path.join(w_dir, "tasker_performance_dashboard.html")
                    mgmt_path = os.path.join(w_dir, "management_dashboard.html")
                    t_html = ""
                    m_html = ""
                    if os.path.exists(tasker_path):
                        with open(tasker_path, "r", encoding="utf-8") as f:
                            t_html = f.read()
                    if os.path.exists(mgmt_path):
                        with open(mgmt_path, "r", encoding="utf-8") as f:
                            m_html = f.read()
                    if t_html or m_html:
                        window_data[wkey] = {}
                        if t_html:
                            window_data[wkey]["tasker"] = t_html
                        if m_html:
                            window_data[wkey]["management"] = m_html

                # Rewrite nav links in all HTMLs to use Odoo routes
                run_id = self.id
                for wkey, dashboards in window_data.items():
                    for dtype in ("tasker", "management"):
                        html = dashboards.get(dtype, "")
                        if not html:
                            continue
                        # Replace relative hrefs in nav tabs with Odoo route URLs
                        html = self._rewrite_window_nav(html, dtype, run_id)
                        window_data[wkey][dtype] = html

                # "all" window is the default/main dashboard
                tasker_html = window_data.get("all", {}).get("tasker", "")
                mgmt_html = window_data.get("all", {}).get("management", "")

                # Copy output to a persistent location
                persistent_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "output",
                    f"run_{self.id}",
                )
                if os.path.exists(persistent_dir):
                    shutil.rmtree(persistent_dir)
                shutil.copytree(out_dir, persistent_dir)

                self.write({
                    "state": "done",
                    "output_dir": persistent_dir,
                    "tasker_dashboard_html": tasker_html,
                    "management_dashboard_html": mgmt_html,
                    "window_dashboards_json": json.dumps(window_data),
                })

            except subprocess.TimeoutExpired:
                self.write({"state": "error", "log_output": "Pipeline timed out after 300 seconds."})
                raise UserError("Pipeline timed out after 300 seconds.")
            except UserError:
                raise
            except Exception as e:
                _logger.exception("Pipeline execution failed")
                self.write({"state": "error", "log_output": str(e)})
                raise UserError(f"Pipeline error: {e}")

    def _rewrite_window_nav(self, html, dashboard_type, run_id):
        """Rewrite nav tab hrefs from relative file paths to Odoo route URLs."""
        base = f"/non_stem_dashboard/{dashboard_type}/{run_id}"

        def _replace_href(match):
            href = match.group(1)
            # Check each non-"all" window key in the href path
            for wkey in WINDOW_KEYS:
                if wkey == "all":
                    continue
                if f"/{wkey}/" in href or href.startswith(f"{wkey}/") or href.startswith(f"../{wkey}/"):
                    return f'href="{base}/{wkey}"'
            # If no window key matched, it points to the root = "all"
            return f'href="{base}/all"'

        html = re.sub(
            r'href="([^"]*(?:tasker_performance_dashboard|management_dashboard)\.html[^"]*)"',
            _replace_href, html,
        )
        return html

    def get_window_html(self, dashboard_type, window_key):
        """Return HTML for a specific window and dashboard type."""
        self.ensure_one()
        if not self.window_dashboards_json:
            return False
        data = json.loads(self.window_dashboards_json)
        window = data.get(window_key, {})
        return window.get(dashboard_type, False)

    @api.model
    def check_is_manager(self):
        user = self.env.user
        return user._is_admin() or user.has_group("non_stem_dashboard.group_admin")

    @api.model
    def get_user_role(self):
        user = self.env.user
        if user._is_admin() or user.has_group("non_stem_dashboard.group_admin"):
            return "admin"
        elif user.has_group("non_stem_dashboard.group_user"):
            return "user"
        elif user.has_group("non_stem_dashboard.group_viewer"):
            return "viewer"
        return False

    def action_copy_public_tasker_link(self):
        self.ensure_one()
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        url = f"{base_url}/non_stem_dashboard/public/tasker"
        return {
            "type": "ir.actions.client",
            "tag": "non_stem_copy_public_link",
            "params": {"url": url},
        }

    def action_copy_public_management_link(self):
        self.ensure_one()
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        url = f"{base_url}/non_stem_dashboard/public/management"
        return {
            "type": "ir.actions.client",
            "tag": "non_stem_copy_public_link",
            "params": {"url": url},
        }

    def action_view_tasker_dashboard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/non_stem_dashboard/tasker/{self.id}",
            "target": "new",
        }

    def action_view_management_dashboard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/non_stem_dashboard/management/{self.id}",
            "target": "new",
        }
