from odoo import http
from odoo.http import request


class EtpAssessmentPromptController(http.Controller):

    def _require_manager(self):
        if not request.env.user.has_group(
                "etp_assessment_pro.group_assessment_manager"):
            return {"error": "forbidden"}
        return None

    @http.route("/pro_etp/question_gen/drafts/<int:draft_id>/approve",
                type="jsonrpc", auth="user", methods=["POST"])
    def question_gen_approve(self, draft_id):
        forbidden = self._require_manager()
        if forbidden:
            return forbidden
        draft = request.env["etp.assessment.pro.prompt.question"].browse(draft_id).exists()
        if not draft:
            return {"error": "draft_not_found"}
        draft.action_approve()
        return {
            "id": draft.id,
            "state": draft.state,
            "approved_question_id": draft.approved_question_id.id or False,
        }

    @http.route("/pro_etp/question_gen/drafts/<int:draft_id>/deny",
                type="jsonrpc", auth="user", methods=["POST"])
    def question_gen_deny(self, draft_id):
        forbidden = self._require_manager()
        if forbidden:
            return forbidden
        draft = request.env["etp.assessment.pro.prompt.question"].browse(draft_id).exists()
        if not draft:
            return {"error": "draft_not_found"}
        draft.action_deny()
        return {"id": draft.id, "state": draft.state}
