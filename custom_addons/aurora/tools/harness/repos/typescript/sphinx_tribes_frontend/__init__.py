import os
from odoo.addons.aurora.tools.harness.instance import Instance
from .base import StakworkSphinxTribesFrontendInstance

# Dynamic registration of stakwork/sphinx-tribes-frontend instances
Instance.register("stakwork", "sphinx-tribes-frontend")(StakworkSphinxTribesFrontendInstance)
