# -*- coding: utf-8 -*-
"""prompt.qc.run — one LLM-as-a-judge QC run.

Holds the subject (user_prompt), an optional per-run rubric (uploaded .json, decoded into
rubric_text), and the judge's response. The judge's system prompt is NOT per-run: it is uploaded
once on the Settings page and applied universally to every run (see `_get_system_prompt`).

Both the single "Start QC" button and "Bulk Import" use ONE background path — the LLM call never
runs inside an HTTP request (an inline multi-minute Bedrock call in a web worker is what collapses
an instance under load). `action_enqueue` (the button) and controllers/bulk.py both create runs in
state 'queued' and return immediately. A gohan/leviathan-style background worker (a per-process
ThreadPoolExecutor + ir.cron dispatcher/reaper) claims them with SELECT ... FOR UPDATE SKIP LOCKED,
runs Bedrock per run in its own cursor, commits per run, and isolates failures. While a run
executes, the judge's tokens stream live to the run owner over the bus (bus.bus); the verdict is
persisted only on completion. One Bedrock code path (services/bedrock_judge.py, now with
429/5xx retry+backoff) and one pair of terminal writers (`_mark_done` / `_mark_failed`) are shared.

Lifecycle: draft/queued -> running -> done/failed. A partial/aborted run is always 'failed'.
"""
import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS = (".md", ".markdown")
_ALLOWED_RUBRIC_EXTENSIONS = (".json",)

# ----------------------------------------------------------------------
# Background worker (gohan pattern: bounded thread pool + cron dispatch/reap).
# ----------------------------------------------------------------------
# The pool size IS the concurrency cap: at most _POOL_SIZE runs are ever marked 'running'
# DB-wide, because the dispatcher only claims up to (_POOL_SIZE - running) queued runs at a
# time. Keeping the 'running' count aligned with the pool's real execution capacity is what
# keeps the stuck-run reaper correct — a 'running' run is genuinely executing, never just
# parked in the executor's queue waiting for a thread. The advisory lock serializes
# dispatchers so two of them can never each claim a full pool and together overshoot it.
_POOL_SIZE = int(os.environ.get("PROMPT_QC_POOL_SIZE", "16"))
_DISPATCH_LOCK_ID = 729114301  # arbitrary, stable advisory-lock key for this module

# Per-process pool registry (leviathan pattern). Odoo prefork forks N worker processes; a
# ThreadPoolExecutor built once at import time (pre-fork) has DEAD threads in the children — that
# is the "21 May" collapse leviathan documents. Keying the pool on os.getpid() gives every worker
# process a live pool. Global concurrency is still capped at _POOL_SIZE regardless of how many
# processes run, because the dispatcher only ever flips (_POOL_SIZE - running) rows to 'running'
# under the DB-wide advisory lock (see _dispatch_pending) — `running` is a global DB count.
_POOL_REGISTRY = {}
_POOL_REGISTRY_LOCK = threading.Lock()


def _pool_is_dead(pool):
    """A ThreadPoolExecutor is unusable once a worker thread failed to spawn (`_broken`) or it was
    shut down (`_shutdown`). Either way it must be discarded and rebuilt, not served forever
    (leviathan P0-1: a dead pool handed out for 80 min forced every job onto the inline fallback)."""
    return bool(getattr(pool, "_broken", None) or getattr(pool, "_shutdown", False))


def _get_pool():
    """Return this process's judge pool, building it lazily and rebuilding it if it died."""
    pid = os.getpid()
    pool = _POOL_REGISTRY.get(pid)
    if pool is not None and not _pool_is_dead(pool):
        return pool
    with _POOL_REGISTRY_LOCK:
        pool = _POOL_REGISTRY.get(pid)
        if pool is not None and not _pool_is_dead(pool):
            return pool
        pool = ThreadPoolExecutor(
            max_workers=_POOL_SIZE, thread_name_prefix="prompt_qc[pid=%s]" % pid)
        _POOL_REGISTRY[pid] = pool
    return pool


_DEFAULT_TIMEOUT_MIN = 15
_DEFAULT_MAX_FILE_MB = 1
# Extra minutes the reaper waits beyond the run timeout before forcing a 'running' run to
# 'failed'. It must exceed the reaper cron interval (5 min) so the reaper only ever fires AFTER a
# healthy worker has already self-terminated at the wall-clock budget — that keeps the reaper from
# freeing a slot for a worker that is still holding a live Bedrock connection (would overshoot N).
_REAPER_GRACE_MIN = 6

# Live-streaming over the bus. The worker pushes judge tokens to the run owner's bus channel as
# they arrive, batched so we do not open a cursor per token. Each flush commits in its own short
# cursor because Postgres delivers a NOTIFY only at commit — a single end-of-run commit would make
# the whole verdict appear at once instead of streaming.
_BUS_FLUSH_INTERVAL = 0.2   # seconds between flushes
_BUS_FLUSH_CHARS = 400      # or flush early once this many characters have buffered

# Bulk Import creates runs in batches of this size, so one very large submit is not one giant
# in-memory create(). There is no row-count cap (removed by design); this only bounds peak memory.
_CREATE_CHUNK_SIZE = int(os.environ.get("PROMPT_QC_CREATE_CHUNK", "100"))


def _bus_publish(db_name, partner_id, payload):
    """Send one bus notification in a fresh, immediately-committed cursor.

    Streaming progress must commit per message (Postgres fires NOTIFY only at commit), so this is
    deliberately independent of the worker's run-processing transaction. Best-effort: a bus failure
    is logged and swallowed — it must never fail the judge call itself."""
    if not partner_id:
        return
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            partner = env["res.partner"].browse(partner_id)
            env["bus.bus"]._sendone(partner, "prompt_qc.stream", payload)
            cr.commit()
    except Exception:
        _logger.exception("prompt_qc: bus publish failed for run %s", payload.get("run_id"))


class _StreamPublisher:
    """Batches judge deltas and pushes them to the run owner's bus channel.

    Flushes when the buffer is older than _BUS_FLUSH_INTERVAL or longer than _BUS_FLUSH_CHARS. Each
    flush is one bus message in its own committed cursor (see _bus_publish), so the browser sees a
    steady live stream rather than the whole verdict at the very end."""

    def __init__(self, db_name, partner_id, run_id):
        self._db = db_name
        self._partner_id = partner_id
        self._run_id = run_id
        self._buf = ""
        self._last = time.monotonic()

    def push(self, text):
        if not text:
            return
        self._buf += text
        if (len(self._buf) >= _BUS_FLUSH_CHARS
                or (time.monotonic() - self._last) >= _BUS_FLUSH_INTERVAL):
            self.flush()

    def flush(self):
        if not self._buf:
            return
        payload = {"run_id": self._run_id, "kind": "delta", "text": self._buf}
        self._buf = ""
        self._last = time.monotonic()
        _bus_publish(self._db, self._partner_id, payload)


def _dispatch_pending(db_name):
    """Claim up to (_POOL_SIZE - running) queued runs and submit each to the worker pool.

    Safe to call from a request postcommit hook, from a cron, or from a finishing worker — it
    opens its own short-lived cursor and never touches a caller's env. The pool-size bound is
    enforced globally: the advisory lock serializes the slot computation, and SKIP LOCKED + the
    state='queued' filter guarantee a run is claimed at most once.
    """
    ids = []
    try:
        registry = Registry(db_name)
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            Model = env["prompt.qc.run"]
            # Transaction-scoped advisory lock: auto-released on commit OR rollback, so a failed
            # commit can never strand a session lock on a pooled connection. It serializes the
            # slot computation across dispatchers (postcommit kick, cron, finishing workers) so
            # two of them cannot each claim a full pool and overshoot the global cap.
            cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (_DISPATCH_LOCK_ID,))
            got = cr.fetchone()
            if not got or not got[0]:
                return  # another dispatcher holds the lock; it fills the available slots
            running = Model.search_count([("state", "=", "running")])
            slots = max(0, _POOL_SIZE - running)
            if slots <= 0:
                return
            cr.execute(
                """
                SELECT id FROM prompt_qc_run
                 WHERE state = 'queued'
                 ORDER BY id ASC
                 LIMIT %s
                   FOR UPDATE SKIP LOCKED
                """,
                (slots,),
            )
            ids = [r[0] for r in cr.fetchall()]
            if not ids:
                return
            Model.browse(ids).write({
                "state": "running",
                "started_at": fields.Datetime.now(),
                "error_message": False,
            })
            cr.commit()  # releases the xact advisory lock
    except Exception:
        _logger.exception("prompt_qc: dispatch failed")
        return

    for rec_id in ids:
        try:
            _get_pool().submit(_process_run, db_name, rec_id)
        except Exception:
            # Pool refused the task (shutdown): leave the run 'running' for the reaper, which
            # will time it out, and for the next dispatch tick.
            _logger.exception("prompt_qc: could not submit run %s to the pool", rec_id)


def _process_run(db_name, rec_id):
    """Worker body: run one judge call in its own cursor, persist, commit, then refill the slot."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            run = env["prompt.qc.run"].browse(rec_id)
            if not run.exists() or run.state != "running":
                # Already terminal, reaped, or vanished — nothing to do.
                return
            run._execute_judge()
            cr.commit()
    except Exception:
        _logger.exception("prompt_qc: worker crashed for run %s", rec_id)
        _safe_mark_failed(db_name, rec_id, "Worker crashed unexpectedly; see server log.")
    finally:
        # A slot just freed: pull the next queued run without waiting for the cron tick.
        _dispatch_pending(db_name)


def _safe_mark_failed(db_name, rec_id, message):
    """Best-effort terminal write in a fresh cursor (used when the worker's own cursor died)."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            run = env["prompt.qc.run"].browse(rec_id)
            if run.exists():
                run._mark_failed(message, 0.0, run.model_label)
            cr.commit()
    except Exception:
        _logger.exception("prompt_qc: failed to mark run %s failed", rec_id)


class PromptQcRun(models.Model):
    _name = "prompt.qc.run"
    _description = "Prompt QC Run"
    _order = "id desc"

    name = fields.Char(string="Reference", default="New", copy=False)

    user_prompt = fields.Text(
        string="User Prompt",
        required=True,
        help="The subject prompt being evaluated. Sent as the user message to the judge.",
    )

    # NOTE: the judge's system prompt (.md) is NO LONGER per-run. It is uploaded once on the
    # Settings page and applied UNIVERSALLY to every run — see `_get_system_prompt` and
    # res.config.settings (ir.config_parameter prompt_qc.system_prompt_text).

    # Uploaded .json rubric (optional, per-run). Decoded into rubric_text and, when present, sent
    # to the judge as a second system block (decision D5). Passthrough only — not parsed into scores.
    rubric_file = fields.Binary(string="Rubric (.json)", attachment=False)
    rubric_filename = fields.Char(string="Rubric Filename")
    rubric_text = fields.Text(
        string="Rubric Text",
        help="Decoded contents of the uploaded .json rubric. Sent to the judge alongside the "
             "system prompt. Optional; left empty when no rubric is uploaded.",
    )

    response = fields.Text(
        string="Judge Response",
        readonly=True,
        copy=False,
    )

    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("running", "Running"),
            ("streaming", "Streaming"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="draft",
        required=True,
        copy=False,
    )

    error_message = fields.Text(string="Error", readonly=True, copy=False)

    # Stamped when a bulk run is claimed by a worker (state -> running). The reaper uses this to
    # time out a run that never reaches a terminal state.
    started_at = fields.Datetime(string="Started At", readonly=True, copy=False)

    # Metadata stamped on completion.
    model_label = fields.Char(string="Model", readonly=True, copy=False)
    input_tokens = fields.Integer(string="Input Tokens", readonly=True, copy=False)
    output_tokens = fields.Integer(string="Output Tokens", readonly=True, copy=False)
    duration = fields.Float(string="Duration (s)", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Upload decode (aurora pattern).
    # ------------------------------------------------------------------
    @api.model
    def _decode_md_text(self, raw_b64, filename, max_file_bytes=None):
        """Decode an uploaded .md (base64) to UTF-8 text, validating extension and (optionally)
        size. Used by the Settings page to store the ONE universal judge system prompt. Raises
        UserError on a bad upload. Returns "" for an empty/cleared upload."""
        if not raw_b64:
            return ""
        if max_file_bytes:
            self._check_file_size(raw_b64, max_file_bytes, _("System prompt"))
        try:
            data = base64.b64decode(raw_b64)
        except Exception as exc:
            raise UserError(_("Invalid upload for the system prompt file: %s") % exc)
        if not data:
            return ""
        fname = (filename or "").strip().lower()
        if fname and not fname.endswith(_ALLOWED_EXTENSIONS):
            raise UserError(_("The system prompt must be a .md / .markdown file."))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UserError(_("The system prompt file is not valid UTF-8 text: %s") % exc)

    @api.model
    def _decode_rubric(self, vals):
        raw_b64 = vals.get("rubric_file")
        if not raw_b64:
            return
        try:
            data = base64.b64decode(raw_b64)
        except Exception as exc:
            raise UserError(_("Invalid upload for the rubric file: %s") % exc)
        if not data:
            return
        filename = (vals.get("rubric_filename") or "").strip().lower()
        if filename and not filename.endswith(_ALLOWED_RUBRIC_EXTENSIONS):
            raise UserError(_("The rubric must be a .json file."))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UserError(_("The rubric file is not valid UTF-8 text: %s") % exc)
        # D6: validate well-formedness only. We forward the raw text, not a parsed structure.
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise UserError(_("The rubric file is not valid JSON: %s") % exc)
        vals["rubric_text"] = text

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("prompt.qc.run") or "New"
            self._decode_rubric(vals)
        return super().create(vals_list)

    def write(self, vals):
        if "rubric_file" in vals:
            self._decode_rubric(vals)
        return super().write(vals)

    @api.model
    def create_runs_chunked(self, vals_list):
        """Create many runs in bounded batches, so one very large Bulk Import is not one giant
        in-memory create(). Same transaction as the caller — this caps peak memory and keeps each
        create() call small; there is intentionally no row-count limit on the import itself."""
        runs = self.browse()
        for i in range(0, len(vals_list), _CREATE_CHUNK_SIZE):
            runs |= self.create(vals_list[i:i + _CREATE_CHUNK_SIZE])
        return runs

    def action_enqueue(self):
        """Queue these runs and kick the background dispatcher, then return immediately.

        This is the single-run entry point for the 'Start QC' button. The LLM call never runs in
        the calling (HTTP) request — it runs in the background pool (capped at _POOL_SIZE, with
        retry/backoff), streaming tokens to the owner over the bus and persisting on completion.
        Runs already in flight ('queued'/'running') are left untouched."""
        to_queue = self.filtered(lambda r: r.state not in ("queued", "running"))
        if to_queue:
            to_queue.write({
                "state": "queued",
                "response": False,
                "error_message": False,
                "started_at": False,
            })
        self._enqueue_dispatch()
        return True

    # ------------------------------------------------------------------
    # Config helpers (read once from System Parameters; default + clamp).
    # ------------------------------------------------------------------
    @api.model
    def _icp_int(self, key, default, minimum=1, maximum=None):
        # get_param returns False for an unset key, and int(False) == 0 (no exception), so an
        # unset config_parameter must be treated as "use the default" explicitly — otherwise every
        # unset setting would silently clamp to `minimum` before an admin first saves Settings.
        raw = self.env["ir.config_parameter"].sudo().get_param(key)
        if raw in (False, None, ""):
            val = default
        else:
            try:
                val = int(raw)
            except (TypeError, ValueError):
                val = default
        if val < minimum:
            val = minimum
        if maximum is not None and val > maximum:
            val = maximum
        return val

    @api.model
    def _get_run_timeout_minutes(self):
        return self._icp_int("prompt_qc.run_timeout_minutes", _DEFAULT_TIMEOUT_MIN, minimum=1)

    @api.model
    def _get_max_file_size_mb(self):
        return self._icp_int("prompt_qc.max_file_size_mb", _DEFAULT_MAX_FILE_MB, minimum=1)

    # ------------------------------------------------------------------
    # Bulk row validation (shared, non-raising — one error per row, valid rows untouched).
    # ------------------------------------------------------------------
    @api.model
    def _validate_bulk_row(self, row, max_file_bytes):
        """Validate a single bulk-import row.

        Returns ``(vals, error)``:
          - (None, None)  -> empty row, silently skipped (no prompt and no files)
          - (None, "...") -> invalid row, message to surface to the user
          - (vals, None)  -> valid; ``vals`` is ready for create() in state 'queued'

        Requirements mirror a single run: user_prompt is mandatory; the .json rubric is optional
        (no rubric -> no rubric block). The judge's system prompt is global (Settings), not per-row.
        """
        user_prompt = (row.get("user_prompt") or "").strip()
        rub_b64 = (row.get("rubric_b64") or "").strip()

        if not user_prompt and not rub_b64:
            return None, None  # empty row — skip

        if not user_prompt:
            return None, _("User prompt is required.")

        vals = {"user_prompt": user_prompt, "state": "queued"}
        if rub_b64:
            vals["rubric_file"] = rub_b64
            vals["rubric_filename"] = row.get("rubric_filename") or "rubric.json"

        try:
            self._check_file_size(rub_b64, max_file_bytes, _("Rubric"))
            # Reuse the exact decode/extension/JSON validation used by create().
            self._decode_rubric(vals)
        except UserError as exc:
            msg = exc.args[0] if exc.args else str(exc)
            return None, msg
        return vals, None

    @api.model
    def _check_file_size(self, raw_b64, max_file_bytes, label):
        if not raw_b64:
            return
        # Cheap guard BEFORE decoding: the base64 text for `max_file_bytes` decoded bytes is about
        # ceil(n/3)*4 chars. Reject an oversized string up front so a hostile payload is never
        # base64-decoded into memory just to be measured (resource amplification).
        b64_ceiling = (max_file_bytes // 3 + 1) * 4 + 64  # +slack for padding/whitespace
        if len(raw_b64) > b64_ceiling:
            raise UserError(
                _("The %(label)s file is over the %(cap).1f MB limit.") % {
                    "label": label,
                    "cap": max_file_bytes / (1024.0 * 1024.0),
                }
            )
        try:
            size = len(base64.b64decode(raw_b64))
        except Exception as exc:
            raise UserError(_("Invalid upload for the %s file: %s") % (label, exc))
        if size > max_file_bytes:
            raise UserError(
                _("The %(label)s file is %(size).1f MB, over the %(cap).1f MB limit.") % {
                    "label": label,
                    "size": size / (1024.0 * 1024.0),
                    "cap": max_file_bytes / (1024.0 * 1024.0),
                }
            )

    # ------------------------------------------------------------------
    # Background dispatch entry points.
    # ------------------------------------------------------------------
    @api.model
    def _enqueue_dispatch(self):
        """Kick the dispatcher right after the current request commits (so the new 'queued'
        runs are durably visible). The actual LLM work runs in pool threads, never here."""
        db_name = self.env.cr.dbname
        self.env.cr.postcommit.add(lambda: _dispatch_pending(db_name))

    @api.model
    def _cron_dispatch_queue(self):
        """Safety-net dispatcher (every minute): drains queued runs even if a postcommit kick
        was lost (e.g. process restart between commit and kick)."""
        _dispatch_pending(self.env.cr.dbname)

    @api.model
    def _cron_reap_stuck_runs(self):
        """Backstop timeout guard for a DEAD worker (crashed thread / killed process that never
        wrote a terminal state). A healthy worker self-terminates at its wall-clock budget, so we
        wait timeout + grace (> the reaper cron interval) before forcing 'running' -> 'failed' —
        this keeps the reaper from freeing a slot for a worker that is still mid-call. Runs sudo so
        the 'no run is ever stuck' guarantee never depends on the cron's Scheduler User."""
        timeout_min = self._get_run_timeout_minutes()
        cutoff = fields.Datetime.now() - timedelta(minutes=timeout_min + _REAPER_GRACE_MIN)
        Model = self.sudo()
        stuck = Model.search([("state", "=", "running"), ("started_at", "<", cutoff)])
        if not stuck:
            return
        for run in stuck:
            _logger.warning("prompt_qc: reaping run %s stuck in 'running' > %s min",
                            run.id, timeout_min + _REAPER_GRACE_MIN)
            run._mark_failed(
                _("Timed out: no completion within %s minutes.") % timeout_min,
                0.0, run.model_label,
            )
        self.env.cr.commit()
        _dispatch_pending(self.env.cr.dbname)

    # ------------------------------------------------------------------
    # Judge execution + terminal-state writers (shared single + bulk).
    # ------------------------------------------------------------------
    def _execute_judge(self):
        """Run one Bedrock judge call for this (already 'running') run, stream it, and persist.

        Called by the background worker on its own cursor. Tokens are accumulated server-side AND
        pushed live to the run owner over the bus (via _StreamPublisher); the full verdict is
        written exactly once, on completion (persist-only-on-completion, decision D1).
        """
        self.ensure_one()
        from ..services import bedrock_judge

        t0 = time.time()
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = bedrock_judge.get_api_key(ICP)
        inference_arn, region = bedrock_judge.resolve_arn_and_region(ICP)
        model_label = (inference_arn.rsplit("/", 1)[-1][:120] if inference_arn else "") or False

        def _fail(msg):
            self._mark_failed(msg, round(time.time() - t0, 2), model_label)

        if not api_key:
            return _fail(_("Missing Bedrock API key. Set prompt_qc.bedrock_api_key in Settings "
                           "or AWS_BEARER_TOKEN_BEDROCK in .env."))
        if not inference_arn:
            return _fail(_("Missing prompt_qc.bedrock_inference_arn in Settings."))
        if not (self.user_prompt and self.user_prompt.strip()):
            return _fail(_("The user prompt is empty."))
        system_prompt = self._get_system_prompt()
        if not (system_prompt and system_prompt.strip()):
            return _fail(_("No system prompt configured. Upload a .md judge prompt in "
                           "Settings -> Prompt QC."))

        # Hard wall-clock budget for this run. The worker self-terminates at this budget — well
        # before the reaper window (budget + grace) — so a healthy worker is never reaped mid-call
        # and the global concurrency cap N is never overshot. `timeout` bounds connect/idle-read;
        # `max_seconds` bounds TOTAL elapsed even for a slow-trickle stream.
        budget = float(self._get_run_timeout_minutes() * 60)
        # Stream tokens live to the run owner over the bus while accumulating server-side. The bus
        # push is fire-forward; the durable verdict is still written only once, on completion.
        publisher = _StreamPublisher(
            self.env.cr.dbname, self.create_uid.partner_id.id, self.id)
        result = bedrock_judge.collect_judge(
            api_key, inference_arn, region, system_prompt, self.user_prompt,
            rubric_text=self._get_rubric(),
            max_tokens=bedrock_judge.JUDGE_MAX_TOKENS,
            temperature=bedrock_judge.JUDGE_TEMPERATURE,
            timeout=budget, max_seconds=budget,
            on_delta=publisher.push,
        )
        publisher.flush()  # emit any buffered tail before the terminal event
        duration = round(time.time() - t0, 2)
        if result["ok"]:
            self._mark_done(result["text"], result["usage"], duration, model_label)
        else:
            self._mark_failed(result["error"] or _("Stream ended before completion."),
                              duration, model_label)

    def _is_already_terminal_locked(self):
        """Lock this run's row and read its COMMITTED state. Returns True if the row is gone or
        already terminal (done/failed) — the caller must then NOT write. This serializes the
        worker's terminal write against the reaper's on the row itself, so the FIRST terminal state
        wins: a finished run can never be flipped (e.g. a reaped 'failed' run resurrected to 'done'
        by a late-finishing worker)."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT state FROM prompt_qc_run WHERE id = %s FOR UPDATE", (self.id,))
        row = self.env.cr.fetchone()
        if not row:
            return True
        return row[0] in ("done", "failed")

    def _mark_done(self, response, usage, duration, model_label):
        """Terminal success write. First-terminal-wins (row-locked). Notifies the owner on commit."""
        self.ensure_one()
        if self._is_already_terminal_locked():
            return
        usage = usage or {}
        self.write({
            "state": "done",
            "response": response,
            "error_message": False,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "duration": duration,
            "model_label": model_label or False,
        })
        self._notify_stream_terminal("done", False)

    def _mark_failed(self, error, duration, model_label):
        """Terminal failure write. D1: never store partial text. First-terminal-wins (row-locked)."""
        self.ensure_one()
        if self._is_already_terminal_locked():
            return
        message = (error or _("Stream ended before completion."))[:2000]
        self.write({
            "state": "failed",
            "error_message": message,
            "duration": duration,
            "model_label": model_label or False,
        })
        self._notify_stream_terminal("failed", message)

    def _notify_stream_terminal(self, state, error):
        """After this transaction commits, push the terminal event to the run owner's bus channel
        so the browser stops the spinner and reloads the durable result. Registered as a postcommit
        so the frontend never reloads before the write is visible. Fires exactly once, because the
        terminal writers are guarded by _is_already_terminal_locked()."""
        partner = self.create_uid.partner_id
        if not partner:
            return
        payload = {
            "run_id": self.id,
            "kind": "done",
            "state": state,
            "error": (error or "")[:500] or False,
        }
        db_name = self.env.cr.dbname
        partner_id = partner.id
        self.env.cr.postcommit.add(lambda: _bus_publish(db_name, partner_id, payload))

    # ------------------------------------------------------------------
    # Helpers used by the background worker.
    # ------------------------------------------------------------------
    def _get_system_prompt(self):
        """Resolve the judge instructions for ANY run: the ONE universal system prompt uploaded on
        the Settings page (ir.config_parameter prompt_qc.system_prompt_text), else the packaged
        seed prompt. The same prompt is used for every run — single and bulk alike. The stored text
        is returned verbatim (only the emptiness check strips)."""
        raw = self.env["ir.config_parameter"].sudo().get_param("prompt_qc.system_prompt_text") or ""
        if raw.strip():
            return raw
        return self._default_seed_prompt()

    def _get_rubric(self):
        """Resolve the rubric to send to the judge: the uploaded text, else empty.

        The rubric is optional. When empty, no rubric block is sent and behaviour matches a
        plain (no-rubric) run. We deliberately do NOT fall back to a default rubric, so adding
        this feature never changes an existing run that uploaded no rubric.
        """
        self.ensure_one()
        if self.rubric_text and self.rubric_text.strip():
            return self.rubric_text
        return ""

    @api.model
    def _default_seed_prompt(self):
        from odoo.modules.module import get_module_path

        try:
            path = os.path.join(get_module_path("prompt_qc"), "data", "qc_seed_prompt.md")
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read()
        except Exception:
            _logger.warning("prompt_qc: could not load default seed prompt")
        return ""
