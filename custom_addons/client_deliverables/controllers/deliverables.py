import json
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    generate_s3_link,
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

# Only these API roles may use the endpoints.
ALLOWED_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
)
LINK_FIELDS = ("git_repo_url", "excel_sheet_url", "gdrive_url")
VALID_STATUSES = ("draft", "delivered", "commented", "rework", "approved")
S3_FEEDBACK_PREFIX = "client_deliverable_feedback"


def _read_params():
    """Merge query/form params with a JSON body (POST endpoints)."""
    params = dict(request.params or {})
    try:
        raw_body = request.httprequest.get_data(as_text=True) or ""
        if raw_body:
            body = json.loads(raw_body)
            if isinstance(body, dict):
                params.update(body)
    except (json.JSONDecodeError, ValueError):
        pass
    return params


def _allowed_role_ids(env):
    ids = []
    for xmlid in ALLOWED_ROLE_XMLIDS:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _is_allowed(env):
    """Only TPM and CTO roles may access deliverables."""
    role = env.user.user_role
    return bool(role) and role.id in _allowed_role_ids(env)


def _forbidden():
    return return_Response(
        message="Only TPM and CTO roles can access client deliverables.",
        status=403,
    )


def _serialize_s3_link(link):
    return {"id": link.id, "name": link.name or None, "url": link.url}


def _serialize_feedback(feedback):
    return {
        "id": feedback.id,
        "date": feedback.feedback_date.isoformat() if feedback.feedback_date else None,
        "author": feedback.author_name or (feedback.author_id.name or None),
        "comment": feedback.comment or "",
        "file_url": feedback.file_url or None,
        "file_name": feedback.file_name or None,
    }


def _serialize_log(log):
    return {
        "id": log.id,
        "iteration": log.iteration,
        "from_status": log.from_status or None,
        "to_status": log.to_status or None,
        "changed_by": log.user_id.name or None,
        "date": log.create_date.isoformat() if log.create_date else None,
        "note": log.note or None,
    }


def _serialize(record):
    return {
        "id": record.id,
        "name": record.name,
        "project_id": record.project_id.id or None,
        "project_name": record.project_id.display_name or None,
        "status": record.status,
        "iteration": record.iteration,
        "links": {key: getattr(record, key) or None for key in LINK_FIELDS},
        "s3_links": [_serialize_s3_link(s) for s in record.s3_link_ids],
        "feedback_count": record.feedback_count,
        "feedback": [_serialize_feedback(f) for f in record.feedback_ids],
        "iteration_logs": [_serialize_log(g) for g in record.iteration_log_ids],
    }


def _parse_int(raw, label):
    raw = (str(raw) if raw is not None else "").strip()
    if not raw:
        return None, None
    if not raw.isdigit():
        return None, return_Response(
            message="Invalid %s '%s'. Expected an integer." % (label, raw),
            status=400,
        )
    return int(raw), None


def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message="Invalid feedback_date '%s'. Expected YYYY-MM-DD." % raw,
            status=400,
        )


def _get_deliverable(env, params):
    """Resolve a deliverable by id, or an error response."""
    deliverable_id, error = _parse_int(params.get("deliverable_id"), "deliverable_id")
    if error is not None:
        return None, error
    if deliverable_id is None:
        return None, return_Response(
            message="Missing required parameter 'deliverable_id'.", status=400
        )
    record = env["client.deliverable"].sudo().browse(deliverable_id)
    if not record.exists():
        return None, return_Response(
            message="Deliverable '%s' not found." % deliverable_id, status=404
        )
    return record, None


class ClientDeliverablesController(http.Controller):

    @http.route(
        "/api/v1/client_deliverables_ext/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def client_deliverables_ext_info(self, **kwargs):
        """List deliverables (links, status, feedback, iteration logs)."""
        env = request.env
        if not _is_allowed(env):
            return _forbidden()

        params = request.params or {}
        domain = []

        deliverable_id, error = _parse_int(params.get("deliverable_id"), "deliverable_id")
        if error is not None:
            return error
        if deliverable_id is not None:
            domain += [("id", "=", deliverable_id)]

        project_id, error = _parse_int(params.get("project_id"), "project_id")
        if error is not None:
            return error
        if project_id is not None:
            domain += [("project_id", "=", project_id)]

        records = env["client.deliverable"].sudo().search(domain)
        return return_Response(
            message="OK",
            status=200,
            data={"deliverables": [_serialize(r) for r in records]},
        )

    @http.route(
        "/api/v1/client_deliverables_ext/links/update",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def client_deliverables_ext_update_links(self, **kwargs):
        """Update git/excel/drive URLs and (optionally) replace the S3 links."""
        env = request.env
        if not _is_allowed(env):
            return _forbidden()

        params = _read_params()
        record, error = _get_deliverable(env, params)
        if error is not None:
            return error

        vals = {}
        for key in LINK_FIELDS:
            if key in params:
                vals[key] = (params.get(key) or "").strip()

        # Replace the full S3 link set when 's3_links' is provided.
        replace_s3 = "s3_links" in params
        s3_links = params.get("s3_links")
        if replace_s3:
            if not isinstance(s3_links, list):
                return return_Response(
                    message="'s3_links' must be a list of {url, name?} objects.",
                    status=400,
                )
            commands = [(5, 0, 0)]  # clear existing
            for entry in s3_links:
                if not isinstance(entry, dict):
                    return return_Response(
                        message="Each 's3_links' item must be an object with a 'url'.",
                        status=400,
                    )
                url = (entry.get("url") or "").strip()
                if not url:
                    return return_Response(
                        message="Each 's3_links' item requires a non-empty 'url'.",
                        status=400,
                    )
                commands.append(
                    (0, 0, {"url": url, "name": (entry.get("name") or "").strip()})
                )
            vals["s3_link_ids"] = commands

        if not vals:
            return return_Response(
                message="Nothing to update. Provide at least one of: %s or 's3_links'."
                % ", ".join(LINK_FIELDS),
                status=400,
            )

        record.write(vals)
        return return_Response(
            message="Links updated.",
            status=200,
            data={"deliverable": _serialize(record)},
        )

    @http.route(
        "/api/v1/client_deliverables_ext/status/set",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def client_deliverables_ext_set_status(self, **kwargs):
        """Change the deliverable status (logs the transition; bumps iteration
        when moving to 'rework')."""
        env = request.env
        if not _is_allowed(env):
            return _forbidden()

        params = _read_params()
        record, error = _get_deliverable(env, params)
        if error is not None:
            return error

        status = (params.get("status") or "").strip()
        if status not in VALID_STATUSES:
            return return_Response(
                message="Invalid 'status'. Expected one of: %s."
                % ", ".join(VALID_STATUSES),
                status=400,
            )

        write_vals = {"status": status}
        note = (params.get("note") or "").strip()
        if note:
            write_vals["status_note"] = note
        record.write(write_vals)

        latest_log = record.iteration_log_ids[:1]
        return return_Response(
            message="Status updated.",
            status=200,
            data={
                "deliverable_id": record.id,
                "status": record.status,
                "iteration": record.iteration,
                "log": _serialize_log(latest_log) if latest_log else None,
            },
        )

    @http.route(
        "/api/v1/client_deliverables_ext/feedback/add",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def client_deliverables_ext_add_feedback(self, **kwargs):
        """Add a dated feedback entry, optionally with a file uploaded to S3.

        Send the file as base64 in 'file' plus a 'file_name'. We store only the
        resulting S3 link, never the bytes.
        """
        env = request.env
        if not _is_allowed(env):
            return _forbidden()

        params = _read_params()
        record, error = _get_deliverable(env, params)
        if error is not None:
            return error

        comment = (params.get("comment") or "").strip()
        if not comment:
            return return_Response(
                message="Missing required parameter 'comment'.", status=400
            )

        feedback_date, error = _parse_date(params.get("feedback_date"))
        if error is not None:
            return error

        file_url = ""
        file_b64 = params.get("file")
        file_name = (params.get("file_name") or "").strip()
        if file_b64:
            if env["s3.connector"].sudo().search_count([]) == 0:
                return return_Response(
                    message="No S3 connector is configured; cannot upload file.",
                    status=400,
                )
            try:
                file_url = generate_s3_link(
                    file_b64,
                    prefix=S3_FEEDBACK_PREFIX,
                    uid=env.user.id,
                    filename=file_name or None,
                )
            except Exception as exc:
                _logger.exception("client_deliverables feedback S3 upload failed")
                return return_Response(
                    message="File upload to S3 failed.",
                    status=400,
                    errors=[str(exc)],
                )
            if not file_url:
                return return_Response(
                    message="File upload to S3 returned no URL.", status=400
                )

        vals = {
            "deliverable_id": record.id,
            "comment": comment,
            "author_id": env.user.id,
            "author_name": env.user.name,
            "file_url": file_url or False,
            "file_name": file_name or False,
        }
        if feedback_date:
            vals["feedback_date"] = feedback_date

        feedback = env["client.deliverable.feedback"].sudo().create(vals)
        return return_Response(
            message="Feedback added.",
            status=200,
            data={
                "feedback": _serialize_feedback(feedback),
                "feedback_count": record.feedback_count,
            },
        )
