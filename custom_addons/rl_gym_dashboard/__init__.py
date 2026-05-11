# -*- coding: utf-8 -*-

from . import models
from . import controllers


def seed_demo_data(env):
    from .data.seed_demo_runs import seed_demo_data as _seed
    _seed(env)
    from .models.training_job import start_training_runner
    if env['rl.training.job'].search_count([('state', '=', 'training')]):
        start_training_runner(env.cr.dbname)
