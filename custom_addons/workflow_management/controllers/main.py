import json
from odoo import http
from odoo.http import request


class WorkflowManagementAPI(http.Controller):
    """REST API endpoints for Workflow Management module.

    All endpoints use type='json' for automatic JSON serialization.
    Auth: 'user' requires login, 'public' is open.
    """

    # ─── Floors ────────────────────────────────────────────────────────

    @http.route("/api/workflow/floors", type="jsonrpc", auth="user", methods=["POST"])
    def list_floors(self, **kw):
        """List all floors with seat counts."""
        floors = request.env["workflow.floor"].search([])
        return {
            "status": "success",
            "data": [
                {
                    "id": f.id,
                    "floor_code": f.floor_code,
                    "name": f.name,
                    "total_seats": f.total_seats,
                    "occupied_seats": f.occupied_seats,
                    "available_seats": f.available_seats,
                    "reserved_seats": f.reserved_seats,
                }
                for f in floors
            ],
        }

    # ─── Seats ─────────────────────────────────────────────────────────

    @http.route("/api/workflow/seats", type="jsonrpc", auth="user", methods=["POST"])
    def list_seats(self, floor_id=None, status=None, **kw):
        """List seats, optionally filtered by floor or status."""
        domain = []
        if floor_id:
            domain.append(("floor_id", "=", int(floor_id)))
        if status:
            domain.append(("status", "=", status))

        seats = request.env["workflow.seat"].search(domain)
        return {
            "status": "success",
            "data": [
                {
                    "id": s.id,
                    "seat_code": s.seat_code,
                    "floor_id": s.floor_id.id,
                    "floor_name": s.floor_id.name,
                    "zone": s.zone or "",
                    "row": s.row,
                    "number": s.number,
                    "status": s.status,
                    "position_x": s.position_x,
                    "position_y": s.position_y,
                    "employee_id": s.employee_id.id if s.employee_id else None,
                    "employee_name": s.employee_id.name if s.employee_id else "",
                }
                for s in seats
            ],
        }

    @http.route("/api/workflow/seats/assign", type="jsonrpc", auth="user", methods=["POST"])
    def assign_seat(self, seat_id, employee_id, **kw):
        """Assign a seat to an employee.

        Args:
            seat_id: ID of the workflow.seat record
            employee_id: ID of the hr.employee record
        """
        seat = request.env["workflow.seat"].browse(int(seat_id))
        if not seat.exists():
            return {"status": "error", "message": "Seat not found."}

        employee = request.env["hr.employee"].browse(int(employee_id))
        if not employee.exists():
            return {"status": "error", "message": "Employee not found."}

        try:
            seat.action_assign(employee)
            return {
                "status": "success",
                "message": f"Seat {seat.seat_code} assigned to {employee.name}.",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @http.route("/api/workflow/seats/release", type="jsonrpc", auth="user", methods=["POST"])
    def release_seat(self, seat_id, **kw):
        """Release a seat from its current occupant."""
        seat = request.env["workflow.seat"].browse(int(seat_id))
        if not seat.exists():
            return {"status": "error", "message": "Seat not found."}

        try:
            seat.action_release()
            return {
                "status": "success",
                "message": f"Seat {seat.seat_code} released.",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ─── Employees ─────────────────────────────────────────────────────

    @http.route("/api/workflow/employees", type="jsonrpc", auth="user", methods=["POST"])
    def list_employees(self, department=None, project_category=None, **kw):
        """List employees with workflow-specific fields."""
        domain = []
        if department:
            domain.append(("department_id.name", "ilike", department))
        if project_category:
            domain.append(("project_category", "=", project_category))

        employees = request.env["hr.employee"].search(domain)
        return {
            "status": "success",
            "data": [
                {
                    "id": emp.id,
                    "name": emp.name,
                    "email": emp.work_email or "",
                    "department": emp.department_id.name if emp.department_id else "",
                    "job_title": emp.job_title or "",
                    "project_category": emp.project_category or "",
                    "current_project": emp.current_project or "",
                    "platform": emp.platform or "",
                    "tasking_status": emp.tasking_status or "",
                    "allocation_status": emp.allocation_status or "",
                    "current_seat": emp.current_seat_id.seat_code if emp.current_seat_id else "",
                    "main_location": emp.main_location or "",
                    "current_location": emp.current_location or "",
                }
                for emp in employees
            ],
        }

    # ─── Room Bookings ─────────────────────────────────────────────────

    @http.route("/api/workflow/bookings", type="jsonrpc", auth="user", methods=["POST"])
    def list_bookings(self, room_code=None, status=None, **kw):
        """List room bookings, optionally filtered."""
        domain = []
        if room_code:
            domain.append(("room_code", "=", room_code))
        if status:
            domain.append(("status", "=", status))

        bookings = request.env["workflow.room.booking"].search(domain)
        return {
            "status": "success",
            "data": [
                {
                    "id": b.id,
                    "room_code": b.room_code,
                    "name": b.name,
                    "floor": b.floor_id.name if b.floor_id else "",
                    "organizer": b.organizer_id.name,
                    "organizer_email": b.organizer_email or "",
                    "reason": b.reason or "",
                    "booking_start": str(b.booking_start) if b.booking_start else "",
                    "booking_end": str(b.booking_end) if b.booking_end else "",
                    "duration_minutes": b.duration_minutes,
                    "status": b.status,
                }
                for b in bookings
            ],
        }

    @http.route("/api/workflow/bookings/create", type="jsonrpc", auth="user", methods=["POST"])
    def create_booking(self, room_code, name, booking_start, booking_end,
                       reason=None, floor_id=None, **kw):
        """Create a new room booking."""
        try:
            vals = {
                "room_code": room_code,
                "name": name,
                "booking_start": booking_start,
                "booking_end": booking_end,
                "reason": reason or "",
            }
            if floor_id:
                vals["floor_id"] = int(floor_id)

            booking = request.env["workflow.room.booking"].create(vals)
            return {
                "status": "success",
                "message": f"Booking {booking.room_code} created.",
                "booking_id": booking.id,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @http.route("/api/workflow/bookings/cancel", type="jsonrpc", auth="user", methods=["POST"])
    def cancel_booking(self, booking_id, **kw):
        """Cancel a room booking."""
        booking = request.env["workflow.room.booking"].browse(int(booking_id))
        if not booking.exists():
            return {"status": "error", "message": "Booking not found."}
        try:
            booking.action_cancel()
            return {"status": "success", "message": "Booking cancelled."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @http.route("/api/workflow/bookings/active", type="jsonrpc", auth="user", methods=["POST"])
    def active_bookings(self, room_code=None, **kw):
        """Get currently active (confirmed) bookings."""
        from datetime import datetime
        now = datetime.now()
        domain = [
            ("status", "=", "confirmed"),
            ("booking_start", "<=", now),
            ("booking_end", ">=", now),
        ]
        if room_code:
            domain.append(("room_code", "=", room_code))

        bookings = request.env["workflow.room.booking"].search(domain)
        return {
            "status": "success",
            "data": [
                {
                    "id": b.id,
                    "room_code": b.room_code,
                    "name": b.name,
                    "organizer": b.organizer_id.name,
                    "reason": b.reason or "",
                    "booking_end": str(b.booking_end),
                }
                for b in bookings
            ],
        }

    # ─── Seat Transfers ────────────────────────────────────────────────

    @http.route("/api/workflow/transfers", type="jsonrpc", auth="user", methods=["POST"])
    def list_transfers(self, state=None, **kw):
        """List seat transfer requests."""
        domain = []
        if state:
            domain.append(("state", "=", state))

        transfers = request.env["workflow.seat.transfer"].search(domain)
        return {
            "status": "success",
            "data": [
                {
                    "id": t.id,
                    "name": t.name,
                    "employee": t.employee_id.name,
                    "from_seat": t.from_seat_id.seat_code if t.from_seat_id else "",
                    "to_seat": t.to_seat_id.seat_code,
                    "requested_by": t.requested_by.name,
                    "reason": t.reason or "",
                    "state": t.state,
                    "created_at": str(t.create_date),
                    "decided_by": t.decided_by.name if t.decided_by else "",
                    "decided_at": str(t.decided_at) if t.decided_at else "",
                }
                for t in transfers
            ],
        }

    @http.route("/api/workflow/transfers/create", type="jsonrpc", auth="user", methods=["POST"])
    def create_transfer(self, employee_id, to_seat_id, reason=None, **kw):
        """Create a new seat transfer request."""
        try:
            transfer = request.env["workflow.seat.transfer"].create({
                "employee_id": int(employee_id),
                "to_seat_id": int(to_seat_id),
                "reason": reason or "",
            })
            return {
                "status": "success",
                "message": f"Transfer request {transfer.name} created.",
                "transfer_id": transfer.id,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @http.route("/api/workflow/transfers/submit", type="jsonrpc", auth="user", methods=["POST"])
    def submit_transfer(self, transfer_id, **kw):
        """Submit a transfer request for approval."""
        transfer = request.env["workflow.seat.transfer"].browse(int(transfer_id))
        if not transfer.exists():
            return {"status": "error", "message": "Transfer not found."}
        try:
            transfer.action_submit()
            return {"status": "success", "message": "Transfer submitted for approval."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @http.route("/api/workflow/transfers/approve", type="jsonrpc", auth="user", methods=["POST"])
    def approve_transfer(self, transfer_id, notes=None, **kw):
        """Approve a seat transfer (manager/admin only)."""
        transfer = request.env["workflow.seat.transfer"].browse(int(transfer_id))
        if not transfer.exists():
            return {"status": "error", "message": "Transfer not found."}
        try:
            if notes:
                transfer.write({"approval_notes": notes})
            transfer.action_approve()
            return {"status": "success", "message": f"Transfer {transfer.name} approved and executed."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @http.route("/api/workflow/transfers/reject", type="jsonrpc", auth="user", methods=["POST"])
    def reject_transfer(self, transfer_id, notes=None, **kw):
        """Reject a seat transfer (manager/admin only)."""
        transfer = request.env["workflow.seat.transfer"].browse(int(transfer_id))
        if not transfer.exists():
            return {"status": "error", "message": "Transfer not found."}
        try:
            if notes:
                transfer.write({"approval_notes": notes})
            transfer.action_reject()
            return {"status": "success", "message": f"Transfer {transfer.name} rejected."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @http.route("/api/workflow/transfers/pending-count", type="jsonrpc", auth="user", methods=["POST"])
    def pending_transfer_count(self, **kw):
        """Get count of pending transfer requests."""
        count = request.env["workflow.seat.transfer"].search_count(
            [("state", "=", "submitted")]
        )
        return {"status": "success", "count": count}

    # ─── Audit Logs ────────────────────────────────────────────────────

    @http.route("/api/workflow/audit-logs", type="jsonrpc", auth="user", methods=["POST"])
    def list_audit_logs(self, category=None, limit=50, **kw):
        """List recent audit logs."""
        domain = []
        if category:
            domain.append(("category", "=", category))

        logs = request.env["workflow.audit.log"].sudo().search(
            domain, limit=int(limit), order="create_date desc",
        )
        return {
            "status": "success",
            "data": [
                {
                    "id": log.id,
                    "event": log.event,
                    "category": log.category,
                    "status": log.status,
                    "user_name": log.user_name or "",
                    "details": json.loads(log.details_json) if log.details_json else {},
                    "timestamp": str(log.create_date),
                }
                for log in logs
            ],
        }

    # ─── Public Endpoints (no auth) ───────────────────────────────────

    @http.route("/api/workflow/public/floors", type="jsonrpc", auth="public",
                methods=["POST"], csrf=False)
    def public_floors(self, **kw):
        """Public: list floors with seat counts."""
        floors = request.env["workflow.floor"].sudo().search([])
        return {
            "status": "success",
            "data": [
                {
                    "floor_code": f.floor_code,
                    "name": f.name,
                    "total_seats": f.total_seats,
                    "occupied_seats": f.occupied_seats,
                    "available_seats": f.available_seats,
                }
                for f in floors
            ],
        }

    @http.route("/api/workflow/public/seats/<string:floor_code>", type="jsonrpc",
                auth="public", methods=["POST"], csrf=False)
    def public_seats(self, floor_code, **kw):
        """Public: list seats for a floor (no employee contact details)."""
        floor = request.env["workflow.floor"].sudo().search(
            [("floor_code", "=", floor_code)], limit=1,
        )
        if not floor:
            return {"status": "error", "message": "Floor not found."}

        seats = request.env["workflow.seat"].sudo().search(
            [("floor_id", "=", floor.id)]
        )
        return {
            "status": "success",
            "data": [
                {
                    "seat_code": s.seat_code,
                    "zone": s.zone or "",
                    "row": s.row,
                    "number": s.number,
                    "status": s.status,
                    "position_x": s.position_x,
                    "position_y": s.position_y,
                    "employee_name": s.employee_id.name if s.employee_id else "",
                }
                for s in seats
            ],
        }

    @http.route("/api/workflow/public/bookings/active", type="jsonrpc",
                auth="public", methods=["POST"], csrf=False)
    def public_active_bookings(self, **kw):
        """Public: list currently active room bookings."""
        from datetime import datetime
        now = datetime.now()
        bookings = request.env["workflow.room.booking"].sudo().search([
            ("status", "=", "confirmed"),
            ("booking_start", "<=", now),
            ("booking_end", ">=", now),
        ])
        return {
            "status": "success",
            "data": [
                {
                    "room_code": b.room_code,
                    "name": b.name,
                    "organizer": b.organizer_id.name,
                    "booking_end": str(b.booking_end),
                }
                for b in bookings
            ],
        }
