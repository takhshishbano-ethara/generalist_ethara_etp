from odoo import api, SUPERUSER_ID

_MODEL = "gemini-3-pro-image"
_OLD_MODELS = ("gemini-2.5-pro", "gemini-3.1-pro-preview", "gemini-3.1-flash-image")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    icp = env["ir.config_parameter"].sudo()

    if (icp.get_param("etp_assessment_pro.vertex_location") or "").strip() == "us-central1":
        icp.set_param("etp_assessment_pro.vertex_location", "global")

    model = (icp.get_param("etp_assessment_pro.vertex_model") or "").strip()
    if not model or model in _OLD_MODELS:
        icp.set_param("etp_assessment_pro.vertex_model", _MODEL)

    cr.execute(
        "DELETE FROM ir_config_parameter "
        "WHERE key = 'etp_assessment_pro.subjective_points'")

    cr.execute("""
        UPDATE etp_assessment_pro_response
           SET llm_raw_100 = CASE
                 WHEN COALESCE(llm_raw_score, 0) > 0 THEN llm_raw_score * 100.0
                 WHEN llm_score >= 1 THEN 100.0
                 ELSE 0.0
               END
         WHERE COALESCE(llm_raw_100, 0) = 0
           AND llm_state = 'scored'
    """)

    scored = env["etp.assessment.pro.response"].search(
        [("llm_state", "=", "scored")])
    if scored:
        scored.modified(["llm_raw_100"])
        scored.flush_recordset()

    Draft = env["etp.assessment.pro.prompt.question"]
    for draft in Draft.search([("answer_dimension_ids", "=", False)]):
        if (draft.dimensions_json or draft.options_json
                or draft.correct_answer_json):
            draft._sync_answer_relational_from_json()
