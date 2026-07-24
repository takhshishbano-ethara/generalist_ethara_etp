from odoo import models, fields, api
from odoo.exceptions import UserError


class SeatTransfer(models.Model):
    _name = "workflow.seat.transfer"
    _description = "Seat Transfer Request"
    _order = "create_date desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        string="Reference", readonly=True, default="New", copy=False,
    )
    employee_id = fields.Many2one(
        "hr.employee", string="Employee", required=True, tracking=True,
    )
    from_seat_id = fields.Many2one(
        "workflow.seat", string="From Seat", tracking=True,
    )
    to_seat_id = fields.Many2one(
        "workflow.seat", string="To Seat", required=True, tracking=True,
    )

    requested_by = fields.Many2one(
        "res.users",
        string="Requested By",
        default=lambda self: self.env.user,
        required=True,
    )
    reason = fields.Text(string="Reason")

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Submitted"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("completed", "Completed"),
        ],
        string="Status",
        default="draft",
        required=True,
        tracking=True,
    )

    approval_notes = fields.Text(string="Approval Notes")
    decided_by = fields.Many2one("res.users", string="Decided By", readonly=True)
    decided_at = fields.Datetime(string="Decided At", readonly=True)

    # Computed: role of current user (for view visibility)
    current_user_role = fields.Selection(
        [("user", "User"), ("manager", "Manager"), ("admin", "Admin")],
        compute="_compute_current_user_role",
    )
    is_current_user_manager = fields.Boolean(
        compute="_compute_is_current_user_manager",
    )

    def _compute_current_user_role(self):
        user = self.env.user
        if user._is_admin() or user.has_group("workflow_management.group_admin"):
            role = "admin"
        elif user.has_group("workflow_management.group_manager"):
            role = "manager"
        else:
            role = "user"
        for rec in self:
            rec.current_user_role = role

    def _compute_is_current_user_manager(self):
        user = self.env.user
        is_mgr = user._is_admin() or user.has_group("workflow_management.group_manager")
        for rec in self:
            rec.is_current_user_manager = is_mgr

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "workflow.seat.transfer"
                ) or "New"
            # Auto-fill from_seat from employee's current seat
            if not vals.get("from_seat_id") and vals.get("employee_id"):
                emp = self.env["hr.employee"].browse(vals["employee_id"])
                if emp.current_seat_id:
                    vals["from_seat_id"] = emp.current_seat_id.id
        return super().create(vals_list)

    def action_submit(self):
        """Submit transfer request for approval."""
        for rec in self:
            if rec.state != "draft":
                raise UserError("Only draft requests can be submitted.")
            if rec.to_seat_id.status not in ("available", "reserved"):
                raise UserError(
                    f"Target seat {rec.to_seat_id.seat_code} is not available."
                )
            rec.write({"state": "submitted"})
            self.env["workflow.audit.log"].log_event(
                event="transfer_submitted",
                category="seat_transfer",
                details={
                    "reference": rec.name,
                    "employee": rec.employee_id.name,
                    "to_seat": rec.to_seat_id.seat_code,
                },
            )

    def action_approve(self):
        """Approve transfer request and execute the seat swap."""
        for rec in self:
            if rec.state != "submitted":
                raise UserError("Only submitted requests can be approved.")
            rec.write({
                "state": "approved",
                "decided_by": self.env.user.id,
                "decided_at": fields.Datetime.now(),
            })
            rec._execute_transfer()
            self.env["workflow.audit.log"].log_event(
                event="transfer_approved",
                category="seat_transfer",
                details={
                    "reference": rec.name,
                    "employee": rec.employee_id.name,
                    "from_seat": rec.from_seat_id.seat_code if rec.from_seat_id else "",
                    "to_seat": rec.to_seat_id.seat_code,
                    "approved_by": self.env.user.name,
                },
            )

    def action_reject(self):
        """Reject transfer request."""
        for rec in self:
            if rec.state != "submitted":
                raise UserError("Only submitted requests can be rejected.")
            rec.write({
                "state": "rejected",
                "decided_by": self.env.user.id,
                "decided_at": fields.Datetime.now(),
            })
            self.env["workflow.audit.log"].log_event(
                event="transfer_rejected",
                category="seat_transfer",
                details={
                    "reference": rec.name,
                    "employee": rec.employee_id.name,
                    "rejected_by": self.env.user.name,
                },
            )

    def action_reset_to_draft(self):
        """Reset rejected request back to draft for re-submission."""
        for rec in self:
            if rec.state != "rejected":
                raise UserError("Only rejected requests can be reset to draft.")
            rec.write({
                "state": "draft",
                "decided_by": False,
                "decided_at": False,
                "approval_notes": False,
            })

    def _execute_transfer(self):
        """Execute the actual seat transfer after approval."""
        self.ensure_one()
        # Release old seat
        if self.from_seat_id:
            self.from_seat_id.action_release()

        # Assign new seat
        self.to_seat_id.action_assign(
            self.employee_id,
            notes=f"Transfer {self.name}: {self.reason or ''}",
        )

        # Create transfer history entry
        self.env["workflow.seat.history"].create({
            "seat_id": self.to_seat_id.id,
            "employee_id": self.employee_id.id,
            "floor_id": self.to_seat_id.floor_id.id,
            "action": "transfer",
            "notes": f"Transfer {self.name} from {self.from_seat_id.seat_code if self.from_seat_id else 'N/A'}",
        })

        self.write({"state": "completed"})
