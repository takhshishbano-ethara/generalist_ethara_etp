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
        help="Combined reward across tests AND rubrics: "
             "max(0, (Σ passed_positive − Σ |triggered_negative|) / Σ all_positive)",
    )
    rubric_weights_percentage = fields.Float(
        string="Combined Weights %",
        compute="_compute_reward_metrics",
        store=True,
        digits=(6, 2),
        help="final_reward × 100 (combined across tests + rubrics)",
    )
    test_weights_percentage = fields.Float(
        string="Test Weights %",
        compute="_compute_reward_metrics",
        store=True,
        digits=(6, 2),
        help="Reward from test functions only (test_scores) × 100. "
             "Same formula as final_reward but considers only test items.",
    )
    rubric_only_weights_percentage = fields.Float(
        string="Rubric Weights %",
        compute="_compute_reward_metrics",
        store=True,
        digits=(6, 2),
        help="Reward from rubrics only (kensei2_id.rubrics) × 100. "
             "Same formula as final_reward but considers only rubric items.",
    )
    average_rubric_weights_percentage = fields.Float(
        string="Avg Combined Weights % (model)",
        compute="_compute_average_rubric_weights",
        store=False,
        digits=(6, 2),
        help="Mean of rubric_weights_percentage (combined) across all runs of this trajectory's model for the parent task.",
    )

    @api.depends(
        "test_scores",
        "test_output",
        "test_function_outputs",
        "test_code",
        "kensei2_id.rubrics",
        "kensei2_id.test_weights",
        "sandbox_id.model_type",
        "trajectory_index",
    )
    def _compute_reward_metrics(self):
        for rec in self:
            try:
                test_r, rubric_r, final_r = rec._calculate_rewards()
            except Exception as e:
                _logger.warning(
                    "reward metrics compute failed (result=%s): %s", rec.id, e
                )
                test_r, rubric_r, final_r = 0.0, 0.0, 0.0
            rec.final_reward = final_r
            rec.rubric_weights_percentage = final_r * 100.0
            rec.test_weights_percentage = test_r * 100.0
            rec.rubric_only_weights_percentage = rubric_r * 100.0

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

    def _calculate_rewards(self):
        """Return (test_only_reward, rubric_only_reward, combined_reward), each in [0, 1].

        Each reward applies the same formula
            max(0, (Σ passed_positive − Σ |triggered_negative|) / Σ all_positive)
        but scoped differently:
          * test_only_reward — uses only items from test_scores
          * rubric_only_reward — uses only items from kensei2_id.rubrics
          * combined_reward — uses both pooled together (same as old final_reward)
        """
        self.ensure_one()
        tp, tn, ta = self._accumulate_test_weights(0.0, 0.0, 0.0)
        rp, rn, ra = self._accumulate_rubric_weights(0.0, 0.0, 0.0)

        test_reward = max(0.0, (tp - tn) / ta) if ta > 0 else 0.0
        rubric_reward = max(0.0, (rp - rn) / ra) if ra > 0 else 0.0

        cp = tp + rp
        cn = tn + rn
        ca = ta + ra
        combined_reward = max(0.0, (cp - cn) / ca) if ca > 0 else 0.0

        return test_reward, rubric_reward, combined_reward

    def _calculate_final_reward(self):
        """Back-compat shim: returns just the combined reward."""
        return self._calculate_rewards()[2]

    def _accumulate_test_weights(self, sum_passed_pos, sum_triggered_neg, sum_all_pos):
        scores = self._load_effective_test_scores()
        if not scores:
            return sum_passed_pos, sum_triggered_neg, sum_all_pos

        try:
            func_outputs = json.loads(self.test_function_outputs or "{}")
        except (ValueError, TypeError):
            func_outputs = {}

        code = self.test_code or ""
        agg_output = self.test_output or ""

        # Iterate over the SCORED functions (not over functions found in
        # test_code) — Claude and GPT trajectories may have different
        # per-trajectory test_code yet share a single task-level scoring map.
        # For each scored function we accept it if it appears in this
        # trajectory's code OR its pytest output; otherwise it didn't run
        # here and is skipped.
        for fn_name, weight in scores.items():
            if not isinstance(weight, (int, float)) or weight == 0:
                continue
            in_code = bool(re.search(
                r"(?:def|async def)\s+" + re.escape(fn_name) + r"\s*\(", code
            ))
            fn_output = func_outputs.get(fn_name) or agg_output
            ran_here = bool(re.search(
                re.escape(fn_name) + r"\s+(PASSED|FAILED|ERROR)", fn_output
            ))
            if not (in_code or ran_here):
                continue
            passed = bool(re.search(
                re.escape(fn_name) + r"\s+PASSED", fn_output
            ))
            abs_w = abs(weight)
            if weight > 0:
                sum_all_pos += abs_w
                if passed:
                    sum_passed_pos += abs_w
            else:
                if passed:
                    sum_triggered_neg += abs_w
        return sum_passed_pos, sum_triggered_neg, sum_all_pos

    def _load_effective_test_scores(self):
        """Return the score map for this trajectory.

        Falls back to the parent task's test_weights if this record's own
        test_scores is empty — covers the case where the test.result record
        was created AFTER kensei2.kensei2.action_generate_test_weights ran
        (the apply loop in that method only updates records that exist at
        the time, so later-created records inherit nothing).
        """
        try:
            scores = json.loads(self.test_scores or "{}")
        except (ValueError, TypeError):
            scores = {}
        if isinstance(scores, dict) and scores:
            return scores

        if not self.kensei2_id or not self.kensei2_id.test_weights:
            return {}
        try:
            parsed = json.loads(self.kensei2_id.test_weights)
        except (ValueError, TypeError):
            return {}
        if not isinstance(parsed, list):
            return {}
        fallback = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = item.get("test_name")
            weight = item.get("weight")
            if not name or not isinstance(weight, (int, float)):
                continue
            if weight >= 100:
                fallback[name] = 30
            elif weight <= -30:
                fallback[name] = -30
            else:
                fallback[name] = max(-30, min(30, weight))
        return fallback

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
