from . import erza_model
from . import erza_task
from . import erza_run


def _erza_post_init(env):
    env["erza.bench.run"].seed_from_bundle()
