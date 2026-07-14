# -*- coding: utf-8 -*-
"""Load-test seeder for the etp_assessment_pro candidate portal.

Provisions, idempotently, everything a Locust harness needs to drive N virtual
candidates through the real Odoo website portal single-sitting exam flow:

  1. One reusable ``etp.assessment.pro`` bound to a Category whose question bank
     is a hand-built pool of MCQ/MSQ questions with KNOWN correct options (so the
     harness can submit valid answers) plus one subjective question (so the
     justification branch of ``_record_response`` is exercised).
  2. N candidates: an ``hr.applicant`` + a linked portal ``res.users``
     (``candidate_user_id``) each with a KNOWN password so Locust can
     ``POST /web/login``.
  3. Launches the exam via ``action_start`` so a per-candidate
     ``etp.assessment.pro.evaluator`` row + ``access_token`` is minted. Invite
     email delivery is NOT synchronous (action_start only queues invites for the
     background cron), so nothing blocks the seed transaction.
  4. Writes ``candidates.csv`` next to this file with columns:
         login,password,token,question_plan
     where ``question_plan`` is a URL-safe base64 of a JSON list encoding, per
     question in the evaluator's ``question_order``, the fields the harness needs
     to POST a valid answer:
         [{"qid": 42, "type": "mcq", "dims": {"7": [15]}, "text": ""},
          {"qid": 43, "type": "msq", "dims": {"7": [15, 16]}, "text": ""},
          {"qid": 44, "type": "subjective_rubric", "dims": {},
           "text": "<justification>"}]

Candidate channel is the Odoo WEBSITE PORTAL. The tokenized routes are
``auth="public"`` but redirect to ``/web/login`` when the session is public, so a
virtual candidate must FIRST authenticate a portal session and carry the session
cookie; the token only scopes which evaluator.

Run inside an Odoo shell against a THROWAWAY DB:

    N=300 odoo-bin shell -d etp_test --no-http \
        < custom_addons/etp_assessment_pro/tests/load/seed_load_data.py

Reads N from ``ETP_LOAD_CANDIDATES`` (or ``N``, default 300). Idempotent: reuses
the assessment / questions / candidates by marker name, tops up missing
evaluators, and rewrites the CSV. Not a TransactionCase; not discovered by the
test runner.
"""
import base64
import csv
import json
import logging
import os
import random
import uuid

_logger = logging.getLogger("etp_assessment_pro.load_seed")

MARKER = "LOAD-TEST"
ASSESSMENT_NAME = "%s Single-Sitting Load Cohort" % MARKER
CATEGORY_NAME = "%s Category" % MARKER
CANDIDATE_EMAIL_DOMAIN = "loadtest.example.com"
CANDIDATE_PASSWORD = "loadtest123"

QUESTIONS_TOTAL = 8
DURATION_MINUTES = 60
DEFAULT_N = 300

CANNED_JUSTIFICATION = (
    "Load-test canned justification: the correct choice follows from the "
    "stated premises. Filler text to satisfy the required justification field."
)


def _get_or_create_category(env):
    Category = env["etp.assessment.pro.prompt"]
    cat = Category.search([("name", "=", CATEGORY_NAME)], limit=1)
    if not cat:
        cat = Category.create({"name": CATEGORY_NAME})
    return cat


def _build_mcq(env, category, name, opt_names, correct_idx):
    Question = env["etp.assessment.pro.question"]
    QDim = env["etp.assessment.pro.question.dimension"]
    q = Question.create({
        "name": name,
        "question_type": "mcq",
        "prompt": "Load-test MCQ: pick option %s." % opt_names[correct_idx],
        "difficulty": "easy",
        "generator_id": category.id,
    })
    QDim.create({
        "question_id": q.id,
        "name": "Dim %s" % name,
        "option_line_ids": [
            (0, 0, {"name": n, "sequence": (i + 1) * 10,
                    "is_correct": i == correct_idx})
            for i, n in enumerate(opt_names)
        ],
    })
    return q


def _build_msq(env, category, name, opt_names, correct_idxs):
    Question = env["etp.assessment.pro.question"]
    QDim = env["etp.assessment.pro.question.dimension"]
    q = Question.create({
        "name": name,
        "question_type": "msq",
        "prompt": "Load-test MSQ: pick all correct options.",
        "difficulty": "medium",
        "generator_id": category.id,
    })
    QDim.create({
        "question_id": q.id,
        "name": "Dim %s" % name,
        "option_line_ids": [
            (0, 0, {"name": n, "sequence": (i + 1) * 10,
                    "is_correct": i in correct_idxs})
            for i, n in enumerate(opt_names)
        ],
    })
    return q


def _build_subjective(env, category, name):
    return env["etp.assessment.pro.question"].create({
        "name": name,
        "question_type": "subjective_rubric",
        "prompt": "Load-test subjective: explain your reasoning.",
        "difficulty": "medium",
        "generator_id": category.id,
    })


def _active_pool(env, category):
    return env["etp.assessment.pro.question"].search([
        ("generator_id", "=", category.id), ("active", "=", True)])


def _ensure_question_pool(env, category):
    """Top up the category to QUESTIONS_TOTAL keyed questions, rotating
    MCQ / true-false / MSQ / subjective. Idempotent: builds only the shortfall,
    so a re-run whose pool is already full adds nothing."""
    builders = [
        lambda i: _build_mcq(
            env, category, "%s Q%s MCQ" % (MARKER, i),
            ["Alpha", "Bravo", "Charlie", "Delta"], correct_idx=(i % 4)),
        lambda i: _build_mcq(
            env, category, "%s Q%s TF" % (MARKER, i),
            ["True", "False"], correct_idx=(i % 2)),
        lambda i: _build_msq(
            env, category, "%s Q%s MSQ" % (MARKER, i),
            ["W", "X", "Y", "Z"], correct_idxs=(0, 2)),
        lambda i: _build_subjective(
            env, category, "%s Q%s SUBJ" % (MARKER, i)),
    ]
    i = 0
    while len(_active_pool(env, category)) < QUESTIONS_TOTAL and i < QUESTIONS_TOTAL * 4:
        builders[i % len(builders)](i)
        i += 1


def _get_or_create_candidate(env, idx):
    Applicant = env["hr.applicant"].sudo()
    Users = env["res.users"].sudo()
    login = "loadcand_%04d@%s" % (idx, CANDIDATE_EMAIL_DOMAIN)
    name = "%s Candidate %04d" % (MARKER, idx)

    applicant = Applicant.search([("email_from", "=ilike", login)], limit=1)
    user = Users.with_context(active_test=False).search(
        [("login", "=", login)], limit=1)

    if not user:
        portal = env.ref("base.group_portal")
        company = (
            env.ref("base.main_company", raise_if_not_found=False)
            or env["res.company"].search([], limit=1, order="id asc"))
        user = Users.with_company(company).with_context(
            no_reset_password=True,
            mail_create_nosubscribe=True,
            mail_create_nolog=True,
            tracking_disable=True,
        ).create({
            "name": name,
            "login": login,
            "email": login,
            "company_id": company.id,
            "company_ids": [(6, 0, [company.id])],
            "group_ids": [(6, 0, [portal.id])],
        })
    if not user.active:
        user.active = True
    user.password = CANDIDATE_PASSWORD

    if not applicant:
        applicant = Applicant.create({
            "partner_name": name,
            "email_from": login,
            "partner_id": user.partner_id.id,
            "candidate_user_id": user.id,
        })
    else:
        vals = {}
        if not applicant.candidate_user_id:
            vals["candidate_user_id"] = user.id
        if not applicant.partner_id:
            vals["partner_id"] = user.partner_id.id
        if vals:
            applicant.write(vals)
    return applicant, login


def _get_or_create_assessment(env, category, applicants):
    Assessment = env["etp.assessment.pro"]
    assessment = Assessment.search([("name", "=", ASSESSMENT_NAME)], limit=1)
    if not assessment:
        assessment = Assessment.create({
            "name": ASSESSMENT_NAME,
            "generator_id": category.id,
            "question_limit": QUESTIONS_TOTAL,
            "duration_minutes": DURATION_MINUTES,
            "results_release": "immediate",
            "llm_auto_score": False,
            "max_violations": 0,
        })
    want_ids = set(applicants.ids)
    have_ids = set(assessment.evaluator_ids.ids)
    add = list(want_ids - have_ids)
    if add:
        assessment.write({"evaluator_ids": [(4, aid) for aid in add]})
    return assessment


def _launch_or_topup(env, assessment):
    """Launch the exam (draft) or top up evaluators for newly-added candidates
    (already in_progress). Each evaluator gets a shuffled question_order and an
    access_token; invites stay queued (the background cron sends them)."""
    if assessment.state == "draft":
        assessment.action_start()
        return
    pool = assessment.question_ids.ids or _active_pool(
        env, assessment.generator_id).ids
    limit = assessment.question_limit or len(pool)
    Evaluator = env["etp.assessment.pro.evaluator"]
    for applicant in assessment.evaluator_ids:
        ev = Evaluator.search([
            ("assessment_id", "=", assessment.id),
            ("applicant_id", "=", applicant.id)], limit=1)
        if ev:
            continue
        order = random.sample(pool, min(limit, len(pool)))
        random.shuffle(order)
        Evaluator.create({
            "assessment_id": assessment.id,
            "applicant_id": applicant.id,
            "question_order": json.dumps(order),
            "access_token": str(uuid.uuid4()),
            "invite_state": "queued",
        })


def _plan_entry_for_question(question):
    qtype = question.question_type
    entry = {"qid": question.id, "type": qtype, "dims": {}, "text": ""}
    if qtype in ("subjective_rubric",
                 "image_prompt", "image_label"):
        entry["text"] = CANNED_JUSTIFICATION
        return entry
    if qtype in ("mcq", "msq", "image_ab"):
        for qd in question.question_dimension_ids:
            correct_lines = qd.option_line_ids.filtered("is_correct")
            option_ids = correct_lines.ids
            if not option_ids:
                option_ids = qd.option_line_ids[:1].ids
            if not option_ids:
                continue
            if qtype == "msq":
                entry["dims"][str(qd.id)] = list(option_ids)
            else:
                entry["dims"][str(qd.id)] = [option_ids[0]]
        return entry
    entry["text"] = CANNED_JUSTIFICATION
    return entry


def _encode_question_plan(env, evaluator):
    Question = env["etp.assessment.pro.question"]
    order = json.loads(evaluator.question_order or "[]")
    plan = []
    for qid in order:
        q = Question.browse(qid)
        if not q.exists():
            continue
        plan.append(_plan_entry_for_question(q))
    raw = json.dumps(plan, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _csv_path():
    override = os.environ.get("ETP_LOAD_CSV")
    if override:
        return override
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        base = os.environ.get("ETP_LOAD_DIR") or os.getcwd()
    return os.path.join(base, "candidates.csv")


def _write_csv(env, assessment, login_by_applicant):
    """One row per candidate: login,password,token,question_plan."""
    evaluators = env["etp.assessment.pro.evaluator"].sudo().search(
        [("assessment_id", "=", assessment.id)], order="applicant_id")
    path = _csv_path()
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["login", "password", "token", "question_plan"])
        for ev in evaluators:
            applicant = ev.applicant_id
            login = login_by_applicant.get(applicant.id) or applicant.email_from
            if not login or not ev.access_token:
                continue
            writer.writerow([
                login,
                CANDIDATE_PASSWORD,
                ev.access_token,
                _encode_question_plan(env, ev),
            ])
            rows += 1
    return path, rows


def seed(env, num_candidates=None):
    if num_candidates is None:
        raw_n = os.environ.get("ETP_LOAD_CANDIDATES") or os.environ.get("N")
        try:
            num_candidates = int(raw_n) if raw_n else DEFAULT_N
        except (TypeError, ValueError):
            num_candidates = DEFAULT_N
    if num_candidates <= 0:
        num_candidates = DEFAULT_N

    _logger.info("Seeding %s load-test candidates...", num_candidates)

    category = _get_or_create_category(env)
    _ensure_question_pool(env, category)

    applicant_ids = []
    login_by_applicant = {}
    for idx in range(1, num_candidates + 1):
        applicant, login = _get_or_create_candidate(env, idx)
        applicant_ids.append(applicant.id)
        login_by_applicant[applicant.id] = login
    applicants = env["hr.applicant"].browse(applicant_ids)

    assessment = _get_or_create_assessment(env, category, applicants)
    _launch_or_topup(env, assessment)

    env.cr.commit()

    path, rows = _write_csv(env, assessment, login_by_applicant)

    evaluator_count = env["etp.assessment.pro.evaluator"].sudo().search_count(
        [("assessment_id", "=", assessment.id)])

    _logger.info("Load seed complete.")
    print("=" * 68)
    print("ETP Assessment Pro - load seed complete")
    print("-" * 68)
    print("Assessment      : %s (id=%s, state=%s)" % (
        assessment.name, assessment.id, assessment.state))
    print("Category        : %s" % assessment.generator_id.name)
    print("Candidates      : %s requested" % num_candidates)
    print("Evaluators      : %s (one token per candidate)" % evaluator_count)
    print("Password        : %s (same for all)" % CANDIDATE_PASSWORD)
    print("CSV             : %s (%s rows: login,password,token,question_plan)"
          % (path, rows))
    print("=" * 68)
    print("Locust login: POST /web/login with login=<login>&password=<password>,")
    print("carry the session cookie, then drive /pro_assessment/<token>.")
    print("Decode question_plan: json.loads(base64.urlsafe_b64decode(cell)) ->")
    print("  [{'qid','type','dims':{dim_id:[opt_ids]},'text'}, ...] (see README).")
    print("=" * 68)
    return assessment


try:
    env  # noqa: F821 - provided by the Odoo shell namespace when piped in
except NameError:
    env = None
if env is not None:  # pragma: no cover - only inside an Odoo shell
    seed(env)  # noqa: F821
