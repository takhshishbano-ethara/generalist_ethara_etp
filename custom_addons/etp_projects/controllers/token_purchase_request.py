import base64
import json
import logging

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


def _serialize_request(rec):
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
        "model_name": rec.model_name or "",
        "requested_amount": float(rec.requested_amount or 0.0),
        "approved_amount": float(rec.approved_amount or 0.0),
        "description": rec.description or "",
        "cost_center": rec.cost_center or "",
        "rejection_reason": rec.rejection_reason or "",
        "requester_id": rec.requester_id.id if rec.requester_id else False,
        "requester_name": rec.requester_id.name if rec.requester_id else "",
        "approver_id": rec.approver_id.id if rec.approver_id else False,
        "approver_name": rec.approver_id.name if rec.approver_id else "",
        "approval_date": _fmt_dt(rec.approval_date),
        "completed_by_id": rec.completed_by_id.id if rec.completed_by_id else False,
        "completed_by_name": rec.completed_by_id.name if rec.completed_by_id else "",
        "completed_date": _fmt_dt(rec.completed_date),
        "balance_before": float(rec.balance_before or 0.0),
        "create_date": _fmt_dt(rec.create_date),
        "supporting_document_ids": rec.supporting_document_ids.ids,
        "supporting_document_count": rec.supporting_document_count,
    }


def _serialize_attachment(att):
    return {
        "id": att.id,
        "name": att.name or "",
        "mimetype": att.mimetype or "",
        "file_size": int(att.file_size or 0),
    }


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


class EtpTokenPurchaseRequestApiController(http.Controller):

    @http.route(
        "/api/v1/etp_projects/token_purchase/list",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def list_requests(self, **params):
        try:
            jdata = _read_json_body()
            domain = []

            state = jdata.get("state")
            if state:
                allowed = {"draft", "pending", "approved", "rejected", "completed"}
                if isinstance(state, list):
                    if not all(s in allowed for s in state):
                        return return_Response(
                            message="'state' contains invalid values. Allowed: %s." % sorted(allowed),
                            status=400,
                        )
                    domain.append(("state", "in", state))
                elif isinstance(state, str):
                    if state not in allowed:
                        return return_Response(
                            message="'state' must be one of %s." % sorted(allowed),
                            status=400,
                        )
                    domain.append(("state", "=", state))
                else:
                    return return_Response(
                        message="'state' must be a string or list of strings.",
                        status=400,
                    )

            for fld in ("project_id", "budget_id", "requester_id"):
                if jdata.get(fld) is not None:
                    domain.append((fld, "=", _coerce_int(jdata[fld], fld)))

            if jdata.get("mine_only"):
                domain.append(("requester_id", "=", request.env.uid))

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
                    "results": [_serialize_request(r) for r in records],
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
            payload = _serialize_request(req)
            payload["supporting_documents"] = [
                _serialize_attachment(a) for a in req.supporting_document_ids
            ]
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
            if not model_name:
                return return_Response(message="'model_name' is required.", status=400)
            if not description:
                return return_Response(message="'description' is required.", status=400)

            budget = request.env[BUDGET_MODEL].sudo().browse(budget_id)
            if not budget.exists():
                return return_Response(message="Budget not found.", status=404)

            req = request.env[REQUEST_MODEL].create({
                "budget_id": budget_id,
                "model_name": model_name,
                "requested_amount": requested_amount,
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
                            "request": _serialize_request(req),
                            "submitted": False,
                        }},
                    )

            return return_Response(
                message="Created.", status=200,
                data={"data": {
                    "request": _serialize_request(req),
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
        "/api/v1/etp_projects/token_purchase/submit",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def submit_request(self, **params):
        try:
            jdata = _read_json_body()
            req_id = _coerce_int(jdata.get("id"), "id")
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)
            req.action_submit()
            return return_Response(
                message="Submitted.", status=200,
                data={"data": {"request": _serialize_request(req.sudo())}},
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
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)
            req.action_approve()
            return return_Response(
                message="Approved.", status=200,
                data={"data": {"request": _serialize_request(req.sudo())}},
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
            reason = jdata.get("rejection_reason") or ""
            req = request.env[REQUEST_MODEL].browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)
            req._check_backend_approver()
            req._act_reject(request.env.user, reason=str(reason).strip() or None)
            return return_Response(
                message="Rejected.", status=200,
                data={"data": {"request": _serialize_request(req.sudo())}},
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
            approved_amount = _coerce_float(jdata.get("approved_amount"), "approved_amount")
            cost_center = (jdata.get("cost_center") or "").strip()
            existing_attachment_ids = jdata.get("attachment_ids") or []
            documents = jdata.get("supporting_documents") or []

            req = request.env[REQUEST_MODEL].sudo().browse(req_id)
            if not req.exists():
                return return_Response(message="Request not found.", status=404)

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
                    "request": _serialize_request(req),
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
