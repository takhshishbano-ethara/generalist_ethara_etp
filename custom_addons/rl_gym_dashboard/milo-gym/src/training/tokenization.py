"""Multi-turn tokenization with response masking for RL training.

Converts Trajectory objects into padded tensors with:
- input_ids: full conversation tokens
- response_mask: 1 for assistant tokens (where loss applies), 0 elsewhere
- turn_spans: token-level boundaries for each assistant turn (for per-step advantages)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import torch

from src.core.schemas import Trajectory, Turn


@dataclass
class TurnSpan:
    """Token-level boundary for one assistant turn within a sequence."""

    turn_index: int
    start_token: int
    end_token: int

    @property
    def length(self) -> int:
        return self.end_token - self.start_token


@dataclass
class TokenizedTrajectory:
    """Tokenized multi-turn conversation ready for training."""

    input_ids: list[int]
    response_mask: list[int]
    turn_spans: list[TurnSpan]
    seq_length: int
    task_id: str


def turns_to_messages(turns: list[Turn]) -> list[dict]:
    """Convert Turn objects to OpenAI chat format for tokenizer.apply_chat_template."""
    messages = []
    for turn in turns:
        msg = {"role": turn.role, "content": turn.content}
        if turn.role == "tool" and turn.tool_call_id:
            msg["tool_call_id"] = turn.tool_call_id
        messages.append(msg)
    return messages


def tokenize_trajectory(
    tokenizer,
    trajectory: Trajectory,
    max_length: int = 131072,
    system_prompt: str | None = None,
) -> TokenizedTrajectory:
    """Tokenize a trajectory with per-turn response masking.

    Strategy: tokenize the full conversation, then determine assistant turn boundaries
    by incrementally tokenizing prefixes. This handles chat template formatting correctly.

    Args:
        tokenizer: HuggingFace tokenizer with apply_chat_template support.
        trajectory: Multi-turn trajectory to tokenize.
        max_length: Maximum sequence length (truncates from left if exceeded).
        system_prompt: Optional system prompt prepended to conversation.

    Returns:
        TokenizedTrajectory with input_ids, response_mask, and turn_spans.
    """
    turns = trajectory.turns
    if not turns:
        return TokenizedTrajectory(
            input_ids=[],
            response_mask=[],
            turn_spans=[],
            seq_length=0,
            task_id=trajectory.task_id,
        )

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(turns_to_messages(turns))

    full_ids = tokenizer.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=False
    )

    if len(full_ids) > max_length:
        full_ids = full_ids[-max_length:]

    seq_length = len(full_ids)
    response_mask = [0] * seq_length
    turn_spans: list[TurnSpan] = []

    # Incrementally tokenize to find assistant turn boundaries
    prefix_messages = []
    if system_prompt:
        prefix_messages.append({"role": "system", "content": system_prompt})

    assistant_turn_idx = 0
    for i, turn in enumerate(turns):
        prefix_before = list(prefix_messages)
        prefix_messages.append({"role": turn.role, "content": turn.content})

        if turn.role != "assistant":
            continue

        # Tokens up to (not including) this assistant turn
        if prefix_before:
            prefix_ids = tokenizer.apply_chat_template(
                prefix_before, tokenize=True, add_generation_prompt=True
            )
        else:
            prefix_ids = []

        # Tokens including this assistant turn
        prefix_with_turn_ids = tokenizer.apply_chat_template(
            list(prefix_messages), tokenize=True, add_generation_prompt=False
        )

        start_token = min(len(prefix_ids), seq_length)
        end_token = min(len(prefix_with_turn_ids), seq_length)

        # Account for truncation
        truncation_offset = len(full_ids) - seq_length if len(full_ids) > max_length else 0
        start_token = max(0, start_token - truncation_offset)
        end_token = max(0, end_token - truncation_offset)

        if start_token < end_token:
            for pos in range(start_token, end_token):
                response_mask[pos] = 1

            turn_spans.append(TurnSpan(
                turn_index=assistant_turn_idx,
                start_token=start_token,
                end_token=end_token,
            ))

        assistant_turn_idx += 1

    return TokenizedTrajectory(
        input_ids=full_ids,
        response_mask=response_mask,
        turn_spans=turn_spans,
        seq_length=seq_length,
        task_id=trajectory.task_id,
    )


def batch_tokenize_trajectories(
    tokenizer,
    trajectories: list[Trajectory],
    max_length: int = 131072,
    system_prompt: str | None = None,
    device: str = "cuda",
) -> dict[str, torch.Tensor | list]:
    """Tokenize and pad a batch of trajectories into training tensors.

    Returns dict with:
        input_ids: [batch_size, max_seq_len] padded token IDs
        attention_mask: [batch_size, max_seq_len] 1=real token, 0=padding
        response_mask: [batch_size, max_seq_len] 1=assistant token, 0=else
        turn_spans: list[list[TurnSpan]] per-trajectory turn boundaries
    """
    tokenized = [
        tokenize_trajectory(tokenizer, traj, max_length, system_prompt)
        for traj in trajectories
    ]

    tokenized = [t for t in tokenized if t.seq_length > 0]
    if not tokenized:
        empty = torch.zeros(0, 0, dtype=torch.long, device=device)
        return {
            "input_ids": empty,
            "attention_mask": empty,
            "response_mask": empty.float(),
            "turn_spans": [],
        }

    max_seq_len = max(t.seq_length for t in tokenized)
    batch_size = len(tokenized)
    pad_id = tokenizer.pad_token_id or 0

    input_ids = torch.full((batch_size, max_seq_len), pad_id, dtype=torch.long, device=device)
    attention_mask = torch.zeros(batch_size, max_seq_len, dtype=torch.long, device=device)
    response_mask = torch.zeros(batch_size, max_seq_len, dtype=torch.float32, device=device)
    all_turn_spans: list[list[TurnSpan]] = []

    for i, tok_traj in enumerate(tokenized):
        seq_len = tok_traj.seq_length
        # Left-pad: content goes to the right
        offset = max_seq_len - seq_len
        input_ids[i, offset:] = torch.tensor(tok_traj.input_ids, dtype=torch.long)
        attention_mask[i, offset:] = 1
        for pos, val in enumerate(tok_traj.response_mask):
            if val:
                response_mask[i, offset + pos] = 1.0

        shifted_spans = [
            TurnSpan(
                turn_index=s.turn_index,
                start_token=s.start_token + offset,
                end_token=s.end_token + offset,
            )
            for s in tok_traj.turn_spans
        ]
        all_turn_spans.append(shifted_spans)

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "response_mask": response_mask,
        "turn_spans": all_turn_spans,
    }
