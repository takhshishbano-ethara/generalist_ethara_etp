# -*- coding: utf-8 -*-
"""Locust load-test harness modeling the REAL candidate journey for
etp_assessment_pro (Odoo 19, v19.0.1.3.1).

ADDITIVE-ONLY OPERATOR SCRIPT. This is NOT a unit test: it has no ``test_``
prefix and is NOT imported by ``tests/__init__.py``, so Odoo's test runner never
discovers it. It requires ``locust`` (``pip install locust``) — an OPTIONAL
tooling dependency that is deliberately NOT added to the module manifest.

WHY THIS SHAPE
--------------
The candidate channel is the Odoo WEBSITE PORTAL, not Flutter and not the
etp_assessment_extension REST API. Every candidate action is an HTTP form POST
against the tokenized portal routes in controllers/portal.py. Those routes are
declared ``auth="public"`` BUT portal.py redirects any *public* (unauthenticated)
visitor to /web/login:

    controllers/portal.py   assessment_landing -> redirect /web/login

So a virtual candidate MUST first establish a real portal SESSION COOKIE via
POST /web/login, then carry that cookie through the exam. The access_token in
the URL only scopes *which* evaluator / day-session is being answered; it does
NOT authenticate. Locust's ``HttpUser.client`` is a requests.Session, so the
``session_id`` cookie persists across requests once we log in.

CSRF: the POST exam routes (begin/submit/finish/violation) are ``csrf=False``
(portal.py:118/138/196/211 and the day equivalents at :298/:316/:365/:387), so
we do NOT need a csrf_token on those. The Odoo LOGIN form (/web/login) DOES
require a csrf_token, which we scrape from the GET /web/login HTML.

NO VERTEX / NO LLM ON THIS PATH
-------------------------------
The portal submit path (controllers/portal.py:572 ``_record_response``) only
upserts a response row and calls ``response.action_submit()`` /
``existing._enqueue_subjective_scoring()``. Scoring is NEVER inline: subjective
answers are graded later by the ``_cron_llm_auto_score`` cron via
services/scoring.py -> services/vertex.py:_call_vertex. Therefore this load test
exercises ZERO Vertex/LLM calls and needs no model budget. LLM/scoring load is a
SEPARATE concern (cron throughput). See README.md "What to watch".

KNOWN RACE THIS HARNESS CAN SURFACE (inventory open-question #19)
-----------------------------------------------------------------
``_record_response`` does ``search([...], limit=1)`` then create-or-overwrite
with NO row lock, backstopped by the unique index on
(assessment_evaluator_id, question_id). Concurrent double-submit of the same
question by the same candidate is a real race. A single Locust user is
sequential per-question, so to provoke it run duplicate rows for the same
candidate (README) or use the spike scenario with zero think-time.

================================================================================
CANDIDATE CSV SCHEMA  (path via env LOCUST_CANDIDATES_CSV; default: this dir's
candidates.csv). Produced by the seed script ``seed_load_data.py`` (see README
"Generating candidates.csv"). Columns MUST stay in sync with that seeder.

PRIMARY schema (multi-question, base64 plan):

    login,password,token,question_plan

  login          portal user login (res.users.login of the candidate user
                 linked to the hr.applicant via candidate_user_id).
  password       that user's password (the seeder sets a known password).
  token      access_token of the etp.assessment.pro.evaluator assigned to
                 this candidate. This test drives the single-sitting flow
                 (/pro_assessment/<token>...).
  question_plan  URL-safe base64 of a JSON list, IN question_order, one dict per
                 question the candidate answers:
                     {"qid": <int question_id>,
                      "type": "mcq|msq|subjective_rubric|image_ab|image_prompt|image_label",
                       "dims": {"<question_dimension_id>": [<option_id>, ...]},
                       "text": "<justification text or ''>"}
                  - "dims" maps the ``dimension_<qd.id>`` POST field -> option
                    id(s). mcq / image_ab / image_prompt-gate / image_label-gate =
                    single option; msq = the
                   list joined with commas (the template posts a comma-separated
                   CSV in ONE hidden ``dimension_<qd.id>`` field, portal.py:602
                   splits on ","). Values are per-question option-line ids —
                   _record_response validates them against
                   ``qd.option_line_ids.ids``
                   (portal.py:601) and silently drops anything else.
                  - "text" is REQUIRED for subjective_rubric /
                    image_prompt / image_label (portal.py:585)
                    else the
                   submit is silently dropped (returns False, no row written).
                 base64-of-JSON keeps embedded commas/quotes out of the CSV.

LEGACY schema (single-question; what the current seed_load_data.py emits) is
also accepted for backward compatibility:

    email(or login),password,token,question_id,option_id,dimension_field

  It is normalized into a one-item plan. Prefer the PRIMARY schema for multi-
  question days. See README "Generating candidates.csv".
================================================================================

RUN (headless, steady state):
    locust -f locustfile.py --headless -u 300 -r 20 \
           -H http://localhost:8069 --run-time 20m

SPIKE (deadline stampede):  LOCUST_SCENARIO=spike locust -f locustfile.py ...
See README.md for the soak/spike variants and tuning.
"""
import base64
import csv
import json
import logging
import os
import queue
import random
import re
import time

from locust import (HttpUser, LoadTestShape, between, constant, events, tag,
                    task)

_logger = logging.getLogger("etp.loadtest")

# ---------------------------------------------------------------------------
# Tunables (all overridable via environment so CI / operators need no edits).
# ---------------------------------------------------------------------------
DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "candidates.csv")
CANDIDATES_CSV = os.environ.get(
    "LOCUST_CANDIDATES_CSV",
    # Back-compat with the seeder's older env name.
    os.environ.get("ETP_LOAD_CSV", DEFAULT_CSV))

# "steady" (default) or "spike". Also selectable via the LoadTestShape below
# when Locust is started WITHOUT -u/-r (it then drives users itself).
SCENARIO = os.environ.get("LOCUST_SCENARIO", "steady").strip().lower()

# Target concurrency + ramp. When you pass -u/-r on the CLI those win; these are
# the defaults the LoadTestShape uses and the numbers documented in the README.
TARGET_USERS = int(os.environ.get("LOCUST_USERS", "300"))
SPAWN_RATE = float(os.environ.get("LOCUST_SPAWN_RATE", "20"))

# Think-time (seconds) between answering questions == the autosave/next cadence.
THINK_MIN = float(os.environ.get("LOCUST_THINK_MIN", "3"))
THINK_MAX = float(os.environ.get("LOCUST_THINK_MAX", "15"))

# Probability a given question also fires a proctoring-violation POST (optional,
# tagged "violation" so it can be excluded with --exclude-tags violation).
VIOLATION_PROB = float(os.environ.get("LOCUST_VIOLATION_PROB", "0.0"))

# A shared pool so each virtual user claims a DISTINCT candidate. Day-session
# tokens are per-candidate attempts and must not be shared across users; sharing
# would serialize writes on one row and hide real cross-candidate concurrency.
_POOL = queue.Queue()
_ALL_CANDIDATES = []  # kept for wrap-around when users outnumber CSV rows


def _decode_plan(encoded):
    """Decode the URL-safe-base64 JSON question plan into a python list.
    Empty / malformed plans yield [] so the user still exercises login+landing
    instead of crashing the whole locust user."""
    encoded = (encoded or "").strip()
    if not encoded:
        return []
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, TypeError) as exc:
        _logger.warning("bad question_plan (%s): %r", exc, encoded[:40])
        return []


def _normalize_row(raw):
    """Turn one CSV row (PRIMARY or LEGACY schema) into a candidate dict:
        {"login", "password", "token", "plan": [ {qid,type,dims,text}, ...]}
    Returns None for rows missing a login or token."""
    login = (raw.get("login") or raw.get("email") or "").strip()
    token = (raw.get("token") or "").strip()
    if not login or not token:
        return None
    password = (raw.get("password") or "").strip()

    plan = _decode_plan(raw.get("question_plan") or "")
    if not plan and raw.get("question_id"):
        # LEGACY single-question schema -> synthesize a one-item plan.
        dim_field = (raw.get("dimension_field") or "").strip()
        # dimension_field is the full POST field name "dimension_<id>".
        dim_id = dim_field[len("dimension_"):] if dim_field.startswith(
            "dimension_") else dim_field
        plan = [{
            "qid": int(raw["question_id"]),
            "type": (raw.get("question_type") or "mcq").strip() or "mcq",
            "dims": {dim_id: [raw.get("option_id")]} if dim_id else {},
            "text": (raw.get("justification") or "").strip(),
        }]
    return {"login": login, "password": password,
            "token": token, "plan": plan}


def _load_candidates(path):
    """Parse the seed CSV into normalized candidate dicts. Tolerant of a missing
    file so the module still imports/compiles; failure is reported at start."""
    rows = []
    if not os.path.exists(path):
        _logger.warning("candidates CSV not found at %s -- seed it first "
                        "(see README 'Generating candidates.csv').", path)
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            cand = _normalize_row(raw)
            if cand:
                rows.append(cand)
    return rows


# CSRF token lives in the /web/login form as a hidden input. Match both
# attribute orders (name-before-value and value-before-name).
_CSRF_RE = re.compile(
    r'name="csrf_token"[^>]*value="([^"]+)"'
    r'|value="([^"]+)"[^>]*name="csrf_token"')


def _extract_csrf(html):
    m = _CSRF_RE.search(html or "")
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


@events.test_start.add_listener
def _on_start(environment, **_kw):
    global _ALL_CANDIDATES
    _ALL_CANDIDATES = _load_candidates(CANDIDATES_CSV)
    # Fill the claim queue so each user gets a distinct candidate first.
    for cand in _ALL_CANDIDATES:
        _POOL.put(cand)
    if not _ALL_CANDIDATES:
        _logger.error(
            "No candidates loaded from %s. Users will spin up but cannot log "
            "in. Seed the CSV first (README).", CANDIDATES_CSV)
    else:
        _logger.info("Loaded %d candidates from %s (scenario=%s)",
                     len(_ALL_CANDIDATES), CANDIDATES_CSV, SCENARIO)


class CandidateJourney(HttpUser):
    """One virtual candidate: login -> hub -> open day -> begin -> answer loop
    -> review -> finish. Mirrors the real portal flow in controllers/portal.py.
    """

    # wait_time governs the pause BETWEEN whole journeys (tasks). The intra-
    # question think-time is applied explicitly inside the answer loop.
    if SCENARIO == "spike":
        # Spike: near-zero pause so submit/finish pile up together (deadline
        # stampede) and the no-lock upsert race (Q#19) is most likely to trip.
        wait_time = constant(0.1)
    else:
        wait_time = between(THINK_MIN, THINK_MAX)

    def on_start(self):
        """Claim a candidate identity and establish a portal session cookie."""
        try:
            self._candidate = _POOL.get_nowait()
        except queue.Empty:
            # More users than CSV rows: wrap around (multiple users share a
            # candidate). That intentionally exercises the same-token race.
            self._candidate = (random.choice(_ALL_CANDIDATES)
                               if _ALL_CANDIDATES else None)
        self._logged_in = self._login() if self._candidate else False

    # -- helpers -----------------------------------------------------------
    def _login(self):
        """POST /web/login to set the session cookie. Odoo's login form needs a
        csrf_token scraped from the GET page. Returns True on apparent success.
        Requests are named so per-endpoint p95 is readable in the stats table.
        """
        cand = self._candidate
        with self.client.get(
                "/web/login", name="GET /web/login",
                catch_response=True) as resp:
            if resp.status_code >= 400:
                resp.failure("login page %s" % resp.status_code)
                return False
            csrf = _extract_csrf(resp.text)
            resp.success()
        payload = {
            "login": cand["login"],
            "password": cand["password"],
            "csrf_token": csrf,
            # Drives Odoo to land us on the candidate hub on success; a failed
            # login re-renders /web/login (still 200).
            "redirect": "/my/pro_assessments",
        }
        with self.client.post(
                "/web/login", data=payload, name="POST /web/login",
                catch_response=True) as resp:
            if resp.status_code >= 400:
                resp.failure("login POST %s" % resp.status_code)
                return False
            # Heuristic: still on /web/login without any assessment content
            # means bad creds / unseeded user.
            if "/web/login" in (resp.url or "") \
                    and "pro_assessment" not in (resp.text or ""):
                resp.failure("login rejected (still on /web/login)")
                return False
            resp.success()
        return True

    def _think(self):
        if SCENARIO == "spike":
            return  # no pause under spike; users race the deadline
        time.sleep(random.uniform(THINK_MIN, THINK_MAX))

    def _submit_fields(self, item):
        """Build the POST body for ONE question submit, matching the exact form
        field names the template renders (views/portal_templates.xml:464-657)
        and that _record_response reads (controllers/portal.py:576-619)."""
        data = {
            "question_id": str(item.get("qid") or ""),
            "nav": "next",  # portal.py:337 uses this to pick the next question
        }
        qtype = item.get("type") or "mcq"
        # dimension_<id> fields: single option for
        # mcq/image_ab/image_prompt-gate/image_label-gate;
        # msq is a comma-separated CSV in ONE field (portal.py:602 splits on ",")
        for dim_id, opt_ids in (item.get("dims") or {}).items():
            vals = [str(o) for o in (opt_ids or []) if o not in (None, "")]
            if not vals:
                continue
            data["dimension_%s" % dim_id] = ",".join(vals)
        # justification REQUIRED for subjective_* and image_prompt/image_label
        # (portal.py:585).
        text = item.get("text") or ""
        if text or qtype in ("subjective_rubric",
                             "image_prompt", "image_label"):
            data["justification"] = text or (
                "Load-test answer for q%s" % item.get("qid"))
        data["csrf_token"] = getattr(self, "_exam_csrf", "")  # P3-1
        return data

    # -- the journey -------------------------------------------------------
    @task
    def full_journey(self):
        cand = getattr(self, "_candidate", None)
        if not cand or not getattr(self, "_logged_in", False):
            return
        token = cand["token"]

        # 1) Candidate hub (assigned assessments/days) - candidate_portal.py:112
        with self.client.get(
                "/my/pro_assessments", name="GET /my/pro_assessments",
                catch_response=True) as r:
            self._ok(r)

        # 2) Open the tokenized day landing - portal.py:232 (renders
        #    instructions when state == 'available', else the question runner).
        with self.client.get(
                "/pro_assessment/%s" % token,
                name="GET /pro_assessment/[token]",
                catch_response=True) as r:
            self._ok(r)
            self._exam_csrf = _extract_csrf(r.text or "")  # P3-1: POSTs need it

        # 3) Begin -> assessment_begin (started_at set), redirects back to landing. portal.py:297.
        #    Redirect is EXPECTED; requests follows it so status is usually 200.
        with self.client.post(
                "/pro_assessment/%s/begin" % token,
                data={"csrf_token": getattr(self, "_exam_csrf", "")},
                name="POST /pro_assessment/[token]/begin",
                catch_response=True) as r:
            self._ok(r)

        # 4) Answer loop: GET each question page (?q=<1-based idx>) then POST
        #    /submit with think-time between answers (the autosave cadence).
        plan = cand.get("plan") or []
        for idx, item in enumerate(plan, start=1):
            with self.client.get(
                    "/pro_assessment/%s?q=%d" % (token, idx),
                    name="GET /pro_assessment/[token]?q=",
                    catch_response=True) as r:
                self._ok(r)
                _t = _extract_csrf(r.text or "")
                if _t:
                    self._exam_csrf = _t

            # Optional proctoring violation (tagged; may be excluded).
            if VIOLATION_PROB and random.random() < VIOLATION_PROB:
                self.post_violation()

            with self.client.post(
                    "/pro_assessment/%s/submit" % token,
                    data=self._submit_fields(item),
                    name="POST /pro_assessment/[token]/submit",
                    catch_response=True) as r:
                self._ok(r)

            self._think()

        # 5) Review (optional GET) then Finish -> final submit / lock.
        with self.client.get(
                "/pro_assessment/%s/review" % token,
                name="GET /pro_assessment/[token]/review",
                catch_response=True) as r:
            self._ok(r)
        with self.client.post(
                "/pro_assessment/%s/finish" % token,
                data={"csrf_token": getattr(self, "_exam_csrf", "")},
                name="POST /pro_assessment/[token]/finish",
                catch_response=True) as r:
            self._ok(r)

    @tag("violation")
    @task(0)
    def post_violation(self):
        """Proctoring violation increment (portal.py:386). Registered as a
        zero-weight @task so it is never picked at random by the scheduler, and
        tagged 'violation' so operators can include/exclude it with
        --tags / --exclude-tags. It is normally called inline from the answer
        loop when VIOLATION_PROB fires."""
        cand = getattr(self, "_candidate", None)
        if not cand or not getattr(self, "_logged_in", False):
            return
        with self.client.post(
                "/pro_assessment/%s/violation" % cand["token"],
                data={"violation_reason": "tab_switch (load-test synthetic)",
                      "csrf_token": getattr(self, "_exam_csrf", "")},
                name="POST /pro_assessment/[token]/violation",
                catch_response=True) as r:
            self._ok(r)

    @staticmethod
    def _ok(response):
        """Success on any 2xx/3xx; fail only on real 4xx/5xx. Odoo answers
        begin/submit/finish with 302/303 redirects which requests follows, so
        the final status is usually 200 -- we must NOT abort on the expected
        redirect. A 4xx bounce back to /web/login means the session cookie was
        lost, which SHOULD fail (that is what we want to catch under load)."""
        if response.status_code < 400:
            response.success()
            return
        url = response.request.url if response.request else "?"
        response.failure("HTTP %s at %s" % (response.status_code, url))


# ---------------------------------------------------------------------------
# LoadTestShape: only DRIVES the run when Locust is started WITHOUT -u/-r AND
# LOCUST_SCENARIO=spike. With -u/-r the operator controls the ramp directly
# (recommended for steady/soak). This shape is a self-contained DEADLINE-SPIKE
# profile: warm ramp -> plateau -> hard spike to 2x near the "deadline" ->
# drain, reproducing everyone hitting submit/finish at once.
# ---------------------------------------------------------------------------
class DeadlineSpikeShape(LoadTestShape):
    _stages = [
        # (end_time_secs, users, spawn_rate)
        (120, max(1, TARGET_USERS // 2), SPAWN_RATE),   # warm ramp to 50%
        (600, TARGET_USERS, SPAWN_RATE),                # plateau at target
        (660, TARGET_USERS * 2, TARGET_USERS),          # SPIKE: 2x, fast
        (900, TARGET_USERS * 2, SPAWN_RATE),            # hold the spike
        (960, 0, TARGET_USERS),                         # drain
    ]

    def tick(self):
        if SCENARIO != "spike":
            return None  # let -u/-r (or the default steady rule) govern the run
        run_time = self.get_run_time()
        for end, users, rate in self._stages:
            if run_time < end:
                return (users, rate)
        return None  # end the test after the last stage
