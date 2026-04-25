# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

# Adapted for the Aurora Pipeline Odoo addon (LGPL-3).
# Apache 2.0 is forward-compatible with LGPL-3 per ASF/FSF guidance.

import hashlib
import logging
import os
import time
import threading
from pathlib import Path

from github import Auth, Github

_logger = logging.getLogger(__name__)


class AuroraPipelineError(Exception):
    pass


_RATE_LIMIT_FLOOR = 50
_RATE_LIMIT_SLEEP_SECONDS = 30
_RATE_LIMIT_CHECK_INTERVAL = 100


class TokenRotator:
    def __init__(self, tokens: list[str]):
        if not tokens:
            raise AuroraPipelineError("TokenRotator requires at least one token")
        self._tokens = list(tokens)
        self._index = 0
        self._lock = threading.Lock()
        self._rate_limit_event = threading.Event()
        self._call_counts: dict[int, int] = {i: 0 for i in range(len(tokens))}
        self._clients: dict[int, Github] = {}

    def _make_client(self, idx: int) -> Github:
        if idx not in self._clients:
            self._clients[idx] = Github(auth=Auth.Token(self._tokens[idx]), per_page=100)
        return self._clients[idx]

    def _pick_next_index(self) -> int:
        n = len(self._tokens)
        start = self._index

        for _ in range(n):
            idx = self._index % n
            self._index += 1
            client = self._make_client(idx)

            remaining, _ = client.rate_limiting
            if remaining > _RATE_LIMIT_FLOOR:
                return idx

        earliest_reset = float("inf")
        for i in range(n):
            c = self._make_client(i)
            _, reset_ts = c.rate_limiting
            if reset_ts < earliest_reset:
                earliest_reset = reset_ts

        wait = max(earliest_reset - time.time(), 0) + 5
        if wait > 0:
            _logger.info(f"  [TokenRotator] All {n} token(s) near rate limit. "
                        f"Sleeping {wait:.0f}s until reset...")
            self._lock.release()
            try:
                self._rate_limit_event.wait(timeout=wait)
                self._rate_limit_event.clear()
            finally:
                self._lock.acquire()

        self._index = start
        return self._index % n

    def get_client(self) -> Github:
        with self._lock:
            idx = self._pick_next_index()
            self._call_counts[idx] += 1
            return self._make_client(idx)

    def get_token(self) -> str:
        with self._lock:
            idx = self._pick_next_index()
            self._call_counts[idx] += 1
            return self._tokens[idx]

    @property
    def token_count(self) -> int:
        return len(self._tokens)

    def summary(self) -> str:
        parts = []
        for i, tok in enumerate(self._tokens):
            client = self._make_client(i)
            remaining, _ = client.rate_limiting
            parts.append(f"  token {i+1}: {remaining} remaining, {self._call_counts[i]} calls")
        return "\n".join(parts)

    def get_rate_limits(self) -> dict[str, dict]:
        result = {}
        for i, tok in enumerate(self._tokens):
            tok_hash = hashlib.sha256(tok.encode()).hexdigest()
            client = self._make_client(i)
            remaining, reset_ts = client.rate_limiting
            result[tok_hash] = {"remaining": remaining, "reset": reset_ts}
        return result
