# -*- coding: utf-8 -*-
"""
Kimi-only evaluation pipeline (self-contained).

Performs:
  1. Prompt rejection check
  2. Per-dimension metric scores + reasons (5 dimensions)
  3. AB preference (-3 to +3)
  4. AB justification (overall_comment)
  5. QC on the evaluated fields only (no external model checks)
  6. Follow-up prompt generation from a given prompt and response(s) (Kimi)
  7. Follow-up relevance check: whether a follow-up prompt is relevant to the previous prompt and winning response (Kimi)

LLM backend: AWS Bedrock only (Kimi K2.5 via Converse API). Set AWS_REGION or BEDROCK_REGION
(e.g. us-east-1) and AWS credentials (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, or IAM role).
Requires: pip install boto3

No imports from llm_actions.py — fully standalone.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

_addon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path = os.path.join(_addon_root, ".env")
if os.path.isfile(_env_path):
    load_dotenv(_env_path)
else:
    load_dotenv()

# Optional: boto3 for AWS Bedrock (Kimi K2.5 on Bedrock)
try:
    import boto3
    import botocore
except ImportError:
    boto3 = None  # type: ignore
    botocore = None  # type: ignore

_logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

# Official model ID from https://platform.moonshot.ai/docs/api/chat (List: kimi-k2.5, kimi-k2-turbo-preview, ...)
DEFAULT_KIMI_MODEL = "kimi-k2.5"
# AWS Bedrock model ID for Kimi K2.5 (Moonshot on Bedrock)
BEDROCK_KIMI_MODEL_ID = "moonshotai.kimi-k2.5"

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


def get_kimi_api_key() -> str:
    """Return empty string; kept for caller compatibility. Kimi uses Bedrock only (no API key)."""
    return (os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY") or "").strip()


def _get_bedrock_region() -> str:
    """Return AWS region for Bedrock from BEDROCK_REGION or AWS_REGION."""
    return (
        (os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "").strip()
    )


def _task_id(d: Dict[str, Any]) -> Any:
    return d.get("task_id") or d.get("taskId")


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
    # Strip markdown code block if present
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            text = match.group(1).strip()
    if not text:
        return {}
    # Find first { or [ and parse from there
    start = -1
    for i, c in enumerate(text):
        if c == "{" or c == "[":
            start = i
            break
    if start == -1:
        return {}
    text = text[start:]
    # Find matching closing bracket
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
        return json.loads(text[:end])
    except json.JSONDecodeError:
        return {}


# =============================================================================
# SYSTEM PROMPTS
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

# ---------------------------------------------------------------------------
# Evaluation rubrics
# ---------------------------------------------------------------------------

_STRICT_HEADER = """
You are an expert LLM response evaluator performing side-by-side (SxS) ratings on prompts/responses.
Treat this entire system message as instructions. Every line is part of the evaluation rules.
Evaluate strictly and critically. Do not give the benefit of the doubt. When in doubt, choose the lower score.
"""

_RUBRICS_INTRO = """
## Evaluation Rubrics (Score 1-6)

**CRITICAL: SCORE DISTRIBUTION EXPECTATIONS**
- Scores 5-6: Should be RARE (<15% of evaluations) — reserved for truly exceptional responses
- Score 4: The TYPICAL score for good responses with minor issues
- Score 3: COMMON for average responses with noticeable gaps
- Scores 1-2: For clearly flawed responses

**ANTI-INFLATION RULES:**
1. START at score 3 or 4, then adjust based on evidence
2. You MUST cite specific flaws/gaps to justify NOT deducting points
3. For ANY score of 5 or 6, you MUST provide explicit justification why there are no issues
4. When uncertain between two scores, ALWAYS choose the lower one
5. Generic praise like "well-written" or "comprehensive" without specific evidence = score 4 max

Apply these rubrics ONLY to the pre-loaded Response A and Response B.
"""

RUBRIC_INSTRUCTION_FOLLOWING = """
### 1. Instruction Following (IF) — Weight: 0.25

**SCORING CALIBRATION:** Most responses score 3-4. Score 5-6 is rare and requires perfect adherence.

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

RUBRIC_TRUTHFULNESS = """
### 2. Truthfulness — Weight: 0.25

**CRITICAL SCORING CALIBRATION:**
- Scores of 5-6 are EXCEPTIONAL and should be given to fewer than 10% of responses
- Most responses contain at least minor issues and should score 3-4
- You MUST start at score 4 and only move UP if you can prove every claim is verifiable
- Default assumption: the response likely contains errors until proven otherwise

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

| Score | Criteria | When to Use |
|-------|----------|-------------|
| 6 | Every single claim is verifiable and verified. Zero errors. | RARE |
| 5 | All major claims verified, only 1 trivial imprecision | UNCOMMON |
| 4 | Generally accurate, but 1-2 unverifiable or weakly supported claims | TYPICAL |
| 3 | Mix of correct and questionable claims; some unsupported statements | COMMON |
| 2 | Multiple errors or misleading claims; unreliable overall | For responses with clear factual mistakes |
| 1 | Major fabrications or contradicts established facts | For hallucinated or false content |

**BEFORE SCORING 5 OR 6:** Cite the specific verifiable source for each major claim. If you cannot, score MUST be 4 or below.
"""

RUBRIC_PROMPT_CORRECTNESS = """
### 3. Prompt Correctness (Does it answer the prompt?) — Weight: 0.20

**SCORING CALIBRATION:** Fully correct answers are uncommon. Most responses have gaps or minor errors (score 3-4).

**MANDATORY CORRECTNESS AUDIT:**
1. Break down the prompt into distinct questions/requirements
2. For each requirement, mark: [CORRECT], [PARTIALLY CORRECT], [INCORRECT], [MISSING]
3. Verify factual claims against known standards
4. Check for outdated information (especially in tech/science domains)

**DEDUCTION TRIGGERS:**
- Any [PARTIALLY CORRECT] element → cap at 4
- Any [INCORRECT] element → cap at 3
- Any [MISSING] element → -1 point
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

**BEFORE SCORING 5 OR 6:** List each prompt requirement and show how it was correctly answered.
"""

RUBRIC_WRITING_STYLE = """
### 4. Writing Style (Writing Quality) — Weight: 0.15

**SCORING CALIBRATION:** Most responses score 3-4. Professional writing with zero issues is rare.

**MANDATORY QUALITY AUDIT (must complete before scoring):**
1. Read for: clarity, structure, tone, grammar, audience-appropriateness
2. Identify AT LEAST ONE area for improvement (even minor)
3. If you cannot find any issue, re-read more critically before scoring 5+

**DEDUCTION TRIGGERS (each one = -1 point from starting score of 4):**
- Run-on sentences or overly complex sentence structure → -1
- Inconsistent tone (formal/informal mix) → -1
- Missing paragraph breaks or poor organization → -1
- Grammar errors (even minor) → -1
- Jargon without explanation for general audience → -1
- Passive voice overuse → -1
- Redundant phrasing → -1

| Score | Criteria | Issue Count |
|-------|----------|-------------|
| 6 | Publication-ready; would not change a single word | 0 issues |
| 5 | Professional quality; only 1 trivial style preference | 1 minor issue |
| 4 | Good but has identifiable style/structure weaknesses | 2-3 minor issues |
| 3 | Readable but noticeably awkward or unclear sections | 4+ minor or 1 major issue |
| 2 | Difficult to follow; significant clarity problems | Multiple major issues |
| 1 | Unintelligible or unacceptable | Pervasive problems |

**BEFORE SCORING 5 OR 6:** Quote the specific phrases that demonstrate exceptional quality.
"""

RUBRIC_VERBOSITY = """
### 5. Verbosity (appropriateness of length) — Weight: 0.15

**SCORING CALIBRATION:** Perfect length calibration is rare. Most responses are slightly too long or too short (score 3-4).

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

**BEFORE SCORING 5 OR 6:** State "Nothing can be cut because..." AND "Nothing is missing because..."
"""

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

EVALUATION_RUBRICS = (
    _RUBRICS_INTRO
    + RUBRIC_INSTRUCTION_FOLLOWING
    + RUBRIC_TRUTHFULNESS
    + RUBRIC_PROMPT_CORRECTNESS
    + RUBRIC_WRITING_STYLE
    + RUBRIC_VERBOSITY
    + RUBRIC_OVERALL_QUALITY
)

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

_ALL_DIMENSIONS_TASK = """
---
TASK: EVALUATE_ALL_DIMENSIONS
Evaluate ALL five dimensions for Response A and Response B in one pass:
instruction_following, truthfulness, prompt_correctness, writing_style, verbosity.

For EACH dimension, output the exact dimension key (snake_case) with { "score": <1-6>, "reason": "<cite specific evidence or fault>" }.

Output JSON (all five dimension keys in each of response_a and response_b):
{
  "response_a": {
    "instruction_following": { "score": <1-6>, "reason": "..." },
    "truthfulness": { "score": <1-6>, "reason": "..." },
    "prompt_correctness": { "score": <1-6>, "reason": "..." },
    "writing_style": { "score": <1-6>, "reason": "..." },
    "verbosity": { "score": <1-6>, "reason": "..." }
  },
  "response_b": {
    "instruction_following": { "score": <1-6>, "reason": "..." },
    "truthfulness": { "score": <1-6>, "reason": "..." },
    "prompt_correctness": { "score": <1-6>, "reason": "..." },
    "writing_style": { "score": <1-6>, "reason": "..." },
    "verbosity": { "score": <1-6>, "reason": "..." }
  }
}
Return ONLY valid JSON. Use the exact keys above.
"""


def get_evaluation_system_prompt_all_dimensions() -> str:
    """System prompt to evaluate all 5 dimensions in a single call."""
    rubrics = _RUBRICS_INTRO
    for dk in DIMENSION_KEYS:
        rubrics += (DIMENSION_RUBRICS.get(dk, "") or "")
    return (
        _STRICT_HEADER
        + """
*** SCORING RULES (MANDATORY — ENFORCE STRICTLY) ***
- Assume 3 or 4 for each dimension unless you have cited, overwhelming evidence of perfection. Most scores MUST be 3 or 4.
- Reserve 5 only for near-perfect performance with at most trivial flaws; reserve 6 only when there is zero room for criticism. Do not default to 5 or 6.
- When in doubt or borderline, always choose the lower score. Err heavily on the side of strictness.

*** FAULT-FINDING (MANDATORY) ***
For EACH dimension, you MUST:
1. Identify what is wrong, missing, or improvable in each response.
2. In the "reason" field, cite that fault with specific evidence.
3. Only if you find nothing meaningful wrong may you consider 5 or 6; state in the reason what fault you considered and why it does not apply.

"""
        + rubrics
        + _ALL_DIMENSIONS_TASK
    )


def get_evaluation_system_prompt_for_dimension(dim_key: str) -> str:
    rubric = DIMENSION_RUBRICS.get(dim_key, "")
    return (
        _STRICT_HEADER
        + """
*** SCORING RULES (MANDATORY — ENFORCE STRICTLY) ***
- Assume 3 or 4 for this dimension unless you have cited, overwhelming evidence of perfection. Most scores for this dimension MUST be 3 or 4.
- Reserve 5 only for near-perfect performance with at most trivial flaws; reserve 6 only when there is zero room for criticism and you can cite concrete evidence. Do not default to 5 or 6.
- When in doubt or borderline, always choose the lower score. Err heavily on the side of strictness.

*** FAULT-FINDING (MANDATORY) ***
For this dimension, you MUST:
1. First identify what is wrong, missing, or improvable (a flaw, gap, or weakness) in each response.
2. In the "reason" field, cite that fault: state what you looked for and what you found (e.g., "Partially follows but omits X", "Unverified claim about Y").
3. Only if you find nothing meaningful wrong and can cite concrete evidence may you consider 5 or 6. Before giving 5 or 6, you MUST state in the reason what fault you considered and why it does not apply.
If you cannot name a specific fault you considered for this dimension, assign 4 or lower. Do not give 5 or 6 without explicitly stating in the reason what weakness you considered and rejected.

"""
        + rubric
        + _ONE_DIMENSION_TASK
    )


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

# ---------------------------------------------------------------------------
# Simplified QC system prompt
# ---------------------------------------------------------------------------

QC_SYSTEM_PROMPT_SIMPLE = """
You are an expert Quality Control auditor for the Valor evaluation pipeline.
Be thorough and precise. Flag genuine, clear inconsistencies between the human
rater's scores, preference, and comment. Do NOT fabricate issues or flag
borderline cases as failures — only flag contradictions that are unambiguous.

You will receive a record containing:
- evaluation_result: the human rater's per-dimension SCORES (1-6) for Response A
  and Response B across five dimensions (instruction_following, truthfulness,
  prompt_correctness, writing_style, verbosity). The "reason" fields are
  AI-generated reference analyses — they are NOT written by the human rater.
- ab_preference: the human rater's overall A-vs-B preference (-3 to +3).
- ab_comment: the human rater's written justification for that preference.
- comparison_ab: an AI-generated reference comparison (ignore for QC decisions;
  use only as supplementary context if needed).
- prompt, response_a, response_b: the original texts being evaluated.

Your job is to check that the HUMAN-ENTERED data (scores, ab_preference,
ab_comment) is internally consistent and properly grounded. Perform exactly
THREE quality checks.

================================================================================
QC CHECK CATEGORIES
================================================================================

--------------------------------------------------------------------------------
CHECK 1: AI-GENERATED TEXT DETECTION (Major)
--------------------------------------------------------------------------------
Scan the ab_comment field for signs of AI-generated writing.

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

Severity:
  - 3+ prohibited words/phrases found → Major (severity 1)
  - 1-2 prohibited words/phrases found → Minor (severity 2)
  - Structural signals only (no prohibited words) → Advisory (severity 3)

--------------------------------------------------------------------------------
CHECK 2: SCORE AND PREFERENCE CONSISTENCY (Major)
--------------------------------------------------------------------------------
All of the following use ONLY the human rater's scores — not the AI-generated
reasons or comparison_ab.

Step A — COMPUTE THE AGGREGATE OVERALL SCORE for each response from the human's
five dimension scores using:
  aggregate_A = IF_A*0.25 + Truth_A*0.25 + Correctness_A*0.20 + Writing_A*0.15 + Verbosity_A*0.15
  aggregate_B = IF_B*0.25 + Truth_B*0.25 + Correctness_B*0.20 + Writing_B*0.15 + Verbosity_B*0.15
(These are computed by YOU from the human's scores; they are not in the input.)

Step B — CHECK THAT ab_preference DIRECTION MATCHES THE SCORES:
  - If aggregate_A > aggregate_B by ≥ 0.5, then ab_preference SHOULD be negative
    (A preferred: -1, -2, or -3).
  - If aggregate_B > aggregate_A by ≥ 0.5, then ab_preference SHOULD be positive
    (B preferred: +1, +2, or +3).
  - If |aggregate_A − aggregate_B| < 0.5, ab_preference of 0 is acceptable, and
    a slight lean (-1 or +1) in either direction is also acceptable since scores
    are close.
  - Only flag a CLEAR contradiction: e.g. aggregate_A is higher by ≥ 1.0 but
    ab_preference is positive (favors B), or vice versa.

Step C — CHECK ab_preference MAGNITUDE (advisory only, do NOT fail for this):
  - |aggregate difference| ≥ 2.0 → preference magnitude should ideally be ±2 or ±3.
  - Otherwise, any magnitude ±1 through ±3 is acceptable.
  - Human raters may have legitimate reasons for mild or strong preferences that
    don't perfectly track the weighted average. Only note this as advisory.

IMPORTANT: Do NOT independently re-evaluate the responses yourself. Your job is
to check INTERNAL CONSISTENCY between the human's own scores, preference, and
comment — not to second-guess the human's judgment on individual dimensions.

Failure conditions:
  - ab_preference direction clearly contradicts score aggregates (gap ≥ 1.0 and
    preference goes the wrong way) → Major (severity 1)
  - ab_preference magnitude wildly inconsistent (gap ≥ 2.0 and pref is 0) →
    Minor (severity 2)

--------------------------------------------------------------------------------
CHECK 3: AB COMMENT GROUNDING (Major) — THREE SUB-CHECKS
--------------------------------------------------------------------------------
The human's ab_comment must be consistent and grounded along three lines.

3a) PREFERENCE DIRECTION MATCH
  - ab_preference scale: -1 to -3 = A preferred; +1 to +3 = B preferred; 0 = tie.
  - ab_comment must clearly state which response is preferred and that verdict
    must align with the SIGN of ab_preference.
  - If ab_comment says A is better → ab_preference must be negative.
  - If ab_comment says B is better → ab_preference must be positive.
  - If ab_comment says tie/similar → ab_preference must be 0.
  - If ab_comment is missing, empty, or non-substantive (e.g. "Placeholder",
    "N/A", or text that does not contain a real comparison or justification),
    FAIL with severity 1 and set status.issue to explain. QC must never pass
    when comment and ratings cannot be meaningfully compared.

3b) GROUNDED IN DIMENSION SCORES
  - Use ONLY the human's per-dimension scores (not the AI reasons).
  - Every substantive claim in ab_comment (e.g. "A was more truthful",
    "B followed instructions better", "A had clearer writing") must be
    supported by the corresponding human dimension scores.
    Example: if ab_comment claims "A is more truthful", then the human's
    truthfulness_a score must be higher than truthfulness_b.
  - ab_comment must not assert differences that contradict the human's own
    scores. If the scores show response A is better on a dimension but the
    comment says B is better on that dimension, that is a contradiction.
  - Also check that the overall TONE of ab_comment (which response is
    presented as stronger overall) is consistent with which response has
    the higher aggregate score (from Step A of CHECK 2).

3c) GROUNDED IN RESPONSE TEXTS
  - ab_comment must reference actual content from the provided response_a and
    response_b.
  - Claims about what A or B said, or specific phrases/examples cited, must
    be present in the actual response text.
  - Fail if ab_comment cites content, quotes, or claims that are not present
    in response_a or response_b.

Failure conditions (overall CHECK 3):
  - 3a: ab_preference sign contradicts ab_comment verdict → Major (severity 1)
  - 3b: ab_comment contradicts the human's own dimension scores → Major (severity 1)
  - 3c: ab_comment cites content not present in responses → Major (severity 1)
  - 3a: ab_preference magnitude inconsistent with comment strength → Minor (severity 2)
  - 3b: ab_comment only loosely tied to dimension scores → Minor (severity 2)
  - 3c: ab_comment only loosely grounded in response text → Minor (severity 2)

================================================================================
SEVERITY LEVELS
================================================================================
| Severity | Level | Description                                    | Action              |
|----------|-------|------------------------------------------------|---------------------|
| Major    | 1     | Contradiction between scores/preference/comment | Return for correction |
| Minor    | 2     | Weak grounding, loose consistency               | Fix during QC       |
| Advisory | 3     | Style-only AI signals                           | Note for feedback   |

================================================================================
DECISION LOGIC
================================================================================
- AI detection (CHECK 1) is for reporting only: does NOT affect pass/fail.
- For pass/fail, consider only: dimension_score_consistency (CHECK 2),
  ab_preference_comment_grounding (CHECK 3).
- If EITHER check has result = "fail" with severity 1 → qc_status = "QC_Fail"
- If both checks have result = "pass" or severity 2-3 → qc_status = "QC_Pass"
- overall_severity = lowest (most severe) severity among those two checks, or 3
  if both pass
- IMPORTANT: Set status.result to "fail" ONLY for clear, unambiguous
  contradictions. Borderline or debatable cases should be result "pass" with
  an advisory note in the issue field.

================================================================================
OUTPUT FORMAT (STRICT JSON ONLY)
================================================================================
- All information related to ab_comment must be returned in a single key "ab_comment"
  with a list of strings (no subkeys). Each string is one finding, e.g.:
  "preference_matches_comment: pass",
  "grounded_in_dimension_ratings: pass" or "grounded_in_dimension_ratings: fail - <brief issue>",
  "grounded_in_responses: pass" or "grounded_in_responses: fail - <brief issue>",
  "ai_flagged: <comma-separated detected words/phrases>" (only if CHECK 1 found any; else omit this line)

{
  "qc_status": "QC_Pass" or "QC_Fail",
  "overall_severity": <1-3>,
  "ab_comment": [
    "preference_matches_comment: pass",
    "grounded_in_dimension_ratings: pass",
    "grounded_in_responses: pass",
    "ai_flagged: delve, nuanced"
  ],
  "checks": {
    "ai_detection": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
      "severity": <1-3 or null if pass>,
      "structural_signals": ["<signal description>", ...]
    },
    "dimension_score_consistency": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
      "severity": <1-3 or null if pass>,
      "details": {
        "response_a": {"issues": ["<dimension: issue description>", ...]},
        "response_b": {"issues": ["<dimension: issue description>", ...]},
        "weighted_score_check": {"result": "pass" or "fail", "issue": ""}
      }
    },
    "ab_preference_comment_grounding": {
      "status": {"result": "pass", "issue": ""} or {"result": "fail", "issue": "<explanation>"},
      "severity": <1-3 or null if pass>
    }
  },
  "summary": "<1-2 sentence summary>"
}

Return ONLY valid JSON. No text outside JSON structure.
"""


FOLLOW_UP_PROMPT_GENERATION_SYSTEM_PROMPT = """
You are an expert at generating natural, conversational follow-up prompts for model evaluation.

You will be given one or more conversation TURNS. Each turn has:
- USER PROMPT (what the user asked).
- MODEL RESPONSE (what the model replied).

Your task: Generate a single, natural follow-up prompt that a human user might ask next. The follow-up must be MEDIUM to DIFFICULT in difficulty so that it:
- Challenges the model: ask for precision, edge cases, counterexamples, or trade-offs the previous response did not fully address.
- Tests depth: request step-by-step reasoning, concrete examples, or verification of claims (citations, sources, or "how do you know?").
- Exposes gaps: ask for clarification of vague parts, probe assumptions, or request the same idea in a different form (simplify, formalize, or apply to a new case).
- Encourages multi-turn improvement: the prompt should be answerable but non-trivial, so the model may need to refine or correct itself over further turns.

Keep the prompt natural and conversational (one to a few sentences). Stay in English. Do not repeat earlier prompts. Do not add meta-commentary (e.g. "The user might say:"). Output ONLY the follow-up prompt text—no quotes, no prefix, no explanation.
"""


FOLLOW_UP_RELEVANCE_CHECK_SYSTEM_PROMPT = """
You are an expert judge for multi-turn conversation quality.

You will be given:
1. DIALOG HISTORY: The full conversation so far, as one or more TURNS. Each turn has:
   - USER PROMPT: What the user asked in that turn.
   - MODEL RESPONSE: The model's reply in that turn (the chosen/winning response for that turn).
2. FOLLOW-UP PROMPT: A new user prompt that is supposed to continue the conversation.

Your task: Determine whether the FOLLOW-UP PROMPT is RELEVANT to the ENTIRE dialog history.

Check the follow-up against EVERYTHING in the conversation so far:
- It must be relevant to the topic, task, or thread established across all previous turns.
- It should build on, deepen, or challenge what was said in the conversation (e.g. clarification, examples, edge cases, or correctness of earlier answers).
- It must not abruptly switch to an unrelated topic or ignore what was already discussed in any turn.

RELEVANT means:
- The follow-up naturally continues the same topic or thread established in the dialog history.
- It connects to at least one earlier turn (prompt and/or response) in a clear way.
- It does not contradict or ignore the existing conversation.

NOT RELEVANT means:
- The follow-up is about a different subject with no clear link to any part of the dialog history.
- It ignores or repeats earlier content without building on it.
- It is generic and could apply to any conversation (e.g. "Tell me more" with no reference to prior content).

Output strict JSON only:
{
  "is_relevant": true or false,
  "reason": "<brief explanation: why the follow-up is or is not relevant to the full dialog history>"
}

Return ONLY valid JSON. No text outside the JSON.
"""


# =============================================================================
# CORE KIMI API CALL (Moonshot HTTP + AWS Bedrock Converse)
# =============================================================================

def call_bedrock_converse_sync(
    region_name: str,
    model_id: str,
    system_prompt: str,
    user_content: str,
    response_format: str = "text",
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Single synchronous chat completion via AWS Bedrock Converse API.
    Returns same shape as call_kimi_sync: {"text": str, "usage": {"input_tokens", "output_tokens", "cached_tokens"}}.
    """
    if not boto3:
        raise RuntimeError("boto3 is required for Bedrock; pip install boto3")
    client = boto3.client("bedrock-runtime", region_name=region_name)
    kwargs: Dict[str, Any] = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": user_content}]}],
        "inferenceConfig": {"maxTokens": 8192},
    }
    if system_prompt:
        kwargs["system"] = [{"text": system_prompt}]
    if temperature is not None:
        kwargs["inferenceConfig"]["temperature"] = float(temperature)
    # JSON output: rely on prompt "Return ONLY valid JSON" if model has no separate JSON mode
    try:
        response = client.converse(**kwargs)
    except Exception as e:
        _logger.warning("Bedrock Converse request failed: %s", e)
        raise
    output = response.get("output") or {}
    message = output.get("message") or {}
    content_blocks = message.get("content") or []
    text = "".join(
        (b.get("text") or "") for b in content_blocks if isinstance(b, dict) and "text" in b
    ).strip()
    usage_raw = response.get("usage") or {}
    usage = {
        "input_tokens": int(usage_raw.get("inputTokens", 0)),
        "output_tokens": int(usage_raw.get("outputTokens", 0)),
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
    Single synchronous chat completion via AWS Bedrock Converse API only.
    api_key is ignored (kept for caller compatibility). Returns {"text": str, "usage": {...}}.
    """
    region = _get_bedrock_region()
    if not region:
        raise ValueError("BEDROCK_REGION or AWS_REGION must be set for Kimi (Bedrock)")
    model_id = BEDROCK_KIMI_MODEL_ID if model == DEFAULT_KIMI_MODEL else model
    return call_bedrock_converse_sync(
        region_name=region,
        model_id=model_id,
        system_prompt=system_prompt,
        user_content=user_content,
        response_format=response_format,
        temperature=temperature,
    )


# =============================================================================
# PARALLEL RUNNER
# =============================================================================

def _kimi_run_requests_parallel(
    kimi_api_key: str,
    model: str,
    requests_config: List[Dict[str, Any]],
    max_workers: int = 32,
) -> Dict[str, Dict[str, Any]]:
    """
    Run a list of requests via call_kimi_sync sequentially.
    Each request: {key, system_prompt, user_content, response_format, temperature}.
    Returns {key: {"parsed": ..., "error": ...}}.
    """
    if not requests_config:
        return {}
    result_by_key: Dict[str, Dict[str, Any]] = {}

    for req in requests_config:
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
            parsed = _parse_json_from_response(text) if req.get("response_format") == "json" else {}
            result_by_key[key] = {"parsed": parsed, "error": None}
        except Exception as e:
            result_by_key[key] = {"parsed": {}, "error": {"message": str(e)}}

    return result_by_key


# =============================================================================
# DIMENSION RESULT BUILDER
# =============================================================================

def _build_evaluation_result_from_dimensions(
    dimension_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge 5 per-dimension parsed responses into evaluation_result with overall_quality."""
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


# =============================================================================
# 1. PROMPT REJECTION
# =============================================================================

def run_prompt_rejection_kimi(
    kimi_api_key: str,
    tasks: List[Dict[str, Any]],
    model: str = DEFAULT_KIMI_MODEL,
    max_workers: int = 16,
) -> List[Dict[str, Any]]:
    """
    Run prompt rejection checks in parallel via Kimi.

    Input:  list of dicts with at least {"prompt": str} (and optionally "task_id")
    Output: same list with added keys: rejection_status, rejection_reason, justification, accepted
    """
    if not tasks:
        return []

    requests_config = []
    for i, task in enumerate(tasks):
        prompt = task.get("prompt", "")
        requests_config.append({
            "key": str(i),
            "system_prompt": PROMPT_REJECTION_SYSTEM_PROMPT,
            "user_content": f"USER PROMPT:\n{prompt}",
            "response_format": "json",
            "temperature": 0.2,
        })

    raw = _kimi_run_requests_parallel(kimi_api_key, model, requests_config, max_workers=max_workers)

    results = []
    for i, task in enumerate(tasks):
        parsed = (raw.get(str(i), {}).get("parsed")) or {}
        error = raw.get(str(i), {}).get("error")
        status = (parsed.get("status") or "REJECT").upper()
        accepted = status == "ACCEPT"

        result = dict(task)
        result.update({
            "rejection_status": status,
            "rejection_reason": parsed.get("rejection_reason"),
            "justification": parsed.get("justification", ""),
            "accepted": accepted,
        })
        if error:
            result["rejection_error"] = error
            result["rejection_status"] = "REJECT"
            result["accepted"] = False
        results.append(result)

    return results


# =============================================================================
# 2. EVALUATION (5 dimensions + COMPARE_TWO)
# =============================================================================

def run_evaluation_kimi(
    kimi_api_key: str,
    evaluation_inputs: List[Dict[str, Any]],
    model: str = DEFAULT_KIMI_MODEL,
    max_workers: int = 32,
) -> List[Dict[str, Any]]:
    """
    Run evaluation via Kimi: one call for all 5 dimensions per task, one call for COMPARE_TWO per task.
    No parallel threads; all calls run sequentially.

    Input:  list of dicts with {task_id, prompt, response_a, response_b}
    Output: list of dicts with {task_id, prompt, evaluation_result, comparison_ab, sxs_winner_label}
    """
    if not evaluation_inputs:
        return []

    all_dims_system = get_evaluation_system_prompt_all_dimensions()
    out = []

    for idx, inp in enumerate(evaluation_inputs):
        tid = str(_task_id(inp) or idx)
        prompt = inp.get("prompt", "")
        resp_a = inp.get("response_a", "")
        resp_b = inp.get("response_b", "")

        user_content = (
            f"USER PROMPT:\n{prompt}\n\n"
            f"--------------------\nRESPONSE A:\n{resp_a}\n\n"
            f"--------------------\nRESPONSE B:\n{resp_b}"
        )

        # Single call for all 5 dimensions
        try:
            r = call_kimi_sync(
                kimi_api_key,
                model,
                all_dims_system,
                user_content,
                response_format="json",
                temperature=0.2,
            )
            text = (r.get("text") or "").strip()
            full_parsed = _parse_json_from_response(text)
        except Exception as e:
            _logger.warning("Kimi all-dimensions call failed for task %s: %s", tid, e)
            full_parsed = {}

        # Build dimension_results list from single response (for _build_evaluation_result_from_dimensions)
        ra = full_parsed.get("response_a") or {}
        rb = full_parsed.get("response_b") or {}
        dimension_results = [
            {"response_a": {dk: (ra.get(dk) or {})}, "response_b": {dk: (rb.get(dk) or {})}}
            for dk in DIMENSION_KEYS
        ]
        evaluation_result = _build_evaluation_result_from_dimensions(dimension_results)

        # Single call for COMPARE_TWO
        compare_content = (
            "TASK: COMPARE_TWO\n\n"
            f"USER PROMPT:\n{prompt}\n\n"
            f"--------------------\nRESPONSE A:\n{resp_a}\n\n"
            f"--------------------\nRESPONSE B:\n{resp_b}"
        )
        try:
            r_compare = call_kimi_sync(
                kimi_api_key,
                model,
                EVALUATION_SYSTEM_PROMPT_COMPARE_TWO,
                compare_content,
                response_format="json",
                temperature=0.2,
            )
            text_c = (r_compare.get("text") or "").strip()
            pc = _parse_json_from_response(text_c)
        except Exception as e:
            _logger.warning("Kimi compare-two call failed for task %s: %s", tid, e)
            pc = {}

        comparison_ab = {
            "comparison_score": pc.get("comparison_score", 0),
            "overall_comment": pc.get("overall_comment", ""),
        }
        comp_score = comparison_ab.get("comparison_score", 0)
        sxs_winner_label = "Response A" if comp_score <= 0 else "Response B"

        out.append({
            "task_id": _task_id(inp),
            "prompt": inp.get("prompt", ""),
            "evaluation_result": evaluation_result,
            "comparison_ab": comparison_ab,
            "sxs_winner_label": sxs_winner_label,
        })

    return out


# =============================================================================
# 3. FULL PIPELINE (rejection → evaluation)
# =============================================================================

def run_full_pipeline_kimi(
    kimi_api_key: str,
    tasks: List[Dict[str, Any]],
    model: str = DEFAULT_KIMI_MODEL,
    max_workers: int = 32,
) -> Dict[str, Any]:
    """
    Full simplified pipeline:
      1. Prompt rejection check
      2. Evaluation (5 dimensions + AB preference) for accepted prompts

    Input:  list of dicts with {task_id, prompt, response_a, response_b}
    Output: dict with rejection_results, accepted_indices, evaluation_results
    """
    if not tasks:
        return {"rejection_results": [], "accepted_indices": [], "evaluation_results": []}

    # Step 1: Rejection
    rejection_results = run_prompt_rejection_kimi(kimi_api_key, tasks, model=model, max_workers=max_workers)

    # Filter accepted
    accepted_indices = [i for i, r in enumerate(rejection_results) if r.get("accepted")]
    accepted_tasks = [tasks[i] for i in accepted_indices]

    if not accepted_tasks:
        return {
            "rejection_results": rejection_results,
            "accepted_indices": accepted_indices,
            "evaluation_results": [],
        }

    # Step 2: Evaluation
    evaluation_results = run_evaluation_kimi(kimi_api_key, accepted_tasks, model=model, max_workers=max_workers)

    return {
        "rejection_results": rejection_results,
        "accepted_indices": accepted_indices,
        "evaluation_results": evaluation_results,
    }


# =============================================================================
# 4. QC CHECKS
# =============================================================================

_QC_EM_DASH_PATTERN = re.compile(r"\u2014")


def _qc_input_has_em_dash(inp: Dict[str, Any]) -> bool:
    val = inp.get("ab_comment", "")
    return bool(isinstance(val, str) and _QC_EM_DASH_PATTERN.search(val))


def _qc_ab_comment_is_placeholder_or_empty(ab_comment: Any) -> bool:
    """True if ab_comment is effectively placeholder/empty/non-substantive (must fail QC)."""
    if ab_comment is None:
        return True
    if not isinstance(ab_comment, str):
        return True
    s = ab_comment.strip().lower()
    if not s:
        return True
    # Common placeholders or non-substantive text
    if s in ("placeholder", "place holder", "n/a", "na", "tbd", "todo", "none", "."):
        return True
    if len(s) < 10 and s.replace(".", "").replace(",", "").strip() in ("placeholder", "n/a", "na"):
        return True
    return False


def run_qc_kimi(
    kimi_api_key: str,
    qc_inputs: List[Dict[str, Any]],
    model: str = DEFAULT_KIMI_MODEL,
    max_workers: int = 16,
) -> List[Dict[str, Any]]:
    """
    Run simplified QC checks in parallel via Kimi.

    Input: list of dicts, each containing:
      - task_id
      - prompt, response_a, response_b (original texts)
      - evaluation_result (response_a/response_b with dimension scores)
      - comparison_ab (comparison_score, overall_comment)
      - ab_preference (human rating, -3 to +3)
      - ab_comment (human justification)

    Output: list of QC result dicts per task
    """
    if not qc_inputs:
        return []

    requests_config = []
    for i, inp in enumerate(qc_inputs):
        user_content = f"EVALUATION RECORD TO QC:\n{json.dumps(inp, indent=2)}"
        requests_config.append({
            "key": str(i),
            "system_prompt": QC_SYSTEM_PROMPT_SIMPLE,
            "user_content": user_content,
            "response_format": "json",
            "temperature": 0.2,
        })

    raw = _kimi_run_requests_parallel(kimi_api_key, model, requests_config, max_workers=max_workers)

    results = []
    for i, inp in enumerate(qc_inputs):
        task_id = _task_id(inp)
        r = raw.get(str(i), {"parsed": {}, "error": {"message": "missing"}})
        parsed = r.get("parsed") or {}
        err = r.get("error")

        if err:
            results.append({
                "task_id": task_id,
                "qc_status": "QC_Fail",
                "overall_severity": 1,
                "checks": {},
                "summary": f"QC API call failed: {err.get('message', '')}",
                "error": err,
            })
        else:
            result = {
                "task_id": task_id,
                "qc_status": parsed.get("qc_status", "QC_Fail"),
                "overall_severity": parsed.get("overall_severity", 1),
                "checks": parsed.get("checks", {}),
                "summary": parsed.get("summary", ""),
                "ab_comment": list(parsed.get("ab_comment", [])),
                "error": None,
            }
            if _qc_input_has_em_dash(inp):
                result["qc_status"] = "QC_Fail"
                result["overall_severity"] = 1
                result["summary"] = "Em-dash detected in ab_comment."
                result["failure_reason"] = "em-dash"
            # Force fail when ab_comment is placeholder/empty/non-substantive (comment and ratings cannot match)
            if _qc_ab_comment_is_placeholder_or_empty(inp.get("ab_comment")):
                result["qc_status"] = "QC_Fail"
                result["overall_severity"] = 1
                result["summary"] = result.get("summary") or "ab_comment is placeholder or non-substantive."
                result["failure_reason"] = "placeholder_or_empty"
                if not any("placeholder" in str(line).lower() or "non-substantive" in str(line).lower() for line in result["ab_comment"]):
                    result["ab_comment"].insert(0, "preference_matches_comment: fail - ab_comment is placeholder or non-substantive; cannot match ratings.")
            ab_grounding = (result.get("checks") or {}).get("ab_preference_comment_grounding") or {}
            grounding_result = (ab_grounding.get("status") or {}).get("result") or ""
            grounding_issue = (ab_grounding.get("status") or {}).get("issue") or ""
            if grounding_result == "fail" and isinstance(grounding_issue, str) and grounding_issue.strip():
                result["qc_status"] = "QC_Fail"
                result["overall_severity"] = min(result.get("overall_severity", 3), 1)
                result["summary"] = result.get("summary") or grounding_issue.strip()
                if result["ab_comment"] and not any("fail - " in str(line) for line in result["ab_comment"]):
                    result["ab_comment"].append("preference_matches_comment: fail - " + grounding_issue.strip())
            results.append(result)

    return results


# =============================================================================
# 5. FOLLOW-UP PROMPT GENERATION
# =============================================================================

def generate_follow_up_prompt_kimi(
    kimi_api_key: str,
    conversation_turns: List[Tuple[str, str]],
    model: str = DEFAULT_KIMI_MODEL,
    temperature: float = 0.5,
) -> Dict[str, Any]:
    """
    Generate a natural follow-up user prompt from a list of (prompt, response) pairs
    using Kimi. Each turn has one user prompt and one model response. Any number
    of turns can be provided; the follow-up is generated from the full context.

    Args:
        kimi_api_key: Kimi (Moonshot) API key.
        conversation_turns: List of (user_prompt, model_response) pairs. Can be
            any length (0, 1, 2, ...). Empty list: model will generate an opening prompt.
        model: Kimi model name.
        temperature: Sampling temperature for generation.

    Returns:
        {
            "follow_up_prompt": str,
            "usage": {"input_tokens", "output_tokens", "cached_tokens"},
            "error": optional error dict if the call failed,
        }
    """
    if not isinstance(conversation_turns, list):
        return {
            "follow_up_prompt": "",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
            "error": {"message": "conversation_turns must be a list of (prompt, response) pairs"},
        }

    parts = []
    if not conversation_turns:
        parts.append("No prior conversation turns provided.")
        parts.append("Generate a natural opening user prompt that is MEDIUM to DIFFICULT: a substantive question or request that will challenge the model (e.g. multi-part, requires reasoning, or has a non-obvious answer).")
    else:
        for i, (user_prompt, model_response) in enumerate(conversation_turns, start=1):
            user_prompt = (user_prompt or "").strip()
            model_response = (model_response or "").strip()
            parts.append(f"--- TURN {i} ---")
            parts.append("USER PROMPT:")
            parts.append(user_prompt)
            parts.append("")
            parts.append("MODEL RESPONSE:")
            parts.append(model_response)
            parts.append("")
        parts.append("Generate the follow-up user prompt (output only the prompt text):")

    user_content = "\n".join(parts)

    try:
        r = call_kimi_sync(
            kimi_api_key,
            model,
            FOLLOW_UP_PROMPT_GENERATION_SYSTEM_PROMPT,
            user_content,
            response_format="text",
            temperature=temperature,
        )
        text = (r.get("text") or "").strip()
        usage = r.get("usage") or {}
        return {
            "follow_up_prompt": text,
            "usage": {
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "cached_tokens": int(usage.get("cached_tokens", 0)),
            },
            "error": None,
        }
    except Exception as e:
        _logger.exception("generate_follow_up_prompt_kimi failed")
        return {
            "follow_up_prompt": "",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
            "error": {"message": str(e)},
        }


# =============================================================================
# 6. FOLLOW-UP RELEVANCE CHECK
# =============================================================================

def check_follow_up_relevance_kimi(
    kimi_api_key: str,
    dialog_history: List[Tuple[str, str]],
    follow_up_prompt: str,
    model: str = DEFAULT_KIMI_MODEL,
    temperature: float = 0.5,
) -> Dict[str, Any]:
    """
    Check whether a follow-up prompt is relevant to the entire dialog history.
    Uses Kimi to judge relevance against all previous turns (every user prompt
    and model response in the history).

    Args:
        kimi_api_key: Kimi (Moonshot) API key.
        dialog_history: Full conversation so far. List of (user_prompt, model_response)
            tuples in order. Each model_response is the chosen/winning response for that turn.
        follow_up_prompt: The generated follow-up user prompt to check.
        model: Kimi model name.
        temperature: Sampling temperature (not sent for kimi-k2.5).

    Returns:
        {
            "is_relevant": bool,
            "reason": str,
            "usage": {"input_tokens", "output_tokens", "cached_tokens"},
            "error": optional error dict if the call failed,
        }
    """
    if not isinstance(dialog_history, list):
        return {
            "is_relevant": False,
            "reason": "dialog_history must be a list of (user_prompt, model_response) tuples.",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
            "error": {"message": "dialog_history must be a list of (user_prompt, model_response) tuples."},
        }

    follow_up_prompt = (follow_up_prompt or "").strip()

    parts = ["DIALOG HISTORY:\n"]
    for i, (user_text, assistant_text) in enumerate(dialog_history, start=1):
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        parts.append(f"--- TURN {i} ---")
        parts.append("USER PROMPT:")
        parts.append(user_text)
        parts.append("")
        parts.append("MODEL RESPONSE:")
        parts.append(assistant_text)
        parts.append("")
    parts.append("FOLLOW-UP PROMPT:")
    parts.append(follow_up_prompt)
    parts.append("")
    parts.append("Is the FOLLOW-UP PROMPT relevant to the entire dialog history above? Output JSON only.")
    user_content = "\n".join(parts)

    default_result = {
        "is_relevant": False,
        "reason": "",
        "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
        "error": None,
    }

    try:
        r = call_kimi_sync(
            kimi_api_key,
            model,
            FOLLOW_UP_RELEVANCE_CHECK_SYSTEM_PROMPT,
            user_content,
            response_format="json",
            temperature=temperature,
        )
        text = (r.get("text") or "").strip()
        usage = r.get("usage") or {}
        default_result["usage"] = {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "cached_tokens": int(usage.get("cached_tokens", 0)),
        }
        if not text:
            default_result["reason"] = "Empty model response."
            return default_result
        parsed = _parse_json_from_response(text)
        if not parsed:
            default_result["reason"] = "Invalid or empty JSON from model."
            return default_result
        is_relevant = parsed.get("is_relevant", False)
        if isinstance(is_relevant, str):
            is_relevant = is_relevant.strip().lower() in ("true", "1", "yes")
        default_result["is_relevant"] = bool(is_relevant)
        default_result["reason"] = (parsed.get("reason") or "").strip() or "No reason provided."
        return default_result
    except Exception as e:
        _logger.exception("check_follow_up_relevance_kimi failed")
        default_result["error"] = {"message": str(e)}
        default_result["is_relevant"] = False
        default_result["reason"] = ""
        return default_result
