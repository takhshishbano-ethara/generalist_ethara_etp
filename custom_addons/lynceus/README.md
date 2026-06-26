# Lynceus - Prompt-to-Tasker Allocation

Odoo 19 module that generates AI-image adversarial prompts via **Gemini 3.5
Flash on GCP Vertex AI** (batched: one LLM call returns N prompts, each parsed
prompt becomes one DB record), distributes them fairly to active taskers,
captures outcomes, and recycles untouched prompts after 12 hours of tasker
inactivity.

## Five-stage lifecycle

```
GENERATE  ->  INTAKE  ->  ALLOCATE  ->  OUTCOME  ->  RECLAIM
```

## Roles

| Role                      | Access                                                                       |
| ------------------------- | ---------------------------------------------------------------------------- |
| **Lynceus Manager** | Generate batch, configuration, full pool view, all tasker queues, dashboard. |
| **Lynceus Tasker**  | Sees only their own assigned queue, three outcome buttons.                   |

## Outcomes

| Button                        | Backend state       | Remarks             |
| ----------------------------- | ------------------- | ------------------- |
| Yes - Submitted on MultiMango | `USED` (terminal) | optional            |
| No - With Remarks             | `BAD` (terminal)  | **mandatory** |
| No - Untouched                | stays`ASSIGNED`   | none                |

MultiMango is checked manually by the tasker - no MultiMango API in this module.

## Generation model

- **Provider:** Gemini 3.5 Flash via Vertex AI Express endpoint (`aiplatform.googleapis.com`).
- **Auth:** API key in `?key=` query string. Stored encrypted in `ir.config_parameter`.
- **Batching:** one LLM call returns `lynceus.batch_call_size` prompts (default 20). Each parsed prompt becomes one `lynceus.prompt` record.
- **Structured output:** Gemini JSON schema mode (`responseSchema`) guarantees the response shape `{"prompts": [...]}`.
- **Thinking disabled:** `thinkingBudget=0` - per spec, prompts are generated independently and immediately, without deliberation.
- **Dedup:** SHA256 of normalized content. Intra-batch + cross-batch + DB unique constraint.
- **Max target per batch:** 10000 prompts.

## Dependencies

Odoo `base`, `web`, `mail`. Python `cryptography`, `requests`.

## Environment

| Variable                            | Purpose                                      |
| ----------------------------------- | -------------------------------------------- |
| `LYNCEUS_ENCRYPTION_KEY`          | Fernet key encrypting the Vertex AI API key. |
| `LYNCEUS_ENCRYPTION_KEY_PREVIOUS` | Optional, for key-rotation overlap.          |

## Install

```bash
python src/odoo-bin -c odoo.conf -i lynceus --stop-after-init
```
