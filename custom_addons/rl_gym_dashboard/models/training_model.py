# -*- coding: utf-8 -*-

from odoo import models, fields


class RlTrainingModel(models.Model):
    _name = 'rl.training.model'
    _description = 'RL Training Model'
    _order = 'sequence, name'

    name = fields.Char(string='Model Name', required=True)
    technical_name = fields.Char(string='Technical Name', required=True,
                                 help='HuggingFace model identifier or local path')
    description = fields.Text(string='Description')
    model_type = fields.Selection([
        ('causal_lm', 'Causal Language Model'),
        ('seq2seq', 'Sequence-to-Sequence'),
        ('reward_model', 'Reward Model'),
        ('custom', 'Custom Architecture'),
    ], string='Model Type', default='causal_lm', required=True)
    default_config_ids = fields.One2many('rl.training.config', 'model_id',
                                         string='Default Configurations')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    # Model metadata
    parameter_count = fields.Char(string='Parameters')
    architecture = fields.Char(string='Architecture')
    base_model = fields.Char(string='Base Model',
                             help='Parent model if fine-tuned')
    recommended_policy = fields.Selection([
        ('gspo', 'GSPO'),
        ('gtpo', 'GTPO'),
    ], string='Recommended Policy', default='gspo')
