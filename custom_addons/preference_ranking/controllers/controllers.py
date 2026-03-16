# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import random
import requests
import os
from dotenv import load_dotenv
import logging

_logger = logging.getLogger(__name__)

load_dotenv()
from .llm_actions import run_prompt_rejection_for_tasks, run_response_generation_for_tasks, prompt_rejection_check_sync, response_generation_for_tasks_sync, evaluation_for_tasks_sync
from ..services.rabbitmq_service import publish_eval_task, batch_publish_eval_tasks

GRAPH_BASE_URL = "https://graph-genai.facebook.com/v24.0"
WORKSTREAM = "vendor_onboarding"


class PreferenceRanking(http.Controller):
    @http.route('/api/get_jsonl_data', type='http', auth='public', methods=['POST'], csrf=False, cors='*')
    def method_get_jsonl_data(self, **params):
        try:
            try:
                jdata = json.loads(request.httprequest.stream.read())
            except:
                try:
                    jdata = json.loads(request.httprequest.data)
                except:
                    jdata = {}
            if 'url' not in jdata:
                return http.Response(
                    json.dumps({'message': 'URL not in body', 'status': 400}),
                    content_type='application/json',
                    status=400
                )
            if not jdata['url']:
                return http.Response(
                    json.dumps({'message': 'URL is empty', 'status': 400}),
                    content_type='application/json',
                    status=400
                )
            url = str(jdata['url'])

            response = requests.get(url, timeout=60)
            response.raise_for_status()  # fail fast if error

            data = []
            # Parse concatenated JSON objects (multi-line or single-line)
            text = response.text.strip()
            decoder = json.JSONDecoder()
            idx = 0
            while idx < len(text):
                # Skip whitespace between objects
                while idx < len(text) and text[idx] in ' \t\n\r':
                    idx += 1
                if idx >= len(text):
                    break
                obj, end_idx = decoder.raw_decode(text, idx)
                data.append(obj)
                idx = end_idx

            if data:
                flags = [True] * (len(data) // 2) + [False] * (len(data) - len(data) // 2)
                random.shuffle(flags)

                for d, swap in zip(data, flags):
                    if swap:
                        d["response_a"], d["response_b"] = d["response_b"], d["response_a"]
                    d["is_randomized"] = swap
                vals_list = []
                for i in data:
                    # Extract prompt from new schema: prompt_metadata.dialog_history[-1].content
                    prompt_text = ''
                    prompt_metadata = i.get('prompt_metadata', {})
                    dialog_history = prompt_metadata.get('dialog_history', [])
                    if dialog_history:
                        prompt_text = dialog_history[-1].get('content', '')

                    vals_list.append({
                        'task_id': i.get('evaluation_id', '') or '',
                        'client_prompt': prompt_text,
                        'client_response_a': i.get('response_a', '') or '',
                        'client_response_b': i.get('response_b', '') or '',
                        'is_randomized': i.get('is_randomized', False),
                    })
                CREATE_CHUNK = 100
                all_record_ids = []
                queued_count = 0
                queue_errors = []

                for chunk_start in range(0, len(vals_list), CREATE_CHUNK):
                    chunk_vals = vals_list[chunk_start:chunk_start + CREATE_CHUNK]
                    chunk_records = request.env['preference.ranking'].sudo().create(chunk_vals)
                    chunk_ids = chunk_records.ids
                    all_record_ids.extend(chunk_ids)

                    request.env.cr.commit()

                    try:
                        batch_publish_eval_tasks(chunk_ids)
                        queued_count += len(chunk_ids)
                    except Exception as eq:
                        _logger.error('Batch publish failed for chunk %d, falling back: %s',
                                      chunk_start, eq)
                        for rid in chunk_ids:
                            try:
                                publish_eval_task(rid)
                                queued_count += 1
                            except Exception as eq2:
                                _logger.error('Failed to queue eval for record %s: %s', rid, eq2)
                                queue_errors.append({'record_id': rid, 'error': str(eq2)})

                    _logger.info('Processed chunk %d-%d / %d records',
                                 chunk_start + 1, chunk_start + len(chunk_vals), len(vals_list))

                return http.Response(
                    json.dumps({
                        'success': True,
                        'message': 'Success',
                        'records_created': len(all_record_ids),
                        'records_queued': queued_count,
                        'queue_errors': queue_errors,
                        'status': 200,
                    }),
                    content_type='application/json',
                    status=200
                )
            else:
                return http.Response(
                    json.dumps({'message': 'Data Not Found', 'status': 400}),
                    content_type='application/json',
                    status=400
                )
        except Exception as e:
            return http.Response(
                json.dumps({'error': str(e), 'status': 500}),
                content_type='application/json',
                status=500
            )

    @http.route('/api/get_llm_response', type='http', auth='public', methods=['GET'], csrf=False, cors='*')
    def method_get_llm_response(self, **params):
        try:
            error_list = []
            openai_api_key = os.getenv("openai_api_key")
            genai_api_key = os.getenv("genai_api_key")
            url = f"{GRAPH_BASE_URL}/llm_annotations_model_router_workstream"
            payload = {
                "access_token": genai_api_key,
                "workstream": WORKSTREAM,
            }
            conf_response = requests.post(url, json=payload)
            conf_response.raise_for_status()
            router_config = conf_response.json()
            dialog_id = router_config.get("dialog_id") if "dialog_id" in router_config and router_config.get(
                "dialog_id") else ''
            model_1 = router_config.get("model_1") if "model_1" in router_config and router_config.get(
                "model_1") else ''
            model_2 = router_config.get("model_2") if "model_2" in router_config and router_config.get(
                "model_2") else ''
            _logger.info("---------------------dialog_id %s", dialog_id)
            _logger.info("---------------------model1 %s", model_1)
            _logger.info("---------------------model2 %s", model_2)
            preference_ranking = request.env['preference.ranking']
            preference_ranking_data = preference_ranking.sudo().search([('is_processed', '=', False)], order='id asc')
            for i in preference_ranking_data:
                i.sudo()._trigger_universal_background_task(genai_api_key=genai_api_key, openai_api_key=openai_api_key, dialog_id=dialog_id, model_1=model_1, model_2=model_2)
                # try:
                #     rejection_reason = ''
                #     rejection_status = 'ACCEPT'
                #     gemini_response = ''
                #     gpt_response = ''
                #     list1 = [i.client_prompt]
                #     tasks_response = prompt_rejection_check_sync(gemini_api_key=gemini_api_key, user_prompts=list1)
                #     if tasks_response:
                #         if 'status' in tasks_response[0] and tasks_response[0]['status'] != 'ACCEPT':
                #             rejection_reason = tasks_response[0]['result']['reason']
                #             i.sudo().write({
                #                 'prompt_rejection_reason': rejection_reason,
                #                 'is_processed': True,
                #                 'is_ratable': False
                #             })
                #     if rejection_status == 'ACCEPT':
                #         list2 = [{
                #             'task_id': i.task_id,
                #             'prompt': i.client_prompt
                #         }]
                #         tasks2_response = response_generation_for_tasks_sync(gemini_api_key=gemini_api_key, openai_api_key=openai_api_key ,tasks=list2)
                #         if tasks2_response:
                #             gemini_response = tasks2_response[0]['gemini_response']
                #             gpt_response = tasks2_response[0]['gpt_response']
                #         list3 = [{
                #             'task_id': i.task_id,
                #             'prompt': i.client_prompt,
                #             'response_a': i.client_response_a,
                #             'response_b': i.client_response_b,
                #             'gemini_response': gemini_response,
                #             'gpt_response': gpt_response
                #         }]
                #         print('***************************************************88')
                #         print(list3)
                #         print('***************************************************88')
                #         tasks3_response = evaluation_for_tasks_sync(gemini_api_key=gemini_api_key, evaluation_inputs=list3)
                #         if tasks3_response:
                #             print(tasks3_response)
                #             truthfulness_a = str(tasks3_response[0]['evaluation_result']['response_a']['truthfulness']['score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'truthfulness' in tasks3_response[0]['evaluation_result']['response_a'] and 'score' in tasks3_response[0]['evaluation_result']['response_a']['truthfulness'] and tasks3_response[0]['evaluation_result']['response_a']['truthfulness']['score'] else ''
                #             instruction_following_a = str(tasks3_response[0]['evaluation_result']['response_a']['instruction_following']['score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'instruction_following' in tasks3_response[0]['evaluation_result']['response_a'] and 'score' in tasks3_response[0]['evaluation_result']['response_a']['instruction_following'] and tasks3_response[0]['evaluation_result']['response_a']['instruction_following']['score'] else ''
                #             writing_quality_a = str(tasks3_response[0]['evaluation_result']['response_a']['writing_style']['score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'writing_style' in tasks3_response[0]['evaluation_result']['response_a'] and 'score' in tasks3_response[0]['evaluation_result']['response_a']['writing_style'] and tasks3_response[0]['evaluation_result']['response_a']['writing_style']['score'] else ''
                #             verbosity_a = str(tasks3_response[0]['evaluation_result']['response_a']['verbosity']['score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'verbosity' in tasks3_response[0]['evaluation_result']['response_a'] and 'score' in tasks3_response[0]['evaluation_result']['response_a']['verbosity'] and tasks3_response[0]['evaluation_result']['response_a']['verbosity']['score'] else ''
                #             prompt_correctness_a = str(tasks3_response[0]['evaluation_result']['response_a']['prompt_correctness']['score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'prompt_correctness' in tasks3_response[0]['evaluation_result']['response_a'] and 'score' in tasks3_response[0]['evaluation_result']['response_a']['prompt_correctness'] and tasks3_response[0]['evaluation_result']['response_a']['prompt_correctness']['score'] else ''
                #             overall_quality_a = str(int(tasks3_response[0]['evaluation_result']['response_a']['overall_quality']['weighted_score'])) if 'evaluation_result' in tasks3_response[0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'overall_quality' in tasks3_response[0]['evaluation_result']['response_a'] and 'weighted_score' in tasks3_response[0]['evaluation_result']['response_a']['overall_quality'] and tasks3_response[0]['evaluation_result']['response_a']['overall_quality']['weighted_score'] else ''
                #             truthfulness_b = str(tasks3_response[0]['evaluation_result']['response_b']['truthfulness'][
                #                 'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                             tasks3_response[0]['evaluation_result'] and 'truthfulness' in \
                #                             tasks3_response[0]['evaluation_result']['response_b'] and 'score' in \
                #                             tasks3_response[0]['evaluation_result']['response_b']['truthfulness'] and tasks3_response[0]['evaluation_result']['response_b']['truthfulness']['score'] else ''
                #             instruction_following_b = \
                #             str(tasks3_response[0]['evaluation_result']['response_b']['instruction_following'][
                #                 'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                             tasks3_response[0]['evaluation_result'] and 'instruction_following' in \
                #                             tasks3_response[0]['evaluation_result']['response_b'] and 'score' in \
                #                             tasks3_response[0]['evaluation_result']['response_b'][
                #                                 'instruction_following'] and tasks3_response[0]['evaluation_result']['response_b'][
                #                                 'instruction_following']['score'] else ''
                #             writing_quality_b = str(tasks3_response[0]['evaluation_result']['response_b']['writing_style'][
                #                 'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                             tasks3_response[0]['evaluation_result'] and 'writing_style' in \
                #                             tasks3_response[0]['evaluation_result']['response_b'] and 'score' in \
                #                             tasks3_response[0]['evaluation_result']['response_b']['writing_style'] and tasks3_response[0]['evaluation_result']['response_b']['writing_style']['score'] else ''
                #             verbosity_b = str(tasks3_response[0]['evaluation_result']['response_b']['verbosity'][
                #                 'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                             tasks3_response[0]['evaluation_result'] and 'verbosity' in \
                #                             tasks3_response[0]['evaluation_result']['response_b'] and 'score' in \
                #                             tasks3_response[0]['evaluation_result']['response_b']['verbosity'] and tasks3_response[0]['evaluation_result']['response_b']['verbosity']['score'] else ''
                #             prompt_correctness_b = \
                #             str(tasks3_response[0]['evaluation_result']['response_b']['prompt_correctness'][
                #                 'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                             tasks3_response[0]['evaluation_result'] and 'prompt_correctness' in \
                #                             tasks3_response[0]['evaluation_result']['response_b'] and 'score' in \
                #                             tasks3_response[0]['evaluation_result']['response_b'][
                #                                 'prompt_correctness'] and tasks3_response[0]['evaluation_result']['response_b'][
                #                                 'prompt_correctness']['score'] else ''
                #             overall_quality_b = str(int(tasks3_response[0]['evaluation_result']['response_b']['overall_quality'][
                #                 'weighted_score'])) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                                      tasks3_response[0]['evaluation_result'] and 'overall_quality' in \
                #                                      tasks3_response[0]['evaluation_result'][
                #                                          'response_b'] and 'weighted_score' in \
                #                                      tasks3_response[0]['evaluation_result']['response_b'][
                #                                          'overall_quality'] and tasks3_response[0]['evaluation_result']['response_b'][
                #                                          'overall_quality']['weighted_score'] else ''
                #
                #             reason_truthfulness_a = str(tasks3_response[0]['evaluation_result']['response_a']['truthfulness'][
                #                                      'reason']) if 'evaluation_result' in tasks3_response[
                #                 0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'truthfulness' in \
                #                                                   tasks3_response[0]['evaluation_result'][
                #                                                       'response_a'] and 'reason' in \
                #                                                   tasks3_response[0]['evaluation_result']['response_a'][
                #                                                       'truthfulness'] and \
                #                                                   tasks3_response[0]['evaluation_result']['response_a'][
                #                                                       'truthfulness']['reason'] else ''
                #             reason_instruction_following_a = str(
                #                 tasks3_response[0]['evaluation_result']['response_a']['instruction_following'][
                #                     'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                #                                  tasks3_response[0]['evaluation_result'] and 'instruction_following' in \
                #                                  tasks3_response[0]['evaluation_result']['response_a'] and 'reason' in \
                #                                  tasks3_response[0]['evaluation_result']['response_a'][
                #                                      'instruction_following'] and \
                #                                  tasks3_response[0]['evaluation_result']['response_a'][
                #                                      'instruction_following']['reason'] else ''
                #             reason_writing_quality_a = str(
                #                 tasks3_response[0]['evaluation_result']['response_a']['writing_style'][
                #                     'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                #                                  tasks3_response[0]['evaluation_result'] and 'writing_style' in \
                #                                  tasks3_response[0]['evaluation_result']['response_a'] and 'reason' in \
                #                                  tasks3_response[0]['evaluation_result']['response_a'][
                #                                      'writing_style'] and \
                #                                  tasks3_response[0]['evaluation_result']['response_a']['writing_style'][
                #                                      'reason'] else ''
                #             reason_verbosity_a = str(tasks3_response[0]['evaluation_result']['response_a']['verbosity'][
                #                                   'reason']) if 'evaluation_result' in tasks3_response[
                #                 0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'verbosity' in \
                #                                                tasks3_response[0]['evaluation_result'][
                #                                                    'response_a'] and 'reason' in \
                #                                                tasks3_response[0]['evaluation_result']['response_a'][
                #                                                    'verbosity'] and \
                #                                                tasks3_response[0]['evaluation_result']['response_a'][
                #                                                    'verbosity']['reason'] else ''
                #             reason_prompt_correctness_a = str(
                #                 tasks3_response[0]['evaluation_result']['response_a']['prompt_correctness'][
                #                     'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                #                                  tasks3_response[0]['evaluation_result'] and 'prompt_correctness' in \
                #                                  tasks3_response[0]['evaluation_result']['response_a'] and 'reason' in \
                #                                  tasks3_response[0]['evaluation_result']['response_a'][
                #                                      'prompt_correctness'] and \
                #                                  tasks3_response[0]['evaluation_result']['response_a'][
                #                                      'prompt_correctness']['reason'] else ''
                #             reason_overall_quality_a = str(
                #                 tasks3_response[0]['evaluation_result']['response_a']['overall_quality'][
                #                     'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                #                                            tasks3_response[0][
                #                                                'evaluation_result'] and 'overall_quality' in \
                #                                            tasks3_response[0]['evaluation_result'][
                #                                                'response_a'] and 'reason' in \
                #                                            tasks3_response[0]['evaluation_result']['response_a'][
                #                                                'overall_quality'] and \
                #                                            tasks3_response[0]['evaluation_result']['response_a'][
                #                                                'overall_quality']['reason'] else ''
                #             reason_truthfulness_b = str(tasks3_response[0]['evaluation_result']['response_b']['truthfulness'][
                #                                      'reason']) if 'evaluation_result' in tasks3_response[
                #                 0] and 'response_b' in \
                #                                                   tasks3_response[0][
                #                                                       'evaluation_result'] and 'truthfulness' in \
                #                                                   tasks3_response[0]['evaluation_result'][
                #                                                       'response_b'] and 'reason' in \
                #                                                   tasks3_response[0]['evaluation_result']['response_b'][
                #                                                       'truthfulness'] and \
                #                                                   tasks3_response[0]['evaluation_result']['response_b'][
                #                                                       'truthfulness']['reason'] else ''
                #             reason_instruction_following_b = \
                #                 str(tasks3_response[0]['evaluation_result']['response_b']['instruction_following'][
                #                         'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                                      tasks3_response[0][
                #                                          'evaluation_result'] and 'instruction_following' in \
                #                                      tasks3_response[0]['evaluation_result'][
                #                                          'response_b'] and 'reason' in \
                #                                      tasks3_response[0]['evaluation_result']['response_b'][
                #                                          'instruction_following'] and \
                #                                      tasks3_response[0]['evaluation_result']['response_b'][
                #                                          'instruction_following']['reason'] else ''
                #             reason_writing_quality_b = str(
                #                 tasks3_response[0]['evaluation_result']['response_b']['writing_style'][
                #                     'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                                  tasks3_response[0]['evaluation_result'] and 'writing_style' in \
                #                                  tasks3_response[0]['evaluation_result']['response_b'] and 'reason' in \
                #                                  tasks3_response[0]['evaluation_result']['response_b'][
                #                                      'writing_style'] and \
                #                                  tasks3_response[0]['evaluation_result']['response_b']['writing_style'][
                #                                      'reason'] else ''
                #             reason_verbosity_b = str(tasks3_response[0]['evaluation_result']['response_b']['verbosity'][
                #                                   'reason']) if 'evaluation_result' in tasks3_response[
                #                 0] and 'response_b' in \
                #                                                tasks3_response[0][
                #                                                    'evaluation_result'] and 'verbosity' in \
                #                                                tasks3_response[0]['evaluation_result'][
                #                                                    'response_b'] and 'reason' in \
                #                                                tasks3_response[0]['evaluation_result']['response_b'][
                #                                                    'verbosity'] and \
                #                                                tasks3_response[0]['evaluation_result']['response_b'][
                #                                                    'verbosity']['reason'] else ''
                #             reason_prompt_correctness_b = \
                #                 str(tasks3_response[0]['evaluation_result']['response_b']['prompt_correctness'][
                #                         'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                #                                      tasks3_response[0]['evaluation_result'] and 'prompt_correctness' in \
                #                                      tasks3_response[0]['evaluation_result'][
                #                                          'response_b'] and 'reason' in \
                #                                      tasks3_response[0]['evaluation_result']['response_b'][
                #                                          'prompt_correctness'] and \
                #                                      tasks3_response[0]['evaluation_result']['response_b'][
                #                                          'prompt_correctness']['reason'] else ''
                #             reason_overall_quality_b = str(
                #                 tasks3_response[0]['evaluation_result']['response_b']['overall_quality'][
                #                         'reason']) if 'evaluation_result' in tasks3_response[
                #                 0] and 'response_b' in \
                #                                                tasks3_response[0][
                #                                                    'evaluation_result'] and 'overall_quality' in \
                #                                                tasks3_response[0]['evaluation_result'][
                #                                                    'response_b'] and 'reason' in \
                #                                                tasks3_response[0]['evaluation_result']['response_b'][
                #                                                    'overall_quality'] and \
                #                                                tasks3_response[0]['evaluation_result']['response_b'][
                #                                                    'overall_quality']['reason'] else ''
                #
                #             ab_preference = str(tasks3_response[0]['comparison_ab']['comparison_score']) if 'comparison_ab' in tasks3_response[0] and 'comparison_score' in tasks3_response[0]['comparison_ab'] and tasks3_response[0]['comparison_ab']['comparison_score'] else ''
                #             ab_comment = tasks3_response[0]['comparison_ab']['overall_comment'] if 'comparison_ab' in tasks3_response[0] and 'overall_comment' in tasks3_response[0]['comparison_ab'] and tasks3_response[0]['comparison_ab']['overall_comment'] else ''
                #             ab_gemini_preference = str(tasks3_response[0]['comparison_vs_gemini']['comparison_score']) if 'comparison_vs_gemini' in tasks3_response[0] and 'comparison_score' in tasks3_response[0]['comparison_vs_gemini'] and tasks3_response[0]['comparison_vs_gemini']['comparison_score'] else ''
                #             ab_gemini_comment = str(tasks3_response[0]['comparison_vs_gemini']['comparison_comment']) if 'comparison_vs_gemini' in tasks3_response[0] and 'comparison_comment' in tasks3_response[0]['comparison_vs_gemini'] and tasks3_response[0]['comparison_vs_gemini']['comparison_comment'] else ''
                #             ab_gpt_preference = str(tasks3_response[0]['comparison_vs_gpt']['comparison_score']) if 'comparison_vs_gpt' in tasks3_response[0] and 'comparison_score' in tasks3_response[0]['comparison_vs_gpt'] and tasks3_response[0]['comparison_vs_gpt']['comparison_score'] else ''
                #             ab_gpt_comment = str(tasks3_response[0]['comparison_vs_gpt']['comparison_comment']) if 'comparison_vs_gpt' in tasks3_response[0] and 'comparison_comment' in tasks3_response[0]['comparison_vs_gpt'] and tasks3_response[0]['comparison_vs_gpt']['comparison_comment'] else ''
                #             gpt_rubric_name = str(tasks3_response[0]['rubrics_vs_gpt']['name']) if 'rubrics_vs_gpt' in tasks3_response[0] and 'name' in tasks3_response[0]['rubrics_vs_gpt'] and tasks3_response[0]['rubrics_vs_gpt']['name'] else ''
                #             gpt_rubric_description = str(tasks3_response[0]['rubrics_vs_gpt']['description']) if 'rubrics_vs_gpt' in tasks3_response[0] and 'description' in tasks3_response[0]['rubrics_vs_gpt'] and tasks3_response[0]['rubrics_vs_gpt']['description'] else ''
                #             gpt_rubric_scale_rating = str(tasks3_response[0]['rubrics_vs_gpt']['rating']) if 'rubrics_vs_gpt' in tasks3_response[0] and 'rating' in tasks3_response[0]['rubrics_vs_gpt'] and tasks3_response[0]['rubrics_vs_gpt']['rating'] else ''
                #             gemini_rubric_name = str(tasks3_response[0]['rubrics_vs_gemini']['name']) if 'rubrics_vs_gemini' in tasks3_response[0] and 'name' in tasks3_response[0]['rubrics_vs_gemini'] and tasks3_response[0]['rubrics_vs_gemini']['name'] else ''
                #             gemini_rubric_description = str(tasks3_response[0]['rubrics_vs_gemini']['description']) if 'rubrics_vs_gemini' in tasks3_response[0] and 'description' in tasks3_response[0]['rubrics_vs_gemini'] and tasks3_response[0]['rubrics_vs_gemini']['description'] else ''
                #             gemini_rubric_scale_rating = str(tasks3_response[0]['rubrics_vs_gemini']['rating']) if 'rubrics_vs_gemini' in tasks3_response[0] and 'rating' in tasks3_response[0]['rubrics_vs_gemini'] and tasks3_response[0]['rubrics_vs_gemini']['rating'] else ''
                #
                #             i.sudo().write({
                #                 'is_ratable': True,
                #                 'is_processed': True,
                #                 'gemini_response': gemini_response,
                #                 'gpt_response': gpt_response,
                #                 'store_truthfulness_a': truthfulness_a,
                #                 'store_instruction_following_a': instruction_following_a,
                #                 'store_writing_quality_a': writing_quality_a,
                #                 'store_verbosity_a': verbosity_a,
                #                 'store_prompt_correctness_a': prompt_correctness_a,
                #                 'store_overall_quality_a': overall_quality_a,
                #                 'store_truthfulness_b': truthfulness_b,
                #                 'store_instruction_following_b': instruction_following_b,
                #                 'store_writing_quality_b': writing_quality_b,
                #                 'store_verbosity_b': verbosity_b,
                #                 'store_prompt_correctness_b': prompt_correctness_b,
                #                 'store_overall_quality_b': overall_quality_b,
                #                 'store_ab_preference': ab_preference,
                #                 'store_ab_comment': ab_comment,
                #                 'store_ab_gpt_preference': ab_gpt_preference,
                #                 'store_ab_gpt_comment': ab_gpt_comment,
                #                 'store_ab_gemini_preference': ab_gemini_preference,
                #                 'store_ab_gemini_comment': ab_gemini_comment,
                #                 'store_gpt_rubric_name': gpt_rubric_name,
                #                 'store_gpt_rubric_description': gpt_rubric_description,
                #                 'store_gpt_rubric_scale_rating': gpt_rubric_scale_rating,
                #                 'store_gemini_rubric_name': gemini_rubric_name,
                #                 'store_gemini_rubric_description': gemini_rubric_description,
                #                 'store_gemini_rubric_scale_rating': gemini_rubric_scale_rating,
                #                 'reason1_truthfulness_a': reason_truthfulness_a,
                #                 'reason1_instruction_following_a': reason_instruction_following_a,
                #                 'reason1_writing_quality_a': reason_writing_quality_a,
                #                 'reason1_verbosity_a': reason_verbosity_a,
                #                 'reason1_prompt_correctness_a': reason_prompt_correctness_a,
                #                 'reason1_overall_quality_a': reason_overall_quality_a,
                #                 'reason1_truthfulness_b': reason_truthfulness_b,
                #                 'reason1_instruction_following_b': reason_instruction_following_b,
                #                 'reason1_writing_quality_b': reason_writing_quality_b,
                #                 'reason1_verbosity_b': reason_verbosity_b,
                #                 'reason1_prompt_correctness_b': reason_prompt_correctness_b,
                #                 'reason1_overall_quality_b': reason_overall_quality_b,
                #                 'prompt_rejection_reason': rejection_reason
                #             })
                # except Exception as e:
                #     print('----------error', str(e))
                #     error_list.append({
                #         'record_name': i.task_id,
                #         'error': str(e)
                #     })

            return http.Response(
                json.dumps({'success': True, 'error_list': error_list, 'message': 'Success', 'status': 200}),
                content_type='application/json',
                status=200
            )
        except Exception as e:
            return http.Response(
                json.dumps({'error': str(e), 'status': 500}),
                content_type='application/json',
                status=500
            )
