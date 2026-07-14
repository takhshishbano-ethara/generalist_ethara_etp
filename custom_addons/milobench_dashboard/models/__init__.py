from . import res_config_settings
from . import milobench_model
from . import milobench_task
from . import milobench_run


def _milobench_post_init(env):
    env["milobench.run"].seed_from_bundle()
