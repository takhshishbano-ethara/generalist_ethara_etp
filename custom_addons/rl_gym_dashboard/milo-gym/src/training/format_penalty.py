"""Format penalty for multi-turn tool-integrated reasoning.

Implements the GTPO paper's format enforcement:
  r_format = -0.1 if turn has format violations; 0.0 otherwise
  First turn MUST have tool calls or gets -0.1 penalty

Applies to each assistant turn independently. Violations:
  1. No <tool_call> tag when tool use expected (first turn always, later turns when not submitting)
  2. Malformed JSON inside <tool_call>
  3. Invalid action name
  4. Missing required arguments for the action

Reference: arXiv:2511.14846, Section 3.3
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

TOOL_CALL_PATTERN = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)

VALID_ACTIONS = frozenset({
    "apply_patch",
    "run_tests",
    "read_file",
    "search",
    "submit",
    "run_command",
    "list_files",
})


@dataclass
class FormatPenaltyConfig:
    """Configuration for format penalties."""

    enabled: bool = True
    penalty_per_violation: float = -0.1
    first_turn_must_have_tool: bool = True
    penalize_malformed_json: bool = True
    penalize_invalid_action: bool = True
    penalize_missing_args: bool = True

    # Required arguments per action
    required_args: dict[str, list[str]] | None = None

    def __post_init__(self) -> None:
        if self.required_args is None:
            self.required_args = {
                "apply_patch": ["patch"],
                "run_tests": [],
                "read_file": ["path"],
                "search": ["query"],
                "submit": ["patch"],
            }


class FormatPenaltyComputer:
    """Computes per-turn format penalties for reward shaping.

    Returns a list of penalties (negative values) per assistant turn.
    These are ADDED to per-turn rewards before GTPO discounted returns.
    """

    def __init__(self, config: FormatPenaltyConfig | None = None) -> None:
        self._config = config or FormatPenaltyConfig()

    def compute_turn_penalties(
        self,
        assistant_turns: list[str],
    ) -> list[float]:
        """Compute format penalty for each assistant turn.

        Args:
            assistant_turns: Content of each assistant message in the trajectory.

        Returns:
            Penalty for each turn (0.0 = no violation, -0.1 = violation).
        """
        if not self._config.enabled:
            return [0.0] * len(assistant_turns)

        penalties: list[float] = []

        for turn_idx, content in enumerate(assistant_turns):
            penalty = self._check_turn(content, turn_idx)
            penalties.append(penalty)

        return penalties

    def compute_trajectory_penalties(
        self,
        messages: list[dict[str, str]],
    ) -> list[float]:
        """Compute penalties from a full message list (extracts assistant turns).

        Args:
            messages: Full conversation (system/user/assistant/tool turns).

        Returns:
            Penalty per assistant turn (aligned with turn indices used by PRM).
        """
        assistant_turns = [
            msg.get("content", "")
            for msg in messages
            if msg.get("role") == "assistant"
        ]
        return self.compute_turn_penalties(assistant_turns)

    def _check_turn(self, content: str, turn_idx: int) -> float:
        """Check a single assistant turn for format violations."""
        violations = 0

        # Find tool calls
        matches = TOOL_CALL_PATTERN.findall(content)

        # First turn MUST have tool calls (paper requirement)
        if turn_idx == 0 and self._config.first_turn_must_have_tool:
            if not matches:
                violations += 1

        # Check each tool call for format issues
        for match_str in matches:
            try:
                call_data = json.loads(match_str)
            except json.JSONDecodeError:
                if self._config.penalize_malformed_json:
                    violations += 1
                continue

            # Check valid action name
            action = call_data.get("name", call_data.get("tool", ""))
            if self._config.penalize_invalid_action and action not in VALID_ACTIONS:
                violations += 1

            # Check required arguments
            if self._config.penalize_missing_args and self._config.required_args:
                required = self._config.required_args.get(action, [])
                arguments = call_data.get("arguments", call_data.get("args", {}))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        violations += 1
                        continue
                for arg in required:
                    if arg not in arguments:
                        violations += 1

        # Return penalty (capped at one penalty per turn to avoid compounding)
        if violations > 0:
            return self._config.penalty_per_violation
        return 0.0
