from odoo import http
from odoo.http import request


class EtpAssessmentPromptController(http.Controller):

    @http.route("/pro_etp/skill_gen/extract", type="jsonrpc", auth="user", methods=["POST"])
    def skill_gen_extract(self, prompt_id):
        rec = request.env["etp.assessment.pro.prompt"].browse(prompt_id).exists()
        if not rec:
            return {"error": "prompt_not_found"}
        from ..services import vertex
        summary = vertex.extract_skills(request.env, rec)
        rec.write({
            "state": "skills_ready",
            "last_extract_summary": "Created %s, Skipped %s, Total %s" % (
                summary.get("created", 0),
                summary.get("skipped", 0),
                summary.get("total", 0),
            ),
        })
        return {
            "prompt_id": rec.id,
            "summary": summary,
            "skills": [
                {
                    "id": s.id, "name": s.name,
                    "description": s.description or "",
                    "tags": s.tags or "",
                    "question_type": s.question_type,
                    "question_count": s.question_count,
                    "difficulty": s.difficulty,
                }
                for s in rec.skill_bank_ids
            ],
        }

    @http.route("/pro_etp/skill_gen/skills", type="jsonrpc", auth="user", methods=["POST"])
    def skill_gen_list(self, query=None, question_type=None, difficulty=None,
                      limit=100, offset=0):
        domain = [("active", "=", True)]
        if query:
            domain.append(("name", "ilike", query))
        if question_type:
            domain.append(("question_type", "=", question_type))
        if difficulty:
            domain.append(("difficulty", "=", difficulty))
        Skill = request.env["etp.assessment.pro.skill"]
        total = Skill.search_count(domain)
        recs = Skill.search(domain, limit=limit, offset=offset, order="name")
        return {
            "total": total,
            "skills": [
                {
                    "id": s.id, "name": s.name,
                    "description": s.description or "",
                    "tags": s.tags or "",
                    "question_type": s.question_type,
                    "question_count": s.question_count,
                    "time_minutes": s.time_minutes,
                    "difficulty": s.difficulty,
                }
                for s in recs
            ],
        }

    @http.route("/pro_etp/question_gen/generate", type="jsonrpc", auth="user", methods=["POST"])
    def question_gen_generate(self, prompt_id, skill_ids):
        rec = request.env["etp.assessment.pro.prompt"].browse(prompt_id).exists()
        if not rec:
            return {"error": "prompt_not_found"}
        skills = request.env["etp.assessment.pro.skill"].browse(skill_ids).exists()
        if not skills:
            return {"error": "no_skills"}
        from ..services import vertex
        all_drafts = []
        for skill in skills:
            draft_ids = vertex.generate_questions(request.env, rec, skill)
            all_drafts.extend(draft_ids)
        rec.write({"state": "done"})
        drafts = request.env["etp.assessment.pro.prompt.question"].browse(all_drafts)
        return {
            "prompt_id": rec.id,
            "draft_count": len(drafts),
            "drafts": [
                {
                    "id": d.id, "name": d.name,
                    "skill_id": d.skill_id.id,
                    "skill_name": d.skill_id.name or "",
                    "question_prompt": d.question_prompt or "",
                    "question_type": d.question_type,
                    "difficulty": d.difficulty or "",
                    "options_json": d.options_json or "",
                    "correct_answer_json": d.correct_answer_json or "",
                    "rubric_json": d.rubric_json or "",
                    "state": d.state,
                }
                for d in drafts
            ],
        }

    @http.route("/pro_etp/question_gen/drafts/<int:draft_id>/approve",
                type="jsonrpc", auth="user", methods=["POST"])
    def question_gen_approve(self, draft_id):
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
        draft = request.env["etp.assessment.pro.prompt.question"].browse(draft_id).exists()
        if not draft:
            return {"error": "draft_not_found"}
        draft.action_deny()
        return {"id": draft.id, "state": draft.state}
