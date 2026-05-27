"""
T2AV enrichment runner: walk Tasks.csv, generate enriched_prompt per row via
AWS Bedrock (Converse API), gate every output through t2av_validator, and
split rows into:

  * enriched.csv  (validator-pass rows; warnings carried via review_required flag)
  * rejected.csv  (validator FATAL or LLM-call failure rows)

Architecture
------------
- Provider:      AWS Bedrock Converse API via aioboto3 (real async client)
- Concurrency:   asyncio.Semaphore (default 16)
- System prompt: extracted live from `T2AV Enrichment System Prompt.md`
                 (first fenced block) so there is exactly one source of truth.
- Validator:     imported from t2av_validator (same directory).
- Resume:        on startup, scan both output CSVs for IDs already processed;
                 skip them. Append-mode writes with row-level fsync after each
                 row so SIGINT/network failures lose at most one in-flight row.
- Retries:       exponential backoff on Bedrock throttle / 5xx / transient
                 ClientError (max 5 attempts, 2s ** n + jitter). Hard failure
                 routes to rejected.csv with reason="llm_error".

Outputs
-------
enriched.csv columns:
    ID, Category, Sub_Category, Style, Priority, Topic, Complexity,
    enriched_prompt, validator_status, validator_warnings, review_required

rejected.csv columns:
    ID, Category, Sub_Category, Style, Priority, Topic, Complexity,
    enriched_prompt, reject_reason, fatal_rules, fatal_evidence

Install
-------
    pip install aioboto3 botocore

Auth
----
Standard AWS credential resolution: env vars, ~/.aws/credentials, IAM role.
    export AWS_REGION=ap-south-1                    # or pass --region
    export AWS_PROFILE=...                          # optional

Run
---
    python3 run_enrichment.py \\
        --input  "Tasks.csv" \\
        --enriched enriched.csv \\
        --rejected rejected.csv \\
        --model-id anthropic.claude-3-5-sonnet-20241022-v2:0 \\
        --region ap-south-1 \\
        --concurrency 16
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from t2av_validator import Report, validate  # noqa: E402


DEFAULT_INPUT = _HERE / "Tasks.csv"
DEFAULT_SYSTEM_PROMPT_FILE = _HERE / "T2AV Enrichment System Prompt.md"
DEFAULT_ENRICHED = _HERE / "enriched.csv"
DEFAULT_REJECTED = _HERE / "rejected.csv"

DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"
DEFAULT_REGION = os.environ.get("AWS_REGION", "ap-south-1")
DEFAULT_CONCURRENCY = 16
DEFAULT_MAX_TOKENS = 600
DEFAULT_TEMPERATURE = 0.8
DEFAULT_TOP_P = 0.9
DEFAULT_MAX_ATTEMPTS = 5

ENRICHED_FIELDS = [
    "ID",
    "Category",
    "Sub_Category",
    "Style",
    "Priority",
    "Topic",
    "Complexity",
    "enriched_prompt",
    "validator_status",
    "validator_warnings",
    "review_required",
]

REJECTED_FIELDS = [
    "ID",
    "Category",
    "Sub_Category",
    "Style",
    "Priority",
    "Topic",
    "Complexity",
    "enriched_prompt",
    "reject_reason",
    "fatal_rules",
    "fatal_evidence",
]

REASON_VALIDATOR_FATAL = "validator_fatal"
REASON_LLM_ERROR = "llm_error"
REASON_EMPTY_INPUT = "empty_input"
REASON_NON_ENGLISH = "non_english"

LOG = logging.getLogger("t2av.enrich")


_FENCE_RE = re.compile(r"^```\s*$", re.MULTILINE)


def load_system_prompt(path: Path) -> str:
    """Return the verbatim system-prompt fenced block from the markdown file.

    The companion `T2AV Enrichment System Prompt.md` puts the canonical prompt
    inside the FIRST fenced ```...``` block. We slice between the first two
    fences so editing the markdown file is the single way to update the prompt.
    """
    text = path.read_text(encoding="utf-8")
    fences = list(_FENCE_RE.finditer(text))
    if len(fences) < 2:
        raise SystemExit(
            f"Could not locate fenced system prompt block in {path}. "
            f"Found {len(fences)} fences, need at least 2."
        )
    start = fences[0].end()
    end = fences[1].start()
    block = text[start:end].strip("\n")
    if not block:
        raise SystemExit(f"System prompt fenced block in {path} is empty.")
    return block


_USER_TURN_KEYS = [
    ("Category", "Category"),
    ("Sub_Category", "Sub_Category"),
    ("Style", "Style"),
    ("Priority", "Priority"),
    ("Topic", "Topic"),
    ("Complexity", "Complexity"),
    ("Prompt", "Prompt"),
]


def build_user_turn(row: dict[str, str]) -> str:
    lines = []
    for csv_key, label in _USER_TURN_KEYS:
        val = (row.get(csv_key) or "").strip()
        lines.append(f"{label}: {val}")
    return "\n".join(lines)


class BedrockClient:
    """Thin async wrapper over aioboto3 bedrock-runtime Converse.

    Held open across the whole run so the underlying connection pool can be
    reused. Use as `async with BedrockClient(...) as client: ...`.
    """

    def __init__(
        self,
        *,
        region: str,
        model_id: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> None:
        self.region = region
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self._session = None
        self._client_cm = None
        self._client = None

    async def __aenter__(self) -> "BedrockClient":
        try:
            import aioboto3
        except ImportError as exc:
            raise SystemExit(
                "aioboto3 is required. Install with: pip install aioboto3"
            ) from exc
        self._session = aioboto3.Session()
        self._client_cm = self._session.client(
            "bedrock-runtime", region_name=self.region
        )
        self._client = await self._client_cm.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client_cm is not None:
            await self._client_cm.__aexit__(exc_type, exc, tb)
        self._client = None
        self._client_cm = None

    async def converse(self, system_prompt: str, user_turn: str) -> str:
        """Single Converse call. Raises on hard error after retries handled in caller."""
        assert self._client is not None, "BedrockClient not entered"
        resp = await self._client.converse(
            modelId=self.model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_turn}]}],
            inferenceConfig={
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
                "topP": self.top_p,
            },
        )
        # Converse response shape: output.message.content[ {text: "..."} ]
        try:
            blocks = resp["output"]["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Bedrock response shape: {resp!r}") from exc
        text_parts = [b.get("text", "") for b in blocks if "text" in b]
        return "".join(text_parts).strip()


_RETRYABLE_ERROR_NAMES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelStreamErrorException",
    "RequestTimeout",
}


def _is_retryable(exc: BaseException) -> bool:
    # botocore ClientError carries .response['Error']['Code']; duck-typed
    # so we don't need a hard botocore import to classify retryability.
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
        if code in _RETRYABLE_ERROR_NAMES:
            return True
    name = exc.__class__.__name__
    if name in _RETRYABLE_ERROR_NAMES:
        return True
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError))


async def call_with_retry(
    client: BedrockClient,
    system_prompt: str,
    user_turn: str,
    *,
    max_attempts: int,
) -> str:
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await client.converse(system_prompt, user_turn)
        except BaseException as exc:  # noqa: BLE001 - we re-classify below
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            delay = min(30.0, (2 ** attempt) + random.random())
            LOG.warning(
                "bedrock retry %d/%d in %.1fs (%s: %s)",
                attempt, max_attempts, delay, exc.__class__.__name__, exc,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


@dataclass
class ResumeState:
    completed_ids: set[str]
    enriched_exists: bool
    rejected_exists: bool


def scan_completed_ids(enriched_path: Path, rejected_path: Path) -> ResumeState:
    """Return the union of IDs already present in either output CSV.

    Tolerates missing files and files with header-only contents.
    """
    completed: set[str] = set()
    enriched_exists = enriched_path.exists()
    rejected_exists = rejected_path.exists()

    for path in (enriched_path, rejected_path):
        if not path.exists():
            continue
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    rid = (row.get("ID") or "").strip()
                    if rid:
                        completed.add(rid)
        except (OSError, csv.Error) as exc:
            LOG.warning("could not read %s for resume: %s", path, exc)

    return ResumeState(
        completed_ids=completed,
        enriched_exists=enriched_exists,
        rejected_exists=rejected_exists,
    )


class AppendCsvWriter:
    """Append-mode CSV writer with row-level fsync.

    fsync after each row so a SIGKILL between rows loses at most the in-flight
    row, never a previously-written one. Concurrency-safe under asyncio because
    callers serialize through an asyncio.Lock.
    """

    def __init__(self, path: Path, fieldnames: list[str], *, write_header: bool) -> None:
        self.path = path
        self.fieldnames = fieldnames
        self._lock = asyncio.Lock()
        self._fh = path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=fieldnames)
        if write_header:
            self._writer.writeheader()
            self._fh.flush()
            os.fsync(self._fh.fileno())

    async def write(self, row: dict[str, Any]) -> None:
        clean = {k: ("" if row.get(k) is None else str(row.get(k))) for k in self.fieldnames}
        async with self._lock:
            self._writer.writerow(clean)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        self._fh.close()


@dataclass
class Stats:
    total: int = 0
    skipped_resume: int = 0
    skipped_empty: int = 0
    skipped_non_english: int = 0
    enriched_clean: int = 0
    enriched_warned: int = 0
    rejected_validator: int = 0
    rejected_llm_error: int = 0
    started_at: float = 0.0

    def banner(self) -> str:
        elapsed = max(1e-3, time.monotonic() - self.started_at)
        rate = (self.enriched_clean + self.enriched_warned + self.rejected_validator) / elapsed
        return (
            f"total={self.total} resume_skip={self.skipped_resume} "
            f"empty={self.skipped_empty} non_en={self.skipped_non_english} "
            f"clean={self.enriched_clean} warned={self.enriched_warned} "
            f"rej_validator={self.rejected_validator} rej_llm={self.rejected_llm_error} "
            f"rate={rate:.2f}/s elapsed={elapsed:.0f}s"
        )


def _meta_row(row: dict[str, str]) -> dict[str, str]:
    return {k: row.get(k, "") for k in (
        "ID", "Category", "Sub_Category", "Style", "Priority", "Topic", "Complexity"
    )}


async def process_row(
    row: dict[str, str],
    *,
    client: BedrockClient,
    system_prompt: str,
    enriched_writer: AppendCsvWriter,
    rejected_writer: AppendCsvWriter,
    sem: asyncio.Semaphore,
    stats: Stats,
    max_attempts: int,
) -> None:
    rid = (row.get("ID") or "").strip()
    style = (row.get("Style") or "").strip().lower() or None
    category = (row.get("Category") or "").strip() or None
    src_prompt = (row.get("Prompt") or "").strip()

    if not src_prompt:
        stats.skipped_empty += 1
        await rejected_writer.write({
            **_meta_row(row),
            "enriched_prompt": "",
            "reject_reason": REASON_EMPTY_INPUT,
            "fatal_rules": "",
            "fatal_evidence": "",
        })
        return

    lang = (row.get("Language") or row.get("Prompt_Language") or "").strip().lower()
    if lang and lang not in {"english", "en", "en-us", "en-gb"}:
        stats.skipped_non_english += 1
        await rejected_writer.write({
            **_meta_row(row),
            "enriched_prompt": "",
            "reject_reason": REASON_NON_ENGLISH,
            "fatal_rules": "",
            "fatal_evidence": f"language={lang}",
        })
        return

    user_turn = build_user_turn(row)

    async with sem:
        try:
            enriched = await call_with_retry(
                client, system_prompt, user_turn, max_attempts=max_attempts,
            )
        except BaseException as exc:  # noqa: BLE001
            stats.rejected_llm_error += 1
            LOG.error("llm error on ID=%s: %s: %s", rid, exc.__class__.__name__, exc)
            await rejected_writer.write({
                **_meta_row(row),
                "enriched_prompt": "",
                "reject_reason": REASON_LLM_ERROR,
                "fatal_rules": exc.__class__.__name__,
                "fatal_evidence": str(exc)[:500],
            })
            return

    # Validator gate. Run off-thread so a heavy regex pass cannot stall the
    # event loop when concurrency is high.
    report: Report = await asyncio.to_thread(
        validate, enriched, style=style, category=category,
    )

    if not report.passed:
        stats.rejected_validator += 1
        rules = ";".join(f.rule for f in report.fatal)
        evidence = "||".join((f.evidence or f.message)[:200] for f in report.fatal)
        await rejected_writer.write({
            **_meta_row(row),
            "enriched_prompt": enriched,
            "reject_reason": REASON_VALIDATOR_FATAL,
            "fatal_rules": rules,
            "fatal_evidence": evidence,
        })
        return

    warnings = report.warnings
    if warnings:
        stats.enriched_warned += 1
        status = "warned"
        warn_codes = ";".join(f.rule for f in warnings)
        review_required = "true"
    else:
        stats.enriched_clean += 1
        status = "clean"
        warn_codes = ""
        review_required = "false"

    await enriched_writer.write({
        **_meta_row(row),
        "enriched_prompt": enriched,
        "validator_status": status,
        "validator_warnings": warn_codes,
        "review_required": review_required,
    })


async def run(args: argparse.Namespace) -> int:
    system_prompt = load_system_prompt(Path(args.system_prompt))

    input_path = Path(args.input)
    enriched_path = Path(args.enriched)
    rejected_path = Path(args.rejected)

    if not input_path.exists():
        raise SystemExit(f"Input CSV not found: {input_path}")

    resume = scan_completed_ids(enriched_path, rejected_path)
    LOG.info(
        "resume: %d IDs already in outputs; enriched_exists=%s rejected_exists=%s",
        len(resume.completed_ids), resume.enriched_exists, resume.rejected_exists,
    )

    enriched_writer = AppendCsvWriter(
        enriched_path, ENRICHED_FIELDS, write_header=not resume.enriched_exists,
    )
    rejected_writer = AppendCsvWriter(
        rejected_path, REJECTED_FIELDS, write_header=not resume.rejected_exists,
    )

    stats = Stats(started_at=time.monotonic())
    sem = asyncio.Semaphore(args.concurrency)
    stop_event = asyncio.Event()

    def _on_signal(signum, _frame) -> None:  # pragma: no cover - signal path
        LOG.warning("signal %s received; draining in-flight tasks", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log_every = max(50, args.concurrency * 4)

    try:
        async with BedrockClient(
            region=args.region,
            model_id=args.model_id,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
        ) as client:
            tasks: list[asyncio.Task] = []
            with input_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None or "ID" not in reader.fieldnames:
                    raise SystemExit(
                        f"Input CSV {input_path} missing 'ID' header. "
                        f"Headers: {reader.fieldnames}"
                    )
                for row in reader:
                    if stop_event.is_set():
                        break
                    stats.total += 1
                    rid = (row.get("ID") or "").strip()
                    if not rid:
                        continue
                    if rid in resume.completed_ids:
                        stats.skipped_resume += 1
                        continue
                    if args.limit and (
                        stats.total - stats.skipped_resume
                    ) > args.limit:
                        break

                    task = asyncio.create_task(
                        process_row(
                            row,
                            client=client,
                            system_prompt=system_prompt,
                            enriched_writer=enriched_writer,
                            rejected_writer=rejected_writer,
                            sem=sem,
                            stats=stats,
                            max_attempts=args.max_attempts,
                        )
                    )
                    tasks.append(task)

                    # Drain finished tasks periodically so memory stays bounded
                    # and progress logs stay live for a 13K-row run.
                    if len(tasks) >= log_every:
                        done, pending = await asyncio.wait(
                            tasks, return_when=asyncio.FIRST_COMPLETED,
                        )
                        for d in done:
                            exc = d.exception()
                            if exc is not None:
                                LOG.error("task crashed: %s", exc)
                        tasks = list(pending)
                        LOG.info(stats.banner())

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        enriched_writer.close()
        rejected_writer.close()

    LOG.info("done. %s", stats.banner())
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_enrichment.py",
        description="Walk Tasks.csv, enrich every prompt via Bedrock, gate "
                    "through t2av_validator, split into enriched.csv + rejected.csv.",
    )
    p.add_argument("--input", default=str(DEFAULT_INPUT), help="Source CSV (default: Tasks.csv).")
    p.add_argument("--enriched", default=str(DEFAULT_ENRICHED), help="Output CSV for validator-pass rows.")
    p.add_argument("--rejected", default=str(DEFAULT_REJECTED), help="Output CSV for FATAL/error rows.")
    p.add_argument("--system-prompt", default=str(DEFAULT_SYSTEM_PROMPT_FILE),
                   help="Markdown file whose first fenced block is the system prompt.")
    p.add_argument("--model-id", default=DEFAULT_MODEL_ID,
                   help="Bedrock modelId or inference-profile ARN.")
    p.add_argument("--region", default=DEFAULT_REGION, help="AWS region.")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help="Max in-flight Bedrock calls.")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    p.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                   help="Retry attempts per row on transient errors.")
    p.add_argument("--limit", type=int, default=0,
                   help="Process at most N new rows (0 = all). Useful for smoke tests.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
