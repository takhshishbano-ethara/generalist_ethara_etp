from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from . import anthropic_client
from . import deduplicator
from . import openrouter_client

_logger = logging.getLogger(__name__)


CATEGORIES = [
    "Readable Text",
    "Attribute Binding",
    "Spatial Relationships",
    "Subtle Emotions",
    "Precise Lighting",
    "Reflections & Glass",
    "Rare Animals",
    "Exact Counts",
    "Data Values",
    "Verifiable Facts",
    "Hands & Fine Motor",
    "Out-of-Distribution Pairings",
]

ARCHETYPES = [
    "Desk / study",
    "Family kitchen",
    "Hobby / maker bench",
    "Garage / vehicle",
    "Junk drawer / noticeboard",
    "Travel / transit / street",
    "Clinic / counter walls",
    "Commercial",
]


def _build_call_plan(target_n: int) -> list[anthropic_client.GenerationCall]:
    plan: list[anthropic_client.GenerationCall] = []
    for i in range(target_n):
        tier = "dense" if random.random() < 0.6 else "medium"
        archetype = random.choice(ARCHETYPES)
        cat_count = random.randint(3, 5)
        cats = random.sample(CATEGORIES, cat_count)
        seed = f"{int(time.time() * 1000)}-{i}-{uuid.uuid4().hex[:8]}"
        plan.append(anthropic_client.GenerationCall(
            tier=tier,
            archetype=archetype,
            categories=cats,
            seed=seed,
        ))
    return plan


def _build_config_and_worker(env, http_session):
    provider = env["ir.config_parameter"].sudo().get_param("lynceus.provider", "anthropic")
    if provider == "openrouter":
        config = openrouter_client.build_config_from_env(env)
        config.http_session = http_session
        return config, openrouter_client._generate_one_pure
    config = anthropic_client.build_config_from_env(env)
    config.http_session = http_session
    return config, anthropic_client._generate_one_pure


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

    Prompt = env["lynceus.prompt"].sudo()
    seed_prompt = anthropic_client.load_seed_prompt()

    known_hashes = _load_known_hashes(env)
    _logger.info(
        "Lynceus batch %s: loaded %d existing content hashes into local cache",
        batch.name, len(known_hashes),
    )

    http_session = requests.Session()
    try:
        config, worker_fn = _build_config_and_worker(env, http_session)
    except Exception:
        http_session.close()
        raise

    api_calls = 0
    dedup_rejected = 0
    generated = 0
    total_cost = 0.0
    errors: list[str] = []
    pending_creates: list[dict] = []
    pending_rejections: list[str] = []

    max_total_attempts = target_n * 3

    def flush_pending():
        nonlocal generated, pending_creates, pending_rejections
        if pending_creates:
            Prompt.create(pending_creates)
            generated += len(pending_creates)
            pending_creates = []
        if pending_rejections:
            for raw in pending_rejections:
                deduplicator.record_rejection(env, raw, batch_id=batch.id)
            pending_rejections = []
        batch.write({
            "generated_count": generated,
            "dedup_rejected": dedup_rejected,
            "api_calls": api_calls,
            "cost_usd": total_cost,
            "error_log": "\n".join(errors[-20:]) if errors else False,
        })
        if not getattr(threading.current_thread(), "testing", False):
            env.cr.commit()

    try:
        while (generated + len(pending_creates)) < target_n and api_calls < max_total_attempts:
            remaining = target_n - generated - len(pending_creates)
            wave_size = max(parallel_workers, int(remaining * 1.3))
            wave_size = min(wave_size, parallel_workers * 10)
            wave_size = min(wave_size, max_total_attempts - api_calls)
            if wave_size <= 0:
                break

            wave_plan = _build_call_plan(wave_size)
            _logger.info(
                "Lynceus batch %s: dispatching wave of %d calls (%d workers, %d/%d generated so far)",
                batch.name, wave_size, parallel_workers, generated, target_n,
            )

            ex = ThreadPoolExecutor(
                max_workers=parallel_workers,
                thread_name_prefix="lynceus-llm",
            )
            try:
                future_to_call = {
                    ex.submit(worker_fn, config, seed_prompt, c): c
                    for c in wave_plan
                }

                for fut in as_completed(future_to_call):
                    call = future_to_call[fut]
                    api_calls += 1

                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        err_msg = f"call {api_calls}: {type(exc).__name__}: {exc}"
                        _logger.warning("Lynceus batch %s: %s", batch.name, err_msg)
                        errors.append(err_msg)
                        continue

                    total_cost += result.cost_usd

                    raw_content = result.content
                    h = deduplicator.content_hash(raw_content)

                    if h in known_hashes:
                        dedup_rejected += 1
                        pending_rejections.append(raw_content)
                        continue

                    known_hashes.add(h)
                    pending_creates.append({
                        "content": raw_content,
                        "content_hash": h,
                        "batch_id": batch.id,
                        "tier": call.tier,
                        "archetype": call.archetype,
                        "categories": ", ".join(call.categories or []),
                        "seed": call.seed,
                    })

                    if len(pending_creates) >= bulk_chunk:
                        flush_pending()

                    if (generated + len(pending_creates)) >= target_n:
                        break
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
            "Lynceus batch %s reached attempt cap (%d) with only %d/%d prompts.",
            batch.name, api_calls, generated, target_n,
        )
