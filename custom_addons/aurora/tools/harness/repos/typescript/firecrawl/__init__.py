import os
from odoo.addons.aurora.tools.harness.instance import Instance
from .base import FirecrawlFirecrawlInstance

# Dynamic registration of firecrawl/firecrawl instances
Instance.register("firecrawl", "firecrawl")(FirecrawlFirecrawlInstance)
