# -*- coding: utf-8 -*-
from . import models
from . import controllers
from . import services
from . import wizard

from .hooks import post_init_hook  # noqa: F401 — referenced by __manifest__.py
