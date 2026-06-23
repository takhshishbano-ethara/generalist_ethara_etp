from odoo import models, api, _
from odoo.exceptions import ValidationError


class HrLeaveAllocation(models.Model):
    _inherit = 'hr.leave.allocation'

    @api.constrains('number_of_days', 'state', 'holiday_status_id', 'employee_id')
    def _check_ethara_allocation_limits(self):
        """Validate allocations of Ethara leave types:

        - the leave type must have an entitlement (default_annual_days > 0);
        - total allocated for an employee may not exceed that entitlement;
        - total allocated may not drop below what the employee has already taken.
        Non-Ethara (Odoo default) leave types are left untouched.
        """
        Allocation = self.env['hr.leave.allocation'].sudo()
        Leave = self.env['hr.leave'].sudo()
        for alloc in self:
            leave_type = alloc.holiday_status_id
            if not leave_type.ethara_leave_code:
                continue  # only the 11 Ethara types are governed
            if alloc.allocation_type == 'regular' and alloc.number_of_days < 0:
                raise ValidationError(
                    _("Leave days cannot be negative."))
            if alloc.state != 'validate':
                continue

            entitlement = leave_type.default_annual_days or 0.0
            if entitlement <= 0:
                raise ValidationError(_(
                    "Cannot allocate '%s' to %s: no entitlement is set for this "
                    "leave type yet. Set 'Default Annual Days' (greater than 0) on "
                    "the leave type first, then allocate up to that limit."
                ) % (leave_type.name, alloc.employee_id.name))

            total = sum(Allocation.search([
                ('employee_id', '=', alloc.employee_id.id),
                ('holiday_status_id', '=', leave_type.id),
                ('state', '=', 'validate'),
            ]).mapped('number_of_days'))

            if total > entitlement + 0.01:
                already = total - alloc.number_of_days
                raise ValidationError(_(
                    "Total '%s' for %s would be %.1f day(s), exceeding the %.1f-day "
                    "entitlement. You can give at most %.1f more."
                ) % (leave_type.name, alloc.employee_id.name, total, entitlement,
                     max(entitlement - already, 0.0)))

            taken = sum(Leave.search([
                ('employee_id', '=', alloc.employee_id.id),
                ('holiday_status_id', '=', leave_type.id),
                ('state', '=', 'validate'),
            ]).mapped('number_of_days'))
            if total < taken - 0.01:
                raise ValidationError(_(
                    "Cannot reduce '%s' for %s to %.1f day(s): %.1f day(s) have "
                    "already been taken."
                ) % (leave_type.name, alloc.employee_id.name, total, taken))
