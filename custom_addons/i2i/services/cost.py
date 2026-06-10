from __future__ import annotations


def estimate_cost_usd(tokens: int, usd_per_mtoken: float) -> float:
    if not tokens or tokens <= 0:
        return 0.0
    return (float(tokens) / 1_000_000.0) * float(usd_per_mtoken)
