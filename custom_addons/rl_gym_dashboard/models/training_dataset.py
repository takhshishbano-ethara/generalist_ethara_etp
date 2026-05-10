# -*- coding: utf-8 -*-

from odoo import models, fields


class RlTrainingDataset(models.Model):
    _name = 'rl.training.dataset'
    _description = 'RL Training Dataset'
    _order = 'name'

    name = fields.Char(string='Dataset Name', required=True)
    hf_repo_id = fields.Char(string='HuggingFace Repo ID', required=True,
                             help='e.g. ethara/my-dataset')
    description = fields.Text(string='Description')

    # Dataset metadata
    dataset_type = fields.Selection([
        ('instruction', 'Instruction Following'),
        ('code', 'Code Generation'),
        ('math', 'Mathematical Reasoning'),
        ('dialogue', 'Dialogue'),
        ('reward', 'Reward Modeling'),
        ('mixed', 'Mixed'),
    ], string='Dataset Type', default='code')

    split = fields.Selection([
        ('train', 'Train'),
        ('validation', 'Validation'),
        ('test', 'Test'),
    ], string='Split', default='train')

    # Size info
    num_rows = fields.Integer(string='Number of Rows')
    size_bytes = fields.Float(string='Size (MB)')
    num_columns = fields.Integer(string='Number of Columns')
    columns_info = fields.Text(string='Column Schema (JSON)')

    # Training split ratio
    train_ratio = fields.Float(string='Train Ratio', default=0.8,
                               digits=(4, 2))
    val_ratio = fields.Float(string='Validation Ratio', default=0.1,
                             digits=(4, 2))
    test_ratio = fields.Float(string='Test Ratio', default=0.1,
                              digits=(4, 2))

    # Filtering
    difficulty_filter = fields.Selection([
        ('all', 'All Difficulties'),
        ('easy', 'Easy Only'),
        ('medium', 'Medium Only'),
        ('hard', 'Hard Only'),
        ('progressive', 'Progressive (Curriculum)'),
    ], string='Difficulty Filter', default='all')

    language_filter = fields.Char(string='Language Filter',
                                  help='Comma-separated language codes')

    # Status
    is_loaded = fields.Boolean(string='Loaded', default=False)
    preview_data = fields.Text(string='Preview Data (JSON)',
                               help='First few rows for preview')
    active = fields.Boolean(default=True)
