import base64
import logging
import os
import subprocess
import sys
import tempfile

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
        string="Resource Sheet CSV", attachment=True,
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

            # Write resource sheet if provided
            if self.resource_sheet_csv:
                rs_path = os.path.join(tmpdir, self.resource_sheet_filename or "resource_sheet.csv")
                with open(rs_path, "wb") as f:
                    f.write(base64.b64decode(self.resource_sheet_csv))
                cmd += ["--rs", rs_path]

            try:
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

                # The pipeline creates output next to the script file, not in cwd
                script_dir = os.path.dirname(SCRIPT_PATH)
                output_dirs = [
                    d for d in os.listdir(script_dir)
                    if d.startswith("mm_performance_") and os.path.isdir(os.path.join(script_dir, d))
                ]
                if not output_dirs:
                    self.write({"state": "error"})
                    raise UserError("Pipeline ran but no output folder was created.")

                # Use the most recently created output dir
                output_dirs.sort(
                    key=lambda d: os.path.getctime(os.path.join(script_dir, d)),
                    reverse=True,
                )
                out_dir = os.path.join(script_dir, output_dirs[0])

                # Read the HTML dashboards
                tasker_html = ""
                mgmt_html = ""
                tasker_path = os.path.join(out_dir, "tasker_performance_dashboard.html")
                mgmt_path = os.path.join(out_dir, "management_dashboard.html")

                if os.path.exists(tasker_path):
                    with open(tasker_path, "r", encoding="utf-8") as f:
                        tasker_html = f.read()
                if os.path.exists(mgmt_path):
                    with open(mgmt_path, "r", encoding="utf-8") as f:
                        mgmt_html = f.read()

                # Copy output to a persistent location
                persistent_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "output",
                    f"run_{self.id}",
                )
                os.makedirs(persistent_dir, exist_ok=True)
                for fname in os.listdir(out_dir):
                    src = os.path.join(out_dir, fname)
                    dst = os.path.join(persistent_dir, fname)
                    with open(src, "rb") as sf, open(dst, "wb") as df:
                        df.write(sf.read())

                self.write({
                    "state": "done",
                    "output_dir": persistent_dir,
                    "tasker_dashboard_html": tasker_html,
                    "management_dashboard_html": mgmt_html,
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

    @api.model
    def check_is_manager(self):
        user = self.env.user
        return user._is_admin() or user.has_group("non_stem_dashboard.group_manager")

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
