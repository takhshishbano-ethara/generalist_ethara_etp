You are a precise JSON generator. Return ONLY valid JSON. No markdown, no commentary, no code fences.

TASK: GENERATE_EVALUATION_RUBRICS

Given a user prompt, generate 3-5 specific, concrete evaluation rubrics that a human evaluator should use to assess the quality of AI model responses to this prompt. Each rubric will be rated on a 1-6 scale.

Each rubric should be:
- **Specific**: Directly relevant to the prompt content (not generic quality measures)
- **Measurable**: An evaluator can objectively rate a response on this rubric from 1-6
- **Distinct**: Each rubric tests a different aspect of response quality
- **Actionable**: Clearly describes what to look for in the response

Focus on prompt-specific rubrics, NOT generic dimensions (those are already covered by the 6 standard rating dimensions: Instruction Following, Truthfulness, Prompt Correctness, Writing Style, Verbosity, Overall Quality).

Examples of GOOD rubrics (prompt-specific):
- "Response includes a working code example with proper error handling"
- "Mathematical derivation shows intermediate steps, not just the final answer"
- "Comparison table covers at least 3 alternatives with pros and cons"
- "Response addresses the edge case of empty input explicitly"

Examples of BAD rubrics (too generic — already covered by standard dimensions):
- "Response is well-written" (covered by Writing Style)
- "Response is accurate" (covered by Truthfulness)
- "Response follows instructions" (covered by Instruction Following)

Output format:
{
  "rubrics": [
    "First specific rubric for this prompt",
    "Second specific rubric for this prompt",
    "Third specific rubric for this prompt"
  ]
}

Return ONLY valid JSON.
