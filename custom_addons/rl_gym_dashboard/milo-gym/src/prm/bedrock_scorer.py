"""Bedrock Claude scorer — uses AWS Bedrock Converse API with Bearer token auth for PRM step scoring."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from statistics import median
from urllib.parse import quote

import aiohttp

from src.core.config import PRMConfig
from src.core.schemas import Turn
from src.prm.scorer import PRMScorer

log = logging.getLogger(__name__)


def _build_converse_url(region: str, model_arn: str) -> str:
    """Build the Bedrock Converse API endpoint URL."""
    encoded_arn = quote(model_arn, safe="")
    return f"https://bedrock-runtime.{region}.amazonaws.com/model/{encoded_arn}/converse"


class BedrockClaudeScorer(PRMScorer):
    """Scores turns via Claude on AWS Bedrock using the Converse API with Bearer auth.

    Uses majority vote (configurable) and parses \\boxed{+1/0/-1} format,
    same as LLMJudgeScorer but via Bedrock REST API with ABSK Bearer token.
    """

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
        self._model_arn = config.bedrock_model_arn or os.environ.get(
            "BEDROCK_MODEL_ARN", ""
        )
        self._region = config.bedrock_region or os.environ.get(
            "BEDROCK_REGION", "ap-south-1"
        )
        self._api_key = os.environ.get("BEDROCK_API_KEY", "")
        self._votes = config.judge_votes
        self._max_concurrent = config.judge_max_concurrent
        self._timeout = config.judge_timeout
        self._url = _build_converse_url(self._region, self._model_arn)
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop_id: int = 0
        self._session: aiohttp.ClientSession | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if self._semaphore is None or self._semaphore_loop_id != loop_id:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
            self._semaphore_loop_id = loop_id
        return self._semaphore

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self._session

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
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    session = await self._get_session()
                    payload = {
                        "messages": [
                            {
                                "role": "user",
                                "content": [{"text": prompt}],
                            }
                        ],
                        "inferenceConfig": {
                            "temperature": 0.3,
                            "maxTokens": 32,
                        },
                    }
                    async with session.post(self._url, json=payload) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            wait = 2 ** attempt
                            log.warning("Bedrock HTTP %d, retry %d/%d in %ds",
                                        resp.status, attempt + 1, max_retries, wait)
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            body = await resp.text()
                            log.warning("Bedrock vote HTTP %d: %s", resp.status, body[:200])
                            return None
                        data = await resp.json()
                        content = data["output"]["message"]["content"][0]["text"]
                        return self._parse_boxed_score(content)
                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    log.warning("Bedrock vote failed after %d retries: %s", max_retries, e)
                    return None
                except Exception as e:
                    log.warning("Bedrock vote failed: %s", e)
                    return None
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

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()


class BedrockTeacherClient:
    """Standalone Bedrock Claude client for RFT trajectory generation (Stage 1).

    Not a PRMScorer — used directly by rft_warmup.py to generate code responses.
    Uses Bearer token auth with the ABSK API key.
    """

    def __init__(
        self,
        model_arn: str | None = None,
        region: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 32768,
    ):
        self._model_arn = model_arn or os.environ.get("BEDROCK_MODEL_ARN", "")
        self._region = region or os.environ.get("BEDROCK_REGION", "ap-south-1")
        self._api_key = os.environ.get("BEDROCK_API_KEY", "")
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._url = _build_converse_url(self._region, self._model_arn)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                timeout=aiohttp.ClientTimeout(total=120),
            )
        return self._session

    async def generate(self, problem_statement: str, system_prompt: str = "") -> str:
        """Generate a single code solution for a task via Claude."""
        session = await self._get_session()

        payload: dict = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": problem_statement}],
                }
            ],
            "inferenceConfig": {
                "temperature": self._temperature,
                "maxTokens": self._max_tokens,
            },
        }
        if system_prompt:
            payload["system"] = [{"text": system_prompt}]

        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with session.post(self._url, json=payload) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        wait = 2 ** attempt
                        log.warning("Bedrock teacher HTTP %d, retry %d/%d in %ds",
                                    resp.status, attempt + 1, max_retries, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("Bedrock teacher HTTP %d: %s", resp.status, body[:300])
                        return ""
                    data = await resp.json()
                    return data["output"]["message"]["content"][0]["text"]
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                log.error("Bedrock teacher generation failed after %d retries: %s", max_retries, e)
                return ""
            except Exception as e:
                log.error("Bedrock teacher generation failed: %s", e)
                return ""
        return ""

    async def generate_batch(
        self,
        problem_statement: str,
        n: int = 16,
        system_prompt: str = "",
        max_concurrent: int = 8,
    ) -> list[str]:
        """Generate N solutions for a task concurrently."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _one():
            async with semaphore:
                return await self.generate(problem_statement, system_prompt)

        results = await asyncio.gather(*[_one() for _ in range(n)])
        return [r for r in results if r]

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
