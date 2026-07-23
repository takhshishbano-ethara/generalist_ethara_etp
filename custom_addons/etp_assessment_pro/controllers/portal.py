# -*- coding: utf-8 -*-
"""Candidate-facing exam portal — single-sitting exam routes."""
import json
import logging
import re
from urllib.parse import quote

from psycopg2 import IntegrityError

from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)

# Hard cap on any candidate-supplied free-text answer BEFORE it is stored or
# ever reaches the LLM scorer. A subjective answer / per-box label is prose, not
# a document; 8000 chars is far more than any honest response and stops a
# candidate stuffing megabytes of CJK into `justification` to run up the Vertex
# bill (each char ~1 token; the scorer re-sends the whole thing, and MAX_TOKENS
# retries double it). Long-form answers past this are truncated, not rejected,
# so an honest candidate never loses their attempt.
_JUSTIFICATION_MAX_LEN = 8000
# Per-box cap for image_label annotations. Coverage scoring only counts filled
# boxes, so a box label needs no more than a short phrase; this bounds the
# serialized label JSON regardless of how many boxes an image has.
_LABEL_VALUE_MAX_LEN = 500


def _rules_json(assessment, evaluator=None):
    """Serialize proctoring rules + violation policy as JSON for inline JS.
    max_violations 0 means the first violation ends the exam, matching
    _record_violation_single's ``not cap`` branch.
    """
    watermark_label = ""
    if evaluator and assessment.rule_watermark:
        applicant = evaluator.applicant_id
        parts = [applicant.partner_name or applicant.display_name or ""]
        if applicant.email_from:
            parts.append(applicant.email_from)
        watermark_label = "  ".join(p for p in parts if p)
    return json.dumps({
        "tab_switch": bool(assessment.rule_block_tab_switch),
        "copy_paste": bool(assessment.rule_block_copy_paste),
        "right_click": bool(assessment.rule_block_right_click),
        "devtools": bool(assessment.rule_block_devtools),
        "screenshot": bool(assessment.rule_block_screenshot),
        "fullscreen": bool(assessment.rule_fullscreen),
        "webcam": bool(assessment.rule_webcam),
        "watermark": bool(assessment.rule_watermark),
        "watermark_label": watermark_label,
        "max_violations": int(assessment.max_violations or 0),
        "violation_action": assessment.violation_action or "log_only",
        "violation_count": int(evaluator.violation_count or 0) if evaluator else 0,
    })


def _deadline_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def _ideal_labels_count(value):
    """How many labelling boxes an image_label ``ideal_labels`` answer key
    implies — used to synthesise one "Label N" input per numbered box when the
    boxes are baked into the rendered image and no detections_json exists.

    ``value`` may be a Python list, a JSON-array/-object string, or free text
    with entries separated by ';' and/or newlines (optionally "Box N = ..."
    segments). Returns the count of distinct non-empty entries, capped to a
    sane 0..20 render range; 0 for empty/None (keeps the textarea fallback)."""
    if not value:
        return 0
    if isinstance(value, str):
        raw = value.strip()
        if raw[:1] in ("[", "{"):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                value = parsed
            elif isinstance(parsed, dict):
                value = list(parsed.values())
    if isinstance(value, (list, tuple)):
        n = sum(1 for v in value if str(v or "").strip())
        return max(0, min(n, 20))
    if not isinstance(value, str):
        return 0
    text = value.strip()
    if not text:
        return 0
    box_nums = re.findall(r"box\s*(\d+)", text, flags=re.IGNORECASE)
    if box_nums:
        n = len({int(x) for x in box_nums})
    else:
        n = sum(1 for seg in re.split(r"[;\n]+", text) if seg.strip())
    return max(0, min(n, 20))


class EtpAssessmentPortal(http.Controller):

    def _get_evaluator_from_token(self, token):
        if not token:
            return False
        return request.env["etp.assessment.pro.evaluator"].sudo().search(
            [("access_token", "=", token)], limit=1)

    def _candidate_guard(self, evaluator, assessment):
        applicant = evaluator.applicant_id
        candidate_user = evaluator._candidate_user()
        current = request.env.user
        login_url = "/web/session/logout?redirect=" + quote(
            "/web/login?redirect=" + request.httprequest.path, safe="")
        if candidate_user:
            if current.id != candidate_user.id:
                return request.render(
                    "etp_assessment_pro.portal_wrong_candidate",
                    {"assessment": assessment, "candidate": applicant,
                     "current_user": current, "login_url": login_url})
            return False
        if not current.has_group("etp_assessment_pro.group_assessment_manager"):
            return request.render(
                "etp_assessment_pro.portal_wrong_candidate",
                {"assessment": assessment, "candidate": applicant,
                 "current_user": current, "login_url": login_url})
        return False

    def _guard_or_abort(self, evaluator, assessment):
        if request.env.user._is_public():
            return request.redirect("/web/login")
        return self._candidate_guard(evaluator, assessment)

    def _is_real_candidate(self, evaluator):
        candidate_user = evaluator._candidate_user()
        return bool(candidate_user) and request.env.user.id == candidate_user.id

    @http.route("/pro_assessment/<string:token>", type="http",
                auth="public", website=True)
    def assessment_landing(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        if request.env.user._is_public():
            return request.redirect(
                "/web/login?redirect=/pro_assessment/%s" % token)
        block = self._candidate_guard(evaluator, evaluator.assessment_id)
        if block:
            return block
        assessment = evaluator.assessment_id
        if evaluator.is_locked or evaluator.state == "submitted":
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator})
        if assessment.state != "in_progress":
            return request.render(
                "etp_assessment_pro.portal_assessment_closed",
                {"assessment": assessment, "evaluator": evaluator})
        if not evaluator.started_at:
            return request.render(
                "etp_assessment_pro.portal_instructions",
                {"assessment": assessment, "evaluator": evaluator,
                 "token": token,
                 "duration_minutes": assessment.duration_minutes,
                 "submit_url": f"/pro_assessment/{token}/begin",
                 "preview": not self._is_real_candidate(evaluator)})
        if evaluator.is_time_expired():
            self._auto_submit_remaining_single(evaluator)
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator})
        return self._serve_question(
            evaluator=evaluator, token=token,
            base="/pro_assessment/%s" % token, requested_q=kw.get("q"))

    @http.route("/pro_assessment/<string:token>/begin", type="http",
                auth="public", website=True, methods=["POST"], csrf=True)
    def assessment_begin(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            evaluator, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked or evaluator.assessment_id.state != "in_progress":
            return request.redirect("/pro_assessment/%s" % token)
        if not self._is_real_candidate(evaluator):
            return request.redirect("/pro_assessment/%s" % token)
        if not evaluator.started_at:
            evaluator.write({"started_at": fields.Datetime.now()})
        if evaluator.state == "pending":
            evaluator.write({"state": "in_progress"})
        return request.redirect(f"/pro_assessment/{token}")

    @http.route("/pro_assessment/<string:token>/submit", type="http",
                auth="public", website=True, methods=["POST"], csrf=True)
    def assessment_submit_response(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            evaluator, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked \
                or evaluator.assessment_id.state != "in_progress":
            return request.redirect(f"/pro_assessment/{token}")
        if evaluator.is_time_expired():
            self._auto_submit_remaining_single(evaluator)
            return request.redirect(f"/pro_assessment/{token}")
        if not self._is_real_candidate(evaluator):
            return request.redirect(f"/pro_assessment/{token}")
        self._record_response(evaluator=evaluator, form=kw)
        if evaluator.state == "pending":
            evaluator.write({"state": "in_progress"})
        target = self._next_index(
            json.loads(evaluator.question_order or "[]"),
            current=kw.get("question_id"), nav=kw.get("nav") or "next")
        if target is None:
            return request.redirect(f"/pro_assessment/{token}/review")
        return request.redirect(f"/pro_assessment/{token}?q={target}")

    @http.route("/pro_assessment/<string:token>/review", type="http",
                auth="public", website=True)
    def assessment_review_single(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        if request.env.user._is_public():
            return request.redirect(
                "/web/login?redirect=/pro_assessment/%s/review" % token)
        block = self._candidate_guard(evaluator, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked or evaluator.state == "submitted":
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": evaluator.assessment_id,
                 "evaluator": evaluator})
        return self._render_review(
            evaluator=evaluator, token=token,
            base="/pro_assessment/%s" % token,
            finish_url=f"/pro_assessment/{token}/finish",
            blocked=bool(kw.get("incomplete")))

    @http.route("/pro_assessment/<string:token>/answers", type="http",
                auth="public", website=True)
    def assessment_answers_single(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        if request.env.user._is_public():
            return request.redirect(
                "/web/login?redirect=/pro_assessment/%s/answers" % token)
        block = self._candidate_guard(evaluator, evaluator.assessment_id)
        if block:
            return block
        if not ((evaluator.is_locked or evaluator.state == "submitted")
                and evaluator.results_released):
            return request.redirect("/pro_assessment/%s" % token)
        return request.render(
            "etp_assessment_pro.portal_assessment_answers",
            {"assessment": evaluator.assessment_id, "evaluator": evaluator,
             "review_rows": self._build_answer_review(evaluator),
             "back_url": "/pro_assessment/%s" % token})

    def _build_answer_review(self, evaluator):
        order = json.loads(evaluator.question_order or "[]")
        questions = request.env["etp.assessment.pro.question"].sudo() \
            .browse(order).exists()
        responses = request.env["etp.assessment.pro.response"].sudo().search([
            ("assessment_evaluator_id", "=", evaluator.id),
            ("question_id", "in", questions.ids)])
        by_q = {r.question_id.id: r for r in responses}
        badge_map = {"pass": "text-bg-success", "fail": "text-bg-danger",
                     "info": "text-bg-secondary"}
        rows = []
        for i, q in enumerate(questions):
            r = by_q.get(q.id)
            qtype = q.question_type
            chosen = r.line_ids.mapped("selected_option_id").mapped("name") \
                if r else []
            justification = (r.justification or "") if r else ""
            correct, score_raw = [], None
            if qtype in ("mcq", "msq"):
                correct = q.question_dimension_ids.mapped("option_line_ids") \
                    .filtered("is_correct").mapped("name")
                if not r:
                    verdict, kind = "Not answered", "info"
                elif r.max_score and r.score >= r.max_score:
                    verdict, kind = "Correct", "pass"
                else:
                    verdict, kind = "Incorrect", "fail"
            else:
                if not r or not (justification or chosen):
                    verdict, kind = "Not answered", "info"
                elif r.llm_state == "scored":
                    verdict = "Passed" if r.llm_passed else "Failed"
                    kind = "pass" if r.llm_passed else "fail"
                    # Raw grader score as a 0-1 decimal (e.g. 0.667), matching the
                    # admin 'Raw Score' convention - NOT a 0-100 percentage.
                    score_raw = round(r.llm_raw_score or 0.0, 3)
                elif r.auto_submitted:
                    # Auto-submitted placeholder from a time-expired sitting: it is
                    # never sent to the grader, so it has no grade (not 'awaiting').
                    verdict, kind = "No Grade", "info"
                else:
                    verdict, kind = "Awaiting grading", "info"
            rows.append({
                "index": i + 1,
                "name": q.name or "Question %s" % (i + 1),
                "prompt": q.prompt or q.name or "",
                "type": qtype,
                "chosen": chosen,
                "correct": correct,
                "justification": justification,
                "verdict": verdict,
                "badge": badge_map.get(kind, "text-bg-secondary"),
                "score_raw": score_raw,
                # Candidate-facing grader feedback for subjective/graded answers.
                # Only surfaced once results are released (the /answers route already
                # gates on results_released) and only for a genuinely scored answer,
                # so an un-graded or objective row shows no empty feedback block.
                "feedback": (
                    (r.llm_feedback or "").strip()
                    if r and qtype not in ("mcq", "msq")
                    and r.llm_state == "scored"
                    else ""),
            })
        return rows

    def _unanswered_question_ids(self, evaluator):
        """Question ids in this attempt with no submitted response yet."""
        order = json.loads(evaluator.question_order or "[]")
        answered = self._answered_question_ids(evaluator)
        return [qid for qid in order if qid not in answered]

    @http.route("/pro_assessment/<string:token>/finish", type="http",
                auth="public", website=True, methods=["POST"], csrf=True)
    def assessment_finish_single(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(evaluator, evaluator.assessment_id)
        if block:
            return block
        # Only the assigned candidate may finish their own attempt (mirrors /begin, /submit).
        if not self._is_real_candidate(evaluator):
            return request.redirect(f"/pro_assessment/{token}")
        if not evaluator.is_locked and evaluator.state != "submitted":
            if (not evaluator.is_time_expired()
                    and self._unanswered_question_ids(evaluator)):
                return request.redirect(
                    f"/pro_assessment/{token}/review?incomplete=1")
            self._auto_submit_remaining_single(evaluator)
        return request.redirect(f"/pro_assessment/{token}")

    @http.route("/pro_assessment/<string:token>/violation", type="http",
                auth="public", website=True, methods=["POST"], csrf=True)
    def assessment_violation_single(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            evaluator, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked:
            return request.redirect(f"/pro_assessment/{token}")
        # Only the assigned candidate may record a violation (mirrors /begin, /submit).
        if not self._is_real_candidate(evaluator):
            return request.redirect(f"/pro_assessment/{token}")
        reason = (kw.get("violation_reason") or "Unknown violation")[:240]
        self._record_violation_single(evaluator, reason)
        return request.redirect(f"/pro_assessment/{token}")

    def _serve_image(self, image, annotated=False):
        """Serve one question image. Prefer a 302 redirect to a short-lived
        presigned S3 URL so the browser streams straight from S3 and no worker
        is occupied for the whole transfer (same pattern as ``_serve_video``).
        Only if signing is unavailable does it fall back to proxying the bytes
        through the worker (private-bucket safe), and a small on-record Binary
        is the dev fallback."""
        binary_field = "annotated_image" if annotated else "image"
        binary = image.annotated_image if annotated else image.image
        external_url = image.annotated_image_url if annotated else image.image_url
        if binary:
            return request.env["ir.binary"]._get_image_stream_from(
                image, field_name=binary_field).get_response()
        if external_url:
            from ..services import s3_service
            key = s3_service.object_key_from_url(request.env, external_url)
            if key:
                signed = s3_service.presigned_url(request.env, key)
                if signed:
                    return request.redirect(signed, code=302, local=False)
                data, ctype = s3_service.download(request.env, key)
                if data is not None:
                    return request.make_response(data, headers=[
                        ("Content-Type", ctype or "image/png"),
                        ("Cache-Control", "private, max-age=3600"),
                    ])
            return request.redirect(external_url, code=302, local=False)
        return request.not_found()

    @http.route("/pro_assessment/qimage/<string:token>/<int:image_id>",
                type="http", auth="public", website=True)
    def serve_question_image(self, token, image_id, **kw):
        """Serve an image-evaluation question image scoped by the exam token."""
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.not_found()
        assessment = evaluator.assessment_id
        order_raw = evaluator.question_order
        if self._candidate_guard(evaluator, assessment):
            return request.not_found()
        try:
            order = set(json.loads(order_raw or "[]"))
        except (TypeError, ValueError):
            order = set()
        image = request.env["etp.assessment.pro.question.image"].sudo().browse(
            image_id)
        if not image.exists() or image.question_id.id not in order:
            return request.not_found()
        return self._serve_image(image, bool(kw.get("annotated")))

    @http.route("/etp_assessment/admin_qimage/<int:image_id>",
                type="http", auth="user")
    def serve_admin_question_image(self, image_id, **kw):
        """Internal, ACL-checked proxy so backend admins can preview a
        private-S3 question image (raw S3 URLs 403). Mirrors the candidate
        ``serve_question_image`` serving logic but gates on the internal user's
        model/record read access instead of an exam token."""
        # Portal candidates must NEVER reach this backend route: they carry a
        # broad base.group_portal read grant on question.image with no record
        # rule, so check_access("read") alone would pass for ANY image_id and
        # leak question content across assessments (bypassing the token +
        # question_order scoping the candidate route enforces). Restrict to
        # internal/backend users; check_access stays as the second layer.
        if not request.env.user._is_internal():
            return request.not_found()
        image = request.env["etp.assessment.pro.question.image"].browse(
            image_id)
        if not image.exists():
            return request.not_found()
        try:
            # ACL gate: raises AccessError unless this user may read the record.
            image.check_access("read")
        except Exception:  # noqa: BLE001 - never leak existence on denial
            return request.not_found()
        return self._serve_image(image.sudo(), bool(kw.get("annotated")))

    def _serve_video(self, video):
        """Stream one question video. Prefer a 302 redirect to video_url so a
        large clip is served straight from the CDN/S3 and never pulled whole
        through the worker (images do the same via a presigned redirect in
        _serve_image; s3_service.download is only their fallback). Only a small
        on-record Binary dev fallback is streamed here, via ir.binary whose
        Stream honours Range requests (Accept-Ranges) so the
        <video> element can seek."""
        if video.video_url:
            # Private-bucket safe: sign the S3 object (like _serve_image). A
            # non-S3/CDN url yields no key, so fall back to the raw url. An mp4
            # is never proxied through the worker (no s3_service.download here).
            from ..services import s3_service
            key = s3_service.object_key_from_url(request.env, video.video_url)
            if key:
                signed = s3_service.presigned_url(request.env, key)
                if signed:
                    return request.redirect(signed, code=302, local=False)
            return request.redirect(video.video_url, code=302, local=False)
        if video.video:
            mimetype = "video/webm" if (video.video_filename or "").lower(
            ).endswith(".webm") else "video/mp4"
            return request.env["ir.binary"]._get_stream_from(
                video, field_name="video", mimetype=mimetype).get_response(
                    as_attachment=False)
        return request.not_found()

    @http.route("/pro_assessment/qvideo/<string:token>/<int:video_id>",
                type="http", auth="public", website=True)
    def serve_question_video(self, token, video_id, **kw):
        """Serve a video-evaluation question clip scoped by the exam token."""
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.not_found()
        assessment = evaluator.assessment_id
        if self._candidate_guard(evaluator, assessment):
            return request.not_found()
        try:
            order = set(json.loads(evaluator.question_order or "[]"))
        except (TypeError, ValueError):
            order = set()
        video = request.env["etp.assessment.pro.question.video"].sudo().browse(
            video_id)
        if not video.exists() or video.question_id.id not in order:
            return request.not_found()
        return self._serve_video(video)

    @http.route("/etp_assessment/admin_qvideo/<int:video_id>",
                type="http", auth="user")
    def serve_admin_question_video(self, video_id, **kw):
        """Internal, ACL-checked proxy so backend admins can preview a
        private-S3 question video. Mirrors serve_admin_question_image: gates on
        the internal user's record read access, then reuses _serve_video."""
        # Same IDOR guard as serve_admin_question_image: portal candidates hold
        # a base.group_portal read grant on question.video with no record rule,
        # so check_access("read") alone would leak any clip by integer id.
        # Backend users only; check_access remains the second layer.
        if not request.env.user._is_internal():
            return request.not_found()
        video = request.env["etp.assessment.pro.question.video"].browse(
            video_id)
        if not video.exists():
            return request.not_found()
        try:
            video.check_access("read")
        except Exception:  # noqa: BLE001 - never leak existence on denial
            return request.not_found()
        return self._serve_video(video.sudo())

    def _serve_draft_video(self, draft, slot):
        """Serve one staged clip from a video_prompt DRAFT's video_files_json.
        Prefer a presigned 302 (private S3) like _serve_video; else stream the
        small base64 dev-fallback. not_found when that slot has no clip."""
        spec = next((f for f in draft._video_files()
                     if (f.get("slot") or "single") == slot), None)
        if not spec:
            return request.not_found()
        url = spec.get("url")
        if url:
            from ..services import s3_service
            key = s3_service.object_key_from_url(request.env, url)
            if key:
                signed = s3_service.presigned_url(request.env, key)
                if signed:
                    return request.redirect(signed, code=302, local=False)
            return request.redirect(url, code=302, local=False)
        data = spec.get("data")
        if isinstance(data, str) and data.startswith("data:"):
            import base64 as _b64
            try:
                raw = _b64.b64decode(data.split(",", 1)[1])
            except Exception:  # noqa: BLE001 - malformed staged data
                return request.not_found()
            return request.make_response(
                raw, headers=[("Content-Type", "video/mp4"),
                              ("Content-Length", str(len(raw))),
                              ("Accept-Ranges", "none")])
        return request.not_found()

    @http.route(
        "/etp_assessment/admin_draft_qvideo/<int:draft_id>/<string:slot>",
        type="http", auth="user")
    def serve_admin_draft_video(self, draft_id, slot, **kw):
        """Internal, ACL-checked proxy so a backend reviewer can preview a
        video_prompt DRAFT's staged clips before Approve. Mirrors
        serve_admin_question_video: gates on the internal user's read access to
        the draft record, then serves from video_files_json."""
        draft = request.env[
            "etp.assessment.pro.prompt.question"].browse(draft_id)
        if not draft.exists():
            return request.not_found()
        try:
            draft.check_access("read")
        except Exception:  # noqa: BLE001 - never leak existence on denial
            return request.not_found()
        return self._serve_draft_video(draft.sudo(), slot)

    def _answered_question_ids(self, evaluator):
        domain = [("state", "=", "submitted"),
                  ("assessment_evaluator_id", "=", evaluator.id)]
        return set(request.env["etp.assessment.pro.response"].sudo().search(
            domain).mapped("question_id.id"))

    def _existing_response(self, evaluator, qid):
        domain = [("question_id", "=", qid),
                  ("assessment_evaluator_id", "=", evaluator.id)]
        return request.env["etp.assessment.pro.response"].sudo().search(
            domain, limit=1)

    def _next_index(self, order, current, nav):
        """Return 1-based index to navigate to, or None to go to the review page.

        nav == "review" means "save this answer, THEN review": the Review &
        Submit control used to be a plain <a href> GET sitting inside the form,
        so leaving the page silently discarded whatever the candidate had just
        typed. It posts now, and lands here.
        """
        n = len(order)
        if not n:
            return None
        if nav == "review":
            return None
        try:
            cur_qid = int(current or 0)
        except (TypeError, ValueError):
            cur_qid = 0
        cur_idx = order.index(cur_qid) if cur_qid in order else 0
        if nav == "prev":
            return max(1, cur_idx)
        nxt = cur_idx + 2
        return nxt if nxt <= n else None

    def _serve_question(self, evaluator, token, base, requested_q=None):
        assessment = evaluator.assessment_id
        order = json.loads(evaluator.question_order or "[]")
        if order:
            live = set(request.env["etp.assessment.pro.question"]
                       .sudo().browse(order).exists().ids)
            order = [q for q in order if q in live]
        if not order:
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator})
        answered = self._answered_question_ids(evaluator)
        idx = None
        if requested_q:
            try:
                ridx = int(requested_q)
                if 1 <= ridx <= len(order):
                    idx = ridx
            except (TypeError, ValueError):
                idx = None
        if idx is None:
            idx = 1
            for i, qid in enumerate(order):
                if qid not in answered:
                    idx = i + 1
                    break
        qid = order[idx - 1]
        question = request.env["etp.assessment.pro.question"].sudo().browse(qid)
        existing = self._existing_response(evaluator, qid)
        selected_option_ids = existing.line_ids.mapped(
            "selected_option_id.id") if existing else []
        label_image, label_boxes, label_answers = self._image_label_context(
            question, existing)
        deadline = evaluator.deadline_datetime
        submit_url = (f"{base}/submit")
        return request.render(
            "etp_assessment_pro.portal_question_page",
            {
                "assessment": assessment,
                "evaluator": evaluator,
                "question": question,
                "dimensions": question.question_dimension_ids,
                "images": question.image_ids,
                "videos": question.video_ids,
                "current_index": idx,
                "total_questions": len(order),
                "token": token,
                "submit_url": submit_url,
                "violation_url": f"{base}/violation",
                "review_url": f"{base}/review",
                "progress_percent": int((len(answered) / len(order)) * 100)
                    if order else 0,
                "deadline_iso": _deadline_iso(deadline),
                "rules_json": _rules_json(assessment, evaluator),
                "selected_option_ids": selected_option_ids,
                "existing_justification": existing.justification if existing else "",
                "label_image": label_image,
                "label_boxes": label_boxes,
                "label_answers": label_answers,
            })

    def _image_label_context(self, question, existing):
        """image_label render helpers: the single source image, the sorted list
        of detected box numbers (from its answer-key detections_json — the
        candidate never sees the labels/descriptions), and any previously stored
        per-box answers parsed back out of the JSON justification for prefill.

        Returns (label_image, label_boxes, label_answers). When detection has not
        run (boxes baked into the rendered image), label_boxes is derived from the
        count of ideal_labels in the answer key so per-box inputs still show; it
        stays empty (single-textarea fallback) only when no box count is known."""
        if question.question_type != "image_label":
            return False, [], {}
        label_image = question.image_ids.filtered(
            lambda i: i.slot == "single")[:1]
        label_boxes = []
        if label_image and label_image.detections_json:
            try:
                dets = json.loads(label_image.detections_json)
            except (TypeError, ValueError):
                dets = []
            nums = {d["number"] for d in dets
                    if isinstance(d, dict) and isinstance(d.get("number"), int)}
            label_boxes = sorted(nums)
        if not label_boxes:
            from ..services import scoring
            n = _ideal_labels_count(
                scoring._image_label_key(question).get("ideal_labels"))
            label_boxes = list(range(1, n + 1))
        label_answers = {}
        if existing and existing.justification:
            try:
                parsed = json.loads(existing.justification)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                label_answers = {str(k): v for k, v in parsed.items()}
        return label_image, label_boxes, label_answers

    def _render_review(self, evaluator, token, base, finish_url, blocked=False):
        assessment = evaluator.assessment_id
        order = json.loads(evaluator.question_order or "[]")
        if order:
            live = set(request.env["etp.assessment.pro.question"]
                       .sudo().browse(order).exists().ids)
            order = [q for q in order if q in live]
        answered = self._answered_question_ids(evaluator)
        questions = request.env["etp.assessment.pro.question"].sudo().browse(order)
        rows, unanswered = [], []
        for i, q in enumerate(questions):
            is_ans = q.id in answered
            if not is_ans:
                unanswered.append(q.id)
            rows.append({
                "index": i + 1,
                "name": q.name,
                "answered": is_ans,
                "goto_url": f"{base}?q={i + 1}",
            })
        return request.render(
            "etp_assessment_pro.portal_review_page",
            {
                "assessment": assessment,
                "evaluator": evaluator,
                "review_rows": rows,
                "answered_count": len(answered),
                "total_questions": len(order),
                "unanswered": unanswered,
                "back_url": f"{base}?q=1",
                "finish_url": finish_url,
                "submit_blocked": blocked,
            })

    def _collect_image_label_answers(self, form):
        """Gather image_label per-box inputs (``label_<number>`` text fields)
        into a dict mapping str(number) -> candidate label, dropping blanks.
        Stored as JSON in the shared ``justification`` answer field so phase-6
        scoring can match it against the image's detections_json answer key."""
        labels = {}
        for key, val in (form or {}).items():
            if not key.startswith("label_"):
                continue
            num = key[len("label_"):]
            if not num.isdigit():
                continue
            if isinstance(val, (list, tuple)):
                val = val[0] if val else ""
            text = (val or "").strip()[:_LABEL_VALUE_MAX_LEN]
            if text:
                labels[num] = text
        return {k: labels[k] for k in sorted(labels, key=int)}

    def _record_response(self, evaluator, form):
        """Create or update one response from form POST data."""
        Response = request.env["etp.assessment.pro.response"].sudo()
        try:
            qid = int(form.get("question_id") or 0)
        except (TypeError, ValueError):
            qid = 0
        if not qid:
            return False
        question = request.env["etp.assessment.pro.question"].sudo().browse(qid)
        if not question.exists():
            return False
        order = json.loads(evaluator.question_order or "[]")
        if qid not in order:
            return False
        justification = (form.get("justification") or "").strip()[
            :_JUSTIFICATION_MAX_LEN]
        if question.question_type == "image_label":
            label_map = self._collect_image_label_answers(form)
            if label_map:
                justification = json.dumps(label_map, ensure_ascii=False)
        if question.question_type in (
                "subjective_rubric",
                "image_prompt", "image_label", "video_prompt") \
                and not justification:
            return False

        line_vals = []
        httpreq = getattr(request, "httprequest", None)
        for qd in question.question_dimension_ids:
            key = f"dimension_{qd.id}"
            raw_values = None
            if httpreq is not None:
                try:
                    raw_values = httpreq.form.getlist(key)
                except Exception:
                    raw_values = None
            if not raw_values:
                v = form.get(key)
                raw_values = v if isinstance(v, list) else ([v] if v else [])
            if not raw_values:
                continue
            valid_option_ids = set(qd.option_line_ids.ids)
            seen = set()
            for raw in raw_values:
                for tok in str(raw).split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        oid = int(tok)
                    except (TypeError, ValueError):
                        continue
                    if oid not in valid_option_ids or oid in seen:
                        continue
                    seen.add(oid)
                    line_vals.append((0, 0, {
                        "question_dimension_id": qd.id,
                        "selected_option_id": oid,
                    }))

        if question.question_type in ("mcq", "msq", "image_ab") and not line_vals:
            return False
        gate_incomplete = (question.question_type in ("image_prompt", "image_label")
                           and question.question_dimension_ids and not line_vals)

        domain = [
            ("question_id", "=", qid),
            ("assessment_evaluator_id", "=", evaluator.id),
        ]
        existing = Response.search(domain, limit=1)
        if existing:
            if gate_incomplete:
                if existing.state != "submitted":
                    existing.write({"justification": justification})
                return False
            existing.line_ids.unlink()
            existing.write({
                "justification": justification,
                "line_ids": line_vals,
            })
            if existing.state != "submitted":
                existing.action_submit()
            elif (existing.justification or "").strip() \
                    or existing.question_id.question_type == "image_ab":
                existing._enqueue_subjective_scoring()
            return existing

        vals = {
            "assessment_id": evaluator.assessment_id.id,
            "assessment_evaluator_id": evaluator.id,
            "evaluator_id": evaluator.applicant_id.id,
            "question_id": qid,
            "justification": justification,
            "line_ids": line_vals,
        }
        try:
            with request.env.cr.savepoint():
                response = Response.create(vals)
                response.flush_recordset()
        except IntegrityError:
            existing = Response.search(domain, limit=1)
            if not existing:
                return False
            if gate_incomplete:
                if existing.state != "submitted":
                    existing.write({"justification": justification})
                return False
            existing.line_ids.unlink()
            existing.write({
                "justification": justification,
                "line_ids": line_vals,
            })
            if existing.state != "submitted":
                existing.action_submit()
            return existing
        if gate_incomplete:
            return False
        response.action_submit()
        return response

    def _record_violation_single(self, evaluator, reason):
        assessment = evaluator.assessment_id
        request.env.cr.execute(
            "UPDATE etp_assessment_pro_evaluator "
            "SET violation_count = COALESCE(violation_count, 0) + 1 "
            "WHERE id = %s RETURNING violation_count", (evaluator.id,))
        new_count = request.env.cr.fetchone()[0]
        evaluator.sudo().invalidate_recordset(["violation_count"])
        evaluator.sudo().write({
            "is_violated": True,
            "violation_reason": reason,
            "violation_datetime": fields.Datetime.now(),
        })
        _logger.warning(
            "VIOLATION (single) candidate=%s assessment=%s reason=%s count=%s",
            evaluator.applicant_id.partner_name, assessment.name, reason,
            new_count)
        cap = assessment.max_violations or 0
        if assessment.violation_action == "auto_submit" and (not cap or new_count >= cap):
            self._auto_submit_remaining_single(evaluator)

    def _auto_submit_remaining_single(self, evaluator):
        # Single source of truth shared with the _cron_expire_stale_attempts sweep.
        evaluator.sudo()._auto_submit_expired()
