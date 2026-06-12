import base64
import json
import logging
from datetime import datetime

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


REQUEST_MODEL = "etp.project.token.purchase.request"
BUDGET_MODEL = "etp.project.aws.budget"
ATTACHMENT_MODEL = "ir.attachment"
PROJECT_MODEL = "project.project"

# Role gate for initial-budget creation (pattern copied from
# leviathan_extension): CTO/TPM may initiate for any project; a Project Lead
# may initiate only for projects they lead.
INITIAL_BUDGET_FULL_ACCESS_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
)

INITIAL_BUDGET_PL_ROLE_XMLIDS = (
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)


def _get_role_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _user_has_role(env, xmlids):
    role = env.user.user_role
    if not role:
        return False
    return role.id in _get_role_ids(env, xmlids)


def _read_json_body():
    raw = b""
    try:
        raw = request.httprequest.stream.read() or b""
    except Exception:
        raw = request.httprequest.data or b""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fmt_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _attachment_download_url(att_id, req_id):
    return (
        "/api/v1/etp_projects/token_purchase/attachment"
        "?attachment_id=%s&request_id=%s" % (att_id, req_id)
    )


def _serialize_request(rec, uid, approver_ids, finance_ids):
    requested = float(rec.requested_amount or 0.0)
    approved = float(rec.approved_amount or 0.0)
    resolved = rec.state in ("approved", "completed")
    amount_not_granted = max(0.0, requested - approved) if resolved else 0.0
    is_partial = resolved and 0 < approved < requested
    balance_before = float(rec.balance_before or 0.0)
    balance_after = (
        round(balance_before + approved, 2) if rec.state == "completed" else 0.0
    )

    requester_uid = rec.requester_id.id if rec.requester_id else False
    can_approve = (
        uid in approver_ids
        and requester_uid != uid
        and rec.state == "pending"
    )
    can_complete = uid in finance_ids and rec.state == "approved"
    can_withdraw = requester_uid == uid and rec.state == "pending"

    return {
        "id": rec.id,
        "name": rec.name or "",
        "state": rec.state,
        "budget_id": rec.budget_id.id if rec.budget_id else False,
        "budget_name": rec.budget_id.name if rec.budget_id else "",
        "project_id": rec.project_id.id if rec.project_id else False,
        "project_name": rec.project_id.name if rec.project_id else "",
        "currency_id": rec.currency_id.id if rec.currency_id else False,
        "currency": rec.currency_id.name if rec.currency_id else "",
        "currency_symbol": rec.currency_id.symbol if rec.currency_id else "",
        "model_name": rec.model_name or "",
        "requested_amount": requested,
        "approved_amount": approved,
        "amount_not_granted": round(amount_not_granted, 2),
        "is_partial": is_partial,
        "description": rec.description or "",
        "decision_note": rec.decision_note or "",
        "cost_center": rec.cost_center or "",
        "rejection_reason": rec.rejection_reason or "",
        "requester_id": requester_uid,
        "requester_name": rec.requester_id.name if rec.requester_id else "",
        "approver_id": rec.approver_id.id if rec.approver_id else False,
        "approver_name": rec.approver_id.name if rec.approver_id else "",
        "approval_date": _fmt_dt(rec.approval_date),
        "completed_by_id": rec.completed_by_id.id if rec.completed_by_id else False,
        "completed_by_name": rec.completed_by_id.name if rec.completed_by_id else "",
        "completed_date": _fmt_dt(rec.completed_date),
        "balance_before": balance_before,
        "balance_after": balance_after,
        "create_date": _fmt_dt(rec.create_date),
        "supporting_document_ids": rec.supporting_document_ids.ids,
        "supporting_document_count": rec.supporting_document_count,
        "supporting_documents": [
            _serialize_attachment(a, rec.id) for a in rec.supporting_document_ids
        ],
        "can_approve": can_approve,
        "can_complete": can_complete,
        "can_withdraw": can_withdraw,
    }


def _serialize_attachment(att, req_id):
    return {
        "id": att.id,
        "name": att.name or "",
        "mimetype": att.mimetype or "",
        "file_size": int(att.file_size or 0),
        "download_url": _attachment_download_url(att.id, req_id),
    }


def _gating_sets():
    """Return (uid, approver_ids:set, finance_ids:set) for the real token user."""
    Model = request.env[REQUEST_MODEL]
    approver_ids = set(Model._get_approver_users().ids)
    finance_ids = set(Model._get_finance_users().ids)
    return request.env.uid, approver_ids, finance_ids


def _is_project_lead_of(env, project):
    """True if the token user is a Project Lead AND leads `project`."""
    if not project:
        return False
    employee = env.user.employee_id
    return bool(
        _user_has_role(env, INITIAL_BUDGET_PL_ROLE_XMLIDS)
        and employee
        and employee in project.sudo().project_lead
    )


def _can_view_request(env, req, uid, approver_ids, finance_ids):
    """Who may read a single TPR via /token_purchase/get: its requester, any
    approver/finance user, a full-access role (CTO/TPM), or the Project Lead of
    the request's project. Anyone else is blocked — closes IDOR enumeration."""
    if uid in approver_ids or uid in finance_ids:
        return True
    if req.requester_id and req.requester_id.id == uid:
        return True
    if _user_has_role(env, INITIAL_BUDGET_FULL_ACCESS_ROLE_XMLIDS):
        return True
    return _is_project_lead_of(env, req.project_id)


def _attach_supporting_documents(req_id, documents):
    if not documents:
        return []
    if not isinstance(documents, list):
        raise ValidationError("'supporting_documents' must be a list.")
    Attachment = request.env[ATTACHMENT_MODEL].sudo()
    created_ids = []
    for idx, doc in enumerate(documents):
        if not isinstance(doc, dict):
            raise ValidationError(
                "supporting_documents[%d] must be an object with 'filename' and 'data_b64'." % idx
            )
        filename = (doc.get("filename") or "").strip()
        data_b64 = doc.get("data_b64") or ""
        mimetype = doc.get("mimetype") or "application/octet-stream"
        if not filename:
            raise ValidationError("supporting_documents[%d].filename is required." % idx)
        if not data_b64 or not isinstance(data_b64, str):
            raise ValidationError(
                "supporting_documents[%d].data_b64 is required (base64-encoded string)." % idx
            )
        try:
            base64.b64decode(data_b64, validate=True)
        except Exception:
            raise ValidationError(
                "supporting_documents[%d].data_b64 is not valid base64." % idx
            )
        att = Attachment.create({
            "name": filename,
            "datas": data_b64,
            "type": "binary",
            "res_model": REQUEST_MODEL,
            "res_id": req_id,
            "mimetype": mimetype,
        })
        created_ids.append(att.id)
    return created_ids


def _coerce_int(value, field_name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("'%s' must be an integer." % field_name)


def _coerce_float(value, field_name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError("'%s' must be a number." % field_name)


ALLOWED_STATES = {
    "draft", "pending", "approved", "rejected", "withdrawn", "completed",
}


def _parse_iso_date(value, field_name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValidationError("'%s' must be a date in YYYY-MM-DD format." % field_name)


def _build_filter_domain(jdata, uid, approver_ids, finance_ids):
    """Shared list/summary domain. Applies requester scoping for users who are
    neither approvers nor finance users."""
    domain = []

    state = jdata.get("state")
    if state:
        if isinstance(state, list):
            if not all(s in ALLOWED_STATES for s in state):
                raise ValidationError(
                    "'state' contains invalid values. Allowed: %s." % sorted(ALLOWED_STATES)
                )
            domain.append(("state", "in", state))
        elif isinstance(state, str):
            if state not in ALLOWED_STATES:
                raise ValidationError(
                    "'state' must be one of %s." % sorted(ALLOWED_STATES)
                )
            domain.append(("state", "=", state))
        else:
            raise ValidationError("'state' must be a string or list of strings.")

    for fld in ("project_id", "budget_id", "requester_id"):
        if jdata.get(fld) is not None:
            domain.append((fld, "=", _coerce_int(jdata[fld], fld)))

    model_name = (jdata.get("model_name") or "").strip()
    if model_name:
        domain.append(("model_name", "=", model_name))

    search = (jdata.get("search") or "").strip()
    if search:
        domain += [
            "|", "|",
            ("name", "ilike", search),
            ("project_id.name", "ilike", search),
            ("requester_id.name", "ilike", search),
        ]

    if jdata.get("date_from"):
        d_from = _parse_iso_date(jdata["date_from"], "date_from")
        domain.append(("create_date", ">=", "%s 00:00:00" % d_from.isoformat()))
    if jdata.get("date_to"):
        d_to = _parse_iso_date(jdata["date_to"], "date_to")
        domain.append(("create_date", "<=", "%s 23:59:59" % d_to.isoformat()))

    # Requester scoping: non-approver, non-finance users only see their own.
    is_privileged = uid in approver_ids or uid in finance_ids
    if not is_privileged:
        domain.append(("requester_id", "=", uid))
    elif jdata.get("mine_only"):
        domain.append(("requester_id", "=", uid))
    elif not state:
        # Approvers' default queue shows submitted requests only — another
        # user's unsubmitted draft is not actionable, and including it would
        # make the list count disagree with the (draft-excluding) summary. An
        # explicit `state` filter still lets the UI ask for drafts on purpose.
        domain.append(("state", "!=", "draft"))

    return domain


class EtpTokenPurchaseRequestApiController(http.Controller):

    @http.route(
        "/api/v1/etp_projects/token_purchase/list",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def list_requests(self, **params):
        try:
            jdata = _read_json_body()
            uid, approver_ids, finance_ids = _gating_sets()
            domain = _build_filter_domain(jdata, uid, approver_ids, finance_ids)

            try:
                limit = int(jdata.get("limit") or 100)
            except (TypeError, ValueError):
                limit = 100
            try:
                offset = int(jdata.get("offset") or 0)
            except (TypeError, ValueError):
                offset = 0
            limit = max(1, min(limit, 500))
            offset = max(0, offset)

            Model = request.env[REQUEST_MODEL].sudo()
            total = Model.search_count(domain)
            records = Model.search(domain, limit=limit, offset=offset)

            return return_Response(
                message="OK",
                status=200,
                data={"data": {
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "count": len(records),
                    "meta": {
                        "is_approver": uid in approver_ids,
                        "is_finance": uid in finance_ids,
                    },
                    "results": [
                        _serialize_request(r, uid, approver_ids, finance_ids)
                        for r in records
                    ],
                }},
            )
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.list failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/get",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def get_request(self, **params):
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            req = request.env[REQUEST_MODEL].sudo().browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)
            uid, approver_ids, finance_ids = _gating_sets()
            if not _can_view_request(request.env, req, uid, approver_ids, finance_ids):
                return return_Response(
                    message="You are not allowed to view this request.",
                    status=403, errors=[{"code": "forbidden"}],
                )
            payload = _serialize_request(req, uid, approver_ids, finance_ids)
            payload["activity"] = req._activity_entries()
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.get failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/create",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def create_request(self, **params):
        try:
            jdata = _read_json_body()
            budget_id = _coerce_int(jdata.get("budget_id"), "budget_id")
            model_name = (jdata.get("model_name") or "").strip()
            description = (jdata.get("description") or "").strip()
            requested_amount = _coerce_float(jdata.get("requested_amount"), "requested_amount")
            if requested_amount <= 0:
                return return_Response(
                    message="'requested_amount' must be greater than zero.", status=400,
                )
            if requested_amount > 1_000_000:
                return return_Response(
                    message="'requested_amount' cannot exceed 1,000,000.", status=400,
                )
            if not model_name:
                return return_Response(message="'model_name' is required.", status=400)
            if not description:
                return return_Response(message="'description' is required.", status=400)

            budget = request.env[BUDGET_MODEL].sudo().browse(budget_id)
            if not budget.exists():
                return return_Response(message="Budget not found.", status=404)

            # Authorization: only a full-access role (CTO/TPM) or the Project
            # Lead of this budget's project may raise a top-up request against
            # it. Mirrors the initial_budget gate and prevents creating a
            # request against another team's budget by guessing its id.
            env = request.env
            if not (
                _user_has_role(env, INITIAL_BUDGET_FULL_ACCESS_ROLE_XMLIDS)
                or _is_project_lead_of(env, budget.project_id)
            ):
                return return_Response(
                    message="You are not allowed to request a budget for this project.",
                    status=403, errors=[{"code": "not_project_lead"}],
                )

            uid, approver_ids, finance_ids = _gating_sets()
            req = request.env[REQUEST_MODEL].create({
                "budget_id": budget_id,
                "model_name": model_name,
                "requested_amount": round(requested_amount, 2),
                "description": description,
            })

            submitted = False
            if jdata.get("submit"):
                try:
                    req.action_submit()
                    submitted = True
                except (UserError, ValidationError) as e:
                    return return_Response(
                        message="Created but submission failed: %s" % str(e),
                        status=400,
                        data={"data": {
                            "request": _serialize_request(req, uid, approver_ids, finance_ids),
                            "submitted": False,
                        }},
                    )

            return return_Response(
                message="Created.", status=200,
                data={"data": {
                    "request": _serialize_request(req, uid, approver_ids, finance_ids),
                    "submitted": submitted,
                }},
            )
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except UserError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.create failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/initial_budget",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def initial_budget(self, **params):
        try:
            jdata = _read_json_body()
            project_id = _coerce_int(jdata.get("project_id"), "project_id")
            amount = _coerce_float(jdata.get("amount"), "amount")
            model_name = (jdata.get("model_name") or "").strip()
            description = (jdata.get("description") or "").strip()

            if not (0 < amount <= 1_000_000):
                return return_Response(
                    message="'amount' must be greater than zero and at most 1,000,000.",
                    status=400,
                )
            if not model_name:
                return return_Response(message="'model_name' is required.", status=400)
            if not description:
                return return_Response(message="'description' is required.", status=400)

            # Browse with the real env (not sudo) so the role gate runs against
            # the real token user.
            project = request.env[PROJECT_MODEL].browse(project_id)
            if not project.exists():
                return return_Response(message="Project not found.", status=404)

            # Role gate: CTO/TPM → any project; PL → only projects they lead.
            env = request.env
            is_full_access = _user_has_role(env, INITIAL_BUDGET_FULL_ACCESS_ROLE_XMLIDS)
            is_pl = _user_has_role(env, INITIAL_BUDGET_PL_ROLE_XMLIDS)
            employee = env.user.employee_id
            is_project_lead = (
                is_pl
                and employee
                and employee in project.sudo().project_lead
            )
            if not (is_full_access or is_project_lead):
                return return_Response(
                    message="You are not allowed to create the initial budget for this project.",
                    status=403, errors=[{"code": "not_project_lead"}],
                )

            # First-budget guard: the project must have no budget at all
            # (active OR inactive).
            Budget = request.env[BUDGET_MODEL].sudo()
            if Budget.with_context(active_test=False).search_count(
                [("project_id", "=", project_id)]
            ):
                return return_Response(
                    message="This project already has a budget.",
                    status=400, errors=[{"code": "budget_exists"}],
                )

            amount = round(amount, 2)
            # Create budget + request + submit atomically. Without the
            # savepoint, a failure in action_submit() (e.g. no approver
            # recipients) leaves the budget/request committed — because the
            # UserError is caught below and a normal Response is returned, so
            # Odoo never rolls back the request transaction. That phantom budget
            # then trips the first-budget guard on every retry (budget_exists).
            # currency_id is left to the model default (company / INR).
            with request.env.cr.savepoint():
                budget = Budget.create({
                    "project_id": project_id,
                    "name": "%s Budget" % (project.sudo().name or ""),
                    "budget_amount": amount,
                    "project_budget": 0.0,
                    "active": False,
                })

                uid, approver_ids, finance_ids = _gating_sets()
                req = request.env[REQUEST_MODEL].create({
                    "budget_id": budget.id,
                    "model_name": model_name,
                    "requested_amount": amount,
                    "description": description,
                })
                req.action_submit()

            return return_Response(
                message="Initial budget request submitted.", status=200,
                data={"data": {
                    "request": _serialize_request(
                        req.sudo(), uid, approver_ids, finance_ids),
                    "budget_id": budget.id,
                }},
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.initial_budget failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/submit",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def submit_request(self, **params):
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            uid, approver_ids, finance_ids = _gating_sets()
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)
            if not req.requester_id or req.requester_id.id != uid:
                return return_Response(
                    message="Only the requester can submit this request.",
                    status=403, errors=[{"code": "not_requester"}],
                )
            req.action_submit()
            return return_Response(
                message="Submitted.", status=200,
                data={"data": {"request": _serialize_request(
                    req.sudo(), uid, approver_ids, finance_ids)}},
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.submit failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/approve",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def approve_request(self, **params):
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            approved_amount = jdata.get("approved_amount")
            if approved_amount is not None:
                approved_amount = _coerce_float(approved_amount, "approved_amount")
            decision_note = jdata.get("decision_note")

            uid, approver_ids, finance_ids = _gating_sets()
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)

            # Approver + two-person gate → 403 not_approver (incl. self-approval).
            if uid not in approver_ids or (
                req.requester_id and req.requester_id.id == uid
            ):
                return return_Response(
                    message="You are not allowed to approve this request.",
                    status=403, errors=[{"code": "not_approver"}],
                )

            req._act_approve(request.env.user, approved_amount, decision_note)
            return return_Response(
                message="Approved.", status=200,
                data={"data": {"request": _serialize_request(
                    req.sudo(), uid, approver_ids, finance_ids)}},
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.approve failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/reject",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def reject_request(self, **params):
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            reason = (jdata.get("rejection_reason") or "").strip()

            uid, approver_ids, finance_ids = _gating_sets()
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)

            if uid not in approver_ids or (
                req.requester_id and req.requester_id.id == uid
            ):
                return return_Response(
                    message="You are not allowed to reject this request.",
                    status=403, errors=[{"code": "not_approver"}],
                )

            # A rejection must carry a reason (the requester is notified with
            # it). Enforced here at the API boundary ONLY — the model's
            # _act_reject is also driven by the Odoo backend action_reject(),
            # which passes no reason, so the model itself stays reason-optional.
            if not reason:
                return return_Response(
                    message="A rejection reason is required.",
                    status=400, errors=[{"code": "rejection_reason_required"}],
                )

            req._act_reject(request.env.user, reason=reason)
            # Rejecting an initial-budget shell may cascade-delete this request
            # along with its inactive shell budget; serialize defensively.
            req = req.sudo()
            if not req.exists():
                return return_Response(
                    message="Rejected.", status=200,
                    data={"data": {"request": None}},
                )
            return return_Response(
                message="Rejected.", status=200,
                data={"data": {"request": _serialize_request(
                    req, uid, approver_ids, finance_ids)}},
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.reject failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/complete",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def complete_request(self, **params):
        attachment_ids_created = []
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            # Optional: reuse the locked approved_amount when omitted.
            approved_amount = jdata.get("approved_amount")
            if approved_amount is not None:
                approved_amount = _coerce_float(approved_amount, "approved_amount")
            cost_center = (jdata.get("cost_center") or "").strip()
            existing_attachment_ids = jdata.get("attachment_ids") or []
            documents = jdata.get("supporting_documents") or []

            uid, approver_ids, finance_ids = _gating_sets()
            req = request.env[REQUEST_MODEL].sudo().browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)

            if uid not in finance_ids:
                return return_Response(
                    message="You are not allowed to complete this request.",
                    status=403, errors=[{"code": "not_finance"}],
                )

            if not isinstance(existing_attachment_ids, list) or not all(
                isinstance(x, int) for x in existing_attachment_ids
            ):
                return return_Response(
                    message="'attachment_ids' must be a list of integers.", status=400,
                )

            attachment_ids_created = _attach_supporting_documents(req_id, documents)
            all_attachment_ids = list(existing_attachment_ids) + attachment_ids_created

            req._do_complete(
                approved_amount,
                cost_center,
                all_attachment_ids,
                request.env.user,
                mark_finance_token_used=False,
            )

            return return_Response(
                message="Completed.", status=200,
                data={"data": {
                    "request": _serialize_request(req, uid, approver_ids, finance_ids),
                    "attachment_ids_created": attachment_ids_created,
                }},
            )
        except (UserError, ValidationError) as e:
            if attachment_ids_created:
                request.env[ATTACHMENT_MODEL].sudo().browse(attachment_ids_created).unlink()
            return return_Response(message=str(e), status=400)
        except Exception as e:
            if attachment_ids_created:
                try:
                    request.env[ATTACHMENT_MODEL].sudo().browse(attachment_ids_created).unlink()
                except Exception:
                    pass
            _logger.exception("token_purchase.complete failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/withdraw",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def withdraw_request(self, **params):
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            uid, approver_ids, finance_ids = _gating_sets()
            # Browse with the real env (not sudo) for the membership check.
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)

            if not req.requester_id or req.requester_id.id != uid:
                return return_Response(
                    message="Only the requester can withdraw this request.",
                    status=403, errors=[{"code": "not_requester"}],
                )
            if req.state != "pending":
                return return_Response(
                    message="Only pending requests can be withdrawn.", status=400,
                )

            req.action_withdraw()
            return return_Response(
                message="Withdrawn.", status=200,
                data={"data": {"request": _serialize_request(
                    req.sudo(), uid, approver_ids, finance_ids)}},
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.withdraw failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/update",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def update_request(self, **params):
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            uid, approver_ids, finance_ids = _gating_sets()
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)

            if not req.requester_id or req.requester_id.id != uid:
                return return_Response(
                    message="Only the requester can edit this request.",
                    status=403, errors=[{"code": "not_requester"}],
                )
            if req.state not in ("draft", "pending"):
                return return_Response(
                    message="Only draft or pending requests can be edited.",
                    status=400,
                )

            vals = {}
            for fld in ("model_name", "description", "requested_amount"):
                if fld in jdata:
                    vals[fld] = jdata[fld]
            req.action_update(vals)
            return return_Response(
                message="Updated.", status=200,
                data={"data": {"request": _serialize_request(
                    req.sudo(), uid, approver_ids, finance_ids)}},
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.update failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/summary",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def summary_requests(self, **params):
        try:
            jdata = _read_json_body()
            uid, approver_ids, finance_ids = _gating_sets()
            domain = _build_filter_domain(jdata, uid, approver_ids, finance_ids)

            Model = request.env[REQUEST_MODEL].sudo()
            records = Model.search(domain)

            total_count = 0
            total_requested_amount = 0.0
            total_approved_amount = 0.0
            pending_amount = 0.0
            approved_count = pending_count = completed_count = 0
            rejected_count = withdrawn_count = 0
            currency = ""
            currency_symbol = ""

            for rec in records:
                state = rec.state
                # Drafts are unsubmitted (nothing committed). Keep them out of
                # every total and bucket so the per-state buckets always
                # reconcile to total_count / total_requested_amount.
                if state == "draft":
                    continue
                total_count += 1
                total_requested_amount += float(rec.requested_amount or 0.0)
                if not currency and rec.currency_id:
                    currency = rec.currency_id.name or ""
                    currency_symbol = rec.currency_id.symbol or ""
                if state == "pending":
                    pending_count += 1
                    pending_amount += float(rec.requested_amount or 0.0)
                elif state == "approved":
                    # A granted-but-not-yet-completed request already carries its
                    # approved_amount; count it toward "Total Approved" so the KPI
                    # updates the moment the CTO approves — not only on completion.
                    approved_count += 1
                    total_approved_amount += float(rec.approved_amount or 0.0)
                elif state == "completed":
                    completed_count += 1
                    total_approved_amount += float(rec.approved_amount or 0.0)
                elif state == "rejected":
                    rejected_count += 1
                elif state == "withdrawn":
                    withdrawn_count += 1

            return return_Response(
                message="OK", status=200,
                data={"data": {
                    "total_count": total_count,
                    "total_requested_amount": round(total_requested_amount, 2),
                    "total_approved_amount": round(total_approved_amount, 2),
                    "approved_count": approved_count,
                    "pending_count": pending_count,
                    "pending_amount": round(pending_amount, 2),
                    "completed_count": completed_count,
                    "rejected_count": rejected_count,
                    "withdrawn_count": withdrawn_count,
                    "currency": currency,
                    "currency_symbol": currency_symbol,
                }},
            )
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.summary failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/capabilities",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def capabilities(self, **params):
        try:
            uid, approver_ids, finance_ids = _gating_sets()
            user = request.env.user
            return return_Response(
                message="OK", status=200,
                data={"data": {
                    "is_approver": uid in approver_ids,
                    "is_finance": uid in finance_ids,
                    "can_request": True,
                    "user_id": uid,
                    "user_name": user.name or "",
                }},
            )
        except Exception as e:
            _logger.exception("token_purchase.capabilities failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/models",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def list_models(self, **params):
        try:
            jdata = _read_json_body()
            domain = [("model_name", "!=", False), ("model_name", "!=", "")]
            if jdata.get("project_id") is not None:
                domain.append(
                    ("project_id", "=", _coerce_int(jdata["project_id"], "project_id"))
                )
            Model = request.env[REQUEST_MODEL].sudo()
            rows = Model.search_read(domain, ["model_name"])
            models = sorted(
                {(r.get("model_name") or "").strip() for r in rows} - {""}
            )
            return return_Response(
                message="OK", status=200,
                data={"data": {"models": models}},
            )
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("token_purchase.models failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/token_purchase/attachment",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def download_attachment(self, **params):
        try:
            try:
                attachment_id = int(params.get("attachment_id"))
                req_id = int(params.get("request_id"))
            except (TypeError, ValueError):
                return return_Response(
                    message="'attachment_id' and 'request_id' must be integers.",
                    status=400,
                )

            req = request.env[REQUEST_MODEL].sudo().browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)

            uid, approver_ids, finance_ids = _gating_sets()
            # Requester scoping: non-privileged users can only fetch their own.
            is_privileged = uid in approver_ids or uid in finance_ids
            if not is_privileged and (
                not req.requester_id or req.requester_id.id != uid
            ):
                return return_Response(
                    message="You are not allowed to access this attachment.",
                    status=403, errors=[{"code": "not_requester"}],
                )

            if attachment_id not in req.supporting_document_ids.ids:
                return return_Response(
                    message="Attachment not found for this request.", status=404,
                )

            att = request.env[ATTACHMENT_MODEL].sudo().browse(attachment_id)
            if not att.exists():
                return return_Response(message="Attachment not found.", status=404)

            data = base64.b64decode(att.datas or b"")
            filename = att.name or ("attachment_%s" % attachment_id)
            headers = [
                ("Content-Type", att.mimetype or "application/octet-stream"),
                ("Content-Disposition",
                 'attachment; filename="%s"' % filename.replace('"', "")),
                ("Content-Length", str(len(data))),
            ]
            return request.make_response(data, headers=headers)
        except Exception as e:
            _logger.exception("token_purchase.attachment failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])
