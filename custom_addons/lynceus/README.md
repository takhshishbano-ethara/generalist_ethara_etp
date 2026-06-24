# Lynceus - Prompt-to-Tasker Allocation

Odoo 19 module that generates AI-image adversarial prompts via Anthropic Claude
(Sonnet 4.6), distributes them fairly to active taskers, captures outcomes, and
recycles untouched prompts after 24 hours of tasker inactivity.

## Five-stage lifecycle

```
GENERATE  ->  INTAKE  ->  ALLOCATE  ->  OUTCOME  ->  RECLAIM
```

## Roles

| Role | Access |
|---|---|
| **Lynceus Manager** | Generate batch, configuration, full pool view, all tasker queues, dashboard. |
| **Lynceus Tasker** | Sees only their own assigned queue, three outcome buttons. |

## Outcomes

| Button | Backend state | Remarks |
|---|---|---|
| Yes - Submitted on MultiMango | `USED` (terminal) | optional |
| No - With Remarks | `BAD` (terminal) | **mandatory** |
| No - Untouched | stays `ASSIGNED` | none |

MultiMango is checked manually by the tasker - no MultiMango API in this module.

## Dependencies

Odoo `base`, `web`, `mail`. Python `cryptography`, `requests`.

## Environment

| Variable | Purpose |
|---|---|
| `LYNCEUS_ENCRYPTION_KEY` | Fernet key encrypting the Anthropic API key. |
| `LYNCEUS_ENCRYPTION_KEY_PREVIOUS` | Optional, for key-rotation overlap. |

## Install

```bash
python src/odoo-bin -c odoo.conf -i lynceus --stop-after-init
```
