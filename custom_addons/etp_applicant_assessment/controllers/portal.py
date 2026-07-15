import base64
import json
import logging
from datetime import datetime

from odoo import fields, http
from odoo.http import request

from ..models._common import MCQ_TYPES, OPTION_STORAGE_TYPES
from ..services import s3_service

_logger = logging.getLogger(__name__)


_EVENT_KINDS = {
    "window_change", "tab_switch", "fullscreen_exit", "copy_paste",
    "no_face", "other_person", "mobile_phone", "lip_movement", "look_away",
}
_CLIP_EVENT_KINDS = {
    "no_face", "other_person", "mobile_phone", "lip_movement", "look_away",
}
_SNAPSHOT_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_KEY_LEN = 240
_MAX_ANSWER_FILE_BYTES = 25 * 1024 * 1024
_ANSWER_MIME_ALLOWLIST = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "application/zip",
    "image/png",
    "image/jpeg",
}


class ApplicantAssessmentPortal(http.Controller):

    def _get_assessment(self, token):
        if not token:
            return None
        return (
            request.env["etp.applicant.assessment"]
            .sudo()
            .search([("access_token", "=", token)], limit=1)
        )

    def _render_state(self, assessment):
        state = assessment.state
        if state == "cancelled":
            return request.render(
                "etp_applicant_assessment.portal_cancelled",
                {"assessment": assessment},
            )
        if state in ("submitted", "scored"):
            return request.render(
                "etp_applicant_assessment.portal_completed",
                {"assessment": assessment},
            )
        if assessment._check_time_expired() and state != "sent":
            assessment.action_submit()
            return request.render(
                "etp_applicant_assessment.portal_expired",
                {"assessment": assessment},
            )
        if state == "sent":
            return request.render(
                "etp_applicant_assessment.portal_instructions",
                {
                    "assessment": assessment,
                    "user_lang": request.env.lang or "en_US",
                },
            )
        return request.render(
            "etp_applicant_assessment.portal_test",
            {
                "assessment": assessment,
                "user_lang": request.env.lang or "en_US",
            },
        )

    @http.route(
        "/applicant-assessment/<string:token>",
        type="http", auth="public", website=True, methods=["GET"],
        csrf=False, sitemap=False,
    )
    def landing(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment:
            return request.render(
                "etp_applicant_assessment.portal_invalid_token", {},
            )
        return self._render_state(assessment)

    @http.route(
        "/applicant-assessment/<string:token>/begin",
        type="http", auth="public", website=True, methods=["POST"], csrf=True,
    )
    def begin(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state not in ("sent", "in_progress"):
            return request.redirect(f"/applicant-assessment/{token}")
        assessment.action_begin()
        return request.redirect(f"/applicant-assessment/{token}")

    def _json_body(self):
        try:
            raw = request.httprequest.get_data(as_text=True) or "{}"
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    def _json_response(self, payload, status=200):
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json")],
            status=status,
        )

    @http.route(
        "/applicant-assessment/<string:token>/answer",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def answer(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        if assessment._check_time_expired():
            assessment.action_submit()
            return self._json_response({"ok": False, "reason": "expired"}, 400)

        body = self._json_body()
        question_id = body.get("question_id")
        if not question_id:
            return self._json_response({"ok": False, "reason": "missing_question"}, 400)

        question = assessment.question_ids.filtered(lambda q: q.id == int(question_id))
        if not question:
            return self._json_response({"ok": False, "reason": "unknown_question"}, 400)

        Answer = request.env["etp.applicant.assessment.answer"].sudo()
        existing = Answer.search([
            ("assessment_id", "=", assessment.id),
            ("question_id", "=", question.id),
        ], limit=1)

        vals = {
            "assessment_id": assessment.id,
            "question_id": question.id,
            "submitted_at": fields.Datetime.now(),
        }
        if question.question_type in OPTION_STORAGE_TYPES:
            option_ids = body.get("option_ids") or []
            valid_ids = set(question.option_ids.ids)
            clean = [int(o) for o in option_ids if int(o) in valid_ids]
            if question.question_type in ("mcq_single", "dropdown", "true_false", "rating"):
                clean = clean[:1]
            vals["selected_option_ids"] = [(6, 0, clean)]
            vals["text_answer"] = False
        else:
            text = (body.get("text_answer") or "").strip()
            vals["text_answer"] = text
            vals["selected_option_ids"] = [(5, 0, 0)]

        if existing:
            existing.write(vals)
        else:
            Answer.create(vals)

        return self._json_response({"ok": True})

    @http.route(
        "/applicant-assessment/<string:token>/submit",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def submit(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment:
            return request.redirect(f"/applicant-assessment/{token}")
        if assessment.state in ("sent", "in_progress"):
            assessment.action_submit()
        return request.redirect(f"/applicant-assessment/{token}")

    @http.route(
        "/applicant-assessment/<string:token>/event",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def event(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)

        body = self._json_body()
        kind = body.get("kind")
        if kind not in _EVENT_KINDS:
            return self._json_response({"ok": False, "reason": "bad_kind"}, 400)

        meta = body.get("meta") or {}
        Warning = request.env["etp.applicant.assessment.warning"].sudo()
        Warning.create({
            "assessment_id": assessment.id,
            "kind": kind,
            "raw_meta_json": json.dumps(meta)[:4000],
            "detector_confidence": float(meta.get("confidence", 0.0) or 0.0),
        })
        return self._json_response({
            "ok": True,
            "warning_count": assessment.warning_count,
            "state": assessment.state,
        })

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/consent",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_consent(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        body = self._json_body()
        consent = assessment.record_consent(version=body.get("version") or "v1")
        return self._json_response({
            "ok": True,
            "consent": {
                "at": consent.get("at").isoformat() if consent.get("at") else None,
                "version": consent.get("version") or "",
            },
        })

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/snapshot",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_snapshot(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        upload = kw.get("file")
        if not upload:
            return self._json_response({"ok": False, "reason": "no_file"}, 400)
        content_type = (upload.mimetype or "").lower()
        if content_type not in _SNAPSHOT_CONTENT_TYPES:
            return self._json_response({"ok": False, "reason": "bad_content_type"}, 400)
        blob = upload.read()
        if not blob:
            return self._json_response({"ok": False, "reason": "empty"}, 400)
        if len(blob) > _MAX_SNAPSHOT_BYTES:
            return self._json_response({"ok": False, "reason": "too_large"}, 413)

        env = request.env
        reason = (kw.get("reason") or "periodic")[:40]
        url, s3_key, attachment_id = "", "", False

        if s3_service.is_configured(env):
            ext = _ext_from_content_type(content_type)
            try:
                url, s3_key = s3_service.upload_bytes(
                    env, blob,
                    key_hint=f"snapshot/{assessment.id}",
                    content_type=content_type,
                    extension=ext,
                )
            except Exception:
                _logger.exception("Snapshot S3 upload failed for assessment %s", assessment.id)
                assessment.record_media_error("snapshot-not-stored", 500, "s3_upload_failed")
                return self._json_response({"ok": False, "reason": "storage_failed"}, 502)
        else:
            attachment = env["ir.attachment"].sudo().create({
                "name": f"proctoring-{assessment.id}-{fields.Datetime.now().strftime('%Y%m%d%H%M%S%f')}.{_ext_from_content_type(content_type)}",
                "res_model": "etp.applicant.assessment",
                "res_id": assessment.id,
                "type": "binary",
                "datas": base64.b64encode(blob),
                "mimetype": content_type,
                "public": False,
            })
            attachment_id = attachment.id
            url = f"/web/content/{attachment.id}?download=false"

        env["etp.applicant.assessment.snapshot"].sudo().create({
            "assessment_id": assessment.id,
            "reason": reason,
            "url": url,
            "s3_key": s3_key,
            "attachment_id": attachment_id,
            "captured_at": fields.Datetime.now(),
        })
        return self._json_response({"ok": True, "url": url})

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/video/presign",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_video_presign(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        env = request.env
        if not s3_service.is_configured(env):
            return self._json_response({"ok": True, "presign": None})
        ICP = env["ir.config_parameter"].sudo()
        try:
            max_bytes = int(ICP.get_param(
                "etp_applicant_assessment.proctor_max_clip_bytes", "10485760"))
            expires = int(ICP.get_param(
                "etp_applicant_assessment.proctor_presign_expires_seconds", "120"))
        except (TypeError, ValueError):
            max_bytes = 10 * 1024 * 1024
            expires = 120
        presign = s3_service.generate_presigned_post(
            env,
            key_prefix=f"proctoring-video/{assessment.id}",
            max_bytes=max_bytes,
            content_type="video/webm",
            extension="webm",
            expires=expires,
        )
        if not presign:
            return self._json_response({"ok": True, "presign": None})
        return self._json_response({"ok": True, "presign": presign})

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/video/commit",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_video_commit(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        body = self._json_body()
        key = (body.get("key") or "").strip()
        reason = (body.get("reason") or "signal")[:40]
        if reason not in _EVENT_KINDS and reason not in {"signal", "manual"}:
            reason = "signal"
        env = request.env
        folder = (
            env["ir.config_parameter"].sudo()
            .get_param("etp_applicant_assessment.s3_folder",
                       "etp_applicant_assessment").rstrip("/")
        )
        prefix = f"{folder}/proctoring-video/{assessment.id}/"
        if not key.startswith(prefix) or len(key) > _MAX_KEY_LEN:
            return self._json_response({"ok": False, "reason": "invalid_key"}, 400)
        if not s3_service.head_object(env, key):
            return self._json_response({"ok": False, "reason": "not_landed"}, 400)

        bucket = env["ir.config_parameter"].sudo().get_param(
            "etp_applicant_assessment.s3_bucket", "")
        region = env["ir.config_parameter"].sudo().get_param(
            "etp_applicant_assessment.s3_region", "us-east-1")
        cdn = env["ir.config_parameter"].sudo().get_param(
            "etp_applicant_assessment.s3_cdn_url", "").rstrip("/")
        clip_url = (
            f"{cdn}/{key}" if cdn
            else f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
        )

        snapshot_url = (body.get("snapshot_url") or "")[:512]
        snapshot_key = (body.get("snapshot_key") or "")[:_MAX_KEY_LEN]
        confidence = 0.0
        try:
            confidence = float(body.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        Warning = env["etp.applicant.assessment.warning"].sudo()
        warning_kind = reason if reason in _EVENT_KINDS else "no_face"
        Warning.create({
            "assessment_id": assessment.id,
            "kind": warning_kind,
            "s3_url": clip_url,
            "s3_key": key,
            "snapshot_url": snapshot_url,
            "snapshot_key": snapshot_key,
            "detector_confidence": confidence,
            "raw_meta_json": json.dumps(body.get("meta") or {})[:4000],
        })
        return self._json_response({
            "ok": True,
            "warning_count": assessment.warning_count,
            "state": assessment.state,
        })

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/media-error",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_media_error(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        body = self._json_body()
        step = (body.get("step") or "")[:40]
        message = body.get("message") or ""
        status_code = body.get("status")
        assessment.record_media_error(step, status_code=status_code, message=message)
        return self._json_response({"ok": True})

    @http.route(
        "/applicant-assessment/<string:token>/answer/file/presign",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def answer_file_presign(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        if assessment._check_time_expired():
            return self._json_response({"ok": False, "reason": "expired"}, 400)

        body = self._json_body()
        try:
            qid = int(body.get("question_id") or 0)
        except (TypeError, ValueError):
            qid = 0
        question = assessment.question_ids.filtered(lambda q: q.id == qid)
        if not question or question.question_type != "file_upload":
            return self._json_response({"ok": False, "reason": "unknown_question"}, 400)

        mime = (body.get("mime") or "")[:120]
        if mime not in _ANSWER_MIME_ALLOWLIST:
            return self._json_response({"ok": False, "reason": "mime_not_allowed"}, 400)

        env = request.env
        if not s3_service.is_configured(env):
            return self._json_response({"ok": False, "reason": "s3_not_configured"}, 400)

        folder = s3_service._param(env, "etp_applicant_assessment.s3_folder", "") or ""
        prefix_parts = [p for p in [folder, "answer-file", str(assessment.id), str(question.id)] if p]
        key_prefix = "/".join(prefix_parts) + "/"

        try:
            expires = int(s3_service._param(
                env, "etp_applicant_assessment.answer_presign_expires_seconds", "300"))
        except (TypeError, ValueError):
            expires = 300

        presign = s3_service.generate_presigned_post(
            env, key_prefix, max_bytes=_MAX_ANSWER_FILE_BYTES, expires=expires,
        )
        if not presign:
            return self._json_response({"ok": False, "reason": "presign_failed"}, 500)
        return self._json_response({"ok": True, "presign": presign})

    @http.route(
        "/applicant-assessment/<string:token>/answer/file/commit",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def answer_file_commit(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state != "in_progress":
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        if assessment._check_time_expired():
            return self._json_response({"ok": False, "reason": "expired"}, 400)

        body = self._json_body()
        try:
            qid = int(body.get("question_id") or 0)
        except (TypeError, ValueError):
            qid = 0
        question = assessment.question_ids.filtered(lambda q: q.id == qid)
        if not question or question.question_type != "file_upload":
            return self._json_response({"ok": False, "reason": "unknown_question"}, 400)

        env = request.env
        key = (body.get("storage_key") or "")[:_MAX_KEY_LEN]
        folder = s3_service._param(env, "etp_applicant_assessment.s3_folder", "") or ""
        expected_prefix_parts = [p for p in [folder, "answer-file", str(assessment.id), str(question.id)] if p]
        expected_prefix = "/".join(expected_prefix_parts) + "/"
        if not key.startswith(expected_prefix) or len(key) > _MAX_KEY_LEN:
            return self._json_response({"ok": False, "reason": "invalid_key"}, 400)
        if not s3_service.head_object(env, key):
            return self._json_response({"ok": False, "reason": "not_uploaded"}, 400)

        filename = (body.get("filename") or "attachment")[:200]
        filename = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        mime = (body.get("mime") or "application/octet-stream")[:120]
        if mime not in _ANSWER_MIME_ALLOWLIST:
            return self._json_response({"ok": False, "reason": "mime_not_allowed"}, 400)

        s3_url = body.get("s3_url") or ""
        if len(s3_url) > 1024:
            return self._json_response({"ok": False, "reason": "url_too_long"}, 400)

        Answer = env["etp.applicant.assessment.answer"].sudo()
        answer = Answer.search([
            ("assessment_id", "=", assessment.id),
            ("question_id", "=", question.id),
        ], limit=1)
        if not answer:
            answer = Answer.create({
                "assessment_id": assessment.id,
                "question_id": question.id,
                "submitted_at": fields.Datetime.now(),
            })

        Attachment = env["ir.attachment"].sudo()
        attachment = Attachment.create({
            "name": filename,
            "type": "url",
            "url": s3_url,
            "mimetype": mime,
            "res_model": "etp.applicant.assessment.answer",
            "res_id": answer.id,
        })
        answer.write({
            "answer_attachment_ids": [(4, attachment.id)],
            "submitted_at": fields.Datetime.now(),
        })

        return self._json_response({
            "ok": True,
            "attachment_id": attachment.id,
            "name": filename,
        })


def _ext_from_content_type(content_type):
    if content_type == "image/jpeg":
        return "jpg"
    if content_type == "image/png":
        return "png"
    if content_type == "image/webp":
        return "webp"
    return "bin"
