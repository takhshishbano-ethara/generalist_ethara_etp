from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from odoo import fields

from . import deduplicator
from . import vertex_client

_logger = logging.getLogger(__name__)


def _plan_batched_calls(
    n_calls: int,
    batch_call_size: int,
) -> list[vertex_client.BatchedGenerationCall]:
    plan: list[vertex_client.BatchedGenerationCall] = []
    for i in range(n_calls):
        seed = f"{int(time.time() * 1000)}-{i}-{uuid.uuid4().hex[:8]}"
        plan.append(
            vertex_client.BatchedGenerationCall(count=batch_call_size, seed=seed)
        )
    return plan


def _load_known_hashes(env) -> set[str]:
    env.cr.execute("SELECT content_hash FROM lynceus_history")
    return {row[0] for row in env.cr.fetchall() if row[0]}


def run(env, batch) -> None:
    target_n = batch.target_n
    if target_n <= 0:
        return

    ICP = env["ir.config_parameter"].sudo()
    try:
        parallel_workers = max(1, int(ICP.get_param("lynceus.parallel_calls", "10") or "10"))
    except (TypeError, ValueError):
        parallel_workers = 10
    try:
        bulk_chunk = max(1, int(ICP.get_param("lynceus.bulk_insert_chunk", "50") or "50"))
    except (TypeError, ValueError):
        bulk_chunk = 50
    try:
        batch_call_size = max(
            1,
            int(
                ICP.get_param(
                    "lynceus.batch_call_size",
                    str(vertex_client.DEFAULT_BATCH_CALL_SIZE),
                )
                or vertex_client.DEFAULT_BATCH_CALL_SIZE
            ),
        )
    except (TypeError, ValueError):
        batch_call_size = vertex_client.DEFAULT_BATCH_CALL_SIZE

    Prompt = env["lynceus.prompt"].sudo()
    LLMCall = env["lynceus.llm.call"].sudo()
    seed_prompt = vertex_client.load_seed_prompt()

    known_hashes = _load_known_hashes(env)
    _logger.info(
        "Lynceus batch %s: loaded %d existing content hashes; batch_call_size=%d, target=%d",
        batch.name, len(known_hashes), batch_call_size, target_n,
    )

    http_session = requests.Session()
    try:
        config = vertex_client.build_config_from_env(env)
    except Exception:
        http_session.close()
        raise
    config.http_session = http_session
    config.batch_call_size = batch_call_size

    api_calls = batch.api_calls or 0
    dedup_rejected = batch.dedup_rejected or 0
    generated = batch.generated_count or 0
    total_cost = batch.cost_usd or 0.0
    errors: list[str] = []
    parse_notes: list[str] = []
    pending_creates: list[dict] = []
    pending_rejections: list[str] = []
    pending_llm_calls: list[dict] = []

    max_total_batch_calls = max(1, math.ceil(target_n / batch_call_size) * 3)

    def flush_pending():
        nonlocal generated, pending_creates, pending_rejections, pending_llm_calls
        if pending_creates:
            Prompt.create(pending_creates)
            generated += len(pending_creates)
            pending_creates = []
        if pending_rejections:
            for raw in pending_rejections:
                deduplicator.record_rejection(env, raw, batch_id=batch.id)
            pending_rejections = []
        if pending_llm_calls:
            LLMCall.create(pending_llm_calls)
            pending_llm_calls = []
        log_lines = (errors + parse_notes)[-30:]
        batch.write({
            "generated_count": generated,
            "dedup_rejected": dedup_rejected,
            "api_calls": api_calls,
            "cost_usd": total_cost,
            "error_log": "\n".join(log_lines) if log_lines else False,
            "last_heartbeat_at": fields.Datetime.now(),
        })
        if not getattr(threading.current_thread(), "testing", False):
            env.cr.commit()

    try:
        while (
            (generated + len(pending_creates)) < target_n
            and api_calls < max_total_batch_calls
        ):
            remaining = target_n - generated - len(pending_creates)
            # No oversample multiplier here: dedup shortfall is recovered by the outer while loop dispatching another wave, not by pre-inflating each wave.
            wave_call_count = math.ceil(remaining / batch_call_size)
            wave_call_count = min(wave_call_count, parallel_workers * 5)
            wave_call_count = min(wave_call_count, max_total_batch_calls - api_calls)
            if wave_call_count <= 0:
                break

            wave_plan = _plan_batched_calls(wave_call_count, batch_call_size)
            _logger.info(
                "Lynceus batch %s: dispatching wave of %d calls x %d prompts each "
                "(%d workers, %d/%d generated)",
                batch.name, len(wave_plan), batch_call_size,
                parallel_workers, generated, target_n,
            )

            ex = ThreadPoolExecutor(
                max_workers=parallel_workers,
                thread_name_prefix="lynceus-llm",
            )
            try:
                future_to_call = {
                    ex.submit(
                        vertex_client._generate_batch_pure,
                        config, seed_prompt, c,
                    ): c
                    for c in wave_plan
                }
                stop_after_wave = False
                for fut in as_completed(future_to_call):
                    call = future_to_call[fut]
                    api_calls += 1

                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        err_msg = f"call {api_calls}: {type(exc).__name__}: {exc}".replace("\x00", "")
                        _logger.warning("Lynceus batch %s: %s", batch.name, err_msg)
                        errors.append(err_msg)
                        pending_llm_calls.append({
                            "batch_id": batch.id,
                            "sequence": api_calls,
                            "model": config.model,
                            "seed": call.seed,
                            "requested_count": call.count,
                            "returned_count": 0,
                            "finish_reason": "EXCEPTION",
                            "parse_errors": err_msg,
                        })
                        continue

                    total_cost += result.cost_usd
                    if result.parse_errors:
                        for pe in result.parse_errors:
                            parse_notes.append(f"call {api_calls}: {pe}".replace("\x00", ""))

                    raw_response_text = (
                        json.dumps(result.raw_response, indent=2, default=str, ensure_ascii=False).replace("\x00", "")
                        if result.raw_response else False
                    )
                    parse_errors_text = (
                        "\n".join(result.parse_errors).replace("\x00", "")
                        if result.parse_errors else False
                    )
                    pending_llm_calls.append({
                        "batch_id": batch.id,
                        "sequence": api_calls,
                        "model": result.model,
                        "seed": call.seed,
                        "requested_count": result.requested_count,
                        "returned_count": len(result.prompts),
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.candidate_tokens,
                        "thoughts_tokens": result.thoughts_tokens,
                        "cost_usd": result.cost_usd,
                        "finish_reason": result.finish_reason or "",
                        "parse_errors": parse_errors_text,
                        "raw_response": raw_response_text,
                    })

                    intra_batch_hashes: set[str] = set()
                    for idx, raw_content in enumerate(result.prompts):
                        if (generated + len(pending_creates)) >= target_n:
                            stop_after_wave = True
                            break
                        h = deduplicator.content_hash(raw_content)
                        if h in intra_batch_hashes or h in known_hashes:
                            dedup_rejected += 1
                            pending_rejections.append(raw_content)
                            continue
                        intra_batch_hashes.add(h)
                        known_hashes.add(h)
                        pending_creates.append({
                            "content": raw_content,
                            "content_hash": h,
                            "batch_id": batch.id,
                            "seed": f"{call.seed}-{idx}",
                        })
                        if len(pending_creates) >= bulk_chunk:
                            flush_pending()

                    if stop_after_wave:
                        continue
            finally:
                ex.shutdown(wait=True, cancel_futures=True)

            flush_pending()
    finally:
        if pending_creates or pending_rejections:
            try:
                flush_pending()
            except Exception:
                _logger.exception("Lynceus batch %s: final flush failed", batch.name)
        http_session.close()

    if generated < target_n:
        _logger.warning(
            "Lynceus batch %s reached attempt cap (%d calls) with only %d/%d prompts.",
            batch.name, api_calls, generated, target_n,
        )
