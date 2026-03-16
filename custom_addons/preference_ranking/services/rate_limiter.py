# -*- coding: utf-8 -*-
"""
Per-provider rate limiting for LLM API calls.

Uses threading.Semaphore to cap the number of concurrent calls to each
API provider, preventing 429 / rate-limit errors when running many
consumer workers in parallel.

Usage inside eval code:
    from ..services.rate_limiter import api_semaphore
    with api_semaphore('kimi'):
        result = call_kimi_api(...)

Environment variables (all optional, sensible defaults provided):
    KIMI_MAX_CONCURRENT    (default 20)
    OPENAI_MAX_CONCURRENT  (default 10)
    GEMINI_MAX_CONCURRENT  (default 10)
    GENAI_MAX_CONCURRENT   (default 10)
"""
import logging
import os
import threading
import time
from contextlib import contextmanager
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)

_LIMITS = {
    'kimi': int(os.getenv('KIMI_MAX_CONCURRENT', '20')),
    'openai': int(os.getenv('OPENAI_MAX_CONCURRENT', '10')),
    'gemini': int(os.getenv('GEMINI_MAX_CONCURRENT', '10')),
    'genai': int(os.getenv('GENAI_MAX_CONCURRENT', '10')),
}

_semaphores = {name: threading.Semaphore(limit) for name, limit in _LIMITS.items()}

_logger.info('Rate limiters initialised: %s', {k: v for k, v in _LIMITS.items()})


@contextmanager
def api_semaphore(provider):
    """Context manager that blocks until a slot is available for *provider*.

    Args:
        provider: One of 'kimi', 'openai', 'gemini', 'genai'.
    """
    sem = _semaphores.get(provider)
    if sem is None:
        yield
        return
    acquired = sem.acquire(timeout=1800)
    if not acquired:
        raise TimeoutError(f'Rate limiter timeout waiting for {provider} slot (1800s)')
    try:
        yield
    finally:
        sem.release()


def rate_limited(provider):
    """Decorator that wraps a function call with the rate limiter for *provider*."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with api_semaphore(provider):
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def with_retry(fn, max_retries=3, backoff_base=2.0, provider=None):
    """Call *fn* with exponential backoff on failure.

    If *provider* is given, the call is also rate-limited.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            if provider:
                with api_semaphore(provider):
                    return fn()
            else:
                return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                wait = backoff_base ** attempt
                _logger.warning(
                    '%s call failed (attempt %d/%d), retrying in %.1fs: %s',
                    provider or 'unknown', attempt, max_retries, wait, e,
                )
                time.sleep(wait)
    raise last_exc
