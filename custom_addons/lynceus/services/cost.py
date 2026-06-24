from __future__ import annotations

from dataclasses import dataclass


MODEL_PRICING_USD_PER_1M = {
    "claude-sonnet-4-6": {"input": 3.40, "output": 17.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-opus-4-7": {"input": 17.00, "output": 85.00},
    "google/gemini-3.5-flash": {"input": 0.30, "output": 2.50},
    "google/gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "google/gemini-2.0-flash-001": {"input": 0.10, "output": 0.40},
    "google/gemini-flash-1.5": {"input": 0.075, "output": 0.30},
    "anthropic/claude-3.5-sonnet": {"input": 3.00, "output": 15.00},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
}


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


def estimate_usd(model: str, usage: TokenUsage) -> float:
    pricing = MODEL_PRICING_USD_PER_1M.get(model)
    if not pricing:
        return 0.0
    return (
        usage.input_tokens * pricing["input"] / 1_000_000
        + usage.output_tokens * pricing["output"] / 1_000_000
    )
