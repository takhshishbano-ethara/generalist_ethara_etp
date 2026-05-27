from . import models
from . import controllers
from . import wizards
from . import log_handler


def post_load():
    log_handler.install_handler()
