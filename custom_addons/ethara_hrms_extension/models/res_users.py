from odoo import models


class ResUsers(models.Model):
    _inherit = 'res.users'

    def write(self, vals):
        result = super().write(vals)
        if self.env.context.get('skip_applicant_sync'):
            return result
        if 'name' not in vals and 'phone' not in vals:
            return result
        sync = {}
        if 'name' in vals:
            sync['partner_name'] = vals['name']
        if 'phone' in vals:
            sync['partner_phone'] = vals['phone']
        if not sync:
            return result
        Applicant = self.env['hr.applicant'].sudo()
        for user in self:
            applicants = Applicant.with_context(active_test=False).search(
                [('candidate_user_id', '=', user.id)],
            )
            if applicants:
                applicants.with_context(skip_user_sync=True).write(sync)
        return result
