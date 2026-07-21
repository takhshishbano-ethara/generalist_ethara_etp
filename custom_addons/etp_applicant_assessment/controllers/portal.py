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
            try:
                assessment.action_submit()
            except Exception:
                _logger.exception(
                    "action_submit failed for assessment=%s token=%s; "
                    "forcing terminal state so candidate never sees 500",
                    assessment.id, token,
                )
                try:
                    assessment.sudo().write({
                        "state": "submitted",
                        "submitted_at": fields.Datetime.now(),
                    })
                except Exception:
                    _logger.exception(
                        "Fallback state write failed for assessment=%s",
                        assessment.id,
                    )
                if assessment.applicant_id:
                    try:
                        assessment.applicant_id.sudo().write({
                            "status": "assessment_pending_review",
                        })
                    except Exception:
                        _logger.exception(
                            "Fallback applicant.status write failed for "
                            "applicant=%s assessment=%s",
                            assessment.applicant_id.id, assessment.id,
                        )
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
        warning = Warning.create({
            "assessment_id": assessment.id,
            "kind": kind,
            "raw_meta_json": json.dumps(meta)[:4000],
            "detector_confidence": float(meta.get("confidence", 0.0) or 0.0),
        })
        return self._json_response({
            "ok": True,
            "warning_id": warning.id,
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
        return self._json_response({"ok": True, "url": url, "s3_key": s3_key})

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
            return self._json_response({"ok": False, "reason": "s3_not_configured"}, 400)

        try:
            max_bytes = int(s3_service._param(env, "proctor_max_clip_bytes", "10485760"))
        except (TypeError, ValueError):
            max_bytes = 10 * 1024 * 1024
        try:
            expires = int(s3_service._param(env, "proctor_presign_expires_seconds", "120"))
        except (TypeError, ValueError):
            expires = 120

        presign = s3_service.generate_presigned_post(
            env, f"proctoring-video/{assessment.id}",
            max_bytes=max_bytes, content_type="video/webm",
            extension="webm", expires=expires,
        )
        if not presign:
            return self._json_response({"ok": False, "reason": "presign_failed"}, 500)
        return self._json_response({"ok": True, "presign": presign})

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/video/commit",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_video_commit(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state not in ("in_progress", "submitted"):
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        env = request.env
        body = self._json_body()

        key = (body.get("key") or "")[:_MAX_KEY_LEN]
        reason = (body.get("reason") or "signal")[:40]
        snapshot_url = (body.get("snapshot_url") or "")[:1024]
        snapshot_key = (body.get("snapshot_key") or "")[:_MAX_KEY_LEN]
        try:
            warning_id_int = int(body.get("warning_id") or 0)
        except (TypeError, ValueError):
            warning_id_int = 0

        folder = s3_service._param(env, "s3_folder", "etp_applicant_assessment") or "etp_applicant_assessment"
        expected_prefix = f"{folder.rstrip('/')}/proctoring-video/{assessment.id}/"
        if not key.startswith(expected_prefix) or len(key) > _MAX_KEY_LEN:
            return self._json_response({"ok": False, "reason": "invalid_key"}, 400)
        if not s3_service.head_object(env, key):
            return self._json_response({"ok": False, "reason": "not_uploaded"}, 400)

        bucket = s3_service._param(env, "s3_bucket", "") or ""
        region = s3_service._param(env, "s3_region", "us-east-1") or "us-east-1"
        clip_url = s3_service._build_url(env, bucket, region, key)

        Warning = env["etp.applicant.assessment.warning"].sudo()
        kind_val = reason if reason in _EVENT_KINDS else "no_face"

        if warning_id_int:
            existing = Warning.search([
                ("id", "=", warning_id_int),
                ("assessment_id", "=", assessment.id),
            ], limit=1)
            if existing:
                update_vals = {"s3_url": clip_url, "s3_key": key}
                if snapshot_url:
                    update_vals["snapshot_url"] = snapshot_url
                if snapshot_key:
                    update_vals["snapshot_key"] = snapshot_key
                existing.write(update_vals)
                return self._json_response({
                    "ok": True,
                    "warning_id": existing.id,
                    "warning_count": assessment.warning_count,
                    "state": assessment.state,
                })

        warning = Warning.create({
            "assessment_id": assessment.id,
            "kind": kind_val,
            "s3_url": clip_url,
            "s3_key": key,
            "snapshot_url": snapshot_url,
            "snapshot_key": snapshot_key,
        })
        return self._json_response({
            "ok": True,
            "warning_id": warning.id,
            "warning_count": assessment.warning_count,
            "state": assessment.state,
        })

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/video/upload",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_video_upload(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment or assessment.state not in ("in_progress", "submitted"):
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        env = request.env

        upload = kw.get("file")
        if not upload:
            return self._json_response({"ok": False, "reason": "no_file"}, 400)
        content_type = (upload.mimetype or "video/webm").lower()
        if content_type not in ("video/webm", "video/mp4"):
            return self._json_response({"ok": False, "reason": "bad_content_type"}, 400)

        try:
            max_bytes = int(s3_service._param(env, "proctor_max_clip_bytes", "10485760"))
        except (TypeError, ValueError):
            max_bytes = 10 * 1024 * 1024

        blob = upload.read()
        if not blob:
            return self._json_response({"ok": False, "reason": "empty"}, 400)
        if len(blob) > max_bytes:
            return self._json_response({"ok": False, "reason": "too_large"}, 413)

        reason = (kw.get("reason") or "signal")[:40]
        snapshot_url = (kw.get("snapshot_url") or "")[:1024]
        snapshot_key = (kw.get("snapshot_key") or "")[:_MAX_KEY_LEN]
        try:
            warning_id_int = int(kw.get("warning_id") or 0)
        except (TypeError, ValueError):
            warning_id_int = 0

        ext = "mp4" if content_type == "video/mp4" else "webm"
        clip_url = key = None
        attachment_id = False
        if s3_service.is_configured(env):
            try:
                clip_url, key = s3_service.upload_bytes(
                    env, blob,
                    key_hint=f"proctoring-video/{assessment.id}",
                    content_type=content_type,
                    extension=ext,
                )
            except Exception:
                _logger.exception(
                    "Server-side clip upload failed for assessment=%s; "
                    "falling back to local attachment storage",
                    assessment.id,
                )
                assessment.record_media_error(
                    "s3-post-failed", 500, "server_fallback_upload_failed",
                )
        if not clip_url:
            # Evidence must never be dropped: without S3 (or when S3
            # errors) the clip is kept as a private attachment on the
            # assessment, mirroring what snapshots already do.
            attachment = env["ir.attachment"].sudo().create({
                "name": (
                    f"proctoring-clip-{assessment.id}-"
                    f"{fields.Datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
                ),
                "res_model": "etp.applicant.assessment",
                "res_id": assessment.id,
                "type": "binary",
                "datas": base64.b64encode(blob),
                "mimetype": content_type,
                "public": False,
            })
            attachment_id = attachment.id
            clip_url = f"/web/content/{attachment.id}?download=false"
            key = ""

        Warning = env["etp.applicant.assessment.warning"].sudo()
        kind_val = reason if reason in _EVENT_KINDS else "no_face"

        if warning_id_int:
            existing = Warning.search([
                ("id", "=", warning_id_int),
                ("assessment_id", "=", assessment.id),
            ], limit=1)
            if existing:
                update_vals = {
                    "s3_url": clip_url,
                    "s3_key": key,
                    "attachment_id": attachment_id,
                }
                if snapshot_url:
                    update_vals["snapshot_url"] = snapshot_url
                if snapshot_key:
                    update_vals["snapshot_key"] = snapshot_key
                existing.write(update_vals)
                return self._json_response({
                    "ok": True,
                    "warning_id": existing.id,
                    "warning_count": assessment.warning_count,
                    "state": assessment.state,
                })

        warning = Warning.create({
            "assessment_id": assessment.id,
            "kind": kind_val,
            "s3_url": clip_url,
            "s3_key": key,
            "attachment_id": attachment_id,
            "snapshot_url": snapshot_url,
            "snapshot_key": snapshot_key,
        })
        return self._json_response({
            "ok": True,
            "warning_id": warning.id,
            "warning_count": assessment.warning_count,
            "state": assessment.state,
        })

    @http.route(
        "/applicant-assessment/<string:token>/proctoring/media-error",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def proctoring_media_error(self, token, **kw):
        assessment = self._get_assessment(token)
        if not assessment:
            return self._json_response({"ok": False, "reason": "invalid_state"}, 400)
        body = self._json_body()
        step = (body.get("step") or "")[:64]
        message = (body.get("message") or "")[:200]
        try:
            status_code = int(body.get("status") or 0)
        except (TypeError, ValueError):
            status_code = 0
        if step:
            try:
                assessment.record_media_error(step, status_code, message)
            except Exception:
                _logger.exception("record_media_error failed for %s", assessment.id)
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

        env = request.env
        if not s3_service.is_configured(env):
            return self._json_response({"ok": False, "reason": "s3_not_configured"}, 400)

        body = self._json_body()
        try:
            qid = int(body.get("question_id") or 0)
        except (TypeError, ValueError):
            qid = 0
        question = assessment.question_ids.filtered(lambda q: q.id == qid)
        if not question or question.question_type != "file_upload":
            return self._json_response({"ok": False, "reason": "unknown_question"}, 400)

        mime = (body.get("mime") or "application/octet-stream")[:120]
        if mime not in _ANSWER_MIME_ALLOWLIST:
            return self._json_response({"ok": False, "reason": "mime_not_allowed"}, 400)

        try:
            size = int(body.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0 or size > _MAX_ANSWER_FILE_BYTES:
            return self._json_response({"ok": False, "reason": "size_invalid"}, 400)

        try:
            expires = int(s3_service._param(env, "answer_presign_expires_seconds", "300"))
        except (TypeError, ValueError):
            expires = 300

        ext = (mime.rsplit("/", 1)[-1] or "bin").split("+", 1)[0][:12] or "bin"
        presign = s3_service.generate_presigned_post(
            env, f"answer-file/{assessment.id}/{question.id}",
            max_bytes=_MAX_ANSWER_FILE_BYTES, content_type=mime,
            extension=ext, expires=expires,
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
        folder = s3_service._param(env, "s3_folder", "etp_applicant_assessment") or "etp_applicant_assessment"
        expected_prefix = f"{folder.rstrip('/')}/answer-file/{assessment.id}/{question.id}/"
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
