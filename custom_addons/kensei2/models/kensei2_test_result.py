import json
import logging
import re
import time

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class Kensei2TestResult(models.Model):
    _name = "kensei2.test.result"
    _description = "Kensei2 Auto-Generated Test Result"
    _order = "create_date desc, id desc"

    sandbox_id = fields.Many2one(
        "kensei2.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    model_type = fields.Selection(
        related="sandbox_id.model_type", store=True, readonly=True
    )
    kensei2_id = fields.Many2one(
        related="sandbox_id.kensei2_id", store=True, readonly=True
    )
    task_id = fields.Char(
        related="kensei2_id.task_id", string="Task ID", store=True, index=True
    )
    trajectory_index = fields.Integer(
        string="Trajectory #", default=0, index=True
    )

    model_used = fields.Char(string="LLM Used for Generation")
    status = fields.Selection(
        [
            ("pending", "Pending Execution"),
            ("generating", "Generating"),
            ("running", "Running"),
            ("passed", "Passed"),
            ("failed", "Failed"),
            ("error", "Error"),
        ],
        string="Status",
        default="generating",
    )

    score = fields.Integer(
        string="Score", default=0,
    )
    test_scores = fields.Text(
        string="Per-Function Scores", default="{}"
    )
    test_function_outputs = fields.Text(
        string="Per-Function Re-run Outputs", default="{}"
    )

    test_code = fields.Text(string="Test Code")
    test_output = fields.Text(string="Execution Output")

    tests_total = fields.Integer(string="Total Tests", default=0)
    tests_passed = fields.Integer(string="Passed", default=0)
    tests_failed = fields.Integer(string="Failed", default=0)
    tests_errored = fields.Integer(string="Errors", default=0)

    generation_prompt = fields.Text(string="Generation Prompt")
    generation_tokens_in = fields.Integer(string="Input Tokens", default=0)
    generation_tokens_out = fields.Integer(string="Output Tokens", default=0)
    duration_generation_ms = fields.Float(string="Generation Duration (ms)")
    duration_execution_ms = fields.Float(string="Execution Duration (ms)")

    def action_rerun_test(self):
        """Manually re-execute this test against its sandbox.

        Recovery for the race where _generate_intent_tests finishes after
        action_stop_sandbox has already run _run_pending_tests, leaving the
        record stuck at status='pending' with no output. Requires the sandbox
        container/pod to still be alive — mock-API state lives there and
        cannot be reconstructed once torn down.
        """
        for record in self:
            sandbox = record.sandbox_id
            if not sandbox:
                raise UserError("No sandbox linked to this test result.")
            if not record.test_code or not record.test_code.strip():
                raise UserError(
                    "No test code to execute. Generation may have failed — "
                    "check Generation Prompt and regenerate via the sandbox."
                )
            if sandbox.docker_status not in ("running", "stopping"):
                raise UserError(
                    "Cannot re-execute: sandbox status is '%s'. The container "
                    "and mock-API state are gone. Restart the sandbox and "
                    "re-run the task to regenerate tests against fresh state."
                    % sandbox.docker_status
                )

            record.write({"status": "running", "test_output": False})

            try:
                exec_start = time.time()
                test_output = sandbox._execute_tests_in_sandbox(record.test_code)
                exec_duration_ms = (time.time() - exec_start) * 1000

                total, passed, failed, errored = sandbox._parse_pytest_output(
                    test_output
                )
                status = "passed" if failed == 0 and errored == 0 else "failed"

                record.write({
                    "test_output": test_output,
                    "tests_total": total,
                    "tests_passed": passed,
                    "tests_failed": failed,
                    "tests_errored": errored,
                    "duration_execution_ms": exec_duration_ms,
                    "status": status,
                })

                _logger.info(
                    "Manual re-run complete (result=%s, sandbox=%s): "
                    "%d total, %d passed, %d failed, %d errors",
                    record.id, sandbox.id, total, passed, failed, errored,
                )

            except Exception as e:
                _logger.exception(
                    "Manual re-run failed (result=%s, sandbox=%s): %s",
                    record.id, sandbox.id, e,
                )
                record.write({
                    "status": "error",
                    "test_output": "Exception during re-execution: %s" % str(e)[:2000],
                })
                raise UserError("Re-execution failed: %s" % str(e)[:500])

        return True

    def action_rerun_test_function(self, func_name):
        """Re-execute a single test function within this trajectory.

        Targets the race where one test in a trajectory fails due to mock-API
        timing while the rest are valid — rerunning only the affected function
        avoids invalidating sibling results. Output is stored in
        test_function_outputs[func_name]; the aggregate test_output stays put
        so untouched functions keep their original execution context.
        """
        self.ensure_one()
        if not func_name or not re.match(r"^test_\w+$", func_name):
            raise UserError("Invalid test function name: %r" % func_name)

        sandbox = self.sandbox_id
        if not sandbox:
            raise UserError("No sandbox linked to this test result.")
        if not self.test_code or not self.test_code.strip():
            raise UserError(
                "No test code to execute. Generation may have failed — "
                "check Generation Prompt and regenerate via the sandbox."
            )
        if sandbox.docker_status not in ("running", "stopping"):
            raise UserError(
                "Cannot re-execute: sandbox status is '%s'. The container "
                "and mock-API state are gone. Restart the sandbox and "
                "re-run the task to regenerate tests against fresh state."
                % sandbox.docker_status
            )
        if func_name not in self.test_code:
            raise UserError(
                "Function %s not found in this trajectory's test code." % func_name
            )

        try:
            test_output = sandbox._execute_tests_in_sandbox(
                self.test_code, test_filter=func_name
            )
            total, passed, failed, errored = sandbox._parse_pytest_output(
                test_output
            )

            try:
                func_outputs = json.loads(self.test_function_outputs or "{}")
            except (ValueError, TypeError):
                func_outputs = {}
            func_outputs[func_name] = test_output
            self.test_function_outputs = json.dumps(func_outputs)

            _logger.info(
                "Per-function re-run complete (result=%s, sandbox=%s, fn=%s): "
                "%d total, %d passed, %d failed, %d errors",
                self.id, sandbox.id, func_name, total, passed, failed, errored,
            )

            return {
                "func_name": func_name,
                "test_output": test_output,
                "test_function_outputs": self.test_function_outputs,
                "total": total, "passed": passed,
                "failed": failed, "errored": errored,
            }

        except Exception as e:
            _logger.exception(
                "Per-function re-run failed (result=%s, sandbox=%s, fn=%s): %s",
                self.id, sandbox.id, func_name, e,
            )
            raise UserError(
                "Re-execution of %s failed: %s" % (func_name, str(e)[:500])
            )
