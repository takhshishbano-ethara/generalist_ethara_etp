# ETP Assessment

Odoo 19 module implementing the **Skill Bank** and **Question Bank** generation
flow driven by Vertex AI Gemini.

## Scope

This module implements only the *Skill Generation* path of the assessment
program:

1. **Stage 1 — Skill Extraction.** An admin creates a Prompt, uploads SOP /
   vendor / client documents (resources). Clicking *Extract Skills* concatenates
   resource text, sends it together with `prompts/skill_gen.md` to Vertex AI
   Gemini, and upserts each returned skill into the **Skill Bank**
   (`etp.assessment.skill`). Names are unique; existing skills are skipped.
2. **Stage 2 — Question Generation.** The admin picks one or more skills on the
   prompt, clicks *Generate Questions*. For each selected skill, the LLM
   generates draft questions per `prompts/question.md`. Drafts can be approved
   into the **Question Bank** (`etp.assessment.question`) or denied.

Assessments, evaluators, candidate responses, portal, scoring, and email
invitations are intentionally **out of scope** for this module.

## Models

| Model                                       | Purpose                                  |
|---------------------------------------------|------------------------------------------|
| `etp.assessment.skill`                      | First-class skill bank (UNIQUE name)     |
| `etp.assessment.category`                   | Question categories                      |
| `etp.assessment.dimension` + `.option`      | Scoring dimensions (objective questions) |
| `etp.assessment.question`                   | Question bank entry                      |
| `etp.assessment.question.dimension` + `.option` | Per-question dimension/option link   |
| `etp.assessment.prompt`                     | One LLM session (resources + drafts)     |
| `etp.assessment.prompt.resource`            | Uploaded source file                     |
| `etp.assessment.prompt.skill`               | Transient view of what this run extracted|
| `etp.assessment.prompt.question`            | Draft question awaiting approve/deny     |
| `etp.assessment.bank.import` (abstract)     | JSON question-bank importer              |

## Configuration

Settings → ETP Assessment lets a Manager configure:

- **Vertex AI**: project, location, model, API key OR static OAuth bearer OR
  uploaded service-account JSON (the module mints and refreshes 1h bearers).
- **S3 Storage**: bucket, region, access key, secret, prefix, optional CDN.
- **System Prompts**: upload custom `skill_gen.md` and `question.md` to override
  the bundled defaults.

All credentials are stored in `ir.config_parameter` under the `etp_assessment.*`
namespace. The default-data XML ships only safe non-secret defaults.

## JSON-RPC API

| Route                                                   | Description                            |
|---------------------------------------------------------|----------------------------------------|
| `POST /etp/skill_gen/extract`                           | Run Stage 1 on a prompt                |
| `POST /etp/skill_gen/skills`                            | List/search the skill bank             |
| `POST /etp/question_gen/generate`                       | Run Stage 2 for chosen skills          |
| `POST /etp/question_gen/drafts/<int:draft_id>/approve`  | Approve a draft into the question bank |
| `POST /etp/question_gen/drafts/<int:draft_id>/deny`     | Deny a draft                           |

All routes are `type="jsonrpc"`, `auth="user"`. Use the standard Odoo
`{"jsonrpc":"2.0","method":"call","params":{...}}` envelope.

## Security

Two groups under the *ETP Assessment* category:

- `group_assessment_evaluator` — read-only access to the bank.
- `group_assessment_manager` — full CRUD on all models + access to Configuration.

## Dependencies

- Odoo 19, Python 3.11+
- `PyJWT[crypto]` — JWT signing for service-account auth (the right `jwt`
  package; the module raises if the wrong `jwt` PyPI package is installed).
- `httpx` — HTTP client for Vertex calls.
- `boto3`, `cryptography` — S3 and signing.
