"""Settings — the few knobs that must not be hard-coded.

The assessment needs nothing here: v2 §4.6.2 makes it a plain link-out, so the URL lives
on the project's own ``ethara.assessment`` record and Odoo never calls anybody.
"""

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'


    epo_s3_connector_id = fields.Many2one(
        's3.connector', string='Document bucket',
        config_parameter='epo.s3.connector_id',
        help='Where project files are stored. Leave empty to keep them in Odoo — the '
             'module works either way, and a misconfigured bucket falls back rather '
             'than losing an upload.')
    epo_s3_url_ttl = fields.Integer(
        string='Download link lifetime (seconds)', default=300,
        config_parameter='epo.s3.url_ttl',
        help='Files are never public. Every download mints a presigned link that '
             'expires after this long.')

    epo_roster_carry_forward = fields.Boolean(
        string='Carry the roster forward nightly', default=True,
        config_parameter='epo.roster.carry_forward',
        help='Clones yesterday\'s roster into today for anyone without a row. Without '
             'it the board is empty every morning until somebody fills it in by hand.')
    epo_payroll_lock_days = fields.Integer(
        string='Lock roster after (days)', default=7,
        config_parameter='epo.roster.lock_days',
        help='Roster days older than this are locked against retro-editing. Only an '
             'Admin can unlock one, with a reason.')
    epo_max_allocation_pct = fields.Integer(
        string='Maximum total allocation %', default=100,
        config_parameter='epo.allocation.max_pct',
        help='Ceiling on a person\'s combined allocation across concurrent projects.')
