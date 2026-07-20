from . import res_config_settings
from . import erza_model
from . import erza_task
from . import erza_run


def _erza_post_init(env):
    env["erza.run"].seed_from_bundle()
