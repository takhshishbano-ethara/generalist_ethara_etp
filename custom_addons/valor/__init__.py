# -*- coding: utf-8 -*-

from . import controllers
from . import models


def _migrate_flat_turns_to_one2many(env):
    """Post-init hook: migrate existing flat turn fields to valor.turn records."""
    import logging
    _logger = logging.getLogger(__name__)

    RATING_DIMS = [
        'truthfulness_a', 'instruction_following_a', 'writing_quality_a',
        'verbosity_a', 'prompt_correctness_a', 'overall_quality_a',
        'truthfulness_b', 'instruction_following_b', 'writing_quality_b',
        'verbosity_b', 'prompt_correctness_b', 'overall_quality_b',
    ]

    ValorTurn = env['valor.turn']
    all_valor = env['valor'].search([])
    migrated = 0

    for rec in all_valor:
        if rec.turn_ids:
            continue
        for t in range(1, 8):
            prompt = getattr(rec, f'client_prompt{t}', None)
            resp_a = getattr(rec, f'client_response_a{t}', None)
            if not prompt and not resp_a:
                continue

            vals = {
                'valor_id': rec.id,
                'sequence': t,
                'client_prompt': prompt or '',
                'store_client_prompt': getattr(rec, f'store_client_prompt{t}', '') or '',
                'image': getattr(rec, f'image_{t}', None),
                'image_mime': getattr(rec, f'image_mime_{t}', '') or '',
                'image_handle_id': getattr(rec, f'image_handle_id_{t}', '') or '',
                'client_response_a': resp_a or '',
                'client_response_b': getattr(rec, f'client_response_b{t}', '') or '',
                'is_randomized': getattr(rec, f'is_randomized_{t}', False),
                'ab_preference': getattr(rec, f'ab_preference{t}', None),
                'ab_comment': getattr(rec, f'ab_comment{t}', '') or '',
                'qc_task_status': getattr(rec, f'qc_task_status_{t}', None),
            }

            for dim in RATING_DIMS:
                vals[dim] = getattr(rec, f'{dim}{t}', None)
                vals[f'store_{dim}'] = getattr(rec, f'store_{dim}{t}', None)

            old_reason_prefix = f'reason{t}_'
            old_error_prefix = f'error{t}_'
            for dim in RATING_DIMS + ['ab_preference', 'ab_comment']:
                vals[f'reason_{dim}'] = getattr(rec, f'{old_reason_prefix}{dim}', '') or ''
                vals[f'error_{dim}'] = getattr(rec, f'{old_error_prefix}{dim}', False)

            ValorTurn.create(vals)
            migrated += 1

    _logger.info('Migrated %d turn records from flat fields to valor.turn', migrated)
