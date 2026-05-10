from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from statistics import median

import aiohttp

from src.core.config import PRMConfig
from src.core.schemas import Turn

log = logging.getLogger(__name__)


class PRMScorer(ABC):
    @abstractmethod
    async def score_turn(self, turns_so_far: list[Turn], task_description: str) -> float:
        ...

    async def score_trajectory(
        self, turns: list[Turn], task_description: str
    ) -> list[float | None]:
        """Score all turns in a trajectory.

        Non-assistant turns receive None (not scored).
        Assistant turns are scored concurrently (all score_turn calls fired together).

        Returns:
            List of scores (float for assistant turns, None for non-assistant),
            same length as turns.
        """
        assistant_indices: list[int] = [
            i for i, turn in enumerate(turns) if turn.role == "assistant"
        ]

        if not assistant_indices:
            return [None] * len(turns)

        coros = [
            self.score_turn(turns[: idx + 1], task_description)
            for idx in assistant_indices
        ]
        assistant_scores = await asyncio.gather(*coros)

        scores: list[float | None] = [None] * len(turns)
        for idx, score in zip(assistant_indices, assistant_scores):
            scores[idx] = score
        return scores

    async def score_trajectory_batch(
        self, trajectories: list[list[Turn]], task_descriptions: list[str]
    ) -> list[list[float | None]]:
        tasks = [
            self.score_trajectory(turns, desc)
            for turns, desc in zip(trajectories, task_descriptions)
        ]
        return await asyncio.gather(*tasks)


class LLMJudgeScorer(PRMScorer):
    """Scores via external LLM with majority vote. Parse \\boxed{+1/0/-1}."""

    PROMPT_TEMPLATE = (
        "You are evaluating a coding agent's action in a multi-turn debugging session.\n\n"
        "## Task\n{task_description}\n\n"
        "## Conversation (last {n_turns} turns)\n{conversation}\n\n"
        "## Instructions\n"
        "Rate the LAST assistant action on this scale:\n"
        "+1 = Clearly progresses toward solving the task\n"
        " 0 = Neutral\n"
        "-1 = Counterproductive\n\n"
        "Respond with ONLY \\boxed{{+1}}, \\boxed{{0}}, or \\boxed{{-1}}."
    )

    _BOXED_RE = re.compile(r"\\boxed\{([+-]?[01])\}")
    _FALLBACK_RE = re.compile(r"^\s*([+-]?[01])\s*$")

    def __init__(self, config: PRMConfig) -> None:
        self._endpoint = config.judge_endpoint
        self._model = config.judge_model
        self._votes = config.judge_votes
        self._timeout = config.judge_timeout
        self._max_concurrent = config.judge_max_concurrent
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop_id: int = 0
        self._session: aiohttp.ClientSession | None = None
        self._session_loop_id: int = 0

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if self._semaphore is None or self._semaphore_loop_id != loop_id:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
            self._semaphore_loop_id = loop_id
        return self._semaphore

    def _get_session(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if self._session is None or self._session.closed or self._session_loop_id != loop_id:
            self._session = aiohttp.ClientSession()
            self._session_loop_id = loop_id
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def score_turn(self, turns_so_far: list[Turn], task_description: str) -> float:
        conversation = self._format_conversation(turns_so_far)
        prompt = self.PROMPT_TEMPLATE.format(
            task_description=task_description[:1000],
            conversation=conversation,
            n_turns=min(len(turns_so_far), 10),
        )
        votes = await asyncio.gather(
            *[self._single_vote(prompt) for _ in range(self._votes)]
        )
        valid_votes = [v for v in votes if v is not None]
        if not valid_votes:
            return 0.0
        return median(valid_votes)

    async def _single_vote(self, prompt: str) -> float | None:
        async with self._get_semaphore():
            try:
                session = self._get_session()
                payload = {
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 16,
                }
                async with session.post(
                    self._endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status != 200:
                        log.warning("Judge returned status %d", resp.status)
                        return None
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    return self._parse_boxed_score(content)
            except asyncio.TimeoutError:
                log.warning("Judge vote timed out after %.1fs", self._timeout)
                return None
            except Exception as e:
                log.warning("Judge vote failed: %s", e)
                return None

    @classmethod
    def _parse_boxed_score(cls, text: str) -> float | None:
        match = cls._BOXED_RE.search(text)
        if match:
            return float(match.group(1))
        match = cls._FALLBACK_RE.match(text.strip())
        if match:
            val = int(match.group(1))
            if val in (-1, 0, 1):
                return float(val)
        return None

    @staticmethod
    def _format_conversation(turns: list[Turn], max_turns: int = 10) -> str:
        recent = turns[-max_turns:]
        lines: list[str] = []
        for t in recent:
            prefix = t.role.upper()
            content = t.content
            if len(content) > 500:
                content = content[:500] + " [...]"
            lines.append(f"[{prefix}]: {content}")
        return "\n\n".join(lines)


class TrainedPRMScorer(PRMScorer):
    """Scores steps using fine-tuned 1.5B model with regression head.
    Score is sigmoid-activated to [0, 1], then scaled to [-1, 1].
    """

    def __init__(self, config: PRMConfig) -> None:
        self._model_path = config.prm_model_path
        self._checkpoint = config.prm_checkpoint
        self._lora_rank = config.prm_lora_rank
        self._model = None
        self._tokenizer = None
        self._device = "cuda"
        self._batch_size = 8

    def load_model(self) -> None:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(self._model_path)
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token

        base_model = AutoModelForSequenceClassification.from_pretrained(
            self._model_path,
            num_labels=1,
            torch_dtype=torch.bfloat16,
        )

        if self._checkpoint:
            self._model = PeftModel.from_pretrained(base_model, self._checkpoint)
        else:
            self._model = base_model

        self._model.eval()
        if torch.cuda.is_available():
            self._model = self._model.to(self._device)

    async def score_turn(self, turns_so_far: list[Turn], task_description: str) -> float:
        if self._model is None:
            self.load_model()

        import torch

        text = self._format_for_model(turns_so_far, task_description)
        loop = asyncio.get_running_loop()
        score = await loop.run_in_executor(None, self._score_text_sync, text)
        return score

    def _score_text_sync(self, text: str) -> float:
        import torch

        inputs = self._tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=2048,
            padding=False,
        )
        inputs = {k: v.to(self._model.device) for k, v in inputs.items()}

        with torch.no_grad():
            logits = self._model(**inputs).logits

        raw_score = logits.squeeze(-1).item()
        prob = torch.sigmoid(torch.tensor(raw_score)).item()
        return (prob * 2.0) - 1.0

    def _score_batch_sync(self, texts: list[str]) -> list[float]:
        import torch

        inputs = self._tokenizer(
            texts,
            return_tensors="pt",
            truncation=True,
            max_length=2048,
            padding=True,
        )
        inputs = {k: v.to(self._model.device) for k, v in inputs.items()}

        with torch.no_grad():
            logits = self._model(**inputs).logits

        raw_scores = logits.squeeze(-1)
        probs = torch.sigmoid(raw_scores)
        return ((probs * 2.0) - 1.0).tolist()

    async def score_trajectory(
        self, turns: list[Turn], task_description: str
    ) -> list[float | None]:
        if self._model is None:
            self.load_model()

        assistant_indices: list[int] = [
            i for i, turn in enumerate(turns) if turn.role == "assistant"
        ]

        if not assistant_indices:
            return [None] * len(turns)

        texts = [
            self._format_for_model(turns[:idx + 1], task_description)
            for idx in assistant_indices
        ]

        loop = asyncio.get_running_loop()
        all_scores: list[float] = []
        for batch_start in range(0, len(texts), self._batch_size):
            batch = texts[batch_start:batch_start + self._batch_size]
            batch_scores = await loop.run_in_executor(None, self._score_batch_sync, batch)
            all_scores.extend(batch_scores)

        scores: list[float | None] = [None] * len(turns)
        for idx, score in zip(assistant_indices, all_scores):
            scores[idx] = score
        return scores

    @staticmethod
    def _format_for_model(turns: list[Turn], task_description: str, max_turns: int = 6) -> str:
        parts: list[str] = [f"<|task|>\n{task_description[:500]}"]
        for t in turns[-max_turns:]:
            content = t.content[:300]
            parts.append(f"<|{t.role}|>\n{content}")
        return "\n".join(parts)
