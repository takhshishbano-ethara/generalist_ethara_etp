from __future__ import annotations

from dataclasses import dataclass


MODEL_PRICING_USD_PER_1M = {
    "gemini-3.5-flash": {"input": 0.30, "output": 2.50},
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
