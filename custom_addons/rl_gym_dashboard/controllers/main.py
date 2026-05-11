# -*- coding: utf-8 -*-

import io
import json
import logging
import requests

from odoo import http
from odoo.http import request

from ..models.training_job import ensure_runner_alive

_logger = logging.getLogger(__name__)

HF_API_BASE = 'https://huggingface.co/api'


class RlGymController(http.Controller):

    def _get_hf_token(self):
        return request.env['ir.config_parameter'].sudo().get_param(
            'rl_gym_dashboard.hf_token', default=''
        )

    @http.route('/rl_gym/models', type='json', auth='public', cors='*')
    def get_models(self):
        models = request.env['rl.training.model'].sudo().search_read(
            [('active', '=', True)],
            ['name', 'technical_name', 'model_type', 'description',
             'parameter_count', 'architecture'],
            order='sequence, name'
        )
        return models

    @http.route('/rl_gym/datasets/search', type='json', auth='public', cors='*')
    def search_datasets(self, query='', author='ethara'):
        headers = {'Authorization': f'Bearer {self._get_hf_token()}'}
        params = {'author': author, 'search': query, 'limit': 50}
        try:
            resp = requests.get(
                f'{HF_API_BASE}/datasets',
                headers=headers, params=params, timeout=15
            )
            resp.raise_for_status()
            datasets = resp.json()
            return [{
                'id': ds.get('id', ''),
                'name': ds.get('id', '').split('/')[-1],
                'description': ds.get('description', ''),
                'downloads': ds.get('downloads', 0),
                'likes': ds.get('likes', 0),
                'tags': ds.get('tags', []),
                'last_modified': ds.get('lastModified', ''),
            } for ds in datasets]
        except requests.RequestException as e:
            _logger.warning('HuggingFace API error: %s', e)
            return {'error': str(e)}

    @http.route('/rl_gym/datasets/info', type='json', auth='public', cors='*')
    def get_dataset_info(self, repo_id):
        headers = {'Authorization': f'Bearer {self._get_hf_token()}'}
        try:
            resp = requests.get(
                f'{HF_API_BASE}/datasets/{repo_id}',
                headers=headers, timeout=15
            )
            resp.raise_for_status()
            info = resp.json()

            preview = self._fetch_dataset_preview(repo_id, headers)

            return {
                'id': info.get('id'),
                'description': info.get('description', ''),
                'citation': info.get('citation', ''),
                'card_data': info.get('cardData', {}),
                'tags': info.get('tags', []),
                'downloads': info.get('downloads', 0),
                'splits': info.get('splits', {}),
                'preview': preview,
            }
        except requests.RequestException as e:
            _logger.warning('HuggingFace dataset info error: %s', e)
            return {'error': str(e)}

    def _fetch_dataset_preview(self, repo_id, headers):
        """Fetch actual row data for preview. Handles JSONL, JSON, Parquet, and datasets-server."""
        MAX_PREVIEW_ROWS = 50

        # Strategy 1: datasets-server first-rows (public/enterprise datasets)
        for split in ('train', 'test', 'validation'):
            try:
                resp = requests.get(
                    f'https://datasets-server.huggingface.co/first-rows'
                    f'?dataset={repo_id}&config=default&split={split}&length={MAX_PREVIEW_ROWS}',
                    headers=headers, timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    columns = [f['column']['name'] for f in data.get('features', [])]
                    raw_rows = data.get('rows', [])
                    rows = [r.get('row', r) if isinstance(r, dict) else r
                            for r in raw_rows][:MAX_PREVIEW_ROWS]
                    if columns:
                        return {'columns': columns, 'rows': self._truncate_rows(rows, columns)}
            except requests.RequestException:
                continue

        # Strategy 2: Collect all data files (root + all subdirectories)
        all_files = []
        dirs_to_scan = ['', 'data']
        try:
            root_resp = requests.get(
                f'{HF_API_BASE}/datasets/{repo_id}/tree/main',
                headers=headers, timeout=10
            )
            if root_resp.status_code == 200:
                for f in root_resp.json():
                    if f.get('type') == 'file':
                        all_files.append(f)
                    elif f.get('type') == 'directory':
                        dname = f.get('path', '')
                        if dname and not dname.startswith('.'):
                            dirs_to_scan.append(dname)
        except requests.RequestException:
            pass

        for path in dirs_to_scan:
            if not path:
                continue
            try:
                tree_resp = requests.get(
                    f'{HF_API_BASE}/datasets/{repo_id}/tree/main/{path}',
                    headers=headers, timeout=10
                )
                if tree_resp.status_code == 200:
                    for f in tree_resp.json():
                        if f.get('type') == 'file':
                            all_files.append(f)
            except requests.RequestException:
                continue

        # Separate by type
        jsonl_files = [f for f in all_files
                       if self._get_path(f).endswith('.jsonl')]
        parquet_files = [f for f in all_files
                         if self._get_path(f).endswith('.parquet')]
        json_files = [f for f in all_files
                      if self._get_path(f).endswith('.json')
                      and not self._get_path(f).startswith('.')]

        # Priority: JSON arrays (cheap) → Parquet (single download) → JSONL (many downloads)

        if json_files:
            for file_info in json_files[:5]:
                fname = self._get_path(file_info)
                file_size = file_info.get('size', 0)
                if file_size > 5_000_000:
                    continue
                dl_url = f'https://huggingface.co/datasets/{repo_id}/resolve/main/{fname}'
                try:
                    dl_resp = requests.get(dl_url, headers=headers, timeout=20)
                    if dl_resp.status_code == 200:
                        data = dl_resp.json()
                        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                            columns = list(data[0].keys())
                            rows = data[:MAX_PREVIEW_ROWS]
                            return {'columns': columns, 'rows': self._truncate_rows(rows, columns)}
                        elif isinstance(data, dict):
                            first_val = next(iter(data.values()), None)
                            if isinstance(first_val, list):
                                columns = list(data.keys())
                                max_len = min(MAX_PREVIEW_ROWS, max(len(v) for v in data.values() if isinstance(v, list)))
                                rows = []
                                for i in range(max_len):
                                    row = {}
                                    for col in columns:
                                        vals = data[col]
                                        row[col] = vals[i] if isinstance(vals, list) and i < len(vals) else ''
                                    rows.append(row)
                                return {'columns': columns, 'rows': self._truncate_rows(rows, columns)}
                except (requests.RequestException, json.JSONDecodeError, ValueError):
                    continue

        if parquet_files:
            try:
                import pyarrow.parquet as pq
            except ImportError:
                pq = None
            if pq:
                for file_info in parquet_files[:3]:
                    fname = self._get_path(file_info)
                    file_size = file_info.get('size', 0)
                    dl_url = f'https://huggingface.co/datasets/{repo_id}/resolve/main/{fname}'
                    try:
                        if file_size > 10_000_000:
                            dl_resp = requests.get(
                                dl_url, headers={**headers, 'Range': 'bytes=0-4194304'},
                                timeout=20
                            )
                        else:
                            dl_resp = requests.get(dl_url, headers=headers, timeout=30)
                        if dl_resp.status_code in (200, 206):
                            buf = io.BytesIO(dl_resp.content)
                            table = pq.read_table(buf)
                            columns = table.column_names
                            df = table.slice(0, min(MAX_PREVIEW_ROWS, table.num_rows)).to_pandas()
                            rows = []
                            for _, r in df.iterrows():
                                rows.append({col: r[col] for col in columns})
                            return {'columns': columns, 'rows': self._truncate_rows(rows, columns)}
                    except Exception as e:
                        _logger.debug('Parquet read failed for %s: %s', fname, e)
                        continue

        if jsonl_files:
            import time as _time
            rows = []
            columns = None
            budget_start = _time.time()
            for file_info in jsonl_files[:20]:
                if len(rows) >= MAX_PREVIEW_ROWS:
                    break
                if _time.time() - budget_start > 30:
                    break
                fname = self._get_path(file_info)
                dl_url = f'https://huggingface.co/datasets/{repo_id}/resolve/main/{fname}'
                try:
                    dl_resp = requests.get(dl_url, headers=headers, timeout=8, stream=True)
                    if dl_resp.status_code == 200:
                        for line in dl_resp.iter_lines(decode_unicode=True):
                            if not line:
                                continue
                            if len(rows) >= MAX_PREVIEW_ROWS:
                                break
                            row = json.loads(line)
                            if columns is None:
                                columns = list(row.keys())
                            rows.append(row)
                        dl_resp.close()
                except (requests.RequestException, json.JSONDecodeError):
                    continue
            if columns and rows:
                return {'columns': columns, 'rows': self._truncate_rows(rows, columns)}

        if json_files:
            rows = []
            columns = None
            for file_info in json_files[:MAX_PREVIEW_ROWS]:
                if len(rows) >= MAX_PREVIEW_ROWS:
                    break
                fname = self._get_path(file_info)
                file_size = file_info.get('size', 0)
                if file_size > 1_000_000:
                    continue
                dl_url = f'https://huggingface.co/datasets/{repo_id}/resolve/main/{fname}'
                try:
                    dl_resp = requests.get(dl_url, headers=headers, timeout=15)
                    if dl_resp.status_code == 200:
                        data = dl_resp.json()
                        if isinstance(data, dict) and not isinstance(next(iter(data.values()), None), (list, dict)):
                            if columns is None:
                                columns = list(data.keys())
                            rows.append(data)
                except (requests.RequestException, json.JSONDecodeError, ValueError):
                    continue
            if columns and rows:
                return {'columns': columns, 'rows': self._truncate_rows(rows, columns)}

        return {'columns': [], 'rows': [], 'note': 'No previewable data files found in this dataset.'}

    def _get_path(self, file_info):
        return file_info.get('rfilename', file_info.get('path', ''))

    def _truncate_rows(self, rows, columns):
        truncated = []
        for row in rows:
            preview_row = {}
            for col in columns:
                val = row.get(col, '')
                sv = str(val) if not isinstance(val, str) else val
                preview_row[col] = sv[:500] if len(sv) > 500 else sv
            truncated.append(preview_row)
        return truncated

    @http.route('/rl_gym/datasets/save', type='json', auth='public', cors='*')
    def save_dataset(self, values):
        dataset = request.env['rl.training.dataset'].sudo().create(values)
        return {'id': dataset.id, 'name': dataset.name}

    @http.route('/rl_gym/training/create_job', type='json', auth='public', cors='*')
    def create_training_job(self, values, model_id=None, config_id=None):
        vals = dict(values or {})
        if model_id:
            vals['model_id'] = int(model_id)
        if config_id:
            vals['config_id'] = int(config_id)
        vals.setdefault('total_steps', 450)
        job = request.env['rl.training.job'].sudo().create(vals)
        return {'id': job.id, 'name': job.name, 'state': job.state}

    @http.route('/rl_gym/training/start', type='json', auth='public', cors='*')
    def start_training(self, job_id):
        job = request.env['rl.training.job'].sudo().browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}
        job.action_start_training()
        return {'state': job.state, 'started_at': str(job.started_at)}

    @http.route('/rl_gym/training/step', type='json', auth='public', cors='*')
    def training_step(self, job_id):
        job = request.env['rl.training.job'].sudo().browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}

        if job.state == 'training':
            ensure_runner_alive(request.env.cr.dbname)

        metric = request.env['rl.training.metric'].sudo().search(
            [('job_id', '=', job.id)],
            order='step desc',
            limit=1,
        )
        m = {}
        if metric:
            m = {
                'loss': metric.loss,
                'reward': metric.reward,
                'gradient_norm': metric.gradient_norm,
                'learning_rate': metric.learning_rate,
                'entropy': metric.entropy,
                'kl_divergence': metric.kl_divergence,
                'tokens_per_second': metric.tokens_per_second,
                'policy_loss': metric.policy_loss,
                'value_loss': metric.value_loss,
                'clip_fraction': metric.clip_fraction,
                'reward_mean': metric.reward_mean,
                'reward_std': metric.reward_std,
                'advantage_mean': metric.advantage_mean,
                'samples_per_second': metric.samples_per_second,
                'gpu_memory_used': metric.gpu_memory_used,
                'cpu_percent': metric.cpu_percent,
                'memory_percent': metric.memory_percent,
                'gpu_utilization': metric.gpu_utilization,
            }

        return {
            'state': job.state,
            'current_step': job.current_step,
            'total_steps': job.total_steps,
            'progress': job.progress,
            'current_loss': job.current_loss,
            'current_reward': job.current_reward,
            'best_reward': job.best_reward,
            'policy_type': job.config_id.policy_type if job.config_id else 'gtpo',
            **m,
        }

    @http.route('/rl_gym/training/metrics', type='json', auth='public', cors='*')
    def get_training_metrics(self, job_id, since_step=0):
        metrics = request.env['rl.training.metric'].sudo().search_read(
            [('job_id', '=', int(job_id)), ('step', '>', int(since_step))],
            ['step', 'loss', 'reward', 'gradient_norm', 'learning_rate',
                'entropy', 'kl_divergence', 'tokens_per_second',
                'policy_loss', 'value_loss', 'clip_fraction',
                'reward_mean', 'reward_std', 'advantage_mean',
                'samples_per_second', 'gpu_memory_used',
                'cpu_percent', 'memory_percent', 'gpu_utilization'],
            order='step asc',
            limit=2000
        )
        return metrics

    @http.route('/rl_gym/training/status', type='json', auth='public', cors='*')
    def get_training_status(self, job_id):
        job = request.env['rl.training.job'].sudo().browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}
        return {
            'id': job.id,
            'state': job.state,
            'current_step': job.current_step,
            'total_steps': job.total_steps,
            'progress': job.progress,
            'current_loss': job.current_loss,
            'current_reward': job.current_reward,
            'best_reward': job.best_reward,
            'started_at': str(job.started_at) if job.started_at else None,
            'completed_at': str(job.completed_at) if job.completed_at else None,
        }

    @http.route('/rl_gym/weights/upload', type='json', auth='public', cors='*')
    def upload_weights(self, weight_id):
        weight = request.env['rl.training.weight'].sudo().browse(int(weight_id))
        if not weight.exists():
            return {'error': 'Weight not found'}

        config = weight.get_upload_config()
        if not config.get('aws_bucket'):
            return {'error': 'S3 not configured. Set system parameters: rl_gym.aws_*'}

        weight.write({'upload_state': 'uploading', 'upload_progress': 0})

        try:
            import boto3
            import os
            safe_path = os.path.realpath(weight.file_path or '')
            if not os.path.isfile(safe_path):
                return {'error': 'Weight file not found on server'}
            allowed_dirs = ['/tmp', '/var/lib/odoo', '/home']
            if not any(safe_path.startswith(d) for d in allowed_dirs):
                return {'error': 'File path not in allowed directory'}

            s3 = boto3.client(
                's3',
                aws_access_key_id=config['aws_access_key'],
                aws_secret_access_key=config['aws_secret_key'],
                region_name=config['aws_region'],
            )

            s3_key = f"models/{weight.job_id.name}/{weight.name}"
            s3.upload_file(
                safe_path,
                config['aws_bucket'],
                s3_key,
            )

            s3_url = f"https://{config['aws_bucket']}.s3.{config['aws_region']}.amazonaws.com/{s3_key}"
            weight.write({
                'upload_state': 'uploaded',
                'upload_progress': 100,
                's3_bucket': config['aws_bucket'],
                's3_key': s3_key,
                's3_url': s3_url,
            })
            return {'state': 'uploaded', 's3_url': s3_url}

        except Exception as e:
            weight.write({
                'upload_state': 'failed',
                'upload_error': str(e),
            })
            return {'error': str(e)}

    @http.route('/rl_gym/weights/create', type='json', auth='public', cors='*')
    def create_weight(self, values):
        weight = request.env['rl.training.weight'].sudo().create(values)
        return {'id': weight.id, 'name': weight.name}

    @http.route('/rl_gym/inference/run', type='json', auth='public', cors='*')
    def run_inference(self, job_id, prompt, max_tokens=256, temperature=0.7):
        job = request.env['rl.training.job'].sudo().browse(int(job_id))
        if not job.exists():
            return {'error': 'Job not found'}
        if job.state != 'completed':
            return {'error': 'Training not completed'}

        simulated_response = self._simulate_inference(prompt, max_tokens, temperature)
        return {
            'prompt': prompt,
            'response': simulated_response,
            'tokens_used': len(simulated_response.split()),
            'model': job.model_id.name,
            'job': job.name,
        }

    def _simulate_inference(self, prompt, max_tokens, temperature):
        responses = [
            "Based on the training data analysis, the optimal approach involves "
            "implementing a multi-step verification process that validates each "
            "intermediate result before proceeding to the final computation.",
            "The model identifies three key patterns in the input: structural "
            "consistency, semantic coherence, and logical flow. Each pattern "
            "contributes to the overall confidence score of the prediction.",
            "After analyzing the provided context, the recommended solution "
            "leverages curriculum-based learning with progressive difficulty "
            "scaling to achieve robust generalization across domains.",
        ]
        import hashlib
        idx = int(hashlib.md5(prompt.encode()).hexdigest(), 16) % len(responses)
        return responses[idx]

    @http.route('/rl_gym/config/save', type='json', auth='public', cors='*')
    def save_config(self, values, model_id=None, job_name=''):
        # Map frontend field names to model field names
        field_map = {
            'gradient_accumulation': 'gradient_accumulation_steps',
            'curriculum_stages': 'curriculum_phases',
            'gpu_count': 'num_gpus',
        }
        mapped = {}
        config_fields = request.env['rl.training.config'].sudo()._fields
        for k, v in (values or {}).items():
            mapped_key = field_map.get(k, k)
            if mapped_key in config_fields:
                mapped[mapped_key] = v

        if model_id:
            mapped['model_id'] = int(model_id)
        mapped.setdefault('name', job_name or 'default-config')

        config = request.env['rl.training.config'].sudo().create(mapped)
        return {'id': config.id, 'name': config.name}

    @http.route('/rl_gym/config/defaults', type='json', auth='public', cors='*')
    def get_config_defaults(self, model_id=None):
        MODEL_DEFAULTS = {
            'nemotron': {
                'policy_type': 'gtpo', 'lora_alpha': 256, 'lora_rank': 64,
                'lora_dropout': 0.0, 'batch_size': 64, 'gspo_group_size': 8,
                'clip_low': 0.2, 'clip_high': 0.28, 'gspo_kl_coeff': 0.0,
                'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
                'learning_rate': 3e-6, 'max_steps': 450, 'warmup_steps': 10,
                'gradient_accumulation': 4, 'max_grad_norm': 1.0, 'weight_decay': 0.01,
                'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
                'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
                'curriculum_enabled': True, 'curriculum_stages': 4,
                'advance_threshold': 0.7, 'advance_window': 5,
                'phase_max_turns': '10,20,35,50',
                'gpu_count': 8, 'precision': 'bf16',
                'tp_size': 2, 'max_model_len': 131072,
                'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
                'outcome_pass': 1.0, 'outcome_fail': -0.1,
                'outcome_empty': -0.2, 'outcome_timeout': -0.5,
                'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
                'partial_credit_alpha': 0.5, 'format_penalty_enabled': True,
                'format_penalty_value': -0.1, 'overlong_penalty': True,
                'overlong_penalty_threshold': 10,
                'checkpoint_every': 10, 'eval_every': 10,
                'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
                'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
                'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
                'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
                'min_lr_ratio': 0.1,
            },
            'llama': {
                'policy_type': 'gtpo', 'lora_alpha': 256, 'lora_rank': 64,
                'lora_dropout': 0.0, 'batch_size': 64, 'gspo_group_size': 8,
                'clip_low': 0.2, 'clip_high': 0.28, 'gspo_kl_coeff': 0.0,
                'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
                'learning_rate': 2e-6, 'max_steps': 450, 'warmup_steps': 10,
                'gradient_accumulation': 4, 'max_grad_norm': 1.0, 'weight_decay': 0.01,
                'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
                'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
                'curriculum_enabled': True, 'curriculum_stages': 4,
                'advance_threshold': 0.7, 'advance_window': 5,
                'phase_max_turns': '10,20,35,50',
                'gpu_count': 4, 'precision': 'bf16',
                'tp_size': 1, 'max_model_len': 131072,
                'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 1,
                'outcome_pass': 1.0, 'outcome_fail': -0.1,
                'outcome_empty': -0.2, 'outcome_timeout': -0.5,
                'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
                'partial_credit_alpha': 0.5, 'format_penalty_enabled': True,
                'format_penalty_value': -0.1, 'overlong_penalty': True,
                'overlong_penalty_threshold': 10,
                'checkpoint_every': 10, 'eval_every': 10,
                'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
                'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
                'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
                'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
                'min_lr_ratio': 0.1,
            },
            'qwen': {
                'policy_type': 'gtpo', 'lora_alpha': 256, 'lora_rank': 64,
                'lora_dropout': 0.0, 'batch_size': 64, 'gspo_group_size': 8,
                'clip_low': 0.2, 'clip_high': 0.28, 'gspo_kl_coeff': 0.0,
                'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
                'learning_rate': 2e-6, 'max_steps': 450, 'warmup_steps': 10,
                'gradient_accumulation': 4, 'max_grad_norm': 1.0, 'weight_decay': 0.01,
                'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
                'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
                'curriculum_enabled': True, 'curriculum_stages': 4,
                'advance_threshold': 0.7, 'advance_window': 5,
                'phase_max_turns': '10,20,35,50',
                'gpu_count': 4, 'precision': 'bf16',
                'tp_size': 1, 'max_model_len': 131072,
                'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 1,
                'outcome_pass': 1.0, 'outcome_fail': -0.1,
                'outcome_empty': -0.2, 'outcome_timeout': -0.5,
                'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
                'partial_credit_alpha': 0.5, 'format_penalty_enabled': True,
                'format_penalty_value': -0.1, 'overlong_penalty': True,
                'overlong_penalty_threshold': 10,
                'checkpoint_every': 10, 'eval_every': 10,
                'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
                'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
                'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
                'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
                'min_lr_ratio': 0.1,
            },
            'mistral': {
                'policy_type': 'gtpo', 'lora_alpha': 256, 'lora_rank': 64,
                'lora_dropout': 0.0, 'batch_size': 64, 'gspo_group_size': 8,
                'clip_low': 0.2, 'clip_high': 0.28, 'gspo_kl_coeff': 0.0,
                'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
                'learning_rate': 2e-6, 'max_steps': 450, 'warmup_steps': 10,
                'gradient_accumulation': 4, 'max_grad_norm': 1.0, 'weight_decay': 0.01,
                'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
                'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
                'curriculum_enabled': True, 'curriculum_stages': 4,
                'advance_threshold': 0.7, 'advance_window': 5,
                'phase_max_turns': '10,20,35,50',
                'gpu_count': 4, 'precision': 'bf16',
                'tp_size': 1, 'max_model_len': 131072,
                'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 1,
                'outcome_pass': 1.0, 'outcome_fail': -0.1,
                'outcome_empty': -0.2, 'outcome_timeout': -0.5,
                'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
                'partial_credit_alpha': 0.5, 'format_penalty_enabled': True,
                'format_penalty_value': -0.1, 'overlong_penalty': True,
                'overlong_penalty_threshold': 10,
                'checkpoint_every': 10, 'eval_every': 10,
                'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
                'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
                'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
                'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
                'min_lr_ratio': 0.1,
            },
        }

        if not model_id:
            return MODEL_DEFAULTS.get('nemotron', {})

        model = request.env['rl.training.model'].sudo().browse(int(model_id))
        if not model.exists():
            return MODEL_DEFAULTS.get('nemotron', {})

        model_key = (model.technical_name or model.name or '').lower()
        for key, defaults in MODEL_DEFAULTS.items():
            if key in model_key:
                return defaults

        return MODEL_DEFAULTS.get('nemotron', {})

    # ─── Dataset-driven dynamic config ──────────────────────────────────────
    DATASET_CONFIGS = {
        'ethara/MILO-Bench': {
            'policy_type': 'gtpo', 'learning_rate': 3e-6, 'batch_size': 64,
            'gradient_accumulation': 4, 'max_steps': 450, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.0,
            'clip_low': 0.2, 'clip_high': 0.28,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': -0.1,
            'outcome_empty': -0.2, 'outcome_timeout': -0.5,
            'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
            'partial_credit_alpha': 0.5, 'format_penalty_enabled': True,
            'format_penalty_value': -0.1, 'overlong_penalty': True,
            'overlong_penalty_threshold': 10,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Gated RLVR + PRM step-level credit + length penalty. Gate opens only on PASS. Multi-turn with discounted returns (\u03b3=0.9).',
        },
        'ethara/Kaiju': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 32,
            'gradient_accumulation': 4, 'max_steps': 450, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 4, 'gspo_kl_coeff': 0.01,
            'clip_low': 0.2, 'clip_high': 0.2,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 0.8, 'top_p': 0.95, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': 0.0,
            'outcome_empty': -0.2, 'outcome_timeout': -0.3,
            'length_penalty_weight': 0.05, 'partial_credit_enabled': True,
            'partial_credit_alpha': 0.7, 'format_penalty_enabled': True,
            'format_penalty_value': -0.1, 'overlong_penalty': True,
            'overlong_penalty_threshold': 15,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Hierarchical reward: compile (0.2) + link (0.1) + test pass (0.7). File-level grouping with KL annealing 0.01→0.04.',
        },
        'ethara/Kraken': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 64,
            'gradient_accumulation': 4, 'max_steps': 300, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.0,
            'clip_low': 0.2, 'clip_high': 0.2,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 0.7, 'top_p': 0.95, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': 0.0,
            'outcome_empty': -0.1, 'outcome_timeout': -0.3,
            'length_penalty_weight': 0.0, 'partial_credit_enabled': True,
            'partial_credit_alpha': 0.8, 'format_penalty_enabled': True,
            'format_penalty_value': -0.05, 'overlong_penalty': False,
            'overlong_penalty_threshold': 10,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Continuous speedup ratio (HSR). Reward = min(1, speedup/target). Performance-shaping with logarithmic scaling.',
        },
        'ethara/tesseract': {
            'policy_type': 'gtpo', 'learning_rate': 3e-6, 'batch_size': 64,
            'gradient_accumulation': 4, 'max_steps': 400, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.0,
            'clip_low': 0.2, 'clip_high': 0.28,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': -0.1,
            'outcome_empty': -0.2, 'outcome_timeout': -0.5,
            'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
            'partial_credit_alpha': 0.5, 'format_penalty_enabled': True,
            'format_penalty_value': -0.1, 'overlong_penalty': True,
            'overlong_penalty_threshold': 10,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Binary execution reward + step-level credit assignment. PRM scores per-turn contributions with entropy-weighted credit.',
        },
        'ethara/Valkyrie': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 64,
            'gradient_accumulation': 4, 'max_steps': 300, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.0,
            'clip_low': 0.2, 'clip_high': 0.2,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': 0.0,
            'outcome_empty': -0.1, 'outcome_timeout': -0.3,
            'length_penalty_weight': 0.05, 'partial_credit_enabled': False,
            'partial_credit_alpha': 0.0, 'format_penalty_enabled': True,
            'format_penalty_value': -0.1, 'overlong_penalty': True,
            'overlong_penalty_threshold': 12,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Binary all-pass reward (+1 if all tests pass, 0 otherwise). Strict gating with no partial credit.',
        },
        'ethara/Janus': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 64,
            'gradient_accumulation': 4, 'max_steps': 400, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.0,
            'clip_low': 0.2, 'clip_high': 0.28,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.8, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': -0.05,
            'outcome_empty': -0.2, 'outcome_timeout': -0.4,
            'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
            'partial_credit_alpha': 0.6, 'format_penalty_enabled': True,
            'format_penalty_value': -0.1, 'overlong_penalty': True,
            'overlong_penalty_threshold': 15,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Multi-signal: V-tool correctness + V-true verification + S-axis structural + accuracy + efficiency. Adaptive KL 0.1\u21920.01.',
        },
        'ethara/terra': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 64,
            'gradient_accumulation': 4, 'max_steps': 350, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.0,
            'clip_low': 0.2, 'clip_high': 0.28,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 1.0, 'top_p': 1.0, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': 0.0,
            'outcome_empty': -0.2, 'outcome_timeout': -0.5,
            'length_penalty_weight': 0.15, 'partial_credit_enabled': False,
            'partial_credit_alpha': 0.0, 'format_penalty_enabled': True,
            'format_penalty_value': -0.1, 'overlong_penalty': True,
            'overlong_penalty_threshold': 8,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Binary exact-match + overlong penalty. GTPO+ARPO hybrid with length penalty at 70% max_turns.',
        },
        'ethara/Mars': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 64,
            'gradient_accumulation': 8, 'max_steps': 300, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.0,
            'clip_low': 0.2, 'clip_high': 0.2,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 0.7, 'top_p': 1.0, 'max_new_tokens': 8192,
            'outcome_pass': 1.0, 'outcome_fail': 0.0,
            'outcome_empty': -0.1, 'outcome_timeout': -0.3,
            'length_penalty_weight': 0.0, 'partial_credit_enabled': False,
            'partial_credit_alpha': 0.0, 'format_penalty_enabled': False,
            'format_penalty_value': 0.0, 'overlong_penalty': False,
            'overlong_penalty_threshold': 20,
            'prm_weight': 0.0, 'shaping_alpha': 0.0, 'advantage_mode': 'rloo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Binary test-suite reward. Sequence-level GTPO with large effective batch (64×8=512 rollouts). No KL — pure on-policy.',
        },
        'ethara/Vesta': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 64,
            'gradient_accumulation': 4, 'max_steps': 300, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 8, 'gspo_kl_coeff': 0.04,
            'clip_low': 0.2, 'clip_high': 0.2,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.7, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 0.8, 'top_p': 0.95, 'max_new_tokens': 4096,
            'outcome_pass': 1.0, 'outcome_fail': -0.1,
            'outcome_empty': -0.2, 'outcome_timeout': -0.4,
            'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
            'partial_credit_alpha': 0.6, 'format_penalty_enabled': True,
            'format_penalty_value': -0.1, 'overlong_penalty': True,
            'overlong_penalty_threshold': 12,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Dual-layered: rule-based verification + LLM-judge scoring. KL=0.04 for stable exploration.',
        },
        'ethara/pax': {
            'policy_type': 'gtpo', 'learning_rate': 1e-6, 'batch_size': 32,
            'gradient_accumulation': 4, 'max_steps': 400, 'warmup_steps': 10,
            'max_grad_norm': 1.0, 'weight_decay': 0.01,
            'lora_rank': 64, 'lora_alpha': 256, 'lora_dropout': 0.0,
            'gspo_group_size': 4, 'gspo_kl_coeff': 0.04,
            'clip_low': 0.2, 'clip_high': 0.2,
            'gtpo_gamma': 0.9, 'gtpo_ent_threshold': 0.8, 'gtpo_ent_scale': 0.1,
            'dual_clip': True, 'dual_clip_coef': 5.0, 'norm_adv_by_std': False,
            'temperature': 0.7, 'top_p': 0.9, 'max_new_tokens': 2048,
            'outcome_pass': 1.0, 'outcome_fail': -0.2,
            'outcome_empty': -0.3, 'outcome_timeout': -0.5,
            'length_penalty_weight': 0.1, 'partial_credit_enabled': True,
            'partial_credit_alpha': 0.4, 'format_penalty_enabled': True,
            'format_penalty_value': -0.15, 'overlong_penalty': True,
            'overlong_penalty_threshold': 10,
            'prm_weight': 0.3, 'shaping_alpha': 0.3, 'advantage_mode': 'gtpo',
            'lora_exclude_modules': '*out_proj*', 'lora_a_init': 'xavier',
            'min_lr_ratio': 0.1,
            'checkpoint_every': 10, 'eval_every': 10,
            'echo_trap_threshold': 0.02, 'echo_trap_window': 20,
            'grad_explosion_threshold': 100.0, 'dead_training_window': 20,
            'curriculum_enabled': True, 'curriculum_stages': 4,
            'advance_threshold': 0.7, 'advance_window': 5,
            'phase_max_turns': '10,20,35,50',
            'gpu_count': 8, 'precision': 'bf16',
            'tp_size': 2, 'max_model_len': 131072,
            'docker_containers': 64, 'docker_timeout': 1800, 'vllm_gpus': 2,
            'reward_description': 'Multi-dim safety: R_safety + 0.3×R_helpful + 0.2×R_tool − 0.5×R_over_refusal. Progressive group G=4→12. KL=0.04 for safety stability.',
        },
    }

    @http.route('/rl_gym/config/dataset_defaults', type='json', auth='public', cors='*')
    def get_dataset_defaults(self, repo_id=None):
        if not repo_id:
            return {}
        return self.DATASET_CONFIGS.get(repo_id, {})

    @http.route('/rl_gym/dashboard/runs', type='json', auth='public', cors='*')
    def get_dashboard_runs(self):
        jobs = request.env['rl.training.job'].sudo().search_read(
            [('state', 'in', ('completed', 'training', 'failed', 'cancelled'))],
            ['name', 'state', 'model_id', 'config_id', 'current_step', 'total_steps',
             'progress', 'current_loss', 'current_reward', 'best_reward',
             'started_at', 'completed_at', 'elapsed_time'],
            order='create_date desc',
            limit=50
        )
        Metric = request.env['rl.training.metric'].sudo()
        result = []
        for job in jobs:
            job_id = job['id']
            first_m = Metric.search_read([('job_id', '=', job_id)], ['loss', 'reward'], order='step asc', limit=1)
            last_m = Metric.search_read([('job_id', '=', job_id)], ['loss', 'reward'], order='step desc', limit=1)
            duration_label = ''
            if job.get('started_at') and job.get('completed_at'):
                try:
                    delta = job['completed_at'] - job['started_at']
                    secs = delta.total_seconds()
                    hours = int(secs // 3600)
                    mins = int((secs % 3600) // 60)
                    duration_label = f"{hours}h {mins}m" if hours else f"{mins}m"
                except Exception:
                    duration_label = str(job.get('elapsed_time', ''))
            elif job.get('started_at'):
                duration_label = "Running..."
            result.append({
                'id': job['id'],
                'name': job['name'],
                'state': job['state'],
                'model_name': job['model_id'][1] if job.get('model_id') else '',
                'current_step': job['current_step'],
                'total_steps': job['total_steps'],
                'progress': job['progress'],
                'best_reward': round(job['best_reward'], 4) if job.get('best_reward') else 0,
                'duration_label': duration_label,
                'first_loss': round(first_m[0]['loss'], 4) if first_m else None,
                'last_loss': round(last_m[0]['loss'], 4) if last_m else None,
                'first_reward': round(first_m[0]['reward'], 4) if first_m else None,
                'last_reward': round(last_m[0]['reward'], 4) if last_m else None,
            })
        return result

    @http.route('/rl_gym/dashboard/sparkline', type='json', auth='public', cors='*')
    def get_run_sparkline(self, job_id, max_points=50):
        job_id = int(job_id)
        metrics = request.env['rl.training.metric'].sudo().search_read(
            [('job_id', '=', job_id)],
            ['step', 'loss', 'reward'],
            order='step asc'
        )
        if not metrics:
            return {'steps': [], 'loss': [], 'reward': []}
        step_size = max(1, len(metrics) // max_points)
        sampled = metrics[::step_size][:max_points]
        return {
            'steps': [m['step'] for m in sampled],
            'loss': [round(m['loss'], 4) for m in sampled],
            'reward': [round(m['reward'], 4) for m in sampled],
        }
