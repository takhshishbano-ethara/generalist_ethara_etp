from odoo import fields, models


class VegetaCategory(models.Model):
    _name = "vegeta.category"
    _description = "Vegeta Website Category (buildable-PRD vertical)"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    technical_key = fields.Char(
        string="Technical Key",
        required=True,
        help="snake_case key used by scoring/QC services (e.g. retail, gov_portal, ai_platform).",
    )
    active = fields.Boolean(default=True)

    gated = fields.Boolean(
        string="Login-gated SaaS",
        default=False,
        help="Login-gated verticals (CRM, ERP, HCM, TMS, Procurement, AI Platform) "
             "default scrape_coverage to marketing_only. PRD sections 3-7, 9, 10 are "
             "predominantly Tier-3 inference from the named reference brands.",
    )
    core_entities = fields.Text(
        string="Core Entities",
        help="Comma-separated list of canonical entities for this category. "
             "Drives PRD Section 6 (Data Model).",
    )
    core_flows = fields.Text(
        string="Core Flows",
        help="Comma-separated list of signature user flows. "
             "Drives PRD Section 5 (Core Features & User Flows).",
    )
    defining_mechanic = fields.Text(
        string="Defining Mechanic",
        help="The mechanic the PRD must carry for this category to be unmistakable. "
             "Drives PRD Section 11 (Category-Specific Guidelines).",
    )
    signature_routes = fields.Char(
        string="Signature Routes",
        help="Comma-separated route prefixes characteristic of this category.",
    )
    reference_brands = fields.Char(
        string="Reference Brands",
        help="Comma-separated list of canonical brands. Used only in the "
             "PRD header 'Reference Style' field, never in the PRD body.",
    )
    must_include_bullets = fields.Text(
        string="Must-Include Bullets (Section 11)",
        help="Newline-separated bullets the category PRD's Section 11 must include.",
    )
