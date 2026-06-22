# -*- coding: utf-8 -*-
"""Candidate-facing "My Assessments" portal page.

Candidates are PORTAL users (group_portal) — they have NO model ACL on the
etp_assessment models, so every read here goes through ``sudo()`` (mirroring
the exam portal in controllers/portal.py). The candidate is bound strictly by
their linked ``hr.employee``: we only ever surface work that belongs to the
logged-in user's employee.

The page lists the candidate's assessments grouped into four buckets:
Available now, In Progress, Upcoming (locked / scheduled), and Completed. Each
item links to the proctored runner — ``/assessment/day/<token>`` for multi-day
sessions and ``/assessment/<token>`` for single-mode tests. The runner pages
themselves keep their own login gate + candidate guard untouched.
"""
from odoo import http
from odoo.http import request, route
from odoo.tools import format_datetime
from odoo.addons.portal.controllers.portal import CustomerPortal


class EtpCandidatePortal(http.Controller):

    def _resolve_applicant(self):
        """Return the logged-in user's hr.applicant (sudo). Portal users can't
        read hr.applicant directly, so resolve it with sudo by the direct
        candidate link, falling back to a partner match."""
        user = request.env.user
        Applicant = request.env["hr.applicant"].sudo()
        applicant = Applicant.search(
            [("candidate_user_id", "=", user.id)], limit=1)
        if not applicant and user.partner_id:
            applicant = Applicant.search(
                [("partner_id", "=", user.partner_id.id)], limit=1)
        return applicant

    # ------------------------------------------------------------------
    # Item builders — normalize day-sessions and single evaluators into a
    # uniform dict the template can render without branching on the source.
    # ------------------------------------------------------------------
    # status_kind drives only the badge colour in the template; it collapses
    # the per-mode state vocabularies into one small palette.
    _DAY_STATUS_KIND = {
        "available": "available", "in_progress": "progress",
        "locked": "locked", "submitted": "done", "scored": "done",
        "missed": "missed",
    }
    _SINGLE_STATUS_KIND = {
        "available": "available", "in_progress": "progress",
        "upcoming": "locked", "completed": "done", "closed": "missed",
    }

    def _day_item(self, sess):
        return {
            "kind": "day",
            "name": sess.assessment_id.name,
            "day_label": "Day %s" % sess.day_id.sequence,
            "skill": sess.skill_id.name or "",
            "state": sess.state,
            "state_label": dict(
                sess._fields["state"].selection).get(sess.state, sess.state),
            "status_kind": self._DAY_STATUS_KIND.get(sess.state, "locked"),
            "score_display": sess.score_display or "",
            "total_questions": sess.total_questions,
            "answered_count": sess.answered_count,
            "scheduled_start": format_datetime(
                self.env, sess.scheduled_start, dt_format="MMM d, h:mm a")
                if sess.scheduled_start else "",
            "deadline": format_datetime(
                self.env, sess.deadline_datetime, dt_format="MMM d, h:mm a")
                if sess.deadline_datetime else "",
            "url": "/assessment/day/%s" % sess.access_token,
        }

    def _single_item(self, ev):
        assessment = ev.assessment_id
        # Derive a coarse candidate-facing state for single mode from the
        # evaluator + assessment lifecycle.
        if ev.is_locked or ev.state == "submitted":
            state = "completed"
        elif assessment.state == "done":
            state = "closed"
        elif ev.state == "in_progress" and ev.started_at:
            state = "in_progress"
        elif assessment.state == "in_progress":
            state = "available"
        else:  # assessment still draft / not opened
            state = "upcoming"

        # Score display respects the results_released gate, same as day mode.
        score_display = ""
        if state in ("completed", "closed"):
            if not ev.results_released:
                score_display = "Awaiting results"
            else:
                verdict = ev.result.upper() if ev.result and ev.result != "pending" else ""
                score_display = "%s%%%s" % (
                    int(round(ev.score_percent)),
                    " · %s" % verdict if verdict else "")

        state_labels = {
            "available": "Available",
            "in_progress": "In Progress",
            "upcoming": "Upcoming",
            "completed": "Completed",
            "closed": "Closed",
        }
        return {
            "kind": "single",
            "name": assessment.name,
            "day_label": "",
            "skill": "",
            "state": state,
            "state_label": state_labels.get(state, state),
            "status_kind": self._SINGLE_STATUS_KIND.get(state, "locked"),
            "score_display": score_display,
            "total_questions": ev.total_questions,
            "answered_count": ev.answered_count,
            "scheduled_start": format_datetime(
                self.env, assessment.start_date, dt_format="MMM d, h:mm a")
                if assessment.start_date else "",
            "deadline": format_datetime(
                self.env, ev.deadline_datetime, dt_format="MMM d, h:mm a")
                if ev.deadline_datetime else "",
            "url": "/assessment/%s" % ev.access_token,
        }

    @http.route("/my/assessments", type="http", auth="user", website=True)
    def my_assessments(self, **kw):
        applicant = self._resolve_applicant()
        if not applicant:
            # No linked candidate → friendly empty state (still authenticated).
            return request.render(
                "etp_assessment.portal_my_assessments",
                {"no_employee": True, "available": [], "in_progress": [],
                 "upcoming": [], "completed": [], "total_count": 0})

        # All reads via sudo() — portal users have no ACL on etp models.
        day_sessions = request.env["etp.assessment.day.session"].sudo().search(
            [("evaluator_id.applicant_id", "=", applicant.id)],
            order="assessment_id, day_sequence")
        single_evaluators = request.env["etp.assessment.evaluator"].sudo().search(
            [("applicant_id", "=", applicant.id),
             ("assessment_id.assessment_mode", "=", "single")],
            order="create_date desc")

        available, in_progress, upcoming, completed = [], [], [], []

        for sess in day_sessions:
            item = self._day_item(sess)
            if sess.state == "available":
                available.append(item)
            elif sess.state == "in_progress":
                in_progress.append(item)
            elif sess.state == "locked":
                upcoming.append(item)
            else:  # submitted / scored / missed
                completed.append(item)

        for ev in single_evaluators:
            item = self._single_item(ev)
            if item["state"] == "available":
                available.append(item)
            elif item["state"] == "in_progress":
                in_progress.append(item)
            elif item["state"] == "upcoming":
                upcoming.append(item)
            else:  # completed / closed
                completed.append(item)

        total_count = (len(available) + len(in_progress)
                       + len(upcoming) + len(completed))
        return request.render(
            "etp_assessment.portal_my_assessments",
            {
                "no_employee": False,
                "employee": applicant,
                "available": available,
                "in_progress": in_progress,
                "upcoming": upcoming,
                "completed": completed,
                "total_count": total_count,
            })


class EtpPortalHome(CustomerPortal):

    def _candidate_applicant(self):
        user = request.env.user
        if not user.share:
            return request.env["hr.applicant"].browse()
        Applicant = request.env["hr.applicant"].sudo()
        applicant = Applicant.search(
            [("candidate_user_id", "=", user.id)], limit=1)
        if not applicant and user.partner_id:
            applicant = Applicant.search(
                [("partner_id", "=", user.partner_id.id)], limit=1)
        return applicant

    def _candidate_assessment_count(self, applicant):
        if not applicant:
            return 0
        Day = request.env["etp.assessment.day.session"].sudo()
        Single = request.env["etp.assessment.evaluator"].sudo()
        return (Day.search_count(
                    [("evaluator_id.applicant_id", "=", applicant.id)])
                + Single.search_count(
                    [("applicant_id", "=", applicant.id),
                     ("assessment_id.assessment_mode", "=", "single")]))

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if "assessment_count" in counters:
            values["assessment_count"] = self._candidate_assessment_count(
                self._candidate_applicant())
        return values

    @route()
    def home(self, **kw):
        # Candidates are portal-only assessment takers — send them straight to
        # their progress view instead of the generic "My account" home.
        applicant = self._candidate_applicant()
        if applicant and self._candidate_assessment_count(applicant):
            return request.redirect("/my/assessments")
        return super().home(**kw)
