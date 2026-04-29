"""
GitHub Token Pool with round-robin rotation and per-token rate limit tracking.

Thread-safe singleton that distributes GitHub API requests across multiple
Personal Access Tokens, automatically rotating when a token's rate limit
approaches zero and sleeping when ALL tokens are exhausted.
"""
import logging
import threading
import time

_logger = logging.getLogger(__name__)

_pool_lock = threading.Lock()
_pool_instance = None


class GitHubTokenPool:
    """Round-robin token rotation with per-token rate limit tracking."""

    def __init__(self, tokens):
        self._tokens = list(tokens)
        self._index = 0
        self._lock = threading.Lock()
        # Per-token rate limit state
        self._limits = {
            t: {"remaining": 5000, "reset_at": 0.0}
            for t in self._tokens
        }

    def get_token(self):
        """Get the next available token via round-robin.

        If the current token has fewer than 100 remaining requests,
        rotates to the next one. If ALL tokens are exhausted, sleeps
        until the earliest reset time.
        """
        with self._lock:
            # Try each token in round-robin order
            for _ in range(len(self._tokens)):
                token = self._tokens[self._index]
                self._index = (self._index + 1) % len(self._tokens)
                limit = self._limits[token]
                if limit["remaining"] > 100:
                    return token

            # All tokens exhausted -- find earliest reset
            earliest_reset = min(
                info["reset_at"] for info in self._limits.values()
            )
            wait = max(earliest_reset - time.time(), 0) + 5
            _logger.warning(
                "All %d GitHub tokens exhausted. Sleeping %.0fs until reset...",
                len(self._tokens), wait,
            )

        # Sleep outside the lock so other threads aren't blocked
        time.sleep(wait)

        # After sleeping, reset counters and return first token
        with self._lock:
            for info in self._limits.values():
                if info["reset_at"] <= time.time():
                    info["remaining"] = 5000
            token = self._tokens[self._index]
            self._index = (self._index + 1) % len(self._tokens)
            return token

    def report_usage(self, token, remaining, reset_at):
        """Update rate limit state for a token after an API call.

        Called by vendored tools after each PyGithub request to keep
        the pool's tracking accurate.

        Args:
            token: The GitHub PAT string.
            remaining: Remaining API calls (from X-RateLimit-Remaining header).
            reset_at: Unix timestamp when the rate limit resets.
        """
        with self._lock:
            if token in self._limits:
                self._limits[token]["remaining"] = remaining
                self._limits[token]["reset_at"] = reset_at

    def wait_if_needed(self, token):
        """Block if the given token's rate limit is near zero.

        Returns when the token has available capacity (either because
        the rate limit reset, or because we rotated to a different token).
        """
        with self._lock:
            limit = self._limits.get(token, {})
            if limit.get("remaining", 5000) > 100:
                return  # Plenty of capacity
            reset_at = limit.get("reset_at", 0)

        wait = max(reset_at - time.time(), 0) + 5
        if wait > 0:
            _logger.info(
                "Token ...%s near rate limit (%d remaining). Sleeping %.0fs...",
                token[-4:], limit.get("remaining", 0), wait,
            )
            time.sleep(wait)

    def get_github_client(self, per_page=100):
        from github import Auth, Github
        token = self.get_token()
        g = Github(auth=Auth.Token(token), per_page=per_page)
        return g, token

    def report_from_client(self, g, token):
        try:
            rate = g.get_rate_limit()
            self.report_usage(
                token,
                rate.core.remaining,
                rate.core.reset.timestamp(),
            )
        except Exception:
            pass

    @property
    def token_count(self):
        """Return the number of tokens in the pool."""
        return len(self._tokens)

    def get_status(self):
        """Return a dict of per-token status for monitoring.

        Returns dict mapping token suffix (last 4 chars) to remaining/reset info.
        """
        with self._lock:
            return {
                f"...{t[-4:]}": {
                    "remaining": info["remaining"],
                    "reset_at": info["reset_at"],
                }
                for t, info in self._limits.items()
            }


def get_token_pool(env):
    """Get or create the singleton token pool.

    Priority:
    1. If jaeger.github.token has active records → decrypt and use those
    2. Else → fall back to legacy jaeger.github_tokens comma-separated config param

    The singleton is created once per process and shared across all threads.
    """
    global _pool_instance
    with _pool_lock:
        if _pool_instance is not None:
            return _pool_instance

    from odoo.exceptions import UserError

    tokens = _load_tokens_from_model(env)

    if not tokens:
        tokens = _load_tokens_from_config(env)

    if not tokens:
        raise UserError(
            "No GitHub tokens configured. Go to Settings -> Jaeger -> GitHub Tokens.",
        )

    with _pool_lock:
        if _pool_instance is None:
            _pool_instance = GitHubTokenPool(tokens)
            _logger.info("GitHub token pool initialized with %d tokens", len(tokens))
        return _pool_instance


def _load_tokens_from_model(env):
    try:
        TokenModel = env["jaeger.github.token"].sudo()
        active_tokens = TokenModel.search([("state", "=", "active")])
        if not active_tokens:
            return []
        tokens = []
        for rec in active_tokens:
            raw = rec._decrypt_token(rec.token)
            if raw:
                tokens.append(raw)
        if tokens:
            _logger.info(
                "Token pool: loaded %d tokens from jaeger.github.token model", len(tokens)
            )
        return tokens
    except Exception:
        return []


def _load_tokens_from_config(env):
    try:
        from ..models.credential_manager import get_encrypted_param
        tokens_str = get_encrypted_param(env, "jaeger.github_tokens", "")
    except Exception:
        _logger.warning(
            "credential_manager import failed; tokens may be encrypted and unusable"
        )
        tokens_str = ""
    tokens = [t.strip() for t in tokens_str.split(",") if t.strip()]
    if tokens:
        _logger.info(
            "Token pool: loaded %d tokens from legacy config param", len(tokens)
        )
    return tokens


def reset_pool():
    """Reset the singleton pool. Useful for testing or when tokens change."""
    global _pool_instance
    with _pool_lock:
        _pool_instance = None
    _logger.info("GitHub token pool reset")
