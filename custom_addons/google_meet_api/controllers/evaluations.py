import functools
import json
import logging
from datetime import datetime, timedelta, timezone

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

EVAL_BASE = "/api/v1/evaluations"
PMS_BASE = "/api/v1/pms-evaluations"
AUDIT_BASE = "/api/v1/audit-logs"
CAND_BASE = "/api/v1/candidates"

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _read_body():
    raw = request.httprequest.get_data()
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None, return_Response(
            message="Malformed JSON body.", status=400, errors=["Malformed JSON body."],
        )
    if not isinstance(data, dict):
        return None, return_Response(
            message="JSON body must be an object.", status=400,
            errors=["JSON body must be an object."],
        )
    return data, None


def _parse_dt(raw, label, required=True):
    if raw in (None, "", False):
        if required:
            return None, return_Response(
                message=f"{label} is required.", status=400,
                errors=[f"{label} is required."],
            )
        return None, None
    text = str(raw).strip()
    try:
        if "T" in text:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            value = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'.", status=400,
            errors=[f"Invalid {label} '{raw}'."],
        )
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value, None


def _handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AccessError as exc:
            request.env.cr.rollback()
            return return_Response(message=str(exc), status=403, errors=[str(exc)])
        except (ValidationError, UserError) as exc:
            request.env.cr.rollback()
            return return_Response(message=str(exc), status=400, errors=[str(exc)])
        except Exception:  # noqa: BLE001
            request.env.cr.rollback()
            _logger.exception("evaluations api: unhandled error in %s", func.__name__)
            return return_Response(
                message="Internal server error.", status=500,
                errors=["Internal server error."],
            )
    return wrapper


def _paginate(params):
    page = max(1, _int(params.get("page"), 1))
    limit = min(max(1, _int(params.get("limit"), DEFAULT_LIMIT)), MAX_LIMIT)
    return page, limit, (page - 1) * limit


def _pagination_block(total, page, limit):
    total_pages = (total + limit - 1) // limit if total else 0
    return {"total_records": total, "page": page, "limit": limit, "total_pages": total_pages}


def _iso(value):
    return value.isoformat() if value else None


def _audit(entity_type, entity_id, action, candidate_id=None, old=None, new=None):
    user = request.env.user
    request.env["hr.applicant.audit.log"].sudo().create({
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "performed_by_id": user.id,
        "performed_by_name": user.name,
        "performed_by_role": (user.user_role.name if getattr(user, "user_role", False) else None),
        "candidate_id": candidate_id,
        "old_value": json.dumps(old, default=str) if old else None,
        "new_value": json.dumps(new, default=str) if new else None,
        "ip_address": request.httprequest.remote_addr,
        "user_agent": request.httprequest.user_agent.string if request.httprequest.user_agent else None,
    })


def _eval_dict(rec):
    return {
        "id": rec.id,
        "candidate_id": rec.candidate_id.id if rec.candidate_id else None,
        "candidate_name": (
            rec.candidate_id.partner_name or rec.candidate_id.name
            if rec.candidate_id else None
        ),
        "evaluator_id": rec.evaluator_id.id if rec.evaluator_id else None,
        "evaluator_name": rec.evaluator_id.name if rec.evaluator_id else None,
        "job": (
            {"id": rec.job_id.id, "name": rec.job_id.name}
            if rec.job_id else None
        ),
        "pi_score": rec.pi_score or 0,
        "pms_score": rec.pms_score or 0,
        "recommendation": rec.recommendation or None,
        "final_verdict": rec.final_verdict or None,
        "status": rec.status,
        "notes": rec.notes or None,
        "bypass_reason": rec.bypass_reason or None,
        "completed_at": _iso(rec.completed_at),
        "rounds_count": rec.rounds_count or 0,
        "rounds_completed": rec.rounds_completed or 0,
        "create_date": _iso(rec.create_date),
        "write_date": _iso(rec.write_date),
    }


def _pms_dict(rec):
    return {
        "id": rec.id,
        "candidate_id": rec.candidate_id.id if rec.candidate_id else None,
        "employee_id": rec.employee_id.id,
        "employee_name": rec.employee_id.name,
        "evaluator_id": rec.evaluator_id.id if rec.evaluator_id else None,
        "metrics": {f: getattr(rec, f) or 0 for f in rec.METRIC_FIELDS},
        "metric_remarks": rec.metric_remarks or None,
        "total_score": rec.total_score or 0,
        "average_score": rec.average_score or 0,
        "overall_rating": rec.overall_rating or None,
        "remarks": rec.remarks or None,
        "submitted_at": _iso(rec.submitted_at),
        "create_date": _iso(rec.create_date),
    }


def _audit_dict(rec):
    return {
        "id": rec.id,
        "entity_type": rec.entity_type,
        "entity_id": rec.entity_id,
        "action": rec.action,
        "performed_by": {
            "id": rec.performed_by_id.id if rec.performed_by_id else None,
            "name": rec.performed_by_name or None,
            "role": rec.performed_by_role or None,
        },
        "candidate_id": rec.candidate_id.id if rec.candidate_id else None,
        "old_value": json.loads(rec.old_value) if rec.old_value else None,
        "new_value": json.loads(rec.new_value) if rec.new_value else None,
        "ip_address": rec.ip_address or None,
        "user_agent": rec.user_agent or None,
        "created_at": _iso(rec.create_date),
    }


class EvaluationsApi(http.Controller):

    @http.route(EVAL_BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @_handle_errors
    def evaluations_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)

        domain = []
        cid = _int(params.get("candidate_id"))
        if cid:
            domain.append(("candidate_id", "=", cid))
        stat = params.get("status")
        if stat:
            domain.append(("status", "=", stat))
        verdict = params.get("final_verdict")
        if verdict:
            domain.append(("final_verdict", "=", verdict))

        Eval = request.env["hr.applicant.evaluation"].sudo()
        total = Eval.search_count(domain)
        records = Eval.search(domain, offset=offset, limit=limit, order="create_date desc")

        return return_Response(
            message="OK", status=200,
            data={
                "evaluations": [_eval_dict(r) for r in records],
                "pagination": _pagination_block(total, page, limit),
            },
        )

    @http.route(EVAL_BASE, type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def evaluation_assign(self, **kwargs):
        data, err = _read_body()
        if err is not None:
            return err

        cid = _int(data.get("candidateId"))
        if not cid:
            return return_Response(message="candidateId is required.", status=400, errors=["candidateId is required."])
        candidate = request.env["hr.applicant"].sudo().browse(cid).exists()
        if not candidate:
            return return_Response(message=f"Candidate id={cid} not found.", status=404)

        Eval = request.env["hr.applicant.evaluation"].sudo()
        existing = Eval.search([("candidate_id", "=", cid)], limit=1)
        if existing:
            return return_Response(
                message="Evaluation already exists for this candidate.",
                status=200, data={"evaluation": _eval_dict(existing)},
            )

        vals = {"candidate_id": cid, "status": "assigned"}
        evaluator_id = _int(data.get("evaluatorId"))
        if evaluator_id:
            vals["evaluator_id"] = evaluator_id

        rec = Eval.create(vals)
        _audit("evaluation", rec.id, "assigned", candidate_id=cid, new=vals)
        return return_Response(
            message="Evaluation assigned.", status=200,
            data={"evaluation": _eval_dict(rec)},
        )

    @http.route(EVAL_BASE + "/<int:eid>", type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @_handle_errors
    def evaluation_detail(self, eid, **kwargs):
        rec = request.env["hr.applicant.evaluation"].sudo().browse(eid).exists()
        if not rec:
            return return_Response(message="Evaluation not found.", status=404)
        return return_Response(message="OK", status=200, data={"evaluation": _eval_dict(rec)})

    @http.route(EVAL_BASE + "/<int:eid>/submit", type="http", auth="none", methods=["PUT", "PATCH"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def evaluation_submit(self, eid, **kwargs):
        rec = request.env["hr.applicant.evaluation"].sudo().browse(eid).exists()
        if not rec:
            return return_Response(message="Evaluation not found.", status=404)
        data, err = _read_body()
        if err is not None:
            return err

        vals = {}
        if "recommendation" in data:
            vals["recommendation"] = data.get("recommendation")
        if "notes" in data:
            vals["notes"] = data.get("notes")
        vals["status"] = "submitted"

        old = {"status": rec.status}
        rec.write(vals)
        _audit("evaluation", rec.id, "submitted", candidate_id=rec.candidate_id.id,
               old=old, new=vals)
        return return_Response(
            message="Evaluation submitted.", status=200,
            data={"evaluation": _eval_dict(rec)},
        )

    @http.route(EVAL_BASE + "/<int:eid>/complete", type="http", auth="none", methods=["PUT", "PATCH"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def evaluation_complete(self, eid, **kwargs):
        rec = request.env["hr.applicant.evaluation"].sudo().browse(eid).exists()
        if not rec:
            return return_Response(message="Evaluation not found.", status=404)
        data, err = _read_body()
        if err is not None:
            return err

        vals = {}
        if "pi_score" in data:
            v = _float(data["pi_score"])
            if v is None or not (0 <= v <= 100):
                return return_Response(message="pi_score must be 0-100.", status=400)
            vals["pi_score"] = v
        verdict = data.get("final_verdict")
        if verdict is not None:
            if verdict not in ("hire", "reject", "hold", "next_round", "", False, None):
                msg = "final_verdict must be one of: hire, reject, hold, next_round."
                return return_Response(message=msg, status=400, errors=[msg])
            vals["final_verdict"] = verdict or False
        if "notes" in data:
            vals["notes"] = data["notes"]
        if verdict in ("hire", "reject", "hold"):
            vals["status"] = "completed"
            vals["completed_at"] = datetime.utcnow()
        elif verdict == "next_round":
            vals["status"] = "in_progress"
            vals["completed_at"] = False
        else:
            vals["status"] = "in_progress"

        old = {"status": rec.status, "pi_score": rec.pi_score}
        rec.write(vals)
        _audit("evaluation", rec.id, "interview_completed",
               candidate_id=rec.candidate_id.id, old=old, new=vals)
        return return_Response(
            message="Interview completed.", status=200,
            data={"evaluation": _eval_dict(rec)},
        )

    @http.route(EVAL_BASE + "/<int:eid>/pms-score", type="http", auth="none", methods=["PUT", "PATCH"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def evaluation_pms_score(self, eid, **kwargs):
        rec = request.env["hr.applicant.evaluation"].sudo().browse(eid).exists()
        if not rec:
            return return_Response(message="Evaluation not found.", status=404)
        data, err = _read_body()
        if err is not None:
            return err

        score = _float(data.get("pms_score"))
        if score is None or not (0 <= score <= 100):
            return return_Response(message="pms_score must be 0-100.", status=400)

        old = {"pms_score": rec.pms_score}
        rec.pms_score = score
        _audit("evaluation", rec.id, "pms_score_updated",
               candidate_id=rec.candidate_id.id, old=old, new={"pms_score": score})
        return return_Response(
            message="PMS score updated.", status=200,
            data={"evaluation": _eval_dict(rec)},
        )

    @http.route(EVAL_BASE + "/<int:eid>/pi-bypass", type="http", auth="none", methods=["PUT", "PATCH"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def evaluation_pi_bypass(self, eid, **kwargs):
        rec = request.env["hr.applicant.evaluation"].sudo().browse(eid).exists()
        if not rec:
            return return_Response(message="Evaluation not found.", status=404)
        return self._do_pi_bypass(rec)

    @http.route(CAND_BASE + "/<int:cid>/pi-bypass", type="http", auth="none", methods=["PUT", "PATCH"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def candidate_pi_bypass(self, cid, **kwargs):
        candidate = request.env["hr.applicant"].sudo().browse(cid).exists()
        if not candidate:
            return return_Response(message=f"Candidate id={cid} not found.", status=404)
        Eval = request.env["hr.applicant.evaluation"].sudo()
        rec = Eval.search([("candidate_id", "=", cid)], limit=1)
        if not rec:
            rec = Eval.create({"candidate_id": cid, "status": "draft"})
        return self._do_pi_bypass(rec)

    def _do_pi_bypass(self, rec):
        data, err = _read_body()
        if err is not None:
            return err
        vals = {"status": "pi_bypassed", "completed_at": datetime.utcnow()}
        if "final_verdict" in data:
            vals["final_verdict"] = data["final_verdict"]
        if "pi_score" in data:
            v = _float(data["pi_score"])
            if v is None or not (0 <= v <= 100):
                return return_Response(message="pi_score must be 0-100.", status=400)
            vals["pi_score"] = v
        if "notes" in data:
            vals["notes"] = data["notes"]
        if "bypass_reason" in data:
            vals["bypass_reason"] = data["bypass_reason"]
        elif "notes" in data:
            vals["bypass_reason"] = data["notes"]

        old = {"status": rec.status}
        rec.write(vals)
        _audit("evaluation", rec.id, "pi_bypassed",
               candidate_id=rec.candidate_id.id, old=old, new=vals)
        return return_Response(
            message="PI bypassed.", status=200,
            data={"evaluation": _eval_dict(rec)},
        )

    @http.route(AUDIT_BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @_handle_errors
    def audit_logs_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)

        domain = []
        et = params.get("entity_type") or params.get("entityType")
        if et:
            domain.append(("entity_type", "=", et))
        eid = _int(params.get("entity_id") or params.get("entityId"))
        if eid:
            domain.append(("entity_id", "=", eid))
        cid = _int(params.get("candidate_id") or params.get("candidateId"))
        if cid:
            domain.append(("candidate_id", "=", cid))

        Log = request.env["hr.applicant.audit.log"].sudo()
        total = Log.search_count(domain)
        records = Log.search(domain, offset=offset, limit=limit, order="create_date desc")
        return return_Response(
            message="OK", status=200,
            data={
                "logs": [_audit_dict(r) for r in records],
                "pagination": _pagination_block(total, page, limit),
            },
        )

    @http.route(PMS_BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @_handle_errors
    def pms_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)
        domain = []
        eid = _int(params.get("employee_id") or params.get("employeeId"))
        if eid:
            domain.append(("employee_id", "=", eid))
        cid = _int(params.get("candidate_id") or params.get("candidateId"))
        if cid:
            domain.append(("candidate_id", "=", cid))
        PMS = request.env["hr.applicant.pms.evaluation"].sudo()
        total = PMS.search_count(domain)
        records = PMS.search(domain, offset=offset, limit=limit, order="create_date desc")
        return return_Response(
            message="OK", status=200,
            data={
                "pms_evaluations": [_pms_dict(r) for r in records],
                "pagination": _pagination_block(total, page, limit),
            },
        )

    @http.route(PMS_BASE, type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def pms_create(self, **kwargs):
        data, err = _read_body()
        if err is not None:
            return err
        emp_id = _int(data.get("employeeId"))
        cid = _int(data.get("candidateId"))
        if not emp_id and cid:
            candidate = request.env["hr.applicant"].sudo().browse(cid).exists()
            if not candidate:
                return return_Response(message=f"Candidate id={cid} not found.", status=404)
            if not candidate.employee_id:
                return return_Response(
                    message=f"Candidate id={cid} has no linked hr.employee (not hired yet).",
                    status=400,
                )
            emp_id = candidate.employee_id.id
        if not emp_id:
            return return_Response(message="employeeId or candidateId (with hired employee) is required.", status=400)
        vals = {"employee_id": emp_id}
        if cid:
            vals["candidate_id"] = cid
        if data.get("evaluatorId"):
            vals["evaluator_id"] = _int(data["evaluatorId"])
        rec = request.env["hr.applicant.pms.evaluation"].sudo().create(vals)
        _audit("pms_evaluation", rec.id, "created", new=vals)
        return return_Response(
            message="PMS evaluation created.", status=200,
            data={"pms_evaluation": _pms_dict(rec)},
        )

    @http.route(PMS_BASE + "/<int:pid>/submit", type="http", auth="none", methods=["PUT", "PATCH"], csrf=False, cors="*", readonly=False)
    @validate_token
    @_handle_errors
    def pms_submit(self, pid, **kwargs):
        rec = request.env["hr.applicant.pms.evaluation"].sudo().browse(pid).exists()
        if not rec:
            return return_Response(message="PMS evaluation not found.", status=404)
        data, err = _read_body()
        if err is not None:
            return err
        vals = {}
        for f in rec.METRIC_FIELDS:
            if f in data:
                v = _int(data[f])
                if v is None or not (0 <= v <= 10):
                    return return_Response(message=f"{f} must be integer 0-10.", status=400)
                vals[f] = v
        if "overall_rating" in data:
            vals["overall_rating"] = data["overall_rating"]
        if "remarks" in data:
            vals["remarks"] = data["remarks"]
        if "metric_remarks" in data:
            vals["metric_remarks"] = json.dumps(data["metric_remarks"]) if not isinstance(data["metric_remarks"], str) else data["metric_remarks"]
        vals["submitted_at"] = datetime.utcnow()
        rec.write(vals)
        _audit("pms_evaluation", rec.id, "submitted", new=vals)
        return return_Response(
            message="PMS evaluation submitted.", status=200,
            data={"pms_evaluation": _pms_dict(rec)},
        )
