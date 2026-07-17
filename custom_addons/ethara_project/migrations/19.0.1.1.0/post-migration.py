"""Backfill provider_id on existing AWS-managed infrastructure types.

Introduced with the multi-provider infrastructure feature. All pre-existing
`ethara.project.infra.type` rows were implicitly AWS (flagged via
`is_aws_managed`); link them to the seeded AWS provider so the new Provider
column / filter is correct. Runs once, on the upgrade that ships version
19.0.1.1.0. New AWS types synced afterwards are stamped in
`aws_pricing_service._upsert_service`.
"""

from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    aws = env.ref('ethara_project.infra_provider_aws', raise_if_not_found=False)
    if not aws:
        return
    types = env['ethara.project.infra.type'].with_context(active_test=False).search([
        ('is_aws_managed', '=', True),
        ('provider_id', '=', False),
    ])
    if types:
        types.write({'provider_id': aws.id})
