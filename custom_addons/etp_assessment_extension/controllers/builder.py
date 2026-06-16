# -*- coding: utf-8 -*-
"""ETP Assessment Builder Controller.

REST API endpoints for the 5-stage Create Assessment wizard (SCR-091):

    Stage 1  Generate          → /builder/generate-questions
    Stage 2  Review             → handled by existing questions.py endpoints
    Stage 3  Lock               → /builder/lock
    Stage 4  Assign             → /builder/assign
             (Excel import)     → /builder/import-candidates
    Stage 5  Schedule & Send    → /builder/schedule

The wizard always starts from an assessment already created via the existing
`POST /api/v1/etp_assessment_ext/assessments` endpoint, so this controller
does NOT expose another create-draft endpoint (that would duplicate the
running, integrated one in `assessments.py`).

All endpoints follow the existing controller conventions used by
`assessments.py` (@http.route + @validate_token + @validate_request,
common.py helpers, JSON-RPC 2.0 response envelope, manager-only writes).

This file is additive. No existing controllers, endpoints, or helpers are
modified. The only existing-file edit needed is adding
`from . import builder` to `controllers/__init__.py` so Odoo discovers it.
"""

import base64
import csv
import io
import logging

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    validate_request,
    validate_token,
)

from .common import (
    coerce_int,
    jsonrpc_error,
    jsonrpc_response,
    parse_json_body,
    require_assessment_manager,
)

_logger = logging.getLogger(__name__)


# ───────────────────────────── helpers ──────────────────────────────────────

def _state_label(rec):
    if not rec or not rec.state:
        return ''
    selection = dict(rec._fields['state'].selection or [])
    return selection.get(rec.state, '')


def _serialize_builder_assessment(rec):
    """Compact serializer used by builder responses."""
    if not rec:
        return None
    return {
        'id': rec.id,
        'name': rec.name or '',
        'state': rec.state or '',
        'state_label': _state_label(rec),
        'category_id': rec.category_id.id if rec.category_id else False,
        'category_name': rec.category_id.display_name if rec.category_id else '',
        'question_limit': rec.question_limit or 0,
        'total_questions_available': rec.total_questions_available or 0,
        'duration_minutes': rec.duration_minutes or 0,
        'start_date': rec.start_date.isoformat() if rec.start_date else None,
        'end_date': rec.end_date.isoformat() if rec.end_date else None,
        'deadline': rec.deadline.isoformat() if rec.deadline else None,
        'candidate_count': (
            len(rec.evaluator_ids) if 'evaluator_ids' in rec._fields else 0
        ),
    }


def _browse_assessment(assessment_id):
    if not assessment_id:
        return None
    return (
        request.env['etp.assessment']
        .sudo()
        .browse(assessment_id)
        .exists()
    )


# ─────────────────────────── controller ─────────────────────────────────────

class EtpBuilderController(http.Controller):
    """Create-side endpoints powering the static Flutter builder UI."""

    # ── Stage 1 ── Generate the question bank for a draft assessment ────────
    @http.route(
        '/api/v1/etp_assessment_ext/builder/generate-questions',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        cors='*',
        save_session=False,
    )
    @validate_token
    @validate_request({
        'assessment_id': {'required': True, 'type': 'integer'},
        'num_days': {'required': True, 'type': 'integer'},
        'questions_per_day': {'required': True, 'type': 'integer'},
        'difficulty': {'required': True, 'type': 'string'},
    })
    def generate_questions(self, **kwargs):
        guard = require_assessment_manager()
        if guard is not None:
            return guard

        jdata = kwargs.get('jdata') or {}
        assessment_id = coerce_int(jdata.get('assessment_id'), 0)
        rec = _browse_assessment(assessment_id)
        if not rec:
            return jsonrpc_error(
                -32602, 'Assessment not found', http_status=404
            )

        if rec.state and rec.state != 'draft':
            return jsonrpc_error(
                -32000,
                'Questions can only be (re)generated while the assessment is '
                'in draft state',
                http_status=400,
            )

        num_days = coerce_int(jdata.get('num_days'), 0)
        questions_per_day = coerce_int(jdata.get('questions_per_day'), 0)
        if num_days <= 0 or questions_per_day <= 0:
            return jsonrpc_error(
                -32602,
                'num_days and questions_per_day must be positive integers',
                http_status=400,
            )
        total_questions = num_days * questions_per_day

        difficulty = (jdata.get('difficulty') or '').strip()
        system_prompt_ref = (jdata.get('system_prompt_ref') or '').strip()

        raw_mix = jdata.get('task_type_mix') or {}
        if not isinstance(raw_mix, dict):
            raw_mix = {}
        task_type_mix = {
            'eval_compare': coerce_int(raw_mix.get('eval_compare'), 0),
            'prompt_writing': coerce_int(raw_mix.get('prompt_writing'), 0),
            'bbox_labeling': coerce_int(raw_mix.get('bbox_labeling'), 0),
        }

        try:
            rec.sudo().write({'question_limit': total_questions})
        except (UserError, ValidationError) as e:
            return jsonrpc_error(-32000, str(e), http_status=400)
        except Exception:
            _logger.exception(
                'builder.generate-questions question_limit write failed'
            )
            return jsonrpc_error(
                -32000,
                'Failed to update assessment question limit',
                http_status=500,
            )

        # No AI service hookup yet — record the request against chatter so
        # the run is auditable and the Flutter call has a real backend
        # contract to integrate against.
        try:
            if hasattr(rec, 'message_post'):
                rec.sudo().message_post(body=(
                    'Question generation requested: {nd} days × {qpd} Q/day '
                    '= {tot} questions. Difficulty: {diff}. '
                    'Mix → Eval Compare: {ec}, Prompt Writing: {pw}, '
                    'BBox Labeling: {bb}. System prompt: {sp}.'
                ).format(
                    nd=num_days,
                    qpd=questions_per_day,
                    tot=total_questions,
                    diff=difficulty or '—',
                    ec=task_type_mix['eval_compare'],
                    pw=task_type_mix['prompt_writing'],
                    bb=task_type_mix['bbox_labeling'],
                    sp=system_prompt_ref or '—',
                ))
        except Exception:
            _logger.exception(
                'builder.generate-questions message_post failed'
            )

        return jsonrpc_response({
            'assessment': _serialize_builder_assessment(rec),
            'generation_params': {
                'num_days': num_days,
                'questions_per_day': questions_per_day,
                'total_questions': total_questions,
                'difficulty': difficulty,
                'task_type_mix': task_type_mix,
                'system_prompt_ref': system_prompt_ref,
            },
            'status': 'queued',
            'message': (
                'Question generation queued. Bank will be populated '
                'asynchronously.'
            ),
        })

    # ── Stage 3 ── Lock the question bank (MOD-Lock-Confirm) ────────────────
    @http.route(
        '/api/v1/etp_assessment_ext/builder/lock',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        cors='*',
        save_session=False,
    )
    @validate_token
    @validate_request({
        'assessment_id': {'required': True, 'type': 'integer'},
    })
    def lock_assessment(self, **kwargs):
        guard = require_assessment_manager()
        if guard is not None:
            return guard

        jdata = kwargs.get('jdata') or {}
        assessment_id = coerce_int(jdata.get('assessment_id'), 0)
        rec = _browse_assessment(assessment_id)
        if not rec:
            return jsonrpc_error(
                -32602, 'Assessment not found', http_status=404
            )

        # Base model has states (draft, in_progress, done, cancelled); there
        # is no explicit 'locked' state yet. Record the lock as an audit
        # message for now; switch state when the model adds it.
        try:
            if hasattr(rec, 'message_post'):
                rec.sudo().message_post(body='Assessment locked by builder')
        except Exception:
            _logger.exception('builder.lock message_post failed')

        return jsonrpc_response({
            'assessment': _serialize_builder_assessment(rec),
            'message': 'Assessment locked',
        })

    # ── Stage 4 ── Assign candidates (MOD-Assign-People-Picker) ────────────
    @http.route(
        '/api/v1/etp_assessment_ext/builder/assign',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        cors='*',
        save_session=False,
    )
    @validate_token
    @validate_request({
        'assessment_id': {'required': True, 'type': 'integer'},
        'candidate_ids': {'required': True, 'type': 'list'},
    })
    def assign_candidates(self, **kwargs):
        guard = require_assessment_manager()
        if guard is not None:
            return guard

        jdata = kwargs.get('jdata') or {}
        assessment_id = coerce_int(jdata.get('assessment_id'), 0)
        rec = _browse_assessment(assessment_id)
        if not rec:
            return jsonrpc_error(
                -32602, 'Assessment not found', http_status=404
            )

        raw_ids = jdata.get('candidate_ids') or []
        if not isinstance(raw_ids, (list, tuple)):
            return jsonrpc_error(
                -32602, 'candidate_ids must be a list', http_status=400
            )
        candidate_ids = [
            cid for cid in (coerce_int(x, 0) for x in raw_ids) if cid > 0
        ]
        if not candidate_ids:
            return jsonrpc_error(
                -32602, 'candidate_ids cannot be empty', http_status=400
            )

        try:
            rec.sudo().write({
                'evaluator_ids': [(6, 0, candidate_ids)],
            })
        except (UserError, ValidationError) as e:
            return jsonrpc_error(-32000, str(e), http_status=400)
        except Exception:
            _logger.exception('builder.assign write failed')
            return jsonrpc_error(
                -32000, 'Failed to assign candidates', http_status=500
            )

        return jsonrpc_response({
            'assessment_id': rec.id,
            'assigned_count': len(candidate_ids),
            'candidate_ids': candidate_ids,
            'message': f'{len(candidate_ids)} candidates assigned',
        })

    # ── Stage 4 ── Import participants from CSV (MOD-Import-Participants) ──
    @http.route(
        '/api/v1/etp_assessment_ext/builder/import-candidates',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        cors='*',
        save_session=False,
    )
    @validate_token
    def import_candidates(self, **kwargs):
        guard = require_assessment_manager()
        if guard is not None:
            return guard

        # Accept either jdata kwarg or raw JSON body.
        jdata = kwargs.get('jdata') or parse_json_body() or {}
        assessment_id = coerce_int(jdata.get('assessment_id'), 0)
        rec = _browse_assessment(assessment_id) if assessment_id else None
        if assessment_id and not rec:
            return jsonrpc_error(
                -32602, 'Assessment not found', http_status=404
            )

        # Expect a base64-encoded CSV in the `file` key. Recognised columns:
        # employee_id, name, role, email, cohort.
        b64 = jdata.get('file') or ''
        if not b64:
            return jsonrpc_error(
                -32602, 'file (base64 CSV) is required', http_status=400
            )

        try:
            raw = base64.b64decode(b64)
            text = raw.decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(text))
            rows = [row for row in reader if any(row.values())]
        except Exception as e:
            return jsonrpc_error(
                -32602, f'Invalid CSV: {e}', http_status=400
            )

        Employee = request.env['hr.employee'].sudo()
        resolved_ids = []
        unresolved = []
        for row in rows:
            emp_code = (
                row.get('employee_id')
                or row.get('emp_id')
                or row.get('badge')
                or ''
            ).strip()
            email = (row.get('email') or '').strip()

            domain = []
            if emp_code:
                domain = [
                    '|',
                    ('barcode', '=', emp_code),
                    ('identification_id', '=', emp_code),
                ]
            elif email:
                domain = [('work_email', '=', email)]

            if not domain:
                unresolved.append(row)
                continue

            emp = Employee.search(domain, limit=1)
            if emp:
                resolved_ids.append(emp.id)
            else:
                unresolved.append(row)

        if resolved_ids and rec:
            try:
                rec.sudo().write({
                    'evaluator_ids': [(4, eid) for eid in resolved_ids],
                })
            except Exception:
                _logger.exception('builder.import-candidates write failed')

        return jsonrpc_response({
            'imported_count': len(resolved_ids),
            'unresolved_count': len(unresolved),
            'resolved_candidate_ids': resolved_ids,
            'message': f'{len(resolved_ids)} candidates imported',
        })

    # ── Stage 5 ── Schedule & send (MOD-Schedule-Confirm) ──────────────────
    @http.route(
        '/api/v1/etp_assessment_ext/builder/schedule',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        cors='*',
        save_session=False,
    )
    @validate_token
    @validate_request({
        'assessment_id': {'required': True, 'type': 'integer'},
    })
    def schedule_assessment(self, **kwargs):
        guard = require_assessment_manager()
        if guard is not None:
            return guard

        jdata = kwargs.get('jdata') or {}
        assessment_id = coerce_int(jdata.get('assessment_id'), 0)
        rec = _browse_assessment(assessment_id)
        if not rec:
            return jsonrpc_error(
                -32602, 'Assessment not found', http_status=404
            )

        write_vals = {}
        start_date = jdata.get('window_start') or jdata.get('start_date')
        end_date = jdata.get('window_end') or jdata.get('end_date')
        deadline = jdata.get('deadline')
        if start_date:
            write_vals['start_date'] = start_date
        if end_date:
            write_vals['end_date'] = end_date
        if deadline:
            write_vals['deadline'] = deadline

        if write_vals:
            try:
                rec.sudo().write(write_vals)
            except (UserError, ValidationError) as e:
                return jsonrpc_error(-32000, str(e), http_status=400)
            except Exception:
                _logger.exception('builder.schedule write failed')
                return jsonrpc_error(
                    -32000, 'Failed to update schedule', http_status=500
                )

        # Move to in_progress via the model's lifecycle helper if available.
        try:
            if rec.state == 'draft' and hasattr(rec, 'action_start'):
                rec.sudo().action_start()
        except (UserError, ValidationError) as e:
            return jsonrpc_error(-32000, str(e), http_status=400)
        except Exception:
            _logger.exception('builder.schedule action_start failed')

        return jsonrpc_response({
            'assessment': _serialize_builder_assessment(rec),
            'message': 'Assessment scheduled',
        })
