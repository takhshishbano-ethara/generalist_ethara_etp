from . import models
from . import controllers


def uninstall_hook(env):
    env.cr.execute(
        "DELETE FROM ir_config_parameter WHERE key LIKE %s",
        ("aurora.%",),
    )
