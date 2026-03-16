"""
LLM actions with batch processing via Gemini and OpenAI Batch APIs.

--------------------------------------------------------------------------------
TASK-BASED PIPELINE (recommended flow)
--------------------------------------------------------------------------------

  Step 1 — Prompt rejection
    INPUT:  List[dict] with task_id and prompt.
    OUTPUT: Same list with rejection_status, rejection_reason, justification, accepted appended.
    API:    run_prompt_rejection_for_tasks(tasks, ...)

  Step 2 — Response generation (Gemini 3 Pro + GPT 5.2)
    INPUT:  List[dict] with task_id and prompt (passed tasks only).
    OUTPUT: List[dict] with task_id, prompt, gemini_response, gpt_response.
    API:    run_response_generation_for_tasks(openai_api_key, tasks, ...)

  Step 2b — Opalite / Ophelia response generation (Meta GenAI API)
    INPUT:  List[dict] with task_id and prompt.
    OUTPUT: List[dict] with task_id, prompt, opalite_response, ophelia_response, dialog_id.
    API (sync):  opalite_ophelia_generation_for_tasks_sync(genai_api_key, tasks, ...)
    API (batch): run_opalite_ophelia_generation_for_tasks(genai_api_key, tasks, ...)
    Helpers:     get_genai_api_key(), get_genai_dialog_id(genai_api_key),
                 generate_opalite_ophelia(prompt, genai_api_key, ...),
                 _genai_upload_attachment(genai_api_key, file_bytes, filename, mime_type)

  Step 3 — Evaluation
    INPUT:  List[dict] with task_id, prompt, response_a, response_b, gemini_response, gpt_response.
    OUTPUT: List[dict] with task_id, prompt, all ratings and justifications (evaluation_result,
            comparison_ab, comparison_vs_gemini, comparison_vs_gpt, rubrics, sxs_winner_label).
    API:    run_evaluation_for_tasks(evaluation_inputs, ...)

  Step 3b — Enhanced-prompt rubric creation & per-response rating
    Rubrics are derived from enhancements added to the original prompt during prompt enhancement.
    Each rubric is rated 1-6 independently for all 4 responses (opalite, ophelia, gemini, gpt).
    INPUT:  original_prompt, enhanced_prompt, opalite_response, ophelia_response, gemini_response, gpt_response.
    OUTPUT: {rubrics: [...], rubric_ratings: [{rubric_name, opalite: {score, reason}, ophelia, gemini, gpt}, ...]}
    API (sync):  create_and_rate_rubrics_sync(original_prompt, enhanced_prompt, ...)
    API (batch): batch_create_and_rate_rubrics(tasks, ...)
    Kimi:        create_and_rate_rubrics_sync_kimi / batch_create_and_rate_rubrics_kimi
    Low-level:   create_rubrics_from_enhanced_prompt_sync, rate_rubrics_for_responses_sync

  Step 4 — Final output is the list returned by Step 3 (all ratings and justifications).

Chunking: Steps 1-3 accept optional max_requests_per_batch (default 500 for steps 1-2, 1000 for step 3).
Large task lists (e.g. 5000+) are split into multiple API batches; results are merged in order.

Model defaults for Step 2: Gemini 3.1 Pro (DEFAULT_GEMINI_GENERATION_MODEL), GPT 5.2 (DEFAULT_OPENAI_GENERATION_MODEL).

--------------------------------------------------------------------------------
LEGACY / LOW-LEVEL
--------------------------------------------------------------------------------

1) batch_prompt_rejection_check(user_prompts, ...)
   INPUT:  user_prompts: List[str]
   OUTPUT: (results, accepted_indices) — results by index, no task_id.

2) run_batch_pipeline(openai_api_key, evaluation_inputs, ...)
   Single-call pipeline: rejection → evaluate A/B → compare A/B → generate Gemini+GPT → compare external → rubrics.
   INPUT:  evaluation_inputs: List[dict] with prompt, response_a, response_b.
   OUTPUT: dict with rejection_results, evaluation_results, comparison_results, gemini_responses, gpt_responses, external_model_comparisons, rubrics_results.

3) perform_qc_checks_batch(qc_inputs, ...)
   INPUT:  qc_inputs: List[dict] with manual human ratings (ab_comment, ab_preference, human_ab_gpt_comment, human_ab_gemini_comment, human_* rubric fields). Optional: response_a, response_b, gemini_response, gpt_response for comment-response grounding.
   OUTPUT: List[dict] with qc_status, overall_severity, checks (ai_detection, rubric_comment_grounding, ab_preference_comment_grounding, rubric_rating_justification, external_preference_comment_grounding), summary, error.

--------------------------------------------------------------------------------
Kimi uses the same prompts and output schemas as Gemini. LLM backend: AWS Bedrock
Converse API (Kimi K2.5 model ID: moonshotai.kimi-k2.5). No Kimi batch API; batch
variants use parallel sync (ThreadPoolExecutor) with call_kimi_sync.
Set BEDROCK_REGION or AWS_REGION and VALOR_BEDROCK_API_KEY.
Requires: pip install boto3

  - call_kimi_sync(kimi_api_key, model, system_prompt, user_content, ...)
  - prompt_rejection_check_sync_kimi(kimi_api_key, user_prompts, model=...)
  - batch_prompt_rejection_check_kimi(kimi_api_key, user_prompts, model=..., max_workers=...)
  - run_prompt_rejection_for_tasks_kimi(kimi_api_key, tasks, model=..., max_workers=...)
  - evaluation_for_tasks_sync_kimi(kimi_api_key, evaluation_inputs, evaluation_model=...)
  - run_evaluation_for_tasks_kimi(kimi_api_key, evaluation_inputs, evaluation_model=..., max_workers=...)
  - perform_qc_checks_sync_kimi(kimi_api_key, qc_inputs, model=...)
  - perform_qc_checks_batch_kimi(kimi_api_key, qc_inputs, model=..., max_workers=...)
"""

import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from .. import output_parsing
except ImportError:
    output_parsing = (
        None  # optional: output_parsing module may not be on path in all contexts
    )

try:
    import boto3
    import botocore
except ImportError:
    boto3 = None  # type: ignore
    botocore = None  # type: ignore

# Google GenAI unified SDK — Vertex AI Express (primary Gemini provider)
try:
    from google import genai as _google_genai
    from google.genai import types as _genai_types
except ImportError:
    _google_genai = None  # type: ignore[assignment]
    _genai_types = None  # type: ignore[assignment]


# Load .env from custom_addons root (shared across all modules)
def _addon_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


try:
    from dotenv import load_dotenv

    _addons_dir = os.path.dirname(_addon_root())
    _env_path = os.path.join(_addons_dir, ".env")
    if os.path.isfile(_env_path):
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv not installed; rely on os.environ set by shell/config


def get_vertex_api_key() -> str:
    """Return the API key used by Vertex AI Express and the Gemini Batch REST API.

    Reads VERTEX_AI_API_KEY (preferred) or GOOGLE_API_KEY from the environment —
    the same variables consumed by :func:`_get_vertex_client`.
    """
    return (
        os.environ.get("VERTEX_AI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    ).strip()


# Keep as a deprecated alias so any external callers (e.g. controllers.py) don't break.
get_gemini_api_key = get_vertex_api_key


def get_openai_api_key() -> Optional[str]:
    """Return OpenAI API key from environment (set in .env or OPENAI_API_KEY), or None if unset."""
    v = os.environ.get("OPENAI_API_KEY", "").strip()
    return v if v else None


def get_kimi_api_key() -> str:
    """Return Kimi (Moonshot) API key from environment (KIMI_API_KEY or MOONSHOT_API_KEY)."""
    return (
        os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY") or ""
    ).strip()


# Default Kimi model for rejection, evaluation, and QC (not used for response generation).
DEFAULT_KIMI_MODEL = "kimi-k2.5"
# AWS Bedrock model ID for Kimi K2.5 (Moonshot on Bedrock)
BEDROCK_KIMI_MODEL_ID = "moonshotai.kimi-k2.5"
# Moonshot direct API base URL
MOONSHOT_API_BASE_URL = os.environ.get("KIMI_API_URL", "https://api.moonshot.ai/v1")
# OpenRouter API
OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter model ID for Kimi (Moonshot on OpenRouter)
OPENROUTER_KIMI_MODEL_ID = os.environ.get(
    "OPENROUTER_KIMI_MODEL", "moonshotai/moonlight-16b-a3b-instruct"
)
# OpenRouter model ID for Gemini fallback
OPENROUTER_GEMINI_MODEL_ID = os.environ.get(
    "OPENROUTER_GEMINI_MODEL", "google/gemini-pro-1.5"
)


def _get_bedrock_region() -> str:
    """Return AWS region for Bedrock from VALOR_REGION, BEDROCK_REGION, or AWS_REGION."""
    return (
        os.environ.get("VALOR_REGION")
        or os.environ.get("BEDROCK_REGION")
        or os.environ.get("AWS_REGION")
        or ""
    ).strip()


# =============================================================================
# ORIGINAL CODE — COMMENTED OUT (replaced by batch implementation below)
# =============================================================================
# The following original functions are no longer in use; see git history for
# the full synchronous implementation:
#
#   - call_gemini(system_prompt, user_content, api_key, model, response_format, timeout)
#   - call_openai(user_prompt, api_key, model, timeout)
#   - evaluate_two_llm_responses_gemini(user_prompt, response_a, response_b, api_key)
#   - compare_two_llm_responses(api_key, user_prompt, response_1, response_2, response_c, response_d)
#   - check_prompt_rejection_status(api_key, user_prompt)
#   - generate_response_gemini(user_prompt, api_key)
#   - generate_response_gpt(user_prompt, api_key)
#   - create_additional_rubrics(api_key, user_prompt, gemini_response, gpt_response)
#   - compare_three_llm_responses(api_key, user_prompt, response_a, response_b, response_c)
#   - perform_qc_checks(api_key, response_text, ...ratings/justifications...)
#
# Batch equivalents: batch_prompt_rejection_check, run_batch_pipeline, perform_qc_checks_batch.
# =============================================================================


# =============================================================================
# SYSTEM PROMPTS (one per concern: rejection, evaluation, QC)
# =============================================================================

PROMPT_REJECTION_SYSTEM_PROMPT = """
You are an expert prompt quality judge.

Your task is to determine whether the given USER PROMPT should be ACCEPTED or REJECTED.

Reject the prompt ONLY if it falls into ANY of the following categories:

-------------------------
PROMPT REJECTION REASONS

1) Image Handling
Requests to create, draw, generate, interpret, or analyze images.
Example: "Draw me a cat riding a skateboard."

2) Missing Reference Text
Prompt refers to text, code, or data not provided.
Example: "Refactor this Python code into two functions." (no code given)

3) Safety Concerns
Requests for unsafe, harmful, or illegal content.
Example: "Explain how to make explosives."

4) Gibberish / Nonsensical Content
Incoherent or meaningless prompt.
Example: "Blorp the snizzle wharf backwards."

5) Contains PII (Personal Identifiable Information)
Includes real names, addresses, phone numbers, or sensitive personal data.
Example: "Find info on John Smith at 123 Main Street, phone 555-1234."

6) Requires Localized or Real-Time Information
Needs current, live, or location-specific data.
Example: "Best restaurants near me right now."

7) Identity Requests
Asks about the model's identity or role.
Example: "Who are you?" "Are you ChatGPT?"

8) Requires Real-Time Financial or Market Data
Example: "What is Apple's closing stock price today?"

9) Non-English Prompt
Prompt written in a foreign (non-English) language.
Example: "¿Cuáles son las tendencias de la física nuclear?"

-------------------------
DECISION RULES

• If ANY rejection reason applies → REJECT
• If NONE apply → ACCEPT

-------------------------
OUTPUT FORMAT (STRICT JSON ONLY)

{
  "status": "ACCEPT" or "REJECT",
  "rejection_reason": "<one of the rejection categories or null>",
  "justification": "<brief explanation>"
}

If status is ACCEPT, set rejection_reason to null.

Return ONLY valid JSON.
"""

# Evaluation dimension rubrics: divided per dimension with strict checking in each.
# Field names and weights must stay aligned with EVALUATE_TWO / FULL_EVALUATION output.
# Used combined as EVALUATION_RUBRICS; per-dimension prompts use only the relevant RUBRIC_*.

_RUBRICS_INTRO = """
## Evaluation Rubrics (Score 1-6)

**CRITICAL: EVIDENCE-BASED SCORING**
- Every score must be justified by specific, cited evidence from the response.
- Do not assign any score without first identifying concrete strengths and weaknesses.
- High scores (5-6) require explicit justification: state what faults you looked for and why they do not apply.
- Low scores (1-2) require explicit justification: state what specific failures or absences warrant the low mark.
- When uncertain between two scores, choose the score best supported by the evidence you have cited.
- Generic praise ("well-written", "comprehensive") or generic criticism ("poor", "weak") without specific evidence is insufficient to justify any score.
- You MUST cite specific evidence for every score — both strengths that raise it and flaws that lower it.
- For ANY score of 5 or 6, you MUST provide explicit justification for why potential faults do not apply.
- For ANY score of 1 or 2, you MUST cite the specific failures that warrant the low mark.

Apply these rubrics ONLY to the pre-loaded Response A and Response B.
"""

# Instruction Following (Weight: 0.25)
RUBRIC_INSTRUCTION_FOLLOWING = """
### 1. Instruction Following (IF) — Weight: 0.25

**SCORING GUIDANCE:** Score based on the checklist results below. The checklist provides objective, measurable criteria.

**MANDATORY CHECKLIST (must complete before scoring):**
1. Extract ALL instructions from the prompt (format, steps, constraints, scope, tone)
2. Create a checklist: [ ] or [X] for each instruction element
3. Count: Total instructions vs. Fully satisfied instructions
4. Any unchecked box = cap at score 4

**DEDUCTION TRIGGERS:**
- Missing ANY explicitly requested element → cap at 4
- Adding unrequested content → -1 point
- Wrong format (e.g., list when paragraph requested) → -1 point
- Ignoring constraints (word count, scope limits) → -1 point
- Partial completion of multi-step instructions → -1 point per missing step

| Score | Criteria | Checklist Requirement |
|-------|----------|----------------------|
| 6 | Perfect adherence to every instruction element | 100% boxes checked, zero additions |
| 5 | Near-perfect; only trivial formatting variance | 95%+ checked, no significant omissions |
| 4 | Good but misses 1 required element OR adds unrequested content | 80-94% checked |
| 3 | Misses central requirement(s) or significant additions | 60-79% checked |
| 2 | Misunderstands core instructions | 40-59% checked |
| 1 | Completely ignores instructions | <40% checked |

**BEFORE SCORING 5 OR 6:** List every instruction and confirm each is satisfied. If you skip this step, max score is 4.
"""

# Truthfulness (Weight: 0.25)
RUBRIC_TRUTHFULNESS = """
### 2. Truthfulness — Weight: 0.25

**EVIDENCE-BASED SCORING:**
- Assess each factual claim independently using the fact-check process below.
- The score must reflect the ratio of verified to questionable/false claims.
- Do not assign a score without completing the mandatory fact-check process.
- Support every score with specific evidence cited in the "reason" field.

**MANDATORY FACT-CHECK PROCESS:**
1. List ALL factual claims made in the response (numbers, dates, names, technical specs, cause-effect relationships)
2. For EACH claim, mark as: [VERIFIED], [UNVERIFIABLE], [QUESTIONABLE], or [FALSE]
3. Any [UNVERIFIABLE] claim = automatic cap at score 4
4. Any [QUESTIONABLE] claim = automatic cap at score 3
5. Any [FALSE] claim = automatic score 2 or below

**DEDUCTION TRIGGERS (each one LOWERS the score):**
- Unsourced statistics or percentages → -1 point
- Definitive statements without hedging (e.g., "always", "never", "will") → -1 point
- Technical claims that cannot be independently verified → -1 point
- Outdated information (>2 years old for fast-moving fields) → -1 point
- Missing important caveats or edge cases → -1 point

## Truthfulness Rubric (1-6 Scale)

| Score | Criteria | When to Use |
|-------|----------|-------------|
| 6 | Every single claim is verifiable and verified. Zero errors. | Only for responses with citations or universally known facts |
| 5 | All major claims verified, only 1 trivial imprecision | For thoroughly accurate technical responses |
| 4 | Generally accurate, but 1-2 unverifiable or weakly supported claims | For responses where most claims check out |
| 3 | Mix of correct and questionable claims; some unsupported statements | For responses with uncited claims |
| 2 | Multiple errors or misleading claims; unreliable overall | For responses with clear factual mistakes |
| 1 | Major fabrications or contradicts established facts | For hallucinated or false content |

**BEFORE SCORING 5 OR 6, YOU MUST:**
- Cite the specific verifiable source for each major claim
- Explain why there are zero/minimal issues
- If you cannot do this, the score MUST be 4 or below
"""

# Prompt Correctness (Weight: 0.20)
RUBRIC_PROMPT_CORRECTNESS = """
### 3. Prompt Correctness (Does it answer the prompt?) — Weight: 0.20

**SCORING GUIDANCE:** Score based on the correctness audit results below. Let the ratio of correct to incorrect/missing elements determine the score.

**MANDATORY CORRECTNESS AUDIT:**
1. Break down the prompt into distinct questions/requirements
2. For each requirement, mark: [CORRECT], [PARTIALLY CORRECT], [INCORRECT], [MISSING]
3. Verify factual claims against known standards
4. Check for outdated information (especially in tech/science domains)

**DEDUCTION TRIGGERS:**
- Any [PARTIALLY CORRECT] element → cap at 4
- Any [INCORRECT] element → cap at 3
- Any [MISSING] element → -1 point
- Unverifiable technical claims stated as fact → -1 point
- Information older than 2 years in fast-moving fields → -1 point
- Wrong conclusion even with correct reasoning → cap at 3
- Correct conclusion with flawed reasoning → cap at 4

| Score | Criteria | Correctness Rate |
|-------|----------|------------------|
| 6 | Every requirement answered correctly and verifiably | 100% [CORRECT] |
| 5 | All major requirements correct; trivial omissions only | 95%+ correct |
| 4 | Mostly correct but misses 1 key element or has minor inaccuracies | 80-94% correct |
| 3 | Relevant attempt but wrong conclusion or significant gaps | 60-79% correct |
| 2 | Fundamentally flawed approach or largely incorrect | 40-59% correct |
| 1 | Does not address the prompt or completely wrong | <40% correct |

**BEFORE SCORING 5 OR 6:** List each prompt requirement and show how it was correctly answered. Any requirement you cannot verify = automatic cap at 4.
"""

# Writing Style (Weight: 0.15)
RUBRIC_WRITING_STYLE = """
### 4. Writing Style (Writing Quality) — Weight: 0.15

**SCORING GUIDANCE:** Complete the quality audit below before assigning a score. Let the number and severity of identified issues determine the score.

**MANDATORY QUALITY AUDIT (must complete before scoring):**
1. Read for: clarity, structure, tone, grammar, audience-appropriateness
2. Identify AT LEAST ONE area for improvement (even minor)
3. If you cannot find any issue, re-read more critically before scoring 5+

**DEDUCTION TRIGGERS (each issue identified = -1 point):**
- Run-on sentences or overly complex sentence structure → -1
- Inconsistent tone (formal/informal mix) → -1
- Missing paragraph breaks or poor organization → -1
- Grammar errors (even minor) → -1
- Jargon without explanation for general audience → -1
- Passive voice overuse → -1
- Redundant phrasing → -1

| Score | Criteria | Issue Count |
|-------|----------|-------------|
| 6 | Publication-ready; would not change a single word | 0 issues found after thorough review |
| 5 | Professional quality; only 1 trivial style preference | 1 minor issue |
| 4 | Good but has identifiable style/structure weaknesses | 2-3 minor issues |
| 3 | Readable but noticeably awkward or unclear sections | 4+ minor or 1 major issue |
| 2 | Difficult to follow; significant clarity problems | Multiple major issues |
| 1 | Unintelligible or unacceptable | Pervasive problems |

**BEFORE SCORING 5 OR 6:** Quote the specific phrases/sections that demonstrate exceptional quality. If you can only say "well-written" without specifics, score is 4.
"""

# Verbosity (Weight: 0.15)
RUBRIC_VERBOSITY = """
### 5. Verbosity (appropriateness of length) — Weight: 0.15

**SCORING GUIDANCE:** Complete the length analysis below before assigning a score. Let the results of the analysis determine the score.

**MANDATORY LENGTH ANALYSIS:**
1. Estimate ideal response length for this prompt (short/medium/long)
2. Compare actual length to ideal
3. Identify specific sections that could be cut OR are missing
4. If you find nothing to cut and nothing missing, verify by re-reading

**DEDUCTION TRIGGERS:**
- Any paragraph that could be removed without losing value → -1
- Repetition of the same point in different words → -1
- Unnecessary examples beyond what's needed → -1
- Missing explanation for a complex point → -1
- Filler phrases ("It's important to note that...") → -1
- Over-detailed background when prompt asks for direct answer → -1

| Score | Criteria | Length Assessment |
|-------|----------|-------------------|
| 6 | Perfectly calibrated; nothing to add or remove | Ideal length ±5% |
| 5 | Near-perfect; only trivial trimming possible | Ideal length ±15% |
| 4 | Good but has identifiable padding OR gaps | Ideal length ±25% |
| 3 | Noticeable excess OR missing important content | Ideal length ±40% |
| 2 | Significantly too long OR too short | Ideal length ±60% |
| 1 | Severely miscalibrated length | Way off target |

**BEFORE SCORING 5 OR 6:** State explicitly: "Nothing can be cut because..." AND "Nothing is missing because..." If you cannot complete both statements, score is 4 max.
"""

# Overall Quality (formula only)
RUBRIC_OVERALL_QUALITY = """
### 6. Overall Quality
Calculate from weighted scores. Scale: 1.0 (very poor) to 6.0 (excellent).
- Instruction Following: 0.25
- Truthfulness: 0.25
- Prompt Correctness: 0.20
- Writing Style: 0.15
- Verbosity: 0.15

weighted_score = IF*0.25 + Truth*0.25 + Correctness*0.20 + Writing*0.15 + Verbosity*0.15
"""

# Combined rubrics for prompts that evaluate all dimensions (EVALUATE_TWO, FULL_EVALUATION).
EVALUATION_RUBRICS = (
    _RUBRICS_INTRO
    + RUBRIC_INSTRUCTION_FOLLOWING
    + RUBRIC_TRUTHFULNESS
    + RUBRIC_PROMPT_CORRECTNESS
    + RUBRIC_WRITING_STYLE
    + RUBRIC_VERBOSITY
    + RUBRIC_OVERALL_QUALITY
)

# Per-dimension rubric text for one-dimension evaluation (instruction_following → only that rubric).
DIMENSION_RUBRICS = {
    "instruction_following": RUBRIC_INSTRUCTION_FOLLOWING,
    "truthfulness": RUBRIC_TRUTHFULNESS,
    "prompt_correctness": RUBRIC_PROMPT_CORRECTNESS,
    "writing_style": RUBRIC_WRITING_STYLE,
    "verbosity": RUBRIC_VERBOSITY,
}

_ONE_DIMENSION_TASK = """
---
TASK: EVALUATE_ONE_DIMENSION
The user message will specify which single dimension to evaluate (instruction_following, truthfulness, prompt_correctness, writing_style, or verbosity).
Evaluate ONLY that dimension for Response A and Response B. Output JSON with response_a and response_b; each must contain exactly ONE key (the dimension key specified in the user message) mapping to { "score": <1-6>, "reason": "<cite specific evidence or fault>" }.
Use the exact dimension key from the user message (e.g. instruction_following, not "Instruction Following").

Output JSON (only the one dimension key in each of response_a and response_b):
{
  "response_a": { "<dimension_key>": { "score": <1-6>, "reason": "..." } },
  "response_b": { "<dimension_key>": { "score": <1-6>, "reason": "..." } }
}
Return ONLY valid JSON.
"""


def get_evaluation_system_prompt_for_dimension(dim_key: str) -> str:
    """Return the system prompt for evaluating a single dimension. Only this dimension's rubric is sent—never the full EVALUATION_RUBRICS (all five dimensions)."""
    rubric = DIMENSION_RUBRICS.get(dim_key)
    if rubric is None:
        rubric = ""
    return (
        _STRICT_HEADER
        + """
*** SCORING RULES (MANDATORY — ENFORCE STRICTLY) ***
- Every score must be determined by cited evidence. Do not assign a score without first identifying concrete strengths and weaknesses for this dimension.
- High scores (5-6) require that you explicitly state what faults you looked for and why they do not apply. Do not default to high scores.
- When uncertain or borderline, choose the score best supported by the evidence you have cited.

*** FAULT-FINDING (MANDATORY) ***
For this dimension, you MUST:
1. First identify what is wrong, missing, or improvable (a flaw, gap, or weakness) in each response.
2. In the "reason" field, cite that fault: state what you looked for and what you found (e.g., "Partially follows but omits X", "Unverified claim about Y").
3. Only if you find nothing meaningful wrong and can cite concrete evidence may you consider 5 or 6. Before giving 5 or 6, you MUST state in the reason what fault you considered and why it does not apply.
If you cannot name a specific fault you considered for this dimension, you have not completed the evaluation — revisit the response before assigning a score above 4.

"""
        + rubric
        + _ONE_DIMENSION_TASK
    )


# -----------------------------------------------------------------------------
# Bifurcated system prompts: one per task (for multiple API calls per evaluation).
# Each includes strict/critical evaluation rules relevant to that task.
# Output schema of each must not change; parsers depend on it.
# -----------------------------------------------------------------------------

_STRICT_HEADER = """
You are an expert LLM response evaluator performing side-by-side (SxS) ratings on STEM & Code prompts/responses.
Treat this entire system message as instructions. Every line is part of the evaluation rules.
Evaluate strictly and critically. Do not give the benefit of the doubt. When uncertain between two scores, choose the score best supported by the evidence you have cited.
"""

EVALUATION_SYSTEM_PROMPT_EVALUATE_TWO = (
    _STRICT_HEADER
    + """
*** SCORING RULES (MANDATORY) ***
Every score must be determined by cited evidence. Do not assign a score without first identifying concrete strengths and weaknesses for each dimension. High scores (5-6) require explicit justification of why potential faults do not apply. Low scores (1-2) require citation of specific failures.

*** FAULT-FINDING (MANDATORY) ***
For every dimension, on every response, you MUST:
1. First identify what is wrong, missing, or improvable (a flaw, gap, or weakness).
2. In the "reason" field, cite that fault: state what you looked for and what you found (e.g., "Missing edge-case handling; no validation of input X" or "Partially follows but omits step Y").
3. Only if you find nothing meaningful wrong and can cite concrete evidence may you consider 5 or 6. Before giving 5 or 6, you must state in the reason what fault you considered and why it does not apply.
If you cannot name a specific fault you considered for that dimension, you have not completed the evaluation — revisit the response before assigning a score above 4.

"""
    + EVALUATION_RUBRICS
    + """
## Strict and Critical Evaluation
Apply to every dimension: require fault-first reasoning (cite the flaw or gap before assigning a score). For scores of 5 or 6, you must have found no meaningful fault and must cite evidence. For Truthfulness and Prompt Correctness, treat factual claims with skepticism; unverified claims cap the score at 4 or below.

---
TASK: EVALUATE_TWO
Input: USER PROMPT, RESPONSE A, RESPONSE B
Apply the 5 dimensions and 1-6 scores only to Response A and Response B. For each dimension, find and cite a fault or gap; only then assign score. Compute weighted_score. Every "reason" must cite what is wrong or missing, or what fault you considered and rejected before 5/6.

Output JSON (this schema must not change):
{
  "response_a": {
    "instruction_following": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "truthfulness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "prompt_correctness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "writing_style": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "verbosity": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "overall_quality": {"weighted_score": <float>, "reason": "<summary of strengths/weaknesses>"}
  },
  "response_b": {
    "instruction_following": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "truthfulness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "prompt_correctness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "writing_style": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "verbosity": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "overall_quality": {"weighted_score": <float>, "reason": "<summary of strengths/weaknesses>"}
  }
}
Return ONLY valid JSON.
"""
)

# Default one-dimension prompt (instruction_following). Use get_evaluation_system_prompt_for_dimension(dim_key) in sync/batch for dimension-specific rubric.
EVALUATION_SYSTEM_PROMPT_ONE_DIMENSION = get_evaluation_system_prompt_for_dimension(
    "instruction_following"
)

# Per-dimension evaluation: one call per dimension, then merge. Keys and weights for aggregation.
DIMENSION_KEYS = [
    "instruction_following",
    "truthfulness",
    "prompt_correctness",
    "writing_style",
    "verbosity",
]
DIMENSION_WEIGHTS = {
    "instruction_following": 0.25,
    "truthfulness": 0.25,
    "prompt_correctness": 0.20,
    "writing_style": 0.15,
    "verbosity": 0.15,
}

EVALUATION_SYSTEM_PROMPT_COMPARE_TWO = (
    _STRICT_HEADER
    + """
Comparison scale (-3 to +3): -3 A much better, -2 A moderately better, -1 A slightly better, 0 same, +1 B slightly better, +2 B moderately better, +3 B much better.
Comment must: (1) Start with clear verdict (2) Name differentiating dimensions (3) Cite concrete evidence. Be concise (1-3 sentences).

---
TASK: COMPARE_TWO
Input: USER PROMPT, RESPONSE A, RESPONSE B
Compare and determine winner using the -3 to +3 scale.

Output JSON (this schema must not change):
{
  "comparison_score": <-3 to +3>,
  "overall_comment": "<verdict + differentiating dimensions + concrete evidence, 1-3 sentences>"
}
Return ONLY valid JSON.
"""
)

EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL = (
    _STRICT_HEADER
    + """
Compare SxS winner (internal) vs external model. Scale: -3 to -1 = SxS winner better, 0 = same, +1 to +3 = external model better.
In comparison_comment ALWAYS use the model name: "Gemini 3 Pro" or "GPT 5.2". Name metrics used; one sentence on SxS winner, one on external model; cite concrete evidence (2-4 sentences).

---
TASK: COMPARE_EXTERNAL
Input: USER PROMPT, SXS WINNER RESPONSE, EXTERNAL MODEL RESPONSE, EXTERNAL MODEL NAME
Compare using Instruction Following, Truthfulness, Prompt Correctness, Writing Style, Verbosity.

Output JSON (this schema must not change):
{
  "comparison_score": <-3 to +3>,
  "comparison_comment": "<verdict + metrics + concrete evidence for each model>"
}
Return ONLY valid JSON.
"""
)

EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL = (
    _STRICT_HEADER
    + """
You are creating rubrics to capture qualities where the external model (GPT 5.2 or Gemini 3 Pro) is CLEARLY BETTER than the SxS winner.

================================================================================
RUBRIC CREATION GUIDELINES
================================================================================

1. WHEN TO CREATE A RUBRIC
   Create a rubric ONLY when you observe a quality in the external model's response
   that is clearly better than the SxS winner AND can be graded by a repeatable rubric.

2. SPECIFICITY IS MANDATORY
   Focus on concrete, critical steps or features — NOT vague qualities.
   GOOD examples:
     - "Code includes clear, helpful documentation"
     - "Did the response correctly factor the quadratic equation?"
     - "Step-by-step derivation includes intermediate algebraic simplifications"
     - "Error handling covers edge cases (null input, empty array, overflow)"
   BAD examples (too vague — DO NOT USE):
     - "Was the solution clear?"
     - "Better explanation"
     - "More comprehensive"
     - "Higher quality response"

3. PRESENTATION AND FORMATTING RUBRICS
   Include rubrics for formatting, presentation, and LaTeX rendering quality when
   the external model demonstrates superior:
     - LaTeX equation rendering (correct delimiters, aligned environments, proper symbols)
     - Code formatting (syntax highlighting conventions, indentation, block structure)
     - Structural organization (headings, numbered steps, logical section breaks)
     - Visual clarity (tables, lists, or diagrams used effectively)

4. RUBRIC REQUIREMENTS
   Each rubric must be:
     - Repeatable: another rater could apply the same rubric and reach the same conclusion
     - Clearly gradeable: the criteria are specific enough to distinguish 1-6 levels
     - Not redundant with existing dimensions (instruction_following, truthfulness,
       prompt_correctness, writing_style, verbosity)
     - Grounded in observable differences between the external model response and the
       SxS winner response

5. RUBRIC WRITING RULES
   Each rubric must:
     - Be one sentence long.
     - Check one measurable behavior.
     - Be binary (Yes/No) unless an ordinal scale is clearly necessary.
     - Avoid evaluative adjectives.
     - Avoid template repetition (do not start every rubric with "Does the response...").
   GOOD example:
     - "Includes a section titled exactly '## Verification' at the end of the response."
     - "Lists between 3 and 5 bullet points in the '## Key Takeaways' section."
   BAD example:
     - "The response properly and clearly includes verification."
     - "Provides helpful key takeaways."

6. RATING SCALE
   Rate 1-6 how well the external model performed on the rubric relative to the
   SxS winner:
     - 1-2: External model marginally better on this aspect
     - 3-4: External model noticeably better with clear evidence
     - 5-6: External model substantially better; SxS winner clearly lacks this quality

---
TASK: CREATE_RUBRICS_EXTERNAL
Input: USER PROMPT, SXS WINNER RESPONSE, EXTERNAL MODEL RESPONSE, EXTERNAL MODEL NAME
Identify specific, concrete, gradeable qualities where the external model demonstrates
superior capabilities. Always refer to the external model by its name (Gemini 3 Pro or GPT 5.2).

Output JSON (this schema must not change):
{
  "rubrics": [
    {
      "name": "<short, specific title>",
      "description": "<what to look for — concrete criteria, mention the external model name>",      "scale": "1-6 scale: how well this aspect was performed in the comparison",
      "rating": <1-6>
    }
  ]
}
Return ONLY valid JSON.
"""
)


# =============================================================================
# Enhanced-prompt rubric creation + per-response rubric rating
# =============================================================================

EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS = """
You are an expert rubric designer for LLM response evaluation.

You will receive an ORIGINAL PROMPT (the user's raw input) and an ENHANCED PROMPT (the same
prompt enriched with additional constraints, structural requirements, and evaluation criteria).

Your task is to identify every meaningful enhancement that was added to the original prompt and
create a specific, gradeable rubric for each one. Rubrics MUST directly score adherence to the
concrete, checkable constraints introduced by the enhancement — NOT abstract or high-level
qualities.

================================================================================
RUBRIC CREATION GUIDELINES
================================================================================

1. IDENTIFY CONCRETE, CHECKABLE ENHANCEMENTS
   Compare the ENHANCED PROMPT to the ORIGINAL PROMPT word by word. Every constraint,
   structural requirement, section request, formatting rule, or quality criterion that
   appears ONLY in the enhanced version is an enhancement. Create one rubric per distinct
   enhancement.

   CRITICAL: Each rubric must map to a specific, verifiable requirement from the enhanced
   prompt. A rater should be able to check the response and determine YES or NO (or a
   clear degree) for each rubric without subjective judgment.

   Examples of enhancements that become rubrics:
   - "Present the argument step-by-step, explicitly labeling each stage as Step 1, Step 2, etc."
     → Rubric name: "Step-by-Step Structure Compliance"
     → Description: "Stages explicitly labeled Step 1, Step 2, etc., each clearly
       advancing the argument."
   - "Conclude your output with a section titled exactly ## Summary"
     → Rubric name: "Summary Section Present"
     → Description: "Final section titled exactly '## Summary' concisely recaps the main
       conclusion and key argument steps."
   - "Provide clear inline justification for every nontrivial algebraic manipulation"
     → Rubric name: "Inline Justification and Assumptions Handling"
     → Description: "Nontrivial transformations include clear inline justification;
       assumptions are explicitly stated and justified; no intermediate reasoning steps
       skipped."
   - "Use clear Markdown formatting throughout"
     → Rubric name: "Markdown Formatting Compliance"
     → Description: "Markdown formatting applied throughout — headers, emphasis, code blocks,
       LaTeX delimiters as appropriate."

2. CONCRETE OVER ABSTRACT (MANDATORY)
   Rubrics MUST score adherence to specific enhanced-prompt constraints, NOT abstract qualities.
   If you find yourself writing a rubric about a general quality (e.g., "rigor", "coherence",
   "structure") that is not directly tied to a specific quoted constraint from the enhanced
   prompt, STOP and rewrite it to target the actual constraint.

   *** ANTI-PATTERN EXAMPLES (DO NOT PRODUCE RUBRICS LIKE THESE) ***
   These are real examples of BAD rubrics that are too high-level and abstract:
   - name: "Logical Derivation Structure"
     description: "Response presents a logically coherent derivation where each major result
       follows clearly from prior steps without unnecessary assumptions or structural detours."
     WHY BAD: This is a vague restatement of general quality. It does not score any specific
       constraint from the enhanced prompt. It overlaps with existing evaluation dimensions
       (instruction_following, prompt_correctness).
   - name: "Mathematical Rigor and Justification"
     description: "Response justifies each major step with explicit reasoning, avoids
       unsupported assumptions, and maintains mathematical rigor throughout."
     WHY BAD: "Mathematical rigor" is an abstract quality, not a checkable enhanced-prompt
       constraint. The enhanced prompt may have said "provide clear inline justification for
       every nontrivial algebraic manipulation" — THAT is the concrete constraint to rubric.

   *** CORRECT RUBRICS FOR THE SAME ENHANCED PROMPT ***
   If the enhanced prompt added: step-by-step labeling, inline justification, explicit
   assumptions, Markdown formatting, and a ## Summary section, the rubrics should be:
   - "Step-by-Step Structure Compliance" — stages labeled Step 1, Step 2, etc.
   - "Inline Justification and Assumptions Handling" — nontrivial steps justified inline;
     assumptions explicitly stated
   - "Markdown and Summary-Section Compliance" — Markdown formatting used; ends with ## Summary
   - "Mathematical Correctness" — target statement proved correctly (if applicable to the
     domain; captures correctness of the core task beyond formatting)

3. SPECIFICITY IS MANDATORY
   Each rubric must describe exactly what to look for in the response — not vague qualities.
   The description must be concrete enough that a rater can verify it by reading the response.
   GOOD: "Stages explicitly labeled Step 1, Step 2, etc., each clearly advancing the
     argument toward the final result."
   BAD: "Good structure" or "Well-organized proof"
   GOOD: "Nontrivial transformations include clear inline justification; assumptions
     explicitly stated and justified; no intermediate reasoning steps skipped."
   BAD: "Mathematical rigor and justification" or "Rigorous derivation"

4. PRESENTATION AND FORMATTING RUBRICS
   If the enhanced prompt adds formatting constraints (markdown headers, numbered steps,
   bullet points, LaTeX rendering, code blocks, specific section titles), create rubrics
   for each. These are among the most concrete and checkable requirements.

5. RUBRIC REQUIREMENTS
   Each rubric must be:
   - Repeatable: another rater could apply it and reach the same conclusion
   - Clearly gradeable on a 1-6 scale
   - Not redundant with existing evaluation dimensions (instruction_following, truthfulness,
     prompt_correctness, writing_style, verbosity)
   - Directly traceable to a specific enhancement in the enhanced prompt
   - Verifiable: a rater can check the response and determine the score without subjective
     interpretation

6. RUBRIC WRITING RULES
   Each rubric must:
   - Be one concise sentence.
   - Check one measurable behavior or a small cluster of closely related behaviors.
   - Be gradeable on a 1-6 scale (where 1 = completely fails, 6 = fully meets).
   - Avoid evaluative adjectives ("good", "proper", "clear", "rigorous").
   - Use varied, natural phrasing — do NOT start every rubric the same way.
   - Reference the SPECIFIC constraint from the enhanced prompt, not a paraphrase of general quality.
   GOOD example:
   - "Stages explicitly labeled Step 1, Step 2, etc."
   - "Final section titled exactly '## Summary' present."
   - "Nontrivial algebraic manipulations include inline justification, no intermediate steps skipped."
   BAD example:
   - "The response is well-structured and logically coherent."
   - "Mathematical rigor is maintained throughout."
   - "The response properly and clearly includes verification."

7. INCLUDE ALL ENHANCEMENTS
   Do NOT skip any enhancement. Every added constraint, section request, formatting rule,
   or quality requirement must have a corresponding rubric. If the enhanced prompt added
   4 constraints, output at least 4 rubrics. Related constraints may be grouped into one
   rubric only if they are tightly coupled (e.g., "Markdown formatting" and "## Summary
   section" can be one rubric since both are presentation requirements).

8. DOMAIN-SPECIFIC CORRECTNESS RUBRIC
   When the original prompt asks for a proof, derivation, code solution, or other domain-
   specific task, include one rubric for the correctness of the core task (e.g.,
   "Mathematical Correctness", "Code Correctness") that checks whether the response
   actually achieves the goal of the original prompt. This rubric captures task success
   beyond just formatting and structure compliance.

---
TASK: CREATE_RUBRICS

Input: ORIGINAL PROMPT, ENHANCED PROMPT
Identify all enhancements added to the original prompt and create a rubric for each.
Rubrics MUST be aligned with the concrete constraints in the enhanced prompt — not high-level
abstractions.

Output JSON:
{
  "rubrics": [
    {
      "name": "<short, specific title tied to the constraint>",
      "description": "<concrete, verifiable criteria — what to check in the response>",
      "enhancement_source": "<direct quote or close paraphrase of the specific enhancement from the enhanced prompt>"
    }
  ]
}

Return {"rubrics": []} only if the enhanced prompt is identical to the original (no enhancements added).
Return ONLY valid JSON.
"""


ENHANCEMENT_CRITERIA_CATEGORIZATION_SYSTEM_PROMPT = """
You are a specialist in taxonomic classification of evaluation rubrics for large language model response assessment.

You will receive a list of rubrics that were generated by analyzing the differences between an original user prompt and an enhanced version of that same prompt. Each rubric captures a specific, checkable constraint or requirement that the enhancement introduced. Your task is to analyze each rubric and determine the broad evaluation criteria category it belongs to, then return a deduplicated list of those categories.

================================================================================
WHAT IS A CRITERIA CATEGORY
================================================================================

A criteria category is a concise, descriptive label that identifies the overarching quality dimension or domain that a rubric targets. It abstracts away from the specific details of individual rubrics to capture the fundamental nature of what is being evaluated. Multiple rubrics that assess different specific behaviors within the same general quality dimension should map to the same criteria category.

The criteria category should immediately communicate to a reader what general aspect of response quality the associated rubric or rubrics are concerned with, without requiring any additional context or explanation.

================================================================================
CATEGORIZATION PRINCIPLES
================================================================================

1. APPROPRIATE LEVEL OF ABSTRACTION
   Each criteria category must occupy an intermediate position in the abstraction hierarchy. It must be broad enough that multiple distinct rubrics could plausibly fall under it, yet specific enough to convey substantive information about which dimension of quality is being measured. A criteria category that could apply to virtually any rubric regardless of its content is too broad and must be narrowed. A criteria category that merely restates or minimally rephrases a single rubric's name is too narrow and must be generalized.

2. SEMANTIC COHERENCE
   All rubrics assigned to the same criteria category must share a fundamental evaluative purpose. They must all be assessing the same general aspect of a response, even if they target different specific manifestations of that aspect. Two rubrics should receive the same category only when they evaluate variations of the same underlying quality dimension. Rubrics that evaluate fundamentally different aspects of a response must always receive different categories, regardless of any superficial similarities in phrasing.

3. COMPREHENSIVE DOMAIN COVERAGE
   When analyzing rubrics, consider the full landscape of qualities relevant to evaluating language model outputs. This landscape spans multiple domains including but not limited to: the visual and typographic presentation of information, the logical structure and progression of reasoning, the organizational architecture of the response, the factual and technical correctness of content, the degree of adherence to explicit instructions, the methods used to verify or validate claims, the appropriateness of tone and register for the intended audience, and the inclusion or management of quantitative constraints and parameters. Your categorization must reflect which of these domains each rubric belongs to.

4. MUTUAL EXCLUSIVITY
   Each rubric must be assigned to exactly one criteria category. When a rubric appears to span multiple quality dimensions, assign it to the single category that most centrally captures its primary evaluative intent. The categorization must be decisive, not hedged.

5. NAMING CONVENTIONS
   Every criteria category name must be:
   - A concise noun phrase consisting of two to four words
   - Written entirely in lowercase
   - Descriptive of the general quality dimension rather than any specific rubric behavior
   - Unique within the output set — no two categories should overlap in scope to the degree that a reader would have difficulty distinguishing between them
   - Phrased using established terminology from the domains of writing assessment, instructional design, or evaluation methodology where applicable

6. COMPLETENESS OF COVERAGE
   Every rubric provided in the input must be accounted for by at least one criteria category in the output. There must be no rubric left without a corresponding category. If the input contains rubrics that span a wide range of quality dimensions, the output must contain enough categories to cover all of them.

7. OUTPUT DEDUPLICATION
   The final output must contain only unique criteria categories. When multiple rubrics from the input belong to the same broad quality dimension, that dimension appears exactly once in the output list. The result is a compact, non-redundant set of category labels that collectively account for all rubrics provided.

================================================================================
EXAMPLES
================================================================================

Input Rubric: {"name": "bulletpoints", "description": "Does the response use bullet points for key findings?"}
Criteria Category: formatting specifications

Input Rubric: {"name": "tone_check", "description": "Is the response professional and suitable for an executive audience?"}
Criteria Category: stylistic audience alignment

Input Rubric: {"name": "step_by_step", "description": "Does the model explain the logical reasoning behind each step?"}
Criteria Category: logical progression structure

Input Rubric: {"name": "source_verification", "description": "Are all claims supported by specific citations from the text?"}
Criteria Category: evidence and attribution

Input Rubric: {"name": "word_count_limit", "description": "Ensure the final output does not exceed 250 words."}
Criteria Category: quantitative length constraints

Input Rubric: {"name": "code_comments", "description": "Check if every function includes a descriptive docstring."}
Criteria Category: technical documentation standards

================================================================================
TASK: CATEGORIZE_RUBRICS
================================================================================

Input: A JSON array of rubrics, each containing "name" and "description" fields that together specify a concrete, checkable evaluation criterion derived from prompt enhancement constraints.

Analyze every rubric to determine which broad quality dimension it targets. Produce a deduplicated list of criteria category names that collectively cover all input rubrics.

Output JSON:
{
  "criteria": [
    "<criteria category name>"
  ]
}

If the input rubrics list is empty, return {"criteria": []}.
Return ONLY valid JSON.
"""


EVALUATION_SYSTEM_PROMPT_RATE_RUBRICS = """
You are an expert LLM response evaluator. You will receive a set of RUBRICS and FOUR model
responses to the same prompt. Your task is to rate each response independently on each rubric
using a 1-6 scale.

================================================================================
RATING SCALE
================================================================================
| Score | Meaning |
|-------|---------|
| 1     | The response completely fails to address this rubric; no evidence of the required quality |
| 2     | Minimal, token effort; the response barely touches on the rubric's criteria |
| 3     | Partial fulfillment; some aspects of the rubric are present but incomplete or shallow |
| 4     | Adequate fulfillment; the rubric's criteria are mostly met with minor gaps |
| 5     | Strong fulfillment; the rubric's criteria are clearly and thoroughly met |
| 6     | Exceptional fulfillment; the response exceeds expectations on this rubric |

================================================================================
RATING RULES
================================================================================

1. RATE EACH RESPONSE INDEPENDENTLY
   Do not compare responses to each other. Rate each response solely on how well it satisfies
   the rubric's criteria. Two responses can receive the same score.

2. EVIDENCE-BASED SCORING
   Every rating must be supported by cited evidence from the response. High scores (5-6) require
   clear, strong evidence. Low scores (1-2) require citation of specific failures or absences.

3. PROVIDE REASONS
   For each rating, cite specific evidence from the response. Quote or reference concrete
   content that supports the score. If giving 5+, state what fault you considered and
   rejected. If giving 1-2, state specifically what is missing.

4. RATE ALL RUBRICS FOR ALL RESPONSES
   Every rubric must have a rating for every response. Do not skip any combination.

---
TASK: RATE_RUBRICS

Input: ORIGINAL PROMPT, RUBRICS (list from CREATE_RUBRICS), OPALITE RESPONSE, OPHELIA RESPONSE,
       GEMINI 3 PRO RESPONSE, GPT 5.2 RESPONSE

Rate each rubric for each of the four responses independently.

Output JSON:
{
  "rubric_ratings": [
    {
      "rubric_name": "<must match a rubric name from the input>",
      "opalite": {"score": <1-6>, "reason": "<specific evidence from opalite response>"},
      "ophelia": {"score": <1-6>, "reason": "<specific evidence from ophelia response>"},
      "gemini": {"score": <1-6>, "reason": "<specific evidence from gemini response>"},
      "gpt": {"score": <1-6>, "reason": "<specific evidence from gpt response>"}
    }
  ]
}

Return ONLY valid JSON.
"""


EVALUATION_SYSTEM_PROMPT = (
    """
You are an expert LLM  evaluator performing side-by-side (SxS) ratings on STEM & Code prompts.
Treat this entire system message as instructions. Every line is part of the evaluation rules.

*** SCORING RULES (MANDATORY) ***
Every score must be determined by cited evidence. Do not assign a score without first identifying concrete strengths and weaknesses for each dimension. High scores (5-6) require explicit justification of why potential faults do not apply. Low scores (1-2) require citation of specific failures.

*** FAULT-FINDING (MANDATORY) ***
For every dimension, on every response, you MUST:
1. First identify what is wrong, missing, or improvable (a flaw, gap, or weakness).
2. In the "reason" field, cite that fault: state what you looked for and what you found (e.g., "Missing edge-case handling; no validation of input X" or "Partially follows but omits step Y").
3. Only if you find nothing meaningful wrong and can cite concrete evidence may you consider 5 or 6. Before giving 5 or 6, you must state in the reason what fault you considered and why it does not apply.
If you cannot name a specific fault you considered for that dimension, you have not completed the evaluation — revisit the response before assigning a score above 4.

"""
    + EVALUATION_RUBRICS
    + """
## Scope of Metrics — Response A and Response B Only
The five evaluation dimensions and their 1-6 scores apply ONLY to the pre-loaded Response A and Response B. Do NOT score the later-generated Gemini or GPT responses on these dimensions. Gemini and GPT are used only for: (1) COMPARE_EXTERNAL — a single relative comparison score (-3 to +3) vs the SxS winner, and (2) CREATE_RUBRICS_EXTERNAL — identifying new rubric dimensions and rating the external model on those new rubrics only.

## Strict and Critical Evaluation (Apply to Every Metric)
Evaluate each metric strictly and critically. Do not give the benefit of the doubt.
- Require fault-first reasoning: for every dimension, first identify what is wrong, missing, or improvable; cite that fault in the reason. Only if you find nothing meaningful wrong may you consider 5 or 6; if you give 5 or 6, you must state in the reason what fault you considered and rejected.
- Every "reason" must cite a specific flaw, gap, or weakness (what was missing or wrong), or state what weakness you considered and why it does not apply before 5/6. If you cannot name a fault you considered, you have not completed the evaluation — revisit the response.
- When uncertain between two scores, choose the score best supported by the evidence you have cited.
- FACT-CHECKING: For Truthfulness and Prompt Correctness, treat factual claims with skepticism. Unverified, unsourced, or unverifiable claims should be marked down unless obviously correct or common knowledge.

## Evidence-Based Scoring (Apply to Every Dimension)
- Every score must be backed by specific observations cited in the "reason" field. The score must follow from the evidence, not from a default.
- When assigning any score, cite the specific evidence (e.g., "partially follows but misses X", "generally accurate but includes unverifiable claim Y", "perfectly addresses all requirements with verifiable claims").

--------------------------------------------------------------------------------
SECTION 2: COMPARISON SCALE (Likert -3 to +3)
--------------------------------------------------------------------------------
Use for A vs B comparisons and external model comparisons:

| Score | Meaning |
|-------|---------|
| -3 | Response A is much better than Response B |
| -2 | Response A is moderately better |
| -1 | Response A is slightly better |
| 0 | About the same / equivalent |
| +1 | Response B is slightly better |
| +2 | Response B is moderately better |
| +3 | Response B is much better than Response A |

--------------------------------------------------------------------------------
SECTION 3: OVERALL COMMENT GUIDELINES
--------------------------------------------------------------------------------
The overall comment must:
1. Start with a clear verdict: "Response A is better than Response B because..." OR "Response B is better..." OR "Responses are approximately equivalent because..."
2. Name the differentiating dimension(s) explicitly (IF, Truthfulness, Correctness, Writing Style, Verbosity)
3. Include concrete evidence: cite specific behaviors (e.g., "includes correct algebraic simplification," "misses edge-case handling")
4. Be concise: 1-3 sentences. Do not restate the prompt or summarize both responses fully.

--------------------------------------------------------------------------------
SECTION 4: DECISION THRESHOLDS
--------------------------------------------------------------------------------
| Weighted Score | Interpretation |
|----------------|----------------|
| ≥ 5.2 | High quality - acceptable |
| 4.0 - 5.19 | Moderate - acceptable with notes |
| < 4.0 | Low quality - needs review |

Winner Selection:
- If weighted_score difference ≥ 0.10: declare higher-scoring response as winner
- If difference < 0.05: treat as tie; include rationale in overall_comment

--------------------------------------------------------------------------------
SECTION 5: CODE & LATEX EVALUATION
--------------------------------------------------------------------------------
Code Evaluation Criteria:
- Is it runnable (compilable/interpretable)?
- Does it include imports, functions/entrypoints, and example invocation?
- Are edge cases and input validation considered?
- Are docstrings or inline comments present?
- Do unit tests cover typical and edge cases?

LaTeX/Math Rendering:
- Check for correct use of math notation and clarity
- If rendering or math is ambiguous, deduct in Truthfulness/Correctness

--------------------------------------------------------------------------------
SECTION 6: RUBRIC CREATION GUIDANCE
--------------------------------------------------------------------------------
For CREATE_RUBRICS_EXTERNAL: Rubrics are created only for aspects where the external model (Gemini 3 Pro or GPT 5.2) is better than the SxS winner (the better of Response A and Response B)—i.e., for points that are better in the external model as compared to Response A and Response B. Each rubric must be grounded in the comparison_comment for that external model (the rubric aspect must correspond to observations already stated in that comment).

Create a new rubric when:
- A quality appears that is gradeable (yes/no or scale)
- The quality is NOT covered by existing dimensions (IF, Truthfulness, Correctness, Writing Style, Verbosity)
- Examples: "Includes Unit Tests", "Edge-case Handling", "Time Complexity Analysis", "Code Documentation", "LaTeX Rendering Quality"

Rubric format:
- name: Short title (e.g., "Includes Unit Tests")
- description: Specific definition and what to look for
- scale: Scale description (e.g., "1 (none) to 6 (comprehensive tests covering edge cases)")

Rubric writing rules:
- Each rubric must be one sentence long.
- Each rubric must check one measurable behavior.
- Be binary (Yes/No) unless an ordinal scale is clearly necessary.
- Avoid evaluative adjectives.
- Avoid template repetition (do not start every rubric with "Does the response...").
- GOOD: "Includes a section titled exactly '## Verification' at the end of the response."
- GOOD: "Lists between 3 and 5 bullet points in the '## Key Takeaways' section."
- BAD: "The response properly and clearly includes verification."
- BAD: "Provides helpful key takeaways."

--------------------------------------------------------------------------------
SECTION 7: EXTERNAL MODEL COMPARISON
--------------------------------------------------------------------------------
After determining the A/B winner (SxS winner), compare against external model responses.

External Model Comparison Scale:
| Score | Meaning |
|-------|---------|
| -3 | The SxS winner (internal model) is much better than the external model |
| -2 | The SxS winner is moderately better |
| -1 | The SxS winner is slightly better |
| 0 | About the same quality |
| +1 | The external model is slightly better |
| +2 | The external model is moderately better |
| +3 | The external model is much better than the SxS winner |

Comparison Comment Requirements (same structure as COMPARE_TWO overall_comment):
1. Start with a clear verdict: "The SxS winner is better than [external model] because..." OR "[External model] is better because..." OR "They are approximately equivalent because..."
2. Name the metrics used for comparison explicitly: Instruction Following, Truthfulness, Prompt Correctness, Writing Style, Verbosity (and Overall Quality). State which dimensions differed and how.
3. Mention each response separately: one sentence on the SxS winner's strengths/weaknesses on those metrics, then one on the external model's (e.g., "SxS winner: [concrete evidence]. External model: [concrete evidence].")
4. Include concrete evidence: cite specific behaviors, not generic praise. Be concise: 2-4 sentences total.

================================================================================
TASK INSTRUCTIONS
================================================================================

CANONICAL OUTPUT SCHEMA (do not change; parsers in this file depend on these exact keys and structure):
- EVALUATE_TWO: response_a, response_b; each with instruction_following, truthfulness, prompt_correctness, writing_style, verbosity (each { score, reason }), overall_quality ( { weighted_score, reason } ).
- COMPARE_TWO: comparison_score, overall_comment.
- COMPARE_EXTERNAL: comparison_score, comparison_comment.
- CREATE_RUBRICS_EXTERNAL: rubrics (array of { name, description, scale, rating }).
- FULL_EVALUATION: evaluation_result, comparison_ab, comparison_vs_gemini, comparison_vs_gpt, rubrics_vs_gemini, rubrics_vs_gpt (each with structure above).

You will receive a TASK type and input data. Reference the appropriate sections above and output ONLY valid JSON.

---
TASK: EVALUATE_TWO
Reference: Evaluation Rubrics (above), Section 4 (decision thresholds)

Input: USER PROMPT, RESPONSE A, RESPONSE B
Apply the 5 dimensions and 1-6 scores only to Response A and Response B (the pre-loaded responses). Do not score Gemini or GPT here. For every dimension, apply fault-first reasoning: first identify what is wrong, missing, or improvable; cite that fault in the "reason" field. Only if you find nothing meaningful wrong may you consider 5 or 6; if you give 5 or 6, state in the reason what fault you considered and rejected. Evaluate each of A and B independently using all 5 dimensions. Compute weighted_score.

Every "reason" must cite a specific flaw, gap, or weakness, or state what weakness you considered and rejected; if you cannot name a fault you considered, you have not completed the evaluation — revisit the response. For Truthfulness and Prompt Correctness, apply fact-checking skepticism: unverified or unverifiable factual claims cap the score at 4 or below.

Output JSON:
{
  "response_a": {
    "instruction_following": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "truthfulness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "prompt_correctness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "writing_style": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "verbosity": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "overall_quality": {"weighted_score": <float>, "reason": "<summary of strengths/weaknesses>"}
  },
  "response_b": {
    "instruction_following": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "truthfulness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "prompt_correctness": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "writing_style": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "verbosity": {"score": <1-6>, "reason": "<cite specific evidence>"},
    "overall_quality": {"weighted_score": <float>, "reason": "<summary of strengths/weaknesses>"}
  }
}

---
TASK: COMPARE_TWO
Reference: Section 2 (Likert scale), Section 3 (comment guidelines), Section 4 (decision thresholds)

Input: USER PROMPT, RESPONSE A, RESPONSE B
Compare responses and determine winner using the -3 to +3 scale.

Output JSON:
{
  "comparison_score": <-3 to +3>,
  "overall_comment": "<verdict + differentiating dimensions + concrete evidence, 1-3 sentences>"
}

---
TASK: COMPARE_EXTERNAL
Reference: Section 2 (Likert scale), Section 7 (external model comparison)

Input: USER PROMPT, SXS WINNER RESPONSE, EXTERNAL MODEL RESPONSE, EXTERNAL MODEL NAME
Compare the SxS winner (internal model) against an external model response using the evaluation dimensions (Instruction Following, Truthfulness, Prompt Correctness, Writing Style, Verbosity).

Scale interpretation for this task:
- Negative scores (-3 to -1): The SxS winner (internal) is better than the external model
- Zero (0): About the same quality
- Positive scores (+1 to +3): The external model is better than the SxS winner

IMPORTANT: In the comparison_comment, ALWAYS refer to the external model by its specific name:
- Use "Gemini 3 Pro" (not "external model" or "Gemini")
- Use "GPT 5.2" (not "external model" or "GPT")

Output JSON:
{
  "comparison_score": <-3 to +3>,
  "comparison_comment": "<(1) Clear verdict first. (2) Name the metrics on which you compared (e.g., instruction following, truthfulness, verbosity). (3) One sentence on SxS winner's performance on those metrics with concrete evidence; one sentence on the specific model (Gemini 3 Pro or GPT 5.2). Example: 'The SxS winner is slightly better. On instruction following and verbosity: SxS winner gave a concise, complete answer with correct formatting. Gemini 3 Pro was equally correct but slightly more verbose. Both were equivalent on truthfulness and prompt correctness.'>"
}

---
TASK: CREATE_RUBRICS_EXTERNAL
Reference: Rubric creation guidance below.

Input: USER PROMPT, SXS WINNER RESPONSE, EXTERNAL MODEL RESPONSE, EXTERNAL MODEL NAME
Compare the SxS winner response against the specified external model (Gemini 3 Pro or GPT 5.2) and identify specific, concrete, gradeable qualities where the external model is clearly better.

WHEN TO CREATE A RUBRIC:
Create a rubric ONLY when you observe a quality in the external model's response that is clearly better than the SxS winner AND can be graded by a repeatable rubric. If the external model does not outperform in any concrete, gradeable aspect, return empty {"rubrics": []}.

SPECIFICITY IS MANDATORY:
Focus on concrete, critical steps or features — NOT vague qualities.
GOOD examples:
  - "Code includes clear, helpful documentation"
  - "Did the response correctly factor the quadratic equation?"
  - "Step-by-step derivation includes intermediate algebraic simplifications"
  - "Error handling covers edge cases (null input, empty array, overflow)"
BAD examples (too vague — DO NOT USE):
  - "Was the solution clear?"
  - "Better explanation"
  - "More comprehensive"
  - "Higher quality response"

PRESENTATION AND FORMATTING RUBRICS:
Include rubrics for formatting, presentation, and LaTeX rendering quality when the external model demonstrates superior:
  - LaTeX equation rendering (correct delimiters, aligned environments, proper symbols)
  - Code formatting (syntax highlighting conventions, indentation, block structure)
  - Structural organization (headings, numbered steps, logical section breaks)
  - Visual clarity (tables, lists, or diagrams used effectively)

RUBRIC CREATION CRITERIA:
1. Repeatable - Another rater could apply the same rubric and reach the same conclusion
2. Clearly Gradeable - The criteria are specific enough to distinguish 1-6 levels
3. Not Redundant - Not already covered by existing dimensions (instruction_following, truthfulness, prompt_correctness, writing_style, verbosity)
4. Grounded in observable differences between the external model response and the SxS winner

RUBRIC WRITING RULES:
Each rubric must:
  - Be one sentence long.
  - Check one measurable behavior.
  - Be binary (Yes/No) unless an ordinal scale is clearly necessary.
  - Avoid evaluative adjectives.
  - Avoid template repetition (do not start every rubric with "Does the response...").
GOOD: "Includes a section titled exactly '## Verification' at the end of the response."
GOOD: "Lists between 3 and 5 bullet points in the '## Key Takeaways' section."
BAD: "The response properly and clearly includes verification."
BAD: "Provides helpful key takeaways."

RUBRIC RATING (1-6): How well the external model performed on the rubric relative to the SxS winner:
| 1-2 | External model marginally better on this aspect |
| 3-4 | External model noticeably better with clear evidence |
| 5-6 | External model substantially better; SxS winner clearly lacks this quality |

Always refer to the external model by its specific name (Gemini 3 Pro or GPT 5.2) in the rubric description.

Output JSON:
{
  "rubrics": [
    {
      "name": "<short, specific title>",
      "description": "<concrete criteria — what to look for, mention the external model name>",
      "scale": "1-6 scale: how well this aspect was performed in the comparison",
      "rating": <1-6>
    }
  ]
}

Return empty {"rubrics": []} if the external model does not outperform the SxS winner in any concrete, gradeable aspect.

---
TASK: FULL_EVALUATION
Reference: All sections above (EVALUATE_TWO, COMPARE_TWO, COMPARE_EXTERNAL, CREATE_RUBRICS_EXTERNAL).

Input: USER PROMPT, RESPONSE A, RESPONSE B, EXTERNAL MODEL (Gemini 3 Pro) RESPONSE, EXTERNAL MODEL (GPT 5.2) RESPONSE.

Perform the following in order and output a SINGLE JSON with all results:
1. Evaluate Response A and Response B only (same output as EVALUATE_TWO). Apply fault-first reasoning: find faults on every dimension and cite them in the "reason" field. Apply the 5 dimensions and 1-6 scores only to A and B; do not score Gemini or GPT on these dimensions. For each dimension, cite a specific flaw or gap, or state what fault you considered and rejected before 5/6; if you cannot name a fault, you have not completed the evaluation — revisit the response. For Truthfulness and Prompt Correctness, apply fact-checking skepticism: unverified factual claims cap the score at 4 or below.
2. Compare Response A vs Response B (same output as COMPARE_TWO) to determine the SxS winner.
3. Compare the SxS winner vs the Gemini 3 Pro response (same output as COMPARE_EXTERNAL).
4. Compare the SxS winner vs the GPT 5.2 response (same output as COMPARE_EXTERNAL).
5. Create rubrics comparing SxS winner vs Gemini 3 Pro (same output as CREATE_RUBRICS_EXTERNAL: object with "rubrics" key).
6. Create rubrics comparing SxS winner vs GPT 5.2 (same output as CREATE_RUBRICS_EXTERNAL: object with "rubrics" key).

Output JSON (one object with exactly these top-level keys):
{
  "evaluation_result": { "response_a": {...}, "response_b": {...} },
  "comparison_ab": { "comparison_score": <-3 to +3>, "overall_comment": "..." },
  "comparison_vs_gemini": { "comparison_score": <-3 to +3>, "comparison_comment": "..." },
  "comparison_vs_gpt": { "comparison_score": <-3 to +3>, "comparison_comment": "..." },
  "rubrics_vs_gemini": { "rubrics": { "<slug>": {...}, ... } },
  "rubrics_vs_gpt": { "rubrics": { "<slug>": {...}, ... } }
}

Use the same structure and requirements for each nested part as in the individual tasks (EVALUATE_TWO, COMPARE_TWO, COMPARE_EXTERNAL, CREATE_RUBRICS_EXTERNAL). Return ONLY this single JSON object.

================================================================================
IMPORTANT: Return ONLY valid JSON. No explanatory text outside the JSON structure.
================================================================================
"""
)

QC_SYSTEM_PROMPT = """
You are an expert Quality Control auditor for the Vindex evaluation pipeline.

You will receive a human rater's manual evaluation record containing comparison comments and rubrics
for GPT and Gemini models. When provided, the input will also include the actual response texts
(response_a, response_b, gemini_response, gpt_response) so you can verify that comments are
grounded in the content they refer to. QC is performed on manual ratings done by humans. You must
perform exactly SEVEN quality checks (CHECK 1 through CHECK 7 below).
Use the VINDEX QC GUIDELINES below as reference for overall objectives and standards; your
output must still follow the QC CHECK CATEGORIES and OUTPUT FORMAT specified in this prompt.
Do NOT add new keys to the output; if a comment is not grounded in the responses, fail the
appropriate existing check and explain in the existing "issue" field.

================================================================================
VINDEX QC GUIDELINES (REFERENCE)
================================================================================

1. QC OBJECTIVES
   - Ratings follow defined rubrics and scales
   - Weighted scores calculated correctly (IF*0.25 + Truth*0.25 + Correctness*0.20 + Writing*0.15 + Verbosity*0.15)
   - Winners/ties follow thresholds: difference ≥ 0.10 → winner; difference < 0.05 → tie
   - Comments are concrete, evidence-based, actionable, and must NOT appear LLM-generated
   - External model comparisons and rubrics are properly justified
   - Unsafe or disallowed content is flagged (e.g., em dash "—")

2. SCOPE OF QC REVIEW
   Applies to: Response A/B ratings, weighted scores, SxS winner decision, overall comment,
   external model responses and Likert comparisons, external model comparison comments,
   newly added rubrics, flags and escalation notes.

3. COMMENT QUALITY STANDARDS
   All freeform comments must be: Specific (quote exact phrases, steps, or behaviors);
   Justified (linked to rubric criteria); Grounded in the actual response content (when response
   texts are provided, comments must not cite or describe content absent from those responses);
   Neutral and professional; Human-written (not LLM-style).
   Prohibited: Vague phrases ("better explanation", "looks fine", "more comprehensive");
   repetition of rubric text without analysis; generic statements without concrete examples;
   claims about response content that are not supported by the provided response text.
   Minimum: at least one concrete observation per response; comments must explain WHY scores differ.

4. EXTERNAL MODEL COMPARISON QC
   Both GPT-5.2 and Gemini-3 Pro comparison comments must be present and complete.
   Each Likert comparison comment must: reference specific capability differences; explain WHY
   the model is better/worse/similar; include concrete examples. Prohibited: generic comparisons;
   mismatch between Likert score and explanation; missing justification for non-zero Likert scores.

5. RUBRIC CREATION QC
   New rubrics must be: Repeatable; Clearly gradeable; Not already covered by existing dimensions
   (instruction_following, truthfulness, prompt_correctness, writing_style, verbosity);
   Grounded in the response (when response text is provided, the rubric must describe criteria
   that can be evaluated against that response content).
   Each rubric must have: name (short, specific title), description (concrete criteria — what to
   look for), scale (1-6), rating (1-6).
   SPECIFICITY: Rubric names and descriptions must focus on concrete, critical steps or features.
   GOOD: "Code includes clear, helpful documentation", "Did the response correctly factor the
   quadratic equation?", "Error handling covers edge cases (null input, empty array, overflow)".
   BAD (too vague — must fail QC): "Was the solution clear?", "Better explanation",
   "More comprehensive", "Higher quality response".
   PRESENTATION/FORMATTING: Rubrics for formatting, presentation, and LaTeX rendering quality
   are valid and expected when the external model demonstrates superior LaTeX rendering, code
   formatting, structural organization, or visual clarity.
   RATING SCALE: 1-2 = external model marginally better; 3-4 = noticeably better with clear
   evidence; 5-6 = substantially better, SxS winner clearly lacks this quality.
   Prohibited: vague or overlapping rubrics; rubrics that duplicate existing dimension criteria;
   rubrics that describe dimensions not present or not evaluable in the actual response being rated;
   generic rubric names like "clarity" or "quality" without concrete criteria.

6. SAFETY & FLAGGING QC
   Flag: safety concerns, PII, disallowed characters (e.g. em dash "—"), copyright/IP issues,
   prompt injection. Every flag must have clear justification.

7. SEVERITY LEVELS (REFERENCE)
   Critical (0): Schema break, missing scores, wrong winner logic, unflagged safety → Reject & rework
   Major (1): Wrong weights, rubric misuse, unjustified Likert, calculation errors → Return for correction
   Minor (2): Weak comments, formatting issues, minor inconsistencies → Fix during QC
   Advisory (3): Style/clarity improvements → Note for feedback only

8. SCALE SUMMARY
   pointwise_evaluations: 1-6; ab_preference: -3 to +3; ab_gpt_preference / ab_gemini_preference: -3 to +3;
   rubric scale: 1-6. Decision thresholds: clear winner if weighted_score difference ≥ 0.10;
   tie if difference < 0.05; high quality ≥ 5.2; low quality < 4.0.

================================================================================
QC CHECK CATEGORIES
================================================================================

--------------------------------------------------------------------------------
CHECK 1: AI-GENERATED TEXT DETECTION (Major)
--------------------------------------------------------------------------------
Scan ALL of the following text fields (human rater input) for signs of AI-generated writing:
  - ab_comment (overall A vs B comparison comment explaining which response is preferred and why)
  - human_ab_gpt_comment (human rater's comparison vs GPT comment)
  - human_ab_gemini_comment (human rater's comparison vs Gemini comment)
  - human_gpt_rubric_name
  - human_gpt_rubric_description
  - human_gemini_rubric_name
  - human_gemini_rubric_description

Detection criteria:

A) PROHIBITED WORDS (single words commonly overused by LLMs):
   delve, underscore, pivotal, realm, harness, illuminate, revolutionize,
   cutting-edge, game-changing, transformative, multifaceted, comprehensive,
   nuanced, robust, leverage, utilize, facilitate, endeavor, encompass,
   paramount, intricate, commendable, noteworthy, meticulous, invaluable,
   indispensable, arguably, notably, remarkably, undoubtedly, fundamentally,
   essentially, significantly, substantially, furthermore, moreover,
   overarching, synergy, paradigm, benchmark, landscape, ecosystem,
   trajectory, streamline, bolster, foster, elevate, augment, spearhead,
   navigate, unpack, unravel, demystify, elucidate, showcase, spotlight,
   resonate, align, optimize, empower, innovative, groundbreaking,
   unprecedented, holistic, seamless, versatile, scalable, actionable,
   insightful, impactful, profound, compelling, exceptional, superior,
   vital, crucial, imperative, stellar, exemplary, admirable, laudable,
   praiseworthy

B) PROHIBITED PHRASES (multi-word patterns characteristic of LLM output):
   "that being said", "at its core", "to put it simply", "this underscores",
   "it is worth noting", "it's important to note", "in today's world",
   "in the realm of", "plays a crucial role", "serves as a testament",
   "a testament to", "paves the way", "sheds light on", "in a nutshell",
   "the bottom line is", "when it comes to", "on the other hand",
   "by the same token", "it goes without saying", "needless to say",
   "all things considered", "at the end of the day", "in light of",
   "with that in mind", "having said that", "it's no secret that",
   "the fact remains", "stands as a", "offers a unique",
   "provides a comprehensive", "represents a significant",
   "marks a pivotal", "demonstrates a commitment",
   "underscores the importance", "highlights the need",
   "reflects the growing", "is a prime example", "serves as a reminder",
   "cannot be overstated", "is poised to", "continues to evolve",
   "remains to be seen"

C) STRUCTURAL SIGNALS of AI writing:
   - Overly uniform sentence length and rhythm
   - Formulaic paragraph structure (claim-elaboration-conclusion pattern)
   - Excessive hedging or qualifier stacking
   - Unnaturally smooth transitions between all paragraphs
   - Generic superlatives without concrete evidence

For each field scanned, report any detected AI signals. If ANY field contains
prohibited words, phrases, or structural signals, the check FAILS.

Severity:
  - 3+ prohibited words/phrases found across all fields → Major (severity 1)
  - 1-2 prohibited words/phrases found → Minor (severity 2)
  - Structural signals only (no prohibited words) → Advisory (severity 3)

--------------------------------------------------------------------------------
CHECK 2: RUBRIC-VS-COMMENT GROUNDING VALIDATION (Major)
--------------------------------------------------------------------------------
Verify that each rubric is grounded in its corresponding comparison comment, is specific
enough to be gradeable, and does not duplicate existing evaluation dimensions.
If a rubric has no name/description (empty or missing), skip validation for that model and mark it as pass.

SPECIFICITY VALIDATION (applies to both GPT and Gemini rubrics):
  - The rubric name must be a SHORT, SPECIFIC title describing a concrete quality — not a vague
    or generic label. Fail if the name is vague like "clarity", "quality", "better explanation",
    "more comprehensive", or "higher quality response". Pass if it targets a concrete aspect
    like "Code includes clear documentation", "Correct factoring of the quadratic equation",
    "Edge-case error handling", "LaTeX equation rendering quality".
  - The rubric description must describe CONCRETE, CRITICAL criteria — specific steps, features,
    or behaviors to look for. Fail if it uses generic language without concrete criteria.

REDUNDANCY VALIDATION:
  - The rubric must NOT duplicate or substantially overlap with the five existing evaluation
    dimensions: instruction_following, truthfulness, prompt_correctness, writing_style, verbosity.
    A rubric that essentially restates one of these dimensions under a different name must fail.

PRESENTATION/FORMATTING RUBRICS ARE VALID:
  - Rubrics targeting formatting, presentation, or LaTeX rendering quality are explicitly valid
    when the external model demonstrates superior quality in these areas. Examples of valid
    presentation rubrics: "LaTeX equation rendering (correct delimiters, aligned environments)",
    "Code formatting (indentation, block structure)", "Structural organization (headings,
    numbered steps, logical sections)", "Visual clarity (tables, lists, diagrams)".

GPT rubric grounding:
  - human_gpt_rubric_name must relate to a topic actually discussed in human_ab_gpt_comment
  - human_gpt_rubric_description must reflect content, observations, or analysis present in human_ab_gpt_comment
  - human_gpt_rubric_scale_rating must be consistent with the sentiment/assessment in human_ab_gpt_comment
    Rating scale interpretation: 1-2 = external model marginally better; 3-4 = noticeably better
    with clear evidence; 5-6 = substantially better, SxS winner clearly lacks this quality.
    If the comment says GPT outperforms clearly, the rating should be 3-6;
    if the comment says GPT is only slightly better, the rating should be 1-2.

Gemini rubric grounding:
  - human_gemini_rubric_name must relate to a topic actually discussed in human_ab_gemini_comment
  - human_gemini_rubric_description must reflect content, observations, or analysis present in human_ab_gemini_comment
  - human_gemini_rubric_scale_rating must be consistent with the sentiment/assessment in human_ab_gemini_comment
    (same rating scale interpretation as GPT above)

Comment grounded in responses (when response_a, response_b, gemini_response, gpt_response are provided):
  - human_ab_gpt_comment compares the SxS winner (A or B, from ab_preference) with the GPT response. The comment must be grounded in the actual text of those two responses: any specific claims, quotes, or descriptions (e.g. "Response A says X", "GPT omits Y") must be supported by the provided response_a, response_b, and gpt_response. If the comment cites or describes content that does not appear in the relevant responses, set comment_grounded_in_responses.gpt_comment_grounded_in_responses to {"result": "fail", "issue": "<explanation>"} and fail this check.
  - human_ab_gemini_comment compares the SxS winner with the Gemini response. Similarly, the comment must be grounded in the actual SxS winner text and gemini_response. If the comment references content not present in those responses, set comment_grounded_in_responses.gemini_comment_grounded_in_responses to {"result": "fail", "issue": "<explanation>"} and fail this check.
  - Always output the key comment_grounded_in_responses with gpt_comment_grounded_in_responses and gemini_comment_grounded_in_responses. When response texts are not provided, set both to {"result": "pass", "issue": "skipped (response texts not provided)"}. When response texts are provided and the comment is grounded, set to {"result": true, "issue": ""}.

Rubric grounded in responses (when gemini_response and gpt_response are provided):
  - The rubric name and description for each external model must describe criteria that can be evaluated against the actual response content of that model. human_gpt_rubric_name and human_gpt_rubric_description should reflect aspects that are present or meaningfully absent in gpt_response (e.g. "use of examples" is evaluable only if the response does or does not contain examples). human_gemini_rubric_name and human_gemini_rubric_description similarly must be evaluable against gemini_response. If a rubric describes a dimension that cannot be observed or assessed in the provided response at all (e.g. rubric is "code quality" but the response is plain prose with no code), fail this check and explain in the existing gpt_grounding or gemini_grounding sub-fields (e.g. name_grounded or description_grounded with issue explaining that the rubric is not grounded in the response content).
  - When gemini_response or gpt_response are not provided, skip rubric-response grounding for that model.

Failure conditions:
  - Rubric name is vague or generic (e.g. "clarity", "quality", "better explanation") → Major (severity 1)
  - Rubric description uses generic language without concrete, specific criteria → Major (severity 1)
  - Rubric duplicates or substantially overlaps an existing dimension (instruction_following, truthfulness, prompt_correctness, writing_style, verbosity) → Major (severity 1)
  - Rubric name has NO connection to the comparison comment → Major (severity 1)
  - Rubric description discusses aspects NOT mentioned in the comparison comment → Major (severity 1)
  - Rubric rating contradicts the sentiment of the comparison comment → Major (severity 1)
  - Comparison comment (human_ab_gpt_comment or human_ab_gemini_comment) cites or describes content not present in the actual response texts provided → Major (severity 1)
  - Rubric (name or description) describes a dimension that cannot be evaluated against the actual external model response content provided → Major (severity 1)
  - Rubric is loosely connected but could be more specific → Minor (severity 2)
  - Rubric name is somewhat specific but could be more concrete → Minor (severity 2)
  - Minor rating-sentiment inconsistency → Minor (severity 2)
  - Comment is loosely grounded in responses but overstates or slightly misrepresents content → Minor (severity 2)
  - Rubric is only loosely evaluable against the response (e.g. dimension is vague or only partly present) → Minor (severity 2)

--------------------------------------------------------------------------------
CHECK 3: AB PREFERENCE vs COMMENT GROUNDING (Major)
--------------------------------------------------------------------------------
Verify that ab_preference (integer -3 to +3) is consistent with ab_comment (overall A vs B comparison comment).
  - ab_preference: -1 to -3 means A is preferred; +1 to +3 means B is preferred; 0 means tie/neutral.
  - ab_comment: must explain which response (A or B) is preferred and why, or state that they are tied/similar.

Validation rules:
  - If ab_comment clearly states A is better (or A wins), ab_preference must be positive (1, 2, or 3).
  - If ab_comment clearly states B is better (or B wins), ab_preference must be negative (-1, -2, or -3).
  - If ab_comment states tie, similar, or no clear winner, ab_preference must be 0 (or close to 0; 0 is the only valid "tie" score).
  - If ab_comment is missing or empty, cannot validate → mark as pass (no grounding to check).

Comment grounded in responses (when response_a and response_b are provided):
  - ab_comment must be grounded in the actual content of response_a and response_b. Any specific claims about what A or B said, did, or contained (e.g. "A explains X", "B is more concise because...") must be supported by the provided response text. If the comment cites or describes content that does not appear in response_a or response_b, set ab_comment_grounded_in_responses to {"result": "fail", "issue": "<explanation>"} and fail this check.
  - Always output the key ab_comment_grounded_in_responses. When response_a and response_b are not provided, set to {"result": "pass", "issue": "skipped (response_a/response_b not provided)"}. When they are provided and ab_comment is grounded, set to {"result": true, "issue": ""}.

Failure conditions:
  - ab_preference sign (positive/negative/zero) contradicts the clear verdict in ab_comment → Major (severity 1)
  - ab_comment cites or describes content not present in response_a or response_b (when provided) → Major (severity 1)
  - ab_preference magnitude (e.g. +3 vs +1) is inconsistent with strength of preference stated in comment → Minor (severity 2)
  - Comment is ambiguous but preference is non-zero, or vice versa → Minor (severity 2)
  - ab_comment is loosely grounded in responses but overstates or slightly misrepresents content → Minor (severity 2)

--------------------------------------------------------------------------------
CHECK 4: RUBRIC RATING JUSTIFICATION (Major)
--------------------------------------------------------------------------------
Verify that each rubric scale rating (1-6) is justified by the rubric itself and by the comparison comment.
The rubric name and description define what is being evaluated; the rating must reflect how well the
external model performed on that rubric RELATIVE TO THE SXS WINNER, with evidence in the comparison comment.

Rating scale interpretation (rubrics measure how much better the external model is vs SxS winner):
  - 1-2: External model marginally better on this aspect
  - 3-4: External model noticeably better with clear evidence
  - 5-6: External model substantially better; SxS winner clearly lacks this quality

GPT rubric rating justification:
  - human_gpt_rubric_scale_rating (1-6) must be justified by:
    (a) The rubric's own definition: human_gpt_rubric_name and human_gpt_rubric_description define
        the specific, concrete criteria to evaluate. The rating must align with how much better
        GPT 5.2 performs on that specific criterion compared to the SxS winner.
    (b) Evidence in human_ab_gpt_comment: the comment must contain observations or reasoning that
        support the given rating level for this rubric. A high rating (5-6) requires strong evidence
        that GPT 5.2 is substantially better and the SxS winner clearly lacks this quality; a low
        rating (1-2) indicates only a marginal advantage; mid (3-4) requires clear but not
        overwhelming evidence.
  - If rubric name/description is empty, skip GPT and mark as pass.

Gemini rubric rating justification:
  - human_gemini_rubric_scale_rating (1-6) must be justified by:
    (a) The rubric's own definition: human_gemini_rubric_name and human_gemini_rubric_description;
        rating must align with how much better Gemini 3 Pro performs on that specific criterion.
    (b) Evidence in human_ab_gemini_comment that supports the numeric rating for this rubric.
  - If rubric name/description is empty, skip Gemini and mark as pass.

Failure conditions:
  - Rating is inconsistent with the rubric's stated criteria (e.g. rubric describes specific code quality but rating seems to reflect general clarity) → Major (severity 1)
  - No evidence in the comparison comment to support the given rating for this rubric → Major (severity 1)
  - Rating magnitude mismatches evidence strength (e.g. comment says "slightly better formatting" but rating is 6) → Major (severity 1)
  - Rating could be justified but the link between comment evidence and rubric is weak or implicit → Minor (severity 2)
  - Rubric description is somewhat vague so justification is ambiguous → Minor (severity 2)

--------------------------------------------------------------------------------
CHECK 5: GPT AND GEMINI PREFERENCE vs COMMENT GROUNDING (Major)
--------------------------------------------------------------------------------
Verify that the external-model preference ratings are consistent with their corresponding comparison comments.
Scale: -3 to +3. Positive = external model (GPT or Gemini) preferred over SxS winner; Negative = SxS winner preferred; 0 = tie/neutral.

GPT preference vs comment:
  - ab_gpt_preference (integer -3 to +3): Likert score for SxS winner vs GPT 5.2.
  - human_ab_gpt_comment: Human rater's comparison comment (SxS winner vs GPT). Must explain how GPT compares to the SxS winner.
  - If the comment clearly states GPT is better or outperforms → ab_gpt_preference must be positive (1, 2, or 3).
  - If the comment clearly states SxS winner is better or GPT underperforms → ab_gpt_preference must be negative (-1, -2, or -3).
  - If the comment states tie, similar, or no clear winner → ab_gpt_preference must be 0.
  - If ab_gpt_preference is missing, skip GPT and mark as pass for GPT.

Gemini preference vs comment:
  - ab_gemini_preference (integer -3 to +3): Likert score for SxS winner vs Gemini 3 Pro.
  - human_ab_gemini_comment: Human rater's comparison comment (SxS winner vs Gemini). Must explain how Gemini compares to the SxS winner.
  - Same validation rules: comment verdict must match the sign (and ideally magnitude) of the preference score.
  - If ab_gemini_preference is missing, skip Gemini and mark as pass for Gemini.

Failure conditions:
  - ab_gpt_preference sign contradicts the clear verdict in human_ab_gpt_comment → Major (severity 1)
  - ab_gemini_preference sign contradicts the clear verdict in human_ab_gemini_comment → Major (severity 1)
  - Magnitude (e.g. +3 vs +1) inconsistent with strength of preference in comment → Minor (severity 2)
  - Comment ambiguous but preference non-zero (or vice versa) → Minor (severity 2)

--------------------------------------------------------------------------------
CHECK 6: JUSTIFICATION AI-GENERATION DETECTION (Major)
--------------------------------------------------------------------------------
Perform a HOLISTIC semantic analysis on ALL human-written justification/comment fields to determine
whether the text appears to be generated or heavily rewritten by an AI model, BEYOND the keyword-
based detection in CHECK 1. This check focuses on writing style, tone, and structural patterns
rather than individual prohibited words.

Fields to analyze:
  - ab_comment
  - human_ab_gpt_comment
  - human_ab_gemini_comment
  - human_gpt_rubric_name
  - human_gpt_rubric_description
  - human_gemini_rubric_name
  - human_gemini_rubric_description

Detection criteria (holistic — look at each field as a whole):
  A) TONE: Unnaturally polished, overly formal, or suspiciously fluent for a human rater's quick
     evaluation note. Human raters typically write brief, direct, sometimes informal observations;
     AI text tends to be uniformly polished and impersonal.
  B) STRUCTURE: Every paragraph follows a rigid claim → elaboration → conclusion formula.
     Human raters jump between points, use fragments, and vary paragraph length naturally.
  C) HEDGING & QUALIFIERS: Excessive softening ("it could be argued", "one might note",
     "to some extent") layered throughout, suggesting auto-generated diplomatic language.
  D) COVERAGE BREADTH: The comment covers far more aspects than a human typically would in a quick
     review, systematically enumerating every possible dimension as if following an internal checklist.
  E) VOICE: Lacks personal perspective, first-person observations, or colloquial phrasing that would
     indicate a real human wrote it. Reads like documentation rather than a reviewer's note.

For each field, report whether it appears AI-generated and why. If ANY field shows strong holistic
AI signals, the check FAILS.

Severity:
  - Multiple fields show strong AI-generation signals → Major (severity 1)
  - One field shows moderate AI-generation signals → Minor (severity 2)
  - Faint stylistic hints only → Advisory (severity 3)

--------------------------------------------------------------------------------
CHECK 7: JUSTIFICATION GRAMMAR CHECK (Minor)
--------------------------------------------------------------------------------
Check ALL human-written justification/comment fields for grammar, spelling, and language quality.
This is NOT about penalizing informal writing — human raters are expected to write concise, sometimes
casual notes. Instead, flag errors that impede clarity or indicate carelessness.

Fields to analyze:
  - ab_comment
  - human_ab_gpt_comment
  - human_ab_gemini_comment
  - human_gpt_rubric_description
  - human_gemini_rubric_description

Grammar check criteria:
  A) SPELLING ERRORS: Misspelled words (typos are acceptable if the meaning is clear;
     flag only words that are genuinely wrong or could cause confusion).
  B) GRAMMATICAL ERRORS: Subject-verb disagreement, incorrect tense, dangling modifiers,
     run-on sentences, sentence fragments that obscure meaning.
  C) PUNCTUATION: Missing or misused punctuation that changes meaning or reduces readability
     (not minor style preferences like Oxford comma usage).
  D) CLARITY: Sentences so poorly constructed that the intended meaning is ambiguous or lost.
  E) INDIAN ENGLISH COMPLIANCE: All comments and rubric text MUST use Indian English
     (British English spelling conventions as used in India). Flag any use of American English
     spellings. Common violations to detect:
     - "color" → should be "colour", "favor" → "favour", "honor" → "honour",
       "behavior" → "behaviour", "neighbor" → "neighbour", "labor" → "labour"
     - "organize" → should be "organise", "realize" → "realise", "optimize" → "optimise",
       "analyze" → "analyse", "recognize" → "recognise", "summarize" → "summarise",
       "customize" → "customise", "utilize" → "utilise", "minimize" → "minimise",
       "maximize" → "maximise", "prioritize" → "prioritise", "categorize" → "categorise"
     - "center" → should be "centre", "meter" → "metre", "fiber" → "fibre",
       "theater" → "theatre", "liter" → "litre"
     - "defense" → should be "defence", "offense" → "offence", "license" (noun) → "licence",
       "practice" (noun) → "practise" (verb)
     - "program" (non-computing) → should be "programme", "catalog" → "catalogue",
       "dialog" → "dialogue", "analog" → "analogue"
     - "judgment" → should be "judgement", "acknowledgment" → "acknowledgement",
       "fulfillment" → "fulfilment"
     - "enrollment" → should be "enrolment", "modeling" → "modelling",
       "traveling" → "travelling", "canceled" → "cancelled", "labeled" → "labelled"
     Note: Technical terms, proper nouns, code identifiers, and direct quotes from model
     responses are exempt from this check. Only flag Indian English violations in the
     rater's own written commentary.

For each field, report specific grammar/spelling issues found (including Indian English
violations). If NO issues are found, mark pass.
Minor typos that don't affect clarity should be noted as Advisory, not failure.

Severity:
  - Multiple grammar errors that obscure meaning across fields → Major (severity 1)
  - A few grammar errors that reduce clarity → Minor (severity 2)
  - American English spellings used instead of Indian English → Minor (severity 2)
  - Minor typos or style issues only → Advisory (severity 3)

================================================================================
SEVERITY LEVELS
================================================================================
| Severity | Level | Description                              | Action              |
|----------|-------|------------------------------------------|---------------------|
| Major    | 1     | AI text, ungrounded rubric, rubric not grounded in response content, comment not grounded in responses, preference mismatch, unjustified rating, GPT/Gemini preference mismatch, holistic AI-generation in justifications, grammar errors obscuring meaning | Return for correction |
| Minor    | 2     | Weak signals, loose grounding, weak rating justification, comment or rubric loosely grounded in responses, moderate AI-generation signals, grammar errors reducing clarity | Fix during QC       |
| Advisory | 3     | Style-only AI signals, faint AI hints, minor typos, minor suggestions | Note for feedback   |

================================================================================
DECISION LOGIC
================================================================================
- AI detection (CHECK 1), justification AI detection (CHECK 6), and justification grammar (CHECK 7) are for reporting only: they do NOT affect pass/fail. Report them inside checks.ai_detection but do not use them when setting qc_status.
- For pass/fail, consider only: rubric_comment_grounding, ab_preference_comment_grounding, rubric_rating_justification, external_preference_comment_grounding.
- If ANY of those four checks has severity 1 → qc_status = "QC_Fail"
- If all four have severity 2 or 3 (or pass) → qc_status = "QC_Pass"
- overall_severity = the lowest (most severe) severity number among those four checks only, or 3 if all four pass (do not include ai_detection, justification_ai_check, or justification_grammar_check in this minimum)

================================================================================
OUTPUT FORMAT (STRICT JSON ONLY)
================================================================================
- Do NOT use a separate "issues" key anywhere. For any key that can be true/false (or pass/fail), ALWAYS return a dictionary with "result" and "issue" only. When the condition is pass/true: {"result": "pass", "issue": ""} or {"result": true, "issue": ""}. When the condition is fail/false: {"result": "fail", "issue": "<explanation>"} or {"result": false, "issue": "<explanation>"}. Never return a bare string or boolean; always use the dictionary form with issue empty when pass/true.
- Core structure and key names must not be changed.

{
  "qc_status": "QC_Pass" or "QC_Fail",
  "overall_severity": <1-3, lowest severity found, or 3 if all pass>,
  "checks": {
    "ai_detection": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation when AI detected>"},
      "severity": <1-3 or null if pass>,
      "flagged_fields": {
        "ab_comment": ["<detected word/phrase 1>", ...],
        "human_ab_gpt_comment": ["<detected word/phrase 1>", ...],
        "human_ab_gemini_comment": ["<detected word/phrase 1>", ...],
        "human_gpt_rubric_name": ["<detected word/phrase>", ...],
        "human_gpt_rubric_description": ["<detected word/phrase>", ...],
        "human_gemini_rubric_name": ["<detected word/phrase>", ...],
        "human_gemini_rubric_description": ["<detected word/phrase>", ...]
      },
      "structural_signals": ["<signal description>", ...],
      "justification_ai_check": {
        "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<holistic AI-generation explanation>"},
        "severity": <1-3 or null if pass>,
        "flagged_fields": {
          "ab_comment": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation of AI signals>"},
          "human_ab_gpt_comment": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
          "human_ab_gemini_comment": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
          "human_gpt_rubric_name": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
          "human_gpt_rubric_description": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
          "human_gemini_rubric_name": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
          "human_gemini_rubric_description": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"}
        }
      },
      "justification_grammar_check": {
        "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<grammar issues summary>"},
        "severity": <1-3 or null if pass>,
        "flagged_fields": {
          "ab_comment": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<specific grammar/spelling issues>"},
          "human_ab_gpt_comment": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<specific issues>"},
          "human_ab_gemini_comment": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<specific issues>"},
          "human_gpt_rubric_description": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<specific issues>"},
          "human_gemini_rubric_description": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<specific issues>"}
        }
      }
    },
    "rubric_comment_grounding": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
      "severity": <1-3 or null if pass>,
      "gpt_grounding": {
        "name_grounded": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"},
        "description_grounded": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"},
        "rating_consistent": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"}
      },
      "gemini_grounding": {
        "name_grounded": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"},
        "description_grounded": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"},
        "rating_consistent": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"}
      },
      "comment_grounded_in_responses": {
        "gpt_comment_grounded_in_responses": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"} or {"result": "pass", "issue": "skipped (response texts not provided)"},
        "gemini_comment_grounded_in_responses": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"} or {"result": "pass", "issue": "skipped (response texts not provided)"}
      }
    },
    "ab_preference_comment_grounding": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
      "severity": <1-3 or null if pass>,
      "preference_matches_comment": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"},
      "ab_comment_grounded_in_responses": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"} or {"result": "pass", "issue": "skipped (response_a/response_b not provided)"}
    },
    "rubric_rating_justification": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
      "severity": <1-3 or null if pass>,
      "gpt_rating_justified": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"},
      "gemini_rating_justified": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"}
    },
    "external_preference_comment_grounding": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
      "severity": <1-3 or null if pass>,
      "gpt_preference_matches_comment": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"},
      "gemini_preference_matches_comment": {"result": true, "issue": ""} or {"result": false, "issue": "<explanation>"}
    }
  },
  "summary": "<1-2 sentence summary of QC result>"
}

Return ONLY valid JSON. No text outside JSON structure.
"""


# =============================================================================
# PROMPT ENHANCEMENT — transform a basic prompt into a structured, constraint-
# enriched version that elicits higher-quality LLM responses.
# =============================================================================

PROMPT_ENHANCEMENT_SYSTEM_PROMPT = """
You are an expert Prompt Engineer. Your task: transform a user's original prompt into an enhanced version by adding specific, measurable constraints that test how well an LLM can follow precise instructions.

You will receive an INPUT PROMPT. Produce an ENHANCED PROMPT that preserves the original question exactly while adding well-chosen constraints.

================================================================================
WHAT MAKES A GOOD CONSTRAINT
================================================================================

A good constraint is:
  - SPECIFIC and MEASURABLE: A reviewer could objectively check whether the response followed it.
  - RELEVANT to the input prompt's subject matter and format.
  - DIFFERENTIATING: Something that some models might do well and others might miss.

A bad constraint is:
  - Vague: "Be thorough" or "Explain clearly" (not measurable)
  - Trivially satisfied: "Include text in your response" (every model does this)
  - Irrelevant: Adding a word limit to a yes/no question

================================================================================
CONSTRAINT TYPES TO DRAW FROM
================================================================================

Below are categories of constraint types with examples. These are ILLUSTRATIONS of the kinds of constraints that exist — do NOT copy them. Instead, invent constraints that are specifically tailored to the input prompt.

REASONING — How the model should think through the problem:
  Illustration: requiring step-by-step work, asking for a problem breakdown before solving, demanding justification for each step, requesting a stated strategy before execution, asking for honest assessment of partial progress.


FORMATTING — How the output should be visually organized:
  Illustration: requiring bullet points for specific content, mandating numbered lists, specifying the use of tables for comparisons, requesting code blocks for any code, requiring LaTeX for mathematical expressions.

STRUCTURE — What sections or components the response should include:
  Illustration: requiring specific named sections (e.g., "## Analysis", "## Conclusion"), ordering requirements (analysis before solution), separation of setup from execution, inclusion of a summary.

VERIFICATION — How the model should check or validate its work:
  Illustration: requiring an independent check of the answer, asking for boundary case analysis, requesting identification of potential errors, asking for a comparison with an alternative approach.

STYLISTIC — Tone, voice, audience, and writing style requirements:
  Illustration: specifying the target audience (e.g., "explain as if to a university sophomore"), requesting a particular tone (e.g., professional, conversational, academic), requiring definitions for technical terms, asking to avoid jargon.

NON-TECHNICAL — Concrete, quantifiable parameters and limits:
  Illustration: word or length limits, requiring a specific number of examples (e.g., "include exactly 3 examples"), minimum/maximum number of bullet points, requiring a response within a specific number of paragraphs, time or resource constraints in the scenario.

================================================================================
CRITICAL RULES
================================================================================

1. PRESERVE THE ORIGINAL QUESTION: The enhanced prompt must ask exactly the same thing. Never change what is being asked — only add instructions about HOW to answer.

2. INVENT CONSTRAINTS TAILORED TO THIS SPECIFIC PROMPT: Each constraint you add must be crafted for the particular topic, domain, and difficulty of the input. A prompt about integration by parts should get constraints referencing integration techniques, not generic "show your work." A prompt about sorting algorithms should get constraints about complexity analysis, not generic "use headers."

3. MAKE EVERY CONSTRAINT MEASURABLE: A third-party reviewer should be able to read the response and objectively determine (yes/no or on a 1-6 scale) whether each constraint was followed. "Be clear" is not measurable. "Define any technical terms before using them" is measurable.

4. DIVERSIFY CONSTRAINT TYPES: Do NOT always pick from the same categories. For some prompts, stylistic constraints matter most. For others, verification or non-technical limits are more revealing. Mix your selections based on what this particular prompt would most benefit from. Avoid falling into a pattern of always choosing the same types.

5. CALIBRATE ENHANCEMENTS TO THE ORIGINAL PROMPT:
   The number and weight of added constraints MUST be directly proportional to the
   length and complexity of the original prompt. A short prompt gets light enhancement;
   a long, detailed prompt can support more.

   BEFORE writing, mentally assess the input prompt:
   a) LENGTH: How many sentences/lines does the original prompt have?
   b) COMPLEXITY: Is it a simple factual question, a standard problem, or a complex multi-step task?
   c) DECIDE the constraint count based on BOTH factors:

   d) Each "constraint" = ONE independently checkable requirement. Do NOT pack multiple
      requirements into a single sentence and count it as "one constraint." If a sentence
      contains two independently verifiable requirements, that is TWO constraints.

   SHORT PROMPT EXAMPLE:
     Input: "What is the capital of France?"
     Enhanced: "What is the capital of France? Include one brief historical fact about
       why this city became the capital."
     (1 constraint — proportional to a 1-sentence factual question)

   STANDARD PROMPT EXAMPLE:
     Input: "Prove that the sum of the first n natural numbers is n(n+1)/2."
     Enhanced: "Prove that the sum of the first n natural numbers is n(n+1)/2. Present
       your proof step-by-step, labeling each stage. After the proof, briefly verify
       the result for n = 1, 2, and 5."
     (2 constraints — proportional to a 1-sentence standard math problem)

   OVER-ENHANCED (BAD — too many constraints for a short prompt):
     Input: "Prove that there exists a student who belongs to a larger group on the second
       day than the first."
     Enhanced that adds: three named sections, proof-by-contradiction mandate, pigeonhole
       principle requirement, concrete numerical example, sentence identification, 250-word
       limit → 7+ constraints for a 2-sentence problem. This is disproportionate.

6. WRITE NATURALLY:
   The enhanced prompt must read as if a thoughtful human wrote a more detailed version
   of their original request — NOT as if an engineer appended a checklist of meta-instructions.

   NATURAL (constraints feel like part of the user's voice):
     "Prove that √2 is irrational. Present a clear proof and explicitly justify each
      logical step. After completing the proof, verify your reasoning by checking that
      no step relies on an unproven assumption."

   UNNATURAL (mechanical directives bolted onto the original):
     "Prove that √2 is irrational. Structure your response using exactly two sections:
      '## Proof' and '## Verification'. In the Proof section, use proof by contradiction
      and explicitly identify the exact sentence where the contradiction arises. In the
      Verification section, double-check each step sequentially. Your response must not
      exceed 200 words."

   Key principles:
   - The enhanced prompt should flow as one or two coherent paragraphs.
   - Constraints should feel like natural clarifications of what the user wants,
     not mechanical directives stapled onto the end.
   - Avoid over-specifying exact section names, rigid word counts, or structural
     templates unless the original prompt's length and complexity warrant it.
   - Do NOT prescribe the solution method (e.g., "use proof by contradiction",
     "apply the pigeonhole principle"). That changes WHAT is being solved, not HOW
     to present it. Only constrain presentation, structure, and verification.

================================================================================
OUTPUT
================================================================================

Return ONLY the enhanced prompt as plain text. No JSON, no markdown wrapper, no explanation.
"""


def enhance_prompt_sync(
    input_prompt: str,
    model: str = "gemini-3.1-pro-preview",
    temperature: Optional[float] = 0.4,
) -> Dict[str, Any]:
    """
    Enhance a single prompt using the PROMPT_ENHANCEMENT_SYSTEM_PROMPT.
    Returns {"enhanced_prompt": str, "original_prompt": str, "usage": dict, "error": str|None}.
    """
    usage = _empty_usage()
    try:
        result = call_gemini_sync(
            model,
            PROMPT_ENHANCEMENT_SYSTEM_PROMPT,
            f"INPUT PROMPT:\n{input_prompt}",
            response_format="text",
            temperature=temperature,
        )
        _add_usage(usage, result["usage"])
        return {
            "enhanced_prompt": result["text"].strip(),
            "original_prompt": input_prompt,
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {
            "enhanced_prompt": "",
            "original_prompt": input_prompt,
            "usage": usage,
            "error": str(e),
        }


def enhance_prompt_sync_kimi(
    kimi_api_key: str,
    input_prompt: str,
    model: str = None,
    temperature: Optional[float] = 0.4,
) -> Dict[str, Any]:
    """
    Enhance a single prompt using Kimi via Bedrock.
    Returns {"enhanced_prompt": str, "original_prompt": str, "usage": dict, "error": str|None}.
    """
    if model is None:
        model = DEFAULT_KIMI_MODEL
    usage = _empty_usage()
    try:
        result = call_kimi_sync(
            kimi_api_key,
            model,
            PROMPT_ENHANCEMENT_SYSTEM_PROMPT,
            f"INPUT PROMPT:\n{input_prompt}",
            response_format="text",
            temperature=temperature,
        )
        _add_usage(usage, result["usage"])
        return {
            "enhanced_prompt": result["text"].strip(),
            "original_prompt": input_prompt,
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {
            "enhanced_prompt": "",
            "original_prompt": input_prompt,
            "usage": usage,
            "error": str(e),
        }


def batch_enhance_prompts(
    prompts: List[str],
    model: str = "gemini-3.1-pro-preview",
    temperature: Optional[float] = 0.4,
    max_workers: int = 8,
) -> List[Dict[str, Any]]:
    """
    Enhance multiple prompts in parallel using ThreadPoolExecutor (Gemini).
    Returns list of dicts in the same order as input prompts, each with
    enhanced_prompt, original_prompt, usage, error.
    """
    if not prompts:
        return []
    results: List[Optional[Dict[str, Any]]] = [None] * len(prompts)

    def _run(idx: int, prompt: str) -> None:
        results[idx] = enhance_prompt_sync(prompt, model=model, temperature=temperature)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, p in enumerate(prompts):
            executor.submit(_run, i, p)
    return [
        r
        or {
            "enhanced_prompt": "",
            "original_prompt": prompts[i],
            "usage": _empty_usage(),
            "error": "thread did not complete",
        }
        for i, r in enumerate(results)
    ]


def batch_enhance_prompts_kimi(
    kimi_api_key: str,
    prompts: List[str],
    model: str = None,
    temperature: Optional[float] = 0.4,
    max_workers: int = 8,
) -> List[Dict[str, Any]]:
    """
    Enhance multiple prompts in parallel using Kimi via Bedrock.
    Returns list of dicts in the same order as input prompts, each with
    enhanced_prompt, original_prompt, usage, error.
    """
    if model is None:
        model = DEFAULT_KIMI_MODEL
    if not prompts:
        return []
    results: List[Optional[Dict[str, Any]]] = [None] * len(prompts)

    def _run(idx: int, prompt: str) -> None:
        results[idx] = enhance_prompt_sync_kimi(
            kimi_api_key, prompt, model=model, temperature=temperature
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, p in enumerate(prompts):
            executor.submit(_run, i, p)
    return [
        r
        or {
            "enhanced_prompt": "",
            "original_prompt": prompts[i],
            "usage": _empty_usage(),
            "error": "thread did not complete",
        }
        for i, r in enumerate(results)
    ]


# =============================================================================
# Enhanced-prompt rubric creation + per-response rubric rating (functions)
# =============================================================================


def create_rubrics_from_enhanced_prompt_sync(
    original_prompt: str,
    enhanced_prompt: str,
    model: str = "gemini-3.1-pro-preview",
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """
    Create rubrics by identifying enhancements added to the original prompt.

    INPUT:
        original_prompt: The user's raw input prompt.
        enhanced_prompt: The prompt after enhancement (with added constraints).

    OUTPUT:
        {
            "rubrics": [{"name": str, "description": str, "enhancement_source": str}, ...],
            "usage": dict,
            "error": str | None,
        }
    """
    user_content = (
        f"ORIGINAL PROMPT:\n{original_prompt}\n\n"
        f"--------------------\nENHANCED PROMPT:\n{enhanced_prompt}"
    )
    usage = _empty_usage()
    try:
        result = call_gemini_sync(
            model,
            EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS,
            user_content,
            response_format="json",
            temperature=temperature,
        )
        usage = result.get("usage") or _empty_usage()
        text = result.get("text") or ""
        parsed = json.loads(text) if text else {}
        return {
            "rubrics": parsed.get("rubrics", []),
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {"rubrics": [], "usage": usage, "error": str(e)}


def create_rubrics_from_enhanced_prompt_sync_kimi(
    kimi_api_key: str,
    original_prompt: str,
    enhanced_prompt: str,
    model: str = None,
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """Same as create_rubrics_from_enhanced_prompt_sync but uses Kimi via Bedrock."""
    if model is None:
        model = DEFAULT_KIMI_MODEL
    user_content = (
        f"ORIGINAL PROMPT:\n{original_prompt}\n\n"
        f"--------------------\nENHANCED PROMPT:\n{enhanced_prompt}"
    )
    usage = _empty_usage()
    try:
        result = call_kimi_sync(
            kimi_api_key,
            model,
            EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS,
            user_content,
            response_format="json",
            temperature=temperature,
        )
        usage = result.get("usage") or _empty_usage()
        text = result.get("text") or ""
        parsed = _parse_json_from_response(text) if text else {}
        return {
            "rubrics": parsed.get("rubrics", []),
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {"rubrics": [], "usage": usage, "error": str(e)}


def categorize_rubrics_sync(
    rubrics: List[Dict[str, Any]],
    model: str = "gemini-3.1-pro-preview",
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """
    Categorize rubrics into broad enhancement criteria categories.

    INPUT:
        rubrics: List of rubric dicts with "name" and "description" fields.

    OUTPUT:
        {
            "criteria": [str, ...],
            "usage": dict,
            "error": str | None,
        }
    """
    if not rubrics:
        return {"criteria": [], "usage": _empty_usage(), "error": None}
    rubrics_input = [
        {"name": r.get("name", ""), "description": r.get("description", "")}
        for r in rubrics
    ]
    user_content = json.dumps(rubrics_input, indent=2)
    usage = _empty_usage()
    try:
        result = call_gemini_sync(
            model,
            ENHANCEMENT_CRITERIA_CATEGORIZATION_SYSTEM_PROMPT,
            user_content,
            response_format="json",
            temperature=temperature,
        )
        usage = result.get("usage") or _empty_usage()
        text = result.get("text") or ""
        parsed = json.loads(text) if text else {}
        return {
            "criteria": parsed.get("criteria", []),
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {"criteria": [], "usage": usage, "error": str(e)}


def categorize_rubrics_sync_kimi(
    kimi_api_key: str,
    rubrics: List[Dict[str, Any]],
    model: str = None,
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """Same as categorize_rubrics_sync but uses Kimi via Bedrock."""
    if model is None:
        model = DEFAULT_KIMI_MODEL
    if not rubrics:
        return {"criteria": [], "usage": _empty_usage(), "error": None}
    rubrics_input = [
        {"name": r.get("name", ""), "description": r.get("description", "")}
        for r in rubrics
    ]
    user_content = json.dumps(rubrics_input, indent=2)
    usage = _empty_usage()
    try:
        result = call_kimi_sync(
            kimi_api_key,
            model,
            ENHANCEMENT_CRITERIA_CATEGORIZATION_SYSTEM_PROMPT,
            user_content,
            response_format="json",
            temperature=temperature,
        )
        usage = result.get("usage") or _empty_usage()
        text = result.get("text") or ""
        parsed = _parse_json_from_response(text) if text else {}
        return {
            "criteria": parsed.get("criteria", []),
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {"criteria": [], "usage": usage, "error": str(e)}


def rate_rubrics_for_responses_sync(
    original_prompt: str,
    rubrics: List[Dict[str, Any]],
    opalite_response: str,
    ophelia_response: str,
    gemini_response: str,
    gpt_response: str,
    model: str = "gemini-3.1-pro-preview",
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """
    Rate each rubric 1-6 for each of the four model responses independently.

    INPUT:
        original_prompt: The user's prompt.
        rubrics: List of rubric dicts from create_rubrics_from_enhanced_prompt_sync.
        opalite_response, ophelia_response, gemini_response, gpt_response: The four responses.

    OUTPUT:
        {
            "rubric_ratings": [
                {
                    "rubric_name": str,
                    "opalite": {"score": 1-6, "reason": str},
                    "ophelia": {"score": 1-6, "reason": str},
                    "gemini": {"score": 1-6, "reason": str},
                    "gpt": {"score": 1-6, "reason": str},
                }, ...
            ],
            "usage": dict,
            "error": str | None,
        }
    """
    rubrics_json = json.dumps(rubrics, indent=2)
    user_content = (
        f"ORIGINAL PROMPT:\n{original_prompt}\n\n"
        f"--------------------\nRUBRICS:\n{rubrics_json}\n\n"
        f"--------------------\nOPALITE RESPONSE:\n{opalite_response}\n\n"
        f"--------------------\nOPHELIA RESPONSE:\n{ophelia_response}\n\n"
        f"--------------------\nGEMINI 3 PRO RESPONSE:\n{gemini_response}\n\n"
        f"--------------------\nGPT 5.2 RESPONSE:\n{gpt_response}"
    )
    usage = _empty_usage()
    try:
        result = call_gemini_sync(
            model,
            EVALUATION_SYSTEM_PROMPT_RATE_RUBRICS,
            user_content,
            response_format="json",
            temperature=temperature,
        )
        usage = result.get("usage") or _empty_usage()
        text = result.get("text") or ""
        parsed = json.loads(text) if text else {}
        return {
            "rubric_ratings": parsed.get("rubric_ratings", []),
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {"rubric_ratings": [], "usage": usage, "error": str(e)}


def rate_rubrics_for_responses_sync_kimi(
    kimi_api_key: str,
    original_prompt: str,
    rubrics: List[Dict[str, Any]],
    opalite_response: str,
    ophelia_response: str,
    gemini_response: str,
    gpt_response: str,
    model: str = None,
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """Same as rate_rubrics_for_responses_sync but uses Kimi via Bedrock."""
    if model is None:
        model = DEFAULT_KIMI_MODEL
    rubrics_json = json.dumps(rubrics, indent=2)
    user_content = (
        f"ORIGINAL PROMPT:\n{original_prompt}\n\n"
        f"--------------------\nRUBRICS:\n{rubrics_json}\n\n"
        f"--------------------\nOPALITE RESPONSE:\n{opalite_response}\n\n"
        f"--------------------\nOPHELIA RESPONSE:\n{ophelia_response}\n\n"
        f"--------------------\nGEMINI 3 PRO RESPONSE:\n{gemini_response}\n\n"
        f"--------------------\nGPT 5.2 RESPONSE:\n{gpt_response}"
    )
    usage = _empty_usage()
    try:
        result = call_kimi_sync(
            kimi_api_key,
            model,
            EVALUATION_SYSTEM_PROMPT_RATE_RUBRICS,
            user_content,
            response_format="json",
            temperature=temperature,
        )
        usage = result.get("usage") or _empty_usage()
        text = result.get("text") or ""
        parsed = _parse_json_from_response(text) if text else {}
        return {
            "rubric_ratings": parsed.get("rubric_ratings", []),
            "usage": usage,
            "error": None,
        }
    except Exception as e:
        return {"rubric_ratings": [], "usage": usage, "error": str(e)}


def create_and_rate_rubrics_sync(
    original_prompt: str,
    enhanced_prompt: str,
    opalite_response: str,
    ophelia_response: str,
    gemini_response: str,
    gpt_response: str,
    model: str = "gemini-3.1-pro-preview",
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """
    End-to-end: create rubrics from enhanced prompt, then rate all 4 responses.

    Step 1: Create rubrics from original vs enhanced prompt.
    Step 2: Rate each rubric 1-6 for opalite, ophelia, gemini, gpt.

    OUTPUT:
        {
            "rubrics": [{"name", "description", "enhancement_source"}, ...],
            "rubric_ratings": [{"rubric_name", "opalite": {score, reason}, "ophelia": ..., "gemini": ..., "gpt": ...}, ...],
            "usage": dict (cumulative),
            "error": str | None,
        }
    """
    total_usage = _empty_usage()

    rubric_result = create_rubrics_from_enhanced_prompt_sync(
        original_prompt,
        enhanced_prompt,
        model=model,
        temperature=temperature,
    )
    _add_usage(total_usage, rubric_result.get("usage") or _empty_usage())
    if rubric_result.get("error"):
        return {
            "rubrics": [],
            "rubric_ratings": [],
            "criteria": [],
            "usage": total_usage,
            "error": f"Rubric creation failed: {rubric_result['error']}",
        }

    rubrics = rubric_result.get("rubrics", [])
    if not rubrics:
        return {
            "rubrics": [],
            "rubric_ratings": [],
            "criteria": [],
            "usage": total_usage,
            "error": None,
        }

    # Run rubric rating and criteria categorization in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        rate_future = pool.submit(
            rate_rubrics_for_responses_sync,
            original_prompt,
            rubrics,
            opalite_response,
            ophelia_response,
            gemini_response,
            gpt_response,
            model=model,
            temperature=temperature,
        )
        criteria_future = pool.submit(
            categorize_rubrics_sync,
            rubrics,
            model=model,
            temperature=temperature,
        )
        rating_result = rate_future.result()
        criteria_result = criteria_future.result()

    _add_usage(total_usage, rating_result.get("usage") or _empty_usage())
    _add_usage(total_usage, criteria_result.get("usage") or _empty_usage())

    return {
        "rubrics": rubrics,
        "rubric_ratings": rating_result.get("rubric_ratings", []),
        "criteria": criteria_result.get("criteria", []),
        "usage": total_usage,
        "error": rating_result.get("error"),
    }


def create_and_rate_rubrics_sync_kimi(
    kimi_api_key: str,
    original_prompt: str,
    enhanced_prompt: str,
    opalite_response: str,
    ophelia_response: str,
    gemini_response: str,
    gpt_response: str,
    model: str = None,
    temperature: Optional[float] = 0.2,
) -> Dict[str, Any]:
    """Same as create_and_rate_rubrics_sync but uses Kimi via Bedrock."""
    if model is None:
        model = DEFAULT_KIMI_MODEL
    total_usage = _empty_usage()

    rubric_result = create_rubrics_from_enhanced_prompt_sync_kimi(
        kimi_api_key,
        original_prompt,
        enhanced_prompt,
        model=model,
        temperature=temperature,
    )
    _add_usage(total_usage, rubric_result.get("usage") or _empty_usage())
    if rubric_result.get("error"):
        return {
            "rubrics": [],
            "rubric_ratings": [],
            "criteria": [],
            "usage": total_usage,
            "error": f"Rubric creation failed: {rubric_result['error']}",
        }

    rubrics = rubric_result.get("rubrics", [])
    if not rubrics:
        return {
            "rubrics": [],
            "rubric_ratings": [],
            "criteria": [],
            "usage": total_usage,
            "error": None,
        }

    # Run rubric rating and criteria categorization in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        rate_future = pool.submit(
            rate_rubrics_for_responses_sync_kimi,
            kimi_api_key,
            original_prompt,
            rubrics,
            opalite_response,
            ophelia_response,
            gemini_response,
            gpt_response,
            model=model,
            temperature=temperature,
        )
        criteria_future = pool.submit(
            categorize_rubrics_sync_kimi,
            kimi_api_key,
            rubrics,
            model=model,
            temperature=temperature,
        )
        rating_result = rate_future.result()
        criteria_result = criteria_future.result()

    _add_usage(total_usage, rating_result.get("usage") or _empty_usage())
    _add_usage(total_usage, criteria_result.get("usage") or _empty_usage())

    return {
        "rubrics": rubrics,
        "rubric_ratings": rating_result.get("rubric_ratings", []),
        "criteria": criteria_result.get("criteria", []),
        "usage": total_usage,
        "error": rating_result.get("error"),
    }


def _validate_evaluation_prompt() -> None:
    """Ensure EVALUATION_SYSTEM_PROMPT loaded correctly and is not truncated or broken by syntax."""
    p = EVALUATION_SYSTEM_PROMPT
    assert isinstance(p, str), "EVALUATION_SYSTEM_PROMPT must be a string"
    assert len(p) >= 7_000, (
        f"EVALUATION_SYSTEM_PROMPT appears truncated: length={len(p)} (expected >= 7000)"
    )
    required_markers = [
        "SCORING RULES",
        "FULL_EVALUATION",
        "TASK: EVALUATE_TWO",
        "Return ONLY valid JSON",
    ]
    for marker in required_markers:
        assert marker in p, (
            f"EVALUATION_SYSTEM_PROMPT missing required marker {marker!r}; prompt may be corrupted or incomplete"
        )


_validate_evaluation_prompt()


# Default models for the task-based pipeline (Step 2: response generation)
# Override via function arguments when calling run_response_generation_for_tasks.
DEFAULT_GEMINI_GENERATION_MODEL = (
    "google/gemini-3.1-pro-preview"  # Gemini 3.1 Pro Preview (OpenRouter)
)
DEFAULT_OPENAI_GENERATION_MODEL = "gpt-5.2"  # GPT 5.2 (OpenAI)

# =============================================================================
# TOKEN PRICING (per 1M tokens, standard context ≤200K)
# =============================================================================
# Gemini 3 Pro Preview — https://ai.google.dev/gemini-api/docs/pricing
GEMINI_PRICE_INPUT = 2.00  # $/1M input tokens
GEMINI_PRICE_OUTPUT = 12.00  # $/1M output tokens
GEMINI_PRICE_CACHED = 0.20  # $/1M cached input tokens (90% off input)

# GPT 5.2 — https://openai.com/api/pricing/
OPENAI_PRICE_INPUT = 1.75  # $/1M input tokens
OPENAI_PRICE_OUTPUT = 14.00  # $/1M output tokens
OPENAI_PRICE_CACHED = 0.175  # $/1M cached input tokens (90% off input)

# Kimi K2.5 (Bedrock) — https://docs.aws.amazon.com/bedrock/latest/userguide/model-pricing.html
# Note: prices estimated based on Moonshot/Bedrock standard tier.
KIMI_PRICE_INPUT = 2.00  # $/1M input tokens
KIMI_PRICE_OUTPUT = 12.00  # $/1M output tokens
KIMI_PRICE_CACHED = 0.20  # $/1M cached input tokens


def _empty_usage() -> Dict[str, int]:
    """Return a zeroed token-usage dict."""
    return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def _add_usage(accumulator: Dict[str, int], new: Dict[str, int]) -> None:
    """Add new token counts into accumulator in-place."""
    accumulator["input_tokens"] += new.get("input_tokens", 0)
    accumulator["output_tokens"] += new.get("output_tokens", 0)
    accumulator["cached_tokens"] += new.get("cached_tokens", 0)


def calculate_cost(
    gemini_usage: Dict[str, int],
    openai_usage: Dict[str, int],
    kimi_usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Calculate dollar cost from token-usage dicts.
    Returns dict with per-provider usage, cost, and total_cost_usd.
    """

    def _cost(
        usage: Dict[str, int], p_in: float, p_out: float, p_cache: float
    ) -> float:
        non_cached_input = max(
            usage.get("input_tokens", 0) - usage.get("cached_tokens", 0), 0
        )
        cached = usage.get("cached_tokens", 0)
        output = usage.get("output_tokens", 0)
        return (non_cached_input * p_in + cached * p_cache + output * p_out) / 1_000_000

    gemini_cost = _cost(
        gemini_usage, GEMINI_PRICE_INPUT, GEMINI_PRICE_OUTPUT, GEMINI_PRICE_CACHED
    )
    openai_cost = _cost(
        openai_usage, OPENAI_PRICE_INPUT, OPENAI_PRICE_OUTPUT, OPENAI_PRICE_CACHED
    )
    kimi_usage = kimi_usage or _empty_usage()
    kimi_cost = _cost(
        kimi_usage, KIMI_PRICE_INPUT, KIMI_PRICE_OUTPUT, KIMI_PRICE_CACHED
    )
    return {
        "gemini": {
            "input_tokens": gemini_usage.get("input_tokens", 0),
            "output_tokens": gemini_usage.get("output_tokens", 0),
            "cached_tokens": gemini_usage.get("cached_tokens", 0),
            "cost_usd": round(gemini_cost, 6),
        },
        "openai": {
            "input_tokens": openai_usage.get("input_tokens", 0),
            "output_tokens": openai_usage.get("output_tokens", 0),
            "cached_tokens": openai_usage.get("cached_tokens", 0),
            "cost_usd": round(openai_cost, 6),
        },
        "kimi": {
            "input_tokens": kimi_usage.get("input_tokens", 0),
            "output_tokens": kimi_usage.get("output_tokens", 0),
            "cached_tokens": kimi_usage.get("cached_tokens", 0),
            "cost_usd": round(kimi_cost, 6),
        },
        "total_cost_usd": round(gemini_cost + openai_cost + kimi_cost, 6),
    }


# Use OpenAI Responses API: https://api.openai.com/v1/responses
# See: https://platform.openai.com/docs/guides/migrate-to-responses
def _openai_responses_body(
    model: str,
    input_text: str,
    max_output_tokens: int = 65536,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Build request body for OpenAI Responses API (POST /v1/responses)."""
    body = {"model": model, "input": input_text, **kwargs}
    if max_output_tokens is not None:
        body["max_output_tokens"] = max_output_tokens
    if model and (model.startswith("gpt-5") or "gpt-5" in model):
        body["reasoning"] = {"effort": "none"}
    return body


def _openai_response_output_to_text(body: Dict[str, Any]) -> str:
    """Extract aggregated output text from a Responses API response body (output array)."""
    output = body.get("output") or []
    parts = []
    for item in output:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text" and "text" in block:
                parts.append(block["text"])
    return "".join(parts)


def _task_id(d: Dict[str, Any]) -> Any:
    """Return task identifier from a task dict. Accepts task_id or taskId (e.g. from JSONL)."""
    return d.get("task_id") or d.get("taskId")


# =============================================================================
# GEMINI BATCH API HELPERS
# See: https://ai.google.dev/gemini-api/docs/batch-api
# API reference: https://ai.google.dev/api/batch-mode
# =============================================================================

# Inline requests: keep total request size under 20MB. For larger batches use file input.
INLINE_BATCH_MAX_REQUESTS = 500

# Chunk size for task-based pipeline (rejection, generation, evaluation). Enables handling 5000+ tasks
# by splitting into multiple API batches; results are merged in order.
DEFAULT_MAX_REQUESTS_PER_BATCH = 500

# Timeout (seconds) for downloading Gemini batch result files (file-based output). Chunked batches
# keep each result file smaller; this allows large single-batch downloads when chunking is disabled.
GEMINI_BATCH_DOWNLOAD_TIMEOUT = 600


# =============================================================================
# SYNC (NON-BATCH) API CALLS — for testing / small runs without batch queue
# =============================================================================


# ---------------------------------------------------------------------------
# Vertex AI Express — primary Gemini provider (google-genai unified SDK)
# ---------------------------------------------------------------------------
# Thinking-level → token-budget mapping for the SDK ThinkingConfig.
_THINKING_LEVEL_BUDGET: Dict[str, int] = {
    "NONE": 0,
    "LOW": 1024,
    "MEDIUM": 8192,
    "HIGH": 24576,
}

_vertex_client = None
_vertex_client_lock = threading.Lock()


def _get_vertex_client():
    """Return a reusable Vertex AI Express client (thread-safe singleton).

    Reads *VERTEX_AI_API_KEY* (preferred) or *GOOGLE_API_KEY* from env.
    Optionally reads *GOOGLE_CLOUD_PROJECT* and *GOOGLE_CLOUD_LOCATION* for
    project-scoped Vertex AI features.
    """
    global _vertex_client
    if _vertex_client is None:
        with _vertex_client_lock:
            if _vertex_client is None:
                if _google_genai is None:
                    raise RuntimeError(
                        "google-genai package not installed; run: pip install google-genai"
                    )
                api_key = (
                    os.environ.get("VERTEX_AI_API_KEY")
                    or os.environ.get("GOOGLE_API_KEY")
                    or ""
                ).strip()
                project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip() or None
                location = (
                    os.environ.get("GOOGLE_CLOUD_LOCATION") or ""
                ).strip() or None
                if api_key:
                    _vertex_client = _google_genai.Client(
                        vertexai=True,
                        api_key=api_key,
                    )
                elif project:
                    _vertex_client = _google_genai.Client(
                        vertexai=True,
                        project=project,
                        location=location,
                    )
                else:
                    raise RuntimeError(
                        "VERTEX_AI_API_KEY/GOOGLE_API_KEY or "
                        "GOOGLE_CLOUD_PROJECT env var not set for Vertex AI"
                    )
    return _vertex_client


def _call_vertex_ai_gemini_sync(
    model: str,
    system_prompt: str,
    user_content: str,
    response_format: str = "text",
    temperature: Optional[float] = None,
    thinking_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call Gemini via Vertex AI REST streaming API (streamGenerateContent).

    Returns ``{"text": str, "thinking": str, "usage": dict}`` — same contract
    as :func:`call_gemini_sync`.
    """
    _log = logging.getLogger(__name__)
    if thinking_config is None:
        thinking_config = {"thinkingLevel": "HIGH"}
    model_name = model
    if model_name.startswith("google/"):
        model_name = model_name[len("google/") :]
    if model_name.startswith("models/"):
        model_name = model_name[len("models/") :]

    api_key = get_vertex_api_key()
    if not api_key:
        raise RuntimeError("VERTEX_AI_API_KEY or GOOGLE_API_KEY env var not set")

    url = (
        "https://aiplatform.googleapis.com/v1/publishers/google/models/"
        f"{model_name}:streamGenerateContent"
    )
    params = {"key": api_key}
    headers = {
        "Content-Type": "application/json",
        "X-Vertex-AI-LLM-Request-Type": "shared",
        "X-Vertex-AI-LLM-Shared-Request-Type": "priority",
    }

    contents = [{"role": "user", "parts": [{"text": user_content}]}]

    generation_config: Dict[str, Any] = {"maxOutputTokens": 65536}
    if temperature is not None:
        generation_config["temperature"] = float(temperature)
    if response_format == "json":
        generation_config["responseMimeType"] = "application/json"
    if thinking_config:
        level = (thinking_config.get("thinkingLevel") or "").upper()
        budget = _THINKING_LEVEL_BUDGET.get(level)
        if budget is not None:
            generation_config["thinkingConfig"] = {
                "thinkingBudget": budget,
                "includeThoughts": True,
            }

    data: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    if system_prompt:
        data["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    _log.info(
        "Vertex AI REST streaming request: model=%s, response_format=%s, "
        "temperature=%s, thinking=%s, system_prompt_len=%d, user_content_len=%d",
        model_name,
        response_format,
        temperature,
        thinking_config,
        len(system_prompt) if system_prompt else 0,
        len(user_content) if user_content else 0,
    )

    try:
        resp = requests.post(
            url,
            params=params,
            headers=headers,
            json=data,
            stream=True,
            timeout=300,
        )
        resp.raise_for_status()
    except Exception as e:
        _log.error(
            "Vertex AI REST streaming request failed: model=%s, error_type=%s, error=%s",
            model_name,
            type(e).__name__,
            e,
            exc_info=True,
        )
        raise

    raw_lines = []
    for line in resp.iter_lines():
        if line:
            raw_lines.append(line.decode("utf-8"))
    raw_text = "\n".join(raw_lines)

    try:
        chunks = json.loads(raw_text)
    except json.JSONDecodeError as e:
        _log.error(
            "Vertex AI REST streaming response parse error: model=%s, error=%s, raw_len=%d",
            model_name,
            e,
            len(raw_text),
        )
        raise RuntimeError(f"Failed to parse streaming response: {e}")

    text_parts: list = []
    thought_parts: list = []
    finish_reason = None
    usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

    for chunk in chunks:
        candidates = chunk.get("candidates", [])
        if candidates:
            cand = candidates[0]
            parts = (cand.get("content") or {}).get("parts", [])
            for part in parts:
                part_text = (part.get("text") or "").strip()
                if not part_text:
                    continue
                if part.get("thought"):
                    thought_parts.append(part_text)
                else:
                    text_parts.append(part_text)
            if cand.get("finishReason"):
                finish_reason = cand["finishReason"]
        um = chunk.get("usageMetadata")
        if um:
            usage = {
                "input_tokens": int(um.get("promptTokenCount", 0)),
                "output_tokens": int(um.get("candidatesTokenCount", 0)),
                "cached_tokens": int(um.get("cachedContentTokenCount", 0) or 0),
            }

    text = "".join(text_parts)
    thinking_text = "\n\n".join(thought_parts)

    if not text and finish_reason in (
        "SAFETY",
        "RECITATION",
        "OTHER",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
    ):
        _log.error(
            "Vertex AI REST response blocked: model=%s, finishReason=%s",
            model_name,
            finish_reason,
        )
        raise RuntimeError(f"Vertex AI response blocked: finishReason={finish_reason}")

    _log.info(
        "Vertex AI REST streaming success: model=%s, tokens_in=%d, tokens_out=%d",
        model_name,
        usage["input_tokens"],
        usage["output_tokens"],
    )
    return {"text": text, "thinking": thinking_text, "usage": usage}


def get_openrouter_api_key() -> str:
    """Read OpenRouter API key from env."""
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def _call_openrouter_gemini_sync(
    model: str,
    system_prompt: str,
    user_content: str,
    response_format: str = "text",
    temperature: Optional[float] = None,
    thinking_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call Gemini via OpenRouter. Mirroring common SDK/REST call pattern."""
    _log = logging.getLogger(__name__)
    api_key = get_openrouter_api_key()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY env var not set")

    # Mapping thinking config to OpenRouter if experimental think model used
    # For now, OpenRouter expects standard OpenAI format for Gemini.
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/GreenRiderTechnology",
        "X-Title": "Preference Ranking App",
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 65536,
    }
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}
    if temperature is not None:
        payload["temperature"] = float(temperature)

    _log.info(
        "OpenRouter request: model=%s, response_format=%s, temperature=%s, messages_len=%d",
        model,
        response_format,
        temperature,
        len(messages),
    )

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=300)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.error("OpenRouter request failed: %s", e, exc_info=True)
        raise

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"OpenRouter response has no choices: {data}")

    choice = choices[0]
    text = choice.get("message", {}).get("content", "").strip()
    usage_raw = data.get("usage", {})

    usage = {
        "input_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "output_tokens": int(usage_raw.get("completion_tokens", 0)),
        "cached_tokens": int(usage_raw.get("cached_tokens", 0) or 0),
    }

    _log.info(
        "OpenRouter success: model=%s, tokens_in=%d, tokens_out=%d",
        model,
        usage["input_tokens"],
        usage["output_tokens"],
    )

    return {"text": text, "usage": usage}


def call_gemini_sync(
    model: str,
    system_prompt: str,
    user_content: str,
    response_format: str = "text",
    temperature: Optional[float] = None,
    thinking_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Single synchronous Gemini request via Vertex AI REST streaming API.

    Auth is handled by VERTEX_AI_API_KEY / GOOGLE_API_KEY from env.

    Returns {"text": str, "thinking": str, "usage": dict}.
    usage has input_tokens, output_tokens, cached_tokens.
    """
    return _call_vertex_ai_gemini_sync(
        model=model,
        system_prompt=system_prompt,
        user_content=user_content,
        response_format=response_format,
        temperature=temperature,
        thinking_config=thinking_config,
    )


# Kimi via AWS Bedrock Converse API (replaces Moonshot HTTP API).
# All Kimi rejection, evaluation, and QC functions use exactly the same system prompts as Gemini
# (PROMPT_REJECTION_SYSTEM_PROMPT, EVALUATION_SYSTEM_PROMPT_*, QC_SYSTEM_PROMPT) and return data
# in the same shape as the corresponding Gemini sync/batch functions.


def _parse_json_from_response(text: str) -> Dict[str, Any]:
    """
    Parse JSON from model response text. Handles empty string, markdown code fences,
    and leading/trailing non-JSON text. Returns {} on failure.
    """
    if not text or not isinstance(text, str):
        return {}
    text = text.strip()
    if not text:
        return {}
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            text = match.group(1).strip()
    if not text:
        return {}
    start = -1
    for i, c in enumerate(text):
        if c == "{" or c == "[":
            start = i
            break
    if start == -1:
        return {}
    text = text[start:]
    open_c = text[0]
    close_c = "}" if open_c == "{" else "]"
    depth = 0
    end = -1
    for i, c in enumerate(text):
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return {}
    try:
        result = json.loads(text[:end])
        if isinstance(result, list):
            return result[0] if result and isinstance(result[0], dict) else {}
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, IndexError, TypeError):
        return {}


def call_bedrock_invoke_model_sync(
    region_name: str,
    model_id: str,
    system_prompt: str,
    user_content: str,
    response_format: str = "text",
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Single synchronous chat completion via AWS Bedrock InvokeModel API.
    Returns same shape as call_kimi_sync: {"text": str, "usage": {"input_tokens", "output_tokens", "cached_tokens"}}.
    """
    if not boto3:
        raise RuntimeError("boto3 is required for Bedrock; pip install boto3")

    api_key = (
        os.environ.get("VALOR_BEDROCK_API_KEY")
        or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        or ""
    ).strip()

    if not api_key:
        raise RuntimeError(
            "Bedrock API Key not found. Set VALOR_BEDROCK_API_KEY or AWS_BEARER_TOKEN_BEDROCK."
        )

    # Latest boto3/botocore automatic detection: set the official env var
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = api_key

    client_kwargs: Dict[str, Any] = {"region_name": region_name}
    bedrock_config = botocore.config.Config(
        connect_timeout=10,
        read_timeout=1200,
        retries={"max_attempts": 2},
    )
    client = boto3.client("bedrock-runtime", config=bedrock_config, **client_kwargs)
    system_block = []
    if system_prompt:
        system_block.append({"text": system_prompt})

    messages = [{"role": "user", "content": [{"text": user_content}]}]

    inference_config: Dict[str, Any] = {"maxTokens": 65536}
    if temperature is not None:
        inference_config["temperature"] = float(temperature)

    try:
        response = client.converse(
            modelId=model_id,
            messages=messages,
            system=system_block,
            inferenceConfig=inference_config,
        )
    except Exception as e:
        logging.getLogger(__name__).warning("Bedrock converse request failed: %s", e)
        raise

    output = response.get("output", {})
    message_out = output.get("message", {})
    content_blocks = message_out.get("content", [])
    text = ""
    for block in content_blocks:
        if "text" in block:
            text = block["text"]
            break
    text = text.strip()

    # ── Truncation detection: warn if response hit token cap ──
    _finish = response.get("stopReason", "")
    if _finish == "max_tokens":
        logging.getLogger(__name__).warning(
            "Bedrock response truncated (stopReason=max_tokens)",
        )

    usage_raw = response.get("usage", {})
    usage = {
        "input_tokens": int(usage_raw.get("inputTokens", 0)),
        "output_tokens": int(usage_raw.get("outputTokens", 0)),
        "cached_tokens": 0,
    }
    return {"text": text, "usage": usage}


def _call_moonshot_sync(
    system_prompt: str,
    user_content: str,
    temperature: Optional[float] = None,
    model: str = DEFAULT_KIMI_MODEL,
) -> Dict[str, Any]:
    """
    Fallback: call Moonshot direct API (OpenAI-compatible).
    Uses KIMI_API_KEY / MOONSHOT_API_KEY env var.
    Returns same shape: {"text": str, "usage": {...}}.
    """
    api_key = get_kimi_api_key()
    if not api_key:
        raise RuntimeError(
            "KIMI_API_KEY / MOONSHOT_API_KEY not set; cannot use Moonshot fallback"
        )
    url = f"{MOONSHOT_API_BASE_URL}/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": 65536,
    }
    if temperature is not None:
        body["temperature"] = float(temperature)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    text = ""
    if choices:
        text = (choices[0].get("message") or {}).get("content") or ""
    text = text.strip()
    # ── Truncation detection: warn if response hit token cap ──
    _finish = (choices[0].get("finish_reason") or "") if choices else ""
    if _finish == "length":
        logging.getLogger(__name__).warning(
            "Moonshot response truncated (finish_reason=length, output_tokens=%s, max_tokens=65536)",
            usage_raw.get("completion_tokens", "?"),
        )
    usage_raw = data.get("usage") or {}
    usage = {
        "input_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "output_tokens": int(usage_raw.get("completion_tokens", 0)),
        "cached_tokens": 0,
    }
    return {"text": text, "usage": usage}


def _call_openrouter_sync(
    system_prompt: str,
    user_content: str,
    temperature: Optional[float] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fallback: call OpenRouter API (OpenAI-compatible).
    Uses OPENROUTER_API_KEY env var.
    Returns same shape: {"text": str, "usage": {...}}.
    """
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set; cannot use OpenRouter fallback")
    url = f"{OPENROUTER_API_BASE_URL}/chat/completions"
    model = model or OPENROUTER_KIMI_MODEL_ID
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": 65536,
    }
    if temperature is not None:
        body["temperature"] = float(temperature)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    text = ""
    if choices:
        text = (choices[0].get("message") or {}).get("content") or ""
    text = text.strip()
    # ── Truncation detection: warn if response hit token cap ──
    _finish = (choices[0].get("finish_reason") or "") if choices else ""
    if _finish == "length":
        logging.getLogger(__name__).warning(
            "OpenRouter response truncated (finish_reason=length, output_tokens=%s, max_tokens=65536)",
            usage_raw.get("completion_tokens", "?"),
        )
    usage_raw = data.get("usage") or {}
    usage = {
        "input_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "output_tokens": int(usage_raw.get("completion_tokens", 0)),
        "cached_tokens": 0,
    }
    return {"text": text, "usage": usage}


def call_kimi_sync(
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    response_format: str = "text",
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Single synchronous chat completion with fallback chain:
    Bedrock -> Moonshot direct API -> OpenRouter.
    api_key is accepted for caller compatibility but Bedrock uses AWS creds.
    Returns {"text": str, "usage": {...}}.
    """
    _logger = logging.getLogger(__name__)
    model_id = BEDROCK_KIMI_MODEL_ID if model == DEFAULT_KIMI_MODEL else model

    # --- AWS Bedrock only ---
    region = _get_bedrock_region()
    if not region:
        raise RuntimeError("BEDROCK_REGION not set; cannot use Kimi (Bedrock)")

    return call_bedrock_invoke_model_sync(
        region_name=region,
        model_id=model_id,
        system_prompt=system_prompt,
        user_content=user_content,
        response_format=response_format,
        temperature=temperature,
    )


def prompt_rejection_check_sync(
    user_prompts: List[str],
    model: str = "gemini-3.1-pro-preview",
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Run prompt rejection check one request per prompt (no batch). Same return shape as
    batch_prompt_rejection_check. Use for testing or small lists to avoid batch queue wait.
    """
    if not user_prompts:
        return [], []
    results = []
    accepted_indices = []
    cumulative_gemini = _empty_usage()
    for i, prompt in enumerate(user_prompts):
        user_content = f"USER PROMPT:\n{prompt}"
        task_gemini = _empty_usage()
        try:
            gem_result = call_gemini_sync(
                model,
                PROMPT_REJECTION_SYSTEM_PROMPT,
                user_content,
                response_format="json",
            )
            _add_usage(task_gemini, gem_result["usage"])
            _add_usage(cumulative_gemini, gem_result["usage"])
            text = gem_result["text"]
            parsed = json.loads(text) if text else {}
        except Exception as e:
            results.append(
                {
                    "index": i,
                    "prompt": prompt,
                    "status": "REJECT",
                    "result": {},
                    "accepted": False,
                    "error": {"message": str(e)},
                    "token_usage": calculate_cost(task_gemini, _empty_usage()),
                }
            )
            continue
        status = (parsed.get("status") or "REJECT").upper()
        accepted = status == "ACCEPT"
        if accepted:
            accepted_indices.append(i)
        results.append(
            {
                "index": i,
                "prompt": prompt,
                "status": status,
                "result": parsed,
                "accepted": accepted,
                "error": None,
                "token_usage": calculate_cost(task_gemini, _empty_usage()),
            }
        )
    cumulative = calculate_cost(cumulative_gemini, _empty_usage())
    for d in results:
        d["token_usage_cumulative"] = cumulative
    return results, accepted_indices


def prompt_rejection_check_sync_kimi(
    kimi_api_key: str,
    user_prompts: List[str],
    model: str = DEFAULT_KIMI_MODEL,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Run prompt rejection check with Kimi (one request per prompt). Same return shape as
    batch_prompt_rejection_check / prompt_rejection_check_sync.
    """
    if not user_prompts:
        return [], []
    results = []
    accepted_indices = []
    cumulative = _empty_usage()
    for i, prompt in enumerate(user_prompts):
        user_content = f"USER PROMPT:\n{prompt}"
        task_usage = _empty_usage()
        try:
            kimi_result = call_kimi_sync(
                kimi_api_key,
                model,
                PROMPT_REJECTION_SYSTEM_PROMPT,
                user_content,
                response_format="json",
            )
            _add_usage(task_usage, kimi_result["usage"])
            _add_usage(cumulative, kimi_result["usage"])
            text = kimi_result["text"]
            parsed = _parse_json_from_response(text)
        except Exception as e:
            results.append(
                {
                    "index": i,
                    "prompt": prompt,
                    "status": "REJECT",
                    "result": {},
                    "accepted": False,
                    "error": {"message": str(e)},
                    "token_usage": calculate_cost(
                        _empty_usage(), _empty_usage(), task_usage
                    ),
                }
            )
            continue
        status = (parsed.get("status") or "REJECT").upper()
        accepted = status == "ACCEPT"
        if accepted:
            accepted_indices.append(i)
        results.append(
            {
                "index": i,
                "prompt": prompt,
                "status": status,
                "result": parsed,
                "accepted": accepted,
                "error": None,
                "token_usage": calculate_cost(
                    _empty_usage(), _empty_usage(), task_usage
                ),
            }
        )
    cum_cost = calculate_cost(_empty_usage(), _empty_usage(), cumulative)
    for d in results:
        d["token_usage_cumulative"] = cum_cost
    return results, accepted_indices


def call_openai_sync(
    api_key: str,
    model: str,
    user_content: str,
    max_completion_tokens: int = 65536,
) -> Dict[str, Any]:
    """
    Single synchronous request to OpenAI Responses API (POST /v1/responses).
    Returns {"text": str, "usage": {"input_tokens": int, "output_tokens": int, "cached_tokens": int}}.

    Token caching: OpenAI automatically caches common prompt prefixes (no configuration needed).
    Cached tokens are charged at 50% of normal input token rates.
    """
    _log = logging.getLogger(__name__)

    # --- OpenAI Responses API only ---
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = _openai_responses_body(
        model, user_content, max_output_tokens=max_completion_tokens
    )
    resp = requests.post(url, headers=headers, json=body)
    resp.raise_for_status()
    data = resp.json()
    text = _openai_response_output_to_text(data)
    usage_raw = data.get("usage") or {}
    cached = 0
    input_details = usage_raw.get("input_tokens_details") or {}
    if isinstance(input_details, dict):
        cached = input_details.get("cached_tokens", 0)
    usage = {
        "input_tokens": usage_raw.get("input_tokens", 0),
        "output_tokens": usage_raw.get("output_tokens", 0),
        "cached_tokens": cached,
    }
    return {"text": text, "usage": usage}


def response_generation_for_tasks_sync(
    openai_api_key: str,
    tasks: List[Dict[str, Any]],
    gemini_model: str = DEFAULT_GEMINI_GENERATION_MODEL,
    openai_model: str = DEFAULT_OPENAI_GENERATION_MODEL,
) -> List[Dict[str, Any]]:
    """
    Step 2 (sync): one Gemini + one OpenAI request per task. Same output shape as run_response_generation_for_tasks.
    Each result dict includes a "token_usage" key with per-task and cumulative totals.
    """
    if not tasks:
        return []
    out = []
    total_gemini = _empty_usage()
    total_openai = _empty_usage()
    for i, t in enumerate(tasks):
        prompt = t.get("prompt", "")
        gemini_text = ""
        gpt_text = ""
        task_gemini = _empty_usage()
        task_openai = _empty_usage()
        try:
            gem_result = call_gemini_sync(
                gemini_model,
                "",
                prompt,
                response_format="text",
                thinking_config={"thinkingLevel": "HIGH"},
            )
            gemini_text = gem_result["text"]
            task_gemini = gem_result["usage"]
            _add_usage(total_gemini, task_gemini)
        except Exception as e:
            gemini_text = f"[error: {e}]"
        try:
            oai_result = call_openai_sync(openai_api_key, openai_model, prompt)
            gpt_text = oai_result["text"]
            task_openai = oai_result["usage"]
            _add_usage(total_openai, task_openai)
        except Exception as e:
            gpt_text = f"[error: {e}]"
        out.append(
            {
                "task_id": _task_id(t),
                "prompt": prompt,
                "gemini_response": gemini_text,
                "gpt_response": gpt_text,
                "token_usage": calculate_cost(task_gemini, task_openai),
            }
        )
    # Attach cumulative totals to every result (last element has final totals)
    cumulative = calculate_cost(total_gemini, total_openai)
    for d in out:
        d["token_usage_cumulative"] = cumulative
    return out


# =============================================================================
# Opalite / Ophelia response generation (Meta GenAI API)
# =============================================================================

_GENAI_GRAPH_BASE_URL = "https://graph-genai.facebook.com/v24.0"
_GENAI_WORKSTREAM_OPALITE = "opalite"
_GENAI_WORKSTREAM_OPHELIA = "ophelia"
_GENAI_MODEL_RESPONSE_A = "opalite"
_GENAI_MODEL_RESPONSE_B = "ophelia"

_GENAI_MAX_RETRIES = 4
_GENAI_BACKOFF_BASE = 2.0
_GENAI_RETRY_STATUS_CODES = {500, 502, 503, 504}

_logger_genai = logging.getLogger(__name__ + ".genai")


def _build_genai_session():
    """Build a requests.Session with connection pooling and transport-level retries for Meta GenAI."""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy, pool_connections=4, pool_maxsize=4
    )
    session.mount("https://", adapter)
    session.headers.update(
        {"Content-Type": "application/json", "Accept": "application/json"}
    )
    return session


_GENAI_SESSION = _build_genai_session()


def _genai_post_with_retry(
    url, *, json_payload=None, files=None, data=None, label="API"
):
    """POST with application-level retry for 500-class errors against Meta GenAI."""
    resp = None
    for attempt in range(1, _GENAI_MAX_RETRIES + 1):
        if files:
            resp = requests.post(url, files=files, data=data)
        else:
            resp = _GENAI_SESSION.post(url, json=json_payload)

        if resp.status_code not in _GENAI_RETRY_STATUS_CODES:
            break

        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text[:500]
        _logger_genai.warning(
            "[%s] Attempt %d/%d got HTTP %d: %s",
            label,
            attempt,
            _GENAI_MAX_RETRIES,
            resp.status_code,
            err_body,
        )

        if attempt < _GENAI_MAX_RETRIES:
            wait = _GENAI_BACKOFF_BASE**attempt
            _logger_genai.info("[%s] Retrying in %.1fs...", label, wait)
            time.sleep(wait)

    if resp is not None and resp.status_code >= 400:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text[:500]
        _logger_genai.error(
            "[%s] Final failure HTTP %d: %s", label, resp.status_code, err_body
        )
        resp.raise_for_status()

    return resp


def get_genai_api_key() -> str:
    """Read Meta GenAI access token from env (genai_api_key or GENAI_ACCESS_TOKEN)."""
    return (
        os.environ.get("genai_api_key") or os.environ.get("GENAI_ACCESS_TOKEN") or ""
    ).strip()


def _genai_get_router_config(
    genai_api_key: str, workstream: str = _GENAI_WORKSTREAM_OPALITE
) -> dict:
    """Call Meta config API (llm_annotations_model_router_workstream). Returns raw router response."""
    url = f"{_GENAI_GRAPH_BASE_URL}/llm_annotations_model_router_workstream"
    payload = {"access_token": genai_api_key, "workstream": workstream}
    resp = _genai_post_with_retry(
        url, json_payload=payload, label=f"RouterConfig({workstream})"
    )
    return resp.json()


def get_genai_dialog_id(
    genai_api_key: str, workstream: str = _GENAI_WORKSTREAM_OPALITE
) -> str:
    """Call Meta config API once and return dialog_id for the given workstream."""
    router = _genai_get_router_config(genai_api_key, workstream=workstream)
    return (router.get("dialog_id") or "").strip() or ""


def _genai_upload_attachment(
    genai_api_key: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
) -> dict:
    """Upload a file to Meta GenAI attachment API; returns dict with handle_id."""
    url = f"{_GENAI_GRAPH_BASE_URL}/llm_annotations_attachment_upload"
    files = {"file": (filename, file_bytes, mime_type or "application/octet-stream")}
    data = {"access_token": genai_api_key}
    resp = _genai_post_with_retry(url, files=files, data=data, label="AttachUpload")
    try:
        out = resp.json()
    except Exception:
        out = {}
    handle_id = (out.get("handle_id") or "").strip() if isinstance(out, dict) else ""
    if not handle_id and isinstance(out, dict):
        for k, v in out.items():
            if "handle" in k.lower() and v:
                handle_id = str(v)
                break
    return {"handle_id": handle_id, "mime": mime_type or "image/png"}


def _genai_build_message(
    role: str,
    text: str = None,
    attachment_handle_id: str = None,
    attachment_mime: str = None,
) -> List[dict]:
    """Build one or two message dicts for Meta API (role 'user' or 'assistant')."""
    out = []
    if attachment_handle_id and role == "user":
        out.append(
            {
                "source": {"role": "user"},
                "contents": [
                    {
                        "attachment": {
                            "handle_id": attachment_handle_id,
                            "mime": attachment_mime or "image/png",
                        }
                    }
                ],
                "is_end_of_turn": True,
                "is_complete": True,
            }
        )
    if text is not None:
        out.append(
            {
                "source": {"role": role},
                "contents": [{"text": {"text": text}}],
                "is_end_of_turn": True,
                "is_complete": True,
            }
        )
    return (
        out
        if out
        else [
            {
                "source": {"role": role},
                "contents": [{"text": {"text": text or ""}}],
                "is_end_of_turn": True,
                "is_complete": True,
            }
        ]
    )


def _genai_build_messages_from_history(
    dialog_history: Optional[List[Tuple[str, str]]],
    current_prompt: str,
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
) -> List[dict]:
    """Build the full messages list for the Meta API: all previous turns then the current user message."""
    messages = []
    if dialog_history:
        for i, (user_text, assistant_text) in enumerate(dialog_history):
            h_handle, h_mime = (
                history_handle_ids[i]
                if history_handle_ids and i < len(history_handle_ids)
                else (None, None)
            ) or (None, None)
            if h_handle:
                for msg in _genai_build_message(
                    "user",
                    user_text,
                    attachment_handle_id=h_handle,
                    attachment_mime=h_mime,
                ):
                    messages.append(msg)
            else:
                messages.extend(_genai_build_message("user", user_text))
            messages.extend(_genai_build_message("assistant", assistant_text or ""))
    if current_turn_handle_id:
        for msg in _genai_build_message(
            "user",
            current_prompt,
            attachment_handle_id=current_turn_handle_id,
            attachment_mime=current_turn_mime,
        ):
            messages.append(msg)
    else:
        messages.extend(_genai_build_message("user", current_prompt))
    return messages


def _genai_call_generation(
    model_name: str,
    dialog_id: str,
    messages: List[dict],
    genai_api_key: str,
    workstream: str = _GENAI_WORKSTREAM_OPALITE,
) -> dict:
    """Single Meta GenAI generation call. Returns the raw response dict with dialog_candidates."""
    url = f"{_GENAI_GRAPH_BASE_URL}/llm_annotations_metagen_stream_turn"
    payload = {
        "access_token": genai_api_key,
        "dialog": {"messages": messages},
        "workstream": workstream,
        "model": model_name,
        "dialog_id": dialog_id,
        "options": {"max_tokens": 50000},
    }
    response = _genai_post_with_retry(
        url, json_payload=payload, label=f"Generate({model_name})"
    )

    data = None
    for line in reversed(response.text.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if "dialog_candidates" in parsed:
                data = parsed
                break
        except json.JSONDecodeError:
            continue

    if data is None:
        raise ValueError(
            "No valid response with dialog_candidates found from GenAI API"
        )
    return data


_REASONING_RE = re.compile(r"\|<reasoning_start>\|(.*?)\|<reasoning_end>\|", re.DOTALL)


def _strip_reasoning_tags(text: str) -> str:
    """Strip model reasoning tags from response text.

    Models may include reasoning wrapped in |<reasoning_start>|...|<reasoning_end>|
    tags. Only the content after the reasoning block should be displayed.
    """
    if not text:
        return text
    stripped = re.sub(
        r"\|<reasoning_start>\|.*?\|<reasoning_end>\|\s*", "", text, flags=re.DOTALL
    )
    return stripped.strip()


def _extract_reasoning(text: str) -> str:
    """Extract the reasoning trace from response text (content between reasoning tags).

    Returns the reasoning text, or empty string if no reasoning tags are present.
    """
    if not text:
        return ""
    match = _REASONING_RE.search(text)
    return match.group(1).strip() if match else ""


def _genai_extract_text_from_response(data: dict) -> str:
    """Extract the raw generated text from Meta API response (dialog_candidates path).

    Returns the full text including any reasoning tags — callers are
    responsible for stripping reasoning when needed (e.g. for UI display).
    """
    if not data or "dialog_candidates" not in data or not data["dialog_candidates"]:
        return ""
    cand = data["dialog_candidates"][0]
    dialog = cand.get("dialog") or {}
    messages = dialog.get("messages") or []
    if not messages:
        return ""
    last_msg = messages[-1]
    contents = last_msg.get("contents") or []
    if not contents:
        return ""
    text_obj = contents[0].get("text") or {}
    return (text_obj.get("text") or "").strip()


def _genai_generate_one(
    model_name: str,
    dialog_id: str,
    prompt: str,
    genai_api_key: str,
    dialog_history: Optional[List[Tuple[str, str]]] = None,
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
    workstream: str = _GENAI_WORKSTREAM_OPALITE,
) -> dict:
    messages = _genai_build_messages_from_history(
        dialog_history,
        prompt,
        current_turn_handle_id=current_turn_handle_id,
        current_turn_mime=current_turn_mime,
        history_handle_ids=history_handle_ids,
    )
    raw = _genai_call_generation(
        model_name, dialog_id, messages, genai_api_key, workstream=workstream
    )
    full_text = _genai_extract_text_from_response(raw)
    return {
        "text": full_text,
        "reasoning": _extract_reasoning(full_text),
    }


def get_opalite_config(genai_api_key: str) -> dict:
    """Hit Meta config API with workstream='opalite' and return the raw router response."""
    return _genai_get_router_config(genai_api_key, workstream=_GENAI_WORKSTREAM_OPALITE)


def get_ophelia_config(genai_api_key: str) -> dict:
    """Hit Meta config API with workstream='ophelia' and return the raw router response."""
    return _genai_get_router_config(genai_api_key, workstream=_GENAI_WORKSTREAM_OPHELIA)


def generate_opalite(
    prompt: str,
    genai_api_key: str,
    *,
    dialog_id: Optional[str] = None,
    dialog_history: Optional[List[Tuple[str, str]]] = None,
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
) -> dict:
    """
    Generate an Opalite response via Meta GenAI using the 'opalite' workstream.

    Returns:
        {"response": str, "dialog_id": str, "model": "opalite"}
    """
    if not genai_api_key:
        raise ValueError("genai_api_key is required")
    prompt = (prompt or "").strip()
    if not prompt and not current_turn_handle_id:
        raise ValueError("prompt or current_turn_handle_id is required")

    if not dialog_id:
        dialog_id = get_genai_dialog_id(
            genai_api_key, workstream=_GENAI_WORKSTREAM_OPALITE
        )

    try:
        gen_result = _genai_generate_one(
            _GENAI_MODEL_RESPONSE_A,
            dialog_id,
            prompt,
            genai_api_key,
            dialog_history,
            current_turn_handle_id,
            current_turn_mime,
            history_handle_ids,
            workstream=_GENAI_WORKSTREAM_OPALITE,
        )
    except Exception as e:
        return {
            "response": "",
            "reasoning": "",
            "dialog_id": dialog_id,
            "model": _GENAI_MODEL_RESPONSE_A,
            "error": str(e),
        }

    return {
        "response": gen_result.get("text") or "",
        "reasoning": gen_result.get("reasoning") or "",
        "dialog_id": dialog_id,
        "model": _GENAI_MODEL_RESPONSE_A,
    }


def generate_ophelia(
    prompt: str,
    genai_api_key: str,
    *,
    dialog_id: Optional[str] = None,
    dialog_history: Optional[List[Tuple[str, str]]] = None,
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
) -> dict:
    """
    Generate an Ophelia response via Meta GenAI using the 'ophelia' workstream.

    Returns:
        {"response": str, "dialog_id": str, "model": "ophelia"}
    """
    if not genai_api_key:
        raise ValueError("genai_api_key is required")
    prompt = (prompt or "").strip()
    if not prompt and not current_turn_handle_id:
        raise ValueError("prompt or current_turn_handle_id is required")

    if not dialog_id:
        dialog_id = get_genai_dialog_id(
            genai_api_key, workstream=_GENAI_WORKSTREAM_OPHELIA
        )

    try:
        gen_result = _genai_generate_one(
            _GENAI_MODEL_RESPONSE_B,
            dialog_id,
            prompt,
            genai_api_key,
            dialog_history,
            current_turn_handle_id,
            current_turn_mime,
            history_handle_ids,
            workstream=_GENAI_WORKSTREAM_OPHELIA,
        )
    except Exception as e:
        return {
            "response": "",
            "reasoning": "",
            "dialog_id": dialog_id,
            "model": _GENAI_MODEL_RESPONSE_B,
            "error": str(e),
        }

    return {
        "response": gen_result.get("text") or "",
        "reasoning": gen_result.get("reasoning") or "",
        "dialog_id": dialog_id,
        "model": _GENAI_MODEL_RESPONSE_B,
    }


def generate_opalite_ophelia(
    prompt: str,
    genai_api_key: str,
    *,
    dialog_id: Optional[str] = None,
    opalite_dialog_id: Optional[str] = None,
    ophelia_dialog_id: Optional[str] = None,
    dialog_history: Optional[List[Tuple[str, str]]] = None,
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
) -> dict:
    """
    Generate Response A (opalite) and Response B (ophelia) in parallel via Meta GenAI.

    Each model hits its own workstream config and generation endpoint separately:
    opalite uses workstream='opalite', ophelia uses workstream='ophelia'.

    Args:
        dialog_id: Legacy shared dialog_id (ignored when per-model IDs are given).
        opalite_dialog_id: Dialog ID from the 'opalite' workstream config. Fetched if not provided.
        ophelia_dialog_id: Dialog ID from the 'ophelia' workstream config. Fetched if not provided.

    Returns:
        {
            "response_a": str,
            "response_b": str,
            "dialog_id": str,
            "model_a": "opalite",
            "model_b": "ophelia",
            "errors": dict (present only when errors occurred),
        }
    """
    if not genai_api_key:
        raise ValueError("genai_api_key is required")
    prompt = (prompt or "").strip()
    if not prompt and not current_turn_handle_id:
        raise ValueError("prompt or current_turn_handle_id is required")

    opa_did = opalite_dialog_id or dialog_id
    oph_did = ophelia_dialog_id or dialog_id

    common_kwargs = dict(
        dialog_history=dialog_history,
        current_turn_handle_id=current_turn_handle_id,
        current_turn_mime=current_turn_mime,
        history_handle_ids=history_handle_ids,
    )

    result = {
        "response_a": "",
        "response_b": "",
        "reasoning_a": "",
        "reasoning_b": "",
        "dialog_id": opa_did or oph_did or "",
        "model_a": _GENAI_MODEL_RESPONSE_A,
        "model_b": _GENAI_MODEL_RESPONSE_B,
    }
    errors = {}

    def run_opalite():
        return generate_opalite(
            prompt, genai_api_key, dialog_id=opa_did, **common_kwargs
        )

    def run_ophelia():
        return generate_ophelia(
            prompt, genai_api_key, dialog_id=oph_did, **common_kwargs
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_key = {
            executor.submit(run_opalite): "a",
            executor.submit(run_ophelia): "b",
        }
        for fut in as_completed(future_to_key):
            key = future_to_key[fut]
            try:
                res = fut.result()
                if key == "a":
                    result["response_a"] = res.get("response", "")
                    result["reasoning_a"] = res.get("reasoning", "")
                    if not result["dialog_id"]:
                        result["dialog_id"] = res.get("dialog_id", "")
                else:
                    result["response_b"] = res.get("response", "")
                    result["reasoning_b"] = res.get("reasoning", "")
                    if not result["dialog_id"]:
                        result["dialog_id"] = res.get("dialog_id", "")
                if res.get("error"):
                    errors[key] = res["error"]
            except Exception as e:
                errors[key] = str(e)
                if key == "a":
                    result["response_a"] = ""
                else:
                    result["response_b"] = ""

    if errors:
        result["errors"] = errors
    return result


def opalite_ophelia_generation_for_tasks_sync(
    genai_api_key: str,
    tasks: List[Dict[str, Any]],
    *,
    dialog_id: Optional[str] = None,
    opalite_dialog_id: Optional[str] = None,
    ophelia_dialog_id: Optional[str] = None,
    dialog_history: Optional[List[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """
    Sync per-task Opalite + Ophelia response generation (mirrors response_generation_for_tasks_sync).

    Each model uses its own workstream ('opalite' / 'ophelia') for config and generation.

    INPUT:
        tasks: List[dict], each {"task_id": <id>, "prompt": str}.
        genai_api_key: Meta GenAI access token.
        opalite_dialog_id / ophelia_dialog_id: Per-workstream dialog IDs. Fetched once if not provided.

    OUTPUT:
        List[dict], each:
            {"task_id", "prompt", "opalite_response", "ophelia_response", "dialog_id", "errors" (optional)}
    """
    if not tasks:
        return []
    if not opalite_dialog_id:
        opalite_dialog_id = dialog_id or get_genai_dialog_id(
            genai_api_key, workstream=_GENAI_WORKSTREAM_OPALITE
        )
    if not ophelia_dialog_id:
        ophelia_dialog_id = dialog_id or get_genai_dialog_id(
            genai_api_key, workstream=_GENAI_WORKSTREAM_OPHELIA
        )

    out = []
    for t in tasks:
        prompt = t.get("prompt", "")
        try:
            result = generate_opalite_ophelia(
                prompt,
                genai_api_key,
                opalite_dialog_id=opalite_dialog_id,
                ophelia_dialog_id=ophelia_dialog_id,
                dialog_history=dialog_history,
            )
            entry = {
                "task_id": _task_id(t),
                "prompt": prompt,
                "opalite_response": result.get("response_a", ""),
                "ophelia_response": result.get("response_b", ""),
                "opalite_reasoning": result.get("reasoning_a", ""),
                "ophelia_reasoning": result.get("reasoning_b", ""),
                "dialog_id": result.get("dialog_id", opalite_dialog_id),
            }
            if result.get("errors"):
                entry["errors"] = result["errors"]
            out.append(entry)
        except Exception as e:
            out.append(
                {
                    "task_id": _task_id(t),
                    "prompt": prompt,
                    "opalite_response": "",
                    "ophelia_response": "",
                    "opalite_reasoning": "",
                    "ophelia_reasoning": "",
                    "dialog_id": opalite_dialog_id,
                    "errors": {"generation": str(e)},
                }
            )
    return out


def run_opalite_ophelia_generation_for_tasks(
    genai_api_key: str,
    tasks: List[Dict[str, Any]],
    *,
    dialog_id: Optional[str] = None,
    opalite_dialog_id: Optional[str] = None,
    ophelia_dialog_id: Optional[str] = None,
    dialog_history: Optional[List[Tuple[str, str]]] = None,
    max_workers: int = 8,
) -> List[Dict[str, Any]]:
    """
    Parallel Opalite + Ophelia response generation for a batch of tasks.

    Each model uses its own workstream ('opalite' / 'ophelia') for config and generation.
    Tasks are dispatched in a thread pool for throughput.

    INPUT:
        tasks: List[dict], each {"task_id": <id>, "prompt": str}.
        genai_api_key: Meta GenAI access token.
        opalite_dialog_id / ophelia_dialog_id: Per-workstream dialog IDs. Fetched once if not provided.
        max_workers: Max concurrent tasks (default 8). Each task runs 2 API calls internally.

    OUTPUT:
        List[dict], each:
            {"task_id", "prompt", "opalite_response", "ophelia_response", "dialog_id", "errors" (optional)}
    """
    if not tasks:
        return []
    if not opalite_dialog_id:
        opalite_dialog_id = dialog_id or get_genai_dialog_id(
            genai_api_key, workstream=_GENAI_WORKSTREAM_OPALITE
        )
    if not ophelia_dialog_id:
        ophelia_dialog_id = dialog_id or get_genai_dialog_id(
            genai_api_key, workstream=_GENAI_WORKSTREAM_OPHELIA
        )

    results: List[Optional[Dict[str, Any]]] = [None] * len(tasks)

    def _generate_task(idx: int, t: Dict[str, Any]):
        prompt = t.get("prompt", "")
        try:
            gen = generate_opalite_ophelia(
                prompt,
                genai_api_key,
                opalite_dialog_id=opalite_dialog_id,
                ophelia_dialog_id=ophelia_dialog_id,
                dialog_history=dialog_history,
            )
            entry = {
                "task_id": _task_id(t),
                "prompt": prompt,
                "opalite_response": gen.get("response_a", ""),
                "ophelia_response": gen.get("response_b", ""),
                "opalite_reasoning": gen.get("reasoning_a", ""),
                "ophelia_reasoning": gen.get("reasoning_b", ""),
                "dialog_id": gen.get("dialog_id", opalite_dialog_id),
            }
            if gen.get("errors"):
                entry["errors"] = gen["errors"]
            results[idx] = entry
        except Exception as e:
            results[idx] = {
                "task_id": _task_id(t),
                "prompt": prompt,
                "opalite_response": "",
                "ophelia_response": "",
                "opalite_reasoning": "",
                "ophelia_reasoning": "",
                "dialog_id": opalite_dialog_id,
                "errors": {"generation": str(e)},
            }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_generate_task, i, t) for i, t in enumerate(tasks)]
        for f in futures:
            f.result()

    return [r for r in results if r is not None]


def batch_create_and_rate_rubrics(
    api_key: str,
    tasks: List[Dict[str, Any]],
    model: str = "gemini-3.1-pro-preview",
    temperature: Optional[float] = 0.2,
    max_workers: int = 8,
) -> List[Dict[str, Any]]:
    """
    Batch create rubrics and rate all 4 responses for multiple tasks in parallel.

    INPUT:
        tasks: List[dict], each with:
            - task_id: identifier
            - original_prompt: the user's raw prompt
            - enhanced_prompt: the prompt after enhancement
            - opalite_response: Opalite model response
            - ophelia_response: Ophelia model response
            - gemini_response: Gemini 3 Pro response
            - gpt_response: GPT 5.2 response

    OUTPUT:
        List[dict], each:
            {
                "task_id", "rubrics": [...], "rubric_ratings": [...],
                "usage": dict, "error": str | None
            }
    """
    if not tasks:
        return []
    results: List[Optional[Dict[str, Any]]] = [None] * len(tasks)

    def _run(idx: int, t: Dict[str, Any]):
        task_id = _task_id(t)
        try:
            out = create_and_rate_rubrics_sync(
                api_key,
                t.get("original_prompt", ""),
                t.get("enhanced_prompt", ""),
                t.get("opalite_response", ""),
                t.get("ophelia_response", ""),
                t.get("gemini_response", ""),
                t.get("gpt_response", ""),
                model=model,
                temperature=temperature,
            )
            out["task_id"] = task_id
            results[idx] = out
        except Exception as e:
            results[idx] = {
                "task_id": task_id,
                "rubrics": [],
                "rubric_ratings": [],
                "usage": _empty_usage(),
                "error": str(e),
            }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_run, i, t) for i, t in enumerate(tasks)]
        for f in futures:
            f.result()

    return [r for r in results if r is not None]


def batch_create_and_rate_rubrics_kimi(
    kimi_api_key: str,
    tasks: List[Dict[str, Any]],
    model: str = None,
    temperature: Optional[float] = 0.2,
    max_workers: int = 8,
) -> List[Dict[str, Any]]:
    """Same as batch_create_and_rate_rubrics but uses Kimi via Bedrock."""
    if model is None:
        model = DEFAULT_KIMI_MODEL
    if not tasks:
        return []
    results: List[Optional[Dict[str, Any]]] = [None] * len(tasks)

    def _run(idx: int, t: Dict[str, Any]):
        task_id = _task_id(t)
        try:
            out = create_and_rate_rubrics_sync_kimi(
                kimi_api_key,
                t.get("original_prompt", ""),
                t.get("enhanced_prompt", ""),
                t.get("opalite_response", ""),
                t.get("ophelia_response", ""),
                t.get("gemini_response", ""),
                t.get("gpt_response", ""),
                model=model,
                temperature=temperature,
            )
            out["task_id"] = task_id
            results[idx] = out
        except Exception as e:
            results[idx] = {
                "task_id": task_id,
                "rubrics": [],
                "rubric_ratings": [],
                "usage": _empty_usage(),
                "error": str(e),
            }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_run, i, t) for i, t in enumerate(tasks)]
        for f in futures:
            f.result()

    return [r for r in results if r is not None]


def _build_evaluation_result_from_dimensions(
    dimension_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge 5 per-dimension parsed responses into evaluation_result with overall_quality (same schema as EVALUATE_TWO)."""
    response_a: Dict[str, Any] = {}
    response_b: Dict[str, Any] = {}
    for i, key in enumerate(DIMENSION_KEYS):
        pr = dimension_results[i] if i < len(dimension_results) else {}
        response_a[key] = (pr.get("response_a") or {}).get(key) or {}
        response_b[key] = (pr.get("response_b") or {}).get(key) or {}

    def _weighted(d: Dict[str, Any]) -> float:
        s = 0.0
        for k in DIMENSION_KEYS:
            score = (d.get(k) or {}).get("score")
            if score is not None:
                s += float(score) * DIMENSION_WEIGHTS[k]
        return s

    wa = _weighted(response_a)
    wb = _weighted(response_b)
    response_a["overall_quality"] = {
        "weighted_score": round(wa, 2),
        "reason": f"Weighted: IF×0.25 + Truth×0.25 + Correctness×0.20 + Writing×0.15 + Verbosity×0.15 = {wa:.2f}",
    }
    response_b["overall_quality"] = {
        "weighted_score": round(wb, 2),
        "reason": f"Weighted: IF×0.25 + Truth×0.25 + Correctness×0.20 + Writing×0.15 + Verbosity×0.15 = {wb:.2f}",
    }
    return {"response_a": response_a, "response_b": response_b}


def evaluation_for_tasks_sync(
    evaluation_inputs: List[Dict[str, Any]],
    evaluation_model: str = "gemini-3.1-pro-preview",
) -> List[Dict[str, Any]]:
    """
    Step 3 (sync): per-dimension evaluation (5 calls) + COMPARE_TWO + COMPARE_EXTERNAL x2 + CREATE_RUBRICS_EXTERNAL x2.
    Each dimension is evaluated in a separate API call, then merged into the same evaluation_result schema.
    Output structure unchanged: evaluation_result, comparison_ab, comparison_vs_gemini, comparison_vs_gpt, rubrics_vs_gemini, rubrics_vs_gpt, sxs_winner_label.
    """
    if not evaluation_inputs:
        return []
    total_gemini = _empty_usage()
    out = []

    def _parse(t: str) -> Dict[str, Any]:
        try:
            return json.loads(t) if t else {}
        except json.JSONDecodeError:
            return {}

    max_workers = 6  # wave 1: 5 dimensions + COMPARE_TWO; wave 2: 4 calls
    for idx, inp in enumerate(evaluation_inputs):
        task_gemini = _empty_usage()
        prompt = inp.get("prompt", "")
        resp_a = inp.get("response_a", "")
        resp_b = inp.get("response_b", "")
        gemini_resp = inp.get("gemini_response", "")
        gpt_resp = inp.get("gpt_response", "")

        # Wave 1 (parallel): 5 per-dimension evals + COMPARE_TWO — no cross-dependencies
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures_w1 = []
            for dim_key in DIMENSION_KEYS:
                user_dim = (
                    f"Dimension: {dim_key}\n\n"
                    f"USER PROMPT:\n{prompt}\n\n"
                    "--------------------\nRESPONSE A:\n"
                    f"{resp_a}\n\n"
                    "--------------------\nRESPONSE B:\n"
                    f"{resp_b}"
                )
                futures_w1.append(
                    executor.submit(
                        call_gemini_sync,
                        evaluation_model,
                        get_evaluation_system_prompt_for_dimension(dim_key),
                        user_dim,
                        response_format="json",
                        temperature=0.2,
                    )
                )
            user_compare = (
                "TASK: COMPARE_TWO\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nRESPONSE A:\n"
                f"{resp_a}\n\n"
                "--------------------\nRESPONSE B:\n"
                f"{resp_b}"
            )
            futures_w1.append(
                executor.submit(
                    call_gemini_sync,
                    evaluation_model,
                    EVALUATION_SYSTEM_PROMPT_COMPARE_TWO,
                    user_compare,
                    response_format="json",
                    temperature=0.2,
                )
            )
            results_w1 = [f.result() for f in futures_w1]

        for r in results_w1:
            _add_usage(task_gemini, r["usage"])
            _add_usage(total_gemini, r["usage"])
        dimension_results = [_parse(r["text"]) for r in results_w1[:5]]
        evaluation_result = _build_evaluation_result_from_dimensions(dimension_results)
        parsed_compare = _parse(results_w1[5]["text"])
        comparison_ab = {
            "comparison_score": parsed_compare.get("comparison_score", 0),
            "overall_comment": parsed_compare.get("overall_comment", ""),
        }
        comp_score = comparison_ab.get("comparison_score", 0)
        sxs_winner_label = "Response A" if comp_score <= 0 else "Response B"
        sxs_winner_text = resp_a if comp_score <= 0 else resp_b

        # Wave 2 (parallel): COMPARE_EXTERNAL x2 + CREATE_RUBRICS_EXTERNAL x2 — depend only on SxS winner
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            user_ext_gemini = (
                "TASK: COMPARE_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                f"{gemini_resp}\n\n"
                "EXTERNAL MODEL NAME: Gemini 3 Pro"
            )
            user_ext_gpt = (
                "TASK: COMPARE_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                f"{gpt_resp}\n\n"
                "EXTERNAL MODEL NAME: GPT 5.2"
            )
            user_rub_g = (
                "TASK: CREATE_RUBRICS_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                f"{gemini_resp}\n\n"
                "EXTERNAL MODEL NAME: Gemini 3 Pro"
            )
            user_rub_pt = (
                "TASK: CREATE_RUBRICS_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                f"{gpt_resp}\n\n"
                "EXTERNAL MODEL NAME: GPT 5.2"
            )
            f_ext_g = executor.submit(
                call_gemini_sync,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                user_ext_gemini,
                response_format="json",
                temperature=0.2,
            )
            f_ext_p = executor.submit(
                call_gemini_sync,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                user_ext_gpt,
                response_format="json",
                temperature=0.2,
            )
            f_rub_g = executor.submit(
                call_gemini_sync,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                user_rub_g,
                response_format="json",
                temperature=0.2,
            )
            f_rub_pt = executor.submit(
                call_gemini_sync,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                user_rub_pt,
                response_format="json",
                temperature=0.2,
            )
            result_ext_g = f_ext_g.result()
            result_ext_p = f_ext_p.result()
            result_rub_g = f_rub_g.result()
            result_rub_pt = f_rub_pt.result()

        for r in (result_ext_g, result_ext_p, result_rub_g, result_rub_pt):
            _add_usage(task_gemini, r["usage"])
            _add_usage(total_gemini, r["usage"])
        parsed_ext_g = _parse(result_ext_g["text"])
        parsed_ext_pt = _parse(result_ext_p["text"])
        parsed_rub_g = _parse(result_rub_g["text"])
        parsed_rub_pt = _parse(result_rub_pt["text"])
        comparison_vs_gemini = {
            "comparison_score": parsed_ext_g.get("comparison_score"),
            "comparison_comment": parsed_ext_g.get("comparison_comment", ""),
        }
        comparison_vs_gpt = {
            "comparison_score": parsed_ext_pt.get("comparison_score"),
            "comparison_comment": parsed_ext_pt.get("comparison_comment", ""),
        }
        rubrics_vs_gemini = {"rubrics": parsed_rub_g.get("rubrics", [])}
        rubrics_vs_gpt = {"rubrics": parsed_rub_pt.get("rubrics", [])}

        out.append(
            {
                "task_id": _task_id(inp),
                "prompt": prompt,
                "evaluation_result": evaluation_result,
                "comparison_ab": comparison_ab,
                "comparison_vs_gemini": comparison_vs_gemini,
                "comparison_vs_gpt": comparison_vs_gpt,
                "rubrics_vs_gemini": rubrics_vs_gemini.get("rubrics", {}),
                "rubrics_vs_gpt": rubrics_vs_gpt.get("rubrics", {}),
                "sxs_winner_label": sxs_winner_label,
                "token_usage": calculate_cost(task_gemini, _empty_usage()),
            }
        )

    cumulative = calculate_cost(total_gemini, _empty_usage())
    for d in out:
        d["token_usage_cumulative"] = cumulative
    return out


def evaluation_for_tasks_sync_kimi(
    kimi_api_key: str,
    evaluation_inputs: List[Dict[str, Any]],
    evaluation_model: str = DEFAULT_KIMI_MODEL,
) -> List[Dict[str, Any]]:
    """
    Step 3 (Kimi sync): same as evaluation_for_tasks_sync but using Kimi for all evaluation calls.
    Output structure unchanged: evaluation_result, comparison_ab, comparison_vs_gemini, comparison_vs_gpt, rubrics_vs_gemini, rubrics_vs_gpt, sxs_winner_label.
    """
    if not evaluation_inputs:
        return []
    total_kimi = _empty_usage()
    out = []

    def _parse(t: str) -> Dict[str, Any]:
        return _parse_json_from_response(t)

    max_workers = 4
    for idx, inp in enumerate(evaluation_inputs):
        task_usage = _empty_usage()
        prompt = inp.get("prompt", "")
        resp_a = inp.get("response_a", "")
        resp_b = inp.get("response_b", "")
        gemini_resp = inp.get("gemini_response", "")
        gpt_resp = inp.get("gpt_response", "")

        # Wave 1: single all-dimensions call + COMPARE_TWO (2 parallel calls)
        user_all_dims = (
            f"USER PROMPT:\n{prompt}\n\n"
            "--------------------\nRESPONSE A:\n"
            f"{resp_a}\n\n"
            "--------------------\nRESPONSE B:\n"
            f"{resp_b}"
        )
        user_compare = (
            "TASK: COMPARE_TWO\n\n"
            f"USER PROMPT:\n{prompt}\n\n"
            "--------------------\nRESPONSE A:\n"
            f"{resp_a}\n\n"
            "--------------------\nRESPONSE B:\n"
            f"{resp_b}"
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_dims = executor.submit(
                call_kimi_sync,
                kimi_api_key,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_EVALUATE_TWO,
                user_all_dims,
                response_format="json",
                temperature=0.2,
            )
            future_compare = executor.submit(
                call_kimi_sync,
                kimi_api_key,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_COMPARE_TWO,
                user_compare,
                response_format="json",
                temperature=0.2,
            )
            r_dims = future_dims.result()
            r_compare = future_compare.result()

        for r in (r_dims, r_compare):
            _add_usage(task_usage, r["usage"])
            _add_usage(total_kimi, r["usage"])
        full_parsed = _parse(r_dims["text"])
        ra = full_parsed.get("response_a") or {}
        rb = full_parsed.get("response_b") or {}
        dimension_results = [
            {"response_a": {dk: ra.get(dk, {})}, "response_b": {dk: rb.get(dk, {})}}
            for dk in DIMENSION_KEYS
        ]
        evaluation_result = _build_evaluation_result_from_dimensions(dimension_results)
        parsed_compare = _parse(r_compare["text"])
        comparison_ab = {
            "comparison_score": parsed_compare.get("comparison_score", 0),
            "overall_comment": parsed_compare.get("overall_comment", ""),
        }
        comp_score = comparison_ab.get("comparison_score", 0)
        sxs_winner_label = "Response A" if comp_score <= 0 else "Response B"
        sxs_winner_text = resp_a if comp_score <= 0 else resp_b

        # Wave 2: COMPARE_EXTERNAL x2 + CREATE_RUBRICS_EXTERNAL x2
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            user_ext_gemini = (
                "TASK: COMPARE_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                f"{gemini_resp}\n\n"
                "EXTERNAL MODEL NAME: Gemini 3 Pro"
            )
            user_ext_gpt = (
                "TASK: COMPARE_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                f"{gpt_resp}\n\n"
                "EXTERNAL MODEL NAME: GPT 5.2"
            )
            user_rub_g = (
                "TASK: CREATE_RUBRICS_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                f"{gemini_resp}\n\n"
                "EXTERNAL MODEL NAME: Gemini 3 Pro"
            )
            user_rub_pt = (
                "TASK: CREATE_RUBRICS_EXTERNAL\n\n"
                f"USER PROMPT:\n{prompt}\n\n"
                "--------------------\nSXS WINNER RESPONSE:\n"
                f"{sxs_winner_text}\n\n"
                "--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                f"{gpt_resp}\n\n"
                "EXTERNAL MODEL NAME: GPT 5.2"
            )
            f_ext_g = executor.submit(
                call_kimi_sync,
                kimi_api_key,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                user_ext_gemini,
                response_format="json",
                temperature=0.2,
            )
            f_ext_p = executor.submit(
                call_kimi_sync,
                kimi_api_key,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                user_ext_gpt,
                response_format="json",
                temperature=0.2,
            )
            f_rub_g = executor.submit(
                call_kimi_sync,
                kimi_api_key,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                user_rub_g,
                response_format="json",
                temperature=0.2,
            )
            f_rub_pt = executor.submit(
                call_kimi_sync,
                kimi_api_key,
                evaluation_model,
                EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                user_rub_pt,
                response_format="json",
                temperature=0.2,
            )
            result_ext_g = f_ext_g.result()
            result_ext_p = f_ext_p.result()
            result_rub_g = f_rub_g.result()
            result_rub_pt = f_rub_pt.result()

        for r in (result_ext_g, result_ext_p, result_rub_g, result_rub_pt):
            _add_usage(task_usage, r["usage"])
            _add_usage(total_kimi, r["usage"])
        parsed_ext_g = _parse(result_ext_g["text"])
        parsed_ext_pt = _parse(result_ext_p["text"])
        parsed_rub_g = _parse(result_rub_g["text"])
        parsed_rub_pt = _parse(result_rub_pt["text"])
        comparison_vs_gemini = {
            "comparison_score": parsed_ext_g.get("comparison_score"),
            "comparison_comment": parsed_ext_g.get("comparison_comment", ""),
        }
        comparison_vs_gpt = {
            "comparison_score": parsed_ext_pt.get("comparison_score"),
            "comparison_comment": parsed_ext_pt.get("comparison_comment", ""),
        }
        rubrics_vs_gemini = {"rubrics": parsed_rub_g.get("rubrics", [])}
        rubrics_vs_gpt = {"rubrics": parsed_rub_pt.get("rubrics", [])}

        out.append(
            {
                "task_id": _task_id(inp),
                "prompt": prompt,
                "evaluation_result": evaluation_result,
                "comparison_ab": comparison_ab,
                "comparison_vs_gemini": comparison_vs_gemini,
                "comparison_vs_gpt": comparison_vs_gpt,
                "rubrics_vs_gemini": rubrics_vs_gemini.get("rubrics", {}),
                "rubrics_vs_gpt": rubrics_vs_gpt.get("rubrics", {}),
                "sxs_winner_label": sxs_winner_label,
                "token_usage": calculate_cost(
                    _empty_usage(), _empty_usage(), task_usage
                ),
            }
        )

    cumulative = calculate_cost(_empty_usage(), _empty_usage(), total_kimi)
    for d in out:
        d["token_usage_cumulative"] = cumulative
    return out


def _gemini_build_batch_requests(
    requests_config: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build list of InlinedRequest items per API: request (GenerateContentRequest) + metadata.key.

    Note: Gemini automatically applies implicit caching for repeated identical prefixes.
    """
    inlined = []
    for i, req in enumerate(requests_config):
        key = req.get("key", f"req-{i}")
        system = req.get("system_prompt", "")
        user = req.get("user_content", "")
        content = {"role": "user", "parts": [{"text": user}]}
        gen_config = {}
        if req.get("response_format") == "json":
            gen_config["responseMimeType"] = "application/json"
        if req.get("temperature") is not None:
            gen_config["temperature"] = req["temperature"]
        if req.get("thinking_config") is not None:
            gen_config["thinkingConfig"] = req["thinking_config"]
        request_body = {
            "contents": [content],
            "generationConfig": gen_config,
        }
        if system:
            request_body["systemInstruction"] = {"parts": [{"text": system}]}
        inlined.append(
            {
                "request": request_body,
                "metadata": {"key": key},
            }
        )
    return inlined


def _gemini_batch_create(
    model: str,
    display_name: str,
    requests_config: List[Dict[str, Any]],
    use_file_for_large: bool = True,
) -> str:
    """
    Create a Gemini batch job per https://ai.google.dev/gemini-api/docs/batch-api.
    Auth is handled via get_vertex_api_key().
    """
    api_key = get_vertex_api_key()
    model = model if model.startswith("models/") else f"models/{model}"
    inlined = _gemini_build_batch_requests(requests_config)
    n = len(inlined)

    if use_file_for_large and n > INLINE_BATCH_MAX_REQUESTS:
        return _gemini_batch_create_from_file(model, display_name, inlined)

    # Inline: batch with input_config.requests.requests[] (per API reference)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/{model}:batchGenerateContent"
    )
    headers = {"Content-Type": "application/json"}
    params = {"key": api_key}
    payload = {
        "batch": {
            "displayName": display_name,
            "inputConfig": {
                "requests": {"requests": inlined},
            },
        },
    }
    resp = requests.post(url, headers=headers, params=params, json=payload)
    resp.raise_for_status()
    data = resp.json()
    # Log creation response for diagnostics
    log_verbose = os.environ.get("GEMINI_BATCH_VERBOSE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    log_progress = os.environ.get("GEMINI_BATCH_LOG_PROGRESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    batch_name = _gemini_batch_name_from_response(data)
    if log_progress:
        initial_state = (
            data.get("state") or (data.get("metadata") or {}).get("state") or "(none)"
        )
        print(
            f"  [Gemini batch] Created batch: {batch_name} initial_state={initial_state} requests={n}",
            flush=True,
        )
    if log_verbose:
        print(
            f"  [Gemini batch VERBOSE] Create response:\n{json.dumps(data, indent=2, default=str)[:1500]}",
            flush=True,
        )
    return batch_name


def _gemini_batch_name_from_response(data: Dict[str, Any]) -> str:
    """Extract batch name from create response (may be Operation or Batch)."""
    name = data.get("name") or (data.get("metadata") or {}).get("name")
    if not name and "batch" in data:
        name = data["batch"].get("name")
    if not name:
        raise ValueError(f"Unexpected Gemini batch create response: {data}")
    if not name.startswith("batches/"):
        name = (data.get("metadata") or {}).get("batch") or name
    return name


def _gemini_batch_create_from_file(
    model: str,
    display_name: str,
    inlined_requests: List[Dict[str, Any]],
) -> str:
    """
    Create batch using JSONL file input per docs.
    Auth is handled via get_vertex_api_key().
    """
    api_key = get_vertex_api_key()
    # Build JSONL: each line is {"key": ..., "request": {...}} (no "metadata" in file format per doc)
    log_verbose = os.environ.get("GEMINI_BATCH_VERBOSE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    log_progress = os.environ.get("GEMINI_BATCH_LOG_PROGRESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    lines = []
    for item in inlined_requests:
        key = (item.get("metadata") or {}).get("key", "")
        req = item.get("request", {})
        lines.append(json.dumps({"key": key, "request": req}))
    jsonl_content = "\n".join(lines)
    n_requests = len(lines)
    if log_verbose:
        print(
            f"  [Gemini batch VERBOSE] JSONL sample (first line):\n{lines[0][:500] if lines else '(empty)'}",
            flush=True,
        )
    file_name = _gemini_upload_file(
        api_key, jsonl_content.encode("utf-8"), "batch-input.jsonl", "application/jsonl"
    )
    if log_progress:
        print(
            f"  [Gemini batch] Uploaded JSONL file: {file_name} ({n_requests} requests, {len(jsonl_content)} bytes)",
            flush=True,
        )
    # Create batch with input_config.file_name (doc: input_config: { "file_name": "files/xxx" })
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/{model}:batchGenerateContent"
    )
    headers = {"Content-Type": "application/json"}
    params = {"key": api_key}
    payload = {
        "batch": {
            "displayName": display_name,
            "inputConfig": {
                "fileName": file_name,
            },
        },
    }
    resp = requests.post(url, headers=headers, params=params, json=payload)
    resp.raise_for_status()
    data = resp.json()
    batch_name = _gemini_batch_name_from_response(data)
    if log_progress:
        initial_state = (
            data.get("state") or (data.get("metadata") or {}).get("state") or "(none)"
        )
        print(
            f"  [Gemini batch] Created batch (file-based): {batch_name} initial_state={initial_state} requests={n_requests}",
            flush=True,
        )
    if log_verbose:
        print(
            f"  [Gemini batch VERBOSE] Create response:\n{json.dumps(data, indent=2, default=str)[:1500]}",
            flush=True,
        )
    return batch_name


def _gemini_upload_file(
    api_key: str,
    content: bytes,
    display_name: str,
    mime_type: str,
) -> str:
    """
    Upload a file to Gemini File API (resumable upload). Returns file name (e.g. files/xxx).
    See https://ai.google.dev/gemini-api/docs/files
    """
    # Start resumable upload
    start_url = "https://generativelanguage.googleapis.com/upload/v1beta/files"
    headers_start = {
        "x-goog-api-key": api_key,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(content)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "Content-Type": "application/json",
    }
    body_start = json.dumps({"file": {"display_name": display_name}})
    r = requests.post(start_url, headers=headers_start, data=body_start)
    r.raise_for_status()
    upload_url = r.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise ValueError("File API did not return X-Goog-Upload-URL")
    # Upload bytes
    headers_upload = {
        "Content-Length": str(len(content)),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    r2 = requests.post(upload_url, headers=headers_upload, data=content)
    r2.raise_for_status()
    info = r2.json()
    file_name = (info.get("file") or {}).get("name")
    if not file_name:
        raise ValueError(f"File API response missing file.name: {info}")
    return file_name


# Terminal states per https://ai.google.dev/gemini-api/docs/batch-api (JOB_STATE_* in SDK)
# and https://ai.google.dev/api/batch-mode (BatchState: BATCH_STATE_*)
_GEMINI_BATCH_TERMINAL_STATES = {
    "BATCH_STATE_SUCCEEDED",
    "BATCH_STATE_FAILED",
    "BATCH_STATE_CANCELLED",
    "BATCH_STATE_EXPIRED",
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}
_GEMINI_BATCH_SUCCESS_STATES = {"BATCH_STATE_SUCCEEDED", "JOB_STATE_SUCCEEDED"}


def _gemini_batch_state(batch_obj: Dict[str, Any]) -> str:
    """Resolve batch state from Operation (GET batches/xxx returns Operation; state may be in metadata or response)."""
    data = batch_obj or {}
    resolved = data.get("response") if data.get("response") is not None else data
    resolved = resolved if resolved is not None else data
    return (
        (resolved or {}).get("state", "")
        or data.get("state", "")
        or (data.get("metadata") or {}).get("state", "")
    )


def _gemini_batch_poll(
    batch_name: str,
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    log_progress: Optional[bool] = None,
    verbose_diagnostics: Optional[bool] = None,
) -> Dict[str, Any]:
    """Poll until batch is in a terminal state. Returns batch object.
    Auth is handled via get_vertex_api_key().
    """
    api_key = get_vertex_api_key()
    if log_progress is None:
        log_progress = os.environ.get(
            "GEMINI_BATCH_LOG_PROGRESS", ""
        ).strip().lower() in ("1", "true", "yes")
    if verbose_diagnostics is None:
        verbose_diagnostics = os.environ.get(
            "GEMINI_BATCH_VERBOSE", ""
        ).strip().lower() in ("1", "true", "yes")
    url = f"https://generativelanguage.googleapis.com/v1beta/{batch_name}"
    params = {"key": api_key}
    start = time.time()
    poll_count = 0
    while True:
        poll_count += 1
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        # Resolve batch from Operation: GET may return Operation with response containing the batch
        batch = data.get("response") if data.get("response") is not None else data
        batch = batch if batch is not None else data
        state = (
            (batch or {}).get("state", "")
            or data.get("state", "")
            or (data.get("metadata") or {}).get("state", "")
        )
        elapsed = int(time.time() - start)
        # Extract additional diagnostic info
        done_flag = data.get("done")
        error_info = data.get("error") or batch.get("error")
        metadata = data.get("metadata", {})
        # Progress info if available
        total_requests = batch.get("totalRequests") or metadata.get("totalRequests")
        completed_requests = batch.get("completedRequests") or metadata.get(
            "completedRequests"
        )
        failed_requests = batch.get("failedRequests") or metadata.get("failedRequests")

        if log_progress:
            progress_str = ""
            if total_requests:
                progress_str = f" progress={completed_requests or 0}/{total_requests}"
                if failed_requests:
                    progress_str += f" (failed={failed_requests})"
            print(
                f"  [Gemini batch] poll #{poll_count}: state={state or '(none)'} done={done_flag}{progress_str} (elapsed {elapsed}s)",
                flush=True,
            )

        if verbose_diagnostics:
            print(
                f"  [Gemini batch VERBOSE] Full API response keys: {list(data.keys())}",
                flush=True,
            )
            print(f"  [Gemini batch VERBOSE] batch_name={batch_name}", flush=True)
            print(
                f"  [Gemini batch VERBOSE] state={state!r} done={done_flag}", flush=True
            )
            if error_info:
                print(
                    f"  [Gemini batch VERBOSE] ERROR: {json.dumps(error_info, indent=2)}",
                    flush=True,
                )
            if metadata:
                print(
                    f"  [Gemini batch VERBOSE] metadata: {json.dumps(metadata, indent=2, default=str)}",
                    flush=True,
                )
            # On first poll or every 5th poll, show full response structure
            if poll_count == 1 or poll_count % 5 == 0:
                print(
                    f"  [Gemini batch VERBOSE] Full response:\n{json.dumps(data, indent=2, default=str)[:2000]}",
                    flush=True,
                )

        if error_info and log_progress:
            print(
                f"  [Gemini batch] WARNING: Error in response: {error_info}", flush=True
            )

        if state in _GEMINI_BATCH_TERMINAL_STATES:
            if log_progress:
                print(
                    f"  [Gemini batch] Reached terminal state: {state} after {poll_count} polls ({elapsed}s)",
                    flush=True,
                )
            return data
        if timeout and (time.time() - start) > timeout:
            raise TimeoutError(
                f"Batch {batch_name} did not complete within {timeout}s (last state={state!r}, polls={poll_count})"
            )
        time.sleep(poll_interval)


def _gemini_batch_get_results(
    batch_obj: Dict[str, Any], api_key: str
) -> List[Dict[str, Any]]:
    """
    Parse batch result per API: output.inlinedResponses (inline) or output.responsesFile (file).
    Handles both batch object and Operation (response may contain batch); supports dest (SDK) or output.
    """
    # Resolve batch: GET may return batch directly or Operation with response
    batch = batch_obj
    if batch.get("done") and "response" in batch:
        batch = batch.get("response") or batch
    # When Operation.response is GenerateContentBatchOutput, inlinedResponses is at top level (no "output" key)
    out = batch.get("output") or batch.get("dest") or batch.get("result") or batch
    # inlinedResponses: may be array or object with inlinedResponses[] (per API schema)
    inlined = out.get("inlinedResponses") or out.get("inlined_responses")
    if isinstance(inlined, dict):
        inlined = (
            inlined.get("inlinedResponses") or inlined.get("inlined_responses") or []
        )
    if not inlined:
        inlined = []
    results = []
    if inlined:
        for item in inlined:
            key = (item.get("metadata") or {}).get("key", "")
            err = item.get("error")
            if err:
                results.append(
                    {"key": key, "response_text": None, "error": err, "parsed": None}
                )
                continue
            resp = item.get("response") or {}
            parts = (
                (resp.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            )
            text = (parts[0].get("text", "").strip()) if parts else ""
            parsed = None
            try:
                parsed = json.loads(text) if text else None
            except json.JSONDecodeError:
                pass
            results.append(
                {"key": key, "response_text": text, "error": None, "parsed": parsed}
            )
        return results
    file_name = (
        out.get("responsesFile") or out.get("responses_file") or out.get("fileName")
    )
    if file_name:
        download_url = f"https://generativelanguage.googleapis.com/download/v1beta/{file_name}:download?alt=media"
        r = requests.get(
            download_url, params={"key": api_key}, timeout=GEMINI_BATCH_DOWNLOAD_TIMEOUT
        )
        r.raise_for_status()
        for line in r.text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (row.get("metadata") or {}).get("key", "")
            err = row.get("error")
            if err:
                results.append(
                    {"key": key, "response_text": None, "error": err, "parsed": None}
                )
                continue
            resp = row.get("response", {})
            parts = (
                (resp.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            )
            text = (parts[0].get("text", "").strip()) if parts else ""
            parsed = None
            try:
                parsed = json.loads(text) if text else None
            except json.JSONDecodeError:
                pass
            results.append(
                {"key": key, "response_text": text, "error": None, "parsed": parsed}
            )
    return results


# =============================================================================
# OPENAI BATCH API HELPERS
# See: https://platform.openai.com/docs/guides/batch
# API reference: https://platform.openai.com/docs/api-reference/batch
# Limits: up to 50,000 requests per batch, input file up to 200 MB.
# =============================================================================

OPENAI_BATCH_ENDPOINT_RESPONSES = "/v1/responses"
OPENAI_BATCH_COMPLETION_WINDOW = "24h"
OPENAI_BATCH_MAX_REQUESTS = 50000
OPENAI_BATCH_MAX_FILE_MB = 200


def _openai_upload_batch_file(
    api_key: str,
    requests_body: List[Dict[str, Any]],
    timeout: int = 120,
) -> str:
    """
    Prepare and upload batch input file per https://platform.openai.com/docs/guides/batch.

    Batch file is .jsonl where each line: {"custom_id", "method": "POST", "url", "body"}.
    body uses Responses API shape (model, input, max_output_tokens, etc.).
    Upload via Files API with purpose="batch". Returns file id (e.g. file-abc123).
    """
    lines = []
    for req in requests_body:
        custom_id = req.get("custom_id", str(len(lines)))
        body = req.get("body", req)
        line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": OPENAI_BATCH_ENDPOINT_RESPONSES,
            "body": body,
        }
        lines.append(json.dumps(line))
    content = "\n".join(lines)
    url = "https://api.openai.com/v1/files"
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {
        "file": (
            "batch_input.jsonl",
            io.BytesIO(content.encode("utf-8")),
            "application/jsonl",
        )
    }
    data = {"purpose": "batch"}
    resp = requests.post(url, headers=headers, data=data, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["id"]


def _openai_batch_create(
    api_key: str,
    input_file_id: str,
    endpoint: str = OPENAI_BATCH_ENDPOINT_RESPONSES,
    completion_window: str = OPENAI_BATCH_COMPLETION_WINDOW,
    metadata: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
) -> str:
    """
    Create a batch per guide: input_file_id, endpoint, completion_window ("24h" only).
    Returns batch id (e.g. batch_abc123).
    """
    url = "https://api.openai.com/v1/batches"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "input_file_id": input_file_id,
        "endpoint": endpoint,
        "completion_window": completion_window,
    }
    if metadata is not None:
        payload["metadata"] = metadata
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["id"]


# Status per guide: validating, failed, in_progress, finalizing, completed, expired, cancelling, cancelled
_OPENAI_BATCH_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}


def _openai_batch_poll(
    api_key: str,
    batch_id: str,
    poll_interval: int = 30,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Poll until batch reaches a terminal status (completed, failed, expired, cancelled).
    Returns Batch object with output_file_id, error_file_id, request_counts, etc.
    """
    url = f"https://api.openai.com/v1/batches/{batch_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.time()
    while True:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        batch = resp.json()
        status = batch.get("status", "")
        if status in _OPENAI_BATCH_TERMINAL_STATUSES:
            return batch
        if timeout and (time.time() - start) > timeout:
            raise TimeoutError(f"Batch {batch_id} did not complete within {timeout}s")
        time.sleep(poll_interval)


def _openai_batch_download_results(
    api_key: str,
    output_file_id: Optional[str],
    error_file_id: Optional[str] = None,
    timeout: int = 1200,
) -> List[Dict[str, Any]]:
    """
    Retrieve batch results per guide.

    Output file: one line per successful request; each line has id, custom_id, response {status_code, request_id, body}, error (null).
    Error file: one line per failed request; response is null, error has code and message.
    Output order may not match input order; use custom_id to map. Results are merged from both
    files and returned as list of {custom_id, response, error}; order follows output file then error file.
    """
    by_custom_id = {}
    if output_file_id:
        url = f"https://api.openai.com/v1/files/{output_file_id}/content"
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            custom_id = row.get("custom_id", "")
            err = row.get("error")
            if err:
                by_custom_id[custom_id] = {
                    "custom_id": custom_id,
                    "response": None,
                    "error": err,
                }
                continue
            resp_obj = row.get("response") or {}
            body = resp_obj.get("body", resp_obj)
            content = _openai_response_output_to_text(body)
            by_custom_id[custom_id] = {
                "custom_id": custom_id,
                "response": content,
                "error": None,
            }
    if error_file_id:
        url = f"https://api.openai.com/v1/files/{error_file_id}/content"
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            custom_id = row.get("custom_id", "")
            if custom_id not in by_custom_id:
                err = row.get("error")
                by_custom_id[custom_id] = {
                    "custom_id": custom_id,
                    "response": None,
                    "error": err or {"code": "unknown", "message": "Request failed"},
                }
    return list(by_custom_id.values())


# =============================================================================
# MULTI-BATCH: SPLIT ONE REQUEST LIST INTO MULTIPLE BATCHES (BOTH APIS)
# =============================================================================
# A single large JSONL / request list can be chunked and submitted as multiple
# batch jobs, then results are merged in original order. Use when you exceed
# per-batch limits or want smaller batches for faster turnaround.
# =============================================================================


def load_requests_from_jsonl(file_path_or_content: str) -> List[Dict[str, Any]]:
    """
    Load a JSONL file (or raw JSONL string) into a list of request dicts.

    - If file_path_or_content is a path to an existing file, reads that file.
    - Otherwise treats it as the raw JSONL content (e.g. from a single string).
    Each line must be a valid JSON object. Use with gemini_run_batches_from_requests
    (each dict: key, system_prompt, user_content, response_format) or
    openai_run_batches_from_requests (each dict: custom_id, body, and optionally method, url).
    """
    if os.path.isfile(file_path_or_content):
        with open(file_path_or_content, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = file_path_or_content
    requests = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        requests.append(json.loads(line))
    return requests


def _chunk_list(items: List[Any], chunk_size: int) -> List[List[Any]]:
    """Split a list into chunks of size chunk_size (last chunk may be smaller)."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def gemini_run_batches_from_requests(
    model: str,
    display_name_prefix: str,
    requests_config: List[Dict[str, Any]],
    max_requests_per_batch: Optional[int] = None,
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    use_file_for_large: bool = True,
) -> List[Dict[str, Any]]:
    """
    Split requests_config into multiple Gemini batches, run all, merge results in original order.

    If max_requests_per_batch is None, a single batch is used. Otherwise each chunk is submitted
    as a separate batch (e.g. 2000 requests with max_requests_per_batch=500 → 4 batches).
    Batches are polled in parallel. Returns list of {key, response_text, error, parsed} in the
    same order as requests_config (by key).
    """
    if not requests_config:
        return []
    chunk_size = max_requests_per_batch or len(requests_config)
    chunks = _chunk_list(requests_config, chunk_size)
    if len(chunks) == 1:
        batch_name = _gemini_batch_create(
            api_key,
            model,
            f"{display_name_prefix}-0",
            chunks[0],
            use_file_for_large=use_file_for_large,
        )
        batch = _gemini_batch_poll(
            api_key, batch_name, poll_interval=poll_interval, timeout=timeout
        )
        if _gemini_batch_state(batch) not in _GEMINI_BATCH_SUCCESS_STATES:
            raise RuntimeError(f"Gemini batch failed: {_gemini_batch_state(batch)!r}")
        return _gemini_batch_get_results(batch, api_key)
    batch_names = []
    for i, chunk in enumerate(chunks):
        name = _gemini_batch_create(
            api_key,
            model,
            f"{display_name_prefix}-{i}",
            chunk,
            use_file_for_large=use_file_for_large,
        )
        batch_names.append(name)
    batch_results = [None] * len(batch_names)
    errors = [None] * len(batch_names)

    def _poll_one(idx: int) -> None:
        try:
            batch = _gemini_batch_poll(
                api_key,
                batch_names[idx],
                poll_interval=poll_interval,
                timeout=timeout,
            )
            batch_results[idx] = _gemini_batch_get_results(batch, api_key)
            if _gemini_batch_state(batch) not in _GEMINI_BATCH_SUCCESS_STATES:
                errors[idx] = RuntimeError(
                    f"Batch {batch_names[idx]} failed: {_gemini_batch_state(batch)!r}"
                )
        except Exception as e:
            errors[idx] = e

    threads = [
        threading.Thread(target=_poll_one, args=(i,)) for i in range(len(batch_names))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i, err in enumerate(errors):
        if err is not None:
            raise RuntimeError(f"Gemini batch {batch_names[i]} failed") from err
    key_to_result = {}
    for res_list in batch_results:
        for r in res_list:
            key_to_result[r["key"]] = r
    keys_ordered = [c.get("key", str(i)) for i, c in enumerate(requests_config)]
    return [
        key_to_result.get(
            k,
            {
                "key": k,
                "response_text": None,
                "error": {"message": "missing"},
                "parsed": None,
            },
        )
        for k in keys_ordered
    ]


def openai_run_batches_from_requests(
    api_key: str,
    requests_body: List[Dict[str, Any]],
    max_requests_per_batch: Optional[int] = None,
    endpoint: str = OPENAI_BATCH_ENDPOINT_RESPONSES,
    completion_window: str = OPENAI_BATCH_COMPLETION_WINDOW,
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    timeout_download: int = 1200,
) -> List[Dict[str, Any]]:
    """
    Split requests_body into multiple OpenAI batches, run all, merge results in original order.

    If max_requests_per_batch is None, a single batch is used. Otherwise each chunk is
    uploaded as a separate file and one batch per chunk is created. Batches are polled
    in parallel. Returns list of {custom_id, response, error} in the same order as
    requests_body (by custom_id).
    """
    if not requests_body:
        return []
    chunk_size = max_requests_per_batch or len(requests_body)
    chunks = _chunk_list(requests_body, chunk_size)
    if len(chunks) == 1:
        file_id = _openai_upload_batch_file(api_key, chunks[0])
        batch_id = _openai_batch_create(
            api_key, file_id, endpoint=endpoint, completion_window=completion_window
        )
        batch = _openai_batch_poll(
            api_key, batch_id, poll_interval=poll_interval, timeout=timeout
        )
        if batch.get("status") != "completed":
            raise RuntimeError(f"OpenAI batch failed: {batch.get('status')}")
        results = _openai_batch_download_results(
            api_key,
            batch.get("output_file_id"),
            batch.get("error_file_id"),
            timeout=timeout_download,
        )
        custom_ids_ordered = [
            r.get("custom_id", str(i)) for i, r in enumerate(requests_body)
        ]
        by_cid = {r["custom_id"]: r for r in results}
        return [
            by_cid.get(
                cid,
                {
                    "custom_id": cid,
                    "response": None,
                    "error": {"code": "missing", "message": "missing"},
                },
            )
            for cid in custom_ids_ordered
        ]
    batch_ids = []
    for chunk in chunks:
        file_id = _openai_upload_batch_file(api_key, chunk)
        batch_id = _openai_batch_create(
            api_key, file_id, endpoint=endpoint, completion_window=completion_window
        )
        batch_ids.append(batch_id)
    batch_objects = [None] * len(batch_ids)
    errors = [None] * len(batch_ids)

    def _poll_one(idx: int) -> None:
        try:
            batch_objects[idx] = _openai_batch_poll(
                api_key,
                batch_ids[idx],
                poll_interval=poll_interval,
                timeout=timeout,
            )
        except Exception as e:
            errors[idx] = e

    threads = [
        threading.Thread(target=_poll_one, args=(i,)) for i in range(len(batch_ids))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i, err in enumerate(errors):
        if err is not None:
            raise RuntimeError(f"OpenAI batch {batch_ids[i]} failed") from err
    by_custom_id = {}
    for batch in batch_objects:
        results = _openai_batch_download_results(
            api_key,
            batch.get("output_file_id"),
            batch.get("error_file_id"),
            timeout=timeout_download,
        )
        for r in results:
            by_custom_id[r["custom_id"]] = r
    custom_ids_ordered = [
        r.get("custom_id", str(i)) for i, r in enumerate(requests_body)
    ]
    return [
        by_custom_id.get(
            cid,
            {
                "custom_id": cid,
                "response": None,
                "error": {"code": "missing", "message": "missing"},
            },
        )
        for cid in custom_ids_ordered
    ]


# =============================================================================
# BATCH PIPELINE: REJECTION → GENERATION → EVALUATION
# =============================================================================


def batch_prompt_rejection_check(
    user_prompts: List[str],
    model: str = "gemini-3.1-pro-preview",
    wait: bool = True,
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    max_requests_per_batch: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Run prompt rejection check in batch. Only prompts that pass (ACCEPT) should be used in later steps.

    When max_requests_per_batch is set (e.g. 500), large lists are split into multiple Gemini
    batches and results merged in order (recommended for 5000+ prompts).

    Returns:
        results: list of {"index", "prompt", "status", "result", "accepted"}
        accepted_indices: list of indices where status was ACCEPT
    """
    if not user_prompts:
        return [], []

    requests_config = []
    for i, prompt in enumerate(user_prompts):
        requests_config.append(
            {
                "key": str(i),
                "system_prompt": PROMPT_REJECTION_SYSTEM_PROMPT,
                "user_content": f"USER PROMPT:\n{prompt}",
                "response_format": "json",
            }
        )

    if not wait:
        if max_requests_per_batch is not None:
            raise ValueError("wait=False with max_requests_per_batch is not supported")
        batch_name = _gemini_batch_create(
            model,
            display_name="prompt-rejection-batch",
            requests_config=requests_config,
        )
        return [], []  # caller can poll and parse later

    if max_requests_per_batch is not None:
        raw_results = gemini_run_batches_from_requests(
            model,
            "prompt-rejection-batch",
            requests_config,
            max_requests_per_batch=max_requests_per_batch,
            poll_interval=poll_interval,
            timeout=timeout,
            use_file_for_large=True,
        )
    else:
        batch_name = _gemini_batch_create(
            model,
            display_name="prompt-rejection-batch",
            requests_config=requests_config,
        )
        batch = _gemini_batch_poll(
            batch_name, poll_interval=poll_interval, timeout=timeout
        )
        # Resolve state from Operation: state can be in response, top level, or metadata (GET returns Operation)
        resolved = batch.get("response") or batch
        state = (
            (resolved or {}).get("state", "")
            or batch.get("state", "")
            or (batch.get("metadata") or {}).get("state", "")
        )
        if state not in _GEMINI_BATCH_SUCCESS_STATES:
            print(
                f"[batch_prompt_rejection_check] Raw API response:\n{json.dumps(batch, indent=2, default=str)}",
                flush=True,
            )
            raise RuntimeError(
                f"Batch failed: state={state!r} error={batch.get('error', '')}"
            )
        raw_results = _gemini_batch_get_results(batch)
    key_to_index = {r["key"]: int(r["key"]) for r in raw_results}
    results = []
    accepted_indices = []
    for r in raw_results:
        idx = key_to_index.get(r["key"], -1)
        parsed = r.get("parsed") or {}
        status = (parsed.get("status") or "REJECT").upper()
        accepted = status == "ACCEPT"
        if accepted:
            accepted_indices.append(idx)
        prompt = user_prompts[idx] if 0 <= idx < len(user_prompts) else ""
        results.append(
            {
                "index": idx,
                "prompt": prompt,
                "status": status,
                "result": parsed,
                "accepted": accepted,
                "error": r.get("error"),
            }
        )
    results.sort(key=lambda x: x["index"])
    accepted_indices.sort()
    return results, accepted_indices


def batch_prompt_rejection_check_kimi(
    kimi_api_key: str,
    user_prompts: List[str],
    model: str = DEFAULT_KIMI_MODEL,
    max_workers: Optional[int] = 16,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Run prompt rejection check with Kimi in parallel (no batch API; uses ThreadPoolExecutor).
    Same return shape as batch_prompt_rejection_check: (results, accepted_indices).
    """
    if not user_prompts:
        return [], []
    workers = max_workers or 16
    results_by_index: Dict[int, Dict[str, Any]] = {}
    lock = threading.Lock()

    def _one(i: int, prompt: str) -> None:
        user_content = f"USER PROMPT:\n{prompt}"
        try:
            kimi_result = call_kimi_sync(
                kimi_api_key,
                model,
                PROMPT_REJECTION_SYSTEM_PROMPT,
                user_content,
                response_format="json",
            )
            text = kimi_result["text"]
            parsed = _parse_json_from_response(text)
        except Exception as e:
            with lock:
                results_by_index[i] = {
                    "index": i,
                    "prompt": prompt,
                    "status": "REJECT",
                    "result": {},
                    "accepted": False,
                    "error": {"message": str(e)},
                }
            return
        status = (parsed.get("status") or "REJECT").upper()
        accepted = status == "ACCEPT"
        with lock:
            results_by_index[i] = {
                "index": i,
                "prompt": prompt,
                "status": status,
                "result": parsed,
                "accepted": accepted,
                "error": None,
            }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_one, i, p) for i, p in enumerate(user_prompts)]
        for f in futures:
            f.result()
    results = [results_by_index[i] for i in range(len(user_prompts))]
    accepted_indices = [
        i
        for i in range(len(user_prompts))
        if results_by_index.get(i, {}).get("accepted")
    ]
    return results, accepted_indices


def run_prompt_rejection_for_tasks_kimi(
    kimi_api_key: str,
    tasks: List[Dict[str, Any]],
    model: str = DEFAULT_KIMI_MODEL,
    max_workers: Optional[int] = 16,
) -> List[Dict[str, Any]]:
    """
    Step 1 (Kimi): Run prompt rejection check on tasks. Same input/output as run_prompt_rejection_for_tasks.
    """
    if not tasks:
        return []
    prompts = [t.get("prompt", "") for t in tasks]
    results, _ = batch_prompt_rejection_check_kimi(
        kimi_api_key, prompts, model=model, max_workers=max_workers
    )
    by_index = {r["index"]: r for r in results}
    out = []
    for i, t in enumerate(tasks):
        r = by_index.get(i, {})
        parsed = r.get("result") or {}
        status = (r.get("status") or "REJECT").upper()
        out.append(
            {
                **dict(t),
                "task_id": _task_id(t),
                "prompt": t.get("prompt", ""),
                "rejection_status": status,
                "rejection_reason": parsed.get("rejection_reason"),
                "justification": parsed.get("justification"),
                "accepted": r.get("accepted", False),
                "error": r.get("error"),
            }
        )
    return out


# =============================================================================
# TASK-BASED PIPELINE (4 steps per guidelines)
# =============================================================================
# Step 1: Prompt rejection — list of {task_id, prompt} → same list with rejection status appended.
# Step 2: Response generation — list of {task_id, prompt} (passed only) → list of {task_id, prompt, gemini_response, gpt_response}.
# Step 3: Evaluation — list of {task_id, prompt, response_a, response_b, gemini_response, gpt_response} → list with all ratings and justifications.
# Step 4: Final output is the list returned by Step 3.
# =============================================================================


def run_prompt_rejection_for_tasks(
    tasks: List[Dict[str, Any]],
    model: str = "gemini-3.1-pro-preview",
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    max_requests_per_batch: Optional[int] = DEFAULT_MAX_REQUESTS_PER_BATCH,
) -> List[Dict[str, Any]]:
    """
    Step 1: Run prompt rejection check on a list of tasks. Each task must have task_id (or task_id) and prompt.

    INPUT:
        tasks: List[dict], each {"task_id": <id>, "prompt": str}

    OUTPUT:
        List[dict] — same length and order as tasks; each dict is the original task plus:
        - rejection_status: "ACCEPT" | "REJECT"
        - rejection_reason: str | None (from LLM result)
        - justification: str | None (from LLM result)
        - accepted: bool
        - error: optional error from batch request

    When max_requests_per_batch is set (default 500), large task lists are chunked into
    multiple API batches for reliable handling of 5000+ tasks.
    """
    if not tasks:
        return []
    prompts = [t.get("prompt", "") for t in tasks]
    results, _ = batch_prompt_rejection_check(
        prompts,
        model=model,
        wait=True,
        poll_interval=poll_interval,
        timeout=timeout,
        max_requests_per_batch=max_requests_per_batch,
    )
    by_index = {r["index"]: r for r in results}
    out = []
    for i, t in enumerate(tasks):
        r = by_index.get(i, {})
        parsed = r.get("result") or {}
        status = (r.get("status") or "REJECT").upper()
        out.append(
            {
                **dict(t),
                "task_id": _task_id(t),
                "prompt": t.get("prompt", ""),
                "rejection_status": status,
                "rejection_reason": parsed.get("rejection_reason"),
                "justification": parsed.get("justification"),
                "accepted": r.get("accepted", False),
                "error": r.get("error"),
            }
        )
    return out


def run_response_generation_for_tasks(
    openai_api_key: str,
    tasks: List[Dict[str, Any]],
    gemini_model: str = DEFAULT_GEMINI_GENERATION_MODEL,
    openai_model: str = DEFAULT_OPENAI_GENERATION_MODEL,
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    max_requests_per_batch: Optional[int] = DEFAULT_MAX_REQUESTS_PER_BATCH,
) -> List[Dict[str, Any]]:
    """
    Step 2: Generate Gemini and GPT responses for each task. Input = passed tasks only (task_id, prompt).

    INPUT:
        tasks: List[dict], each {"task_id": <id>, "prompt": str} (typically only tasks that passed rejection).

    OUTPUT:
        List[dict], each: {"task_id", "prompt", "gemini_response", "gpt_response"}

    When max_requests_per_batch is set (default 500), large task lists are chunked into
    multiple API batches for reliable handling of 5000+ tasks.
    """
    if not tasks:
        return []
    prompts = [t.get("prompt", "") for t in tasks]
    gemini_req = [
        {
            "key": str(i),
            "system_prompt": "",
            "user_content": p,
            "response_format": "text",
            "thinking_config": {"thinkingLevel": "LOW"},
        }
        for i, p in enumerate(prompts)
    ]
    openai_req = [
        {
            "custom_id": str(i),
            "body": _openai_responses_body(openai_model, p),
        }
        for i, p in enumerate(prompts)
    ]

    gemini_raw_list = [None]
    openai_list = [None]
    gemini_err = [None]
    openai_err = [None]

    def _run_gemini():
        try:
            gemini_raw_list[0] = gemini_run_batches_from_requests(
                gemini_model,
                "response-gen-gemini",
                gemini_req,
                max_requests_per_batch=max_requests_per_batch,
                poll_interval=poll_interval,
                timeout=timeout,
                use_file_for_large=True,
            )
        except Exception as e:
            gemini_err[0] = e

    def _run_openai():
        try:
            openai_list[0] = openai_run_batches_from_requests(
                openai_api_key,
                openai_req,
                max_requests_per_batch=max_requests_per_batch,
                poll_interval=poll_interval,
                timeout=timeout,
            )
        except Exception as e:
            openai_err[0] = e

    t1 = threading.Thread(target=_run_gemini)
    t2 = threading.Thread(target=_run_openai)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    if gemini_err[0]:
        raise RuntimeError(
            f"Gemini generation batch failed: {gemini_err[0]}"
        ) from gemini_err[0]
    if openai_err[0]:
        raise RuntimeError(
            f"OpenAI generation batch failed: {openai_err[0]}"
        ) from openai_err[0]

    gemini_raw = gemini_raw_list[0] or []
    gemini_by_key = {
        r["key"]: (r.get("response_text") or "")
        for r in gemini_raw
        if not r.get("error")
    }
    openai_results = openai_list[0] or []
    gpt_by_key = {
        r["custom_id"]: (r.get("response") or "")
        for r in openai_results
        if not r.get("error")
    }

    return [
        {
            "task_id": _task_id(t),
            "prompt": t.get("prompt", ""),
            "gemini_response": gemini_by_key.get(str(i), ""),
            "gpt_response": gpt_by_key.get(str(i), ""),
        }
        for i, t in enumerate(tasks)
    ]


def run_evaluation_for_tasks(
    evaluation_inputs: List[Dict[str, Any]],
    evaluation_model: str = "gemini-3.1-pro-preview",
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    max_requests_per_batch: Optional[int] = 1000,
) -> List[Dict[str, Any]]:
    """
    Step 3: Run full evaluation with per-dimension scoring (5 dimension calls + COMPARE_TWO + COMPARE_EXTERNAL x2 + CREATE_RUBRICS_EXTERNAL x2).
    Each dimension is one batch; results are merged into the same evaluation_result schema. Output structure unchanged.

    INPUT:
        evaluation_inputs: List[dict], each:
            - task_id (or taskId), prompt
            - response_a, response_b (pre-existing responses to evaluate)
            - gemini_response, gpt_response (from Step 2)

    OUTPUT (same schema as evaluation_for_tasks_sync; do not change):
        List[dict] — one per input; each dict contains:
        - task_id, prompt
        - evaluation_result: {response_a: {...}, response_b: {...}}
        - comparison_ab: {comparison_score, overall_comment}
        - comparison_vs_gemini: {comparison_score, comparison_comment}
        - comparison_vs_gpt: {comparison_score, comparison_comment}
        - rubrics_vs_gemini: rubrics value (list or dict)
        - rubrics_vs_gpt: rubrics value (list or dict)
        - sxs_winner_label: "Response A" | "Response B"
    """
    if not evaluation_inputs:
        return []

    n = len(evaluation_inputs)
    chunk = max_requests_per_batch or n

    def _run_batch(
        requests_config: List[Dict[str, Any]], batch_name: str
    ) -> Dict[str, Any]:
        if not requests_config:
            return {}
        if len(requests_config) <= chunk:
            name = _gemini_batch_create(evaluation_model, batch_name, requests_config)
            batch = _gemini_batch_poll(
                name, poll_interval=poll_interval, timeout=timeout
            )
            if _gemini_batch_state(batch) not in _GEMINI_BATCH_SUCCESS_STATES:
                raise RuntimeError(
                    f"Evaluation batch {batch_name!r} failed: {_gemini_batch_state(batch)!r}"
                )
            raw = _gemini_batch_get_results(batch)
        else:
            raw = gemini_run_batches_from_requests(
                evaluation_model,
                batch_name,
                requests_config,
                max_requests_per_batch=chunk,
                poll_interval=poll_interval,
                timeout=timeout,
                use_file_for_large=True,
            )
        return {r["key"]: r for r in raw}

    # Wave 1 (parallel): 5 per-dimension batches + COMPARE_TWO — no cross-dependencies
    by_dim_batches: List[Dict[str, Any]] = []
    compare_two_reqs = []
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        prompt, resp_a, resp_b = (
            inp.get("prompt", ""),
            inp.get("response_a", ""),
            inp.get("response_b", ""),
        )
        compare_two_reqs.append(
            {
                "key": f"compare-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_COMPARE_TWO,
                "user_content": "TASK: COMPARE_TWO\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nRESPONSE A:\n"
                + resp_a
                + "\n\n--------------------\nRESPONSE B:\n"
                + resp_b,
                "response_format": "json",
                "temperature": 0.2,
            }
        )

    def _build_dim_reqs(dim_key: str) -> List[Dict[str, Any]]:
        return [
            {
                "key": f"dim-{dim_key}-{str(_task_id(inp) or idx)}",
                "system_prompt": get_evaluation_system_prompt_for_dimension(dim_key),
                "user_content": f"Dimension: {dim_key}\n\nUSER PROMPT:\n{inp.get('prompt', '')}\n\n--------------------\nRESPONSE A:\n{inp.get('response_a', '')}\n\n--------------------\nRESPONSE B:\n{inp.get('response_b', '')}",
                "response_format": "json",
                "temperature": 0.2,
            }
            for idx, inp in enumerate(evaluation_inputs)
        ]

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures_dim = [
            executor.submit(
                _run_batch, _build_dim_reqs(dim_key), f"batch-eval-{dim_key}"
            )
            for dim_key in DIMENSION_KEYS
        ]
        future_compare = executor.submit(
            _run_batch, compare_two_reqs, "batch-compare-two"
        )
        by_dim_batches = [f.result() for f in futures_dim]
        by_compare = future_compare.result()

    # Resolve SxS winner text per task for steps 3–6
    sxs_winner_text_by_key = {}
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        parsed = (by_compare.get(f"compare-{task_id_key}", {}).get("parsed")) or {}
        comp_score = parsed.get("comparison_score", 0)
        sxs_winner_text_by_key[task_id_key] = (
            inp.get("response_a", "") if comp_score <= 0 else inp.get("response_b", "")
        )

    # Wave 2 (parallel): COMPARE_EXTERNAL x2 + CREATE_RUBRICS_EXTERNAL x2
    ext_gemini_reqs = []
    ext_gpt_reqs = []
    rub_g_reqs = []
    rub_p_reqs = []
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        prompt = inp.get("prompt", "")
        sxs = sxs_winner_text_by_key.get(task_id_key, "")
        gemini_resp = inp.get("gemini_response", "")
        gpt_resp = inp.get("gpt_response", "")
        ext_gemini_reqs.append(
            {
                "key": f"ext-g-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                "user_content": "TASK: COMPARE_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                + gemini_resp
                + "\n\nEXTERNAL MODEL NAME: Gemini 3 Pro",
                "response_format": "json",
                "temperature": 0.2,
            }
        )
        ext_gpt_reqs.append(
            {
                "key": f"ext-p-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                "user_content": "TASK: COMPARE_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                + gpt_resp
                + "\n\nEXTERNAL MODEL NAME: GPT 5.2",
                "response_format": "json",
                "temperature": 0.2,
            }
        )
        rub_g_reqs.append(
            {
                "key": f"rub-g-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                "user_content": "TASK: CREATE_RUBRICS_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                + gemini_resp
                + "\n\nEXTERNAL MODEL NAME: Gemini 3 Pro",
                "response_format": "json",
                "temperature": 0.2,
            }
        )
        rub_p_reqs.append(
            {
                "key": f"rub-p-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                "user_content": "TASK: CREATE_RUBRICS_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                + gpt_resp
                + "\n\nEXTERNAL MODEL NAME: GPT 5.2",
                "response_format": "json",
                "temperature": 0.2,
            }
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        f_ext_g = executor.submit(
            _run_batch, ext_gemini_reqs, "batch-compare-ext-gemini"
        )
        f_ext_p = executor.submit(_run_batch, ext_gpt_reqs, "batch-compare-ext-gpt")
        f_rub_g = executor.submit(_run_batch, rub_g_reqs, "batch-rubrics-gemini")
        f_rub_p = executor.submit(_run_batch, rub_p_reqs, "batch-rubrics-gpt")
        by_ext_g = f_ext_g.result()
        by_ext_p = f_ext_p.result()
        by_rub_g = f_rub_g.result()
        by_rub_p = f_rub_p.result()

    # Merge into same output schema (do not change)
    out = []
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        dimension_results = []
        for dim_key, by_dim in zip(DIMENSION_KEYS, by_dim_batches):
            dimension_results.append(
                (by_dim.get(f"dim-{dim_key}-{task_id_key}", {}).get("parsed")) or {}
            )
        evaluation_result = _build_evaluation_result_from_dimensions(dimension_results)
        pc = (by_compare.get(f"compare-{task_id_key}", {}).get("parsed")) or {}
        pg = (by_ext_g.get(f"ext-g-{task_id_key}", {}).get("parsed")) or {}
        pp = (by_ext_p.get(f"ext-p-{task_id_key}", {}).get("parsed")) or {}
        rg = (by_rub_g.get(f"rub-g-{task_id_key}", {}).get("parsed")) or {}
        rp = (by_rub_p.get(f"rub-p-{task_id_key}", {}).get("parsed")) or {}

        comparison_ab = {
            "comparison_score": pc.get("comparison_score", 0),
            "overall_comment": pc.get("overall_comment", ""),
        }
        comp_score = comparison_ab.get("comparison_score", 0)
        sxs_winner_label = "Response A" if comp_score <= 0 else "Response B"

        out.append(
            {
                "task_id": _task_id(inp),
                "prompt": inp.get("prompt", ""),
                "evaluation_result": evaluation_result,
                "comparison_ab": comparison_ab,
                "comparison_vs_gemini": {
                    "comparison_score": pg.get("comparison_score"),
                    "comparison_comment": pg.get("comparison_comment", ""),
                },
                "comparison_vs_gpt": {
                    "comparison_score": pp.get("comparison_score"),
                    "comparison_comment": pp.get("comparison_comment", ""),
                },
                "rubrics_vs_gemini": rg.get("rubrics", {}),
                "rubrics_vs_gpt": rp.get("rubrics", {}),
                "sxs_winner_label": sxs_winner_label,
            }
        )
    return out


def _kimi_run_requests_parallel(
    kimi_api_key: str,
    model: str,
    requests_config: List[Dict[str, Any]],
    max_workers: int = 32,
) -> Dict[str, Dict[str, Any]]:
    """
    Run a list of requests (each with key, system_prompt, user_content, response_format, temperature)
    via call_kimi_sync in parallel. Returns {key: {"parsed": ..., "error": ...}} to match Gemini batch result shape.
    """
    if not requests_config:
        return {}
    result_by_key: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()

    def _one(req: Dict[str, Any]) -> None:
        key = req.get("key", "")
        try:
            r = call_kimi_sync(
                kimi_api_key,
                model,
                req.get("system_prompt", ""),
                req.get("user_content", ""),
                response_format=req.get("response_format", "text"),
                temperature=req.get("temperature"),
            )
            text = r.get("text", "")
            parsed = (
                _parse_json_from_response(text)
                if text and req.get("response_format") == "json"
                else {}
            )
            with lock:
                result_by_key[key] = {"parsed": parsed, "error": None}
        except Exception as e:
            with lock:
                result_by_key[key] = {"parsed": {}, "error": {"message": str(e)}}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_one, r) for r in requests_config]
        for f in futures:
            f.result()
    return result_by_key


def run_evaluation_for_tasks_kimi(
    kimi_api_key: str,
    evaluation_inputs: List[Dict[str, Any]],
    evaluation_model: str = DEFAULT_KIMI_MODEL,
    max_workers: int = 32,
) -> List[Dict[str, Any]]:
    """
    Step 3 (Kimi): Run full evaluation using Kimi in parallel (no batch API). Same input/output as run_evaluation_for_tasks.
    """
    if not evaluation_inputs:
        return []

    def _run_batch_kimi(
        requests_config: List[Dict[str, Any]], _batch_name: str
    ) -> Dict[str, Dict[str, Any]]:
        if not requests_config:
            return {}
        raw = _kimi_run_requests_parallel(
            kimi_api_key, evaluation_model, requests_config, max_workers=max_workers
        )
        return {k: {"parsed": v["parsed"], "error": v["error"]} for k, v in raw.items()}

    # Wave 1: single all-dimensions batch + COMPARE_TWO batch (2 parallel batches)
    all_dims_reqs = []
    compare_two_reqs = []
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        prompt, resp_a, resp_b = (
            inp.get("prompt", ""),
            inp.get("response_a", ""),
            inp.get("response_b", ""),
        )
        all_dims_reqs.append(
            {
                "key": f"dims-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_EVALUATE_TWO,
                "user_content": "USER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nRESPONSE A:\n"
                + resp_a
                + "\n\n--------------------\nRESPONSE B:\n"
                + resp_b,
                "response_format": "json",
                "temperature": 0.2,
            }
        )
        compare_two_reqs.append(
            {
                "key": f"compare-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_COMPARE_TWO,
                "user_content": "TASK: COMPARE_TWO\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nRESPONSE A:\n"
                + resp_a
                + "\n\n--------------------\nRESPONSE B:\n"
                + resp_b,
                "response_format": "json",
                "temperature": 0.2,
            }
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_dims = executor.submit(
            _run_batch_kimi, all_dims_reqs, "batch-eval-all-dims"
        )
        future_compare = executor.submit(
            _run_batch_kimi, compare_two_reqs, "batch-compare-two"
        )
        by_dims = future_dims.result()
        by_compare = future_compare.result()

    sxs_winner_text_by_key = {}
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        parsed = (by_compare.get(f"compare-{task_id_key}", {}).get("parsed")) or {}
        comp_score = parsed.get("comparison_score", 0)
        sxs_winner_text_by_key[task_id_key] = (
            inp.get("response_a", "") if comp_score <= 0 else inp.get("response_b", "")
        )

    ext_gemini_reqs = []
    ext_gpt_reqs = []
    rub_g_reqs = []
    rub_p_reqs = []
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        prompt = inp.get("prompt", "")
        sxs = sxs_winner_text_by_key.get(task_id_key, "")
        gemini_resp = inp.get("gemini_response", "")
        gpt_resp = inp.get("gpt_response", "")
        ext_gemini_reqs.append(
            {
                "key": f"ext-g-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                "user_content": "TASK: COMPARE_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                + gemini_resp
                + "\n\nEXTERNAL MODEL NAME: Gemini 3 Pro",
                "response_format": "json",
                "temperature": 0.2,
            }
        )
        ext_gpt_reqs.append(
            {
                "key": f"ext-p-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_COMPARE_EXTERNAL,
                "user_content": "TASK: COMPARE_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                + gpt_resp
                + "\n\nEXTERNAL MODEL NAME: GPT 5.2",
                "response_format": "json",
                "temperature": 0.2,
            }
        )
        rub_g_reqs.append(
            {
                "key": f"rub-g-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                "user_content": "TASK: CREATE_RUBRICS_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (Gemini 3 Pro) RESPONSE:\n"
                + gemini_resp
                + "\n\nEXTERNAL MODEL NAME: Gemini 3 Pro",
                "response_format": "json",
                "temperature": 0.2,
            }
        )
        rub_p_reqs.append(
            {
                "key": f"rub-p-{task_id_key}",
                "system_prompt": EVALUATION_SYSTEM_PROMPT_CREATE_RUBRICS_EXTERNAL,
                "user_content": "TASK: CREATE_RUBRICS_EXTERNAL\n\nUSER PROMPT:\n"
                + prompt
                + "\n\n--------------------\nSXS WINNER RESPONSE:\n"
                + sxs
                + "\n\n--------------------\nEXTERNAL MODEL (GPT 5.2) RESPONSE:\n"
                + gpt_resp
                + "\n\nEXTERNAL MODEL NAME: GPT 5.2",
                "response_format": "json",
                "temperature": 0.2,
            }
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        f_ext_g = executor.submit(
            _run_batch_kimi, ext_gemini_reqs, "batch-compare-ext-gemini"
        )
        f_ext_p = executor.submit(
            _run_batch_kimi, ext_gpt_reqs, "batch-compare-ext-gpt"
        )
        f_rub_g = executor.submit(_run_batch_kimi, rub_g_reqs, "batch-rubrics-gemini")
        f_rub_p = executor.submit(_run_batch_kimi, rub_p_reqs, "batch-rubrics-gpt")
        by_ext_g = f_ext_g.result()
        by_ext_p = f_ext_p.result()
        by_rub_g = f_rub_g.result()
        by_rub_p = f_rub_p.result()

    out = []
    for idx, inp in enumerate(evaluation_inputs):
        task_id_key = str(_task_id(inp) or idx)
        full_parsed = (by_dims.get(f"dims-{task_id_key}", {}).get("parsed")) or {}
        ra = full_parsed.get("response_a") or {}
        rb = full_parsed.get("response_b") or {}
        dimension_results = [
            {"response_a": {dk: ra.get(dk, {})}, "response_b": {dk: rb.get(dk, {})}}
            for dk in DIMENSION_KEYS
        ]
        evaluation_result = _build_evaluation_result_from_dimensions(dimension_results)
        pc = (by_compare.get(f"compare-{task_id_key}", {}).get("parsed")) or {}
        pg = (by_ext_g.get(f"ext-g-{task_id_key}", {}).get("parsed")) or {}
        pp = (by_ext_p.get(f"ext-p-{task_id_key}", {}).get("parsed")) or {}
        rg = (by_rub_g.get(f"rub-g-{task_id_key}", {}).get("parsed")) or {}
        rp = (by_rub_p.get(f"rub-p-{task_id_key}", {}).get("parsed")) or {}

        comparison_ab = {
            "comparison_score": pc.get("comparison_score", 0),
            "overall_comment": pc.get("overall_comment", ""),
        }
        comp_score = comparison_ab.get("comparison_score", 0)
        sxs_winner_label = "Response A" if comp_score <= 0 else "Response B"

        out.append(
            {
                "task_id": _task_id(inp),
                "prompt": inp.get("prompt", ""),
                "evaluation_result": evaluation_result,
                "comparison_ab": comparison_ab,
                "comparison_vs_gemini": {
                    "comparison_score": pg.get("comparison_score"),
                    "comparison_comment": pg.get("comparison_comment", ""),
                },
                "comparison_vs_gpt": {
                    "comparison_score": pp.get("comparison_score"),
                    "comparison_comment": pp.get("comparison_comment", ""),
                },
                "rubrics_vs_gemini": rg.get("rubrics", {}),
                "rubrics_vs_gpt": rp.get("rubrics", {}),
                "sxs_winner_label": sxs_winner_label,
            }
        )
    return out


def run_batch_pipeline(
    openai_api_key: str,
    evaluation_inputs: List[Dict[str, Any]],
    gemini_model: str = "gemini-3.1-pro-preview",
    openai_model: str = "gpt-5.2",
    rejection_model: str = "gemini-3.1-pro-preview",
    evaluation_model: str = "gemini-3.1-pro-preview",
    poll_interval: int = 30,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Full batch pipeline per Vindex workflow:
    1. Prompt rejection check (batch) — only accepted prompts continue.
    2. Generate Gemini and GPT responses for each accepted prompt (external models).
    3. Run full evaluation in six API calls per task (EVALUATE_TWO, COMPARE_TWO, COMPARE_EXTERNAL x2, CREATE_RUBRICS_EXTERNAL x2) via run_evaluation_for_tasks; output schema unchanged.

    Args:
        openai_api_key: API key for OpenAI.
        evaluation_inputs: List of dicts, each containing:
            - "prompt": str — the user prompt
            - "response_a": str — pre-existing Response A
            - "response_b": str — pre-existing Response B
        gemini_model: Model for generating Gemini external responses (default: Gemini 3 Pro).
        openai_model: Model for generating GPT external responses (default: GPT 5.2).
        rejection_model: Model for prompt rejection check (default: gemini-3-pro-preview).
        evaluation_model: Model for running evaluations (default: gemini-3-pro-preview).
        poll_interval: Polling interval in seconds.
        timeout: Optional timeout in seconds.

    Returns:
        Dict with (key names aligned with run_evaluation_for_tasks / evaluation_for_tasks_sync):
        - rejection_results: List of rejection check results.
        - accepted_indices: List of indices that passed rejection.
        - evaluation_results: List of evaluation_result objects (EVALUATE_TWO shape).
        - comparison_results: List of comparison_ab objects (COMPARE_TWO shape).
        - gemini_responses: List of generated Gemini responses (external model).
        - gpt_responses: List of generated GPT responses (external model).
        - external_model_comparisons: Dict with "gemini" and "gpt" comparison results.
        - rubrics_vs_gemini: List of { "rubrics": {...} } per task.
        - rubrics_vs_gpt: List of { "rubrics": {...} } per task.
    """
    # Extract prompts for rejection check
    user_prompts = [inp.get("prompt", "") for inp in evaluation_inputs]

    rejection_results, accepted_indices = batch_prompt_rejection_check(
        user_prompts,
        model=rejection_model,
        wait=True,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    if not accepted_indices:
        return {
            "rejection_results": rejection_results,
            "accepted_indices": accepted_indices,
            "evaluation_results": [],
            "comparison_results": [],
            "gemini_responses": [],
            "gpt_responses": [],
            "external_model_comparisons": {},
            "rubrics_vs_gemini": [],
            "rubrics_vs_gpt": [],
        }

    # Get accepted inputs
    accepted_inputs = [evaluation_inputs[i] for i in accepted_indices]
    accepted_prompts = [inp.get("prompt", "") for inp in accepted_inputs]
    responses_a = [inp.get("response_a", "") for inp in accepted_inputs]
    responses_b = [inp.get("response_b", "") for inp in accepted_inputs]

    # -------------------------------------------------------------------------
    # STEP 1: Generate Gemini and GPT responses (external models) in parallel
    # -------------------------------------------------------------------------
    gemini_req = []
    for i, prompt in zip(accepted_indices, accepted_prompts):
        gemini_req.append(
            {
                "key": str(i),
                "system_prompt": "",
                "user_content": prompt,
                "response_format": "text",
                "thinking_config": {"thinkingLevel": "LOW"},
            }
        )
    openai_req = []
    for i, prompt in zip(accepted_indices, accepted_prompts):
        openai_req.append(
            {
                "custom_id": str(i),
                "body": _openai_responses_body(openai_model, prompt),
            }
        )

    batch_gemini_name = _gemini_batch_create(
        gemini_model, "batch-gemini-generate", gemini_req
    )
    input_file_id = _openai_upload_batch_file(openai_api_key, openai_req)
    openai_batch_id = _openai_batch_create(openai_api_key, input_file_id)

    batch_gemini_result = [None]
    openai_batch_result = [None]
    gemini_error = [None]
    openai_error = [None]

    def _poll_gemini():
        try:
            batch = _gemini_batch_poll(
                batch_gemini_name,
                poll_interval=poll_interval,
                timeout=timeout,
            )
            batch_gemini_result[0] = batch
        except Exception as e:
            gemini_error[0] = e

    def _poll_openai():
        try:
            batch = _openai_batch_poll(
                openai_api_key,
                openai_batch_id,
                poll_interval=poll_interval,
                timeout=timeout,
            )
            openai_batch_result[0] = batch
        except Exception as e:
            openai_error[0] = e

    threads = [
        threading.Thread(target=_poll_gemini),
        threading.Thread(target=_poll_openai),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if gemini_error[0]:
        raise RuntimeError(
            f"Gemini generation batch failed: {gemini_error[0]}"
        ) from gemini_error[0]
    if openai_error[0]:
        raise RuntimeError(
            f"OpenAI generation batch failed: {openai_error[0]}"
        ) from openai_error[0]

    batch_gemini = batch_gemini_result[0]
    if _gemini_batch_state(batch_gemini) not in _GEMINI_BATCH_SUCCESS_STATES:
        raise RuntimeError(
            f"Gemini generation batch failed: {_gemini_batch_state(batch_gemini)!r}"
        )
    gemini_raw = _gemini_batch_get_results(batch_gemini)
    gemini_by_key = {
        r["key"]: (r.get("response_text") or "")
        for r in gemini_raw
        if not r.get("error")
    }

    openai_batch = openai_batch_result[0]
    if openai_batch.get("status") != "completed":
        raise RuntimeError(
            f"OpenAI generation batch failed: {openai_batch.get('status')}"
        )
    output_file_id = openai_batch.get("output_file_id")
    error_file_id = openai_batch.get("error_file_id")
    openai_results = (
        _openai_batch_download_results(
            openai_api_key, output_file_id, error_file_id=error_file_id
        )
        if output_file_id or error_file_id
        else []
    )
    gpt_by_key = {
        r["custom_id"]: (r.get("response") or "")
        for r in openai_results
        if not r.get("error")
    }

    gemini_responses = [
        {
            "index": i,
            "prompt": p,
            "response": gemini_by_key.get(str(i), ""),
            "model": gemini_model,
        }
        for i, p in zip(accepted_indices, accepted_prompts)
    ]
    gpt_responses = [
        {
            "index": i,
            "prompt": p,
            "response": gpt_by_key.get(str(i), ""),
            "model": openai_model,
        }
        for i, p in zip(accepted_indices, accepted_prompts)
    ]

    # -------------------------------------------------------------------------
    # STEP 2: Full evaluation via run_evaluation_for_tasks (six API calls per task;
    # bifurcated prompts). Output schema unchanged.
    # -------------------------------------------------------------------------
    evaluation_inputs = [
        {
            "task_id": i,
            "prompt": accepted_prompts[idx],
            "response_a": responses_a[idx],
            "response_b": responses_b[idx],
            "gemini_response": gemini_by_key.get(str(i), ""),
            "gpt_response": gpt_by_key.get(str(i), ""),
        }
        for idx, i in enumerate(accepted_indices)
    ]
    eval_out = run_evaluation_for_tasks(
        evaluation_inputs,
        evaluation_model=evaluation_model,
        poll_interval=poll_interval,
        timeout=timeout,
        max_requests_per_batch=1000,
    )

    evaluation_results = [r["evaluation_result"] for r in eval_out]
    comparison_results = [r["comparison_ab"] for r in eval_out]
    external_model_comparisons: Dict[str, List[Dict[str, Any]]] = {
        "gemini": [],
        "gpt": [],
    }
    for r in eval_out:
        tid = r.get("task_id")
        external_model_comparisons["gemini"].append(
            {
                "index": tid,
                "comparison_score": r["comparison_vs_gemini"].get("comparison_score"),
                "comparison_comment": r["comparison_vs_gemini"].get(
                    "comparison_comment"
                ),
                "error": None,
            }
        )
        external_model_comparisons["gpt"].append(
            {
                "index": tid,
                "comparison_score": r["comparison_vs_gpt"].get("comparison_score"),
                "comparison_comment": r["comparison_vs_gpt"].get("comparison_comment"),
                "error": None,
            }
        )
    rubrics_vs_gemini = [{"rubrics": r["rubrics_vs_gemini"]} for r in eval_out]
    rubrics_vs_gpt = [{"rubrics": r["rubrics_vs_gpt"]} for r in eval_out]

    return {
        "rejection_results": rejection_results,
        "accepted_indices": accepted_indices,
        "evaluation_results": evaluation_results,
        "comparison_results": comparison_results,
        "gemini_responses": gemini_responses,
        "gpt_responses": gpt_responses,
        "external_model_comparisons": external_model_comparisons,
        "rubrics_vs_gemini": rubrics_vs_gemini,
        "rubrics_vs_gpt": rubrics_vs_gpt,
    }


# =============================================================================
# QC CHECKS (separate from main pipeline) — SYNC and BATCH versions
# =============================================================================
#
# QC is performed on manual ratings done by humans. It validates human rater
# comments and rubrics for AI-generated text and rubric-comment grounding.
#
# Em-dash rule: if any comment field contains em-dash (U+2014), result is forced to QC_Fail with failure_reason "em-dash".

_QC_COMMENT_KEYS = (
    "ab_comment",
    "human_ab_gpt_comment",
    "human_ab_gemini_comment",
    "human_gpt_rubric_name",
    "human_gpt_rubric_description",
    "human_gemini_rubric_name",
    "human_gemini_rubric_description",
)
_QC_EM_DASH_PATTERN = re.compile(r"\u2014")  # Unicode em-dash


def _qc_input_contains_em_dash(inp: Dict[str, Any]) -> bool:
    """Return True if any comment-like field in inp contains an em-dash character."""
    for key in _QC_COMMENT_KEYS:
        val = inp.get(key)
        if val is None:
            continue
        if isinstance(val, str) and _QC_EM_DASH_PATTERN.search(val):
            return True
    return False


#
# -----------------------------------------------------------------------------
# QC INPUT REQUIREMENTS (manual human ratings — what is needed as input)
# -----------------------------------------------------------------------------
#   qc_inputs: List[Dict[str, Any]] — one dict per task to be QC'd.
#
#   Each dict represents one task's manual human ratings. Keys (used by QC_SYSTEM_PROMPT):
#     - task_id: str (or int) — identifier for the task
#     - ab_comment: str — overall A vs B comparison comment (checked for AI text; used for preference grounding)
#     - ab_preference: int (-3 to +3) — A vs B preference score (validated against ab_comment in CHECK 3)
#     - ab_gpt_preference: int (-3 to +3) — Likert preference SxS winner vs GPT (validated against human_ab_gpt_comment in CHECK 5)
#     - ab_gemini_preference: int (-3 to +3) — Likert preference SxS winner vs Gemini (validated against human_ab_gemini_comment in CHECK 5)
#     - human_ab_gpt_comment: str — human rater's comparison comment (SxS winner vs GPT 5.2)
#     - human_ab_gemini_comment: str — human rater's comparison comment (SxS winner vs Gemini 3 Pro)
#     - human_gpt_rubric_name: str — human rater's rubric name for GPT comparison
#     - human_gpt_rubric_description: str — human rater's description (GPT)
#     - human_gpt_rubric_scale_rating: int (1-6) — human rater's rubric rating for GPT
#     - human_gemini_rubric_name: str — human rater's rubric name for Gemini comparison
#     - human_gemini_rubric_description: str — human rater's description (Gemini)
#     - human_gemini_rubric_scale_rating: int (1-6) — human rater's rubric rating for Gemini
#
#   Optional keys for comment-response grounding (when provided, QC verifies comments are grounded in the actual content):
#     - response_a: str — Response A text (for grounding ab_comment in CHECK 3)
#     - response_b: str — Response B text (for grounding ab_comment in CHECK 3)
#     - gemini_response: str — Gemini model response (for grounding human_ab_gemini_comment in CHECK 2)
#     - gpt_response: str — GPT model response (for grounding human_ab_gpt_comment in CHECK 2)
#
#   The QC API receives: "MANUAL HUMAN RATINGS TO QC:\n" + JSON of the dict.
#   Optional/extra keys are passed through.
#
# Output: List[Dict] - QC results with:
#   - task_id: identifier
#   - qc_status: "QC_Pass" or "QC_Fail"
#   - overall_severity: 1 (Major) to 3 (Advisory)
#   - checks: {ai_detection, rubric_comment_grounding, ab_preference_comment_grounding, rubric_rating_justification, external_preference_comment_grounding}
#   - summary: brief summary of QC result
#   - failure_reason: optional; e.g. "em-dash" when comments contain em-dash (overrides LLM result to QC_Fail)
#   - error: error info if API call failed


_QC_ALL_FLAGGED_FIELDS = [
    "ab_comment",
    "human_ab_gpt_comment",
    "human_ab_gemini_comment",
    "human_gpt_rubric_name",
    "human_gpt_rubric_description",
    "human_gemini_rubric_name",
    "human_gemini_rubric_description",
]


def _merge_qc_subchecks_into_flagged_fields(result: Dict[str, Any]) -> None:
    """Merge CHECK 6 (justification_ai_check) and CHECK 7 (justification_grammar_check)
    issues into the CHECK 1 flagged_fields arrays so that models.py can read all QC
    feedback at flagged_fields[field][0] without any changes.

    For each field, combines:
      - CHECK 1 keyword-flagged words
      - CHECK 6 holistic AI-generation issue
      - CHECK 7 grammar / Indian English issue
    into a single string at flagged_fields[field][0].
    """
    checks = result.get("checks")
    if not checks or not isinstance(checks, dict):
        return
    ai_det = checks.get("ai_detection")
    if not ai_det or not isinstance(ai_det, dict):
        return

    flagged = ai_det.get("flagged_fields") or {}
    c6_fields = (ai_det.get("justification_ai_check") or {}).get("flagged_fields") or {}
    c7_fields = (ai_det.get("justification_grammar_check") or {}).get(
        "flagged_fields"
    ) or {}

    for field in _QC_ALL_FLAGGED_FIELDS:
        parts = []

        existing = flagged.get(field, [])
        if existing and isinstance(existing, list):
            words = [w for w in existing if isinstance(w, str) and w.strip()]
            if words:
                parts.append("AI-flagged words: " + ", ".join(words))

        c6 = c6_fields.get(field, {})
        if isinstance(c6, dict) and c6.get("result") == "fail":
            issue = (c6.get("issue") or "").strip()
            if issue:
                parts.append("AI-generated text: " + issue)

        c7 = c7_fields.get(field, {})
        if isinstance(c7, dict) and c7.get("result") == "fail":
            issue = (c7.get("issue") or "").strip()
            if issue:
                parts.append("Grammar/Indian English: " + issue)

        if parts:
            flagged[field] = ["\n".join(parts)]

    ai_det["flagged_fields"] = flagged


def perform_qc_checks_sync(
    qc_inputs: List[Dict[str, Any]],
    model: str = "gemini-3.1-pro-preview",
) -> List[Dict[str, Any]]:
    """
    Run QC checks synchronously on manual human ratings (one API call per input, no batch).

    Auth is handled internally by the Vertex AI client singleton.

    Args:
        qc_inputs: List[Dict], each containing one task's manual human ratings:
            - task_id: identifier
            - ab_comment: overall A vs B comparison comment (checked for AI text and for preference grounding)
            - ab_preference: int -3 to +3 (validated against ab_comment in CHECK 3)
            - ab_gpt_preference: int -3 to +3 (validated against human_ab_gpt_comment in CHECK 5)
            - ab_gemini_preference: int -3 to +3 (validated against human_ab_gemini_comment in CHECK 5)
            - human_ab_gpt_comment: human rater's comparison vs GPT comment
            - human_ab_gemini_comment: human rater's comparison vs Gemini comment
            - human_gpt_rubric_name/description/scale_rating, human_gemini_rubric_* (see module comment).
            Optional, for comment-response grounding: response_a, response_b, gemini_response, gpt_response.
        model: Gemini model to use.

    Returns:
        List[Dict], each containing:
            - task_id: identifier
            - qc_status: "QC_Pass" or "QC_Fail"
            - overall_severity: 1-3 (1=Major, 3=Advisory)
            - checks: {ai_detection, rubric_comment_grounding, ab_preference_comment_grounding, rubric_rating_justification, external_preference_comment_grounding}
            - summary: brief explanation
            - error: error info if failed
    """
    if not qc_inputs:
        return []

    results = []
    total_gemini = _empty_usage()
    for inp in qc_inputs:
        task_id = _task_id(inp)
        user_content = f"MANUAL HUMAN RATINGS TO QC:\n{json.dumps(inp, indent=2)}"
        task_gemini = _empty_usage()
        try:
            gem_result = call_gemini_sync(
                model,
                QC_SYSTEM_PROMPT,
                user_content,
                response_format="json",
                temperature=0.5,
            )
            task_gemini = gem_result.get("usage") or _empty_usage()
            _add_usage(total_gemini, task_gemini)
            text = gem_result.get("text") or ""
            parsed = json.loads(text) if text else {}
        except Exception as e:
            results.append(
                {
                    "task_id": task_id,
                    "qc_status": "QC_Fail",
                    "overall_severity": 1,
                    "checks": {},
                    "summary": f"QC API call failed: {str(e)}",
                    "error": {"message": str(e)},
                }
            )
            continue
        result = {
            "task_id": task_id,
            "qc_status": parsed.get("qc_status", "QC_Fail"),
            "overall_severity": parsed.get("overall_severity", 1),
            "checks": parsed.get("checks", {}),
            "summary": parsed.get("summary", ""),
            "error": None,
            "token_usage": calculate_cost(task_gemini, _empty_usage()),
        }
        if _qc_input_contains_em_dash(inp):
            result["qc_status"] = "QC_Fail"
            result["overall_severity"] = 1
            result["summary"] = "Em-dash detected in comments."
            result["failure_reason"] = "em-dash"
        _merge_qc_subchecks_into_flagged_fields(result)
        results.append(result)
    cumulative = calculate_cost(total_gemini, _empty_usage())
    for d in results:
        d["token_usage_cumulative"] = cumulative
    if output_parsing is not None:
        results = output_parsing.normalize_qc_results(results)
    return results


def perform_qc_checks_batch(
    qc_inputs: List[Dict[str, Any]],
    model: str = "gemini-3.1-pro-preview",
    poll_interval: int = 30,
    timeout: Optional[int] = None,
    max_requests_per_batch: Optional[int] = 1000,
) -> List[Dict[str, Any]]:
    """
    Run QC checks in batch mode on manual human ratings. Same input/output as perform_qc_checks_sync.

    Args:
        qc_inputs: List[Dict], each containing one task's manual human ratings (same keys as
            perform_qc_checks_sync). Optional keys for comment-response grounding:
            response_a, response_b, gemini_response, gpt_response.
        model: Gemini model to use.
        poll_interval: Polling interval in seconds.
        timeout: Optional timeout in seconds.
        max_requests_per_batch: Max requests per batch (for chunking large inputs).

    Returns:
        List[Dict], each containing:
            - task_id: identifier
            - qc_status: "QC_Pass" or "QC_Fail"
            - overall_severity: 1-3 (1=Major, 3=Advisory)
            - checks: {ai_detection, rubric_comment_grounding, ab_preference_comment_grounding, rubric_rating_justification, external_preference_comment_grounding}
            - summary: brief explanation
            - error: error info if failed
    """
    if not qc_inputs:
        return []

    # Build requests config
    requests_config = []
    for i, inp in enumerate(qc_inputs):
        user_content = f"MANUAL HUMAN RATINGS TO QC:\n{json.dumps(inp, indent=2)}"
        requests_config.append(
            {
                "key": str(i),
                "system_prompt": QC_SYSTEM_PROMPT,
                "user_content": user_content,
                "response_format": "json",
                "temperature": 0.5,
            }
        )

    # Use chunked batch runner for large inputs
    if max_requests_per_batch is not None:
        raw = gemini_run_batches_from_requests(
            model,
            "qc-checks-batch",
            requests_config,
            max_requests_per_batch=max_requests_per_batch,
            poll_interval=poll_interval,
            timeout=timeout,
        )
    else:
        batch_name = _gemini_batch_create(
            model,
            display_name="qc-checks-batch",
            requests_config=requests_config,
        )
        batch = _gemini_batch_poll(
            batch_name, poll_interval=poll_interval, timeout=timeout
        )
        if _gemini_batch_state(batch) not in _GEMINI_BATCH_SUCCESS_STATES:
            raise RuntimeError(f"QC batch failed: {_gemini_batch_state(batch)!r}")
        raw = _gemini_batch_get_results(batch)

    # Map results back to inputs
    key_to_result = {r["key"]: r for r in raw}
    results = []
    for i, inp in enumerate(qc_inputs):
        task_id = _task_id(inp)
        r = key_to_result.get(str(i), {})
        parsed = r.get("parsed") or {}
        err = r.get("error")
        if err:
            results.append(
                {
                    "task_id": task_id,
                    "qc_status": "QC_Fail",
                    "overall_severity": 1,
                    "checks": {},
                    "summary": f"QC API call failed",
                    "error": err,
                }
            )
        else:
            result = {
                "task_id": task_id,
                "qc_status": parsed.get("qc_status", "QC_Fail"),
                "overall_severity": parsed.get("overall_severity", 1),
                "checks": parsed.get("checks", {}),
                "summary": parsed.get("summary", ""),
                "error": None,
            }
            if _qc_input_contains_em_dash(inp):
                result["qc_status"] = "QC_Fail"
                result["overall_severity"] = 1
                result["summary"] = "Em-dash detected in comments."
                result["failure_reason"] = "em-dash"
            _merge_qc_subchecks_into_flagged_fields(result)
            results.append(result)
    if output_parsing is not None:
        results = output_parsing.normalize_qc_results(results)
    return results


def perform_qc_checks_sync_kimi(
    kimi_api_key: str,
    qc_inputs: List[Dict[str, Any]],
    model: str = DEFAULT_KIMI_MODEL,
) -> List[Dict[str, Any]]:
    """
    Run QC checks with Kimi synchronously (one API call per input). Same input/output as perform_qc_checks_sync.
    """
    if not qc_inputs:
        return []
    results = []
    total_usage = _empty_usage()
    for inp in qc_inputs:
        task_id = _task_id(inp)
        user_content = f"MANUAL HUMAN RATINGS TO QC:\n{json.dumps(inp, indent=2)}"
        task_usage = _empty_usage()
        try:
            kimi_result = call_kimi_sync(
                kimi_api_key,
                model,
                QC_SYSTEM_PROMPT,
                user_content,
                response_format="json",
                temperature=0.5,
            )
            task_usage = kimi_result.get("usage") or _empty_usage()
            _add_usage(total_usage, task_usage)
            text = kimi_result.get("text") or ""
            parsed = _parse_json_from_response(text)
        except Exception as e:
            results.append(
                {
                    "task_id": task_id,
                    "qc_status": "QC_Fail",
                    "overall_severity": 1,
                    "checks": {},
                    "summary": f"QC API call failed: {str(e)}",
                    "error": {"message": str(e)},
                }
            )
            continue
        result = {
            "task_id": task_id,
            "qc_status": parsed.get("qc_status", "QC_Fail"),
            "overall_severity": parsed.get("overall_severity", 1),
            "checks": parsed.get("checks", {}),
            "summary": parsed.get("summary", ""),
            "error": None,
            "token_usage": calculate_cost(_empty_usage(), _empty_usage(), task_usage),
        }
        if _qc_input_contains_em_dash(inp):
            result["qc_status"] = "QC_Fail"
            result["overall_severity"] = 1
            result["summary"] = "Em-dash detected in comments."
            result["failure_reason"] = "em-dash"
        _merge_qc_subchecks_into_flagged_fields(result)
        results.append(result)
    cumulative = calculate_cost(_empty_usage(), _empty_usage(), total_usage)
    for d in results:
        d["token_usage_cumulative"] = cumulative
    if output_parsing is not None:
        results = output_parsing.normalize_qc_results(results)
    return results


def perform_qc_checks_batch_kimi(
    kimi_api_key: str,
    qc_inputs: List[Dict[str, Any]],
    model: str = DEFAULT_KIMI_MODEL,
    max_workers: Optional[int] = 16,
) -> List[Dict[str, Any]]:
    """
    Run QC checks with Kimi in parallel (no batch API). Same input/output as perform_qc_checks_batch.
    """
    if not qc_inputs:
        return []
    requests_config = []
    for i, inp in enumerate(qc_inputs):
        user_content = f"MANUAL HUMAN RATINGS TO QC:\n{json.dumps(inp, indent=2)}"
        requests_config.append(
            {
                "key": str(i),
                "system_prompt": QC_SYSTEM_PROMPT,
                "user_content": user_content,
                "response_format": "json",
                "temperature": 0.5,
            }
        )
    raw = _kimi_run_requests_parallel(
        kimi_api_key,
        model,
        requests_config,
        max_workers=max_workers or 16,
    )
    results = []
    for i, inp in enumerate(qc_inputs):
        task_id = _task_id(inp)
        r = raw.get(str(i), {"parsed": {}, "error": {"message": "missing"}})
        parsed = r.get("parsed") or {}
        err = r.get("error")
        if err:
            results.append(
                {
                    "task_id": task_id,
                    "qc_status": "QC_Fail",
                    "overall_severity": 1,
                    "checks": {},
                    "summary": "QC API call failed",
                    "error": err,
                }
            )
        else:
            result = {
                "task_id": task_id,
                "qc_status": parsed.get("qc_status", "QC_Fail"),
                "overall_severity": parsed.get("overall_severity", 1),
                "checks": parsed.get("checks", {}),
                "summary": parsed.get("summary", ""),
                "error": None,
            }
            if _qc_input_contains_em_dash(inp):
                result["qc_status"] = "QC_Fail"
                result["overall_severity"] = 1
                result["summary"] = "Em-dash detected in comments."
                result["failure_reason"] = "em-dash"
            _merge_qc_subchecks_into_flagged_fields(result)
            results.append(result)
    if output_parsing is not None:
        results = output_parsing.normalize_qc_results(results)
    return results
