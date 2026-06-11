# -*- coding: utf-8 -*-
"""JSON API for the LLM Question Bank generator.

All routes: type="json", auth="user". The client must first authenticate via
POST /web/session/authenticate and carry the session_id cookie. Every request
body is the standard Odoo JSON-RPC envelope:

    {"jsonrpc":"2.0","method":"call","params": { ...the documented params... }}

and the response is {"jsonrpc":"2.0","id":...,"result": <documented result>}.
Errors come back as {"error": {"data": {"message": "..."}}}.

Full request/response examples: see docs/PROMPT_API.md in this addon.
"""
from odoo import http
from odoo.http import request


class EtpPromptController(http.Controller):

    # ---------------------------------------------------------------- create
    @http.route("/etp_assessment/prompt/create", type="json", auth="user")
    def create_prompt(self, title=None, source_text=None, category_id=None):
        rec = request.env["etp.assessment.prompt"].create({
            "name": title or "New Prompt",
            "source_text": source_text or "",
            "category_id": category_id or False,
        })
        return rec.to_dict()

    # ------------------------------------------------------------------ list
    @http.route("/etp_assessment/prompt/list", type="json", auth="user")
    def list_prompts(self, limit=50, offset=0):
        """Landing list of prompt sessions (newest first), no children."""
        recs = request.env["etp.assessment.prompt"].search(
            [], limit=limit, offset=offset, order="create_date desc")
        total = request.env["etp.assessment.prompt"].search_count([])
        return {
            "total": total,
            "prompts": [r.to_dict(include_children=False) for r in recs],
        }

    # ------------------------------------------------------------------- get
    @http.route("/etp_assessment/prompt/get", type="json", auth="user")
    def get_prompt(self, prompt_id):
        """Full prompt incl. skills + questions — for resume/reload."""
        rec = request.env["etp.assessment.prompt"].browse(prompt_id).exists()
        if not rec:
            return {"error": "not_found"}
        return rec.to_dict()

    # ---------------------------------------------------------------- delete
    @http.route("/etp_assessment/prompt/delete", type="json", auth="user")
    def delete_prompt(self, prompt_id):
        rec = request.env["etp.assessment.prompt"].browse(prompt_id).exists()
        if rec:
            rec.unlink()
        return {"deleted": True}

    # ---------------------------------------------------------------- update
    @http.route("/etp_assessment/prompt/update", type="json", auth="user")
    def update_prompt(self, prompt_id, title=None, source_text=None, category_id=None):
        rec = request.env["etp.assessment.prompt"].browse(prompt_id)
        vals = {}
        if title is not None:
            vals["name"] = title
        if source_text is not None:
            vals["source_text"] = source_text
        if category_id is not None:
            vals["category_id"] = category_id or False
        if vals:
            rec.write(vals)
        return rec.to_dict(include_children=False)

    # ------------------------------------------------------ resource upload
    @http.route("/etp_assessment/prompt/upload_resource", type="json", auth="user")
    def upload_resource(self, prompt_id, filename, file_b64):
        rec = request.env["etp.assessment.prompt.resource"].create({
            "prompt_id": prompt_id,
            "name": filename,
            "file": file_b64,
        })
        return rec.to_dict()

    @http.route("/etp_assessment/prompt/remove_resource", type="json", auth="user")
    def remove_resource(self, resource_id):
        rec = request.env["etp.assessment.prompt.resource"].browse(resource_id).exists()
        if rec:
            rec.unlink()
        return {"removed": True}

    # ----------------------------------------------- CALL 1: extract skills
    @http.route("/etp_assessment/prompt/extract_skills", type="json", auth="user")
    def extract_skills(self, prompt_id, source_text=None, title=None, category_id=None):
        rec = request.env["etp.assessment.prompt"].browse(prompt_id)
        vals = {}
        if source_text is not None:
            vals["source_text"] = source_text
        if title:
            vals["name"] = title
        if category_id is not None:
            vals["category_id"] = category_id or False
        if vals:
            rec.write(vals)
        skills = rec.action_extract_skills()
        return {"skills": skills, "state": rec.state}

    # ------------------------------------------------------------ save skills
    @http.route("/etp_assessment/prompt/save_skills", type="json", auth="user")
    def save_skills(self, skills):
        """Persist edited max_questions (and optional name) before generation."""
        Skill = request.env["etp.assessment.prompt.skill"]
        for s in skills or []:
            if s.get("id"):
                vals = {"max_questions": int(s.get("max_questions", 5) or 5)}
                if s.get("name"):
                    vals["name"] = s["name"]
                Skill.browse(s["id"]).write(vals)
        return {"saved": True}

    # ------------------------------------------------- generation (seed)
    @http.route("/etp_assessment/prompt/generate", type="json", auth="user")
    def generate(self, prompt_id, title=None, notes=None, golden_example=None,
                 max_questions=None, category_id=None):
        rec = request.env["etp.assessment.prompt"].browse(prompt_id)
        vals = {}
        if title:
            vals["name"] = title
        if notes is not None:
            vals["source_text"] = notes
        if golden_example is not None:
            vals["golden_example"] = golden_example
        if max_questions is not None:
            vals["max_questions"] = int(max_questions or 0)
        if category_id is not None:
            vals["category_id"] = category_id or False
        if vals:
            rec.write(vals)
        questions = rec.action_generate_questions()
        return {"questions": questions, "state": rec.state}

    # -------------------------------------------- image generation (drafts)
    @http.route("/etp_assessment/prompt/generate_images", type="json", auth="user")
    def generate_images(self, prompt_id, question_ids=None):
        rec = request.env["etp.assessment.prompt"].browse(prompt_id)
        drafts = rec.question_ids
        if question_ids:
            drafts = drafts.filtered(lambda q: q.id in question_ids)
        drafts.action_generate_images()
        return {"questions": [
            rec._question_dict(q)
            for q in drafts.filtered(
                lambda q: q.question_type == "image_comparison")
        ]}

    # --------------------------------------------------- approve / deny one
    @http.route("/etp_assessment/prompt/decision", type="json", auth="user")
    def decision(self, question_id, approve):
        q = request.env["etp.assessment.prompt.question"].browse(question_id)
        if approve:
            q.action_approve()
        else:
            q.action_deny()
        return {
            "id": q.id,
            "state": q.state,
            "approved_question_id": q.approved_question_id.id or False,
        }

    # ----------------------------------------------- bulk approve / deny
    @http.route("/etp_assessment/prompt/decision_bulk", type="json", auth="user")
    def decision_bulk(self, question_ids=None, approve=True, prompt_id=None, skill=None):
        """Approve/deny many at once.

        Provide either an explicit `question_ids` list, OR a `prompt_id`
        (optionally narrowed by `skill`) to act on all its pending drafts.
        """
        Q = request.env["etp.assessment.prompt.question"]
        if question_ids:
            recs = Q.browse(question_ids)
        elif prompt_id:
            domain = [("prompt_id", "=", prompt_id), ("state", "=", "draft")]
            if skill:
                domain.append(("skill", "=", skill))
            recs = Q.search(domain)
        else:
            recs = Q.browse([])
        if approve:
            recs.action_approve()
        else:
            recs.action_deny()
        prompt = request.env["etp.assessment.prompt"].browse(prompt_id) if prompt_id else None
        return {
            "updated": [
                {"id": r.id, "state": r.state,
                 "approved_question_id": r.approved_question_id.id or False}
                for r in recs
            ],
            "approved_count": prompt.approved_count if prompt else False,
        }

    # ------------------------------------------------------------ categories
    @http.route("/etp_assessment/prompt/categories", type="json", auth="user")
    def categories(self):
        return request.env["etp.assessment.category"].search_read(
            [], ["id", "name"], order="name")

    # -------------------------------------------------------- system prompts
    @http.route("/etp_assessment/prompt/system_prompts", type="json", auth="user")
    def get_system_prompts(self):
        """The two live prompts: seed (generation) + scoring."""
        Prompt = request.env["etp.assessment.prompt"]
        from odoo.addons.etp_assessment.services.bedrock_scoring import (
            _get_scoring_prompt,
        )
        return {
            "seed": Prompt._get_seed_system_prompt(),
            "scoring": _get_scoring_prompt(request.env),
            # legacy slots kept for the dormant skills mode
            "skills": Prompt._get_skills_system_prompt(),
            "questions": Prompt._get_questions_system_prompt(),
        }

    @http.route("/etp_assessment/prompt/save_system_prompt", type="json", auth="user")
    def save_system_prompt(self, which, value):
        allowed = {"seed", "scoring", "skills", "questions"}
        if which not in allowed:
            return {"error": f"unknown prompt '{which}'"}
        key = f"etp_assessment.{which}_system_prompt"
        request.env["ir.config_parameter"].sudo().set_param(key, value or "")
        return {"saved": True}

    # ------------------------------------------------- bedrock config status
    @http.route("/etp_assessment/prompt/config_status", type="json", auth="user")
    def config_status(self):
        """Lets the app show 'LLM ready' vs 'not configured' without a failed call."""
        ICP = request.env["ir.config_parameter"].sudo()
        arn = ICP.get_param("etp_assessment.bedrock_inference_arn", "")
        token = ICP.get_param("etp_assessment.bedrock_bearer_token", "")
        region = ICP.get_param("etp_assessment.bedrock_region", "")
        return {
            "configured": bool(arn and token),
            "region": region or "us-east-1",
            "has_arn": bool(arn),
            "has_token": bool(token),
        }
