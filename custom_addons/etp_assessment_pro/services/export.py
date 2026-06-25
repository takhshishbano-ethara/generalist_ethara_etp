# -*- coding: utf-8 -*-
"""CSV exporters for assessment results and responses.

Two distinct exports, both reachable from the assessment form and the second
also per-candidate from the evaluator screen:

- **Results** — ONE row per candidate: the scorecard (objective points,
  subjective points, total, %, pass/fail, rank, day progress, violations).
  This is the leaderboard / hiring-decision view.

- **Responses** — ONE row per submitted answer, fully detailed: the
  candidate's chosen options per dimension vs the correct key, objective
  points, the subjective LLM verdict + raw 0-1 score + reasoning, the
  justification text, and every question dimension. Scope can be the whole
  assessment or a single candidate.

Both return an ``ir.actions.act_url`` download of an ``ir.attachment`` (the
house pattern used elsewhere in this addon).
"""
import base64
import csv
import io


# ---------------------------------------------------------------------------
# Column specs (single source of truth; mirrored by the standalone sample
# generator so the docs and the live export never drift).
# ---------------------------------------------------------------------------
RESULTS_COLUMNS = [
    "rank", "candidate", "email", "assessment", "assessment_state",
    "candidate_state", "days_done", "days_total", "day_progress",
    "objective_score", "objective_max",
    "subjective_score", "subjective_max",
    "total_score", "total_max", "score_percent", "pass_threshold",
    "result", "subjective_scoring_state", "results_released",
    "is_locked", "violation_count", "started_at",
]

RESPONSES_COLUMNS = [
    "candidate", "email", "assessment", "day", "skill",
    "question", "question_type", "category", "difficulty", "prompt",
    "candidate_answer", "correct_answer", "dimension_detail",
    "objective_score", "objective_max",
    "needs_subjective", "subjective_result", "subjective_score",
    "subjective_max", "subjective_raw_0_1", "subjective_state",
    "subjective_reasoning", "justification",
    "total_score", "total_max", "response_state",
]


def _attachment_download(env, model, res_id, filename, content_bytes,
                         mimetype="text/csv"):
    att = env["ir.attachment"].create({
        "name": filename,
        "type": "binary",
        "datas": base64.b64encode(content_bytes).decode(),
        "mimetype": mimetype,
        "res_model": model,
        "res_id": res_id,
    })
    return {
        "type": "ir.actions.act_url",
        "url": f"/web/content/{att.id}?download=true",
        "target": "self",
    }


# ---------------------------------------------------------------------------
# Per-response detail helpers.
# ---------------------------------------------------------------------------
def _dimension_detail(resp):
    """Return (candidate_answer, correct_answer, detail) for one response.

    - candidate_answer: "Dim A: Paris | Dim B: 3, 5" (chosen per dimension)
    - correct_answer:   "Dim A: Paris | Dim B: 2, 3" (the key per dimension)
    - detail:           per-dimension "label=[chosen vs correct] ✓/✗" audit
      string so a reviewer can see exactly where points were won/lost.
    """
    rec = resp.sudo()
    q = rec.question_id
    # chosen master-option names grouped by dimension id
    chosen_by_dim = {}
    for line in rec.line_ids:
        if line.selected_option_id:
            chosen_by_dim.setdefault(line.dimension_id.id, []).append(
                line.selected_option_id.name)

    cand_parts, correct_parts, detail_parts = [], [], []
    for qd in q.question_dimension_ids:
        dim = qd.dimension_id
        chosen = chosen_by_dim.get(dim.id, [])
        correct = [ol.name for ol in qd.option_line_ids.filtered("is_correct")
                   if ol.name]
        if chosen:
            cand_parts.append(f"{dim.name}: {', '.join(chosen)}")
        if correct:
            correct_parts.append(f"{dim.name}: {', '.join(correct)}")
        if correct or chosen:
            ok = (set(s.strip().casefold() for s in chosen)
                  == set(s.strip().casefold() for s in correct))
            mark = "\u2713" if ok else "\u2717"
            detail_parts.append(
                f"{dim.name} [chose: {', '.join(chosen) or '-'} | "
                f"correct: {', '.join(correct) or '-'}] {mark}")
    # subjective-only questions have no dimensions; surface the justification.
    cand = " | ".join(cand_parts)
    if not cand and (rec.justification or "").strip():
        cand = (rec.justification or "").strip()
    return cand, " | ".join(correct_parts), " || ".join(detail_parts)


def _response_row(resp):
    rec = resp.sudo()
    q = rec.question_id
    emp = rec.evaluator_id
    day = rec.day_session_id
    cand, correct, detail = _dimension_detail(rec)
    obj = rec.score or 0
    obj_max = rec.max_score or 0
    subj = rec.llm_score or 0
    subj_max = rec.llm_max_score or 0
    return {
        "candidate": emp.partner_name or "",
        "email": emp.email_from or "",
        "assessment": rec.assessment_id.name or "",
        "day": day.day_sequence if day else "",
        "skill": (day.skill_id.name if day and day.skill_id else
                  " | ".join(q.skill_ids.mapped("name"))),
        "question": q.name or "",
        "question_type": q.question_type or "",
        "category": q.category_id.name or "",
        "difficulty": q.difficulty or "",
        "prompt": q.prompt or "",
        "candidate_answer": cand,
        "correct_answer": correct,
        "dimension_detail": detail,
        "objective_score": obj,
        "objective_max": obj_max,
        "needs_subjective": "yes" if rec.needs_llm else "no",
        "subjective_result": rec.subjective_result or "",
        "subjective_score": subj,
        "subjective_max": subj_max,
        "subjective_raw_0_1": round(rec.llm_raw_score, 4) if rec.needs_llm else "",
        "subjective_state": rec.llm_state or "",
        "subjective_reasoning": rec.llm_feedback or "",
        "justification": rec.justification or "",
        "total_score": obj + subj,
        "total_max": obj_max + subj_max,
        "response_state": rec.state or "",
    }


def _write_csv(columns, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Public builders.
# ---------------------------------------------------------------------------
def results_rows(assessment):
    ranked = assessment.assessment_evaluator_ids.sorted(
        key=lambda e: (-(e.score_percent or 0.0),
                       e.applicant_id.partner_name or ""))
    rows = []
    for idx, ev in enumerate(ranked, start=1):
        emp = ev.applicant_id
        obj = ev.total_score or 0
        obj_max = ev.max_possible_score or 0
        subj = ev.llm_total_score or 0
        subj_max = ev.llm_max_score or 0
        rows.append({
            "rank": idx,
            "candidate": emp.partner_name or "",
            "email": emp.email_from or "",
            "assessment": assessment.name or "",
            "assessment_state": assessment.state or "",
            "candidate_state": ev.state or "",
            "days_done": ev.days_done or 0,
            "days_total": ev.days_total or 0,
            "day_progress": ev.day_progress_label or "",
            "objective_score": obj,
            "objective_max": obj_max,
            "subjective_score": subj,
            "subjective_max": subj_max,
            "total_score": obj + subj,
            "total_max": obj_max + subj_max,
            "score_percent": ev.score_percent or 0.0,
            "pass_threshold": ev.pass_threshold or 0.0,
            "result": ev.result or "pending",
            "subjective_scoring_state": ev.llm_state or "",
            "results_released": "yes" if ev.results_released else "no",
            "is_locked": "yes" if ev.is_locked else "no",
            "violation_count": ev.violation_count or 0,
            "started_at": ev.started_at or "",
        })
    return rows


def export_results(assessment):
    assessment.ensure_one()
    rows = results_rows(assessment)
    content = _write_csv(RESULTS_COLUMNS, rows)
    return _attachment_download(
        assessment.env, assessment._name, assessment.id,
        f"{assessment.name}_results.csv", content)


def _summary_row(ev):
    """ONE summary row per candidate for the responses export: the post-scoring
    scorecard (objective correct/total + points, subjective pass/total + points,
    combined total, percent, result) laid into the response column schema so it
    reads as the first row of that candidate's block. Derived from the SAME
    stored response/evaluator fields as the detail rows below it — never a
    separate calculation, so the summary can't disagree with the breakdown.
    """
    submitted = ev.response_ids.filtered(lambda r: r.state == "submitted")
    obj = submitted.filtered(lambda r: r.has_objective)
    obj_total = len(obj)
    obj_correct = len(obj.filtered(
        lambda r: r.max_score and r.score >= r.max_score))
    subj = submitted.filtered(lambda r: r.needs_llm)
    subj_total = len(subj)
    subj_pass = len(subj.filtered(
        lambda r: r.llm_state == "scored" and r.llm_passed))
    pending = len(subj.filtered(
        lambda r: r.llm_state in ("pending", "queued", "failed")))

    obj_pts = ev.total_score or 0
    obj_max = ev.max_possible_score or 0
    sub_pts = ev.llm_total_score or 0
    sub_max = ev.llm_max_score or 0
    detail = (
        f"Objective: {obj_correct}/{obj_total} correct ({obj_pts}/{obj_max} pts)"
        f" | Subjective: {subj_pass}/{subj_total} passed "
        f"({sub_pts}/{sub_max} pts)")
    if pending:
        detail += f" | {pending} subjective pending (not final)"
    return {
        "candidate": ev.applicant_id.partner_name or "",
        "email": ev.applicant_id.email_from or "",
        "assessment": ev.assessment_id.name or "",
        "day": "",
        "skill": "",
        "question": "=== RESULT SUMMARY ===",
        "question_type": "summary",
        "category": "",
        "difficulty": "",
        "prompt": "",
        "candidate_answer": detail,
        "correct_answer": (ev.result or "pending").upper(),
        "dimension_detail": (
            f"answered {ev.answered_count or 0}/{ev.total_questions or 0}"
            + (f" | violations {ev.violation_count}"
               if ev.violation_count else "")),
        "objective_score": obj_pts,
        "objective_max": obj_max,
        "needs_subjective": "yes" if subj_total else "no",
        "subjective_result": ev.result or "pending",
        "subjective_score": sub_pts,
        "subjective_max": sub_max,
        "subjective_raw_0_1": round(ev.score_percent or 0.0, 2),
        "subjective_state": ev.llm_state or "",
        "subjective_reasoning": (
            f"score {round(ev.score_percent or 0.0, 2)}% vs threshold "
            f"{round(ev.pass_threshold or 0.0, 2)}% -> "
            f"{(ev.result or 'pending').upper()}"),
        "justification": "",
        "total_score": obj_pts + sub_pts,
        "total_max": obj_max + sub_max,
        "response_state": ev.state or "",
    }


def export_responses(assessment, evaluator=None):
    """Whole-assessment responses, or just one candidate's when ``evaluator``
    is given. One row per response, submitted ones first, ordered by candidate
    then day then question sequence. Each candidate's block is prefixed with a
    SUMMARY row (the post-scoring scorecard) so an admin reading the export
    sees the result at a glance before the per-answer detail.
    """
    assessment.ensure_one()
    if evaluator is not None:
        responses = evaluator.response_ids
        suffix = (evaluator.applicant_id.partner_name or "candidate").replace(
            " ", "_")
        fname = f"{assessment.name}_{suffix}_responses.csv"
    else:
        responses = assessment.response_ids
        fname = f"{assessment.name}_responses.csv"
    ordered = responses.sorted(
        key=lambda r: (
            r.evaluator_id.partner_name or "",
            r.day_session_id.day_sequence if r.day_session_id else 0,
            r.question_id.sequence or 0,
            r.id))
    # Build rows, inserting a per-candidate summary row at the start of each
    # candidate's block. For a single-candidate export that's just one summary
    # row up top; for the whole-assessment export it's one per candidate, in
    # the same candidate order as the detail rows.
    rows = []
    if evaluator is not None:
        rows.append(_summary_row(evaluator))
        rows.extend(_response_row(r) for r in ordered)
    else:
        seen = set()
        # Map applicant -> its evaluator on this assessment for the summary.
        ev_by_applicant = {
            ev.applicant_id.id: ev
            for ev in assessment.assessment_evaluator_ids}
        for r in ordered:
            app_id = r.evaluator_id.id
            if app_id not in seen:
                seen.add(app_id)
                ev = ev_by_applicant.get(app_id)
                if ev:
                    rows.append(_summary_row(ev))
            rows.append(_response_row(r))
    content = _write_csv(RESPONSES_COLUMNS, rows)
    res_id = evaluator.id if evaluator is not None else assessment.id
    model = evaluator._name if evaluator is not None else assessment._name
    return _attachment_download(
        assessment.env, model, res_id, fname, content)
