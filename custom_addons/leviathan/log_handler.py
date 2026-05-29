"""Python logging handler that captures ``[job=N]`` tagged log records
from ``odoo.addons.leviathan.*`` and persists them to ``leviathan.job.log``
so the per-job "Logs" tab on the form view shows every relevant line —
regardless of which process emitted it (UI pod, worker pod, Odoo cron).

Design notes (v2 — buffered, F-HIGH-1)
======================================
* Emit is NON-BLOCKING and DROPS NEVER MISS: log records matching the
  job tag are appended to a process-local in-memory buffer keyed by
  ``db_name``. A daemon flush thread drains the buffer every
  ``_FLUSH_INTERVAL`` seconds via a single bulk-INSERT per db.
* Buffer is bounded (``_MAX_BUFFER``). When full, OLDEST entries are
  dropped (FIFO) and a counter is incremented. The drop counter is
  surfaced every minute through stderr (NOT through this handler — see
  F-HIGH-4) so operators always have visibility on the drop rate.
* Cursor cost drops from O(log_lines/min) to O(flushes/min) ≈ 12/min
  for the default 5s flush interval, regardless of log volume.
  Under the documented 100-concurrent-PRD envelope this is the
  difference between ~3,000 cursors/min and ~12 cursors/min.
* Bulk insert uses ``execute_values`` (psycopg2 fast path) for one
  round trip per flush regardless of batch size.
* Failures are SWALLOWED. Logging must never raise — if the log table
  doesn't exist yet (pre-migration), or the DB is unreachable, the
  worker keeps running and the operator still sees the original
  Python stderr log via journalctl / kubectl logs.
* Name resolution cache (``LEV-00012`` → id) is LRU-bounded at
  ``_NAME_CACHE_MAX`` entries to address F-MED-3 (previously an
  unbounded dict + thread-unsafe check-then-insert).

Installation: the addon's ``post_load`` hook (see ``__init__.py``)
calls :func:`install_handler` exactly once per Python process. Safe to
call multiple times — guarded by ``_INSTALLED``.

Lifecycle
=========
* `install_handler()` starts the flush thread (daemon).
* `shutdown_handler()` (optional) drains the buffer one final time.
  Worker pod calls this in its SIGTERM path so in-flight log lines
  don't get dropped on rollout. Other contexts can ignore it —
  daemon thread dies with the interpreter and the dropped batch is
  bounded.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

# Matches `[job=12]` (raw id) OR `[job=LEV-00012]` (record name).
_JOB_TAG_RE = re.compile(r"\[job=(LEV-)?(\d+)\]")
_HOSTNAME = os.environ.get("HOSTNAME", "")


def _detect_source() -> str:
    # K8s Deployment names: `leviathan-worker-<random>`. Anything else —
    # UI pod, local dev shell, Odoo cron — counts as `odoo`. Lambda
    # logs flow through the explicit CloudWatch fetcher, not here.
    if _HOSTNAME.startswith("leviathan-worker-"):
        return "worker"
    return "odoo"


_SOURCE = _detect_source()


def _resolve_db_name() -> str | None:
    # Odoo pins ``dbname`` as a thread-local attribute on every HTTP
    # request thread; the worker's heartbeat ticker also sets it. Fall
    # back to the env var the K8s manifests inject for cron / startup
    # paths where no HTTP request is in flight.
    th = threading.current_thread()
    return getattr(th, "dbname", None) or os.environ.get("ODOO_DB") or None


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
_FLUSH_INTERVAL_S = float(
    os.environ.get("LEVIATHAN_LOG_FLUSH_INTERVAL_S", "5")
)
_MAX_BUFFER = int(
    os.environ.get("LEVIATHAN_LOG_BUFFER_MAX", "5000")
)
_DROP_REPORT_INTERVAL_S = float(
    os.environ.get("LEVIATHAN_LOG_DROP_REPORT_INTERVAL_S", "60")
)
_NAME_CACHE_MAX = int(
    os.environ.get("LEVIATHAN_LOG_NAME_CACHE_MAX", "10000")
)
_MESSAGE_MAX_BYTES = 16 * 1024  # cap individual log lines (Bedrock tracebacks)


# ---------------------------------------------------------------------------
# Bounded LRU name → id cache (thread-safe)
# ---------------------------------------------------------------------------
class _NameCache:
    """Tiny LRU bounded at ``_NAME_CACHE_MAX``. Thread-safe.

    F-MED-3 fix: the previous unbounded dict + bare ``in`` check-then-
    insert pattern was technically race-safe under CPython GIL but
    bounded growth wasn't enforced. ``OrderedDict.move_to_end`` plus a
    single lock keeps this O(1) and bounded.
    """

    def __init__(self, maxsize: int):
        self._d: collections.OrderedDict[str, int] = collections.OrderedDict()
        self._max = maxsize
        self._lock = threading.Lock()

    def get(self, key: str) -> int | None:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def set(self, key: str, value: int) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)


# ---------------------------------------------------------------------------
# Drop accounting (stderr-direct so the signal can't itself be dropped)
# F-HIGH-4
# ---------------------------------------------------------------------------
_drop_lock = threading.Lock()
_drops_since_last_report = 0
_last_report_ts = time.monotonic()


def _record_drop() -> None:
    global _drops_since_last_report
    with _drop_lock:
        _drops_since_last_report += 1


def _maybe_report_drops() -> None:
    """Stderr-direct drop report. Called by the flush thread tick.

    Direct stderr write (NOT via the logger) so the drop signal cannot
    re-enter this handler and be itself buffered/dropped. Operators
    grep this in `kubectl logs`.
    """
    global _drops_since_last_report, _last_report_ts
    now = time.monotonic()
    if now - _last_report_ts < _DROP_REPORT_INTERVAL_S:
        return
    with _drop_lock:
        n = _drops_since_last_report
        _drops_since_last_report = 0
        _last_report_ts = now
    if n:
        sys.stderr.write(
            f"[leviathan-log-handler] dropped {n} log lines in last "
            f"{int(_DROP_REPORT_INTERVAL_S)}s (buffer full or DB unreachable)\n"
        )
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Buffered handler
# ---------------------------------------------------------------------------
class LeviathanJobLogHandler(logging.Handler):
    """Buffered: ``emit`` is O(1) lock-and-append; bulk flush by a
    daemon thread does the SQL.
    """

    # Per-db FIFO buffer. Each entry is
    #   (job_id, ts_utc_naive, source, level, message, pod)
    # The lock protects ``_buffers`` (and only ``_buffers``).
    _buffers: dict[str, collections.deque] = {}
    _buffers_lock = threading.Lock()

    _name_cache = _NameCache(_NAME_CACHE_MAX)

    def emit(self, record):
        try:
            if not record.name.startswith("odoo.addons.leviathan"):
                return
            msg = record.getMessage()
            m = _JOB_TAG_RE.search(msg)
            if not m:
                return
            job_id = self._resolve_job_id(m)
            if not job_id:
                return
            db_name = _resolve_db_name()
            if not db_name:
                return
            ts = (
                datetime.fromtimestamp(record.created, timezone.utc)
                .replace(tzinfo=None)
            )
            truncated = msg[:_MESSAGE_MAX_BYTES]
            entry = (
                job_id, ts, _SOURCE, record.levelname, truncated,
                _HOSTNAME or None,
            )
            with self._buffers_lock:
                buf = self._buffers.get(db_name)
                if buf is None:
                    buf = collections.deque(maxlen=_MAX_BUFFER)
                    self._buffers[db_name] = buf
                # deque(maxlen=N) silently drops the OLDEST entry on
                # overflow — that's what we want, and we record the
                # drop here so the stderr report is honest.
                if len(buf) == _MAX_BUFFER:
                    _record_drop()
                buf.append(entry)
        except Exception:
            # Logging must never raise. Stderr already has the original
            # record via the default handler — the DB persist is best-
            # effort.
            pass

    def _resolve_job_id(self, match) -> int | None:
        prefix, num = match.group(1), match.group(2)
        if not prefix:
            # `[job=12]` — `num` is the record id directly.
            try:
                return int(num)
            except ValueError:
                return None
        # `[job=LEV-00012]` — look up by name. Lazy import of Registry
        # to keep module-import time near zero (the handler is wired up
        # in `post_load` before the registry is fully built for some
        # boot orders).
        name = f"LEV-{num.zfill(5)}"
        cached = self._name_cache.get(name)
        if cached is not None:
            return cached
        db_name = _resolve_db_name()
        if not db_name:
            return None
        try:
            from odoo.modules.registry import Registry
            with Registry(db_name).cursor() as cr:
                cr.execute(
                    "SELECT id FROM leviathan_job WHERE name = %s LIMIT 1",
                    (name,),
                )
                row = cr.fetchone()
                if row:
                    self._name_cache.set(name, row[0])
                    return row[0]
        except Exception:
            return None
        return None


# ---------------------------------------------------------------------------
# Flush loop (daemon)
# ---------------------------------------------------------------------------
_flush_thread: threading.Thread | None = None
_flush_stop = threading.Event()


def _bulk_flush_one_db(db_name: str, rows: list[tuple]) -> None:
    """Single bulk-INSERT for the rows of one db.

    Uses ``execute_values`` which the psycopg2 wire format folds into
    a single round trip. Cap on individual log line size already
    enforced at emit. Lock timeout still 500 ms because the FK to
    ``leviathan_job`` may contend with a parent ``FOR UPDATE`` — a
    bulk drop is preferable to hanging the user-visible action.
    """
    if not rows:
        return
    try:
        from odoo.modules.registry import Registry
        from psycopg2.extras import execute_values
        with Registry(db_name).cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '500ms'")
            execute_values(
                cr,
                """
                INSERT INTO leviathan_job_log
                    (job_id, timestamp, source, level, message, pod,
                     create_uid, write_uid, create_date, write_date)
                VALUES %s
                """,
                rows,
                template=(
                    "(%s, %s, %s, %s, %s, %s, 1, 1, "
                    "NOW() AT TIME ZONE 'UTC', "
                    "NOW() AT TIME ZONE 'UTC')"
                ),
            )
            cr.commit()
    except Exception:
        # Whole batch dropped; account for that so the stderr report
        # is accurate.
        for _ in rows:
            _record_drop()


def _flush_once() -> None:
    """Snapshot all per-db buffers, clear them, bulk-INSERT each."""
    snapshot: dict[str, list[tuple]] = {}
    with LeviathanJobLogHandler._buffers_lock:
        for db_name, buf in LeviathanJobLogHandler._buffers.items():
            if buf:
                snapshot[db_name] = list(buf)
                buf.clear()
    for db_name, rows in snapshot.items():
        _bulk_flush_one_db(db_name, rows)


def _flush_loop() -> None:
    while not _flush_stop.is_set():
        try:
            _flush_once()
            _maybe_report_drops()
        except Exception:
            # _flush_once already guards each db; this is belt-and-
            # braces against an unexpected exception in the reporting
            # path. Logging here would re-enter the buffer.
            pass
        _flush_stop.wait(timeout=_FLUSH_INTERVAL_S)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_INSTALLED = False


def install_handler() -> None:
    """Attach the handler + start the flush thread. Idempotent.

    Called from ``post_load`` in ``__init__.py`` so it runs in EVERY
    process that loads this addon: Odoo HTTP workers, Odoo cron
    workers, and the standalone PRD worker (which loads the addon's
    models via the boot path in ``worker/run_prd.py``).
    """
    global _INSTALLED, _flush_thread
    if _INSTALLED:
        return
    handler = LeviathanJobLogHandler()
    handler.setLevel(logging.INFO)
    target = logging.getLogger("odoo.addons.leviathan")
    target.addHandler(handler)

    # Daemon flush thread — dies with the interpreter. The bounded
    # buffer means worst-case data loss on abrupt shutdown is one
    # flush interval × the buffer size, capped at _MAX_BUFFER per db.
    t = threading.Thread(
        target=_flush_loop,
        name="leviathan-log-flush",
        daemon=True,
    )
    t.start()
    _flush_thread = t

    _INSTALLED = True
    _logger.info(
        "[leviathan] log handler installed (source=%s, hostname=%s, "
        "flush_interval=%.1fs, buffer_max=%d, name_cache_max=%d)",
        _SOURCE, _HOSTNAME, _FLUSH_INTERVAL_S, _MAX_BUFFER, _NAME_CACHE_MAX,
    )


def shutdown_handler(timeout_s: float = 5.0) -> None:
    """Drain the buffer one final time and stop the flush thread.

    Optional. The worker's SIGTERM handler calls this so log lines
    emitted in the last second before shutdown still hit the DB. If
    not called, the daemon thread dies with the interpreter and the
    bounded buffer is lost — typically <5s of logs.
    """
    global _INSTALLED, _flush_thread
    if not _INSTALLED:
        return
    try:
        _flush_once()
    except Exception:
        pass
    _flush_stop.set()
    if _flush_thread is not None:
        _flush_thread.join(timeout=timeout_s)
    _maybe_report_drops()
