## Valor test plan

This document summarizes the main behaviours covered by the `valor` tests, with their inputs and expected outcomes. It is grouped by feature so other developers can quickly see what is exercised and where.

> Note: The authoritative source of truth is still the test code; this file is a human-friendly index.

---

### 1. Turn history & helpers (`test_valor_turn_history.py`)

- **Dialog history basic flow**  
  - **Test**: `test_build_dialog_history_uses_prior_turns_and_preferences`  
  - **Input**: Three turns with sequences 1–3, various `client_prompt`, `client_response_*`, `ab_preference` values.  
  - **Expected**: For turn 3, `_build_dialog_history()` returns:  
    `[("Prompt 1", "Resp1A"), ("Prompt 2", "Resp2B")]`.

- **No prior turns**  
  - **Test**: `test_build_dialog_history_no_prior_turns`  
  - **Input**: Single turn with `sequence=1`, `client_prompt="Prompt 1"`.  
  - **Expected**: `_build_dialog_history()` returns `[]`.

- **Evaluation inputs up to current**  
  - **Test**: `test_build_evaluation_inputs_includes_turns_up_to_current_sequence`  
  - **Input**: Two turns (seq 1–2) with prompts and responses; `valor.task_id="task_eval_1"`.  
  - **Expected**: `_build_evaluation_inputs()` on turn 2 returns two dicts (for seq 1 and 2) each containing `task_id`, `prompt`, `response_a`, `response_b`.

- **Evaluation inputs exclude future turns**  
  - **Test**: `test_build_evaluation_inputs_ignores_future_turns`  
  - **Input**: Turns at seq 1, 2, 3; current is seq 2.  
  - **Expected**: Only turns 1 and 2 appear in `_build_evaluation_inputs()`.

- **Preferred response logic**  
  - **Tests**:  
    - `test_get_preferred_response_uses_ab_preference_for_a`  
    - `test_get_preferred_response_uses_ab_preference_for_b`  
    - `test_get_preferred_response_returns_empty_when_no_preference`  
    - `test_get_preferred_response_returns_empty_when_no_responses`  
  - **Input**: Different combinations of `ab_preference` and `client_response_*`.  
  - **Expected**: Returns A, B, or empty string according to preference and presence of responses.

---

### 2. Submit & next-turn flows (`test_valor_turn_submit.py`)

- **Next turn requires prompt**  
  - **Test**: `test_action_next_turn_requires_current_prompt`  
  - **Input**: Turn with `client_prompt=False`.  
  - **Expected**: `ValidationError("Current prompt is required before adding the next turn")`.

- **Next turn creation with suggestion**  
  - **Test**: `test_action_next_turn_creates_next_turn_with_suggestion`  
  - **Input**: Turn 1 with prompt and responses; Kimi suggestion mocked as `"Suggested next prompt"`.  
  - **Expected**: New turn with `sequence=2` and `store_client_prompt="Suggested next prompt"`.

- **Next turn update when already exists**  
  - **Test**: `test_action_next_turn_updates_existing_next_turn`  
  - **Input**: Turns at seq 1 and 2; Kimi suggestion mocked as `"Updated prompt"`.  
  - **Expected**: Existing seq=2 turn updated with `store_client_prompt="Updated prompt"`.

- **Turn 1 submit validation**  
  - **Tests**:  
    - `test_action_submit_prompt_turn1_requires_prompt_or_image`  
    - `test_action_submit_prompt_turn1_with_prompt_only_succeeds`  
    - `test_action_submit_prompt_turn1_with_image_only_succeeds`  
    - `test_action_submit_prompt_turn1_strips_whitespace_in_prompt`  
  - **Input**: Various combinations of `client_prompt` and `image`.  
  - **Expected**: Missing both → `ValidationError("Prompt or image required")`; prompt-only / image-only → success and `_generate_responses()` called once.

- **Later turn submit validation**  
  - **Tests**:  
    - `test_action_submit_prompt_later_turn_requires_all_prior_prompts`  
    - `test_action_submit_prompt_later_turn_requires_dialog_session`  
    - `test_action_submit_prompt_later_turn_rewrites_irrelevant_followup`  
    - `test_action_submit_prompt_later_turn_with_all_prompts_and_dialog_id_succeeds`  
  - **Input**:  
    - Missing prior prompts.  
    - No `dialog_id` on parent.  
    - Kimi `check_follow_up_relevance_kimi` mocked to `is_relevant=False`.  
    - All prompts filled + `dialog_id` set + `is_relevant=True`.  
  - **Expected**:  
    - Missing prior: `ValidationError("All 2 Prompts Required")`.  
    - Missing dialog: `ValidationError("Dialog session is missing...")`.  
    - Irrelevant follow-up: `ValidationError("Please rewrite the prompt")`.  
    - Happy path: `_generate_responses()` called once.

- **Next turn with empty suggestion**  
  - **Test**: `test_action_next_turn_creates_empty_suggestion_when_api_returns_empty`  
  - **Input**: Kimi follow-up mock returns `{}`.  
  - **Expected**: New next turn created with `store_client_prompt=""`.

---

### 3. Auto-evaluation & evaluate (`test_valor_turn_evaluate.py`)

- **_run_auto_evaluation skip cases**  
  - **Tests**:  
    - `test_run_auto_evaluation_skips_when_responses_missing`  
    - `test_run_auto_evaluation_handles_empty_eval_result`  
    - `test_run_auto_evaluation_handles_get_eval_data_none`  
  - **Input**:  
    - Missing responses.  
    - Kimi returns `[]`.  
    - `get_eval_data` returns `None`.  
  - **Expected**: Logs warning and does **not** set `valor.is_eval_done`.

- **_run_auto_evaluation happy path & partial data**  
  - **Tests**:  
    - `test_run_auto_evaluation_writes_scores_and_marks_eval_done`  
    - `test_run_auto_evaluation_partial_data_only_updates_available_fields`  
  - **Input**: Fake Kimi eval result and `fake_data` dicts.  
  - **Expected**:  
    - Ratings and store fields written from `fake_data`.  
    - `ab_preference` / `ab_comment` propagated.  
    - `is_eval_done=True`.  
    - When only some dims present, only those fields are updated.

- **action_evaluate validation**  
  - **Tests**:  
    - `test_action_evaluate_requires_all_dimensions`  
    - `test_action_evaluate_requires_prior_prompts`  
  - **Input**: Missing ratings or missing prior prompt.  
  - **Expected**: `ValidationError("Please fill all the dimensions")` or `ValidationError("Prompt is missing for Turn 1")`.

- **action_evaluate happy paths**  
  - **Tests**:  
    - `test_action_evaluate_runs_eval_and_qc`  
    - `test_action_evaluate_allows_multi_turn_when_valid`  
  - **Input**: All dimensions filled; prior prompts present.  
  - **Expected**: `_run_eval_and_qc` called exactly once (no validation errors).

---

### 4. Valor model behaviour (`test_valor_model.py`)

- **Task ID generation**  
  - **Tests**:  
    - `test_generate_task_id_uses_domain_prefix`  
    - `test_generate_task_id_uses_unk_when_no_level`  
    - `test_generate_task_id_short_level_name`  
  - **Input**: Different `l0` values (`"Safety"`, `"AI"`, missing).  
  - **Expected**: `task_id` starts with `eval_saf_`, `eval_ai`, or `eval_unk_` respectively.

- **Create/write semantics**  
  - **Tests**:  
    - `test_create_auto_generates_task_id_if_missing`  
    - `test_create_keeps_provided_task_id`  
    - `test_write_does_not_allow_clearing_task_id`  
    - `test_write_ignores_empty_string_task_id`  
  - **Input**: Creating or writing `task_id` with/without values.  
  - **Expected**:  
    - When missing: auto‑generated.  
    - When provided: preserved.  
    - Writes of `False` or `""` ignored.

- **Turn creation sequencing**  
  - **Tests**:  
    - `test_action_add_turn_creates_first_and_second_turn`  
    - `test_action_add_turn_uses_max_sequence`  
  - **Input**: Existing `turn_ids` with sequences `[ ]`, then `[1, 3]`.  
  - **Expected**: New turns use `1`, `2`, or `max+1` (e.g. `4`) as sequence.

---

### 5. Media helpers & QC (`test_valor_media_and_qc.py`)

- **S3 upload helper**  
  - **Tests**:  
    - `test_upload_image_to_s3_uses_correct_key_and_content_type`  
    - `test_upload_image_to_s3_defaults_to_png_when_no_mime`  
  - **Input**: Calls to `_upload_image_to_s3` with various MIME types, mocked `boto3.client`.  
  - **Expected**:  
    - S3 `Bucket="prod-grtlabs"`.  
    - Key contains `images/{task_id}/turn_{n}.*`.  
    - `ContentType` matches given MIME or defaults to `"image/png"`.

- **Image handle helpers**  
  - **Tests**:  
    - `test_ensure_image_handle_for_turn_raises_on_invalid_base64`  
    - `test_ensure_image_handle_for_turn_noop_when_handle_already_set`  
    - `test_ensure_image_handle_for_turn_record_writes_handle_and_mime`  
  - **Input**: Various combinations of `image_*` and `image_handle_id_*` fields.  
  - **Expected**:  
    - Invalid base64 → `ValidationError("Invalid image data for this turn.")`.  
    - Existing handle → no upload.  
    - Valid base64 + mocked upload/meta calls → `image_handle_id` / `image_mime` updated.

- **Meta GenAI message builder**  
  - **Tests**:  
    - `test_build_message_metagen_with_text_only`  
    - `test_build_message_metagen_with_user_image_and_text`  
    - `test_build_message_metagen_without_text_or_attachment_returns_minimal`  
  - **Input**: Different combinations of `role`, `text`, `attachment_handle_id`, `attachment_mime`.  
  - **Expected**: Correct number and structure of message dicts (text only, attachment+text, or minimal).

- **QC helper**  
  - **Tests**:  
    - `test_run_kimi_qc_after_eval_returns_none_when_no_eval_result`  
    - `test_run_kimi_qc_after_eval_sets_qc_status_and_error_flags`  
    - `test_run_kimi_qc_after_eval_sets_pass_status_when_qc_pass`  
  - **Input**:  
    - Empty `eval_result`.  
    - Fake eval + QC data with mismatched and matched scores.  
  - **Expected**:  
    - Returns `None` when no eval result.  
    - Writes `qc_task_status` (`"fail"` or `"pass"`) and appropriate `error_*` flags.

