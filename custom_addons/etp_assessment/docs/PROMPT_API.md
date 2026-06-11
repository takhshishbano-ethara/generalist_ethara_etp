# Prompt → Question Bank — JSON API (for the Flutter team)

LLM-based question-bank generator in the `etp_assessment` Odoo module.

## Conventions (read first)

- **Transport:** Odoo JSON-RPC over HTTPS. Every endpoint is `POST`, `Content-Type: application/json`.
- **Auth:** session-cookie based. Call `/web/session/authenticate` once, keep the
  `session_id` cookie, send it on every subsequent call. (Odoo does **not** use bearer
  tokens for these routes.)
- **Request body** is always:
  ```json
  {"jsonrpc":"2.0","method":"call","params": { <documented params> }}
  ```
- **Success response:**
  ```json
  {"jsonrpc":"2.0","id":null,"result": <documented result>}
  ```
- **Error response:**
  ```json
  {"jsonrpc":"2.0","id":null,"error":{"data":{"message":"<human text>","name":"..."}}}
  ```
  Always read `error.data.message` for display.

### Login example
```
POST /web/session/authenticate
{"jsonrpc":"2.0","method":"call","params":{"db":"<db>","login":"<user>","password":"<pw>"}}
-> result.uid is the logged-in user id; the Set-Cookie: session_id=... must be reused.
```

## The flow (2 LLM calls total)

```
create  ->  extract_skills (LLM call 1)  ->  [edit max_questions] save_skills
        ->  generate (LLM call 2)  ->  decision / decision_bulk per question
```

Approved questions are written into the real Question Bank (`etp.assessment.question`).

---

## Endpoints

### 1. Config status (call on screen open)
`POST /etp_assessment/prompt/config_status`
params: none
result:
```json
{"configured": true, "region": "us-east-1", "has_arn": true, "has_token": true}
```
Use `configured` to enable/disable the Skills button and show an "LLM not configured"
banner instead of letting the call fail.

### 2. List prompt sessions (landing screen)
`POST /etp_assessment/prompt/list`
params: `{"limit": 50, "offset": 0}` (both optional)
result:
```json
{"total": 12, "prompts": [
  {"id": 5, "name": "Onboarding SOP", "source_text": "", "category_id": 2,
   "category_name": "Text Quality", "state": "done", "question_count": 18,
   "approved_count": 11, "create_date": "2026-06-10T05:40:00"}
]}
```
(list omits skills/questions for speed — fetch those with `get`).

### 3. Create a prompt
`POST /etp_assessment/prompt/create`
params: `{"title": "Onboarding SOP", "source_text": "...", "category_id": 2}`
result: full prompt object (same shape as `get`, below).

### 4. Get one prompt (resume / reload — important for mobile lifecycle)
`POST /etp_assessment/prompt/get`
params: `{"prompt_id": 5}`
result:
```json
{"id": 5, "name": "Onboarding SOP", "source_text": "...", "category_id": 2,
 "category_name": "Text Quality", "state": "done",
 "question_count": 18, "approved_count": 11, "create_date": "...",
 "skills": [
   {"id": 9, "name": "Data labeling", "description": "why relevant",
    "max_questions": 5, "generated": true}
 ],
 "questions": [
   {"id": 40, "skill": "Data labeling", "name": "Q title",
    "question_prompt": "full question text", "question_type": "text",
    "state": "draft", "approved_question_id": false}
 ]}
```
`state` of a prompt: `draft | skills_ready | generating | done`.
`state` of a question: `draft (pending) | approved | denied`.

### 5. Update a prompt (title / text / category)
`POST /etp_assessment/prompt/update`
params: `{"prompt_id": 5, "title": "...", "source_text": "...", "category_id": 2}` (any subset)
result: prompt object without children.

### 6. Delete a prompt
`POST /etp_assessment/prompt/delete`
params: `{"prompt_id": 5}` -> `{"deleted": true}`

### 7. Extract skills — LLM CALL 1
`POST /etp_assessment/prompt/extract_skills`
params: `{"prompt_id": 5, "source_text": "...", "title": "...", "category_id": 2}`
(source_text/title/category_id optional — sent values are saved before extraction)
result:
```json
{"state": "skills_ready", "skills": [
  {"id": 9, "name": "Data labeling", "description": "...", "max_questions": 5, "generated": false}
]}
```

### 8. Save edited skills (set per-skill max questions before generating)
`POST /etp_assessment/prompt/save_skills`
params:
```json
{"skills": [{"id": 9, "max_questions": 8}, {"id": 10, "max_questions": 3}]}
```
result: `{"saved": true}`

### 9. Generate questions — LLM CALL 2 (all skills at once)
`POST /etp_assessment/prompt/generate`
params: `{"prompt_id": 5}`
result:
```json
{"state": "done", "questions": [
  {"id": 40, "skill": "Data labeling", "name": "...", "question_prompt": "...",
   "question_type": "text", "state": "draft", "approved_question_id": false}
]}
```
Group by `skill` in the UI. May take 10-60s (one Bedrock call). Re-calling clears
previous *draft* questions and regenerates (approved/denied are kept).

### 10. Approve / deny ONE question
`POST /etp_assessment/prompt/decision`
params: `{"question_id": 40, "approve": true}`
result: `{"id": 40, "state": "approved", "approved_question_id": 123}`
(`approved_question_id` is the new row id in the real Question Bank; `false` on deny.)

### 11. Approve / deny MANY (bulk — preferred on mobile)
`POST /etp_assessment/prompt/decision_bulk`
Two ways to target:
- explicit ids: `{"question_ids": [40,41,42], "approve": true}`
- whole prompt (optionally one skill): `{"prompt_id": 5, "approve": true, "skill": "Data labeling"}`
result:
```json
{"approved_count": 11, "updated": [
  {"id": 40, "state": "approved", "approved_question_id": 123}
]}
```
Only `draft` questions are affected; already-decided ones are skipped.

### 12. Categories (dropdown)
`POST /etp_assessment/prompt/categories`
params: none -> `[{"id": 1, "name": "T3 Image Evaluation"}, ...]`

### 13. Read editable system prompts
`POST /etp_assessment/prompt/system_prompts`
params: none -> `{"skills": "<system prompt text>", "questions": "<system prompt text>"}`

### 14. Save a system prompt
`POST /etp_assessment/prompt/save_system_prompt`
params: `{"which": "skills" | "questions", "value": "<text>"}` -> `{"saved": true}`

---

## Notes / gotchas for the app

- **All endpoints require the Manager group** (`etp_assessment.group_assessment_manager`).
  A user without it gets an Access Error. Confirm the API user has it.
- **Generation is synchronous** — the HTTP request to `generate` stays open for the full
  Bedrock round-trip. Use a generous client timeout (90s) and a loading state. There is
  no websocket/streaming; render the returned list with your own stagger if you want the
  "appearing" effect.
- **Re-generating** is allowed; it drops prior *draft* questions only.
- **question_type** enum: `text | coding | image_comparison | image_text | video`.
- **Bedrock config** lives in System Parameters (`etp_assessment.bedrock_inference_arn`,
  `etp_assessment.bedrock_bearer_token`, `etp_assessment.bedrock_region`). If unset,
  `config_status.configured` is false and `extract_skills`/`generate` return an error with
  message "Bedrock not configured…".
