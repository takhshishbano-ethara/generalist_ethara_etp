# -*- coding: utf-8 -*-
import json
import logging
import random
import requests
from odoo import models, fields, api
from odoo.exceptions import ValidationError
from .kimi_eval import (
    check_follow_up_relevance_kimi,
    run_evaluation_kimi,
    generate_follow_up_prompt_kimi,
    run_qc_kimi,
    get_kimi_api_key,
)

_logger = logging.getLogger(__name__)

SCORE_SELECTION = [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')]
PREF_SELECTION = [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')]
QC_SELECTION = [('pass', 'Pass'), ('fail', 'Fail')]

kimi_api_key = get_kimi_api_key()


def _check_error(value, value2):
    return abs(value - value2) > 1


class ValorTurn(models.Model):
    _name = 'valor.turn'
    _description = 'Valor Turn'
    _order = 'sequence'

    valor_id = fields.Many2one('valor', string='Task', required=True, ondelete='cascade')
    sequence = fields.Integer(string='Turn', required=True, default=1)

    # --- Prompt & Response ---
    client_prompt = fields.Text(string='Prompt')
    store_client_prompt = fields.Text(string='Suggested Prompt')
    image = fields.Binary("Image", attachment=False)
    image_mime = fields.Char("Image MIME")
    image_handle_id = fields.Char("Image handle_id")
    client_response_a = fields.Text(string='Response A')
    client_response_b = fields.Text(string='Response B')
    is_randomized = fields.Boolean(string='Responses Randomized')

    # --- Ratings: Response A ---
    truthfulness_a = fields.Selection(SCORE_SELECTION, string='Truthfulness A')
    instruction_following_a = fields.Selection(SCORE_SELECTION, string='Instruction Following A')
    writing_quality_a = fields.Selection(SCORE_SELECTION, string='Writing Quality A')
    verbosity_a = fields.Selection(SCORE_SELECTION, string='Verbosity A')
    prompt_correctness_a = fields.Selection(SCORE_SELECTION, string='Prompt Correctness A')
    overall_quality_a = fields.Selection(SCORE_SELECTION, string='Overall Quality A')

    # --- Ratings: Response B ---
    truthfulness_b = fields.Selection(SCORE_SELECTION, string='Truthfulness B')
    instruction_following_b = fields.Selection(SCORE_SELECTION, string='Instruction Following B')
    writing_quality_b = fields.Selection(SCORE_SELECTION, string='Writing Quality B')
    verbosity_b = fields.Selection(SCORE_SELECTION, string='Verbosity B')
    prompt_correctness_b = fields.Selection(SCORE_SELECTION, string='Prompt Correctness B')
    overall_quality_b = fields.Selection(SCORE_SELECTION, string='Overall Quality B')

    # --- AB Preference ---
    ab_preference = fields.Selection(PREF_SELECTION, string='AB Preference')
    ab_comment = fields.Text(string='AB Comment')

    # --- Store (AI evaluation scores) ---
    store_truthfulness_a = fields.Selection(SCORE_SELECTION)
    store_instruction_following_a = fields.Selection(SCORE_SELECTION)
    store_writing_quality_a = fields.Selection(SCORE_SELECTION)
    store_verbosity_a = fields.Selection(SCORE_SELECTION)
    store_prompt_correctness_a = fields.Selection(SCORE_SELECTION)
    store_overall_quality_a = fields.Selection(SCORE_SELECTION)
    store_truthfulness_b = fields.Selection(SCORE_SELECTION)
    store_instruction_following_b = fields.Selection(SCORE_SELECTION)
    store_writing_quality_b = fields.Selection(SCORE_SELECTION)
    store_verbosity_b = fields.Selection(SCORE_SELECTION)
    store_prompt_correctness_b = fields.Selection(SCORE_SELECTION)
    store_overall_quality_b = fields.Selection(SCORE_SELECTION)
    store_ab_preference = fields.Selection(PREF_SELECTION)
    store_ab_comment = fields.Text()

    # --- QC Reason texts ---
    reason_truthfulness_a = fields.Text()
    reason_instruction_following_a = fields.Text()
    reason_writing_quality_a = fields.Text()
    reason_verbosity_a = fields.Text()
    reason_prompt_correctness_a = fields.Text()
    reason_overall_quality_a = fields.Text()
    reason_truthfulness_b = fields.Text()
    reason_instruction_following_b = fields.Text()
    reason_writing_quality_b = fields.Text()
    reason_verbosity_b = fields.Text()
    reason_prompt_correctness_b = fields.Text()
    reason_overall_quality_b = fields.Text()
    reason_ab_preference = fields.Text()
    reason_ab_comment = fields.Text()

    # --- QC Error flags ---
    error_truthfulness_a = fields.Boolean(default=False)
    error_instruction_following_a = fields.Boolean(default=False)
    error_writing_quality_a = fields.Boolean(default=False)
    error_verbosity_a = fields.Boolean(default=False)
    error_prompt_correctness_a = fields.Boolean(default=False)
    error_overall_quality_a = fields.Boolean(default=False)
    error_truthfulness_b = fields.Boolean(default=False)
    error_instruction_following_b = fields.Boolean(default=False)
    error_writing_quality_b = fields.Boolean(default=False)
    error_verbosity_b = fields.Boolean(default=False)
    error_prompt_correctness_b = fields.Boolean(default=False)
    error_overall_quality_b = fields.Boolean(default=False)
    error_ab_preference = fields.Boolean(default=False)
    error_ab_comment = fields.Boolean(default=False)

    # --- QC Status ---
    qc_task_status = fields.Selection(QC_SELECTION, string='QC Status')

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _get_preferred_response(self):
        """Return the response the annotator preferred based on ab_preference."""
        pref = self.ab_preference
        if not pref:
            return ''
        if pref in ('-3', '-2', '-1', '0'):
            return self.client_response_a or ''
        return self.client_response_b or ''

    def _all_turns_sorted(self):
        return self.valor_id.turn_ids.sorted('sequence')

    def _prior_turns(self):
        """All turns up to (but not including) this one, sorted."""
        return self._all_turns_sorted().filtered(lambda t: t.sequence < self.sequence)

    def _build_dialog_history(self):
        """Build list of (prompt, preferred_response) tuples from all prior turns."""
        history = []
        for t in self._prior_turns():
            history.append((t.client_prompt or '', t._get_preferred_response()))
        return history

    def _build_evaluation_inputs(self):
        """Build evaluation_inputs list for all turns up to and including this one."""
        task_id = self.valor_id.task_id
        inputs = []
        for t in self._all_turns_sorted():
            if t.sequence > self.sequence:
                break
            inputs.append({
                'task_id': task_id,
                'prompt': t.client_prompt or '',
                'response_a': t.client_response_a or '',
                'response_b': t.client_response_b or '',
            })
        return inputs

    def _build_history_handle_ids(self):
        """Collect (handle_id, mime) tuples from all prior turns."""
        ids = []
        for t in self._prior_turns():
            ids.append((
                (t.image_handle_id or '').strip() or None,
                (t.image_mime or '').strip() or None,
            ))
        return ids

    def _ensure_router_config(self):
        """Ensure the parent valor record has dialog_id, model_1, model_2."""
        valor = self.valor_id
        if not valor.dialog_id:
            from . import models as valor_models
            url = f"{valor_models.GRAPH_BASE_URL}/llm_annotations_model_router_workstream"
            payload = {
                "access_token": valor_models.genai_api_key,
                "workstream": valor_models.WORKSTREAM,
            }
            conf_response = requests.post(url, json=payload)
            conf_response.raise_for_status()
            router_config = conf_response.json()
            dialog_id = router_config.get("dialog_id", '') or ''
            valor.dialog_id = dialog_id
            valor.model_1 = (router_config.get("model_1") or "").strip() or ""
            valor.model_2 = (router_config.get("model_2") or "").strip() or ""

    # ------------------------------------------------------------------ #
    #  Action: Submit Prompt (generate responses)                         #
    # ------------------------------------------------------------------ #

    def action_submit_prompt(self):
        self.ensure_one()
        from . import models as valor_models
        genai_api_key = valor_models.genai_api_key
        valor = self.valor_id
        seq = self.sequence

        if seq == 1:
            if not self.client_prompt and not self.image:
                raise ValidationError("Prompt or image required")
        else:
            for t in self._all_turns_sorted():
                if t.sequence <= seq and not t.client_prompt:
                    raise ValidationError(f"All {seq} Prompts Required")

        self._ensure_router_config()

        if seq > 1:
            if not (valor.dialog_id or '').strip():
                raise ValidationError(
                    "Dialog session is missing. Please submit Turn 1 first to start a session."
                )
            dialog_history = self._build_dialog_history()
            check_prompt = check_follow_up_relevance_kimi(
                kimi_api_key=kimi_api_key,
                dialog_history=dialog_history,
                follow_up_prompt=self.client_prompt,
            )
            if not (check_prompt.get('is_relevant')):
                raise ValidationError("Please rewrite the prompt")
        else:
            dialog_history = None

        valor._ensure_image_handle_for_turn_record(self, genai_api_key)
        current_handle = (self.image_handle_id or '').strip() or None
        current_mime = (self.image_mime or '').strip() or None
        history_handle_ids = self._build_history_handle_ids() if seq > 1 else None

        self._generate_responses(
            dialog_history=dialog_history,
            current_turn_handle_id=current_handle,
            current_turn_mime=current_mime,
            history_handle_ids=history_handle_ids,
        )

        # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
        try:
            self._run_auto_evaluation()
        except Exception as e:
            _logger.exception('Auto-evaluation (turn %s) failed (non-blocking): %s', seq, e)

    def _generate_responses(self, dialog_history, current_turn_handle_id, current_turn_mime, history_handle_ids):
        """Run LLM response generation synchronously, write to self."""
        from . import models as valor_models
        valor = self.valor_id
        _logger.info('Response generation (turn %s): starting | record=%s', self.sequence, valor.id)
        try:
            response_a_b = valor._generate_response_a_and_b_with_models(
                valor.model_1, valor.model_2,
                prompt=(self.client_prompt or '').strip() or '',
                genai_api_key=valor_models.genai_api_key,
                dialog_id=valor.dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_turn_handle_id,
                current_turn_mime=current_turn_mime,
                history_handle_ids=history_handle_ids,
            )
            if response_a_b:
                errs = response_a_b.get('errors') or {}
                if errs:
                    _logger.warning('Response generation (turn %s) errors: %s', self.sequence, errs)
                response_a = response_a_b.get('response_a', '')
                response_b = response_a_b.get('response_b', '')
                swap = random.random() < 0.5
                if swap:
                    response_a, response_b = response_b, response_a
                self.write({
                    'client_response_a': response_a,
                    'client_response_b': response_b,
                    'is_randomized': swap,
                })
                _logger.info('Response generation (turn %s): written | valor=%s', self.sequence, valor.id)
        except Exception as e:
            _logger.exception('Response generation (turn %s) failed: %s', self.sequence, e)
            raise

    # ------------------------------------------------------------------ #
    #  Auto-Evaluation: Pre-fill ratings + reasons after response gen     #
    # ------------------------------------------------------------------ #

    def _run_auto_evaluation(self):
        """Run Kimi evaluation right after response generation to pre-fill
        all dimension ratings, reasons, AB preference, and AB comment.
        The AI scores are written to BOTH the user-facing rating fields
        (so the annotator sees them pre-filled) and the store_* fields
        (so later error-checking can compare human vs AI)."""
        seq = self.sequence
        _logger.info('Auto-evaluation (turn %s): starting', seq)

        if not self.client_response_a or not self.client_response_b:
            _logger.warning('Auto-evaluation (turn %s): responses missing, skipping', seq)
            return

        evaluation_inputs = self._build_evaluation_inputs()
        try:
            eval_result = run_evaluation_kimi(
                kimi_api_key=kimi_api_key,
                evaluation_inputs=evaluation_inputs,
            )
        except Exception as e:
            _logger.exception('Auto-evaluation Kimi call (turn %s) failed: %s', seq, e)
            return

        if not eval_result:
            _logger.warning('Auto-evaluation (turn %s): no eval_result returned', seq)
            return

        from .models import get_eval_data
        data = get_eval_data(eval_result, idx=seq - 1)
        if not data:
            _logger.warning('Auto-evaluation (turn %s): get_eval_data returned nothing', seq)
            return

        # Build write_vals: pre-fill BOTH human-facing fields and store_* fields
        write_vals = {}
        dims = [
            'truthfulness_a', 'truthfulness_b',
            'instruction_following_a', 'instruction_following_b',
            'writing_quality_a', 'writing_quality_b',
            'verbosity_a', 'verbosity_b',
            'prompt_correctness_a', 'prompt_correctness_b',
            'overall_quality_a', 'overall_quality_b',
        ]
        for dim in dims:
            val = data.get(dim)
            if val:
                write_vals[dim] = val              # Pre-fill human rating field
                write_vals[f'store_{dim}'] = val    # Store AI score for later comparison

        # Pre-fill reasons for all dimensions
        reason_dims = [
            'reason_truthfulness_a', 'reason_truthfulness_b',
            'reason_instruction_following_a', 'reason_instruction_following_b',
            'reason_writing_quality_a', 'reason_writing_quality_b',
            'reason_verbosity_a', 'reason_verbosity_b',
            'reason_prompt_correctness_a', 'reason_prompt_correctness_b',
            'reason_overall_quality_a', 'reason_overall_quality_b',
        ]
        for reason_key in reason_dims:
            val = data.get(reason_key, '')
            if val:
                write_vals[reason_key] = val

        # Pre-fill AB preference and comment from AI
        ab_pref = data.get('ab_preference', '')
        ab_comment = data.get('ab_comment', '')
        if ab_pref:
            write_vals['ab_preference'] = ab_pref
            write_vals['store_ab_preference'] = ab_pref
        if ab_comment:
            write_vals['ab_comment'] = ab_comment
            write_vals['store_ab_comment'] = ab_comment

        if write_vals:
            self.write(write_vals)
            self.valor_id.write({'is_eval_done': True})
            _logger.info('Auto-evaluation (turn %s): pre-filled %d fields', seq, len(write_vals))

    # ------------------------------------------------------------------ #
    #  Action: Evaluate                                                   #
    # ------------------------------------------------------------------ #

    def action_evaluate(self):
        self.ensure_one()
        required_fields = [
            'client_prompt',
            'truthfulness_a', 'truthfulness_b',
            'instruction_following_a', 'instruction_following_b',
            'writing_quality_a', 'writing_quality_b',
            'verbosity_a', 'verbosity_b',
            'prompt_correctness_a', 'prompt_correctness_b',
            'overall_quality_a', 'overall_quality_b',
            'ab_preference', 'ab_comment',
        ]
        for f in required_fields:
            if not getattr(self, f, None):
                raise ValidationError("Please fill all the dimensions")

        for t in self._prior_turns():
            if not t.client_prompt:
                raise ValidationError(f"Prompt is missing for Turn {t.sequence}")

        evaluation_inputs = self._build_evaluation_inputs()
        self._run_eval_and_qc(evaluation_inputs)

    def _run_eval_and_qc(self, evaluation_inputs):
        """Run Kimi evaluation + QC synchronously, write results to self."""
        seq = self.sequence
        try:
            eval_result = run_evaluation_kimi(kimi_api_key=kimi_api_key, evaluation_inputs=evaluation_inputs)
            if not eval_result:
                _logger.warning('Eval (turn %s): no eval_result returned', seq)
                return
            from .models import get_eval_data
            data = get_eval_data(eval_result, idx=seq - 1)
            if not data:
                _logger.warning('Eval (turn %s): get_eval_data returned nothing', seq)
                return
            store_vals = {
                'store_truthfulness_a': data.get('truthfulness_a'),
                'store_truthfulness_b': data.get('truthfulness_b'),
                'store_instruction_following_a': data.get('instruction_following_a'),
                'store_instruction_following_b': data.get('instruction_following_b'),
                'store_writing_quality_a': data.get('writing_quality_a'),
                'store_writing_quality_b': data.get('writing_quality_b'),
                'store_prompt_correctness_a': data.get('prompt_correctness_a'),
                'store_prompt_correctness_b': data.get('prompt_correctness_b'),
                'store_verbosity_a': data.get('verbosity_a'),
                'store_verbosity_b': data.get('verbosity_b'),
                'store_overall_quality_a': data.get('overall_quality_a'),
                'store_overall_quality_b': data.get('overall_quality_b'),
                'store_ab_preference': data.get('ab_preference'),
                'store_ab_comment': data.get('ab_comment'),
            }
            self.write(store_vals)
            self.valor_id.write({'is_eval_done': True})

            qc_vals = self._run_kimi_qc_after_eval(eval_result, eval_data=data)
            if qc_vals:
                self.write(qc_vals)
            _logger.info('Eval (turn %s) and QC completed', seq)
        except Exception as e:
            _logger.exception('Eval (turn %s) failed: %s', seq, e)

    def _run_kimi_qc_after_eval(self, eval_result, eval_data=None):
        """Run Kimi QC synchronously and return write_vals dict, or None on failure."""
        seq = self.sequence
        _logger.info('_run_kimi_qc_after_eval called (turn=%s)', seq)
        if not eval_result or not isinstance(eval_result, list) or len(eval_result) == 0:
            _logger.warning('_run_kimi_qc_after_eval: no eval_result, skipping')
            return None
        idx = min(seq - 1, len(eval_result) - 1)
        ev = eval_result[idx]
        ai_er = ev.get('evaluation_result') or {}
        comparison_ab = ev.get('comparison_ab') or ai_er.get('comparison_ab')

        def _hscore(field_name):
            if eval_data and field_name.startswith('reason_'):
                eval_key = field_name
                return eval_data.get(eval_key, '') or ''
            return getattr(self, field_name, None) or ''

        human_er = {
            'response_a': {
                'instruction_following': {'score': _hscore('instruction_following_a'), 'reason': _hscore('reason_instruction_following_a')},
                'truthfulness': {'score': _hscore('truthfulness_a'), 'reason': _hscore('reason_truthfulness_a')},
                'writing_style': {'score': _hscore('writing_quality_a'), 'reason': _hscore('reason_writing_quality_a')},
                'verbosity': {'score': _hscore('verbosity_a'), 'reason': _hscore('reason_verbosity_a')},
                'prompt_correctness': {'score': _hscore('prompt_correctness_a'), 'reason': _hscore('reason_prompt_correctness_a')},
            },
            'response_b': {
                'instruction_following': {'score': _hscore('instruction_following_b'), 'reason': _hscore('reason_instruction_following_b')},
                'truthfulness': {'score': _hscore('truthfulness_b'), 'reason': _hscore('reason_truthfulness_b')},
                'writing_style': {'score': _hscore('writing_quality_b'), 'reason': _hscore('reason_writing_quality_b')},
                'verbosity': {'score': _hscore('verbosity_b'), 'reason': _hscore('reason_verbosity_b')},
                'prompt_correctness': {'score': _hscore('prompt_correctness_b'), 'reason': _hscore('reason_prompt_correctness_b')},
            },
        }
        qc_inputs_kimi = [{
            'task_id': self.valor_id.task_id,
            'prompt': self.client_prompt or '',
            'response_a': self.client_response_a or '',
            'response_b': self.client_response_b or '',
            'evaluation_result': human_er,
            'comparison_ab': comparison_ab,
            'ab_preference': self.ab_preference,
            'ab_comment': self.ab_comment or '',
        }]

        _logger.info('Kimi QC (turn %s): calling run_qc_kimi...', seq)
        try:
            qc_data = run_qc_kimi(kimi_api_key=kimi_api_key, qc_inputs=qc_inputs_kimi)
        except Exception as e:
            _logger.exception('Kimi QC (turn %s) failed: %s', seq, e)
            return None
        if not qc_data:
            return None

        raw_qc_json = json.dumps({"turn": seq, "record_id": self.valor_id.id, "qc_data": qc_data}, default=str)
        _logger.info("Kimi QC API output (turn=%s, valor=%s): %s", seq, self.valor_id.id, raw_qc_json)

        qc_status = 'pass' if (qc_data[0].get('qc_status') or '') == 'QC_Pass' else 'fail'
        ab_comment_list = qc_data[0].get('ab_comment') or []
        reason_pref = ''
        reason_comment_parts = []
        for line in ab_comment_list:
            if isinstance(line, str):
                if line.startswith('preference_matches_comment: fail - '):
                    reason_pref = line[len('preference_matches_comment: fail - '):].strip()
                elif line.startswith('grounded_in_dimension_ratings: fail - '):
                    reason_comment_parts.append(line[len('grounded_in_dimension_ratings: fail - '):].strip())
                elif line.startswith('grounded_in_responses: fail - '):
                    reason_comment_parts.append(line[len('grounded_in_responses: fail - '):].strip())
                elif line.startswith('ai_flagged: '):
                    flagged_str = line[len('ai_flagged: '):].strip()
                    comment_lower = (self.ab_comment or '').lower()
                    valid_words = [w.strip() for w in flagged_str.split(',') if w.strip() and w.strip().lower() in comment_lower]
                    if valid_words:
                        reason_comment_parts.append('ai_flagged: ' + ', '.join(valid_words))

        write_vals = {
            'qc_task_status': qc_status,
            'reason_ab_preference': reason_pref,
            'reason_ab_comment': '\n'.join(reason_comment_parts) if reason_comment_parts else '',
            'error_ab_comment': bool(reason_comment_parts),
        }

        # Also update the parent's overall qc_task_status
        self.valor_id.write({'qc_task_status': qc_status})

        for _dim, _side in (
            ('truthfulness', 'a'), ('truthfulness', 'b'),
            ('instruction_following', 'a'), ('instruction_following', 'b'),
            ('writing_quality', 'a'), ('writing_quality', 'b'),
            ('verbosity', 'a'), ('verbosity', 'b'),
            ('prompt_correctness', 'a'), ('prompt_correctness', 'b'),
            ('overall_quality', 'a'), ('overall_quality', 'b'),
        ):
            _human = getattr(self, f'{_dim}_{_side}', None)
            _ai = getattr(self, f'store_{_dim}_{_side}', None)
            if _human and _ai:
                write_vals[f'error_{_dim}_{_side}'] = _check_error(int(_human), int(_ai))

        _human_pref = self.ab_preference
        _ai_pref = self.store_ab_preference
        _eval_pref_err = _check_error(int(_human_pref), int(_ai_pref)) if _human_pref and _ai_pref else False
        write_vals['error_ab_preference'] = _eval_pref_err or bool(reason_pref)

        if eval_data:
            for _dim in ('truthfulness', 'instruction_following', 'writing_quality',
                         'verbosity', 'prompt_correctness', 'overall_quality'):
                for _side in ('a', 'b'):
                    write_vals[f'reason_{_dim}_{_side}'] = eval_data.get(f'reason_{_dim}_{_side}', '') or ''

        for _side in ('a', 'b'):
            _if = int(getattr(self, f'instruction_following_{_side}', 0) or 0)
            _tr = int(getattr(self, f'truthfulness_{_side}', 0) or 0)
            _pc = int(getattr(self, f'prompt_correctness_{_side}', 0) or 0)
            _ws = int(getattr(self, f'writing_quality_{_side}', 0) or 0)
            _vb = int(getattr(self, f'verbosity_{_side}', 0) or 0)
            if _if and _tr and _pc and _ws and _vb:
                _agg = _if * 0.25 + _tr * 0.25 + _pc * 0.20 + _ws * 0.15 + _vb * 0.15
                write_vals[f'reason_overall_quality_{_side}'] = (
                    f"Your weighted score: IF({_if})×0.25 + Truth({_tr})×0.25 "
                    f"+ Correctness({_pc})×0.20 + Writing({_ws})×0.15 "
                    f"+ Verbosity({_vb})×0.15 = {_agg:.2f}"
                )

        return write_vals

    # ------------------------------------------------------------------ #
    #  Action: Next Turn                                                  #
    # ------------------------------------------------------------------ #

    def action_next_turn(self):
        """Generate a follow-up prompt suggestion and create the next turn record."""
        self.ensure_one()
        if not self.client_prompt:
            raise ValidationError("Current prompt is required before adding the next turn")

        dialog_history = self._build_dialog_history()
        dialog_history.append((self.client_prompt or '', self._get_preferred_response()))

        result = generate_follow_up_prompt_kimi(
            kimi_api_key=kimi_api_key,
            conversation_turns=dialog_history,
        )
        suggestion = (result.get("follow_up_prompt") or "").strip() if isinstance(result, dict) else ""

        next_seq = self.sequence + 1
        existing = self.valor_id.turn_ids.filtered(lambda t: t.sequence == next_seq)
        if existing:
            existing.write({'store_client_prompt': suggestion})
        else:
            self.env['valor.turn'].create({
                'valor_id': self.valor_id.id,
                'sequence': next_seq,
                'store_client_prompt': suggestion,
            })
