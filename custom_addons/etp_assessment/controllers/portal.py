# -*- coding: utf-8 -*-
"""Candidate-facing portal — single-mode + multi-day routes.

Single-mode: ``/assessment/<token>`` — token resolves to an
``etp.assessment.evaluator`` (one record per candidate-per-assessment).
The candidate progresses through ``question_order`` until every question
has a submitted response.

Multi-day: ``/assessment/day/<token>`` — token resolves to an
``etp.assessment.day.session`` (one record per candidate-per-day). The
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

from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


class EtpAssessmentPortal(http.Controller):

    def _get_evaluator_from_token(self, token):
        if not token:
            return False
        return request.env["etp.assessment.evaluator"].sudo().search(
            [("access_token", "=", token)], limit=1)

    def _candidate_guard(self, employee, assessment):
        # Bind the session to its assigned candidate: the logged-in user must
        # be the candidate's linked user. Stops link-sharing and any other
        # account (incl. admin/managers) from taking someone else's test.
        # If the candidate has no linked user we cannot bind, so we block all
        # but the manager group rather than let anyone through.
        candidate_user = employee.user_id
        current = request.env.user
        if candidate_user:
            if current.id != candidate_user.id:
                return request.render(
                    "etp_assessment.portal_wrong_candidate",
                    {"assessment": assessment, "candidate": employee,
                     "current_user": current})
            return False
        if not current.has_group("etp_assessment.group_assessment_manager"):
            return request.render(
                "etp_assessment.portal_wrong_candidate",
                {"assessment": assessment, "candidate": employee,
                 "current_user": current})
        return False

    def _guard_or_abort(self, employee, assessment):
        if request.env.user._is_public():
            return request.redirect("/web/login")
        return self._candidate_guard(employee, assessment)

    @http.route("/assessment/<string:token>", type="http",
                auth="public", website=True)
    def assessment_landing(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")
        if request.env.user._is_public():
            return request.redirect(
                "/web/login?redirect=/assessment/%s" % token)
        block = self._candidate_guard(evaluator.employee_id, evaluator.assessment_id)
        if block:
            return block
        assessment = evaluator.assessment_id
        if assessment.assessment_mode != "single":
            return request.render(
                "etp_assessment.portal_invalid_token",
                {"reason": "This link is for a single-mode assessment but "
                           "the assessment is multi-day. Use the per-day "
                           "links instead."})
        if evaluator.is_locked:
            return request.render(
                "etp_assessment.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator})
        if assessment.state != "in_progress":
            return request.render(
                "etp_assessment.portal_assessment_closed",
                {"assessment": assessment, "evaluator": evaluator})
        if not evaluator.started_at:
            return request.render(
                "etp_assessment.portal_instructions",
                {"assessment": assessment, "evaluator": evaluator,
                 "token": token,
                 "duration_minutes": assessment.duration_minutes,
                 "day_session": False})
        if evaluator.is_time_expired():
            self._auto_submit_remaining_single(evaluator)
            return request.render(
                "etp_assessment.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator})

        question_order = json.loads(evaluator.question_order or "[]")
        questions = request.env["etp.assessment.question"].sudo().browse(question_order)
        answered_ids = request.env["etp.assessment.response"].sudo().search([
            ("assessment_evaluator_id", "=", evaluator.id),
            ("state", "=", "submitted"),
        ]).mapped("question_id.id")
        current_question, current_index = None, 0
        for idx, q in enumerate(questions):
            if q.id not in answered_ids:
                current_question = q
                current_index = idx + 1
                break
        if not current_question:
            return request.render(
                "etp_assessment.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator})
        deadline_iso = evaluator.deadline_datetime.strftime(
            "%Y-%m-%dT%H:%M:%SZ") if evaluator.deadline_datetime else ""
        return request.render(
            "etp_assessment.portal_question_page",
            {
                "assessment": assessment,
                "evaluator": evaluator,
                "day_session": False,
                "question": current_question,
                "dimensions": current_question.question_dimension_ids,
                "current_index": current_index,
                "total_questions": len(question_order),
                "token": token,
                "submit_url": f"/assessment/{token}/submit",
                "violation_url": f"/assessment/{token}/violation",
                "progress_percent": int(
                    (len(answered_ids) / len(question_order)) * 100)
                    if question_order else 0,
                "deadline_iso": deadline_iso,
            })

    @http.route("/assessment/<string:token>/begin", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def assessment_begin(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")
        block = self._guard_or_abort(
            evaluator.employee_id, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked or evaluator.assessment_id.state != "in_progress":
            return request.redirect("/assessment/%s" % token)
        if not evaluator.started_at:
            evaluator.write({"started_at": fields.Datetime.now()})
        if evaluator.state == "pending":
            evaluator.write({"state": "in_progress"})
        return request.redirect(f"/assessment/{token}")

    @http.route("/assessment/<string:token>/submit", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def assessment_submit_response(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")
        block = self._guard_or_abort(
            evaluator.employee_id, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked:
            return request.redirect(f"/assessment/{token}")
        if evaluator.is_time_expired():
            self._auto_submit_remaining_single(evaluator)
            return request.redirect(f"/assessment/{token}")
        self._record_response(
            evaluator=evaluator, day_session=False, form=kw)
        if evaluator.state == "pending":
            evaluator.write({"state": "in_progress"})
        return request.redirect(f"/assessment/{token}")

    @http.route("/assessment/<string:token>/violation", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def assessment_violation_single(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")
        block = self._guard_or_abort(
            evaluator.employee_id, evaluator.assessment_id)
        if block:
            return block
        if evaluator.is_locked:
            return request.redirect(f"/assessment/{token}")
        reason = (kw.get("violation_reason") or "Unknown violation")[:240]
        self._record_violation_single(evaluator, reason)
        return request.redirect(f"/assessment/{token}")

    def _get_day_session_from_token(self, token):
        if not token:
            return False
        return request.env["etp.assessment.day.session"].sudo().search(
            [("access_token", "=", token)], limit=1)

    @http.route("/assessment/day/<string:token>", type="http",
                auth="public", website=True)
    def day_landing(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment.portal_invalid_token")
        # Login gate: anonymous visitors are bounced to the standard Odoo
        # login, which returns them to THIS exam URL afterwards (so they land
        # on the test, never the backend). Already-logged-in users fall
        # through straight to the exam. The token still authorizes which
        # session they get; login just ties the attempt to a real account.
        if request.env.user._is_public():
            return request.redirect(
                "/web/login?redirect=/assessment/day/%s" % token)
        block = self._candidate_guard(
            sess.evaluator_id.employee_id, sess.assessment_id)
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
                "etp_assessment.portal_assessment_closed",
                {"assessment": assessment, "evaluator": sess.evaluator_id})
        if sess.state == "locked":
            return request.render(
                "etp_assessment.portal_day_locked",
                {"assessment": assessment, "day_session": sess})
        if sess.state == "available":
            return request.render(
                "etp_assessment.portal_instructions",
                {"assessment": assessment, "evaluator": sess.evaluator_id,
                 "token": token, "day_session": sess,
                 "duration_minutes": sess.day_id.duration_minutes,
                 "submit_url": f"/assessment/day/{token}/begin"})
        # In progress: deadline check, render next unanswered question.
        if (sess.deadline_datetime
                and sess.deadline_datetime < fields.Datetime.now()):
            self._auto_submit_day_on_expiry(sess)
            return self._render_day_result(sess)
        question_order = json.loads(sess.question_order or "[]")
        questions = request.env["etp.assessment.question"].sudo().browse(
            question_order)
        answered_ids = request.env["etp.assessment.response"].sudo().search([
            ("day_session_id", "=", sess.id),
            ("state", "=", "submitted"),
        ]).mapped("question_id.id")
        current_question, current_index = None, 0
        for idx, q in enumerate(questions):
            if q.id not in answered_ids:
                current_question = q
                current_index = idx + 1
                break
        if not current_question:
            return request.render(
                "etp_assessment.portal_day_finish_prompt",
                {"assessment": assessment, "day_session": sess,
                 "token": token,
                 "finish_url": f"/assessment/day/{token}/finish"})
        deadline_iso = sess.deadline_datetime.strftime(
            "%Y-%m-%dT%H:%M:%SZ") if sess.deadline_datetime else ""
        return request.render(
            "etp_assessment.portal_question_page",
            {
                "assessment": assessment,
                "evaluator": sess.evaluator_id,
                "day_session": sess,
                "question": current_question,
                "dimensions": current_question.question_dimension_ids,
                "current_index": current_index,
                "total_questions": len(question_order),
                "token": token,
                "submit_url": f"/assessment/day/{token}/submit",
                "violation_url": f"/assessment/day/{token}/violation",
                "progress_percent": int(
                    (len(answered_ids) / len(question_order)) * 100)
                    if question_order else 0,
                "deadline_iso": deadline_iso,
            })

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
            "etp_assessment.portal_day_result",
            {
                "assessment": assessment,
                "day_session": sess,
                "evaluator": sess.evaluator_id,
                "responses": responses,
                "show_results": show_results,
            })

    @http.route("/assessment/day/<string:token>/begin", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_begin(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id.employee_id, sess.assessment_id)
        if block:
            return block
        if sess.assessment_id.state != "in_progress":
            return request.redirect("/assessment/day/%s" % token)
        if sess.state == "available":
            sess.sudo().action_start_day()
        return request.redirect(f"/assessment/day/{token}")

    @http.route("/assessment/day/<string:token>/submit", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_submit_response(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id.employee_id, sess.assessment_id)
        if block:
            return block
        if sess.state not in ("in_progress",):
            return request.redirect(f"/assessment/day/{token}")
        if (sess.deadline_datetime
                and sess.deadline_datetime < fields.Datetime.now()):
            self._auto_submit_day_on_expiry(sess)
            return request.redirect(f"/assessment/day/{token}")
        self._record_response(
            evaluator=sess.evaluator_id, day_session=sess, form=kw)
        return request.redirect(f"/assessment/day/{token}")

    @http.route("/assessment/day/<string:token>/finish", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_finish(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id.employee_id, sess.assessment_id)
        if block:
            return block
        if sess.state == "in_progress":
            sess.sudo().action_submit_day()
        return request.redirect(f"/assessment/day/{token}")

    @http.route("/assessment/day/<string:token>/violation", type="http",
                auth="public", website=True, methods=["POST"], csrf=False)
    def day_violation(self, token, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.render("etp_assessment.portal_invalid_token")
        block = self._guard_or_abort(
            sess.evaluator_id.employee_id, sess.assessment_id)
        if block:
            return block
        reason = (kw.get("violation_reason") or "Unknown violation")[:240]
        self._record_violation_day(sess, reason)
        return request.redirect(f"/assessment/day/{token}")

    @http.route(
        "/assessment/day/<string:token>/image/<int:question_id>/<string:field>",
        type="http", auth="public", website=False, csrf=False)
    def day_image(self, token, question_id, field, **kw):
        sess = self._get_day_session_from_token(token)
        if not sess:
            return request.not_found()
        # The new question schema has no image_a/image_b binary fields, so
        # this route is a placeholder kept per brief §6.1 for future
        # image-bearing question types.
        return request.not_found()

    def _record_response(self, evaluator, day_session, form):
        """Create or update one response from form POST data.

        Form keys: ``question_id``, ``justification``, and one
        ``dimension_<dim_id>`` per dimension whose value is the picked
        ``etp.assessment.dimension.option.id`` (MASTER option id). MSQ
        questions submit the dim_<id> key MULTIPLE times — request.params
        only keeps the last, so on MSQ the portal serializes the picks
        into the single field as ``"o1,o2"`` and we split here.
        """
        Response = request.env["etp.assessment.response"].sudo()
        try:
            qid = int(form.get("question_id") or 0)
        except (TypeError, ValueError):
            qid = 0
        if not qid:
            return False
        question = request.env["etp.assessment.question"].sudo().browse(qid)
        if not question.exists():
            return False
        justification = (form.get("justification") or "").strip()
        if question.question_type in (
                "subjective_justification", "subjective_rubric") \
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

        if question.question_type in ("mcq", "msq") and not line_vals:
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
                "evaluator_id": evaluator.employee_id.id,
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
            evaluator.employee_id.name, assessment.name, reason, new_count)
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
            evaluator.employee_id.name, assessment.name,
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
        Response = request.env["etp.assessment.response"].sudo()
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
                    "evaluator_id": evaluator.employee_id.id,
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
        Response = request.env["etp.assessment.response"].sudo()
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
                "evaluator_id": sess.evaluator_id.employee_id.id,
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
