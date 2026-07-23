# -*- coding: utf-8 -*-
"""Reusable Vertex AI / Gemini mocks for the etp_assessment_pro test suite.

WHY THIS FILE EXISTS
--------------------
Every LLM path in the module funnels through ONE seam:

    from odoo.addons.etp_assessment_pro.services import vertex
    vertex._call_vertex(env, system_prompt, user_text, max_tokens=..., ...) -> str

(see services/vertex.py:393). ``_call_vertex`` is the single HTTP chokepoint;
the public callers ``vertex.extract_skills`` (vertex.py:775),
``vertex.generate_questions`` (vertex.py:962) and the scorer
``scoring._score_submission`` (services/scoring.py:355, which calls
``vertex_svc._call_vertex`` at scoring.py:369) all go through it. Patch it and
the whole suite runs OFFLINE — no Vertex project, no billing, no pending budget.

This module is an IMPORTABLE HELPER, not a test:
  * it has NO ``test_`` prefix, so Odoo's test runner never collects it, and
  * it is deliberately NOT added to tests/__init__.py.
Import it explicitly from a real test module, e.g.::

    from odoo.addons.etp_assessment_pro.tests import vertex_fixtures as vf

It provides exactly what the real code parses, confirmed by reading the source:

  (a) skills_payload(...)        -> JSON string for vertex.extract_skills
  (b) questions_mcq_payload(...) -> JSON string for vertex.generate_questions
      questions_subjective_payload(...) / questions_image_*_payload(...)
  (c) score_payload(responses)   -> JSON string the grader parses
      (a list of dicts keyed by the response "id", carrying the 0-100 "score")

plus mock_vertex(...) — a context manager over
``patch.object(vertex, "_call_vertex", ...)`` supporting a fixed return, a
prompt-aware side_effect router (skills vs questions vs scoring), and a
refusal/error mode that raises vertex.LLMRefusalError so a test can exercise the
``llm_state == 'error'`` surfacing path (scoring._store_error, scoring.py:296).

GROUND-TRUTH SHAPES (re-confirmed against source — cited file:line):
  * Skills item keys read by extract_skills: name, description, tags,
    question_type, medium, question_count, time_minutes, difficulty
    (vertex.py:793-857).
  * MCQ/MSQ question item keys: name, prompt, question_type, difficulty,
    options (list), correct_answer (string OR 0-based index OR list for msq)
    (vertex.py:906-919, _answer_resolves vertex.py:872).
  * subjective_rubric needs rubric={checklist,constraints,pass_condition}
    (vertex.py:920-925).
  * image_ab image_specs: image_a_prompt, image_b_prompt, dimensions{label:verdict},
    official_reasoning. image_prompt image_specs: images[] each with a prompt,
    answer_key{ideal_prompt,...}; image_label image_specs: images[] each with a
    prompt, answer_key{ideal_labels,...} (vertex.py _validate_question_item).
  * Scoring result item keys read by the grader: id (== response id, coerced to
    int by scoring._result_id, scoring.py:263), score (0-100, or a 0-1 fraction
    that _coerce_100 scales up, scoring.py:252), and the audit fields
    rubric_source / gate / reference_answer / reasoning / feedback / flags
    (_store_scored, scoring.py:273). A missing/unmatched id -> that response is
    routed to llm_state 'error' (scoring._store_error via _score_submission,
    scoring.py:392-398).

USAGE (mirrors tests/test_scoring_v6.py:81 and tests/test_skill_upsert.py:57)::

    from unittest.mock import patch
    from odoo.addons.etp_assessment_pro.services import vertex, scoring
    from odoo.addons.etp_assessment_pro.tests import vertex_fixtures as vf

    # 1) Scoring — the common case (one call per candidate/sub-batch):
    with patch.object(vertex, "_call_vertex",
                      return_value=vf.score_payload(evaluator.response_ids)):
        scoring.score_evaluator(self.env, evaluator)

    # 2) Skill extraction:
    with patch.object(vertex, "_call_vertex", return_value=vf.skills_payload()):
        vertex.extract_skills(self.env, prompt)

    # 3) One patch that routes across a whole flow (extract -> generate -> score):
    with vf.mock_vertex(env=self.env, responses=evaluator.response_ids) as m:
        ...  # any code that calls extract_skills / generate_questions / scoring
        assert m.call_count >= 1

    # 4) Force a refusal to exercise the 'error' surfacing path:
    with vf.mock_vertex(mode="refusal"):
        scoring.score_evaluator(self.env, evaluator)   # -> llm_state 'error'

Or mix in ``VertexMockMixin`` for the tiny sugar helpers (see bottom of file).
"""
import contextlib
import json
from unittest.mock import patch

from odoo.addons.etp_assessment_pro.services import vertex


# --------------------------------------------------------------------------- #
# (a) Skills-extraction payload  (for vertex.extract_skills)
# --------------------------------------------------------------------------- #
# Keys match exactly what extract_skills reads (vertex.py:793-857). medium is
# kept COHERENT with question_type so the extractor does not silently rewrite it
# (vertex.py:808-811): text types use medium 'text', image types medium 'image'.
DEFAULT_SKILLS = [
    {
        "name": "Refund Policy Application",
        "description": "Apply the refund decision tree to edge cases.",
        "tags": "refunds,policy",
        "question_type": "mcq",
        "medium": "text",
        "question_count": 5,
        "time_minutes": 10,
        "difficulty": "medium",
    },
    {
        "name": "Customer Tone Calibration",
        "description": "Match the brand voice in a written reply.",
        "tags": "tone,writing",
        "question_type": "subjective_rubric",
        "medium": "text",
        "question_count": 3,
        "time_minutes": 20,
        "difficulty": "hard",
    },
]


def skill_item(name, question_type="mcq", medium="text", **overrides):
    """One skills-extraction item with every field extract_skills reads.

    Callers can override any field; unknown keys are passed through (the
    extractor ignores keys it does not read). Keeping medium/question_type
    coherent avoids the extractor's auto-normalization (vertex.py:808-811).
    """
    item = {
        "name": name,
        "description": overrides.pop("description", "Auto skill for %s." % name),
        "tags": overrides.pop("tags", ""),
        "question_type": question_type,
        "medium": medium,
        "question_count": overrides.pop("question_count", 5),
        "time_minutes": overrides.pop("time_minutes", 10),
        "difficulty": overrides.pop("difficulty", "medium"),
    }
    item.update(overrides)
    return item


def skills_payload(items=None):
    """JSON string for vertex.extract_skills. ``items`` may be a list of dicts;
    defaults to DEFAULT_SKILLS (2 text skills: one mcq, one subjective_rubric)."""
    return json.dumps(items if items is not None else DEFAULT_SKILLS)


# --------------------------------------------------------------------------- #
# (b) Question-generation payloads  (for vertex.generate_questions)
# --------------------------------------------------------------------------- #
# generate_questions requests exactly ``skill.question_count`` items and caps to
# it (vertex.py:998, 1028); it also VALIDATES each item per type and DROPS bad
# ones (_validate_question_item, vertex.py:901). These builders emit items that
# pass validation, so len(returned draft ids) == count.

def mcq_item(name="Refund within 24h",
             prompt="A customer requests a refund 12 hours after purchase. "
                    "The stated window is 24 hours. What is the correct action?",
             options=None, correct_answer=0, difficulty="easy"):
    """A valid mcq item. correct_answer may be a 0-based index (default) or the
    exact option string — both resolve (vertex.py:_answer_resolves, single=True)."""
    return {
        "name": name,
        "prompt": prompt,
        "question_type": "mcq",
        "difficulty": difficulty,
        "options": options or ["Issue the refund", "Deny it", "Escalate"],
        "correct_answer": correct_answer,
    }


def msq_item(name="Applicable refund conditions",
             prompt="Which of the following independently justify an immediate "
                    "refund under the stated 24-hour, unopened-item policy?",
             options=None, correct_answer=None, difficulty="medium"):
    """A valid msq item. correct_answer is a NON-EMPTY subset (list) of option
    strings or indices (vertex.py:_answer_resolves, single=False)."""
    return {
        "name": name,
        "prompt": prompt,
        "question_type": "msq",
        "difficulty": difficulty,
        "options": options or ["Within 24 hours", "Item unopened",
                               "Customer changed their mind after a month"],
        "correct_answer": correct_answer if correct_answer is not None else [0, 1],
    }


def subjective_rubric_item(name="Apology tone",
                           prompt="Write a two-sentence apology to a customer "
                                  "whose order shipped late, matching a warm, "
                                  "accountable brand voice.",
                           difficulty="hard"):
    """A valid subjective_rubric item. rubric MUST carry all three keys
    checklist/constraints/pass_condition (vertex.py:920-925)."""
    return {
        "name": name,
        "prompt": prompt,
        "question_type": "subjective_rubric",
        "difficulty": difficulty,
        "rubric": {
            "checklist": ["Acknowledges the delay", "Offers a concrete next step"],
            "constraints": ["Under 60 words", "No blame-shifting"],
            "pass_condition": "Warm, accountable, and actionable",
        },
    }


def image_ab_item(name="Sharper render",
                  prompt="Two renders were produced from the same brief. Judge "
                         "which better follows the instruction and explain why.",
                  dimensions=None, difficulty="medium"):
    """A valid image_ab item. Requires image_a_prompt AND image_b_prompt, a
    non-empty ``dimensions`` map, and official_reasoning (vertex.py:928-947).

    NOTE: the verdict labels in ``dimensions`` must be valid for the axis the
    skill resolves. When a skill defines its OWN dimensions, pass matching
    labels/verdicts; the defaults use the built-in A/B verdicts (constants
    AB_CHOICES: 'Response A' / 'Response B' / 'Both Good' / 'Both Bad', and the
    default axis label 'Overall Choice' from constants AB_DIMENSION_NAMES).
    """
    return {
        "name": name,
        "prompt": prompt,
        "question_type": "image_ab",
        "difficulty": difficulty,
        "image_specs": {
            "image_a_prompt": "A photorealistic desk with a crisp, in-focus blue "
                              "ceramic mug, warm daylight, shallow depth of field.",
            "image_b_prompt": "The same desk with a visibly blurry, out-of-focus "
                              "blue ceramic mug, flat overcast lighting, wide "
                              "framing.",
            "dimensions": dimensions or {"Overall Choice": "Response A"},
            "official_reasoning": "A is in focus and matches the brief; B is "
                                  "blurred and misframed.",
        },
    }


def image_prompt_item(name="Write the prompt",
                      prompt="Study the reference image, then write the "
                             "text-to-image prompt that would reproduce it.",
                      difficulty="medium"):
    """A valid image_prompt item. Needs images[] with a prompt AND
    answer_key.ideal_prompt (vertex.py _validate_question_item)."""
    return {
        "name": name,
        "prompt": prompt,
        "question_type": "image_prompt",
        "difficulty": difficulty,
        "image_specs": {
            "images": [{
                "slot": "reference",
                "label": "Reference",
                "prompt": "A photorealistic close-up of a machined aluminium "
                          "bracket with a hairline crack across one mounting hole.",
            }],
            "answer_key": {
                "ideal_prompt": "A photorealistic close-up of a machined "
                                "aluminium bracket with a hairline crack across "
                                "one mounting hole, warm daylight.",
                "mandatory_elements": ["aluminium bracket", "hairline crack",
                                       "mounting hole"],
                "penalty_rules": ["vague or generic prompt"],
                "scoring_guide": "Full credit only if the subject, defect and "
                                 "framing are all named.",
            },
        },
    }


def image_label_item(name="Label the defect",
                     prompt="Label the visible defect on the pictured part "
                            "and state whether it passes QA.",
                     difficulty="medium"):
    """A valid image_label item. Needs images[] with a prompt AND
    answer_key.ideal_labels (vertex.py _validate_question_item)."""
    return {
        "name": name,
        "prompt": prompt,
        "question_type": "image_label",
        "difficulty": difficulty,
        "image_specs": {
            "images": [{
                "slot": "single",
                "label": "Part under inspection",
                "prompt": "A photorealistic close-up of a machined aluminium "
                          "bracket with a hairline crack across one mounting hole.",
            }],
            "answer_key": {
                "ideal_labels": "A hairline crack runs across a mounting hole; "
                                "the part FAILS QA.",
                "mandatory_elements": ["identifies the crack", "states FAIL"],
                "penalty_rules": ["marks it as pass"],
                "scoring_guide": "Full credit only if both the defect and the "
                                 "fail verdict are stated.",
            },
        },
    }


# Convenience payload builders (JSON strings) matching generate_questions ---- #
def questions_mcq_payload(count=2):
    return json.dumps([
        mcq_item(name="Refund within 24h", correct_answer=0),
        mcq_item(name="Refund after 30 days",
                 prompt="A refund is requested 31 days after purchase, past the "
                        "stated 30-day window. What is the correct action?",
                 options=["Issue full refund", "Deny", "Issue partial credit"],
                 correct_answer=1, difficulty="medium"),
    ][:count])


def questions_msq_payload(count=1):
    return json.dumps([msq_item()][:count])


def questions_subjective_payload(count=1):
    return json.dumps([subjective_rubric_item()][:count])


def questions_image_ab_payload(count=1, dimensions=None):
    return json.dumps([image_ab_item(dimensions=dimensions)][:count])


def questions_image_prompt_payload(count=1):
    return json.dumps([image_prompt_item()][:count])


def questions_image_label_payload(count=1):
    return json.dumps([image_label_item()][:count])


def questions_payload_for_skill(skill):
    """Return a generate_questions JSON string whose items match the skill's
    ``question_type`` and ``question_count``. Handy when a test drives generation
    off a real skill record without hand-picking a builder."""
    qtype = skill.question_type or "mcq"
    count = max(1, int(skill.question_count or 1))
    builder = {
        "mcq": lambda: [mcq_item(name="Q%d" % i) for i in range(count)],
        "msq": lambda: [msq_item(name="Q%d" % i) for i in range(count)],
        "subjective_rubric":
            lambda: [subjective_rubric_item(name="Q%d" % i) for i in range(count)],
        "image_ab": lambda: [image_ab_item(name="Q%d" % i) for i in range(count)],
        "image_prompt":
            lambda: [image_prompt_item(name="Q%d" % i) for i in range(count)],
        "image_label":
            lambda: [image_label_item(name="Q%d" % i) for i in range(count)],
    }.get(qtype, lambda: [mcq_item(name="Q%d" % i) for i in range(count)])
    return json.dumps(builder())


# --------------------------------------------------------------------------- #
# (c) Scoring payload  (what scoring._score_submission / the grader parses)
# --------------------------------------------------------------------------- #
def _v6_passed(score):
    """The advisory v6 pass flag: score (0-1, or a 0-100 percent) vs 0.70."""
    val = float(score)
    if val > 1.0:
        val = val / 100.0
    return val >= 0.70


def score_result(response_id, score=0.85, *, rubric_source="supplied",
                 gate="none", reference_answer="A meets the bar because ...",
                 reasoning="Checklist items satisfied by the answer.",
                 verdict_consistency="match", passed=None,
                 feedback="Solid, evidence-backed answer.", flags=None,
                 skills=None):
    """ONE subjective-judge-v6 result dict for a response.

    Keys mirror the v6 per-result contract; _store_scored reads score/gate/
    rubric_source/reference_answer/reasoning/feedback/flags and stores the whole
    dict in llm_result_json (scoring.py), so the v6-only fields (item_id,
    field_key, skills, verdict_consistency, passed) ride along as audit data.
      * id / item_id -> echoed response id (int + the v6 string form)
      * score        -> 0.00-1.00 float (_coerce_100 scales it to 0-100)
      * passed       -> ADVISORY ONLY; the platform ignores it for pass/fail
    """
    return {
        "item_id": str(response_id),
        "id": int(response_id),
        "field_key": "justification",
        "skills": skills if skills is not None else [],
        "rubric_source": rubric_source,
        "rubric": {},
        "reference_answer": reference_answer,
        "gate": gate,
        "reasoning": reasoning,
        "verdict_consistency": verdict_consistency,
        "flags": flags if flags is not None else [],
        "score": score,
        "passed": passed if passed is not None else _v6_passed(score),
        "feedback": feedback,
    }


def score_payload(responses, *, score=0.85, per_id=None, wrap=True, **kwargs):
    """JSON string the grader returns for a submission (the ``_call_vertex``
    return value scoring._score_submission expects), as the subjective-judge-v6
    WRAPPER object {schema_version, pass_threshold, submission_flags, results}.

    ``responses`` may be an Odoo recordset OR any iterable of objects/ids
    exposing ``.id`` (or bare ints). One result dict is emitted per response,
    each keyed by that response's id — matching scoring._score_submission's
    ``by_id`` lookup. Any id NOT in the payload is surfaced as an 'error'.

    Args:
      score:   default 0.00-1.00 score applied to every response.
      per_id:  optional {response_id: score} override for specific responses.
      wrap:    v6 wrapper object (default). Set False for a bare results array —
               _parse_results accepts both.
      kwargs:  forwarded to score_result (rubric_source/gate/feedback/...).
    """
    per_id = per_id or {}
    results = []
    for r in responses:
        rid = getattr(r, "id", r)
        results.append(score_result(rid, per_id.get(rid, score), **kwargs))
    if not wrap:
        return json.dumps(results)
    return json.dumps({
        "schema_version": "subjective-judge-v6",
        "worker_id": None,
        "attempt_id": None,
        "pass_threshold": 0.70,
        "submission_flags": [],
        "results": results,
    })


# A payload string that yields NO usable per-item score for anything: an empty
# results set means every response in the batch is routed to _store_error
# ('failed' then 'error' at the attempt cap). Useful for the missing-result path.
EMPTY_SCORE_PAYLOAD = json.dumps({
    "schema_version": "subjective-judge-v6", "pass_threshold": 0.70,
    "submission_flags": [], "results": []})


# --------------------------------------------------------------------------- #
# The patch-based mock: mock_vertex(...)
# --------------------------------------------------------------------------- #
# System prompts are stable text set by the service, so the router keys off the
# grader's user message ("Grade every candidate answer ...", scoring.py:361) and
# the generation directive ("Generate exactly N question(s) ...", vertex.py:983
# / 634). Anything else falls through to the skills payload.
def _looks_like_scoring(system_prompt, user_text):
    st = (system_prompt or "")
    ut = (user_text or "")
    return ("grader" in st.lower()
            or "Grade every candidate answer" in ut
            or "Grade the submission" in ut
            or "subjective-judge-v6" in ut
            or '"items"' in ut)


def _looks_like_questions(system_prompt, user_text):
    ut = (user_text or "")
    return ("Generate exactly" in ut or "SKILL TO TEST" in ut)


def _multipass_note(kwargs):
    """The P2 quality passes tag their _call_vertex with usage_ctx['note'] =
    'critique' / 'coverage-topup' / 'solutions-backfill'. Route off that so the
    mock can exercise the multi-pass path deterministically (and so call-count
    assertions can prove each pass fired)."""
    ctx = kwargs.get("usage_ctx") or {}
    return (ctx.get("note") or "") if isinstance(ctx, dict) else ""


def _usage_operation(kwargs):
    ctx = kwargs.get("usage_ctx") or {}
    return (ctx.get("operation") or "") if isinstance(ctx, dict) else ""


def _routing_side_effect(env=None, responses=None,
                         skills=None, questions=None,
                         topup=None, critique=None, backfill=None):
    """Build a side_effect(env, system_prompt, user_text, **kw) that returns the
    right JSON string per call: scoring -> score_payload, question-gen ->
    ``questions``/questions_mcq_payload, the P2 passes (coverage-topup /
    solutions-backfill / critique) -> their payloads, else -> skills_payload."""
    skills_str = skills if skills is not None else skills_payload()
    questions_str = questions if questions is not None else questions_mcq_payload()
    resp_recs = responses

    def _side_effect(call_env, system_prompt, user_text, *args, **kwargs):
        if _looks_like_scoring(system_prompt, user_text):
            recs = resp_recs
            if recs is None:
                # No responses handed in: echo back whatever ids appear in the
                # submitted items so the scorer still matches every row.
                recs = _ids_from_items(user_text)
            return score_payload(recs)
        note = _multipass_note(kwargs)
        if note == "coverage-topup":
            # Default: an EMPTY questions array (no shortfall to fill) - a test
            # that wants real top-up passes ``topup=`` an envelope payload.
            return topup if topup is not None else '{"questions": [], "solutions": []}'
        if note == "solutions-backfill":
            return backfill if backfill is not None else "[]"
        if note == "critique":
            # Default: no corrections, no issues - critique is a safe no-op.
            return critique if critique is not None else '{"solutions": [], "issues": []}'
        # Primary SOP question generation: keyed by operation, so it matches even
        # when the directive text ("Generate exactly" vs the fallback wording)
        # varies. The multipass notes above are already handled.
        if _usage_operation(kwargs) == "generate_questions":
            return questions_str
        if _looks_like_questions(system_prompt, user_text):
            return questions_str
        return skills_str

    return _side_effect


def _ids_from_items(user_text):
    """Best-effort: pull the response ids out of the grader's user_text (it
    embeds json.dumps({"items": [...]}) at scoring.py:365) so a routed scoring
    call can echo them even when the caller did not pass the recordset."""
    try:
        start = user_text.index("{")
        payload = json.loads(user_text[start:])
        items = payload.get("items") or []
        return [it.get("id") for it in items if isinstance(it, dict)
                and it.get("id") is not None]
    except Exception:
        return []


class _Refusal:
    """Marker sentinel; see mode='refusal'."""


@contextlib.contextmanager
def mock_vertex(return_value=None, *, mode="route", env=None, responses=None,
                skills=None, questions=None, side_effect=None,
                topup=None, critique=None, backfill=None,
                refusal_message="mocked refusal: the model declined"):
    """Patch ``vertex._call_vertex`` for the duration of the block.

    Modes:
      * mode="fixed" (or pass ``return_value``): every call returns the SAME
        string. Simplest — mirrors tests/test_scoring_v6.py:81.
      * mode="route" (default): a prompt-aware router returns the scoring /
        questions / skills payload depending on the call. Pass ``responses``
        (a recordset) so routed scoring calls echo the right ids; pass
        ``skills`` / ``questions`` to override those two payloads.
      * mode="refusal": every call raises ``vertex.LLMRefusalError`` — the same
        exception the real transport raises on a safety block / empty answer
        (vertex.py:323, 442-475). In the scoring path this drives every response
        in the batch to llm_state 'failed' then 'error' at the attempt cap
        (scoring._store_error, scoring.py:296-315).
      * ``side_effect=<callable>``: full control; the callable receives the same
        args as _call_vertex(env, system_prompt, user_text, **kw).

    Yields the MagicMock so a test can assert ``m.call_count`` / ``m.assert_*``.
    """
    if side_effect is not None:
        cm = patch.object(vertex, "_call_vertex", side_effect=side_effect)
    elif mode == "refusal":
        def _refuse(*_a, **_kw):
            raise vertex.LLMRefusalError(refusal_message)
        cm = patch.object(vertex, "_call_vertex", side_effect=_refuse)
    elif return_value is not None or mode == "fixed":
        cm = patch.object(
            vertex, "_call_vertex",
            return_value=return_value if return_value is not None
            else skills_payload())
    else:  # mode == "route"
        cm = patch.object(
            vertex, "_call_vertex",
            side_effect=_routing_side_effect(
                env=env, responses=responses,
                skills=skills, questions=questions,
                topup=topup, critique=critique, backfill=backfill))
    with cm as m:
        yield m


# --------------------------------------------------------------------------- #
# Optional test mixin - tiny sugar over the helpers above.
# --------------------------------------------------------------------------- #
class VertexMockMixin(object):
    """Mix into a TransactionCase for one-liner Vertex mocking. Example::

        class TestFoo(VertexMockMixin, TransactionCase):
            def test_scores(self):
                ev = self._make_evaluator_with_subjective_answers()
                with self.mock_vertex(responses=ev.response_ids):
                    scoring.score_evaluator(self.env, ev)
                ev.response_ids.invalidate_recordset()
                self.assertTrue(all(r.llm_state == "scored"
                                    for r in ev.response_ids if r.needs_llm))
    """

    def mock_vertex(self, **kwargs):
        """Context manager wrapping module-level mock_vertex; passes self.env."""
        kwargs.setdefault("env", getattr(self, "env", None))
        return mock_vertex(**kwargs)

    def patch_vertex_score(self, responses, **kwargs):
        """Patch _call_vertex to return a fixed score_payload for ``responses``.
        Mirrors tests/test_scoring_v6.py:81 (fixed return_value)."""
        return mock_vertex(return_value=score_payload(responses, **kwargs))

    def patch_vertex_skills(self, items=None):
        return mock_vertex(return_value=skills_payload(items))

    def patch_vertex_refusal(self, message="mocked refusal"):
        return mock_vertex(mode="refusal", refusal_message=message)
