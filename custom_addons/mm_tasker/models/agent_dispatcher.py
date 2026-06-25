# -*- coding: utf-8 -*-
"""Reference-agent dispatch + LLM-judge grading.

Two extension points:
  1. `_call_provider(...)` — POSTs to `mm_tasker.backend_url` (or returns a
     mock if `mm_tasker.test_mode=true`). The backend wraps the actual
     provider SDKs.
  2. `_call_judge(...)` — POSTs to `mm_tasker.judge_backend_url`
     (fallback: `mm_tasker.backend_url`) using the model key from
     `mm_tasker.judge_model_key`. Returns a parsed verdict
     `{passed, rationale, confidence}`.

Both dispatch_runs (Run Models) and judge_runs (Run Judge) are
synchronous in v1. Swap to ThreadPoolExecutor when latency hurts.
"""
import hashlib
import json
import logging
import re
import uuid

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MmTaskerAgentDispatcher(models.AbstractModel):
    _name = 'mm.tasker.agent.dispatcher'
    _description = 'MM Tasker Agent Dispatcher (orchestrates model runs + grading)'

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    @api.model
    def dispatch_runs(self, runs):
        """Run + grade each run record in `runs`.

        Routes on ``mm_tasker.test_mode``:
          - true  -> synchronous in-process mock (unchanged v1 path: execute,
                     grade and score each run right here).
          - false -> submit each run to the async goku service (POST /run) and
                     return immediately. The agent executes on the service in
                     the background; results are ingested later by the poll
                     cron (``mm.tasker.run._cron_poll_jobs``).

        Both callers (the Run Models wizard and per-run Re-run) go through
        here, so neither needs to know which mode is active.
        """
        Param = self.env['ir.config_parameter'].sudo()
        test_mode = self._param_truthy(
            Param.get_param('mm_tasker.test_mode', default='true')
        )
        if not test_mode:
            return self.submit_runs(runs)
        for run in runs:
            try:
                self._execute_run(run)
            except Exception as e:
                _logger.exception('Run %s failed', run.id)
                run.write({
                    'state': 'error',
                    'error_message': f'{type(e).__name__}: {e}',
                    'finished_at': fields.Datetime.now(),
                })
        return True

    def _execute_run(self, run):
        run.write({'state': 'running', 'started_at': fields.Datetime.now()})

        task = run.task_id
        # Total runs for this (task, model) so the backend can stage
        # results into a goku-style run_N/ directory tree instead of
        # overwriting per call. Computed here (not stored) because the
        # set can change if rows are added/removed mid-dispatch.
        runs_total = len(task.run_ids.filtered(
            lambda r: r.model_key == run.model_key
        ))
        result = self._call_provider(
            task_id=str(task.id),
            model_key=run.model_key,
            prompt=task.final_prompt or '',
            media=task.media_ids,
            rubrics=task.rubric_ids,
            system_prompt=getattr(task, 'system_prompt', None),
            idempotency_key=f'mm-tasker-run-{run.id}',
            run_index=run.run_index,
            runs_total=runs_total,
        )

        run.write({
            'response_text': result['response_text'],
            'tokens_in': result.get('tokens_in', 0),
            'tokens_out': result.get('tokens_out', 0),
            'state': 'judging',
        })

        # Output files (v1: none from mock adapter — kept for future tool use)
        for f in result.get('output_files', []):
            self.env['mm.tasker.run.output'].create({
                'run_id': run.id,
                'name': f['name'],
                'file': f.get('file'),
                'sha256': f.get('sha256') or hashlib.sha256(
                    (f.get('file') or b'').encode() if isinstance(f.get('file'), str) else (f.get('file') or b'')
                ).hexdigest(),
                'size_bytes': f.get('size_bytes', 0),
            })

        self._grade_run(run, backend_scores=result.get('scores'))
        self._compute_aggregate(run)
        run.write({'state': 'scored', 'finished_at': fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Async dispatch (live mode) — submit to the goku service /run, then
    # ingest results later via the poll cron. /run returns 202 + job_id and
    # runs the agent in the background, so this never blocks on inference.
    # ------------------------------------------------------------------
    @api.model
    def submit_runs(self, runs):
        """POST each run to the service ``/run`` (async). Stores the returned
        job_id and marks the run ``running``. Failures are captured per-run so
        one bad submit doesn't abort the batch — mirrors dispatch_runs."""
        for run in runs:
            try:
                self._submit_run(run)
            except Exception as e:
                _logger.exception('Run %s submit failed', run.id)
                run.write({
                    'state': 'error',
                    'ext_state': 'error',
                    'error_message': f'{type(e).__name__}: {e}',
                    'finished_at': fields.Datetime.now(),
                })
        return True

    def _submit_run(self, run):
        """Build the /run payload and POST it; store the job handle."""
        task = run.task_id
        runs_total = len(task.run_ids.filtered(
            lambda r: r.model_key == run.model_key
        ))
        key = uuid.uuid4().hex
        payload = self._build_run_payload(
            run, runs_total, idempotency_key=f'mmrun-{run.id}-{key}'
        )
        data = self._post_backend(self._run_url(), payload)
        job_id = (data or {}).get('job_id') or ''
        if not job_id:
            raise UserError(_(
                'Service /run did not return a job_id (got: %s).'
            ) % (str(data)[:200]))
        # Always 'submitted' here. The service's 202 reports status "queued",
        # which is NOT one of our ext_state values — the poll cron maps the
        # live service status (queued/running/done/error) onto ext_state.
        run.write({
            'external_job_id': job_id,
            'idempotency_key': key,
            'ext_state': 'submitted',
            'submitted_at': fields.Datetime.now(),
            'started_at': fields.Datetime.now(),
            'state': 'running',
            'error_message': False,
        })

    def _build_run_payload(self, run, runs_total, idempotency_key=None):
        """Wire payload for POST /run. Identical shape to the legacy
        ``_call_provider_backend`` body (run_index/media/rubrics), so the
        service contract is unchanged — only the call style (async) differs."""
        task = run.task_id
        payload = {
            'task_id': str(task.id),
            'model': run.model_key,
            'run_index': int(run.run_index or 1),
            'runs_total': int(runs_total or 1),
            'system_prompt': getattr(task, 'system_prompt', None)
                or 'You are a helpful assistant.',
            'user_prompt': task.final_prompt or '',
            'media': [
                {
                    'name': m.name or '',
                    'filename': m.filename or m.name or '',
                    'kind': m.kind or 'other',
                    'mime_type': m.mime_type or '',
                    'data_b64': (m.file or b'').decode()
                        if isinstance(m.file, bytes) else (m.file or ''),
                }
                for m in task.media_ids
            ],
            'rubrics': [
                {
                    'number': r.number,
                    'type': r.rubric_type or '',
                    'category': r.category or '',
                    'importance': r.importance or '',
                    'points': r.points or 0,
                    'criterion': r.criterion or '',
                    'raw_json': r.raw_json or '',
                }
                for r in task.rubric_ids
            ],
        }
        if idempotency_key:
            payload['idempotency_key'] = idempotency_key
        return payload

    def _service_base_url(self):
        """Normalised service base — no trailing '/run', no trailing slash.

        Accepts either form of the ``mm_tasker.backend_url`` system parameter
        so a missing/extra ``/run`` can't misroute /run vs /jobs:
          - ``http://host:8000``       (base)
          - ``http://host:8000/run``   (legacy: pointed at the /run endpoint)
        Raises if the parameter is empty (fail-closed — never silently live).
        """
        Param = self.env['ir.config_parameter'].sudo()
        url = (Param.get_param('mm_tasker.backend_url') or '').strip()
        if not url:
            raise UserError(_(
                'mm_tasker.backend_url is not configured. Set it in System '
                'Parameters or flip mm_tasker.test_mode back to true.'
            ))
        base = url.rstrip('/')
        if base.endswith('/run'):
            base = base[:-len('/run')]
        return base

    def _run_url(self):
        return f'{self._service_base_url()}/run'

    def _jobs_url(self, job_id):
        return f'{self._service_base_url()}/jobs/{job_id}'

    def _get_job(self, job_id):
        """GET /jobs/{id}. Returns (status_code, json|None). Never raises —
        transport errors are transient and retried on the next cron tick."""
        Param = self.env['ir.config_parameter'].sudo()
        token = (Param.get_param('mm_tasker.backend_token') or '').strip()
        # Status reads hit the service's in-memory job store and return
        # instantly. Cap the timeout low (independent of backend_timeout, which
        # is sized for the long /run call) so a hung service can't stall the
        # poll cron — which holds a transaction open — for minutes.
        try:
            timeout = min(int(Param.get_param('mm_tasker.backend_timeout', default='120') or 120), 15)
        except (TypeError, ValueError):
            timeout = 15
        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            resp = requests.get(self._jobs_url(job_id), headers=headers, timeout=timeout)
        except requests.RequestException as e:
            _logger.warning('poll job %s failed: %s', job_id, e)
            return 0, None
        if resp.status_code >= 400:
            return resp.status_code, None
        try:
            return 200, resp.json()
        except ValueError:
            return resp.status_code, None

    @api.model
    def ingest_job(self, run):
        """Poll one run's job and, if terminal, ingest. Returns True when the
        run reaches a terminal state (scored/error), False to retry later."""
        if not run.external_job_id:
            return False
        code, data = self._get_job(run.external_job_id)
        run.write({'last_polled_at': fields.Datetime.now()})
        if code == 404:
            run.write({
                'state': 'error', 'ext_state': 'error',
                'error_message': 'Service job expired or not found.',
                'finished_at': fields.Datetime.now(),
            })
            return True
        if not data:
            return False  # transient (network / 5xx) — try again next tick
        status = (data.get('status') or '').lower()
        if status in ('queued', 'running'):
            run.write({'ext_state': 'running'})
            return False
        if status == 'error':
            run.write({
                'state': 'error', 'ext_state': 'error',
                'error_message': (data.get('error') or 'Service reported error')[:2000],
                'finished_at': fields.Datetime.now(),
            })
            return True
        if status == 'done':
            self._ingest_job_result(run, data)
            return True
        return False

    def _ingest_job_result(self, run, data):
        """Back half of _execute_run, fed by an async job result instead of an
        inline call. Reuses _grade_run + _compute_aggregate verbatim, so the
        scoring is byte-for-byte identical to the synchronous path."""
        run.write({
            'response_text': data.get('response_text') or '',
            'tokens_in': int(data.get('tokens_in') or 0),
            'tokens_out': int(data.get('tokens_out') or 0),
            'state': 'judging',
            'ext_state': 'done',
        })
        for f in (data.get('output_files') or []):
            self.env['mm.tasker.run.output'].create({
                'run_id': run.id,
                'name': f.get('name') or 'output',
                'file': f.get('file'),
                'sha256': f.get('sha256') or '',
                'size_bytes': f.get('size_bytes', 0),
            })
        self._grade_run(run, backend_scores=data.get('scores'))
        self._compute_aggregate(run)
        run.write({'state': 'scored', 'finished_at': fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Provider adapters (REAL CALLS GO HERE)
    # ------------------------------------------------------------------
    def _call_provider(self, task_id, model_key, prompt, media, rubrics=None,
                       system_prompt=None, idempotency_key=None,
                       run_index=1, runs_total=1):
        """Return {response_text, tokens_in, tokens_out, output_files, scores}.

        `scores` is the new field carrying per-rubric verdicts the backend
        computed against the agent's workspace (probe_*, shell_succeeds_real,
        response_contains, response_regex_present). May be absent / empty —
        `_grade_run` falls back to local Python grading for the rubric types
        that don't need a workspace.

        `run_index` / `runs_total` describe this call's position in the
        per-model batch (1-indexed). The backend uses them to write into
        goku-style run_N/ directories. Defaults of 1/1 keep the signature
        backwards-compatible with callers that still treat dispatch as
        single-shot.

        Routes on `mm_tasker.test_mode`:
          - true  -> deterministic mock (works without a backend; mocks
                     probe scores so test_mode demos cover every type).
          - false -> POST to `mm_tasker.backend_url` with prompt + media
                     + rubrics; reads `scores[]` from the response.
        """
        Param = self.env['ir.config_parameter'].sudo()
        test_mode = self._param_truthy(Param.get_param('mm_tasker.test_mode', default='true'))
        if test_mode:
            return self._call_provider_mock(
                model_key, prompt, media, rubrics,
                run_index=run_index, runs_total=runs_total,
            )
        return self._call_provider_backend(
            task_id, model_key, prompt, media, rubrics, system_prompt, idempotency_key,
            run_index=run_index, runs_total=runs_total,
        )

    @staticmethod
    def _param_truthy(value):
        return (value or '').strip().lower() in ('1', 'true', 'yes', 'on')

    def _call_provider_mock(self, model_key, prompt, media, rubrics=None,
                            run_index=1, runs_total=1):
        media_summary = self._summarize_media(media)
        prompt_preview = (prompt or '').strip()[:200]
        # Stamp the batch position into the mocked response so test_mode
        # smoke tests can confirm the wizard's runs-per-model count
        # actually fans out to N distinct executions.
        response_text = (
            f"[MOCK {model_key} · run {run_index}/{runs_total}] "
            f"Received prompt: {prompt_preview}...\n\n"
            f"Media: {media_summary}\n\n"
            f"This is a deterministic stub response. Set "
            f"mm_tasker.test_mode=false and mm_tasker.backend_url to hit "
            f"the real backend."
        )
        # Mock probe/shell scores so test_mode exercises the new scores[]
        # path end-to-end. Response-text rubrics (response_contains,
        # response_regex_present) intentionally omitted — they fall through
        # to local _grade_single so that codepath stays tested in test_mode.
        # response_criteria / response_not_criteria are left for Run Judge.
        PROBE_TYPES = {
            'probe_file_exists', 'probe_dir_exists',
            'probe_file_contains', 'shell_succeeds_real',
        }
        mock_scores = []
        for r in (rubrics or []):
            rtype = (r.rubric_type or '').strip()
            if rtype not in PROBE_TYPES:
                continue
            mock_scores.append({
                'rubric_number': r.number,
                'passed': True,
                'triggered': False,
                'judged_by': 'probe',
                'rationale': f'[MOCK] {rtype} auto-passed in test_mode.',
                'awarded_points': r.points or 0,
            })

        return {
            'response_text': response_text,
            'tokens_in': len(prompt) // 4,
            'tokens_out': len(response_text) // 4,
            'output_files': [],
            'scores': mock_scores,
        }

    def _call_provider_backend(self, task_id, model_key, prompt, media, rubrics=None,
                               system_prompt=None, idempotency_key=None,
                               run_index=1, runs_total=1):
        Param = self.env['ir.config_parameter'].sudo()
        url = (Param.get_param('mm_tasker.backend_url') or '').strip()
        if not url:
            raise UserError(_(
                'mm_tasker.backend_url is not configured. '
                'Set it in Settings → Technical → System Parameters, '
                'or flip mm_tasker.test_mode back to true.'
            ))
        payload = {
            'task_id': str(task_id or ''),
            'model': model_key,
            # Batch position so the goku backend can write into
            # run_<run_index>/ rather than overwriting a single
            # per-model directory.
            'run_index': int(run_index or 1),
            'runs_total': int(runs_total or 1),
            'system_prompt': system_prompt or 'You are a helpful assistant.',
            'user_prompt': prompt or '',
            'media': [
                {
                    'name': m.name or '',
                    'filename': m.filename or m.name or '',
                    'kind': m.kind or 'other',
                    'mime_type': m.mime_type or '',
                    'data_b64': (m.file or b'').decode() if isinstance(m.file, bytes) else (m.file or ''),
                }
                for m in (media or [])
            ],
            # The backend grades probe/shell rubrics against the workspace it
            # built for the agent. Sending parsed fields + raw_json so the
            # scorer can use whichever it prefers (raw_json preserves keys
            # mm_tasker doesn't model — e.g. paths, pattern, needles).
            'rubrics': [
                {
                    'number': r.number,
                    'type': r.rubric_type or '',
                    'category': r.category or '',
                    'importance': r.importance or '',
                    'points': r.points or 0,
                    'criterion': r.criterion or '',
                    'raw_json': r.raw_json or '',
                }
                for r in (rubrics or [])
            ],
        }
        if idempotency_key:
            # Reserved for retry / future async flow — backend treats repeat
            # calls with the same key as a re-fetch of the original result.
            payload['idempotency_key'] = idempotency_key
        data = self._post_backend(url, payload)
        return {
            'response_text': data.get('response_text') or '',
            'tokens_in': int(data.get('tokens_in') or 0),
            'tokens_out': int(data.get('tokens_out') or 0),
            'output_files': data.get('output_files') or [],
            'scores': data.get('scores') or [],
        }

    # ------------------------------------------------------------------
    # Re-grade — re-score an existing run with new rubrics
    # ------------------------------------------------------------------
    def call_regrade(self, run, rubrics):
        """POST to goku /regrade for an existing run.

        Returns a list[dict] of score envelopes in the same shape /run
        returns (rubric_number, passed, triggered, judged_by, rationale,
        awarded_points), suitable for feeding straight into
        _row_from_backend_score.

        Mock path (test_mode=true) doesn't hit the network — returns a
        deterministic pass per non-LLM rubric so the regrade flow can
        be exercised end-to-end without goku running.
        """
        Param = self.env['ir.config_parameter'].sudo()
        test_mode = self._param_truthy(Param.get_param('mm_tasker.test_mode', default='true'))
        if test_mode:
            return self._call_regrade_mock(run, rubrics)

        backend_url = (Param.get_param('mm_tasker.backend_url') or '').strip()
        if not backend_url:
            raise UserError(_(
                'mm_tasker.backend_url is not configured. '
                'Set it in System Parameters or flip mm_tasker.test_mode to true.'
            ))
        # Derive /regrade URL from backend_url by swapping the trailing
        # /run path. Keeps the user from having to configure a second
        # URL for what's the same goku service.
        if backend_url.rstrip('/').endswith('/run'):
            regrade_url = backend_url.rstrip('/')[:-len('/run')] + '/regrade'
        else:
            regrade_url = backend_url.rstrip('/') + '/regrade'

        payload = {
            'task_id': str(run.task_id.id),
            'run_index': int(run.run_index or 1),
            'model': run.model_key,
            'rubrics': [
                {
                    'number': r.number,
                    'type': r.rubric_type or '',
                    'category': r.category or '',
                    'importance': r.importance or '',
                    'points': r.points or 0,
                    'criterion': r.criterion or '',
                    'raw_json': r.raw_json or '',
                }
                for r in rubrics
            ],
        }
        data = self._post_backend(regrade_url, payload)
        return data.get('scores') or []

    def _call_regrade_mock(self, run, rubrics):
        """Test-mode regrade — return a canned pass per rubric so the
        full mm_tasker re-grade flow (score replacement, aggregate
        recompute, chatter) is exercisable without goku."""
        # Mirror the goku-side mock score shape exactly.
        _LLM_JUDGE_TYPES = ('response_criteria', 'response_not_criteria')
        return [
            {
                'rubric_number': r.number,
                'passed': True,
                'triggered': False,
                'judged_by': 'probe' if (r.rubric_type or '').startswith(('probe_', 'shell_'))
                              else 'regex' if r.rubric_type == 'response_regex_present'
                              else 'probe',
                'rationale': f'[MOCK regrade] {r.rubric_type} auto-passed.',
                'awarded_points': r.points if (r.points or 0) > 0 else 0,
            }
            for r in rubrics
            if r.rubric_type not in _LLM_JUDGE_TYPES
        ]

    def _post_backend(self, url, payload):
        """Shared HTTP POST with timeout/auth/error handling.

        Used by both Run Models (`_call_provider_backend`) and Run Judge
        (`_call_judge`). Returns parsed JSON body, raises UserError on any
        transport / decode failure.
        """
        Param = self.env['ir.config_parameter'].sudo()
        token = (Param.get_param('mm_tasker.backend_token') or '').strip()
        try:
            timeout = int(Param.get_param('mm_tasker.backend_timeout', default='120') or 120)
        except (TypeError, ValueError):
            timeout = 120

        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.Timeout:
            _logger.exception('mm_tasker backend timeout after %ss (%s)', timeout, url)
            raise UserError(_('Backend request timed out after %s seconds.') % timeout)
        except requests.ConnectionError as e:
            _logger.exception('mm_tasker backend connection error: %s', e)
            raise UserError(_('Could not connect to backend: %s') % e)
        except requests.RequestException as e:
            _logger.exception('mm_tasker backend request failed: %s', e)
            raise UserError(_('Backend request failed: %s') % e)

        if resp.status_code >= 400:
            _logger.error('mm_tasker backend HTTP %s: %s', resp.status_code, resp.text[:1000])
            raise UserError(_('Backend returned HTTP %(code)s: %(body)s') % {
                'code': resp.status_code, 'body': resp.text[:500],
            })

        try:
            return resp.json()
        except ValueError:
            _logger.error('mm_tasker backend returned non-JSON: %s', resp.text[:1000])
            raise UserError(_('Backend response was not valid JSON.'))

    @staticmethod
    def _summarize_media(media):
        if not media:
            return 'no media'
        counts = {}
        for m in media:
            counts[m.kind or 'other'] = counts.get(m.kind or 'other', 0) + 1
        return ', '.join(f'{n} {k}' for k, n in sorted(counts.items()))

    # ------------------------------------------------------------------
    # Grading
    # ------------------------------------------------------------------
    def _grade_run(self, run, backend_scores=None):
        """Grade every rubric on the parent task; create one mm.tasker.run.score row each.

        Sources of truth, in priority order:
          1. `backend_scores` (probe_*, shell_succeeds_real, and any other
             rubric the backend chose to grade against the workspace).
          2. Local `_grade_single` for rubric types that don't need a
             workspace — response_contains, response_regex_present.
          3. Placeholder for response_criteria / response_not_criteria;
             `judge_runs` fills these later when the user clicks Run Judge.

        The backend's verdict wins whenever it's present, even for rubric
        types mm_tasker could grade locally — keeps a single source of
        truth and avoids two implementations drifting apart.
        """
        Score = self.env['mm.tasker.run.score']

        # Index backend scores by rubric number for O(1) lookup.
        scores_by_num = {}
        for s in (backend_scores or []):
            num = s.get('rubric_number')
            if num is not None:
                scores_by_num[num] = s

        for rubric in run.task_id.rubric_ids:
            bs = scores_by_num.get(rubric.number)
            if bs:
                row = self._row_from_backend_score(run, rubric, bs)
            else:
                rtype = (rubric.rubric_type or '').strip()
                if rtype in ('response_criteria', 'response_not_criteria'):
                    # Don't pre-create placeholder rows for LLM-judge rubrics.
                    # judge_runs() creates them when the user clicks Run Judge.
                    # Pre-creating them polluted the LLM Judge tab with rows
                    # that looked like real verdicts but were just "pending".
                    continue
                passed, triggered, judged_by, rationale = self._grade_single(run, rubric)
                row = {
                    'run_id': run.id,
                    'rubric_id': rubric.id,
                    'passed': passed,
                    'triggered': triggered,
                    'awarded_points': self._awarded_points(rubric, passed, triggered),
                    'judged_by': judged_by,
                    'judge_rationale': rationale or '',
                }
            Score.create(row)

    def _row_from_backend_score(self, run, rubric, bs):
        """Map a backend `scores[]` entry to a mm.tasker.run.score row dict.

        Trusts the backend's `awarded_points` if present (so goku is the
        formula's source of truth). Falls back to mm_tasker's local formula
        if absent.
        """
        passed = bool(bs.get('passed'))
        triggered = bool(bs.get('triggered'))
        judged_by = bs.get('judged_by') or 'probe'
        awarded_raw = bs.get('awarded_points')
        if awarded_raw is None:
            awarded = self._awarded_points(rubric, passed, triggered)
        else:
            try:
                awarded = int(awarded_raw)
            except (TypeError, ValueError):
                awarded = self._awarded_points(rubric, passed, triggered)

        row = {
            'run_id': run.id,
            'rubric_id': rubric.id,
            'passed': passed,
            'triggered': triggered,
            'awarded_points': awarded,
            'judged_by': judged_by,
            'judge_rationale': str(bs.get('rationale') or ''),
        }
        # LLM-judge scores carry extra metadata. The backend may include
        # these even on non-judge rows; only attach when judged_by says so.
        if judged_by == 'llm_judge':
            row['judge_confidence'] = float(bs.get('judge_confidence') or 0.0)
            row['judge_raw_response'] = str(bs.get('judge_raw_response') or '')
            row['judge_tokens_in'] = int(bs.get('judge_tokens_in') or 0)
            row['judge_tokens_out'] = int(bs.get('judge_tokens_out') or 0)
        return row

    @staticmethod
    def _awarded_points(rubric, passed, triggered):
        """Per-rubric awarded points. Matches the aggregate formula.

        Positive rubric: full points if passed, else 0.
        Negative rubric: -|points| if triggered, else 0.
        """
        points = rubric.points or 0
        if points > 0:
            return points if passed else 0
        if points < 0:
            return -abs(points) if triggered else 0
        return 0

    def _grade_single(self, run, rubric):
        """Return (passed, triggered, judged_by, rationale).

        Pure-python deterministic graders for v1. LLM-judge items currently
        fall back to a simple heuristic and emit a low-confidence rationale.
        """
        rtype = (rubric.rubric_type or '').strip()
        raw = self._safe_json(rubric.raw_json)

        if rtype in ('probe_file_exists', 'probe_dir_exists', 'probe_file_contains', 'shell_succeeds_real'):
            # We only land here when the backend's response did NOT include
            # a scores[] entry for this rubric number. Probe/shell rubrics
            # must be graded against the agent's workspace, which only the
            # backend has access to — there's no way to grade them locally.
            return (False, False, 'not_applicable',
                    f'Probe rubric "{rtype}" was not graded: the backend did not '
                    f'return a scores[] entry for rubric #{rubric.number}. '
                    f'Either configure mm_tasker.backend_url to a service that '
                    f'returns per-rubric scores (see the mm_tasker README), '
                    f'or remove probe-type rubrics from this task.')

        response = run.response_text or ''

        if rtype == 'response_contains':
            needles = [n.strip().lower() for n in (raw.get('needles') or []) if n and n.strip()]
            if not needles:
                return (False, False, 'probe', 'No needles configured.')
            low = response.lower()
            hits = [n for n in needles if n in low]
            passed = len(hits) == len(needles)
            return (passed, False, 'probe',
                    f'matched {len(hits)}/{len(needles)} needles: {hits}')

        if rtype == 'response_regex_present':
            pattern = raw.get('pattern') or ''
            if not pattern:
                return (False, False, 'regex', 'No pattern configured.')
            try:
                m = re.search(pattern, response, re.MULTILINE)
            except re.error as e:
                return (False, False, 'regex', f'invalid regex: {e}')
            return (bool(m), False, 'regex',
                    f"matched {repr(m.group(0)[:60]) if m else 'nothing'}")

        if rtype in ('response_criteria', 'response_not_criteria'):
            # Placeholder — LLM judge fills these in via judge_runs(). The
            # placeholder is unscored so it doesn't skew aggregates before
            # the judge runs.
            return (False, False, 'llm_judge',
                    'Pending LLM judge — click "Run Judge" to score this rubric.')

        return (False, False, 'not_applicable', f'unknown rubric type: {rtype}')

    # ------------------------------------------------------------------
    # LLM Judge (real call, no heuristic)
    # ------------------------------------------------------------------
    @api.model
    def judge_runs(self, runs):
        """Run the LLM judge over `response_criteria` / `response_not_criteria`
        rubrics on every run in `runs`. Skips runs in `error` state.

        Idempotent: clears existing `judged_by='llm_judge'` scores per run
        before re-creating them, so re-running the judge gives fresh
        verdicts (and fresh token counts for cost tracking).

        After each run, calls `_compute_aggregate` so the run's pass/score
        fields reflect the new judge verdicts immediately.
        """
        Param = self.env['ir.config_parameter'].sudo()
        judge_model = (Param.get_param('mm_tasker.judge_model_key',
                                        default='claude_opus_4_7') or '').strip()
        if not judge_model:
            raise UserError(_('mm_tasker.judge_model_key is not configured.'))

        Score = self.env['mm.tasker.run.score']
        for run in runs:
            if run.state == 'error':
                _logger.info('Skipping judge for run %s (state=error)', run.id)
                continue

            # Idempotency: drop any prior LLM-judge rows on this run.
            run.score_ids.filtered(lambda s: s.judged_by == 'llm_judge').unlink()

            for rubric in run.task_id.rubric_ids:
                rtype = (rubric.rubric_type or '').strip()
                if rtype not in ('response_criteria', 'response_not_criteria'):
                    continue
                negate = (rtype == 'response_not_criteria')

                try:
                    verdict = self._call_judge(
                        judge_model=judge_model,
                        criterion=rubric.criterion or '',
                        response_text=run.response_text or '',
                        negate=negate,
                    )
                except UserError as e:
                    # Captured as a row with no points awarded so the user
                    # sees what went wrong without aborting the whole batch.
                    Score.create({
                        'run_id': run.id,
                        'rubric_id': rubric.id,
                        'passed': False,
                        'triggered': False,
                        'awarded_points': 0,
                        'judged_by': 'llm_judge',
                        'judge_rationale': f'Judge call failed: {e}',
                        'judge_confidence': 0.0,
                        'judge_raw_response': '',
                        'judge_tokens_in': 0,
                        'judge_tokens_out': 0,
                    })
                    continue

                judge_passed = verdict['passed']
                if negate:
                    # Judge was asked "did the agent VIOLATE?". passed=true
                    # from the judge means the agent triggered the bad
                    # behaviour — score-wise this is `triggered=True` and
                    # no points awarded.
                    triggered = judge_passed
                    actually_passed = not judge_passed
                else:
                    triggered = False
                    actually_passed = judge_passed

                Score.create({
                    'run_id': run.id,
                    'rubric_id': rubric.id,
                    'passed': actually_passed,
                    'triggered': triggered,
                    'awarded_points': self._awarded_points(rubric, actually_passed, triggered),
                    'judged_by': 'llm_judge',
                    'judge_rationale': verdict['rationale'],
                    'judge_confidence': verdict['confidence'],
                    'judge_raw_response': verdict['raw'],
                    'judge_tokens_in': verdict['tokens_in'],
                    'judge_tokens_out': verdict['tokens_out'],
                })

            self._compute_aggregate(run)
        return True

    def _call_judge(self, judge_model, criterion, response_text, negate):
        """Single judge call. Routes on mm_tasker.test_mode.

        Returns dict: {passed, rationale, confidence, tokens_in,
        tokens_out, raw}.
        """
        Param = self.env['ir.config_parameter'].sudo()
        test_mode = self._param_truthy(Param.get_param('mm_tasker.test_mode', default='true'))

        if test_mode:
            return self._call_judge_mock(criterion, response_text, negate)

        if negate:
            system_prompt = (
                'You are a strict grader. Decide whether the agent RESPONSE '
                'VIOLATES the given CRITERION. Return ONLY a JSON object '
                'with keys: passed (bool — true means the criterion was '
                'VIOLATED), rationale (short string), confidence (float 0-1).'
            )
        else:
            system_prompt = (
                'You are a strict grader. Decide whether the agent RESPONSE '
                'SATISFIES the given CRITERION. Return ONLY a JSON object '
                'with keys: passed (bool), rationale (short string), '
                'confidence (float 0-1).'
            )
        user_prompt = f'CRITERION: {criterion}\n\nRESPONSE: {response_text}'

        url = (Param.get_param('mm_tasker.judge_backend_url') or '').strip() \
            or (Param.get_param('mm_tasker.backend_url') or '').strip()
        if not url:
            raise UserError(_(
                'No judge backend URL configured. Set mm_tasker.judge_backend_url '
                '(or mm_tasker.backend_url as fallback), or flip test_mode back to true.'
            ))

        envelope = self._post_backend(url, {
            'model': judge_model,
            'system_prompt': system_prompt,
            'user_prompt': user_prompt,
            'media': [],
        })

        raw_text = envelope.get('response_text') or ''
        try:
            parsed = json.loads(raw_text)
        except (ValueError, TypeError):
            parsed = {'passed': False, 'rationale': 'Judge returned non-JSON', 'confidence': 0.0}

        return {
            'passed': bool(parsed.get('passed')),
            'rationale': str(parsed.get('rationale') or ''),
            'confidence': float(parsed.get('confidence') or 0.0),
            'tokens_in': int(envelope.get('tokens_in') or 0),
            'tokens_out': int(envelope.get('tokens_out') or 0),
            'raw': raw_text,
        }

    def _call_judge_mock(self, criterion, response_text, negate):
        """Deterministic stub used while test_mode=true."""
        length = len(response_text or '')
        passed = length > 50
        rationale = (
            f'Mock judge — response length {length} '
            f'{"≥" if passed else "<"} 50 chars (negate={negate}).'
        )
        raw = json.dumps({'passed': passed, 'rationale': rationale, 'confidence': 0.5})
        return {
            'passed': passed,
            'rationale': rationale,
            'confidence': 0.5,
            'tokens_in': max(20, len(criterion) // 4),
            'tokens_out': 30,
            'raw': raw,
        }

    # ------------------------------------------------------------------
    # Aggregate scoring (exact formula from the annotation guideline)
    # ------------------------------------------------------------------
    def _compute_aggregate(self, run):
        positives = run.score_ids.filtered(lambda s: s.rubric_points > 0)
        negatives = run.score_ids.filtered(lambda s: s.rubric_points < 0)

        awarded = sum(s.rubric_points for s in positives if s.passed) \
            - sum(abs(s.rubric_points) for s in negatives if s.triggered)
        max_total = sum(s.rubric_points for s in positives)
        raw_score = (awarded / max_total) if max_total else 0.0
        per_task_score = max(0.0, min(1.0, raw_score))

        mandatory_pos_ok = all(
            s.passed for s in positives if (s.rubric_importance or '').lower() == 'mandatory'
        )
        mandatory_neg_clean = not any(
            s.triggered for s in negatives if (s.rubric_importance or '').lower() == 'mandatory'
        )
        passed = mandatory_pos_ok and mandatory_neg_clean

        run.write({
            'awarded': awarded,
            'max_total': max_total,
            'raw_score': raw_score,
            'per_task_score': per_task_score,
            'passed': passed,
        })

    # ------------------------------------------------------------------
    @staticmethod
    def _safe_json(raw_text):
        try:
            return json.loads(raw_text or '{}')
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @api.model
    def _judge_model_key(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'mm_tasker.judge_model_key', default='claude_opus_4_7',
        )
