# -*- coding: utf-8 -*-
"""Candidate-facing portal — single-mode + multi-day routes.

Single-mode: ``/pro_assessment/<token>`` — token resolves to an
``etp.assessment.pro.evaluator`` (one record per candidate-per-assessment).
The candidate progresses through ``question_order`` until every question
has a submitted response.

Multi-day: ``/pro_assessment/day/<token>`` — token resolves to an
``etp.assessment.pro.day.session`` (one record per candidate-per-day). The
unit of progress is the day; ``action_submit_day`` finalizes it, rolls
up the day's score, unlocks the next day, and bumps the evaluator
overall when all days are done.

Both flavors honor the assessment's proctoring rules and route violation
events to ``/violation``; ``violation_action='auto_submit'`` and
``max_violations`` are enforced server-side at the violation endpoint
(client JS can't be trusted).
"""
import json
import logging
from urllib.parse import quote

from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


def _rules_json(assessment):
    """Serialize the assessment's proctoring rules to a JSON string for safe
    injection into the runner's inline JS. Using json.dumps (not a Python
    dict via t-esc) is what makes the emitted value valid JavaScript."""
    return json.dumps({
        "tab_switch": bool(assessment.rule_block_tab_switch),
        "copy_paste": bool(assessment.rule_block_copy_paste),
        "right_click": bool(assessment.rule_block_right_click),
        "devtools": bool(assessment.rule_block_devtools),
        "screenshot": bool(assessment.rule_block_screenshot),
        "fullscreen": bool(assessment.rule_fullscreen),
        "webcam": bool(assessment.rule_webcam),
        "watermark": bool(assessment.rule_watermark),
    })


def _deadline_iso(dt):
    # Browsers parse a trailing Z as UTC. Odoo Datetimes are naive UTC.
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


class EtpAssessmentPortal(http.Controller):

    def _get_evaluator_from_token(self, token):
        if not token:
            return False
        return request.env["etp.assessment.pro.evaluator"].sudo().search(
            [("access_token", "=", token)], limit=1)

    def _candidate_guard(self, evaluator, assessment):
        # Bind the session to its assigned candidate: the logged-in user must
        # be the candidate's linked user. Stops link-sharing and any other
        # account (incl. admin/managers) from taking someone else's test.
        # If the candidate has no linked user we cannot bind, so we block all
        # but the manager group rather than let anyone through.
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
        # A manager/admin may pass _candidate_guard to PREVIEW a link, but they
        # must never start the timer or write answers on the candidate's real
        # session. Only the candidate's own linked user gets write access.
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
        if assessment.assessment_mode != "single":
            return request.render(
                "etp_assessment_pro.portal_invalid_token",
                {"reason": "This link is for a single-mode assessment but "
                           "the assessment is multi-day. Use the per-day "
                           "links instead."})
        if evaluator.is_locked:
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
                 "day_session": False,
                 "preview": not self._is_real_candidate(evaluator)})
        if evaluator.is_time_expired():
            self._auto_submit_remaining_single(evaluator)
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator})
        return self._serve_question(
            sess=False, evaluator=evaluator, token=token,
            base="/pro_assessment/%s" % token, requested_q=kw.get("q"))

    @http.route("/pro_assessment/<string:token>/begin", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
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
                auth="public", website=True, methods=["POST"], csrf=False)
    def assessment_submit_response(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            evaluator, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked:
            return request.redirect(f"/pro_assessment/{token}")
        if evaluator.is_time_expired():
            self._auto_submit_remaining_single(evaluator)
            return request.redirect(f"/pro_assessment/{token}")
        if not self._is_real_candidate(evaluator):
            return request.redirect(f"/pro_assessment/{token}")
        self._record_response(
            evaluator=evaluator, day_session=False, form=kw)
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
        if evaluator.is_locked or evaluator.state == "submitted":
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": evaluator.assessment_id,
                 "evaluator": evaluator})
        return self._render_review(
            sess=False, evaluator=evaluator, token=token,
            base="/pro_assessment/%s" % token,
            finish_url=f"/pro_assessment/{token}/finish")

    @http.route("/pro_assessment/<string:token>/finish", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def assessment_finish_single(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment_pro.portal_invalid_token")
        if not evaluator.is_locked and evaluator.state != "submitted":
            # Finalize: any unanswered questions get placeholder submissions,
            # then the evaluator is locked + submitted (same as expiry path).
            self._auto_submit_remaining_single(evaluator)
        return request.redirect(f"/pro_assessment/{token}")

    @http.route("/pro_assessment/<string:token>/violation", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
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
        reason = (kw.get("violation_reason") or "Unknown violation")[:240]
        self._record_violation_single(evaluator, reason)
        return request.redirect(f"/pro_assessment/{token}")

    def _get_day_session_from_token(self, token):
        if not token:
            return False
        return request.env["etp.assessment.pro.day.session"].sudo().search(
            [("access_token", "=", token)], limit=1)

    @http.route("/pro_assessment/day/<string:token>", type="http",
                auth="public", website=True)
    def day_landing(self, token, q=None, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment_pro.portal_invalid_token")
        # Login gate: anonymous visitors are bounced to the standard Odoo
        # login, which returns them to THIS exam URL afterwards (so they land
        # on the test, never the backend). Already-logged-in users fall
        # through straight to the exam. The token still authorizes which
        # session they get; login just ties the attempt to a real account.
        if request.env.user._is_public():
            return request.redirect(
                "/web/login?redirect=/pro_assessment/day/%s" % token)
        block = self._candidate_guard(
            sess.evaluator_id, sess.assessment_id)
        if block:
            return block
        assessment = sess.assessment_id
        # A finished day shows its result regardless of the parent
        # assessment's global state (which flips to done once everyone
        # submits) — otherwise a candidate who completed would see the
        # "not available" closed page instead of their score.
        if sess.state in ("submitted", "scored", "missed"):
            return self._render_day_result(sess)
        if assessment.state not in ("in_progress",):
            return request.render(
                "etp_assessment_pro.portal_assessment_closed",
                {"assessment": assessment, "evaluator": sess.evaluator_id})
        if sess.state == "locked":
            return request.render(
                "etp_assessment_pro.portal_day_locked",
                {"assessment": assessment, "day_session": sess})
        if sess.state == "available":
            return request.render(
                "etp_assessment_pro.portal_instructions",
                {"assessment": assessment, "evaluator": sess.evaluator_id,
                 "token": token, "day_session": sess,
                 "duration_minutes": sess.day_id.duration_minutes,
                 "submit_url": f"/pro_assessment/day/{token}/begin",
                 "preview": not self._is_real_candidate(
                     sess.evaluator_id)})
        # In progress: deadline check, then render the requested (or first
        # unanswered) question with FREE navigation.
        if (sess.deadline_datetime
                and sess.deadline_datetime < fields.Datetime.now()):
            self._auto_submit_day_on_expiry(sess)
            return self._render_day_result(sess)
        return self._serve_question(
            sess=sess, evaluator=sess.evaluator_id, token=token,
            base="/pro_assessment/day/%s" % token, requested_q=q)

    def _render_day_result(self, sess):
        # Gate: 'manual' release withholds the breakdown until an admin flips
        # evaluator.results_released — responses stay submitted, scores hidden.
        assessment = sess.assessment_id
        responses = sess.response_ids.filtered(
            lambda r: r.state == "submitted")
        show_results = True
        if (assessment.results_release == "manual"
                and not sess.evaluator_id.results_released):
            show_results = False
        return request.render(
            "etp_assessment_pro.portal_day_result",
            {
                "assessment": assessment,
                "day_session": sess,
                "evaluator": sess.evaluator_id,
                "responses": responses,
                "show_results": show_results,
            })

    @http.route("/pro_assessment/day/<string:token>/begin", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_begin(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id, sess.assessment_id)
        if block:
            return block
        if sess.assessment_id.state != "in_progress":
            return request.redirect("/pro_assessment/day/%s" % token)
        if not self._is_real_candidate(sess.evaluator_id):
            return request.redirect("/pro_assessment/day/%s" % token)
        if sess.state == "available":
            sess.sudo().action_start_day()
        return request.redirect(f"/pro_assessment/day/{token}")

    @http.route("/pro_assessment/day/<string:token>/submit", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_submit_response(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id, sess.assessment_id)
        if block:
            return block
        if sess.state not in ("in_progress",):
            return request.redirect(f"/pro_assessment/day/{token}")
        if (sess.deadline_datetime
                and sess.deadline_datetime < fields.Datetime.now()):
            self._auto_submit_day_on_expiry(sess)
            return request.redirect(f"/pro_assessment/day/{token}")
        if not self._is_real_candidate(sess.evaluator_id):
            return request.redirect(f"/pro_assessment/day/{token}")
        self._record_response(
            evaluator=sess.evaluator_id, day_session=sess, form=kw)
        # Free navigation: compute the next index from nav intent.
        target = self._next_index(
            json.loads(sess.question_order or "[]"),
            current=kw.get("question_id"), nav=kw.get("nav") or "next")
        if target is None:
            # Past the last question on 'next' -> go to review.
            return request.redirect(f"/pro_assessment/day/{token}/review")
        return request.redirect(f"/pro_assessment/day/{token}?q={target}")

    @http.route("/pro_assessment/day/<string:token>/review", type="http",
                auth="public", website=True)
    def day_review(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment_pro.portal_invalid_token")
        if sess.state in ("submitted", "scored", "missed"):
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": sess.assessment_id,
                 "evaluator": sess.evaluator_id, "day_session": sess})
        return self._render_review(
            sess=sess, evaluator=sess.evaluator_id, token=token,
            base="/pro_assessment/day/%s" % token,
            finish_url=f"/pro_assessment/day/{token}/finish")

    @http.route("/pro_assessment/day/<string:token>/finish", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_finish(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id, sess.assessment_id)
        if block:
            return block
        if sess.state == "in_progress":
            sess.sudo().action_submit_day()
        return request.redirect(f"/pro_assessment/day/{token}")

    @http.route("/pro_assessment/day/<string:token>/violation", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_violation(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment_pro.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id, sess.assessment_id)
        if block:
            return block
        reason = (kw.get("violation_reason") or "Unknown violation")[:240]
        self._record_violation_day(sess, reason)
        return request.redirect(f"/pro_assessment/day/{token}")

    @http.route(
        "/pro_assessment/day/<string:token>/image/<int:question_id>/<string:field>",
        type="http", auth="public", website=False, csrf=False)
    def day_image(self, token, question_id, field, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.not_found()
        # The new question schema has no image_a/image_b binary fields, so
        # this route is a placeholder kept per brief §6.1 for future
        # image-bearing question types.
        return request.not_found()

    # ------------------------------------------------------------------
    # Shared question-serving + navigation helpers (used by both single and
    # day flows). The candidate can move freely between questions; answers
    # are saved on every move, and a Review page lists answered/unanswered.
    # ------------------------------------------------------------------
    def _answered_question_ids(self, evaluator, day_session):
        domain = [("state", "=", "submitted")]
        if day_session:
            domain.append(("day_session_id", "=", day_session.id))
        else:
            domain += [("assessment_evaluator_id", "=", evaluator.id),
                       ("day_session_id", "=", False)]
        return set(request.env["etp.assessment.pro.response"].sudo().search(
            domain).mapped("question_id.id"))

    def _existing_response(self, evaluator, day_session, qid):
        domain = [("question_id", "=", qid),
                  ("assessment_evaluator_id", "=", evaluator.id)]
        if day_session:
            domain.append(("day_session_id", "=", day_session.id))
        else:
            domain.append(("day_session_id", "=", False))
        return request.env["etp.assessment.pro.response"].sudo().search(
            domain, limit=1)

    def _next_index(self, order, current, nav):
        """Return the 1-based index to navigate to, or None when 'next' runs
        off the end (caller then routes to review). 'prev' clamps at 1."""
        n = len(order)
        if not n:
            return None
        try:
            cur_qid = int(current or 0)
        except (TypeError, ValueError):
            cur_qid = 0
        cur_idx = order.index(cur_qid) if cur_qid in order else 0
        if nav == "prev":
            return max(1, cur_idx)  # cur_idx is 0-based of current => prev is cur_idx
        nxt = cur_idx + 2  # 1-based next
        return nxt if nxt <= n else None

    def _serve_question(self, sess, evaluator, token, base, requested_q=None):
        assessment = (sess.assessment_id if sess else evaluator.assessment_id)
        order = json.loads(
            (sess.question_order if sess else evaluator.question_order) or "[]")
        if not order:
            return request.render(
                "etp_assessment_pro.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator,
                 "day_session": sess})
        answered = self._answered_question_ids(evaluator, sess)
        # Decide which index to show: explicit ?q wins; else first unanswered.
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
        existing = self._existing_response(evaluator, sess, qid)
        selected_option_ids = existing.line_ids.mapped(
            "selected_option_id.id") if existing else []
        deadline = sess.deadline_datetime if sess else (
            evaluator.deadline_datetime)
        is_day = bool(sess)
        submit_url = (f"{base}/submit")
        return request.render(
            "etp_assessment_pro.portal_question_page",
            {
                "assessment": assessment,
                "evaluator": evaluator,
                "day_session": sess,
                "question": question,
                "dimensions": question.question_dimension_ids,
                "images": question.image_ids,
                "current_index": idx,
                "total_questions": len(order),
                "token": token,
                "submit_url": submit_url,
                "violation_url": f"{base}/violation",
                "review_url": f"{base}/review",
                "progress_percent": int((len(answered) / len(order)) * 100)
                    if order else 0,
                "deadline_iso": _deadline_iso(deadline),
                "rules_json": _rules_json(assessment),
                "selected_option_ids": selected_option_ids,
                "existing_justification": existing.justification if existing else "",
            })

    def _render_review(self, sess, evaluator, token, base, finish_url):
        assessment = (sess.assessment_id if sess else evaluator.assessment_id)
        order = json.loads(
            (sess.question_order if sess else evaluator.question_order) or "[]")
        answered = self._answered_question_ids(evaluator, sess)
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
                "day_session": sess,
                "review_rows": rows,
                "answered_count": len(answered),
                "total_questions": len(order),
                "unanswered": unanswered,
                "back_url": f"{base}?q=1",
                "finish_url": finish_url,
            })

    def _record_response(self, evaluator, day_session, form):
        """Create or update one response from form POST data.

        Form keys: ``question_id``, ``justification``, and one
        ``dimension_<dim_id>`` per dimension whose value is the picked
        ``etp.assessment.pro.dimension.option.id`` (MASTER option id). MSQ
        questions submit the dim_<id> key MULTIPLE times — request.params
        only keeps the last, so on MSQ the portal serializes the picks
        into the single field as ``"o1,o2"`` and we split here.
        """
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
        justification = (form.get("justification") or "").strip()
        if question.question_type in (
                "subjective_justification", "subjective_rubric",
                "image_text") \
                and not justification:
            return False

        # Build option picks per dimension. MSQ encodes multi-select as
        # comma-separated values in the dimension_<id> form field.
        line_vals = []
        for qd in question.question_dimension_ids:
            key = f"dimension_{qd.dimension_id.id}"
            raw = form.get(key)
            if not raw:
                continue
            tokens = raw if isinstance(raw, list) else str(raw).split(",")
            for tok in tokens:
                tok = (tok or "").strip()
                if not tok:
                    continue
                try:
                    oid = int(tok)
                except (TypeError, ValueError):
                    continue
                line_vals.append((0, 0, {
                    "dimension_id": qd.dimension_id.id,
                    "selected_option_id": oid,
                }))

        if question.question_type in ("mcq", "msq", "image_ab") and not line_vals:
            return False

        domain = [
            ("question_id", "=", qid),
            ("assessment_evaluator_id", "=", evaluator.id),
        ]
        if day_session:
            domain.append(("day_session_id", "=", day_session.id))
        existing = Response.search(domain, limit=1)
        if existing and existing.state == "submitted":
            return existing
        if existing:
            existing.line_ids.unlink()
            existing.write({
                "justification": justification,
                "line_ids": line_vals,
            })
            response = existing
        else:
            response = Response.create({
                "assessment_id": evaluator.assessment_id.id,
                "assessment_evaluator_id": evaluator.id,
                "day_session_id": day_session.id if day_session else False,
                "evaluator_id": evaluator.applicant_id.id,
                "question_id": qid,
                "justification": justification,
                "line_ids": line_vals,
            })
        response.action_submit()
        return response

    def _record_violation_single(self, evaluator, reason):
        # Increment counter, persist details, honor max_violations cap +
        # violation_action. log_only stops here; auto_submit also fires
        # the auto-submit when the cap is exceeded.
        assessment = evaluator.assessment_id
        new_count = (evaluator.violation_count or 0) + 1
        evaluator.sudo().write({
            "is_violated": True,
            "violation_reason": reason,
            "violation_datetime": fields.Datetime.now(),
            "violation_count": new_count,
        })
        _logger.warning(
            "VIOLATION (single) candidate=%s assessment=%s reason=%s count=%s",
            evaluator.applicant_id.partner_name, assessment.name, reason,
            new_count)
        cap = assessment.max_violations or 0
        if assessment.violation_action == "auto_submit" and cap and new_count >= cap:
            self._auto_submit_remaining_single(evaluator)

    def _record_violation_day(self, sess, reason):
        # Same logic as single-mode but the auto-submit target is the
        # current day session (action_submit_day handles rollup).
        evaluator = sess.evaluator_id
        assessment = sess.assessment_id
        new_count = (evaluator.violation_count or 0) + 1
        evaluator.sudo().write({
            "is_violated": True,
            "violation_reason": reason,
            "violation_datetime": fields.Datetime.now(),
            "violation_count": new_count,
        })
        _logger.warning(
            "VIOLATION (day) candidate=%s assessment=%s day=%s reason=%s count=%s",
            evaluator.applicant_id.partner_name, assessment.name,
            sess.day_id.sequence, reason, new_count)
        cap = assessment.max_violations or 0
        if assessment.violation_action == "auto_submit" and cap and new_count >= cap:
            if sess.state == "in_progress":
                self._auto_submit_day_on_expiry(sess)

    def _auto_submit_remaining_single(self, evaluator):
        # Fill in placeholder responses for any unanswered question, then
        # flip the evaluator to submitted. Placeholder responses are
        # marked llm_state=not_needed so they don't trigger LLM scoring.
        question_order = json.loads(evaluator.question_order or "[]")
        Response = request.env["etp.assessment.pro.response"].sudo()
        for q_id in question_order:
            existing = Response.search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("question_id", "=", q_id),
                ("state", "=", "submitted"),
            ], limit=1)
            if existing:
                continue
            draft = Response.search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("question_id", "=", q_id),
                ("state", "=", "draft"),
            ], limit=1)
            if draft:
                draft.write({"state": "submitted",
                             "llm_state": "not_needed"})
            else:
                Response.create({
                    "assessment_id": evaluator.assessment_id.id,
                    "assessment_evaluator_id": evaluator.id,
                    "evaluator_id": evaluator.applicant_id.id,
                    "question_id": q_id,
                    "justification": "[Auto-submitted: time expired]",
                    "state": "submitted",
                    "llm_state": "not_needed",
                })
        evaluator.write({"state": "submitted", "is_locked": True})
        assessment = evaluator.assessment_id
        evs = assessment.assessment_evaluator_ids
        if evs and all(e.state == "submitted" for e in evs):
            assessment.write({"state": "done"})

    def _auto_submit_day_on_expiry(self, sess):
        # Same pattern as single-mode auto-submit but the unit is the day
        # session. Fill placeholders, then run action_submit_day so the
        # day's score rolls up and the next day unlocks.
        question_order = json.loads(sess.question_order or "[]")
        Response = request.env["etp.assessment.pro.response"].sudo()
        for q_id in question_order:
            existing = Response.search([
                ("day_session_id", "=", sess.id),
                ("question_id", "=", q_id),
                ("state", "=", "submitted"),
            ], limit=1)
            if existing:
                continue
            draft = Response.search([
                ("day_session_id", "=", sess.id),
                ("question_id", "=", q_id),
                ("state", "=", "draft"),
            ], limit=1)
            if draft:
                draft.write({"state": "submitted",
                             "llm_state": "not_needed"})
                continue
            Response.create({
                "assessment_id": sess.assessment_id.id,
                "assessment_evaluator_id": sess.evaluator_id.id,
                "day_session_id": sess.id,
                "evaluator_id": sess.evaluator_id.applicant_id.id,
                "question_id": q_id,
                "justification": "[Auto-submitted: time expired]",
                "state": "submitted",
                "llm_state": "not_needed",
            })
        try:
            sess.sudo().action_submit_day()
        except Exception:
            _logger.exception(
                "Auto-submit on expiry failed for day session %s", sess.id)
            sess.write({"state": "missed"})
