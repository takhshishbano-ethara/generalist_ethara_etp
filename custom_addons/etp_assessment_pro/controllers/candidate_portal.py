# -*- coding: utf-8 -*-
"""Candidate-facing "My Assessments" portal page.

Candidates are portal users with no model ACL, so every read goes through
``sudo()`` and is bound strictly to the logged-in user's linked applicant.
"""
from odoo import http
from odoo.http import request, route
from odoo.tools import format_datetime
from odoo.addons.portal.controllers.portal import CustomerPortal


class EtpCandidatePortal(http.Controller):

    def _resolve_applicant(self):
        """Applicant for the logged-in user (sudo): candidate link, partner,
        then login==email so an internal-user candidate resolves."""
        user = request.env.user
        Applicant = request.env["hr.applicant"].sudo()
        applicant = Applicant.search(
            [("candidate_user_id", "=", user.id)], limit=1)
        if not applicant and user.partner_id:
            applicant = Applicant.search(
                [("partner_id", "=", user.partner_id.id)], limit=1)
        if not applicant and user.login:
            applicant = Applicant.search(
                [("email_from", "=ilike", user.login)], limit=1)
        return applicant

    _SINGLE_STATUS_KIND = {
        "available": "available", "in_progress": "progress",
        "upcoming": "locked", "completed": "done", "closed": "missed",
    }

    def _single_item(self, ev):
        assessment = ev.assessment_id
        if ev.is_locked or ev.state == "submitted":
            state = "completed"
        elif assessment.state == "done":
            state = "closed"
        elif ev.state == "in_progress" and ev.started_at:
            state = "in_progress"
        elif assessment.state == "in_progress":
            state = "available"
        else:
            state = "upcoming"

        # Results gating: score_pct / result / results_released are ONLY
        # populated for a released evaluator, so the template can never leak a
        # score before an admin flips results_released.
        score_display = ""
        results_released = False
        score_pct = 0
        result_verdict = ""
        if state in ("completed", "closed"):
            if not ev.results_released:
                score_display = "Awaiting results"
            else:
                results_released = True
                score_pct = int(round(ev.score_percent or 0))
                result_verdict = ev.result if ev.result in ("pass", "fail") else ""
                verdict = result_verdict.upper()
                score_display = "%s%%%s" % (
                    score_pct,
                    " · %s" % verdict if verdict else "")

        # Answered / total → progress-bar width (0 when nothing to answer yet).
        total_q = ev.total_questions or 0
        answered = ev.answered_count or 0
        progress_pct = int(round(100.0 * answered / total_q)) if total_q else 0

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
            "results_released": results_released,
            "score_pct": score_pct,
            "result": result_verdict,
            "progress_pct": progress_pct,
            "total_questions": ev.total_questions,
            "answered_count": ev.answered_count,
            "scheduled_start": format_datetime(
                request.env,assessment.start_date, dt_format="MMM d, h:mm a")
                if assessment.start_date else "",
            "deadline": format_datetime(
                request.env,ev.deadline_datetime, dt_format="MMM d, h:mm a")
                if ev.deadline_datetime else "",
            "submitted_on": format_datetime(
                request.env, ev.submitted_at, dt_format="MMM d, y, h:mm a")
                if ev.submitted_at else "",
            "is_violated": ev.is_violated,
            "violation_count": ev.violation_count or 0,
            "violation_reason": ev.violation_reason or "",
            "url": "/pro_assessment/%s" % ev.access_token,
        }

    @http.route("/my/pro_assessments", type="http", auth="user", website=True)
    def my_assessments(self, **kw):
        applicant = self._resolve_applicant()
        if not applicant:
            return request.render(
                "etp_assessment_pro.portal_my_assessments",
                {"no_employee": True, "available": [], "in_progress": [],
                 "upcoming": [], "completed": [], "total_count": 0})

        single_evaluators = request.env["etp.assessment.pro.evaluator"].sudo().search(
            [("applicant_id", "=", applicant.id)],
            order="create_date desc")

        available, in_progress, upcoming, completed = [], [], [], []

        for ev in single_evaluators:
            item = self._single_item(ev)
            if item["state"] == "available":
                available.append(item)
            elif item["state"] == "in_progress":
                in_progress.append(item)
            elif item["state"] == "upcoming":
                upcoming.append(item)
            else:
                completed.append(item)

        total_count = (len(available) + len(in_progress)
                       + len(upcoming) + len(completed))
        kpis = self._candidate_kpis(completed)
        return request.render(
            "etp_assessment_pro.portal_my_assessments",
            {
                "no_employee": False,
                "employee": applicant,
                "available": available,
                "in_progress": in_progress,
                "upcoming": upcoming,
                "completed": completed,
                "total_count": total_count,
                "kpis": kpis,
            })

    def _candidate_kpis(self, completed):
        """Aggregate the candidate's own results into headline KPIs + a
        per-assessment breakdown — the same numbers the admin dashboard shows,
        but scoped to this one candidate and only over RELEASED results (so
        nothing leaks before an admin releases). Pure arithmetic over
        already-loaded card dicts; no extra query, no LLM."""
        released = [c for c in completed if c.get("results_released")]
        # score_pct is a real, released number here (gated above); an unscored
        # released candidate still counts as 0 which is the true shown value.
        graded = released
        taken = len(completed)
        scored = len(graded)
        passed = sum(1 for c in graded if c.get("result") == "pass")
        avg = round(sum(c["score_pct"] for c in graded) / scored, 1) if scored else 0
        best = max((c["score_pct"] for c in graded), default=0)
        pass_rate = round(passed / scored * 100.0, 0) if scored else 0
        # Per-assessment breakdown rows (released only), best score first.
        breakdown = sorted(
            ({"name": c["name"], "score_pct": c["score_pct"],
              "result": c.get("result") or "pending",
              "submitted_on": c.get("submitted_on", "")}
             for c in graded),
            key=lambda r: r["score_pct"], reverse=True)
        return {
            "taken": taken,
            "scored": scored,
            "passed": passed,
            "avg_score": avg,
            "best_score": best,
            "pass_rate": int(pass_rate),
            "awaiting": taken - scored,
            "has_any": taken > 0,
            "breakdown": breakdown,
        }


class EtpPortalHome(CustomerPortal):

    def _candidate_applicant(self):
        # Security: internal users (share=False) intentionally included —
        # matched by login==email so an employee-candidate's assessments show.
        user = request.env.user
        Applicant = request.env["hr.applicant"].sudo()
        applicant = Applicant.search(
            [("candidate_user_id", "=", user.id)], limit=1)
        if not applicant and user.partner_id:
            applicant = Applicant.search(
                [("partner_id", "=", user.partner_id.id)], limit=1)
        if not applicant and user.login:
            applicant = Applicant.search(
                [("email_from", "=ilike", user.login)], limit=1)
        return applicant

    def _candidate_assessment_count(self, applicant):
        if not applicant:
            return 0
        Single = request.env["etp.assessment.pro.evaluator"].sudo()
        return Single.search_count([("applicant_id", "=", applicant.id)])

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if "assessment_count" in counters:
            values["assessment_count"] = self._candidate_assessment_count(
                self._candidate_applicant())
        return values

    @route()
    def home(self, **kw):
        applicant = self._candidate_applicant()
        if applicant and self._candidate_assessment_count(applicant):
            return request.redirect("/my/pro_assessments")
        return super().home(**kw)
