from odoo import models, fields


class ResourceCalendarLeaves(models.Model):
    """Time Off → Configuration → Public Holidays.

    Adds an optional classification so a public holiday can be marked as
    gazetted (mandatory) or restricted (optional). This is the single source
    of truth consumed by the Wiki Holidays page; the Wiki module keeps no
    holiday records of its own.
    """
    _inherit = 'resource.calendar.leaves'

    holiday_classification = fields.Selection(
        [('gazetted', 'Gazetted'), ('restricted', 'Restricted')],
        string='Holiday Type', default='gazetted',
        help='Gazetted holidays are mandatory; restricted holidays are '
             'optional. Surfaced on the Employee Portal Wiki Holidays page.')
