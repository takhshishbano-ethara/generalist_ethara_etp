import os
from odoo.addons.aurora.tools.harness.instance import Instance
from .base import ThenewbostonWebsiteInstance

# Dynamic registration of thenewboston-blockchain/Website instances
Instance.register("thenewboston-blockchain", "Website")(ThenewbostonWebsiteInstance)
