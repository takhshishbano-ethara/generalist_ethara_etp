"""Smoke test: verify Bedrock Claude connectivity for PRM + Teacher roles."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def test_prm_scorer():
    from src.core.config import PRMConfig
    from src.core.schemas import Turn
    from src.prm.bedrock_scorer import BedrockClaudeScorer

    config = PRMConfig(
        enabled=True,
        mode="bedrock",
        bedrock_model_arn=os.environ["BEDROCK_MODEL_ARN"],
        bedrock_region=os.environ.get("BEDROCK_REGION", "ap-south-1"),
        judge_votes=1,
        judge_max_concurrent=2,
        judge_timeout=60.0,
    )

    scorer = BedrockClaudeScorer(config)

    turns = [
        Turn(role="user", content="Fix the TypeError in utils.py line 42 where str is passed instead of int", timestamp=1.0),
        Turn(role="assistant", content="I'll check utils.py to understand the type mismatch.", timestamp=2.0),
    ]

    print("[PRM] Scoring a sample turn via Bedrock Claude...")
    score = await scorer.score_turn(turns, "Fix TypeError in utils.py")
    print(f"[PRM] Score: {score}")
    assert -1.0 <= score <= 1.0, f"Score out of range: {score}"
    print("[PRM] PASSED\n")
    await scorer.close()
    return score


async def test_teacher_generation():
    from src.prm.bedrock_scorer import BedrockTeacherClient

    teacher = BedrockTeacherClient(
        model_arn=os.environ["BEDROCK_MODEL_ARN"],
        region=os.environ.get("BEDROCK_REGION", "ap-south-1"),
        temperature=0.7,
        max_tokens=1024,
    )

    problem = (
        "The function `calculate_total` in `cart.py` raises a TypeError when "
        "the cart contains items with `None` price. Fix it to skip None-priced items."
    )

    print("[TEACHER] Generating a single solution via Bedrock Claude...")
    response = await teacher.generate(
        problem_statement=problem,
        system_prompt=(
            "You are a senior software engineer. Produce a fix as a unified diff patch "
            "wrapped in <submit>...</submit> tags."
        ),
    )
    print(f"[TEACHER] Response length: {len(response)} chars")
    print(f"[TEACHER] First 200 chars: {response[:200]}")
    assert len(response) > 0, "Empty response from teacher"
    print("[TEACHER] PASSED\n")
    await teacher.close()
    return response


async def main():
    print("=" * 60)
    print("SMOKE TEST: Bedrock Claude Integration")
    print(f"Model ARN: {os.environ.get('BEDROCK_MODEL_ARN', 'NOT SET')}")
    print(f"Region: {os.environ.get('BEDROCK_REGION', 'NOT SET')}")
    print(f"API Key: {os.environ.get('BEDROCK_API_KEY', 'NOT SET')[:8]}...")
    print("=" * 60 + "\n")

    try:
        score = await test_prm_scorer()
        response = await test_teacher_generation()

        print("=" * 60)
        print("ALL TESTS PASSED")
        print(f"  PRM score: {score}")
        print(f"  Teacher response: {len(response)} chars")
        print("=" * 60)
    except Exception as e:
        print(f"\n{'=' * 60}")
        print(f"FAILED: {type(e).__name__}: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
