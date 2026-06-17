# -*- coding: utf-8 -*-
"""Candidate convenience redirect.

The candidate hub lives in the BACKEND (apps -> ETP Assessment -> My
Assessments), not on the website. The proctored runner pages, however, link
back to "/my/assessments" after submitting a day. To keep those links working
we resolve "/my/assessments" to the backend My Assessments action.

Authenticated users are sent to the backend menu/action; anyone not logged in
is sent to the login page first (then bounced back here).
"""
from odoo import http
from odoo.http import request


class EtpCandidateRedirect(http.Controller):

    @http.route("/my/assessments", type="http", auth="user", website=True)
    def my_assessments_redirect(self, **kw):
        # Resolve the backend action + menu so the web client opens directly on
        # the candidate's My Assessments tab inside the ETP Assessment app.
        action = request.env.ref(
            "etp_assessment.action_my_assessments", raise_if_not_found=False)
        menu = request.env.ref(
            "etp_assessment.menu_etp_assessment_my", raise_if_not_found=False)
        if action and menu:
            return request.redirect(
                "/odoo/action-%s?menu_id=%s" % (action.id, menu.id))
        if action:
            return request.redirect("/odoo/action-%s" % action.id)
        return request.redirect("/odoo")
