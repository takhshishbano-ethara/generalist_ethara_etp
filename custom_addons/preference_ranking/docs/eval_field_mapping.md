# LLM Eval → Model Field → View Mapping

## Pipeline Overview

The `eval_task()` method calls two LLM functions sequentially:
1. **`evaluation_for_tasks_sync()`** → Produces per-dimension scores, comparisons, and rubrics
2. **`perform_qc_checks_sync()`** → Validates human ratings against LLM baselines

---

## PART 1: `evaluation_for_tasks_sync()` Output → `store_*` Fields

### Input (what gets sent)
```python
list3 = [{
    'task_id': self.task_id,
    'prompt': self.client_prompt,
    'response_a': self.client_response_a,
    'response_b': self.client_response_b,
    'gemini_response': self.gemini_response,
    'gpt_response': self.gpt_response
}]
```

### Output Structure → Field Mapping

#### A) `evaluation_result.response_a` → Response A store fields
| LLM Output Path | Model Field | View Field | Status |
|---|---|---|---|
| `evaluation_result.response_a.truthfulness.score` | `store_truthfulness_a` | `truthfulness_a` (Response A section) | ✅ MAPPED |
| `evaluation_result.response_a.instruction_following.score` | `store_instruction_following_a` | `instruction_following_a` | ✅ MAPPED |
| `evaluation_result.response_a.writing_style.score` | `store_writing_quality_a` | `writing_quality_a` | ✅ MAPPED |
| `evaluation_result.response_a.verbosity.score` | `store_verbosity_a` | `verbosity_a` | ✅ MAPPED |
| `evaluation_result.response_a.prompt_correctness.score` | `store_prompt_correctness_a` | `prompt_correctness_a` | ✅ MAPPED |
| `evaluation_result.response_a.overall_quality.weighted_score` | `store_overall_quality_a` | `overall_quality_a` | ✅ MAPPED |
| `evaluation_result.response_a.truthfulness.reason` | `reason1_truthfulness_a` | Response A mismatch alert | ✅ MAPPED |
| (same pattern for all 6 dimensions .reason) | `reason1_*_a` | Alert fields | ✅ MAPPED |

#### B) `evaluation_result.response_b` → Response B store fields
| LLM Output Path | Model Field | View Field | Status |
|---|---|---|---|
| `evaluation_result.response_b.truthfulness.score` | `store_truthfulness_b` | `truthfulness_b` (Response B section) | ✅ MAPPED |
| (same pattern as A for all 6 dimensions) | `store_*_b` / `reason1_*_b` | Response B section | ✅ MAPPED |

#### C) `comparison_ab` → AB Preference store fields
| LLM Output Path | Model Field | View Field | Status |
|---|---|---|---|
| `comparison_ab.comparison_score` | `store_ab_preference` | `ab_preference` | ✅ MAPPED |
| `comparison_ab.overall_comment` | `store_ab_comment` | `ab_comment` | ✅ MAPPED |

#### D) `comparison_vs_gemini` → Gemini Comparison fields
| LLM Output Path | Model Field | View Field | Status |
|---|---|---|---|
| `comparison_vs_gemini.comparison_score` | `store_ab_gemini_preference` | `ab_gemini_preference` (old) → Now: `gemini_preference` (new) | ⚠️ NEEDS UPDATE |
| `comparison_vs_gemini.comparison_comment` | `store_ab_gemini_comment` | `ab_gemini_comment` (old) → Now: `gemini_comment` (new) | ⚠️ NEEDS UPDATE |

#### E) `comparison_vs_gpt` → GPT Comparison fields
| LLM Output Path | Model Field | View Field | Status |
|---|---|---|---|
| `comparison_vs_gpt.comparison_score` | `store_ab_gpt_preference` | `ab_gpt_preference` (old) → Now: `gpt_preference` (new) | ⚠️ NEEDS UPDATE |
| `comparison_vs_gpt.comparison_comment` | `store_ab_gpt_comment` | `ab_gpt_comment` (old) → Now: `gpt_comment` (new) | ⚠️ NEEDS UPDATE |

#### F) `rubrics_vs_gpt` → GPT Rubric fields (OLD FORMAT - single rubric)
| LLM Output Path | Old Model Field | New Model Field | Status |
|---|---|---|---|
| `rubrics_vs_gpt.name` | `store_gpt_rubric_name` | `store_rubric1_name` (shared) | ⚠️ NEEDS UPDATE |
| `rubrics_vs_gpt.description` | `store_gpt_rubric_description` | `store_rubric1_description` (shared) | ⚠️ NEEDS UPDATE |
| `rubrics_vs_gpt.rating` | `store_gpt_rubric_scale_rating` | N/A - rubrics now come from `batch_create_and_rate_rubrics` | ⚠️ NEEDS UPDATE |

#### G) `rubrics_vs_gemini` → Gemini Rubric fields (OLD FORMAT - single rubric)
| Same pattern as F | | | ⚠️ NEEDS UPDATE |

---

## PART 2: New Sections That Need Eval Mapping

### Ophelia Ratings (Response A equivalent for Ophelia model)
The eval should run the same `evaluation_for_tasks_sync` but with `response_a=ophelia_response_a`.

| LLM Output Path | Store Field | User Field | View Section |
|---|---|---|---|
| `evaluation_result.response_a.truthfulness.score` | `store_ophelia_truthfulness_a` | `ophelia_truthfulness_a` | Ophelia section (line 963) |
| ... (same pattern for all 6 dims) | `store_ophelia_*_a` | `ophelia_*_a` | |
| `evaluation_result.response_a.*.reason` | `reason1_ophelia_*_a` | Alert fields | |

### Opalite Ratings (Response B equivalent for Opalite model)
The eval should run with `response_b=opalite_response_b`.

| LLM Output Path | Store Field | User Field | View Section |
|---|---|---|---|
| `evaluation_result.response_b.truthfulness.score` | `store_opalite_truthfulness_b` | `opalite_truthfulness_b` | Opalite section (line 1261) |
| ... (same pattern for all 6 dims) | `store_opalite_*_b` | `opalite_*_b` | |

### Enhanced AB Preference
Same `comparison_ab` but for Ophelia vs Opalite.

| LLM Output Path | Store Field | User Field | View Section |
|---|---|---|---|
| `comparison_ab.comparison_score` | `store_enhance_ab_preference` | `enhance_ab_preference` | Enhanced section (line 1556) |
| `comparison_ab.overall_comment` | `store_enhance_ab_comment` | `enhance_ab_comment` | |

### GPT SxS Ratings (6 dimensions)
Eval with `response_a=gpt_response` (comparing GPT against winner).

| LLM Output Path | Store Field | User Field | View Section |
|---|---|---|---|
| `evaluation_result.response_a.truthfulness.score` | `store_gpt_truthfulness_a` | `gpt_truthfulness_a` | GPT section (line 1820) |
| ... (same pattern for all 6 dims) | `store_gpt_*_a` | `gpt_*_a` | |
| `comparison_ab.comparison_score` | `store_gpts_ab_preference` | `gpts_ab_preference` | |
| `comparison_ab.overall_comment` | `store_gpts_ab_comment` | `gpts_ab_comment` | |

### Gemini SxS Ratings (6 dimensions)
Eval with `response_b=gemini_response`.

| LLM Output Path | Store Field | User Field | View Section |
|---|---|---|---|
| `evaluation_result.response_b.truthfulness.score` | `store_gemini_truthfulness_b` | `gemini_truthfulness_b` | Gemini section (line 2000) |
| ... (same pattern for all 6 dims) | `store_gemini_*_b` | `gemini_*_b` | |
| `comparison_ab.comparison_score` | `store_geminis_ab_preference` | `geminis_ab_preference` | |
| `comparison_ab.overall_comment` | `store_geminis_ab_comment` | `geminis_ab_comment` | |

### Rubric Ratings (from `batch_create_and_rate_rubrics`)
| LLM Output Path | Store Field | User Field | View Section |
|---|---|---|---|
| `rubrics[0].name` | `store_rubric1_name` | `rubric1_name` | Rubric 1 (line 862) |
| `rubrics[0].description` | `store_rubric1_description` | `rubric1_description` | |
| `rubrics[1].name` | `store_rubric2_name` | `rubric2_name` | Rubric 2 (line 911) |
| `rubrics[1].description` | `store_rubric2_description` | `rubric2_description` | |
| `rubric_ratings[0].ophelia.score` | `store_ophelia_rubric1_rating` | `ophelia_rubric1_rating` | Ophelia Rubric 1 (line 1611) |
| `rubric_ratings[0].opalite.score` | `store_opalite_rubric1_rating` | `opalite_rubric1_rating` | Opalite Rubric 1 (line 1708) |
| `rubric_ratings[0].gpt.score` | `store_gpt_rubric1_rating` | `gpt_rubric1_rating` | GPT Rubric 1 (line 1906) |
| `rubric_ratings[0].gemini.score` | `store_gemini_rubric1_rating` | `gemini_rubric1_rating` | Gemini Rubric 1 (line ~2090) |
| `rubric_ratings[1].ophelia.score` | `store_ophelia_rubric2_rating` | `ophelia_rubric2_rating` | |
| `rubric_ratings[1].opalite.score` | `store_opalite_rubric2_rating` | `opalite_rubric2_rating` | |
| `rubric_ratings[1].gpt.score` | `store_gpt_rubric2_rating` | `gpt_rubric2_rating` | |
| `rubric_ratings[1].gemini.score` | `store_gemini_rubric2_rating` | `gemini_rubric2_rating` | |

---

## PART 3: `perform_qc_checks_sync()` Input → QC Fields

### Current QC Input (what gets sent)
```python
qc_inputs = [{
    'ab_gpt_comment': self.ab_gpt_comment,
    'ab_gemini_comment': self.ab_gemini_comment,
    'gpt_rubric_name': self.gpt_rubric_name,
    'gpt_rubric_description': self.gpt_rubric_description,
    'gpt_rubric_scale_rating': self.gpt_rubric_scale_rating,
    'gemini_rubric_name': self.gemini_rubric_name,
    'gemini_rubric_description': self.gemini_rubric_description,
    'gemini_rubric_scale_rating': self.gemini_rubric_scale_rating,
    'response_a': self.client_response_a,
    'response_b': self.client_response_b,
    'gemini_response': self.gemini_response,
    'gpt_response': self.gpt_response,
    'ab_comment': self.ab_comment,
    'ab_preference': self.ab_preference
}]
```

### QC Input Needs to Map to NEW Field Names
| QC Input Key | Old Model Field | New Model Field |
|---|---|---|
| `ab_gpt_comment` → `human_ab_gpt_comment` | `ab_gpt_comment` | `gpt_comment` |
| `ab_gemini_comment` → `human_ab_gemini_comment` | `ab_gemini_comment` | `gemini_comment` |
| `gpt_rubric_name` → `human_gpt_rubric_name` | `gpt_rubric_name` | `rubric1_name` |
| `gpt_rubric_description` → `human_gpt_rubric_description` | `gpt_rubric_description` | `rubric1_description` |
| `gpt_rubric_scale_rating` → `human_gpt_rubric_scale_rating` | `gpt_rubric_scale_rating` | `gpt_rubric1_rating` |
| `gemini_rubric_name` → `human_gemini_rubric_name` | `gemini_rubric_name` | `rubric2_name` |
| `gemini_rubric_description` → `human_gemini_rubric_description` | `gemini_rubric_description` | `rubric2_description` |
| `gemini_rubric_scale_rating` → `human_gemini_rubric_scale_rating` | `gemini_rubric_scale_rating` | `gemini_rubric2_rating` |
| `ab_gpt_preference` | `ab_gpt_preference` | `gpt_preference` |
| `ab_gemini_preference` | `ab_gemini_preference` | `gemini_preference` |

---

## PART 4: QC Output → Error/Reason Fields

### Currently Mapped QC Checks
| QC Output Path | Model Field | View Field |
|---|---|---|
| `checks.ab_preference_comment_grounding.preference_matches_comment.issue` | `reason1_ab_preference` / `error_ab_preference` | AB Preference alert |
| `checks.ai_detection.flagged_fields.ab_comment[0]` | `reason1_ab_comment` / `error_ab_comment` | AB Comment alert |
| `checks.ai_detection.flagged_fields.human_ab_gpt_comment[0]` | `reason1_ab_gpt_comment` / `error_ab_gpt_comment` | GPT Comment alert |
| `checks.ai_detection.flagged_fields.human_ab_gemini_comment[0]` | `reason1_ab_gemini_comment` / `error_ab_gemini_comment` | Gemini Comment alert |
| `checks.ai_detection.flagged_fields.human_gpt_rubric_name[0]` | `reason1_gpt_rubric_name` / `error_gpt_rubric_name` | GPT Rubric Name alert |
| `checks.ai_detection.flagged_fields.human_gpt_rubric_description[0]` | `reason1_gpt_rubric_description` / `error_gpt_rubric_description` | GPT Rubric Desc alert |
| `checks.ai_detection.flagged_fields.human_gemini_rubric_name[0]` | `reason1_gemini_rubric_name` / `error_gemini_rubric_name` | Gemini Rubric Name alert |
| `checks.ai_detection.flagged_fields.human_gemini_rubric_description[0]` | `reason1_gemini_rubric_description` / `error_gemini_rubric_description` | Gemini Rubric Desc alert |
| `checks.rubric_comment_grounding.gpt_grounding.name_grounded.issue` | appended to `reason1_gpt_rubric_name` | |
| `checks.rubric_comment_grounding.gpt_grounding.description_grounded.issue` | appended to `reason1_gpt_rubric_description` | |
| `checks.rubric_comment_grounding.gpt_grounding.rating_consistent.issue` | `reason1_ab_gpt_preference` | |
| `checks.rubric_comment_grounding.gemini_grounding.*` | (same pattern for gemini) | |
| `checks.rubric_rating_justification.gpt_rating_justified.issue` | `reason1_gpt_rubric_scale_rating` | |
| `checks.rubric_rating_justification.gemini_rating_justified.issue` | `reason1_gemini_rubric_scale_rating` | |
| `checks.external_preference_comment_grounding.gpt_preference_matches_comment.issue` | appended to `reason1_ab_gpt_preference` | |
| `checks.external_preference_comment_grounding.gemini_preference_matches_comment.issue` | appended to `reason1_ab_gemini_preference` | |
| `checks.ab_preference_comment_grounding.ab_comment_grounded_in_responses.issue` | appended to `reason1_ab_comment` | |
| `checks.rubric_comment_grounding.comment_grounded_in_responses.gpt_comment_grounded_in_responses.issue` | appended to `reason1_ab_gpt_comment` | |
| `checks.rubric_comment_grounding.comment_grounded_in_responses.gemini_comment_grounded_in_responses.issue` | appended to `reason1_ab_gemini_comment` | |
