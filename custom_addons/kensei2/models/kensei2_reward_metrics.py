"""Reward / rubric-weight metrics for kensei2 test results.

Computes the rubric-weighted reward across both **test functions** (weighted via
kensei2.test.result.test_scores) and **rubrics** (weighted via
kensei2.kensei2.rubrics[*].score), per the formula:

    final_reward = max(0,
        (Σ passed_positive_weights − Σ |triggered_negative_weights|)
        / Σ all_positive_weights
    )
    rubric_weights_percentage          = final_reward × 100
    average_rubric_weights_percentage  = mean(rubric_weights_percentage
                                              across all runs for a model)

Convention:
- Each kensei2.test.result is "a run" (one trajectory). Rubric slot at
  index = trajectory_index - 1 (clamped) for this run's model_type is what we
  evaluate for that rubric in this run.
- An item "passed"/"triggered" means its criterion was met: for a test
  function, pytest reports PASSED in the most recent output (per-function
  retry overrides aggregate output); for a rubric, the slot is marked "pass".
- Positive items contribute their weight to the denominator (max reward) and
  to the numerator iff they passed.
- Negative items contribute |weight| to the numerator as a penalty iff
  triggered. They do NOT enlarge the denominator.
"""

import json
import logging
import re

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_TEST_FUNC_RE = re.compile(r"(?:def|async def)\s+(test_\w+)\s*\(")


class Kensei2TestResultRewardMetrics(models.Model):
    _inherit = "kensei2.test.result"

    final_reward = fields.Float(
        string="Final Reward",
        compute="_compute_reward_metrics",
        store=True,
        digits=(6, 4),
        help="max(0, (Σ passed_positive − Σ |triggered_negative|) / Σ all_positive)",
    )
    rubric_weights_percentage = fields.Float(
        string="Rubric Weights %",
        compute="_compute_reward_metrics",
        store=True,
        digits=(6, 2),
        help="final_reward × 100",
    )
    average_rubric_weights_percentage = fields.Float(
        string="Avg Rubric Weights % (model)",
        compute="_compute_average_rubric_weights",
        store=False,
        digits=(6, 2),
        help="Mean rubric_weights_percentage across all runs of this trajectory's model for the parent task.",
    )

    @api.depends(
        "test_scores",
        "test_output",
        "test_function_outputs",
        "test_code",
        "kensei2_id.rubrics",
        "sandbox_id.model_type",
        "trajectory_index",
    )
    def _compute_reward_metrics(self):
        for rec in self:
            try:
                fr = rec._calculate_final_reward()
            except Exception as e:
                _logger.warning(
                    "final_reward compute failed (result=%s): %s", rec.id, e
                )
                fr = 0.0
            rec.final_reward = fr
            rec.rubric_weights_percentage = fr * 100.0

    @api.depends("kensei2_id", "sandbox_id.model_type", "rubric_weights_percentage")
    def _compute_average_rubric_weights(self):
        cache = {}
        for rec in self:
            model_type = rec.sandbox_id.model_type if rec.sandbox_id else None
            key = (rec.kensei2_id.id if rec.kensei2_id else None, model_type)
            if not key[0] or not key[1]:
                rec.average_rubric_weights_percentage = 0.0
                continue
            if key not in cache:
                siblings = self.sudo().search([
                    ("kensei2_id", "=", key[0]),
                    ("sandbox_id.model_type", "=", key[1]),
                ])
                vals = [s.rubric_weights_percentage for s in siblings]
                cache[key] = sum(vals) / len(vals) if vals else 0.0
            rec.average_rubric_weights_percentage = cache[key]

    def _calculate_final_reward(self):
        """Return final_reward in [0, 1] for this single test result."""
        self.ensure_one()
        sum_passed_pos = 0.0
        sum_triggered_neg = 0.0
        sum_all_pos = 0.0

        sum_passed_pos, sum_triggered_neg, sum_all_pos = self._accumulate_test_weights(
            sum_passed_pos, sum_triggered_neg, sum_all_pos
        )
        sum_passed_pos, sum_triggered_neg, sum_all_pos = self._accumulate_rubric_weights(
            sum_passed_pos, sum_triggered_neg, sum_all_pos
        )

        if sum_all_pos <= 0:
            return 0.0
        return max(0.0, (sum_passed_pos - sum_triggered_neg) / sum_all_pos)

    def _accumulate_test_weights(self, sum_passed_pos, sum_triggered_neg, sum_all_pos):
        try:
            scores = json.loads(self.test_scores or "{}")
        except (ValueError, TypeError):
            scores = {}
        if not isinstance(scores, dict) or not scores:
            return sum_passed_pos, sum_triggered_neg, sum_all_pos

        try:
            func_outputs = json.loads(self.test_function_outputs or "{}")
        except (ValueError, TypeError):
            func_outputs = {}

        functions = _TEST_FUNC_RE.findall(self.test_code or "")
        agg_output = self.test_output or ""

        for fn in functions:
            weight = scores.get(fn)
            if not isinstance(weight, (int, float)) or weight == 0:
                continue
            fn_output = func_outputs.get(fn) or agg_output
            passed = bool(re.search(re.escape(fn) + r"\s+PASSED", fn_output))
            abs_w = abs(weight)
            if weight > 0:
                sum_all_pos += abs_w
                if passed:
                    sum_passed_pos += abs_w
            else:
                if passed:
                    sum_triggered_neg += abs_w
        return sum_passed_pos, sum_triggered_neg, sum_all_pos

    def _accumulate_rubric_weights(self, sum_passed_pos, sum_triggered_neg, sum_all_pos):
        if not self.kensei2_id or not self.kensei2_id.rubrics:
            return sum_passed_pos, sum_triggered_neg, sum_all_pos
        model_type = self.sandbox_id.model_type if self.sandbox_id else None
        if not model_type:
            return sum_passed_pos, sum_triggered_neg, sum_all_pos

        try:
            parsed = json.loads(self.kensei2_id.rubrics)
        except (ValueError, TypeError):
            return sum_passed_pos, sum_triggered_neg, sum_all_pos

        if isinstance(parsed, list):
            rubrics = parsed
        elif isinstance(parsed, dict) and isinstance(parsed.get("rubrics"), list):
            rubrics = parsed["rubrics"]
        else:
            return sum_passed_pos, sum_triggered_neg, sum_all_pos

        slot_idx = max(0, (self.trajectory_index or 1) - 1)

        for rb in rubrics:
            if not isinstance(rb, dict):
                continue
            try:
                weight = float(rb.get("score") or 0)
            except (ValueError, TypeError):
                continue
            if weight == 0:
                continue
            slots = rb.get(model_type)
            if not isinstance(slots, list) or slot_idx >= len(slots):
                continue
            passed = slots[slot_idx] == "pass"
            is_positive = bool(rb.get("is_positive", weight > 0))
            abs_w = abs(weight)
            if is_positive:
                sum_all_pos += abs_w
                if passed:
                    sum_passed_pos += abs_w
            else:
                if passed:
                    sum_triggered_neg += abs_w
        return sum_passed_pos, sum_triggered_neg, sum_all_pos
