from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import PRMConfig
from src.core.schemas import Turn
from src.prm.scorer import LLMJudgeScorer, TrainedPRMScorer


class TestParseBoxedScore:
    def test_boxed_positive(self):
        assert LLMJudgeScorer._parse_boxed_score("\\boxed{+1}") == 1.0

    def test_boxed_negative(self):
        assert LLMJudgeScorer._parse_boxed_score("\\boxed{-1}") == -1.0

    def test_boxed_zero(self):
        assert LLMJudgeScorer._parse_boxed_score("\\boxed{0}") == 0.0

    def test_boxed_with_surrounding_text(self):
        text = "The action is productive. \\boxed{+1}"
        assert LLMJudgeScorer._parse_boxed_score(text) == 1.0

    def test_fallback_plain_number(self):
        assert LLMJudgeScorer._parse_boxed_score("+1") == 1.0

    def test_fallback_negative_plain(self):
        assert LLMJudgeScorer._parse_boxed_score("-1") == -1.0

    def test_invalid_returns_none(self):
        assert LLMJudgeScorer._parse_boxed_score("great job!") is None

    def test_invalid_number_returns_none(self):
        assert LLMJudgeScorer._parse_boxed_score("\\boxed{5}") is None

    def test_empty_string(self):
        assert LLMJudgeScorer._parse_boxed_score("") is None


class TestFormatConversation:
    def test_truncates_to_max_turns(self):
        turns = [Turn(role="assistant", content=f"msg {i}") for i in range(20)]
        formatted = LLMJudgeScorer._format_conversation(turns, max_turns=5)
        assert "[ASSISTANT]: msg 15" in formatted
        assert "[ASSISTANT]: msg 0" not in formatted

    def test_truncates_long_content(self):
        long_content = "x" * 1000
        turns = [Turn(role="assistant", content=long_content)]
        formatted = LLMJudgeScorer._format_conversation(turns)
        assert "[...]" in formatted
        assert len(formatted) < 600


class TestLLMJudgeScorer:
    @pytest.fixture
    def config(self) -> PRMConfig:
        return PRMConfig(
            judge_endpoint="http://test:8001/v1/chat/completions",
            judge_model="test-model",
            judge_votes=3,
            judge_timeout=5.0,
            judge_max_concurrent=4,
        )

    @pytest.fixture
    def scorer(self, config: PRMConfig) -> LLMJudgeScorer:
        return LLMJudgeScorer(config)

    @pytest.fixture
    def sample_turns(self) -> list[Turn]:
        return [
            Turn(role="user", content="Fix the bug in utils.py"),
            Turn(role="assistant", content="Let me read the file first."),
            Turn(role="tool", content="def foo():\n    return 1"),
            Turn(role="assistant", content="I see the issue. Applying patch..."),
        ]

    def test_score_trajectory_skips_non_assistant(self, scorer):
        turns = [
            Turn(role="user", content="hello"),
            Turn(role="assistant", content="sure"),
            Turn(role="tool", content="output"),
            Turn(role="assistant", content="done"),
        ]

        async def _run():
            with patch.object(scorer, "score_turn", new_callable=AsyncMock) as mock_score:
                mock_score.return_value = 0.5
                scores = await scorer.score_trajectory(turns, "task")
            return scores, mock_score.call_count

        scores, call_count = asyncio.run(_run())

        assert len(scores) == 4
        assert scores[0] is None
        assert scores[1] == 0.5
        assert scores[2] is None
        assert scores[3] == 0.5
        assert call_count == 2

    def test_score_trajectory_empty(self, scorer):
        scores = asyncio.run(scorer.score_trajectory([], "task"))
        assert scores == []


class TestTrainedPRMScorer:
    def test_format_for_model_truncation(self):
        turns = [Turn(role="assistant", content="x" * 500) for _ in range(10)]
        text = TrainedPRMScorer._format_for_model(turns, "task desc", max_turns=6)
        assert text.count("<|assistant|>") == 6
        assert "task desc" in text

    def test_format_for_model_includes_task(self):
        turns = [Turn(role="assistant", content="hello")]
        text = TrainedPRMScorer._format_for_model(turns, "Fix the bug")
        assert "<|task|>" in text
        assert "Fix the bug" in text
