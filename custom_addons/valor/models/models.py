# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api
from odoo.modules.registry import Registry as registry
from odoo.exceptions import ValidationError
from ..controllers import llm_actions
import base64
import json
import boto3
import io
from datetime import datetime, timezone
import os
import random
import requests
from dotenv import load_dotenv
from .kimi_eval import (
    check_follow_up_relevance_kimi,
    run_evaluation_kimi,
    generate_follow_up_prompt_kimi,
    run_qc_kimi,
    get_kimi_api_key,
)

_addon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path = os.path.join(_addon_root, ".env")
if os.path.isfile(_env_path):
    load_dotenv(_env_path)
else:
    load_dotenv()

_logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph-genai.facebook.com/v24.0"
# WORKSTREAM = "vendor_onboarding"
WORKSTREAM = "common_test_workstream"
DEFAULT_KIMI_MODEL = "kimi-k2.5"
# Fallback when router does not return model_1/model_2; production should rely on router.
DEFAULT_MODEL_A = "opalite"
DEFAULT_MODEL_B = "ophelia"
# genai_api_key = os.getenv("genai_api_key")
genai_api_key = '1175298344707910|88c2ee9ee7bdfd146048e4d68c30acdc'
# Kimi: from kimi_eval (Bedrock only; key unused, kept for compatibility)
kimi_api_key = get_kimi_api_key()


def check_error(value, value2):
    minus_val = value - 1
    plus_val = value + 1
    check = True if value2 > plus_val or value2 < minus_val else False
    return check

def get_eval_data(eval_result, idx=0):
    # Normalize: expose only the requested turn's result at index 0
    eval_result = [eval_result[idx]]
    truthfulness_a = str(
        eval_result[0]['evaluation_result']['response_a']['truthfulness'][
            'score']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                         eval_result[0]['evaluation_result'] and 'truthfulness' in \
                         eval_result[0]['evaluation_result'][
                             'response_a'] and 'score' in \
                         eval_result[0]['evaluation_result']['response_a'][
                             'truthfulness'] and \
                         eval_result[0]['evaluation_result']['response_a'][
                             'truthfulness']['score'] else ''
    instruction_following_a = str(
        eval_result[0]['evaluation_result']['response_a']['instruction_following'][
            'score']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                         eval_result[0][
                             'evaluation_result'] and 'instruction_following' in \
                         eval_result[0]['evaluation_result'][
                             'response_a'] and 'score' in \
                         eval_result[0]['evaluation_result']['response_a'][
                             'instruction_following'] and \
                         eval_result[0]['evaluation_result']['response_a'][
                             'instruction_following']['score'] else ''
    writing_quality_a = str(
        eval_result[0]['evaluation_result']['response_a']['writing_style'][
            'score']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                         eval_result[0]['evaluation_result'] and 'writing_style' in \
                         eval_result[0]['evaluation_result'][
                             'response_a'] and 'score' in \
                         eval_result[0]['evaluation_result']['response_a'][
                             'writing_style'] and \
                         eval_result[0]['evaluation_result']['response_a'][
                             'writing_style']['score'] else ''
    verbosity_a = str(eval_result[0]['evaluation_result']['response_a']['verbosity'][
                          'score']) if 'evaluation_result' in eval_result[
        0] and 'response_a' in eval_result[0]['evaluation_result'] and 'verbosity' in \
                                       eval_result[0]['evaluation_result'][
                                           'response_a'] and 'score' in \
                                       eval_result[0]['evaluation_result'][
                                           'response_a']['verbosity'] and \
                                       eval_result[0]['evaluation_result'][
                                           'response_a']['verbosity']['score'] else ''
    prompt_correctness_a = str(
        eval_result[0]['evaluation_result']['response_a']['prompt_correctness'][
            'score']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                         eval_result[0]['evaluation_result'] and 'prompt_correctness' in \
                         eval_result[0]['evaluation_result'][
                             'response_a'] and 'score' in \
                         eval_result[0]['evaluation_result']['response_a'][
                             'prompt_correctness'] and \
                         eval_result[0]['evaluation_result']['response_a'][
                             'prompt_correctness']['score'] else ''
    overall_quality_a = str(int(
        eval_result[0]['evaluation_result']['response_a']['overall_quality'][
            'weighted_score'])) if 'evaluation_result' in eval_result[
        0] and 'response_a' in eval_result[0][
                                       'evaluation_result'] and 'overall_quality' in \
                                   eval_result[0]['evaluation_result'][
                                       'response_a'] and 'weighted_score' in \
                                   eval_result[0]['evaluation_result']['response_a'][
                                       'overall_quality'] and \
                                   eval_result[0]['evaluation_result']['response_a'][
                                       'overall_quality']['weighted_score'] else ''
    truthfulness_b = str(
        eval_result[0]['evaluation_result']['response_b']['truthfulness'][
            'score']) if 'evaluation_result' in eval_result[0] and 'response_b' in \
                         eval_result[0]['evaluation_result'] and 'truthfulness' in \
                         eval_result[0]['evaluation_result'][
                             'response_b'] and 'score' in \
                         eval_result[0]['evaluation_result']['response_b'][
                             'truthfulness'] and \
                         eval_result[0]['evaluation_result']['response_b'][
                             'truthfulness']['score'] else ''
    instruction_following_b = \
        str(eval_result[0]['evaluation_result']['response_b']['instruction_following'][
                'score']) if 'evaluation_result' in eval_result[0] and 'response_b' in \
                             eval_result[0][
                                 'evaluation_result'] and 'instruction_following' in \
                             eval_result[0]['evaluation_result'][
                                 'response_b'] and 'score' in \
                             eval_result[0]['evaluation_result']['response_b'][
                                 'instruction_following'] and \
                             eval_result[0]['evaluation_result']['response_b'][
                                 'instruction_following']['score'] else ''
    writing_quality_b = str(
        eval_result[0]['evaluation_result']['response_b']['writing_style'][
            'score']) if 'evaluation_result' in eval_result[0] and 'response_b' in \
                         eval_result[0]['evaluation_result'] and 'writing_style' in \
                         eval_result[0]['evaluation_result'][
                             'response_b'] and 'score' in \
                         eval_result[0]['evaluation_result']['response_b'][
                             'writing_style'] and \
                         eval_result[0]['evaluation_result']['response_b'][
                             'writing_style']['score'] else ''
    verbosity_b = str(eval_result[0]['evaluation_result']['response_b']['verbosity'][
                          'score']) if 'evaluation_result' in eval_result[
        0] and 'response_b' in \
                                       eval_result[0][
                                           'evaluation_result'] and 'verbosity' in \
                                       eval_result[0]['evaluation_result'][
                                           'response_b'] and 'score' in \
                                       eval_result[0]['evaluation_result'][
                                           'response_b']['verbosity'] and \
                                       eval_result[0]['evaluation_result'][
                                           'response_b']['verbosity']['score'] else ''
    prompt_correctness_b = \
        str(eval_result[0]['evaluation_result']['response_b']['prompt_correctness'][
                'score']) if 'evaluation_result' in eval_result[0] and 'response_b' in \
                             eval_result[0][
                                 'evaluation_result'] and 'prompt_correctness' in \
                             eval_result[0]['evaluation_result'][
                                 'response_b'] and 'score' in \
                             eval_result[0]['evaluation_result']['response_b'][
                                 'prompt_correctness'] and \
                             eval_result[0]['evaluation_result']['response_b'][
                                 'prompt_correctness']['score'] else ''
    overall_quality_b = str(
        int(eval_result[0]['evaluation_result']['response_b']['overall_quality'][
                'weighted_score'])) if 'evaluation_result' in eval_result[
        0] and 'response_b' in \
                                       eval_result[0][
                                           'evaluation_result'] and 'overall_quality' in \
                                       eval_result[0]['evaluation_result'][
                                           'response_b'] and 'weighted_score' in \
                                       eval_result[0]['evaluation_result'][
                                           'response_b'][
                                           'overall_quality'] and \
                                       eval_result[0]['evaluation_result'][
                                           'response_b'][
                                           'overall_quality']['weighted_score'] else ''

    reason_truthfulness_a = str(
        eval_result[0]['evaluation_result']['response_a']['truthfulness'][
            'reason']) if 'evaluation_result' in eval_result[
        0] and 'response_a' in eval_result[0]['evaluation_result'] and 'truthfulness' in \
                          eval_result[0]['evaluation_result'][
                              'response_a'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_a'][
                              'truthfulness'] and \
                          eval_result[0]['evaluation_result']['response_a'][
                              'truthfulness']['reason'] else ''
    reason_instruction_following_a = str(
        eval_result[0]['evaluation_result']['response_a']['instruction_following'][
            'reason']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                          eval_result[0][
                              'evaluation_result'] and 'instruction_following' in \
                          eval_result[0]['evaluation_result'][
                              'response_a'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_a'][
                              'instruction_following'] and \
                          eval_result[0]['evaluation_result']['response_a'][
                              'instruction_following']['reason'] else ''
    reason_writing_quality_a = str(
        eval_result[0]['evaluation_result']['response_a']['writing_style'][
            'reason']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                          eval_result[0]['evaluation_result'] and 'writing_style' in \
                          eval_result[0]['evaluation_result'][
                              'response_a'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_a'][
                              'writing_style'] and \
                          eval_result[0]['evaluation_result']['response_a'][
                              'writing_style'][
                              'reason'] else ''
    reason_verbosity_a = str(
        eval_result[0]['evaluation_result']['response_a']['verbosity'][
            'reason']) if 'evaluation_result' in eval_result[
        0] and 'response_a' in eval_result[0]['evaluation_result'] and 'verbosity' in \
                          eval_result[0]['evaluation_result'][
                              'response_a'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_a'][
                              'verbosity'] and \
                          eval_result[0]['evaluation_result']['response_a'][
                              'verbosity']['reason'] else ''
    reason_prompt_correctness_a = str(
        eval_result[0]['evaluation_result']['response_a']['prompt_correctness'][
            'reason']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                          eval_result[0][
                              'evaluation_result'] and 'prompt_correctness' in \
                          eval_result[0]['evaluation_result'][
                              'response_a'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_a'][
                              'prompt_correctness'] and \
                          eval_result[0]['evaluation_result']['response_a'][
                              'prompt_correctness']['reason'] else ''
    reason_overall_quality_a = str(
        eval_result[0]['evaluation_result']['response_a']['overall_quality'][
            'reason']) if 'evaluation_result' in eval_result[0] and 'response_a' in \
                          eval_result[0][
                              'evaluation_result'] and 'overall_quality' in \
                          eval_result[0]['evaluation_result'][
                              'response_a'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_a'][
                              'overall_quality'] and \
                          eval_result[0]['evaluation_result']['response_a'][
                              'overall_quality']['reason'] else ''
    reason_truthfulness_b = str(
        eval_result[0]['evaluation_result']['response_b']['truthfulness'][
            'reason']) if 'evaluation_result' in eval_result[
        0] and 'response_b' in \
                          eval_result[0][
                              'evaluation_result'] and 'truthfulness' in \
                          eval_result[0]['evaluation_result'][
                              'response_b'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_b'][
                              'truthfulness'] and \
                          eval_result[0]['evaluation_result']['response_b'][
                              'truthfulness']['reason'] else ''
    reason_instruction_following_b = \
        str(eval_result[0]['evaluation_result']['response_b']['instruction_following'][
                'reason']) if 'evaluation_result' in eval_result[0] and 'response_b' in \
                              eval_result[0][
                                  'evaluation_result'] and 'instruction_following' in \
                              eval_result[0]['evaluation_result'][
                                  'response_b'] and 'reason' in \
                              eval_result[0]['evaluation_result']['response_b'][
                                  'instruction_following'] and \
                              eval_result[0]['evaluation_result']['response_b'][
                                  'instruction_following']['reason'] else ''
    reason_writing_quality_b = str(
        eval_result[0]['evaluation_result']['response_b']['writing_style'][
            'reason']) if 'evaluation_result' in eval_result[0] and 'response_b' in \
                          eval_result[0]['evaluation_result'] and 'writing_style' in \
                          eval_result[0]['evaluation_result'][
                              'response_b'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_b'][
                              'writing_style'] and \
                          eval_result[0]['evaluation_result']['response_b'][
                              'writing_style'][
                              'reason'] else ''
    reason_verbosity_b = str(
        eval_result[0]['evaluation_result']['response_b']['verbosity'][
            'reason']) if 'evaluation_result' in eval_result[
        0] and 'response_b' in \
                          eval_result[0][
                              'evaluation_result'] and 'verbosity' in \
                          eval_result[0]['evaluation_result'][
                              'response_b'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_b'][
                              'verbosity'] and \
                          eval_result[0]['evaluation_result']['response_b'][
                              'verbosity']['reason'] else ''
    reason_prompt_correctness_b = \
        str(eval_result[0]['evaluation_result']['response_b']['prompt_correctness'][
                'reason']) if 'evaluation_result' in eval_result[0] and 'response_b' in \
                              eval_result[0][
                                  'evaluation_result'] and 'prompt_correctness' in \
                              eval_result[0]['evaluation_result'][
                                  'response_b'] and 'reason' in \
                              eval_result[0]['evaluation_result']['response_b'][
                                  'prompt_correctness'] and \
                              eval_result[0]['evaluation_result']['response_b'][
                                  'prompt_correctness']['reason'] else ''
    reason_overall_quality_b = str(
        eval_result[0]['evaluation_result']['response_b']['overall_quality'][
            'reason']) if 'evaluation_result' in eval_result[
        0] and 'response_b' in \
                          eval_result[0][
                              'evaluation_result'] and 'overall_quality' in \
                          eval_result[0]['evaluation_result'][
                              'response_b'] and 'reason' in \
                          eval_result[0]['evaluation_result']['response_b'][
                              'overall_quality'] and \
                          eval_result[0]['evaluation_result']['response_b'][
                              'overall_quality']['reason'] else ''

    ab_preference = str(
        eval_result[0]['comparison_ab']['comparison_score']) if 'comparison_ab' in \
                                                                eval_result[
                                                                    0] and 'comparison_score' in \
                                                                eval_result[0][
                                                                    'comparison_ab'] and \
                                                                eval_result[0][
                                                                    'comparison_ab'][
                                                                    'comparison_score'] else ''
    ab_comment = eval_result[0]['comparison_ab'][
        'overall_comment'] if 'comparison_ab' in eval_result[0] and 'overall_comment' in \
                              eval_result[0]['comparison_ab'] and \
                              eval_result[0]['comparison_ab']['overall_comment'] else ''
    return {
        'truthfulness_a': truthfulness_a,
        'instruction_following_a': instruction_following_a,
        'writing_quality_a': writing_quality_a,
        'verbosity_a': verbosity_a,
        'prompt_correctness_a': prompt_correctness_a,
        'overall_quality_a': overall_quality_a,
        'truthfulness_b': truthfulness_b,
        'instruction_following_b': instruction_following_b,
        'writing_quality_b': writing_quality_b,
        'verbosity_b': verbosity_b,
        'prompt_correctness_b': prompt_correctness_b,
        'overall_quality_b': overall_quality_b,
        'reason_truthfulness_a': reason_truthfulness_a,
        'reason_instruction_following_a': reason_instruction_following_a,
        'reason_writing_quality_a': reason_writing_quality_a,
        'reason_verbosity_a': reason_verbosity_a,
        'reason_prompt_correctness_a': reason_prompt_correctness_a,
        'reason_overall_quality_a': reason_overall_quality_a,
        'reason_truthfulness_b': reason_truthfulness_b,
        'reason_instruction_following_b': reason_instruction_following_b,
        'reason_writing_quality_b': reason_writing_quality_b,
        'reason_verbosity_b': reason_verbosity_b,
        'reason_prompt_correctness_b': reason_prompt_correctness_b,
        'reason_overall_quality_b': reason_overall_quality_b,
        'ab_preference':ab_preference,
        'ab_comment': ab_comment
    }


class Valor(models.Model):
    _name = 'valor'
    _description = 'Valor'
    _rec_name = 'task_id'

    def _compute_is_tasker(self):
        has_group = self.env.user.has_group('valor.group_valor_tasker')
        for record in self:
            record.is_tasker = has_group

    @api.depends('task_id')
    def _compute_rater_id(self):
        for record in self:
            record.rater_id = f"rater_{record.task_id}" if record.task_id else False

    def _generate_task_id(self, l0_id=False):
        domain_prefix = "unk"
        if l0_id:
            l0_rec = self.env['domain.level'].browse(l0_id)
            if l0_rec.exists() and l0_rec.name:
                domain_prefix = l0_rec.name.strip().lower()[:3]
        digits = ''.join([str(random.randint(0, 9)) for _ in range(4)])
        return f"eval_{domain_prefix}_{digits}"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('task_id'):
                vals['task_id'] = self._generate_task_id(vals.get('l0'))
        return super().create(vals_list)

    def write(self, vals):
        if 'task_id' in vals and not vals['task_id']:
            vals.pop('task_id')
        return super().write(vals)

    def action_valor_upload_jsonl(self):
        data = []
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        for record in self:
            if record.qc_task_status == 'pass':
                data.append({
                    "evaluation_id": f"eval_sxs_2026_{record.task_id}",
                    "randomized_responses_order": record.is_randomized,
                    "prompt_metadata": {
                        "dialog_history": [{
                            "role": "user",
                            "content": record.client_prompt
                        }],
                        "response_a": record.client_response_a,
                        "response_b": record.client_response_b
                    },
                    "external_model_responses": {
                        "gpt_5_2": record.gpt_response if record.gpt_response else '',
                        "gemini_3_pro": record.gemini_response if record.gemini_response else ''
                    },
                    "ratings_ab": [{
                        "rater_id": record.rater_id,
                        "timestamp": timestamp,
                        "prompt_requires_fresh_info": False,
                        "ab_preference": int(record.ab_preference) if record.ab_preference else False,
                        "ab_comment": record.ab_comment if record.ab_comment else '',
                        "pointwise_evaluations": {
                            "response_a": {
                                "truthfulness": int(record.truthfulness_a) if record.truthfulness_a else False,
                                "instruction_following": int(record.instruction_following_a) if record.instruction_following_a else False,
                                "writing_quality": int(record.writing_quality_a) if record.writing_quality_a else False,
                                "verbosity": int(record.verbosity_a) if record.verbosity_a else False,
                                "correctness": int(record.prompt_correctness_a) if record.prompt_correctness_a else False,
                                "overall_quality": int(record.overall_quality_a) if record.overall_quality_a else False
                            },
                            "response_b": {
                                "truthfulness": int(record.truthfulness_b) if record.truthfulness_b else False,
                                "instruction_following": int(record.instruction_following_b) if record.instruction_following_b else False,
                                "writing_quality": int(record.writing_quality_b) if record.writing_quality_b else False,
                                "verbosity": int(record.verbosity_b) if record.verbosity_b else False,
                                "correctness": int(record.prompt_correctness_b) if record.prompt_correctness_b else False,
                                "overall_quality": int(record.overall_quality_b) if record.overall_quality_b else False
                            }
                        },
                        "external_model_comparisons": {
                            "gpt_5_2": {
                                "ab_gpt_preference": int(record.ab_gpt_preference) if record.ab_gpt_preference else False,
                                "ab_gpt_comment": record.ab_gpt_comment if record.ab_gpt_comment else ''
                            },
                            "gemini_3_pro": {
                                "ab_gemini_preference": int(record.ab_gemini_preference) if record.ab_gemini_preference else False,
                                "ab_gemini_comment": record.ab_gemini_comment if record.ab_gemini_comment else ''
                            }
                        },
                        "rubrics": [{
                            "name": record.gpt_rubric_name if record.gpt_rubric_name else '',
                            "description": record.gpt_rubric_description if record.gpt_rubric_description else '',
                            "scale": int(record.gpt_rubric_scale_rating) if record.gpt_rubric_scale_rating else False
                        },{
                            "name": record.gemini_rubric_name if record.gemini_rubric_name else '',
                            "description": record.gemini_rubric_description if record.gemini_rubric_description else '',
                            "scale": int(record.gemini_rubric_scale_rating) if record.gemini_rubric_scale_rating else False
                        }]
                    }]
                })
        if not data:
            raise ValidationError("No submitted records found.")
        jsonl_output = io.StringIO()
        for entry in data:
            json.dump(entry, jsonl_output)
            jsonl_output.write('\n')

        content = jsonl_output.getvalue().encode('utf-8')

        access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        s3 = boto3.client('s3', aws_access_key_id=access_key_id, aws_secret_access_key=secret_key)
        s3_key = f"delivery/{timestamp}_delivery.jsonl"

        s3.put_object(
            Bucket='prod-grtlabs',
            Key=s3_key,
            Body=content,
            ContentType='application/x-jsonlines'
        )

    # def _trigger_universal_background_task(self, gemini_api_key, openai_api_key):
    #     record_ids = self.ids
    #     model_name = self._name
    #     user_id = 3 or self.env.uid
    #     context = dict(self.env.context)
    #     db_name = self.env.cr.dbname
    #     def background_worker():
    #         with registry(db_name).cursor() as new_cr:
    #             try:
    #                 new_env = api.Environment(new_cr, user_id, context)
    #                 records = new_env[model_name].browse(record_ids)
    #                 for i in records:
    #                     rejection_reason = ''
    #                     rejection_status = 'ACCEPT'
    #                     list1 = [i.client_prompt]
    #                     tasks_response = llm_actions.prompt_rejection_check_sync(gemini_api_key=gemini_api_key, user_prompts=list1)
    #                     if tasks_response:
    #                         _logger.info('tasks1_response---------------------------%s', tasks_response)
    #                         if 'status' in tasks_response[0] and tasks_response[0]['status'] != 'ACCEPT':
    #                             rejection_reason = tasks_response[0]['result']['reason']
    #                             i.sudo().write({
    #                                 'prompt_rejection_reason': rejection_reason,
    #                                 'is_processed': True,
    #                                 'is_ratable': False
    #                             })
    #                     if rejection_status == 'ACCEPT':
    #                         list2 = [{
    #                             'task_id': i.task_id,
    #                             'prompt': i.client_prompt
    #                         }]
    #                         tasks2_response = llm_actions.response_generation_for_tasks_sync(gemini_api_key=gemini_api_key,
    #                                                                              openai_api_key=openai_api_key,
    #                                                                              tasks=list2)
    #                         if tasks2_response:
    #                             _logger.info('tasks2_response---------------------------%s', tasks2_response)
    #                             gemini_response = tasks2_response[0]['gemini_response']
    #                             gpt_response = tasks2_response[0]['gpt_response']
    #                             i.sudo().write({
    #                                 'is_ratable': True,
    #                                 'is_processed': True,
    #                                 'gemini_response': gemini_response,
    #                                 'gpt_response': gpt_response,
    #                                 'prompt_rejection_reason': rejection_reason
    #                             })
    #                 new_cr.commit()
    #             except Exception as e:
    #                 _logger.info(f"{e}")
    #     _logger.info(f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine started")
    #     threading.Thread(target=background_worker, daemon=False).start()
    #     _logger.info(f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine End")

    def _upload_image_to_s3(self, turn_index, image_bytes, mime_type):
        """Upload image bytes to S3 under images/{task_id}/turn_{n}.{ext}. Uses existing bucket and AWS env."""
        ext = "png"
        if mime_type:
            if "jpeg" in mime_type or "jpg" in mime_type:
                ext = "jpeg"
            elif "gif" in mime_type:
                ext = "gif"
            elif "webp" in mime_type:
                ext = "webp"
        task_id = (self.task_id or "unknown").replace("/", "_")
        s3_key = f"images/{task_id}/turn_{turn_index}.{ext}"
        access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        s3 = boto3.client("s3", aws_access_key_id=access_key_id, aws_secret_access_key=secret_key)
        s3.put_object(
            Bucket="prod-grtlabs",
            Key=s3_key,
            Body=image_bytes,
            ContentType=mime_type or "image/png",
        )
        return s3_key

    def _ensure_image_handle_for_turn(self, turn_index, genai_api_key):
        """If image_N is set and image_handle_id_N is not, upload to S3 and Facebook, set handle_id and mime on record."""
        image_data = getattr(self, f"image_{turn_index}", None)
        handle_id = getattr(self, f"image_handle_id_{turn_index}", None) or ""
        if not image_data or (handle_id and handle_id.strip()):
            return
        mime = getattr(self, f"image_mime_{turn_index}", None) or "image/png"
        try:
            image_bytes = base64.b64decode(image_data)
        except Exception as e:
            _logger.warning("Failed to decode image for turn %s: %s", turn_index, e)
            raise ValidationError("Invalid image data for this turn.") from e
        self._upload_image_to_s3(turn_index, image_bytes, mime)
        filename = f"turn_{turn_index}.{mime.split('/')[-1] if mime else 'png'}"
        result = self._upload_attachment_meta(genai_api_key, image_bytes, filename, mime)
        hid = (result.get("handle_id") or "").strip()
        if not hid:
            raise ValidationError("Facebook attachment upload did not return a handle_id.")
        self.sudo().write({
            f"image_handle_id_{turn_index}": hid,
            f"image_mime_{turn_index}": mime,
        })

    def _ensure_image_handle_for_turn_record(self, turn_record, genai_api_key):
        """Upload image from a valor.turn record to S3/Facebook if needed."""
        image_data = turn_record.image
        handle_id = turn_record.image_handle_id or ""
        if not image_data or (handle_id and handle_id.strip()):
            return
        mime = turn_record.image_mime or "image/png"
        try:
            image_bytes = base64.b64decode(image_data)
        except Exception as e:
            _logger.warning("Failed to decode image for turn %s: %s", turn_record.sequence, e)
            raise ValidationError("Invalid image data for this turn.") from e
        self._upload_image_to_s3(turn_record.sequence, image_bytes, mime)
        filename = f"turn_{turn_record.sequence}.{mime.split('/')[-1] if mime else 'png'}"
        result = self._upload_attachment_meta(genai_api_key, image_bytes, filename, mime)
        hid = (result.get("handle_id") or "").strip()
        if not hid:
            raise ValidationError("Facebook attachment upload did not return a handle_id.")
        turn_record.sudo().write({
            "image_handle_id": hid,
            "image_mime": mime,
        })

    def _upload_attachment_meta(self, genai_api_key, file_bytes, filename, mime_type):
        """Upload file to Meta GenAI attachment API; returns dict with handle_id and mime."""
        url = f"{GRAPH_BASE_URL}/llm_annotations_attachment_upload"
        files = {"file": (filename, file_bytes, mime_type or "application/octet-stream")}
        data = {"access_token": genai_api_key}
        resp = requests.post(url, files=files, data=data, timeout=60)
        resp.raise_for_status()
        try:
            out = resp.json()
        except Exception:
            out = {}
        handle_id = (out.get("handle_id") or "").strip() if isinstance(out, dict) else ""
        if not handle_id and isinstance(out, dict):
            for k, v in out.items():
                if "handle" in k.lower() and v:
                    handle_id = str(v)
                    break
        return {"handle_id": handle_id, "mime": mime_type or "image/png"}

    def _build_message_metagen(self, role, text=None, attachment_handle_id=None, attachment_mime=None):
        """Build one or two message dicts for Meta API. Returns list of message dicts."""
        out = []
        if attachment_handle_id and role == "user":
            out.append({
                "source": {"role": "user"},
                "contents": [{"attachment": {"handle_id": attachment_handle_id, "mime": attachment_mime or "image/png"}}],
                "is_end_of_turn": True,
                "is_complete": True,
            })
        if text is not None:
            out.append({
                "source": {"role": role},
                "contents": [{"text": {"text": text}}],
                "is_end_of_turn": True,
                "is_complete": True,
            })
        return out if out else [{
            "source": {"role": role},
            "contents": [{"text": {"text": text or ""}}],
            "is_end_of_turn": True,
            "is_complete": True,
        }]

    def _build_messages_for_metagen(self, dialog_history, current_prompt, current_turn_handle_id=None, current_turn_mime=None, history_handle_ids=None):
        """Build full messages list for metagen API: history turns then current turn, with optional images."""
        messages = []
        # Sanitize so we never send None to the API (can cause 400 for multi-turn for some users)
        current_prompt = (current_prompt or "").strip() if current_prompt is not None else ""
        if dialog_history:
            for i, (user_text, assistant_text) in enumerate(dialog_history):
                u_text = (user_text or "").strip() if user_text is not None else ""
                a_text = (assistant_text or "").strip() if assistant_text is not None else ""
                h_handle, h_mime = (None, None)
                if history_handle_ids and i < len(history_handle_ids):
                    t = history_handle_ids[i]
                    if isinstance(t, (list, tuple)) and len(t) >= 2:
                        h_handle = t[0] if t[0] else None
                        h_mime = (t[1] or "").strip() or None
                if h_handle:
                    for msg in self._build_message_metagen("user", u_text, attachment_handle_id=h_handle, attachment_mime=h_mime):
                        messages.append(msg)
                else:
                    messages.extend(self._build_message_metagen("user", u_text))
                messages.extend(self._build_message_metagen("assistant", a_text))
        if current_turn_handle_id:
            for msg in self._build_message_metagen("user", current_prompt, attachment_handle_id=current_turn_handle_id, attachment_mime=current_turn_mime):
                messages.append(msg)
        else:
            messages.extend(self._build_message_metagen("user", current_prompt))
        return messages

    def _extract_text_from_metagen_response(self, data):
        """Extract assistant text from Meta API dialog_candidates response."""
        if not data or "dialog_candidates" not in data or not data["dialog_candidates"]:
            return ""
        cand = data["dialog_candidates"][0]
        dialog = cand.get("dialog") or {}
        messages = dialog.get("messages") or []
        if not messages:
            return ""
        last_msg = messages[-1]
        contents = last_msg.get("contents") or []
        if not contents:
            return ""
        text_obj = contents[0].get("text") or {}
        return text_obj.get("text") or ""

    def _call_generation(self, model_name, dialog_id, messages, genai_api_key,
                         temperature=None, top_p=None, repetition_penalty=None):
        url = f"{GRAPH_BASE_URL}/llm_annotations_metagen_stream_turn"
        dialog_id = (dialog_id or "").strip()
        if not dialog_id:
            raise ValueError("dialog_id is required for metagen stream turn API (upcoming turns need a valid dialog from turn 1)")

        payload = {
            "access_token": genai_api_key,
            "dialog": {"messages": messages},
            "workstream": WORKSTREAM,
            "model": model_name,
            "dialog_id": dialog_id,
        }
        if temperature is not None and temperature > 0:
            payload["temperature"] = temperature
        if top_p is not None and top_p > 0:
            payload["top_p"] = top_p
        if repetition_penalty is not None and repetition_penalty > 0:
            payload["repetition_penalty"] = repetition_penalty

        response = requests.post(url, json=payload, timeout=60)
        if not response.ok:
            _logger.warning(
                "Metagen API error status=%s dialog_id_set=%s num_messages=%s body=%s",
                response.status_code, bool(dialog_id), len(messages), (response.text or "")[:500]
            )
        response.raise_for_status()

        # Response is streamed (multiple JSON objects per line),
        # parse the last one containing dialog_candidates
        data = None
        for line in reversed(response.text.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if "dialog_candidates" in parsed:
                    data = parsed
                    break
            except json.JSONDecodeError:
                continue

        if data is None:
            raise ValueError("No valid response with dialog_candidates found from GenAI API")

        return data

    def _generate_response_a_and_b_with_models(
        self,
        model_1,
        model_2,
        prompt,
        genai_api_key,
        dialog_id,
        dialog_history=None,
        current_turn_handle_id=None,
        current_turn_mime=None,
        history_handle_ids=None,
        temperature_1=None, top_p_1=None, repetition_penalty_1=None,
        temperature_2=None, top_p_2=None, repetition_penalty_2=None,
    ):
        """Generate response A and B using router model_1 and model_2 with their respective generation params."""
        model_a = (model_1 or "").strip() or DEFAULT_MODEL_A
        model_b = (model_2 or "").strip() or DEFAULT_MODEL_B
        prompt = (prompt or "").strip() or ""
        if not prompt and not current_turn_handle_id:
            raise ValueError("prompt or current_turn_handle_id is required")
        messages = self._build_messages_for_metagen(
            dialog_history or [],
            prompt,
            current_turn_handle_id=current_turn_handle_id,
            current_turn_mime=current_turn_mime,
            history_handle_ids=history_handle_ids,
        )
        result = {"response_a": "", "response_b": "", "dialog_id": dialog_id, "errors": {}}

        try:
            raw_a = self._call_generation(model_a, dialog_id, messages, genai_api_key,
                                          temperature=temperature_1, top_p=top_p_1,
                                          repetition_penalty=repetition_penalty_1)
            result["response_a"] = self._extract_text_from_metagen_response(raw_a) or ""
        except Exception as e:
            result["errors"]["a"] = str(e)
            result["response_a"] = ""

        try:
            raw_b = self._call_generation(model_b, dialog_id, messages, genai_api_key,
                                          temperature=temperature_2, top_p=top_p_2,
                                          repetition_penalty=repetition_penalty_2)
            result["response_b"] = self._extract_text_from_metagen_response(raw_b) or ""
        except Exception as e:
            result["errors"]["b"] = str(e)
            result["response_b"] = ""

        # Log response A and B API outputs as raw JSON
        payload = {"record_id": self.id, "response_a": result.get("response_a") or "", "response_b": result.get("response_b") or "", "errors": result.get("errors") or {}}
        raw_json = json.dumps(payload, default=str)
        _logger.info("Response generation API output (record=%s): %s", self.id, raw_json)
        print("[valor] Response generation API output: %s" % raw_json)

        return result

    def _trigger_universal_background_task(self, gemini_api_key, genai_api_key, openai_api_key, dialog_id, model_1, model_2):
        record_ids = self.ids
        model_name = self._name
        user_id = 3 or self.env.uid
        context = dict(self.env.context)
        db_name = self.env.cr.dbname
        def background_worker():
            with registry(db_name).cursor() as new_cr:
                try:
                    new_env = api.Environment(new_cr, user_id, context)
                    records = new_env[model_name].browse(record_ids)
                    for i in records:
                        if not (i.client_prompt1 or i.image_1):
                            continue
                        i._ensure_image_handle_for_turn(1, genai_api_key)
                        current_handle = (i.image_handle_id_1 or "").strip() or None
                        current_mime = (i.image_mime_1 or "").strip() or None
                        response = i._generate_response_a_and_b_with_models(
                            model_1 or DEFAULT_MODEL_A,
                            model_2 or DEFAULT_MODEL_B,
                            prompt=(i.client_prompt1 or "").strip() or "",
                            genai_api_key=genai_api_key,
                            dialog_id=dialog_id,
                            dialog_history=None,
                            current_turn_handle_id=current_handle,
                            current_turn_mime=current_mime,
                            history_handle_ids=None,
                        )
                        if response:
                            i.sudo().write({
                                'is_processed': True,
                                'client_response_a1': response.get('response_a') or '',
                                'client_response_b1': response.get('response_b') or ''
                            })
                    new_cr.commit()
                except Exception as e:
                    _logger.info(f"{e}")
        _logger.info(f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine started")
        background_worker()
        _logger.info(f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine End")

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    rater_id = fields.Char(string="Rater ID", compute='_compute_rater_id', store=True)
    dialog_id = fields.Char()
    model_1 = fields.Char("Router model A")
    model_2 = fields.Char("Router model B")
    temperature_1 = fields.Float("Temperature A")
    top_p_1 = fields.Float("Top-p A")
    repetition_penalty_1 = fields.Float("Repetition Penalty A")
    temperature_2 = fields.Float("Temperature B")
    top_p_2 = fields.Float("Top-p B")
    repetition_penalty_2 = fields.Float("Repetition Penalty B")
    l0 = fields.Many2one('domain.level', string='Level 0',
                         domain="[('parent_id', '=', False)]")
    l1 = fields.Many2one('domain.level', string='Level 1')
    l2 = fields.Many2one('domain.level', string='Level 2')
    task_status = fields.Selection([('Submitted', 'Submitted'), ('NotSubmitted', 'Not Submitted')])
    employee_id = fields.Many2one('hr.employee')
    user_id = fields.Many2one(related='employee_id.user_id')
    is_ratable = fields.Boolean()
    is_processed = fields.Boolean()
    is_eval_done = fields.Boolean()
    turn_ids = fields.One2many('valor.turn', 'valor_id', string='Turns')

    def action_add_turn(self):
        """Create the first turn for this record (shown when no turns exist)."""
        self.ensure_one()
        next_seq = max(self.turn_ids.mapped('sequence') or [0]) + 1
        self.env['valor.turn'].create({
            'valor_id': self.id,
            'sequence': next_seq,
        })

    is_randomized = fields.Boolean()
    is_randomized_1 = fields.Boolean()
    is_randomized_2 = fields.Boolean()
    is_randomized_3 = fields.Boolean()
    is_randomized_4 = fields.Boolean()
    is_randomized_5 = fields.Boolean()
    is_randomized_6 = fields.Boolean()
    is_randomized_7 = fields.Boolean()
    prompt_rejection_reason = fields.Text()
    rejection_reason = fields.Selection([('Image Handling', 'Image Handling'), ('Missing Reference Text', 'Missing Reference Text'),
                                         ('Safety Concerns', 'Safety Concerns'), ('Gibberish / Nonsensical Content', 'Gibberish / Nonsensical Content'),
                                         ('Contains Personal Identifiable Information', 'Contains Personal Identifiable Information'),
                                         ('Requires Localized or Real-Time Info', 'Requires Localized or Real-Time Info'),
                                         ('Identity Requests', 'Identity Requests'), ('Prompt is In A Foreign (non-English) Language', 'Prompt is In A Foreign (non-English) Language')])
    qc_task_status = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])
    qc_task_status_1 = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])
    qc_task_status_2 = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])
    qc_task_status_3 = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])
    qc_task_status_4 = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])
    qc_task_status_5 = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])
    qc_task_status_6 = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])
    qc_task_status_7 = fields.Selection([('pass', 'Pass'), ('fail', 'Fail')])

    response_generating_1 = fields.Boolean(string='Response generating (Turn 1)', readonly=True)
    response_generating_2 = fields.Boolean(string='Response generating (Turn 2)', readonly=True)
    response_generating_3 = fields.Boolean(string='Response generating (Turn 3)', readonly=True)
    response_generating_4 = fields.Boolean(string='Response generating (Turn 4)', readonly=True)
    response_generating_5 = fields.Boolean(string='Response generating (Turn 5)', readonly=True)
    response_generating_6 = fields.Boolean(string='Response generating (Turn 6)', readonly=True)
    response_generating_7 = fields.Boolean(string='Response generating (Turn 7)', readonly=True)

    qc_running_1 = fields.Boolean(string='QC running (Turn 1)', readonly=True)
    qc_running_2 = fields.Boolean(string='QC running (Turn 2)', readonly=True)
    qc_running_3 = fields.Boolean(string='QC running (Turn 3)', readonly=True)
    qc_running_4 = fields.Boolean(string='QC running (Turn 4)', readonly=True)
    qc_running_5 = fields.Boolean(string='QC running (Turn 5)', readonly=True)
    qc_running_6 = fields.Boolean(string='QC running (Turn 6)', readonly=True)
    qc_running_7 = fields.Boolean(string='QC running (Turn 7)', readonly=True)

    eval_running_1 = fields.Boolean(string='Eval running (Turn 1)', readonly=True)
    eval_running_2 = fields.Boolean(string='Eval running (Turn 2)', readonly=True)
    eval_running_3 = fields.Boolean(string='Eval running (Turn 3)', readonly=True)
    eval_running_4 = fields.Boolean(string='Eval running (Turn 4)', readonly=True)
    eval_running_5 = fields.Boolean(string='Eval running (Turn 5)', readonly=True)
    eval_running_6 = fields.Boolean(string='Eval running (Turn 6)', readonly=True)
    eval_running_7 = fields.Boolean(string='Eval running (Turn 7)', readonly=True)

    client_prompt1 = fields.Text()
    client_prompt2 = fields.Text()
    client_prompt3 = fields.Text()
    client_prompt4 = fields.Text()
    client_prompt5 = fields.Text()
    client_prompt6 = fields.Text()
    client_prompt7 = fields.Text()

    store_client_prompt1 = fields.Text()
    store_client_prompt2 = fields.Text()
    store_client_prompt3 = fields.Text()
    store_client_prompt4 = fields.Text()
    store_client_prompt5 = fields.Text()
    store_client_prompt6 = fields.Text()
    store_client_prompt7 = fields.Text()

    image_1 = fields.Binary("Image (turn 1)", attachment=False)
    image_mime_1 = fields.Char("Image MIME (turn 1)")
    image_handle_id_1 = fields.Char("Image handle_id (turn 1)")
    image_2 = fields.Binary("Image (turn 2)", attachment=False)
    image_mime_2 = fields.Char("Image MIME (turn 2)")
    image_handle_id_2 = fields.Char("Image handle_id (turn 2)")
    image_3 = fields.Binary("Image (turn 3)", attachment=False)
    image_mime_3 = fields.Char("Image MIME (turn 3)")
    image_handle_id_3 = fields.Char("Image handle_id (turn 3)")
    image_4 = fields.Binary("Image (turn 4)", attachment=False)
    image_mime_4 = fields.Char("Image MIME (turn 4)")
    image_handle_id_4 = fields.Char("Image handle_id (turn 4)")
    image_5 = fields.Binary("Image (turn 5)", attachment=False)
    image_mime_5 = fields.Char("Image MIME (turn 5)")
    image_handle_id_5 = fields.Char("Image handle_id (turn 5)")
    image_6 = fields.Binary("Image (turn 6)", attachment=False)
    image_mime_6 = fields.Char("Image MIME (turn 6)")
    image_handle_id_6 = fields.Char("Image handle_id (turn 6)")
    image_7 = fields.Binary("Image (turn 7)", attachment=False)
    image_mime_7 = fields.Char("Image MIME (turn 7)")
    image_handle_id_7 = fields.Char("Image handle_id (turn 7)")

    client_response_a1 = fields.Text()
    client_response_b1 = fields.Text()

    client_response_a2 = fields.Text()
    client_response_b2 = fields.Text()

    client_response_a3 = fields.Text()
    client_response_b3 = fields.Text()

    client_response_a4 = fields.Text()
    client_response_b4 = fields.Text()

    client_response_a5 = fields.Text()
    client_response_b5 = fields.Text()

    client_response_a6 = fields.Text()
    client_response_b6 = fields.Text()

    client_response_a7 = fields.Text()
    client_response_b7 = fields.Text()

    truthfulness_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    truthfulness_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    ab_preference1 = fields.Selection([('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    ab_comment1 = fields.Text()

    truthfulness_a2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_a2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_a2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_a2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_a2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_a2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    truthfulness_b2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_b2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_b2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_b2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_b2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_b2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    ab_preference2 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    ab_comment2 = fields.Text()

    truthfulness_a3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_a3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_a3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_a3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_a3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_a3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    truthfulness_b3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_b3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_b3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_b3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_b3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_b3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    ab_preference3 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    ab_comment3 = fields.Text()

    truthfulness_a4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_a4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_a4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_a4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_a4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_a4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    truthfulness_b4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_b4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_b4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_b4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_b4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_b4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    ab_preference4 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    ab_comment4 = fields.Text()

    truthfulness_a5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_a5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_a5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_a5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_a5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_a5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    truthfulness_b5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_b5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_b5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_b5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_b5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_b5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    ab_preference5 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    ab_comment5 = fields.Text()

    truthfulness_a6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_a6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_a6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_a6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_a6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_a6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    truthfulness_b6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_b6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_b6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_b6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_b6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_b6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    ab_preference6 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    ab_comment6 = fields.Text()

    truthfulness_a7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_a7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_a7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_a7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_a7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_a7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    truthfulness_b7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    instruction_following_b7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    writing_quality_b7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    verbosity_b7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    prompt_correctness_b7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    overall_quality_b7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    ab_preference7 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    ab_comment7 = fields.Text()

    # fields to store in db only start
    store_truthfulness_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_a1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_truthfulness_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_b1 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_ab_preference1 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    store_ab_comment1 = fields.Text()

    store_truthfulness_a2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_a2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_a2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_a2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_a2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_a2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_truthfulness_b2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_b2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_b2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_b2 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_b2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_b2 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_ab_preference2 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    store_ab_comment2 = fields.Text()

    store_truthfulness_a3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_a3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_a3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_a3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_a3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_a3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_truthfulness_b3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_b3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_b3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_b3 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_b3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_b3 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_ab_preference3 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    store_ab_comment3 = fields.Text()

    store_truthfulness_a4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_a4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_a4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_a4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_a4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_a4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_truthfulness_b4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_b4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_b4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_b4 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_b4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_b4 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_ab_preference4 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    store_ab_comment4 = fields.Text()

    store_truthfulness_a5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_a5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_a5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_a5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_a5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_a5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_truthfulness_b5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_b5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_b5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_b5 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_b5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_b5 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_ab_preference5 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    store_ab_comment5 = fields.Text()

    store_truthfulness_a6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_a6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_a6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_a6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_a6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_a6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_truthfulness_b6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_b6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_b6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_b6 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_b6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_b6 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_ab_preference6 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    store_ab_comment6 = fields.Text()

    store_truthfulness_a7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_a7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_a7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_a7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_a7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_a7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_truthfulness_b7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_instruction_following_b7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_writing_quality_b7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_verbosity_b7 = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_prompt_correctness_b7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_overall_quality_b7 = fields.Selection(
        [('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6')])
    store_ab_preference7 = fields.Selection(
        [('-3', '-3'), ('-2', '-2'), ('-1', '-1'), ('0', '0'), ('1', '1'), ('2', '2'), ('3', '3')])
    store_ab_comment7 = fields.Text()
    # end
    # tooltip fields start
    reason1_truthfulness_a = fields.Text()
    reason1_instruction_following_a = fields.Text()
    reason1_writing_quality_a = fields.Text()
    reason1_verbosity_a = fields.Text()
    reason1_prompt_correctness_a = fields.Text()
    reason1_overall_quality_a = fields.Text()
    reason1_truthfulness_b = fields.Text()
    reason1_instruction_following_b = fields.Text()
    reason1_writing_quality_b = fields.Text()
    reason1_verbosity_b = fields.Text()
    reason1_prompt_correctness_b = fields.Text()
    reason1_overall_quality_b = fields.Text()
    reason1_ab_preference = fields.Text()
    reason1_ab_comment = fields.Text()

    reason2_truthfulness_a = fields.Text()
    reason2_instruction_following_a = fields.Text()
    reason2_writing_quality_a = fields.Text()
    reason2_verbosity_a = fields.Text()
    reason2_prompt_correctness_a = fields.Text()
    reason2_overall_quality_a = fields.Text()
    reason2_truthfulness_b = fields.Text()
    reason2_instruction_following_b = fields.Text()
    reason2_writing_quality_b = fields.Text()
    reason2_verbosity_b = fields.Text()
    reason2_prompt_correctness_b = fields.Text()
    reason2_overall_quality_b = fields.Text()
    reason2_ab_preference = fields.Text()
    reason2_ab_comment = fields.Text()

    reason3_truthfulness_a = fields.Text()
    reason3_instruction_following_a = fields.Text()
    reason3_writing_quality_a = fields.Text()
    reason3_verbosity_a = fields.Text()
    reason3_prompt_correctness_a = fields.Text()
    reason3_overall_quality_a = fields.Text()
    reason3_truthfulness_b = fields.Text()
    reason3_instruction_following_b = fields.Text()
    reason3_writing_quality_b = fields.Text()
    reason3_verbosity_b = fields.Text()
    reason3_prompt_correctness_b = fields.Text()
    reason3_overall_quality_b = fields.Text()
    reason3_ab_preference = fields.Text()
    reason3_ab_comment = fields.Text()

    reason4_truthfulness_a = fields.Text()
    reason4_instruction_following_a = fields.Text()
    reason4_writing_quality_a = fields.Text()
    reason4_verbosity_a = fields.Text()
    reason4_prompt_correctness_a = fields.Text()
    reason4_overall_quality_a = fields.Text()
    reason4_truthfulness_b = fields.Text()
    reason4_instruction_following_b = fields.Text()
    reason4_writing_quality_b = fields.Text()
    reason4_verbosity_b = fields.Text()
    reason4_prompt_correctness_b = fields.Text()
    reason4_overall_quality_b = fields.Text()
    reason4_ab_preference = fields.Text()
    reason4_ab_comment = fields.Text()

    reason5_truthfulness_a = fields.Text()
    reason5_instruction_following_a = fields.Text()
    reason5_writing_quality_a = fields.Text()
    reason5_verbosity_a = fields.Text()
    reason5_prompt_correctness_a = fields.Text()
    reason5_overall_quality_a = fields.Text()
    reason5_truthfulness_b = fields.Text()
    reason5_instruction_following_b = fields.Text()
    reason5_writing_quality_b = fields.Text()
    reason5_verbosity_b = fields.Text()
    reason5_prompt_correctness_b = fields.Text()
    reason5_overall_quality_b = fields.Text()
    reason5_ab_preference = fields.Text()
    reason5_ab_comment = fields.Text()

    reason6_truthfulness_a = fields.Text()
    reason6_instruction_following_a = fields.Text()
    reason6_writing_quality_a = fields.Text()
    reason6_verbosity_a = fields.Text()
    reason6_prompt_correctness_a = fields.Text()
    reason6_overall_quality_a = fields.Text()
    reason6_truthfulness_b = fields.Text()
    reason6_instruction_following_b = fields.Text()
    reason6_writing_quality_b = fields.Text()
    reason6_verbosity_b = fields.Text()
    reason6_prompt_correctness_b = fields.Text()
    reason6_overall_quality_b = fields.Text()
    reason6_ab_preference = fields.Text()
    reason6_ab_comment = fields.Text()

    reason7_truthfulness_a = fields.Text()
    reason7_instruction_following_a = fields.Text()
    reason7_writing_quality_a = fields.Text()
    reason7_verbosity_a = fields.Text()
    reason7_prompt_correctness_a = fields.Text()
    reason7_overall_quality_a = fields.Text()
    reason7_truthfulness_b = fields.Text()
    reason7_instruction_following_b = fields.Text()
    reason7_writing_quality_b = fields.Text()
    reason7_verbosity_b = fields.Text()
    reason7_prompt_correctness_b = fields.Text()
    reason7_overall_quality_b = fields.Text()
    reason7_ab_preference = fields.Text()
    reason7_ab_comment = fields.Text()
    # end
    # indicator fields start
    error1_truthfulness_a = fields.Boolean(default=False)
    error1_instruction_following_a = fields.Boolean(default=False)
    error1_writing_quality_a = fields.Boolean(default=False)
    error1_verbosity_a = fields.Boolean(default=False)
    error1_prompt_correctness_a = fields.Boolean(default=False)
    error1_overall_quality_a = fields.Boolean(default=False)
    error1_truthfulness_b = fields.Boolean(default=False)
    error1_instruction_following_b = fields.Boolean(default=False)
    error1_writing_quality_b = fields.Boolean(default=False)
    error1_verbosity_b = fields.Boolean(default=False)
    error1_prompt_correctness_b = fields.Boolean(default=False)
    error1_overall_quality_b = fields.Boolean(default=False)
    error1_ab_preference = fields.Boolean(default=False)
    error1_ab_comment = fields.Boolean(default=False)

    error2_truthfulness_a = fields.Boolean(default=False)
    error2_instruction_following_a = fields.Boolean(default=False)
    error2_writing_quality_a = fields.Boolean(default=False)
    error2_verbosity_a = fields.Boolean(default=False)
    error2_prompt_correctness_a = fields.Boolean(default=False)
    error2_overall_quality_a = fields.Boolean(default=False)
    error2_truthfulness_b = fields.Boolean(default=False)
    error2_instruction_following_b = fields.Boolean(default=False)
    error2_writing_quality_b = fields.Boolean(default=False)
    error2_verbosity_b = fields.Boolean(default=False)
    error2_prompt_correctness_b = fields.Boolean(default=False)
    error2_overall_quality_b = fields.Boolean(default=False)
    error2_ab_preference = fields.Boolean(default=False)
    error2_ab_comment = fields.Boolean(default=False)

    error3_truthfulness_a = fields.Boolean(default=False)
    error3_instruction_following_a = fields.Boolean(default=False)
    error3_writing_quality_a = fields.Boolean(default=False)
    error3_verbosity_a = fields.Boolean(default=False)
    error3_prompt_correctness_a = fields.Boolean(default=False)
    error3_overall_quality_a = fields.Boolean(default=False)
    error3_truthfulness_b = fields.Boolean(default=False)
    error3_instruction_following_b = fields.Boolean(default=False)
    error3_writing_quality_b = fields.Boolean(default=False)
    error3_verbosity_b = fields.Boolean(default=False)
    error3_prompt_correctness_b = fields.Boolean(default=False)
    error3_overall_quality_b = fields.Boolean(default=False)
    error3_ab_preference = fields.Boolean(default=False)
    error3_ab_comment = fields.Boolean(default=False)

    error4_truthfulness_a = fields.Boolean(default=False)
    error4_instruction_following_a = fields.Boolean(default=False)
    error4_writing_quality_a = fields.Boolean(default=False)
    error4_verbosity_a = fields.Boolean(default=False)
    error4_prompt_correctness_a = fields.Boolean(default=False)
    error4_overall_quality_a = fields.Boolean(default=False)
    error4_truthfulness_b = fields.Boolean(default=False)
    error4_instruction_following_b = fields.Boolean(default=False)
    error4_writing_quality_b = fields.Boolean(default=False)
    error4_verbosity_b = fields.Boolean(default=False)
    error4_prompt_correctness_b = fields.Boolean(default=False)
    error4_overall_quality_b = fields.Boolean(default=False)
    error4_ab_preference = fields.Boolean(default=False)
    error4_ab_comment = fields.Boolean(default=False)

    error5_truthfulness_a = fields.Boolean(default=False)
    error5_instruction_following_a = fields.Boolean(default=False)
    error5_writing_quality_a = fields.Boolean(default=False)
    error5_verbosity_a = fields.Boolean(default=False)
    error5_prompt_correctness_a = fields.Boolean(default=False)
    error5_overall_quality_a = fields.Boolean(default=False)
    error5_truthfulness_b = fields.Boolean(default=False)
    error5_instruction_following_b = fields.Boolean(default=False)
    error5_writing_quality_b = fields.Boolean(default=False)
    error5_verbosity_b = fields.Boolean(default=False)
    error5_prompt_correctness_b = fields.Boolean(default=False)
    error5_overall_quality_b = fields.Boolean(default=False)
    error5_ab_preference = fields.Boolean(default=False)
    error5_ab_comment = fields.Boolean(default=False)

    error6_truthfulness_a = fields.Boolean(default=False)
    error6_instruction_following_a = fields.Boolean(default=False)
    error6_writing_quality_a = fields.Boolean(default=False)
    error6_verbosity_a = fields.Boolean(default=False)
    error6_prompt_correctness_a = fields.Boolean(default=False)
    error6_overall_quality_a = fields.Boolean(default=False)
    error6_truthfulness_b = fields.Boolean(default=False)
    error6_instruction_following_b = fields.Boolean(default=False)
    error6_writing_quality_b = fields.Boolean(default=False)
    error6_verbosity_b = fields.Boolean(default=False)
    error6_prompt_correctness_b = fields.Boolean(default=False)
    error6_overall_quality_b = fields.Boolean(default=False)
    error6_ab_preference = fields.Boolean(default=False)
    error6_ab_comment = fields.Boolean(default=False)

    error7_truthfulness_a = fields.Boolean(default=False)
    error7_instruction_following_a = fields.Boolean(default=False)
    error7_writing_quality_a = fields.Boolean(default=False)
    error7_verbosity_a = fields.Boolean(default=False)
    error7_prompt_correctness_a = fields.Boolean(default=False)
    error7_overall_quality_a = fields.Boolean(default=False)
    error7_truthfulness_b = fields.Boolean(default=False)
    error7_instruction_following_b = fields.Boolean(default=False)
    error7_writing_quality_b = fields.Boolean(default=False)
    error7_verbosity_b = fields.Boolean(default=False)
    error7_prompt_correctness_b = fields.Boolean(default=False)
    error7_overall_quality_b = fields.Boolean(default=False)
    error7_ab_preference = fields.Boolean(default=False)
    error7_ab_comment = fields.Boolean(default=False)
    # end
    qc_score = fields.Integer(string='QC score (/5)')
    is_tasker = fields.Boolean(compute='_compute_is_tasker')

    @api.onchange('l0')
    def _onchange_l0(self):
        self.l1 = False
        self.l2 = False

    @api.onchange('l1')
    def _onchange_l1(self):
        self.l2 = False

    def _run_kimi_qc_after_eval(self, eval_result, prompt, response_a, response_b, ab_preference_val, ab_comment_val, turn, eval_data=None):
        """
        Run Kimi QC synchronously and return write_vals to apply in the caller's transaction.
        Returns dict of fields to write, or None on failure.
        Caller must do a single write + commit to avoid concurrent update.
        """
        _logger.info('_run_kimi_qc_after_eval called (turn=%s)', turn)
        if not eval_result or not isinstance(eval_result, list) or len(eval_result) == 0:
            _logger.warning('_run_kimi_qc_after_eval: no eval_result, skipping')
            return None
        idx = min(turn - 1, len(eval_result) - 1)
        ev = eval_result[idx]
        ai_er = ev.get('evaluation_result') or {}
        comparison_ab = ev.get('comparison_ab') or ai_er.get('comparison_ab')

        t = turn
        def _hscore(field):
            if eval_data and field.startswith(f'reason{t}_'):
                eval_key = 'reason_' + field[len(f'reason{t}_'):]
                return eval_data.get(eval_key, '') or ''
            return getattr(self, field, None) or ''
        human_er = {
            'response_a': {
                'instruction_following': {'score': _hscore(f'instruction_following_a{t}'), 'reason': _hscore(f'reason{t}_instruction_following_a')},
                'truthfulness': {'score': _hscore(f'truthfulness_a{t}'), 'reason': _hscore(f'reason{t}_truthfulness_a')},
                'writing_style': {'score': _hscore(f'writing_quality_a{t}'), 'reason': _hscore(f'reason{t}_writing_quality_a')},
                'verbosity': {'score': _hscore(f'verbosity_a{t}'), 'reason': _hscore(f'reason{t}_verbosity_a')},
                'prompt_correctness': {'score': _hscore(f'prompt_correctness_a{t}'), 'reason': _hscore(f'reason{t}_prompt_correctness_a')},
            },
            'response_b': {
                'instruction_following': {'score': _hscore(f'instruction_following_b{t}'), 'reason': _hscore(f'reason{t}_instruction_following_b')},
                'truthfulness': {'score': _hscore(f'truthfulness_b{t}'), 'reason': _hscore(f'reason{t}_truthfulness_b')},
                'writing_style': {'score': _hscore(f'writing_quality_b{t}'), 'reason': _hscore(f'reason{t}_writing_quality_b')},
                'verbosity': {'score': _hscore(f'verbosity_b{t}'), 'reason': _hscore(f'reason{t}_verbosity_b')},
                'prompt_correctness': {'score': _hscore(f'prompt_correctness_b{t}'), 'reason': _hscore(f'reason{t}_prompt_correctness_b')},
            },
        }
        qc_inputs_kimi = [{
            'task_id': self.task_id,
            'prompt': prompt or '',
            'response_a': response_a or '',
            'response_b': response_b or '',
            'evaluation_result': human_er,
            'comparison_ab': comparison_ab,
            'ab_preference': ab_preference_val,
            'ab_comment': ab_comment_val or '',
        }]
        record_id = self.id
        ab_comment_val_capture = ab_comment_val
        eval_data_capture = eval_data

        _logger.info('Kimi QC (turn %s): calling run_qc_kimi...', turn)
        try:
            qc_data = run_qc_kimi(kimi_api_key=kimi_api_key, qc_inputs=qc_inputs_kimi)
        except Exception as e:
            _logger.exception('Kimi QC (turn %s) failed: %s', turn, e)
            return None
        if not qc_data:
            return None
        raw_qc_json = json.dumps({"turn": turn, "record_id": record_id, "qc_data": qc_data}, default=str)
        _logger.info("Kimi QC API output (turn=%s, record=%s): %s", turn, record_id, raw_qc_json)
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
                    comment_lower = (ab_comment_val_capture or '').lower()
                    valid_words = [w.strip() for w in flagged_str.split(',') if w.strip() and w.strip().lower() in comment_lower]
                    if valid_words:
                        reason_comment_parts.append('ai_flagged: ' + ', '.join(valid_words))
        write_vals = {
            'qc_task_status': qc_status,
            f'qc_task_status_{turn}': qc_status,
            'reason%d_ab_preference' % turn: reason_pref,
            'reason%d_ab_comment' % turn: '\n'.join(reason_comment_parts) if reason_comment_parts else '',
            'error%d_ab_comment' % turn: bool(reason_comment_parts),
        }
        for _dim, _side in (
            ('truthfulness', 'a'), ('truthfulness', 'b'),
            ('instruction_following', 'a'), ('instruction_following', 'b'),
            ('writing_quality', 'a'), ('writing_quality', 'b'),
            ('verbosity', 'a'), ('verbosity', 'b'),
            ('prompt_correctness', 'a'), ('prompt_correctness', 'b'),
            ('overall_quality', 'a'), ('overall_quality', 'b'),
        ):
            _human = getattr(self, f'{_dim}_{_side}{turn}', None)
            _ai = getattr(self, f'store_{_dim}_{_side}{turn}', None)
            if _human and _ai:
                write_vals[f'error{turn}_{_dim}_{_side}'] = check_error(int(_human), int(_ai))
        _human_pref = getattr(self, f'ab_preference{turn}', None)
        _ai_pref = getattr(self, f'store_ab_preference{turn}', None)
        _eval_pref_err = check_error(int(_human_pref), int(_ai_pref)) if _human_pref and _ai_pref else False
        write_vals[f'error{turn}_ab_preference'] = _eval_pref_err or bool(reason_pref)
        if eval_data_capture:
            for _dim in ('truthfulness', 'instruction_following', 'writing_quality',
                         'verbosity', 'prompt_correctness', 'overall_quality'):
                for _side in ('a', 'b'):
                    write_vals[f'reason{turn}_{_dim}_{_side}'] = eval_data_capture.get(f'reason_{_dim}_{_side}', '') or ''
        return write_vals

    def _compute_human_weighted_score(self, turn, side):
        """Compute weighted overall quality from the human rater's dimension scores."""
        weights = {
            'instruction_following': 0.25,
            'truthfulness': 0.25,
            'prompt_correctness': 0.20,
            'writing_quality': 0.15,
            'verbosity': 0.15,
        }
        total = 0.0
        for dim, w in weights.items():
            val = getattr(self, f'{dim}_{side}{turn}', None)
            if val:
                total += float(val) * w
        return round(total, 2)

    def _run_eval_and_qc(self, turn, evaluation_inputs):
        """Run Kimi evaluation + QC synchronously in the caller's transaction."""
        try:
            eval_result = run_evaluation_kimi(kimi_api_key=kimi_api_key, evaluation_inputs=evaluation_inputs)
            if not eval_result:
                _logger.warning('Eval (turn %s): no eval_result returned', turn)
                return
            data = get_eval_data(eval_result, idx=turn - 1)
            if not data:
                _logger.warning('Eval (turn %s): get_eval_data returned nothing', turn)
                return

            human_wa = self._compute_human_weighted_score(turn, 'a')
            human_wb = self._compute_human_weighted_score(turn, 'b')

            store_vals = {
                f'store_truthfulness_a{turn}': data.get('truthfulness_a'),
                f'store_truthfulness_b{turn}': data.get('truthfulness_b'),
                f'store_instruction_following_a{turn}': data.get('instruction_following_a'),
                f'store_instruction_following_b{turn}': data.get('instruction_following_b'),
                f'store_writing_quality_a{turn}': data.get('writing_quality_a'),
                f'store_writing_quality_b{turn}': data.get('writing_quality_b'),
                f'store_prompt_correctness_a{turn}': data.get('prompt_correctness_a'),
                f'store_prompt_correctness_b{turn}': data.get('prompt_correctness_b'),
                f'store_verbosity_a{turn}': data.get('verbosity_a'),
                f'store_verbosity_b{turn}': data.get('verbosity_b'),
                f'store_overall_quality_a{turn}': str(int(human_wa)) if human_wa else data.get('overall_quality_a'),
                f'store_overall_quality_b{turn}': str(int(human_wb)) if human_wb else data.get('overall_quality_b'),
                f'store_ab_preference{turn}': data.get('ab_preference'),
                f'store_ab_comment{turn}': data.get('ab_comment'),
                'is_eval_done': True,
                f'reason{turn}_truthfulness_a': data.get('reason_truthfulness_a', ''),
                f'reason{turn}_truthfulness_b': data.get('reason_truthfulness_b', ''),
                f'reason{turn}_instruction_following_a': data.get('reason_instruction_following_a', ''),
                f'reason{turn}_instruction_following_b': data.get('reason_instruction_following_b', ''),
                f'reason{turn}_writing_quality_a': data.get('reason_writing_quality_a', ''),
                f'reason{turn}_writing_quality_b': data.get('reason_writing_quality_b', ''),
                f'reason{turn}_verbosity_a': data.get('reason_verbosity_a', ''),
                f'reason{turn}_verbosity_b': data.get('reason_verbosity_b', ''),
                f'reason{turn}_prompt_correctness_a': data.get('reason_prompt_correctness_a', ''),
                f'reason{turn}_prompt_correctness_b': data.get('reason_prompt_correctness_b', ''),
                f'reason{turn}_overall_quality_a': f"Weighted: IF×0.25 + Truth×0.25 + Correctness×0.20 + Writing×0.15 + Verbosity×0.15 = {human_wa:.2f}",
                f'reason{turn}_overall_quality_b': f"Weighted: IF×0.25 + Truth×0.25 + Correctness×0.20 + Writing×0.15 + Verbosity×0.15 = {human_wb:.2f}",
            }
            self.write(store_vals)
            qc_vals = self._run_kimi_qc_after_eval(
                eval_result,
                getattr(self, f'client_prompt{turn}'),
                getattr(self, f'client_response_a{turn}'),
                getattr(self, f'client_response_b{turn}'),
                getattr(self, f'ab_preference{turn}'),
                getattr(self, f'ab_comment{turn}'),
                turn,
                eval_data=data,
            )
            if qc_vals:
                self.write(qc_vals)
            _logger.info('Eval (turn %s) and QC completed', turn)
        except Exception as e:
            _logger.exception('Eval (turn %s) failed: %s', turn, e)

    def evaluate_button1(self):
        _logger.info('evaluate_button1 called (record id=%s)', self.id)
        if (not self.client_prompt1 or not self.truthfulness_a1 or not self.truthfulness_b1 or
                not self.instruction_following_a1 or not self.instruction_following_b1 or
                not self.writing_quality_a1 or not self.writing_quality_b1 or not self.verbosity_a1 or
                not self.verbosity_b1 or not self.prompt_correctness_a1 or
                not self.prompt_correctness_b1 or not self.overall_quality_a1 or
                not self.overall_quality_b1 or not self.ab_preference1 or not self.ab_comment1):
            raise ValidationError("Please fill all the dimensions")
        evaluation_inputs = [
            {'task_id': self.task_id, 'prompt': self.client_prompt1, 'response_a': self.client_response_a1, 'response_b': self.client_response_b1}]
        self._run_eval_and_qc(1, evaluation_inputs)

    def evaluate_button2(self):
        if (not self.client_prompt1 or not self.client_prompt2 or
                not self.truthfulness_a2 or not self.truthfulness_b2 or
                not self.instruction_following_a2 or not self.instruction_following_b2 or
                not self.writing_quality_a2 or not self.writing_quality_b2 or not self.verbosity_a2 or
                not self.verbosity_b2 or not self.prompt_correctness_a2 or
                not self.prompt_correctness_b2 or not self.overall_quality_a2 or
                not self.overall_quality_b2 or not self.ab_preference2 or not self.ab_comment2):
            raise ValidationError("Please fill all the dimensions")
        evaluation_inputs = [
            {'task_id': self.task_id, 'prompt': self.client_prompt1, 'response_a': self.client_response_a1, 'response_b': self.client_response_b1},
            {'task_id': self.task_id, 'prompt': self.client_prompt2, 'response_a': self.client_response_a2, 'response_b': self.client_response_b2}]
        self._run_eval_and_qc(2, evaluation_inputs)

    def evaluate_button3(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.truthfulness_a3 or not self.truthfulness_b3 or
                not self.instruction_following_a3 or not self.instruction_following_b3 or
                not self.writing_quality_a3 or not self.writing_quality_b3 or not self.verbosity_a3 or
                not self.verbosity_b3 or not self.prompt_correctness_a3 or
                not self.prompt_correctness_b3 or not self.overall_quality_a3 or
                not self.overall_quality_b3 or not self.ab_preference3 or not self.ab_comment3):
            raise ValidationError("Please fill all the dimensions")
        evaluation_inputs = [
            {'task_id': self.task_id, 'prompt': self.client_prompt1, 'response_a': self.client_response_a1, 'response_b': self.client_response_b1},
            {'task_id': self.task_id, 'prompt': self.client_prompt2, 'response_a': self.client_response_a2, 'response_b': self.client_response_b2},
            {'task_id': self.task_id, 'prompt': self.client_prompt3, 'response_a': self.client_response_a3, 'response_b': self.client_response_b3}]
        self._run_eval_and_qc(3, evaluation_inputs)

    def evaluate_button4(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.truthfulness_a4 or not self.truthfulness_b4 or
                not self.instruction_following_a4 or not self.instruction_following_b4 or
                not self.writing_quality_a4 or not self.writing_quality_b4 or not self.verbosity_a4 or
                not self.verbosity_b4 or not self.prompt_correctness_a4 or
                not self.prompt_correctness_b4 or not self.overall_quality_a4 or
                not self.overall_quality_b4 or not self.ab_preference4 or not self.ab_comment4):
            raise ValidationError("Please fill all the dimensions")
        evaluation_inputs = [
            {'task_id': self.task_id, 'prompt': self.client_prompt1, 'response_a': self.client_response_a1, 'response_b': self.client_response_b1},
            {'task_id': self.task_id, 'prompt': self.client_prompt2, 'response_a': self.client_response_a2, 'response_b': self.client_response_b2},
            {'task_id': self.task_id, 'prompt': self.client_prompt3, 'response_a': self.client_response_a3, 'response_b': self.client_response_b3},
            {'task_id': self.task_id, 'prompt': self.client_prompt4, 'response_a': self.client_response_a4, 'response_b': self.client_response_b4}]
        self._run_eval_and_qc(4, evaluation_inputs)

    def evaluate_button5(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5 or
                not self.truthfulness_a5 or not self.truthfulness_b5 or
                not self.instruction_following_a5 or not self.instruction_following_b5 or
                not self.writing_quality_a5 or not self.writing_quality_b5 or not self.verbosity_a5 or
                not self.verbosity_b5 or not self.prompt_correctness_a5 or
                not self.prompt_correctness_b5 or not self.overall_quality_a5 or
                not self.overall_quality_b5 or not self.ab_preference5 or not self.ab_comment5):
            raise ValidationError("Please fill all the dimensions")
        evaluation_inputs = [
            {'task_id': self.task_id, 'prompt': self.client_prompt1, 'response_a': self.client_response_a1, 'response_b': self.client_response_b1},
            {'task_id': self.task_id, 'prompt': self.client_prompt2, 'response_a': self.client_response_a2, 'response_b': self.client_response_b2},
            {'task_id': self.task_id, 'prompt': self.client_prompt3, 'response_a': self.client_response_a3, 'response_b': self.client_response_b3},
            {'task_id': self.task_id, 'prompt': self.client_prompt4, 'response_a': self.client_response_a4, 'response_b': self.client_response_b4},
            {'task_id': self.task_id, 'prompt': self.client_prompt5, 'response_a': self.client_response_a5, 'response_b': self.client_response_b5}]
        self._run_eval_and_qc(5, evaluation_inputs)

    def evaluate_button6(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5 or not self.client_prompt6 or
                not self.truthfulness_a6 or not self.truthfulness_b6 or
                not self.instruction_following_a6 or not self.instruction_following_b6 or
                not self.writing_quality_a6 or not self.writing_quality_b6 or not self.verbosity_a6 or
                not self.verbosity_b6 or not self.prompt_correctness_a6 or
                not self.prompt_correctness_b6 or not self.overall_quality_a6 or
                not self.overall_quality_b6 or not self.ab_preference6 or not self.ab_comment6):
            raise ValidationError("Please fill all the dimensions")
        evaluation_inputs = [
            {'task_id': self.task_id, 'prompt': self.client_prompt1, 'response_a': self.client_response_a1, 'response_b': self.client_response_b1},
            {'task_id': self.task_id, 'prompt': self.client_prompt2, 'response_a': self.client_response_a2, 'response_b': self.client_response_b2},
            {'task_id': self.task_id, 'prompt': self.client_prompt3, 'response_a': self.client_response_a3, 'response_b': self.client_response_b3},
            {'task_id': self.task_id, 'prompt': self.client_prompt4, 'response_a': self.client_response_a4, 'response_b': self.client_response_b4},
            {'task_id': self.task_id, 'prompt': self.client_prompt5, 'response_a': self.client_response_a5, 'response_b': self.client_response_b5},
            {'task_id': self.task_id, 'prompt': self.client_prompt6, 'response_a': self.client_response_a6, 'response_b': self.client_response_b6}]
        self._run_eval_and_qc(6, evaluation_inputs)

    def evaluate_button7(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5 or not self.client_prompt6 or
                not self.client_prompt7 or not self.truthfulness_a7 or not self.truthfulness_b7 or
                not self.instruction_following_a7 or not self.instruction_following_b7 or
                not self.writing_quality_a7 or not self.writing_quality_b7 or not self.verbosity_a7 or
                not self.verbosity_b7 or not self.prompt_correctness_a7 or
                not self.prompt_correctness_b7 or not self.overall_quality_a7 or
                not self.overall_quality_b7 or not self.ab_preference7 or not self.ab_comment7):
            raise ValidationError("Please fill all the dimensions")
        evaluation_inputs = [
            {'task_id': self.task_id, 'prompt': self.client_prompt1, 'response_a': self.client_response_a1, 'response_b': self.client_response_b1},
            {'task_id': self.task_id, 'prompt': self.client_prompt2, 'response_a': self.client_response_a2, 'response_b': self.client_response_b2},
            {'task_id': self.task_id, 'prompt': self.client_prompt3, 'response_a': self.client_response_a3, 'response_b': self.client_response_b3},
            {'task_id': self.task_id, 'prompt': self.client_prompt4, 'response_a': self.client_response_a4, 'response_b': self.client_response_b4},
            {'task_id': self.task_id, 'prompt': self.client_prompt5, 'response_a': self.client_response_a5, 'response_b': self.client_response_b5},
            {'task_id': self.task_id, 'prompt': self.client_prompt6, 'response_a': self.client_response_a6, 'response_b': self.client_response_b6},
            {'task_id': self.task_id, 'prompt': self.client_prompt7, 'response_a': self.client_response_a7, 'response_b': self.client_response_b7}]
        self._run_eval_and_qc(7, evaluation_inputs)

    def eval_task(self):
        gemini_api_key = os.getenv("gemini_api_key")
        try:
            list3 = [{
                'task_id': self.task_id,
                'prompt': self.client_prompt,
                'response_a': self.client_response_a,
                'response_b': self.client_response_b,
                'gemini_response': self.gemini_response,
                'gpt_response': self.gpt_response
            }]
            print('***************************************************88')
            print(list3)
            print('***************************************************88')
            tasks3_response = llm_actions.evaluation_for_tasks_sync(gemini_api_key=gemini_api_key,
                                                                    evaluation_inputs=list3)
            _logger.info('task3_response---------------------%s', tasks3_response)
            if tasks3_response:
                print(tasks3_response)
                _logger.info('tasks3_response---------------------------%s', tasks3_response)
                truthfulness_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['truthfulness'][
                        'score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                     tasks3_response[0]['evaluation_result'] and 'truthfulness' in \
                                     tasks3_response[0]['evaluation_result'][
                                         'response_a'] and 'score' in \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'truthfulness'] and \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'truthfulness']['score'] else ''
                instruction_following_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['instruction_following'][
                        'score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                     tasks3_response[0][
                                         'evaluation_result'] and 'instruction_following' in \
                                     tasks3_response[0]['evaluation_result'][
                                         'response_a'] and 'score' in \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'instruction_following'] and \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'instruction_following']['score'] else ''
                writing_quality_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['writing_style'][
                        'score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                     tasks3_response[0]['evaluation_result'] and 'writing_style' in \
                                     tasks3_response[0]['evaluation_result'][
                                         'response_a'] and 'score' in \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'writing_style'] and \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'writing_style']['score'] else ''
                verbosity_a = str(tasks3_response[0]['evaluation_result']['response_a']['verbosity'][
                                      'score']) if 'evaluation_result' in tasks3_response[
                    0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'verbosity' in \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_a'] and 'score' in \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_a']['verbosity'] and \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_a']['verbosity']['score'] else ''
                prompt_correctness_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['prompt_correctness'][
                        'score']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                     tasks3_response[0]['evaluation_result'] and 'prompt_correctness' in \
                                     tasks3_response[0]['evaluation_result'][
                                         'response_a'] and 'score' in \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'prompt_correctness'] and \
                                     tasks3_response[0]['evaluation_result']['response_a'][
                                         'prompt_correctness']['score'] else ''
                overall_quality_a = str(int(
                    tasks3_response[0]['evaluation_result']['response_a']['overall_quality'][
                        'weighted_score'])) if 'evaluation_result' in tasks3_response[
                    0] and 'response_a' in tasks3_response[0][
                                                   'evaluation_result'] and 'overall_quality' in \
                                               tasks3_response[0]['evaluation_result'][
                                                   'response_a'] and 'weighted_score' in \
                                               tasks3_response[0]['evaluation_result']['response_a'][
                                                   'overall_quality'] and \
                                               tasks3_response[0]['evaluation_result']['response_a'][
                                                   'overall_quality']['weighted_score'] else ''
                truthfulness_b = str(
                    tasks3_response[0]['evaluation_result']['response_b']['truthfulness'][
                        'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                                     tasks3_response[0]['evaluation_result'] and 'truthfulness' in \
                                     tasks3_response[0]['evaluation_result'][
                                         'response_b'] and 'score' in \
                                     tasks3_response[0]['evaluation_result']['response_b'][
                                         'truthfulness'] and \
                                     tasks3_response[0]['evaluation_result']['response_b'][
                                         'truthfulness']['score'] else ''
                instruction_following_b = \
                    str(tasks3_response[0]['evaluation_result']['response_b']['instruction_following'][
                            'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                                         tasks3_response[0][
                                             'evaluation_result'] and 'instruction_following' in \
                                         tasks3_response[0]['evaluation_result'][
                                             'response_b'] and 'score' in \
                                         tasks3_response[0]['evaluation_result']['response_b'][
                                             'instruction_following'] and \
                                         tasks3_response[0]['evaluation_result']['response_b'][
                                             'instruction_following']['score'] else ''
                writing_quality_b = str(
                    tasks3_response[0]['evaluation_result']['response_b']['writing_style'][
                        'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                                     tasks3_response[0]['evaluation_result'] and 'writing_style' in \
                                     tasks3_response[0]['evaluation_result'][
                                         'response_b'] and 'score' in \
                                     tasks3_response[0]['evaluation_result']['response_b'][
                                         'writing_style'] and \
                                     tasks3_response[0]['evaluation_result']['response_b'][
                                         'writing_style']['score'] else ''
                verbosity_b = str(tasks3_response[0]['evaluation_result']['response_b']['verbosity'][
                                      'score']) if 'evaluation_result' in tasks3_response[
                    0] and 'response_b' in \
                                                   tasks3_response[0][
                                                       'evaluation_result'] and 'verbosity' in \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_b'] and 'score' in \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_b']['verbosity'] and \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_b']['verbosity']['score'] else ''
                prompt_correctness_b = \
                    str(tasks3_response[0]['evaluation_result']['response_b']['prompt_correctness'][
                            'score']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                                         tasks3_response[0][
                                             'evaluation_result'] and 'prompt_correctness' in \
                                         tasks3_response[0]['evaluation_result'][
                                             'response_b'] and 'score' in \
                                         tasks3_response[0]['evaluation_result']['response_b'][
                                             'prompt_correctness'] and \
                                         tasks3_response[0]['evaluation_result']['response_b'][
                                             'prompt_correctness']['score'] else ''
                overall_quality_b = str(
                    int(tasks3_response[0]['evaluation_result']['response_b']['overall_quality'][
                            'weighted_score'])) if 'evaluation_result' in tasks3_response[
                    0] and 'response_b' in \
                                                   tasks3_response[0][
                                                       'evaluation_result'] and 'overall_quality' in \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_b'] and 'weighted_score' in \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_b'][
                                                       'overall_quality'] and \
                                                   tasks3_response[0]['evaluation_result'][
                                                       'response_b'][
                                                       'overall_quality']['weighted_score'] else ''

                reason_truthfulness_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['truthfulness'][
                        'reason']) if 'evaluation_result' in tasks3_response[
                    0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'truthfulness' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_a'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'truthfulness'] and \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'truthfulness']['reason'] else ''
                reason_instruction_following_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['instruction_following'][
                        'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                      tasks3_response[0][
                                          'evaluation_result'] and 'instruction_following' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_a'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'instruction_following'] and \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'instruction_following']['reason'] else ''
                reason_writing_quality_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['writing_style'][
                        'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                      tasks3_response[0]['evaluation_result'] and 'writing_style' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_a'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'writing_style'] and \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'writing_style'][
                                          'reason'] else ''
                reason_verbosity_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['verbosity'][
                        'reason']) if 'evaluation_result' in tasks3_response[
                    0] and 'response_a' in tasks3_response[0]['evaluation_result'] and 'verbosity' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_a'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'verbosity'] and \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'verbosity']['reason'] else ''
                reason_prompt_correctness_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['prompt_correctness'][
                        'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                      tasks3_response[0][
                                          'evaluation_result'] and 'prompt_correctness' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_a'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'prompt_correctness'] and \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'prompt_correctness']['reason'] else ''
                reason_overall_quality_a = str(
                    tasks3_response[0]['evaluation_result']['response_a']['overall_quality'][
                        'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_a' in \
                                      tasks3_response[0][
                                          'evaluation_result'] and 'overall_quality' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_a'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'overall_quality'] and \
                                      tasks3_response[0]['evaluation_result']['response_a'][
                                          'overall_quality']['reason'] else ''
                reason_truthfulness_b = str(
                    tasks3_response[0]['evaluation_result']['response_b']['truthfulness'][
                        'reason']) if 'evaluation_result' in tasks3_response[
                    0] and 'response_b' in \
                                      tasks3_response[0][
                                          'evaluation_result'] and 'truthfulness' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_b'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'truthfulness'] and \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'truthfulness']['reason'] else ''
                reason_instruction_following_b = \
                    str(tasks3_response[0]['evaluation_result']['response_b']['instruction_following'][
                            'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                                          tasks3_response[0][
                                              'evaluation_result'] and 'instruction_following' in \
                                          tasks3_response[0]['evaluation_result'][
                                              'response_b'] and 'reason' in \
                                          tasks3_response[0]['evaluation_result']['response_b'][
                                              'instruction_following'] and \
                                          tasks3_response[0]['evaluation_result']['response_b'][
                                              'instruction_following']['reason'] else ''
                reason_writing_quality_b = str(
                    tasks3_response[0]['evaluation_result']['response_b']['writing_style'][
                        'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                                      tasks3_response[0]['evaluation_result'] and 'writing_style' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_b'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'writing_style'] and \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'writing_style'][
                                          'reason'] else ''
                reason_verbosity_b = str(
                    tasks3_response[0]['evaluation_result']['response_b']['verbosity'][
                        'reason']) if 'evaluation_result' in tasks3_response[
                    0] and 'response_b' in \
                                      tasks3_response[0][
                                          'evaluation_result'] and 'verbosity' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_b'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'verbosity'] and \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'verbosity']['reason'] else ''
                reason_prompt_correctness_b = \
                    str(tasks3_response[0]['evaluation_result']['response_b']['prompt_correctness'][
                            'reason']) if 'evaluation_result' in tasks3_response[0] and 'response_b' in \
                                          tasks3_response[0][
                                              'evaluation_result'] and 'prompt_correctness' in \
                                          tasks3_response[0]['evaluation_result'][
                                              'response_b'] and 'reason' in \
                                          tasks3_response[0]['evaluation_result']['response_b'][
                                              'prompt_correctness'] and \
                                          tasks3_response[0]['evaluation_result']['response_b'][
                                              'prompt_correctness']['reason'] else ''
                reason_overall_quality_b = str(
                    tasks3_response[0]['evaluation_result']['response_b']['overall_quality'][
                        'reason']) if 'evaluation_result' in tasks3_response[
                    0] and 'response_b' in \
                                      tasks3_response[0][
                                          'evaluation_result'] and 'overall_quality' in \
                                      tasks3_response[0]['evaluation_result'][
                                          'response_b'] and 'reason' in \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'overall_quality'] and \
                                      tasks3_response[0]['evaluation_result']['response_b'][
                                          'overall_quality']['reason'] else ''

                ab_preference = str(
                    tasks3_response[0]['comparison_ab']['comparison_score']) if 'comparison_ab' in \
                                                                                tasks3_response[
                                                                                    0] and 'comparison_score' in \
                                                                                tasks3_response[0][
                                                                                    'comparison_ab'] and \
                                                                                tasks3_response[0][
                                                                                    'comparison_ab'][
                                                                                    'comparison_score'] else ''
                ab_comment = tasks3_response[0]['comparison_ab'][
                    'overall_comment'] if 'comparison_ab' in tasks3_response[0] and 'overall_comment' in \
                                          tasks3_response[0]['comparison_ab'] and \
                                          tasks3_response[0]['comparison_ab']['overall_comment'] else ''
                ab_gemini_preference = str(tasks3_response[0]['comparison_vs_gemini'][
                                               'comparison_score']) if 'comparison_vs_gemini' in \
                                                                       tasks3_response[
                                                                           0] and 'comparison_score' in \
                                                                       tasks3_response[0][
                                                                           'comparison_vs_gemini'] and \
                                                                       tasks3_response[0][
                                                                           'comparison_vs_gemini'][
                                                                           'comparison_score'] else ''
                ab_gemini_comment = str(tasks3_response[0]['comparison_vs_gemini'][
                                            'comparison_comment']) if 'comparison_vs_gemini' in \
                                                                      tasks3_response[
                                                                          0] and 'comparison_comment' in \
                                                                      tasks3_response[0][
                                                                          'comparison_vs_gemini'] and \
                                                                      tasks3_response[0][
                                                                          'comparison_vs_gemini'][
                                                                          'comparison_comment'] else ''
                ab_gpt_preference = str(tasks3_response[0]['comparison_vs_gpt'][
                                            'comparison_score']) if 'comparison_vs_gpt' in \
                                                                    tasks3_response[
                                                                        0] and 'comparison_score' in \
                                                                    tasks3_response[0][
                                                                        'comparison_vs_gpt'] and \
                                                                    tasks3_response[0][
                                                                        'comparison_vs_gpt'][
                                                                        'comparison_score'] else ''
                ab_gpt_comment = str(tasks3_response[0]['comparison_vs_gpt'][
                                         'comparison_comment']) if 'comparison_vs_gpt' in \
                                                                   tasks3_response[
                                                                       0] and 'comparison_comment' in \
                                                                   tasks3_response[0][
                                                                       'comparison_vs_gpt'] and \
                                                                   tasks3_response[0][
                                                                       'comparison_vs_gpt'][
                                                                       'comparison_comment'] else ''
                gpt_rubric_name = str(
                    tasks3_response[0]['rubrics_vs_gpt']['name']) if 'rubrics_vs_gpt' in \
                                                                     tasks3_response[0] and 'name' in \
                                                                     tasks3_response[0][
                                                                         'rubrics_vs_gpt'] and \
                                                                     tasks3_response[0][
                                                                         'rubrics_vs_gpt'][
                                                                         'name'] else ''
                gpt_rubric_description = str(
                    tasks3_response[0]['rubrics_vs_gpt']['description']) if 'rubrics_vs_gpt' in \
                                                                            tasks3_response[
                                                                                0] and 'description' in \
                                                                            tasks3_response[0][
                                                                                'rubrics_vs_gpt'] and \
                                                                            tasks3_response[0][
                                                                                'rubrics_vs_gpt'][
                                                                                'description'] else ''
                gpt_rubric_scale_rating = str(
                    tasks3_response[0]['rubrics_vs_gpt']['rating']) if 'rubrics_vs_gpt' in \
                                                                       tasks3_response[
                                                                           0] and 'rating' in \
                                                                       tasks3_response[0][
                                                                           'rubrics_vs_gpt'] and \
                                                                       tasks3_response[0][
                                                                           'rubrics_vs_gpt'][
                                                                           'rating'] else ''
                gemini_rubric_name = str(
                    tasks3_response[0]['rubrics_vs_gemini']['name']) if 'rubrics_vs_gemini' in \
                                                                        tasks3_response[0] and 'name' in \
                                                                        tasks3_response[0][
                                                                            'rubrics_vs_gemini'] and \
                                                                        tasks3_response[0][
                                                                            'rubrics_vs_gemini'][
                                                                            'name'] else ''
                gemini_rubric_description = str(
                    tasks3_response[0]['rubrics_vs_gemini']['description']) if 'rubrics_vs_gemini' in \
                                                                               tasks3_response[
                                                                                   0] and 'description' in \
                                                                               tasks3_response[0][
                                                                                   'rubrics_vs_gemini'] and \
                                                                               tasks3_response[0][
                                                                                   'rubrics_vs_gemini'][
                                                                                   'description'] else ''
                gemini_rubric_scale_rating = str(
                    tasks3_response[0]['rubrics_vs_gemini']['rating']) if 'rubrics_vs_gemini' in \
                                                                          tasks3_response[
                                                                              0] and 'rating' in \
                                                                          tasks3_response[0][
                                                                              'rubrics_vs_gemini'] and \
                                                                          tasks3_response[0][
                                                                              'rubrics_vs_gemini'][
                                                                              'rating'] else ''

                self.store_truthfulness_a = truthfulness_a
                self.store_instruction_following_a = instruction_following_a
                self.store_writing_quality_a = writing_quality_a
                self.store_verbosity_a = verbosity_a
                self.store_prompt_correctness_a = prompt_correctness_a
                self.store_overall_quality_a = overall_quality_a
                self.store_truthfulness_b = truthfulness_b
                self.store_instruction_following_b = instruction_following_b
                self.store_writing_quality_b = writing_quality_b
                self.store_verbosity_b = verbosity_b
                self.store_prompt_correctness_b = prompt_correctness_b
                self.store_overall_quality_b = overall_quality_b
                self.store_ab_preference = ab_preference
                self.store_ab_comment = ab_comment
                self.store_ab_gpt_preference = ab_gpt_preference
                self.store_ab_gpt_comment = ab_gpt_comment
                self.store_ab_gemini_preference = ab_gemini_preference
                self.store_ab_gemini_comment = ab_gemini_comment
                self.store_gpt_rubric_name = gpt_rubric_name
                self.store_gpt_rubric_description = gpt_rubric_description
                self.store_gpt_rubric_scale_rating = gpt_rubric_scale_rating
                self.store_gemini_rubric_name = gemini_rubric_name
                self.store_gemini_rubric_description = gemini_rubric_description
                self.store_gemini_rubric_scale_rating = gemini_rubric_scale_rating
                self.reason1_truthfulness_a = reason_truthfulness_a
                self.reason1_instruction_following_a = reason_instruction_following_a
                self.reason1_writing_quality_a = reason_writing_quality_a
                self.reason1_verbosity_a = reason_verbosity_a
                self.reason1_prompt_correctness_a = reason_prompt_correctness_a
                human_wa = self._compute_human_weighted_score(1, 'a')
                human_wb = self._compute_human_weighted_score(1, 'b')
                self.reason1_overall_quality_a = f"Weighted: IF×0.25 + Truth×0.25 + Correctness×0.20 + Writing×0.15 + Verbosity×0.15 = {human_wa:.2f}"
                self.reason1_truthfulness_b = reason_truthfulness_b
                self.reason1_instruction_following_b = reason_instruction_following_b
                self.reason1_writing_quality_b = reason_writing_quality_b
                self.reason1_verbosity_b = reason_verbosity_b
                self.reason1_prompt_correctness_b = reason_prompt_correctness_b
                self.reason1_overall_quality_b = f"Weighted: IF×0.25 + Truth×0.25 + Correctness×0.20 + Writing×0.15 + Verbosity×0.15 = {human_wb:.2f}"
        except Exception as e:
            raise ValidationError(f"Error: {e}")
        if self.truthfulness_a and self.store_truthfulness_a:
            self.error_truthfulness_a = check_error(int(self.truthfulness_a), int(self.store_truthfulness_a))
        if self.instruction_following_a and self.store_instruction_following_a:
            self.error_instruction_following_a = check_error(int(self.instruction_following_a),
                                                             int(self.store_instruction_following_a))
        if self.writing_quality_a and self.store_writing_quality_a:
            self.error_writing_quality_a = check_error(int(self.writing_quality_a), int(self.store_writing_quality_a))
        if self.verbosity_a and self.store_verbosity_a:
            self.error_verbosity_a = check_error(int(self.verbosity_a), int(self.store_verbosity_a))
        if self.prompt_correctness_a and self.store_prompt_correctness_a:
            self.error_prompt_correctness_a = check_error(int(self.prompt_correctness_a),
                                                          int(self.store_prompt_correctness_a))
        if self.overall_quality_a and self.store_overall_quality_a:
            self.error_overall_quality_a = check_error(int(self.overall_quality_a), int(self.store_overall_quality_a))
        if self.truthfulness_b and self.store_truthfulness_b:
            self.error_truthfulness_b = check_error(int(self.truthfulness_b), int(self.store_truthfulness_b))
        if self.instruction_following_b and self.store_instruction_following_b:
            self.error_instruction_following_b = check_error(int(self.instruction_following_b),
                                                             int(self.store_instruction_following_b))
        if self.writing_quality_b and self.store_writing_quality_b:
            self.error_writing_quality_b = check_error(int(self.writing_quality_b), int(self.store_writing_quality_b))
        if self.verbosity_b and self.store_verbosity_b:
            self.error_verbosity_b = check_error(int(self.verbosity_b), int(self.store_verbosity_b))
        if self.prompt_correctness_b and self.store_prompt_correctness_b:
            self.error_prompt_correctness_b = check_error(int(self.prompt_correctness_b),
                                                          int(self.store_prompt_correctness_b))
        if self.overall_quality_b and self.store_overall_quality_b:
            self.error_overall_quality_b = check_error(int(self.overall_quality_b), int(self.store_overall_quality_b))
        # --- Gemini QC (commented out; using Kimi QC below) ---
        # qc_inputs = [{
        #     'ab_gpt_comment': self.ab_gpt_comment,
        #     'ab_gemini_comment': self.ab_gemini_comment,
        #     'gpt_rubric_name': self.gpt_rubric_name,
        #     'gpt_rubric_description': self.gpt_rubric_description,
        #     'gpt_rubric_scale_rating': self.gpt_rubric_scale_rating,
        #     'gemini_rubric_name': self.gemini_rubric_name,
        #     'gemini_rubric_description': self.gemini_rubric_description,
        #     'gemini_rubric_scale_rating': self.gemini_rubric_scale_rating,
        #     'response_a': self.client_response_a,
        #     'response_b': self.client_response_b,
        #     'gemini_response': self.gemini_response,
        #     'gpt_response': self.gpt_response,
        #     'ab_comment': self.ab_comment,
        #     'ab_preference': self.ab_preference
        # }]
        # data = llm_actions.perform_qc_checks_sync(gemini_api_key=gemini_api_key, qc_inputs=qc_inputs)
        # --- Kimi QC ---
        qc_inputs_kimi = [{
            'task_id': self.task_id,
            'prompt': self.client_prompt,
            'response_a': self.client_response_a,
            'response_b': self.client_response_b,
            'evaluation_result': tasks3_response[0].get('evaluation_result'),
            'comparison_ab': tasks3_response[0].get('comparison_ab'),
            'ab_preference': self.ab_preference,
            'ab_comment': self.ab_comment or '',
        }]
        data = run_qc_kimi(kimi_api_key=kimi_api_key, qc_inputs=qc_inputs_kimi)
        _logger.info('Kimi QC result (full) --------------------- %s', json.dumps(data, default=str, indent=2))
        print(data)
        if data:
            # --- Kimi QC result parsing (single key ab_comment as list of strings) ---
            self.qc_task_status = 'pass' if (data[0].get('qc_status') or '') == 'QC_Pass' else 'fail'
            ab_comment_list = data[0].get('ab_comment') or []
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
                        reason_comment_parts.append(line)
            self.reason1_ab_preference = reason_pref
            self.reason1_ab_comment = '\n'.join(reason_comment_parts) if reason_comment_parts else ''
            self.error1_ab_preference = bool(reason_pref)
            self.error1_ab_comment = bool(reason_comment_parts)
            # --- Gemini QC parsing (commented out) ---
            # self.qc_task_status = 'pass' if 'qc_status' in data[0] and data[0]['qc_status'] and data[0][
            #     'qc_status'] == 'QC_Pass' else 'fail'
            # self.reason1_ab_preference = \
            # data[0]['checks']['ab_preference_comment_grounding']['preference_matches_comment']['issue'] if 'checks' in \
            #                                                                                                data[
            #                                                                                                                0] and 'ab_preference_comment_grounding' in \
            #                                                                                                data[0][
            #                                                                                                                    'checks'] and 'preference_matches_comment' in \
            #                                                                                                data[0][
            #                                                                                                                    'checks'][
            #                                                                                                                    'ab_preference_comment_grounding'] and 'result' in \
            #                                                                                                data[0][
            #                                                                                                                    'checks'][
            #                                                                                                                    'ab_preference_comment_grounding'][
            #                                                                                                                    'preference_matches_comment'] and not \
            #                                                                                                data[0][
            #                                                                                                                    'checks'][
            #                                                                                                                    'ab_preference_comment_grounding'][
            #                                                                                                                    'preference_matches_comment'][
            #                                                                                                                    'result'] and 'issue' in \
            #                                                                                                data[0][
            #                                                                                                                    'checks'][
            #                                                                                                                    'ab_preference_comment_grounding'][
            #                                                                                                                    'preference_matches_comment'] and \
            #                                                                                                data[0][
            #                                                                                                                    'checks'][
            #                                                                                                                    'ab_preference_comment_grounding'][
            #                                                                                                                    'preference_matches_comment'][
            #                                                                                                                    'issue'] else ''
            # self.reason1_ab_comment = data[0]['checks']['ai_detection']['flagged_fields']['ab_comment'][
            #     0] if 'checks' in data[0] and 'ai_detection' in data[0]['checks'] and 'flagged_fields' in \
            #           data[0]['checks']['ai_detection'] and 'ab_comment' in data[0]['checks']['ai_detection'][
            #               'flagged_fields'] and data[0]['checks']['ai_detection']['flagged_fields'][
            #               'ab_comment'] else ''
            # self.reason1_ab_gpt_comment = data[0]['checks']['ai_detection']['flagged_fields']['human_ab_gpt_comment'][
            #     0] if 'checks' in data[0] and 'ai_detection' in data[0]['checks'] and 'flagged_fields' in \
            #           data[0]['checks']['ai_detection'] and 'human_ab_gpt_comment' in data[0]['checks']['ai_detection'][
            #               'flagged_fields'] and data[0]['checks']['ai_detection']['flagged_fields'][
            #               'human_ab_gpt_comment'] else ''
            # self.reason1_ab_gpt_comment = self.reason1_ab_gpt_comment + '\n' + \
            #                               data[0]['checks']['rubric_comment_grounding'][
            #                                   'comment_grounded_in_responses']['gpt_comment_grounded_in_responses'][
            #                                   'issue'] if 'checks' in data[0] and 'rubric_comment_grounding' in data[0][
            #     'checks'] and 'comment_grounded_in_responses' in data[0]['checks'][
            #                                                   'rubric_comment_grounding'] and 'gpt_comment_grounded_in_responses' in \
            #                                               data[0]['checks']['rubric_comment_grounding'][
            #                                                   'comment_grounded_in_responses'] and 'result' in \
            #                                               data[0]['checks']['rubric_comment_grounding'][
            #                                                   'comment_grounded_in_responses'][
            #                                                   'gpt_comment_grounded_in_responses'] and not \
            #                                               data[0]['checks']['rubric_comment_grounding'][
            #                                                   'comment_grounded_in_responses'][
            #                                                   'gpt_comment_grounded_in_responses'][
            #                                                   'result'] and 'issue' in \
            #                                               data[0]['checks']['rubric_comment_grounding'][
            #                                                   'comment_grounded_in_responses'][
            #                                                   'gpt_comment_grounded_in_responses'] and \
            #                                               data[0]['checks']['rubric_comment_grounding'][
            #                                                   'comment_grounded_in_responses'][
            #                                                   'gpt_comment_grounded_in_responses'][
            #                                                   'issue'] else self.reason1_ab_gpt_comment
            # self.reason1_ab_gemini_comment = \
            # data[0]['checks']['ai_detection']['flagged_fields']['human_ab_gemini_comment'][0] if 'checks' in data[
            #     0] and 'ai_detection' in data[0]['checks'] and 'flagged_fields' in data[0]['checks'][
            #                                                                                          'ai_detection'] and 'human_ab_gemini_comment' in \
            #                                                                                      data[0]['checks'][
            #                                                                                          'ai_detection'][
            #                                                                                          'flagged_fields'] and \
            #                                                                                      data[0]['checks'][
            #                                                                                          'ai_detection'][
            #                                                                                          'flagged_fields'][
            #                                                                                          'human_ab_gemini_comment'] else ''
            # self.reason1_ab_gemini_comment = self.reason1_ab_gemini_comment + '\n' + \
            #                                  data[0]['checks']['rubric_comment_grounding'][
            #                                      'comment_grounded_in_responses'][
            #                                      'gemini_comment_grounded_in_responses']['issue'] if 'checks' in data[
            #     0] and 'rubric_comment_grounding' in data[0]['checks'] and 'comment_grounded_in_responses' in \
            #                                                                                          data[0]['checks'][
            #                                                                                              'rubric_comment_grounding'] and 'gemini_comment_grounded_in_responses' in \
            #                                                                                          data[0]['checks'][
            #                                                                                              'rubric_comment_grounding'][
            #                                                                                              'comment_grounded_in_responses'] and 'result' in \
            #                                                                                          data[0]['checks'][
            #                                                                                              'rubric_comment_grounding'][
            #                                                                                              'comment_grounded_in_responses'][
            #                                                                                              'gemini_comment_grounded_in_responses'] and not \
            #                                                                                          data[0]['checks'][
            #                                                                                              'rubric_comment_grounding'][
            #                                                                                              'comment_grounded_in_responses'][
            #                                                                                              'gemini_comment_grounded_in_responses'][
            #                                                                                              'result'] and 'issue' in \
            #                                                                                          data[0]['checks'][
            #                                                                                              'rubric_comment_grounding'][
            #                                                                                              'comment_grounded_in_responses'][
            #                                                                                              'gemini_comment_grounded_in_responses'] and \
            #                                                                                          data[0]['checks'][
            #                                                                                              'rubric_comment_grounding'][
            #                                                                                              'comment_grounded_in_responses'][
            #                                                                                              'gemini_comment_grounded_in_responses'][
            #                                                                                              'issue'] else self.reason1_ab_gemini_comment
            # self.reason1_gpt_rubric_name = data[0]['checks']['ai_detection']['flagged_fields']['human_gpt_rubric_name'][
            #     0] if 'checks' in data[0] and 'ai_detection' in data[0]['checks'] and 'flagged_fields' in \
            #           data[0]['checks']['ai_detection'] and 'human_gpt_rubric_name' in \
            #           data[0]['checks']['ai_detection']['flagged_fields'] and \
            #           data[0]['checks']['ai_detection']['flagged_fields']['human_gpt_rubric_name'] else ''
            # self.reason1_gpt_rubric_description = \
            # data[0]['checks']['ai_detection']['flagged_fields']['human_gpt_rubric_description'][0] if 'checks' in data[
            #     0] and 'ai_detection' in data[0]['checks'] and 'flagged_fields' in data[0]['checks'][
            #                                                                                           'ai_detection'] and 'human_gpt_rubric_description' in \
            #                                                                                       data[0]['checks'][
            #                                                                                           'ai_detection'][
            #                                                                                           'flagged_fields'] and \
            #                                                                                       data[0]['checks'][
            #                                                                                           'ai_detection'][
            #                                                                                           'flagged_fields'][
            #                                                                                           'human_gpt_rubric_description'] else ''
            # self.reason1_gemini_rubric_name = \
            # data[0]['checks']['ai_detection']['flagged_fields']['human_gemini_rubric_name'][0] if 'checks' in data[
            #     0] and 'ai_detection' in data[0]['checks'] and 'flagged_fields' in data[0]['checks'][
            #                                                                                           'ai_detection'] and 'human_gemini_rubric_name' in \
            #                                                                                       data[0]['checks'][
            #                                                                                           'ai_detection'][
            #                                                                                           'flagged_fields'] and \
            #                                                                                       data[0]['checks'][
            #                                                                                           'ai_detection'][
            #                                                                                           'flagged_fields'][
            #                                                                                           'human_gemini_rubric_name'] else ''
            # self.reason1_gemini_rubric_description = \
            # data[0]['checks']['ai_detection']['flagged_fields']['human_gemini_rubric_description'][0] if 'checks' in \
            #                                                                                                data[
            #                                                                                                    0] and 'ai_detection' in \
            #                                                                                                data[0][
            #                                                                                                    'checks'] and 'flagged_fields' in \
            #                                                                                                data[0][
            #                                                                                                    'checks'][
            #                                                                                                    'ai_detection'] and 'human_gemini_rubric_description' in \
            #                                                                                                data[0][
            #                                                                                                    'checks'][
            #                                                                                                    'ai_detection'][
            #                                                                                                    'flagged_fields'] and \
            #                                                                                                data[0][
            #                                                                                                    'checks'][
            #                                                                                                    'ai_detection'][
            #                                                                                                    'flagged_fields'][
            #                                                                                                    'human_gemini_rubric_description'] else ''

            # if 'checks' in data[0]:
            #     if 'ab_preference_comment_grounding' in data[0]['checks']:
            #         if 'ab_comment_grounded_in_responses' in data[0]['checks']['ab_preference_comment_grounding']:
            #             if 'result' in data[0]['checks']['ab_preference_comment_grounding']['ab_comment_grounded_in_responses']:
            # self.reason1_ab_comment = self.reason1_ab_comment + '\n' + \
            #                           data[0]['checks']['ab_preference_comment_grounding'][
            #                               'ab_comment_grounded_in_responses']['issue'] if 'checks' in data[
            #     0] and 'ab_preference_comment_grounding' in \
            #                                                                                         data[0][
            #                                                                                             'checks'] and 'ab_comment_grounded_in_responses' in \
            #                                                                                         data[0][
            #                                                                                             'checks'][
            #                                                                                             'ab_preference_comment_grounding'] and 'result' in \
            #                                                                                         data[0][
            #                                                                                             'checks'][
            #                                                                                             'ab_preference_comment_grounding'][
            #                                                                                             'ab_comment_grounded_in_responses'] and not \
            #                                                                                             data[0][
            #                                                                                                 'checks'][
            #                                                                                                 'ab_preference_comment_grounding'][
            #                                                                                                 'ab_comment_grounded_in_responses'][
            #                                                                                                 'result'] and 'issue' in \
            #                                                                                         data[0][
            #                                                                                             'checks'][
            #                                                                                             'ab_preference_comment_grounding'][
            #                                                                                             'ab_comment_grounded_in_responses'] and \
            #                                                                                         data[0]['checks'][
            #                                                                                             'ab_preference_comment_grounding'][
            #                                                                                             'ab_comment_grounded_in_responses'][
            #                                                                                             'issue'] else self.reason1_ab_comment

            # self.reason1_gpt_rubric_name = self.reason1_gpt_rubric_name + '\n' + \
            #                                data[0]['checks']['rubric_comment_grounding']['gpt_grounding'][
            #                                    'name_grounded']['issue'] if 'checks' in data[
            #     0] and 'rubric_comment_grounding' in data[0]['checks'] and 'gpt_grounding' in data[0]['checks'][
            #                                                                                 'rubric_comment_grounding'] and 'name_grounded' in \
            #                                                                             data[0]['checks'][
            #                                                                                 'rubric_comment_grounding'][
            #                                                                                 'gpt_grounding'] and 'result' in \
            #                                                                             data[0]['checks'][
            #                                                                                 'rubric_comment_grounding'][
            #                                                                                 'gpt_grounding'][
            #                                                                                 'name_grounded'] and not \
            #                                                                             data[0]['checks'][
            #                                                                                 'rubric_comment_grounding'][
            #                                                                                 'gpt_grounding']['name_grounded'][
            #                                                                                 'result'] and 'issue' in \
            #                                                                             data[0]['checks'][
            #                                                                                 'rubric_comment_grounding'][
            #                                                                                 'gpt_grounding']['name_grounded'] and \
            #                                                                             data[0]['checks'][
            #                                                                                 'rubric_comment_grounding'][
            #                                                                                 'gpt_grounding']['name_grounded'][
            #                                                                                 'issue'] else self.reason1_gpt_rubric_name
            # self.reason1_gpt_rubric_description = self.reason1_gpt_rubric_description + '\n' + \
            #                                       data[0]['checks']['rubric_comment_grounding']['gpt_grounding'][
            #                                           'description_grounded']['issue'] if 'checks' in data[
            #     0] and 'rubric_comment_grounding' in data[0]['checks'] and 'gpt_grounding' in data[0]['checks'][
            #                                                                                             'rubric_comment_grounding'] and 'description_grounded' in \
            #                                                                                         data[0]['checks'][
            #                                                                                             'rubric_comment_grounding'][
            #                                                                                             'gpt_grounding'] and 'result' in \
            #                                                                                         data[0]['checks'][
            #                                                                                             'rubric_comment_grounding'][
            #                                                                                             'gpt_grounding'][
            #                                                                                             'description_grounded'] and not \
            #                                                                                         data[0]['checks'][
            #                                                                                             'rubric_comment_grounding'][
            #                                                                                             'gpt_grounding'][
            #                                                                                             'description_grounded'][
            #                                                                                             'result'] and 'issue' in \
            #                                                                                         data[0]['checks'][
            #                                                                                             'rubric_comment_grounding'][
            #                                                                                             'gpt_grounding'][
            #                                                                                             'description_grounded'] and \
            #                                                                                         data[0]['checks'][
            #                                                                                             'rubric_comment_grounding'][
            #                                                                                             'gpt_grounding'][
            #                                                                                             'description_grounded'][
            #                                                                                             'issue'] else self.reason1_gpt_rubric_description
            # self.reason1_ab_gpt_preference = \
#             data[0]['checks']['rubric_comment_grounding']['gpt_grounding']['rating_consistent']['issue'] if 'checks' in \
#                                                                                                             data[
#                                                                                                                 0] and 'rubric_comment_grounding' in \
#                                                                                                             data[0][
#                                                                                                                 'checks'] and 'gpt_grounding' in \
#                                                                                                             data[0][
#                                                                                                                 'checks'][
#                                                                                                                 'rubric_comment_grounding'] and 'rating_consistent' in \
#                                                                                                             data[0][
#                                                                                                                 'checks'][
#                                                                                                                 'rubric_comment_grounding'][
#                                                                                                                 'gpt_grounding'] and 'result' in \
#                                                                                                             data[0][
#                                                                                                                 'checks'][
#                                                                                                                 'rubric_comment_grounding'][
#                                                                                                                 'gpt_grounding'][
#                                                                                                                 'rating_consistent'] and not \
#                                                                                                             data[0][
#                                                                                                                 'checks'][
#                                                                                                                 'rubric_comment_grounding'][
#                                                                                                                 'gpt_grounding'][
#                                                                                                                 'rating_consistent'][
#                                                                                                                 'result'] and 'issue' in \
#                                                                                                             data[0][
#                                                                                                                 'checks'][
#                                                                                                                 'rubric_comment_grounding'][
#                                                                                                                 'gpt_grounding'][
#                                                                                                                 'rating_consistent'] and \
#                                                                                                             data[0][
#                                                                                                                 'checks'][
#                                                                                                                 'rubric_comment_grounding'][
#                                                                                                                 'gpt_grounding'][
#                                                                                                                 'rating_consistent'][
#                                                                                                                 'issue'] else ''
#             self.reason1_gemini_rubric_name = self.reason1_gemini_rubric_name + '\n' + \
#                                               data[0]['checks']['rubric_comment_grounding']['gemini_grounding'][
#                                                   'name_grounded']['issue'] if 'checks' in data[
#                 0] and 'rubric_comment_grounding' in data[0]['checks'] and 'gemini_grounding' in data[0]['checks'][
#                                                                                    'rubric_comment_grounding'] and 'name_grounded' in \
#                                                                                data[0]['checks'][
#                                                                                    'rubric_comment_grounding'][
#                                                                                    'gemini_grounding'] and 'result' in \
#                                                                                data[0]['checks'][
#                                                                                    'rubric_comment_grounding'][
#                                                                                    'gemini_grounding'][
#                                                                                    'name_grounded'] and not \
#                                                                                data[0]['checks'][
#                                                                                    'rubric_comment_grounding'][
#                                                                                    'gemini_grounding']['name_grounded'][
#                                                                                    'result'] and 'issue' in \
#                                                                                data[0]['checks'][
#                                                                                    'rubric_comment_grounding'][
#                                                                                    'gemini_grounding'][
#                                                                                    'name_grounded'] and \
#                                                                                data[0]['checks'][
#                                                                                    'rubric_comment_grounding'][
#                                                                                    'gemini_grounding']['name_grounded'][
#                                                                                    'issue'] else self.reason1_gemini_rubric_name
#             self.reason1_gemini_rubric_description = self.reason1_gemini_rubric_description + '\n' + \
#                                                      data[0]['checks']['rubric_comment_grounding']['gemini_grounding'][
#                                                          'description_grounded']['issue'] if 'checks' in data[
#                 0] and 'rubric_comment_grounding' in data[0]['checks'] and 'gemini_grounding' in data[0]['checks'][
#                                                                                                  'rubric_comment_grounding'] and 'description_grounded' in \
#                                                                                              data[0]['checks'][
#                                                                                                  'rubric_comment_grounding'][
#                                                                                                  'gemini_grounding'] and 'result' in \
#                                                                                              data[0]['checks'][
#                                                                                                  'rubric_comment_grounding'][
#                                                                                                  'gemini_grounding'][
#                                                                                                  'description_grounded'] and not \
#                                                                                              data[0]['checks'][
#                                                                                                  'rubric_comment_grounding'][
#                                                                                                  'gemini_grounding'][
#                                                                                                  'description_grounded'][
#                                                                                                  'result'] and 'issue' in \
#                                                                                              data[0]['checks'][
#                                                                                                  'rubric_comment_grounding'][
#                                                                                                  'gemini_grounding'][
#                                                                                                  'description_grounded'] and \
#                                                                                              data[0]['checks'][
#                                                                                                  'rubric_comment_grounding'][
#                                                                                                  'gemini_grounding'][
#                                                                                                  'description_grounded'][
#                                                                                                  'issue'] else self.reason1_gemini_rubric_description
#             self.reason1_ab_gemini_preference = \
#             data[0]['checks']['rubric_comment_grounding']['gemini_grounding']['rating_consistent'][
#                 'issue'] if 'checks' in data[0] and 'rubric_comment_grounding' in data[0][
#                 'checks'] and 'gemini_grounding' in data[0]['checks'][
#                                 'rubric_comment_grounding'] and 'rating_consistent' in \
#                             data[0]['checks']['rubric_comment_grounding']['gemini_grounding'] and 'result' in \
#                             data[0]['checks']['rubric_comment_grounding']['gemini_grounding'][
#                                 'rating_consistent'] and not \
#                             data[0]['checks']['rubric_comment_grounding']['gemini_grounding']['rating_consistent'][
#                                 'result'] and 'issue' in \
#                             data[0]['checks']['rubric_comment_grounding']['gemini_grounding']['rating_consistent'] and \
#                             data[0]['checks']['rubric_comment_grounding']['gemini_grounding']['rating_consistent'][
#                                 'issue'] else ''
#             self.reason1_gpt_rubric_scale_rating = \
#             data[0]['checks']['rubric_rating_justification']['gpt_rating_justified']['issue'] if 'checks' in data[
#                 0] and 'rubric_rating_justification' in data[0]['checks'] and 'gpt_rating_justified' in \
#                                                                                                  data[0]['checks'][
#                                                                                                      'rubric_rating_justification'] and 'result' in \
#                                                                                                  data[0]['checks'][
#                                                                                                      'rubric_rating_justification'][
#                                                                                                      'gpt_rating_justified'] and not \
#                                                                                                  data[0]['checks'][
#                                                                                                      'rubric_rating_justification'][
#                                                                                                      'gpt_rating_justified'][
#                                                                                                      'result'] and 'issue' in \
#                                                                                                  data[0]['checks'][
#                                                                                                      'rubric_rating_justification'][
#                                                                                                      'gpt_rating_justified'] and \
#                                                                                                  data[0]['checks'][
#                                                                                                      'rubric_rating_justification'][
#                                                                                                      'gpt_rating_justified'][
#                                                                                                      'issue'] else ''
#             self.reason1_gemini_rubric_scale_rating = \
#             data[0]['checks']['rubric_rating_justification']['gemini_rating_justified']['issue'] if 'checks' in data[
#                 0] and 'rubric_rating_justification' in data[0]['checks'] and 'gemini_rating_justified' in \
#                                                                                                     data[0]['checks'][
#                                                                                                         'rubric_rating_justification'] and 'result' in \
#                                                                                                     data[0]['checks'][
#                                                                                                         'rubric_rating_justification'][
#                                                                                                         'gemini_rating_justified'] and not \
#                                                                                                     data[0]['checks'][
#                                                                                                         'rubric_rating_justification'][
#                                                                                                         'gemini_rating_justified'][
#                                                                                                         'result'] and 'issue' in \
#                                                                                                     data[0]['checks'][
#                                                                                                         'rubric_rating_justification'][
#                                                                                                         'gemini_rating_justified'] and \
#                                                                                                     data[0]['checks'][
#                                                                                                         'rubric_rating_justification'][
#                                                                                                         'gemini_rating_justified'][
#                                                                                                         'issue'] else ''
#             self.reason1_ab_gpt_preference = self.reason1_ab_gpt_preference + '\n' + \
#                                              data[0]['checks']['external_preference_comment_grounding'][
#                                                  'gpt_preference_matches_comment']['issue'] if 'checks' in data[
#                 0] and 'external_preference_comment_grounding' in data[0][
#                                                                                                    'checks'] and 'gpt_preference_matches_comment' in \
#                                                                                                data[0]['checks'][
#                                                                                                    'external_preference_comment_grounding'] and 'result' in \
#                                                                                                data[0]['checks'][
#                                                                                                    'external_preference_comment_grounding'][
#                                                                                                    'gpt_preference_matches_comment'] and not \
#                                                                                                data[0]['checks'][
#                                                                                                    'external_preference_comment_grounding'][
#                                                                                                    'gpt_preference_matches_comment'][
#                                                                                                    'result'] and 'issue' in \
#                                                                                                data[0]['checks'][
#                                                                                                    'external_preference_comment_grounding'][
#                                                                                                    'gpt_preference_matches_comment'] and \
#                                                                                                data[0]['checks'][
#                                                                                                    'external_preference_comment_grounding'][
#                                                                                                    'gpt_preference_matches_comment'][
#                                                                                                    'issue'] else self.reason1_ab_gpt_preference
#             self.reason1_ab_gemini_preference = self.reason1_ab_gemini_preference + '\n' + \
#                                                 data[0]['checks']['external_preference_comment_grounding'][
#                                                     'gemini_preference_matches_comment']['issue'] if 'checks' in data[
#                 0] and 'external_preference_comment_grounding' in data[0][
#                                                                                                          'checks'] and 'gemini_preference_matches_comment' in \
#                                                                                                      data[0]['checks'][
#                                                                                                          'external_preference_comment_grounding'] and 'result' in \
#                                                                                                      data[0]['checks'][
#                                                                                                          'external_preference_comment_grounding'][
#                                                                                                          'gemini_preference_matches_comment'] and not \
#                                                                                                      data[0]['checks'][
#                                                                                                          'external_preference_comment_grounding'][
#                                                                                                          'gemini_preference_matches_comment'][
#                                                                                                          'result'] and 'issue' in \
#                                                                                                      data[0]['checks'][
#                                                                                                          'external_preference_comment_grounding'][
#                                                                                                          'gemini_preference_matches_comment'] and \
#                                                                                                      data[0]['checks'][
#                                                                                                          'external_preference_comment_grounding'][
#                                                                                                          'gemini_preference_matches_comment'][
#                                                                                                          'issue'] else self.reason1_ab_gemini_preference

#             self.error_ab_preference = True if self.reason1_ab_preference else False
#             self.reason1_ab_comment = True if self.reason1_ab_comment else False
#             self.error_gpt_rubric_name = True if self.reason1_gpt_rubric_name else False
#             self.error_gpt_rubric_description = True if self.reason1_gpt_rubric_description else False
#             self.error_gpt_rubric_scale_rating = True if self.reason1_gpt_rubric_scale_rating else False
#             self.error_ab_gpt_preference = True if self.reason1_ab_gpt_preference else False
#             self.error_ab_gpt_comment = True if self.reason1_ab_gpt_comment else False
#             self.error_gemini_rubric_name = True if self.reason1_gemini_rubric_name else False
#             self.error_gemini_rubric_description = True if self.reason1_gemini_rubric_description else False
#             self.error_gemini_rubric_scale_rating = True if self.reason1_gemini_rubric_scale_rating else False
#             self.error_ab_gemini_preference = True if self.reason1_ab_gemini_preference else False
#             self.error_ab_gemini_comment = True if self.reason1_ab_gemini_comment else False

#             gpt_input_tokens = data[0]['token_usage']['openai']['input_tokens'] if 'token_usage' in data[
#                 0] and 'openai' in data[0]['token_usage'] and 'input_tokens' in data[0]['token_usage']['openai'] and \
#                                                                                    data[0]['token_usage']['openai'][
#                                                                                        'input_tokens'] else 0
#             gemini_input_tokens = data[0]['token_usage']['gemini']['input_tokens'] if 'token_usage' in data[
#                 0] and 'gemini' in data[0]['token_usage'] and 'input_tokens' in data[0]['token_usage']['gemini'] and \
#                                                                                       data[0]['token_usage']['gemini'][
#                                                                                           'input_tokens'] else 0
#             gpt_output_tokens = data[0]['token_usage']['openai']['output_tokens'] if 'token_usage' in data[
#                 0] and 'openai' in data[0]['token_usage'] and 'output_tokens' in data[0]['token_usage']['openai'] and \
#                                                                                      data[0]['token_usage']['openai'][
#                                                                                          'output_tokens'] else 0
#             gemini_output_tokens = data[0]['token_usage']['gemini']['output_tokens'] if 'token_usage' in data[
#                 0] and 'gemini' in data[0]['token_usage'] and 'output_tokens' in data[0]['token_usage']['gemini'] and \
#                                                                                         data[0]['token_usage'][
#                                                                                             'gemini'][
#                                                                                             'output_tokens'] else 0
#             gpt_cache_tokens = data[0]['token_usage']['openai']['cached_tokens'] if 'token_usage' in data[
#                 0] and 'openai' in data[0]['token_usage'] and 'cached_tokens' in data[0]['token_usage']['openai'] and \
#                                                                                     data[0]['token_usage']['openai'][
#                                                                                         'cached_tokens'] else 0
#             gemini_cache_tokens = data[0]['token_usage']['gemini']['cached_tokens'] if 'token_usage' in data[
#                 0] and 'gemini' in data[0]['token_usage'] and 'cached_tokens' in data[0]['token_usage']['gemini'] and \
#                                                                                        data[0]['token_usage']['gemini'][
#                                                                                            'cached_tokens'] else 0
#             gpt_cost = data[0]['token_usage']['openai']['cost_usd'] if 'token_usage' in data[0] and 'openai' in data[0][
#                 'token_usage'] and 'cost_usd' in data[0]['token_usage']['openai'] and data[0]['token_usage']['openai'][
#                                                                            'cost_usd'] else 0.0
#             gemini_cost = data[0]['token_usage']['gemini']['cost_usd'] if 'token_usage' in data[0] and 'gemini' in \
#                                                                           data[0]['token_usage'] and 'cost_usd' in \
#                                                                           data[0]['token_usage']['gemini'] and \
#                                                                           data[0]['token_usage']['gemini'][
#                                                                               'cost_usd'] else 0.0

#             if gpt_input_tokens > 0 or gpt_output_tokens > 0 or gpt_cache_tokens > 0 or gpt_cost > 0.0:
#                 token_vals = {
#                     'preference_record_ids': [(6, 0, [self.id])],
#                     'type': 'QC',
#                     'ai_model_type': 'openai',
#                     'token_line': [(0, 0, {
#                         'input_token': gpt_input_tokens,
#                         'output_token': gpt_output_tokens,
#                         'cache_token': gpt_cache_tokens,
#                         'cost': gpt_cost
#                     })]
#                 }
#                 self.env['preference.ranking.token'].sudo().create(token_vals)
#             if gemini_input_tokens > 0 or gemini_output_tokens > 0 or gemini_cache_tokens > 0 or gemini_cost > 0.0:
#                 token_vals = {
#                     'preference_record_ids': [(6, 0, [self.id])],
#                     'type': 'QC',
#                     'ai_model_type': 'gemini',
#                     'token_line': [(0, 0, {
#                         'input_token': gemini_input_tokens,
#                         'output_token': gemini_output_tokens,
#                         'cache_token': gemini_cache_tokens,
#                         'cost': gemini_cost
#                     })]
#                }
#                self.env['preference.ranking.token'].sudo().create(token_vals)
        self.is_eval_done = True

    def submit_task(self):
        if not self.is_eval_done:
            raise ValidationError(f"Evaluation Not Done!")
        self.task_status = 'Submitted'

    def _generate_response_background(self, turn, model_1, model_2, prompt, genai_api_key, dialog_id, dialog_history, current_turn_handle_id, current_turn_mime, history_handle_ids,
                                      temperature_1=None, top_p_1=None, repetition_penalty_1=None,
                                      temperature_2=None, top_p_2=None, repetition_penalty_2=None):
        """Run LLM response generation synchronously in the caller's transaction."""
        record_id = self.id
        _logger.info('Response generation (turn %s): starting | record=%s', turn, record_id)
        try:
            response_a_b = self._generate_response_a_and_b_with_models(
                model_1, model_2,
                prompt=prompt,
                genai_api_key=genai_api_key,
                dialog_id=dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_turn_handle_id,
                current_turn_mime=current_turn_mime,
                history_handle_ids=history_handle_ids,
                temperature_1=temperature_1, top_p_1=top_p_1, repetition_penalty_1=repetition_penalty_1,
                temperature_2=temperature_2, top_p_2=top_p_2, repetition_penalty_2=repetition_penalty_2,
            )
            if response_a_b:
                errs = response_a_b.get('errors') or {}
                if errs:
                    _logger.warning('Response generation (turn %s) errors: %s', turn, errs)
                response_a = response_a_b.get('response_a', '')
                response_b = response_a_b.get('response_b', '')
                swap = random.random() < 0.5
                if swap:
                    response_a, response_b = response_b, response_a
                self.write({
                    f'client_response_a{turn}': response_a,
                    f'client_response_b{turn}': response_b,
                    f'is_randomized_{turn}': swap,
                })
                _logger.info('Response generation (turn %s): written to DB | record=%s', turn, record_id)
        except Exception as e:
            _logger.exception('Response generation (turn %s) failed: %s', turn, e)

    def _run_auto_evaluation_for_turn(self, turn):
        """Run Kimi evaluation right after response generation to pre-fill
        all dimension ratings, reasons, AB preference, and AB comment for
        the given legacy flat turn (1-7).
        The AI scores are written to BOTH the user-facing rating fields
        and the store_* fields (for later error comparison)."""
        _logger.info('Auto-evaluation (flat turn %s): starting | record=%s', turn, self.id)

        response_a = getattr(self, f'client_response_a{turn}', '') or ''
        response_b = getattr(self, f'client_response_b{turn}', '') or ''
        if not response_a or not response_b:
            _logger.warning('Auto-evaluation (flat turn %s): responses missing, skipping', turn)
            return

        # Build evaluation_inputs for all turns up to current
        evaluation_inputs = []
        for t in range(1, turn + 1):
            prompt = getattr(self, f'client_prompt{t}', '') or ''
            ra = getattr(self, f'client_response_a{t}', '') or ''
            rb = getattr(self, f'client_response_b{t}', '') or ''
            if prompt and ra and rb:
                evaluation_inputs.append({
                    'task_id': self.task_id,
                    'prompt': prompt,
                    'response_a': ra,
                    'response_b': rb,
                })

        if not evaluation_inputs:
            _logger.warning('Auto-evaluation (flat turn %s): no evaluation_inputs, skipping', turn)
            return

        try:
            eval_result = run_evaluation_kimi(
                kimi_api_key=kimi_api_key,
                evaluation_inputs=evaluation_inputs,
            )
        except Exception as e:
            _logger.exception('Auto-evaluation Kimi call (flat turn %s) failed: %s', turn, e)
            return

        if not eval_result:
            _logger.warning('Auto-evaluation (flat turn %s): no eval_result returned', turn)
            return

        data = get_eval_data(eval_result, idx=turn - 1)
        if not data:
            _logger.warning('Auto-evaluation (flat turn %s): get_eval_data returned nothing', turn)
            return

        # Build write_vals: pre-fill BOTH human-facing fields and store_* fields
        write_vals = {}

        # Dimension scores (human-facing + store)
        dims_map = {
            'truthfulness_a': 'truthfulness_a',
            'truthfulness_b': 'truthfulness_b',
            'instruction_following_a': 'instruction_following_a',
            'instruction_following_b': 'instruction_following_b',
            'writing_quality_a': 'writing_quality_a',
            'writing_quality_b': 'writing_quality_b',
            'verbosity_a': 'verbosity_a',
            'verbosity_b': 'verbosity_b',
            'prompt_correctness_a': 'prompt_correctness_a',
            'prompt_correctness_b': 'prompt_correctness_b',
            'overall_quality_a': 'overall_quality_a',
            'overall_quality_b': 'overall_quality_b',
        }
        for data_key in dims_map:
            val = data.get(data_key)
            if val:
                # Human-facing field: e.g. truthfulness_a1
                write_vals[f'{data_key}{turn}'] = val
                # Store field: e.g. store_truthfulness_a1
                write_vals[f'store_{data_key}{turn}'] = val

        # Reasons: e.g. reason1_truthfulness_a
        reason_dims = [
            'truthfulness_a', 'truthfulness_b',
            'instruction_following_a', 'instruction_following_b',
            'writing_quality_a', 'writing_quality_b',
            'verbosity_a', 'verbosity_b',
            'prompt_correctness_a', 'prompt_correctness_b',
            'overall_quality_a', 'overall_quality_b',
        ]
        for dim in reason_dims:
            val = data.get(f'reason_{dim}', '')
            if val:
                write_vals[f'reason{turn}_{dim}'] = val

        # AB preference and comment
        ab_pref = data.get('ab_preference', '')
        ab_comment_val = data.get('ab_comment', '')
        if ab_pref:
            write_vals[f'ab_preference{turn}'] = ab_pref
            write_vals[f'store_ab_preference{turn}'] = ab_pref
        if ab_comment_val:
            write_vals[f'ab_comment{turn}'] = ab_comment_val
            write_vals[f'store_ab_comment{turn}'] = ab_comment_val

        if write_vals:
            write_vals['is_eval_done'] = True
            self.write(write_vals)
            _logger.info('Auto-evaluation (flat turn %s): pre-filled %d fields | record=%s',
                         turn, len(write_vals), self.id)

    def action_submit_prompt1(self):
        _logger.info("action_submit_prompt1 called | record=%s | prompt=%s", self.id, (self.client_prompt1 or "")[:200])
        if not self.client_prompt1 and not self.image_1:
            raise ValidationError("Prompt or image required")
        if not self.dialog_id:
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
            self.dialog_id = dialog_id
            self.model_1 = (router_config.get("model_1") or "").strip() or ""
            self.model_2 = (router_config.get("model_2") or "").strip() or ""
            self.temperature_1 = router_config.get("temperature_1") or 0
            self.top_p_1 = router_config.get("top_p_1") or 0
            self.repetition_penalty_1 = router_config.get("repetition_penalty_1") or 0
            self.temperature_2 = router_config.get("temperature_2") or 0
            self.top_p_2 = router_config.get("top_p_2") or 0
            self.repetition_penalty_2 = router_config.get("repetition_penalty_2") or 0
        self._ensure_image_handle_for_turn(1, genai_api_key)
        current_handle = (self.image_handle_id_1 or "").strip() or None
        current_mime = (self.image_mime_1 or "").strip() or None
        self._generate_response_background(
            turn=1, model_1=self.model_1, model_2=self.model_2,
            prompt=(self.client_prompt1 or "").strip() or "",
            genai_api_key=genai_api_key, dialog_id=self.dialog_id,
            dialog_history=None,
            current_turn_handle_id=current_handle,
            current_turn_mime=current_mime,
            history_handle_ids=None,
            temperature_1=self.temperature_1, top_p_1=self.top_p_1, repetition_penalty_1=self.repetition_penalty_1,
            temperature_2=self.temperature_2, top_p_2=self.top_p_2, repetition_penalty_2=self.repetition_penalty_2,
        )
        # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
        try:
            self._run_auto_evaluation_for_turn(1)
        except Exception as e:
            _logger.exception('Auto-evaluation (flat turn 1) failed (non-blocking): %s', e)

    def action_submit_prompt2(self):
        _logger.info("action_submit_prompt2 called | record=%s | prompt=%s", self.id, (self.client_prompt2 or "")[:200])
        if not self.client_prompt1 or not self.client_prompt2:
            raise ValidationError("All 2 Prompts Required")
        if not self.dialog_id:
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
            self.dialog_id = dialog_id
            self.model_1 = (router_config.get("model_1") or "").strip() or ""
            self.model_2 = (router_config.get("model_2") or "").strip() or ""
            self.temperature_1 = router_config.get("temperature_1") or 0
            self.top_p_1 = router_config.get("top_p_1") or 0
            self.repetition_penalty_1 = router_config.get("repetition_penalty_1") or 0
            self.temperature_2 = router_config.get("temperature_2") or 0
            self.top_p_2 = router_config.get("top_p_2") or 0
            self.repetition_penalty_2 = router_config.get("repetition_penalty_2") or 0
        if not (self.dialog_id or "").strip():
            raise ValidationError(
                "Dialog session is missing. Please submit Turn 1 first to start a session, then try Turn 2 again."
            )
        ab_preference1 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        dialog_history = [(self.client_prompt1, ab_preference1)]
        check_prompt = check_follow_up_relevance_kimi(kimi_api_key=kimi_api_key,dialog_history=dialog_history,follow_up_prompt=self.client_prompt2)
        if 'is_relevant' in check_prompt and check_prompt['is_relevant']:
            self._ensure_image_handle_for_turn(2, genai_api_key)
            history_handle_ids = [
                ((self.image_handle_id_1 or "").strip() or None, (self.image_mime_1 or "").strip() or None),
            ]
            current_handle = (self.image_handle_id_2 or "").strip() or None
            current_mime = (self.image_mime_2 or "").strip() or None
            self._generate_response_background(
                turn=2, model_1=self.model_1, model_2=self.model_2,
                prompt=(self.client_prompt2 or "").strip() or "",
                genai_api_key=genai_api_key, dialog_id=self.dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_handle,
                current_turn_mime=current_mime,
                history_handle_ids=history_handle_ids,
                temperature_1=self.temperature_1, top_p_1=self.top_p_1, repetition_penalty_1=self.repetition_penalty_1,
                temperature_2=self.temperature_2, top_p_2=self.top_p_2, repetition_penalty_2=self.repetition_penalty_2,
            )
            # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
            try:
                self._run_auto_evaluation_for_turn(2)
            except Exception as e:
                _logger.exception('Auto-evaluation (flat turn 2) failed (non-blocking): %s', e)
        else:
            raise ValidationError("Please rewrite the prompt")

    def action_submit_prompt3(self):
        _logger.info("action_submit_prompt3 called | record=%s | prompt=%s", self.id, (self.client_prompt3 or "")[:200])
        if not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3:
            raise ValidationError("All 3 Prompts Required")
        if not self.dialog_id:
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
            self.dialog_id = dialog_id
            self.model_1 = (router_config.get("model_1") or "").strip() or ""
            self.model_2 = (router_config.get("model_2") or "").strip() or ""
            self.temperature_1 = router_config.get("temperature_1") or 0
            self.top_p_1 = router_config.get("top_p_1") or 0
            self.repetition_penalty_1 = router_config.get("repetition_penalty_1") or 0
            self.temperature_2 = router_config.get("temperature_2") or 0
            self.top_p_2 = router_config.get("top_p_2") or 0
            self.repetition_penalty_2 = router_config.get("repetition_penalty_2") or 0
        ab_preference1 = ''
        ab_preference2 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        dialog_history = [(self.client_prompt1, ab_preference1), (self.client_prompt2, ab_preference2)]
        check_prompt = check_follow_up_relevance_kimi(kimi_api_key=kimi_api_key, dialog_history=dialog_history,
                                                      follow_up_prompt=self.client_prompt3)
        if 'is_relevant' in check_prompt and check_prompt['is_relevant']:
            self._ensure_image_handle_for_turn(3, genai_api_key)
            history_handle_ids = [
                ((self.image_handle_id_1 or "").strip() or None, (self.image_mime_1 or "").strip() or None),
                ((self.image_handle_id_2 or "").strip() or None, (self.image_mime_2 or "").strip() or None),
            ]
            current_handle = (self.image_handle_id_3 or "").strip() or None
            current_mime = (self.image_mime_3 or "").strip() or None
            self._generate_response_background(
                turn=3, model_1=self.model_1, model_2=self.model_2,
                prompt=(self.client_prompt3 or "").strip() or "",
                genai_api_key=genai_api_key, dialog_id=self.dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_handle,
                current_turn_mime=current_mime,
                history_handle_ids=history_handle_ids,
                temperature_1=self.temperature_1, top_p_1=self.top_p_1, repetition_penalty_1=self.repetition_penalty_1,
                temperature_2=self.temperature_2, top_p_2=self.top_p_2, repetition_penalty_2=self.repetition_penalty_2,
            )
            # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
            try:
                self._run_auto_evaluation_for_turn(3)
            except Exception as e:
                _logger.exception('Auto-evaluation (flat turn 3) failed (non-blocking): %s', e)
        else:
            raise ValidationError("Please rewrite the prompt")

    def action_submit_prompt4(self):
        _logger.info("action_submit_prompt4 called | record=%s | prompt=%s", self.id, (self.client_prompt4 or "")[:200])
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4):
            raise ValidationError("All 4 Prompts Required")
        if not self.dialog_id:
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
            self.dialog_id = dialog_id
            self.model_1 = (router_config.get("model_1") or "").strip() or ""
            self.model_2 = (router_config.get("model_2") or "").strip() or ""
            self.temperature_1 = router_config.get("temperature_1") or 0
            self.top_p_1 = router_config.get("top_p_1") or 0
            self.repetition_penalty_1 = router_config.get("repetition_penalty_1") or 0
            self.temperature_2 = router_config.get("temperature_2") or 0
            self.top_p_2 = router_config.get("top_p_2") or 0
            self.repetition_penalty_2 = router_config.get("repetition_penalty_2") or 0

        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        dialog_history = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3)]
        check_prompt = check_follow_up_relevance_kimi(kimi_api_key=kimi_api_key, dialog_history=dialog_history,
                                                      follow_up_prompt=self.client_prompt4)
        if 'is_relevant' in check_prompt and check_prompt['is_relevant']:
            self._ensure_image_handle_for_turn(4, genai_api_key)
            history_handle_ids = [
                ((self.image_handle_id_1 or "").strip() or None, (self.image_mime_1 or "").strip() or None),
                ((self.image_handle_id_2 or "").strip() or None, (self.image_mime_2 or "").strip() or None),
                ((self.image_handle_id_3 or "").strip() or None, (self.image_mime_3 or "").strip() or None),
            ]
            current_handle = (self.image_handle_id_4 or "").strip() or None
            current_mime = (self.image_mime_4 or "").strip() or None
            self._generate_response_background(
                turn=4, model_1=self.model_1, model_2=self.model_2,
                prompt=(self.client_prompt4 or "").strip() or "",
                genai_api_key=genai_api_key, dialog_id=self.dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_handle,
                current_turn_mime=current_mime,
                history_handle_ids=history_handle_ids,
                temperature_1=self.temperature_1, top_p_1=self.top_p_1, repetition_penalty_1=self.repetition_penalty_1,
                temperature_2=self.temperature_2, top_p_2=self.top_p_2, repetition_penalty_2=self.repetition_penalty_2,
            )
            # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
            try:
                self._run_auto_evaluation_for_turn(4)
            except Exception as e:
                _logger.exception('Auto-evaluation (flat turn 4) failed (non-blocking): %s', e)
        else:
            raise ValidationError("Please rewrite the prompt")

    def action_submit_prompt5(self):
        _logger.info("action_submit_prompt5 called | record=%s | prompt=%s", self.id, (self.client_prompt5 or "")[:200])
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5):
            raise ValidationError("All 5 Prompts Required")
        if not self.dialog_id:
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
            self.dialog_id = dialog_id
            self.model_1 = (router_config.get("model_1") or "").strip() or ""
            self.model_2 = (router_config.get("model_2") or "").strip() or ""
            self.temperature_1 = router_config.get("temperature_1") or 0
            self.top_p_1 = router_config.get("top_p_1") or 0
            self.repetition_penalty_1 = router_config.get("repetition_penalty_1") or 0
            self.temperature_2 = router_config.get("temperature_2") or 0
            self.top_p_2 = router_config.get("top_p_2") or 0
            self.repetition_penalty_2 = router_config.get("repetition_penalty_2") or 0

        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        ab_preference4 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        if self.ab_preference4 in ['-3', '-2', '-1'] or self.ab_preference4 == '0':
            ab_preference4 = self.client_response_a4
        if self.ab_preference4 in ['3', '2', '1']:
            ab_preference4 = self.client_response_b4
        dialog_history = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3),
                              (self.client_prompt4, ab_preference4)]
        check_prompt = check_follow_up_relevance_kimi(kimi_api_key=kimi_api_key, dialog_history=dialog_history,
                                                      follow_up_prompt=self.client_prompt5)
        if 'is_relevant' in check_prompt and check_prompt['is_relevant']:
            self._ensure_image_handle_for_turn(5, genai_api_key)
            history_handle_ids = [
                ((self.image_handle_id_1 or "").strip() or None, (self.image_mime_1 or "").strip() or None),
                ((self.image_handle_id_2 or "").strip() or None, (self.image_mime_2 or "").strip() or None),
                ((self.image_handle_id_3 or "").strip() or None, (self.image_mime_3 or "").strip() or None),
                ((self.image_handle_id_4 or "").strip() or None, (self.image_mime_4 or "").strip() or None),
            ]
            current_handle = (self.image_handle_id_5 or "").strip() or None
            current_mime = (self.image_mime_5 or "").strip() or None
            self._generate_response_background(
                turn=5, model_1=self.model_1, model_2=self.model_2,
                prompt=(self.client_prompt5 or "").strip() or "",
                genai_api_key=genai_api_key, dialog_id=self.dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_handle,
                current_turn_mime=current_mime,
                history_handle_ids=history_handle_ids,
                temperature_1=self.temperature_1, top_p_1=self.top_p_1, repetition_penalty_1=self.repetition_penalty_1,
                temperature_2=self.temperature_2, top_p_2=self.top_p_2, repetition_penalty_2=self.repetition_penalty_2,
            )
            # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
            try:
                self._run_auto_evaluation_for_turn(5)
            except Exception as e:
                _logger.exception('Auto-evaluation (flat turn 5) failed (non-blocking): %s', e)
        else:
            raise ValidationError("Please rewrite the prompt")

    def action_submit_prompt6(self):
        _logger.info("action_submit_prompt6 called | record=%s | prompt=%s", self.id, (self.client_prompt6 or "")[:200])
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5 or not self.client_prompt6):
            raise ValidationError("All 6 Prompts Required")
        if not self.dialog_id:
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
            self.dialog_id = dialog_id
            self.model_1 = (router_config.get("model_1") or "").strip() or ""
            self.model_2 = (router_config.get("model_2") or "").strip() or ""
            self.temperature_1 = router_config.get("temperature_1") or 0
            self.top_p_1 = router_config.get("top_p_1") or 0
            self.repetition_penalty_1 = router_config.get("repetition_penalty_1") or 0
            self.temperature_2 = router_config.get("temperature_2") or 0
            self.top_p_2 = router_config.get("top_p_2") or 0
            self.repetition_penalty_2 = router_config.get("repetition_penalty_2") or 0
        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        ab_preference4 = ''
        ab_preference5 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        if self.ab_preference4 in ['-3', '-2', '-1'] or self.ab_preference4 == '0':
            ab_preference4 = self.client_response_a4
        if self.ab_preference4 in ['3', '2', '1']:
            ab_preference4 = self.client_response_b4
        if self.ab_preference5 in ['-3', '-2', '-1'] or self.ab_preference5 == '0':
            ab_preference5 = self.client_response_a5
        if self.ab_preference5 in ['3', '2', '1']:
            ab_preference5 = self.client_response_b5
        dialog_history = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3),
                              (self.client_prompt4, ab_preference4),
                              (self.client_prompt5, ab_preference5)]
        check_prompt = check_follow_up_relevance_kimi(kimi_api_key=kimi_api_key, dialog_history=dialog_history,
                                                      follow_up_prompt=self.client_prompt6)
        if 'is_relevant' in check_prompt and check_prompt['is_relevant']:
            self._ensure_image_handle_for_turn(6, genai_api_key)
            history_handle_ids = [
                ((self.image_handle_id_1 or "").strip() or None, (self.image_mime_1 or "").strip() or None),
                ((self.image_handle_id_2 or "").strip() or None, (self.image_mime_2 or "").strip() or None),
                ((self.image_handle_id_3 or "").strip() or None, (self.image_mime_3 or "").strip() or None),
                ((self.image_handle_id_4 or "").strip() or None, (self.image_mime_4 or "").strip() or None),
                ((self.image_handle_id_5 or "").strip() or None, (self.image_mime_5 or "").strip() or None),
            ]
            current_handle = (self.image_handle_id_6 or "").strip() or None
            current_mime = (self.image_mime_6 or "").strip() or None
            self._generate_response_background(
                turn=6, model_1=self.model_1, model_2=self.model_2,
                prompt=(self.client_prompt6 or "").strip() or "",
                genai_api_key=genai_api_key, dialog_id=self.dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_handle,
                current_turn_mime=current_mime,
                history_handle_ids=history_handle_ids,
                temperature_1=self.temperature_1, top_p_1=self.top_p_1, repetition_penalty_1=self.repetition_penalty_1,
                temperature_2=self.temperature_2, top_p_2=self.top_p_2, repetition_penalty_2=self.repetition_penalty_2,
            )
            # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
            try:
                self._run_auto_evaluation_for_turn(6)
            except Exception as e:
                _logger.exception('Auto-evaluation (flat turn 6) failed (non-blocking): %s', e)
        else:
            raise ValidationError("Please rewrite the prompt")

    def action_submit_prompt7(self):
        _logger.info("action_submit_prompt7 called | record=%s | prompt=%s", self.id, (self.client_prompt7 or "")[:200])
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5 or
                not self.client_prompt6 or not self.client_prompt7):
            raise ValidationError("All 7 Prompts Required")
        if not self.dialog_id:
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
            self.dialog_id = dialog_id
            self.model_1 = (router_config.get("model_1") or "").strip() or ""
            self.model_2 = (router_config.get("model_2") or "").strip() or ""
            self.temperature_1 = router_config.get("temperature_1") or 0
            self.top_p_1 = router_config.get("top_p_1") or 0
            self.repetition_penalty_1 = router_config.get("repetition_penalty_1") or 0
            self.temperature_2 = router_config.get("temperature_2") or 0
            self.top_p_2 = router_config.get("top_p_2") or 0
            self.repetition_penalty_2 = router_config.get("repetition_penalty_2") or 0

        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        ab_preference4 = ''
        ab_preference5 = ''
        ab_preference6 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        if self.ab_preference4 in ['-3', '-2', '-1'] or self.ab_preference4 == '0':
            ab_preference4 = self.client_response_a4
        if self.ab_preference4 in ['3', '2', '1']:
            ab_preference4 = self.client_response_b4
        if self.ab_preference5 in ['-3', '-2', '-1'] or self.ab_preference5 == '0':
            ab_preference5 = self.client_response_a5
        if self.ab_preference5 in ['3', '2', '1']:
            ab_preference5 = self.client_response_b5
        if self.ab_preference6 in ['-3', '-2', '-1'] or self.ab_preference6 == '0':
            ab_preference6 = self.client_response_a6
        if self.ab_preference6 in ['3', '2', '1']:
            ab_preference6 = self.client_response_b6
        dialog_history = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3),
                              (self.client_prompt4, ab_preference4),
                              (self.client_prompt5, ab_preference5),
                              (self.client_prompt6, ab_preference6)]
        check_prompt = check_follow_up_relevance_kimi(kimi_api_key=kimi_api_key, dialog_history=dialog_history,
                                                      follow_up_prompt=self.client_prompt7)
        if 'is_relevant' in check_prompt and check_prompt['is_relevant']:
            self._ensure_image_handle_for_turn(7, genai_api_key)
            history_handle_ids = [
                ((self.image_handle_id_1 or "").strip() or None, (self.image_mime_1 or "").strip() or None),
                ((self.image_handle_id_2 or "").strip() or None, (self.image_mime_2 or "").strip() or None),
                ((self.image_handle_id_3 or "").strip() or None, (self.image_mime_3 or "").strip() or None),
                ((self.image_handle_id_4 or "").strip() or None, (self.image_mime_4 or "").strip() or None),
                ((self.image_handle_id_5 or "").strip() or None, (self.image_mime_5 or "").strip() or None),
                ((self.image_handle_id_6 or "").strip() or None, (self.image_mime_6 or "").strip() or None),
            ]
            current_handle = (self.image_handle_id_7 or "").strip() or None
            current_mime = (self.image_mime_7 or "").strip() or None
            self._generate_response_background(
                turn=7, model_1=self.model_1, model_2=self.model_2,
                prompt=(self.client_prompt7 or "").strip() or "",
                genai_api_key=genai_api_key, dialog_id=self.dialog_id,
                dialog_history=dialog_history,
                current_turn_handle_id=current_handle,
                current_turn_mime=current_mime,
                history_handle_ids=history_handle_ids,
                temperature_1=self.temperature_1, top_p_1=self.top_p_1, repetition_penalty_1=self.repetition_penalty_1,
                temperature_2=self.temperature_2, top_p_2=self.top_p_2, repetition_penalty_2=self.repetition_penalty_2,
            )
            # --- Auto-evaluate: pre-fill ratings, reasons, and AI store fields ---
            try:
                self._run_auto_evaluation_for_turn(7)
            except Exception as e:
                _logger.exception('Auto-evaluation (flat turn 7) failed (non-blocking): %s', e)
        else:
            raise ValidationError("Please rewrite the prompt")

    def action_turn2(self):
        if not self.client_prompt1:
            raise ValidationError("Above Prompt Required")
        ab_preference1 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        conversation_turns = [(self.client_prompt1, ab_preference1)]
        result = generate_follow_up_prompt_kimi(kimi_api_key=kimi_api_key, conversation_turns=conversation_turns)
        self.store_client_prompt2 = (result.get("follow_up_prompt") or "").strip() if isinstance(result, dict) else ""

    def action_turn3(self):
        if not self.client_prompt1 or not self.client_prompt2:
            raise ValidationError("Above 2 Prompts Required")
        ab_preference1 = ''
        ab_preference2 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        conversation_turns = [(self.client_prompt1, ab_preference1), (self.client_prompt2, ab_preference2)]
        result = generate_follow_up_prompt_kimi(kimi_api_key=kimi_api_key, conversation_turns=conversation_turns)
        self.store_client_prompt3 = (result.get("follow_up_prompt") or "").strip() if isinstance(result, dict) else ""

    def action_turn4(self):
        if not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3:
            raise ValidationError("Above 3 Prompts Required")
        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        conversation_turns = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3)]
        result = generate_follow_up_prompt_kimi(kimi_api_key=kimi_api_key, conversation_turns=conversation_turns)
        self.store_client_prompt4 = (result.get("follow_up_prompt") or "").strip() if isinstance(result, dict) else ""

    def action_turn5(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4):
            raise ValidationError("Above 4 Prompts Required")
        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        ab_preference4 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        if self.ab_preference4 in ['-3', '-2', '-1'] or self.ab_preference4 == '0':
            ab_preference4 = self.client_response_a4
        if self.ab_preference4 in ['3', '2', '1']:
            ab_preference4 = self.client_response_b4
        conversation_turns = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3),
                              (self.client_prompt4, ab_preference4)]
        result = generate_follow_up_prompt_kimi(kimi_api_key=kimi_api_key, conversation_turns=conversation_turns)
        self.store_client_prompt5 = (result.get("follow_up_prompt") or "").strip() if isinstance(result, dict) else ""

    def action_turn6(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5):
            raise ValidationError("Above 5 Prompts Required")
        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        ab_preference4 = ''
        ab_preference5 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        if self.ab_preference4 in ['-3', '-2', '-1'] or self.ab_preference4 == '0':
            ab_preference4 = self.client_response_a4
        if self.ab_preference4 in ['3', '2', '1']:
            ab_preference4 = self.client_response_b4
        if self.ab_preference5 in ['-3', '-2', '-1'] or self.ab_preference5 == '0':
            ab_preference5 = self.client_response_a5
        if self.ab_preference5 in ['3', '2', '1']:
            ab_preference5 = self.client_response_b5
        conversation_turns = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3),
                              (self.client_prompt4, ab_preference4),
                              (self.client_prompt5, ab_preference5)]
        result = generate_follow_up_prompt_kimi(kimi_api_key=kimi_api_key, conversation_turns=conversation_turns)
        self.store_client_prompt6 = (result.get("follow_up_prompt") or "").strip() if isinstance(result, dict) else ""

    def action_turn7(self):
        if (not self.client_prompt1 or not self.client_prompt2 or not self.client_prompt3 or
                not self.client_prompt4 or not self.client_prompt5 or not self.client_prompt6):
            raise ValidationError("Above 6 Prompts Required")
        ab_preference1 = ''
        ab_preference2 = ''
        ab_preference3 = ''
        ab_preference4 = ''
        ab_preference5 = ''
        ab_preference6 = ''
        if self.ab_preference1 in ['-3', '-2', '-1'] or self.ab_preference1 == '0':
            ab_preference1 = self.client_response_a1
        if self.ab_preference1 in ['3', '2', '1']:
            ab_preference1 = self.client_response_b1
        if self.ab_preference2 in ['-3', '-2', '-1'] or self.ab_preference2 == '0':
            ab_preference2 = self.client_response_a2
        if self.ab_preference2 in ['3', '2', '1']:
            ab_preference2 = self.client_response_b2
        if self.ab_preference3 in ['-3', '-2', '-1'] or self.ab_preference3 == '0':
            ab_preference3 = self.client_response_a3
        if self.ab_preference3 in ['3', '2', '1']:
            ab_preference3 = self.client_response_b3
        if self.ab_preference4 in ['-3', '-2', '-1'] or self.ab_preference4 == '0':
            ab_preference4 = self.client_response_a4
        if self.ab_preference4 in ['3', '2', '1']:
            ab_preference4 = self.client_response_b4
        if self.ab_preference5 in ['-3', '-2', '-1'] or self.ab_preference5 == '0':
            ab_preference5 = self.client_response_a5
        if self.ab_preference5 in ['3', '2', '1']:
            ab_preference5 = self.client_response_b5
        if self.ab_preference6 in ['-3', '-2', '-1'] or self.ab_preference6 == '0':
            ab_preference6 = self.client_response_a6
        if self.ab_preference6 in ['3', '2', '1']:
            ab_preference6 = self.client_response_b6
        conversation_turns = [(self.client_prompt1, ab_preference1),
                              (self.client_prompt2, ab_preference2),
                              (self.client_prompt3, ab_preference3),
                              (self.client_prompt4, ab_preference4),
                              (self.client_prompt5, ab_preference5),
                              (self.client_prompt6, ab_preference6)]
        result = generate_follow_up_prompt_kimi(kimi_api_key=kimi_api_key, conversation_turns=conversation_turns)
        self.store_client_prompt7 = (result.get("follow_up_prompt") or "").strip() if isinstance(result, dict) else ""