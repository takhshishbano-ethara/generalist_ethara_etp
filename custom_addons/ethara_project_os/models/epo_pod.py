"""Pods — the seating / supervision grouping.

A pod is a GROUPING, not the allocation unit. People are allocated to projects
individually (``epo.allocation``); a whole pod is never "on" a project. Modelling the
pod as a record rather than a text label on the employee removes the rename drift the
prototype had (``employees.pod_name``), but nothing downstream is ever allowed to
*require* a pod — a new joiner with no seat yet is a real thing.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EpoPod(models.Model):
    _name = 'epo.pod'
    _description = 'Project OS Pod'
    _inherit = ['mail.thread']
    _order = 'code'

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, tracking=True, help='Short unique handle, e.g. POD-07.')
    floor = fields.Char()
    zone = fields.Char()
    seat_range = fields.Char(help='Free text, e.g. "A12-A30".')
    capacity = fields.Integer(help='Seats available. Used to warn, never to block.')

    work_location_id = fields.Many2one('hr.work.location', string='Work Location')
    lead_employee_id = fields.Many2one(
        'hr.employee', string='Pod Lead', tracking=True,
        help='The PL accountable for this pod\'s daily roster.')

    member_ids = fields.One2many('hr.employee', 'epo_pod_id', string='Members')
    member_count = fields.Integer(compute='_compute_member_count', store=True)
    seats_free = fields.Integer(compute='_compute_member_count', store=True)

    active = fields.Boolean(default=True)

    _code_uniq_live = models.UniqueIndex(
        '(lower(code)) WHERE active',
        'Another live pod already uses that code.')
    _capacity_sane = models.Constraint(
        'CHECK (capacity >= 0 AND capacity <= 500)',
        'Pod capacity must be between 0 and 500.')

    @api.depends('member_ids', 'member_ids.active', 'capacity')
    def _compute_member_count(self):
        for pod in self:
            n = len(pod.member_ids.filtered('active'))
            pod.member_count = n
            pod.seats_free = (pod.capacity - n) if pod.capacity else 0

    @api.constrains('lead_employee_id')
    def _check_lead_pod(self):
        """A pod lead seated in a different pod is a reporting accident, not a plan."""
        for pod in self:
            lead = pod.lead_employee_id
            if lead and lead.epo_pod_id and lead.epo_pod_id != pod:
                raise ValidationError(_(
                    '%(lead)s belongs to pod %(other)s and cannot lead %(this)s.',
                    lead=lead.display_name, other=lead.epo_pod_id.display_name,
                    this=pod.display_name))

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for pod in self:
            pod.display_name = f'{pod.code} · {pod.name}' if pod.code else (pod.name or '')
