# Gohan PRD — Mode 2 Scoring Spec & Authoring Rulebook

You are writing a Gohan-format **functional Product Requirements Document** that
will be submitted as AI training data. The PRD describes a full-stack web
application. A reviewer (and the automated scorer) will grade it against the
exact rules below. **Re-read this file every iteration** — it survives context
compaction; rely on it, not memory.

---

## 1. The Quality Bar (canonical, from the Gohan platform UI)

**Be specific.** Concrete values like:
> "users click Dashboard → see 4-column card grid with revenue, pipeline, conversion rate, win rate → each card shows 30-day trend with sparkline"

NOT "users see a dashboard."

**Banned vague phrases (3+ violations = auto-REJECT):**
- "powerful platform"
- "intuitive interface"
- "comprehensive solution"
- "enterprise-grade"
- "robust functionality"
- (also banned: modern UI, intuitive design, sleek, elegant, seamless experience,
  user-friendly, cutting-edge, world-class, best-in-class, next-generation,
  scalable solution, streamlined workflow, frictionless, delightful experience —
  35 phrases total; see `config.py:TIER1_BANNED_PHRASES`)

**Required patterns:**
- **Arrow-step user flows**: `User clicks X → fills Y → submits Z → lands on W`
- **Entity relationships**: `Each Project has many Tasks, each Task has one Assignee`
- **Typed fields**: `Contact: name (string), email (string), company_id (FK to Company)`
- **API signatures**: `GET /api/contacts → Contact[]`, `POST /api/deals → Deal`
- **UI counts**: "4-column card grid with 300px gap", "list paginated at 25/50/100"
- **State labels**: "Empty state: 'No deals yet — Create your first' with primary CTA"

---

## 2. Word Budget (per category)

Read `prd_data.json` if present; the file declares the band. Defaults:
- **CRM**: 500–1000 words (confirmed on platform UI)
- **All other categories**: default 500–1000 until per-category UI evidence updates
- The scorer auto-rejects below the band's min OR above max.

**Target the middle 60%** of the band (e.g. 600–900 for CRM). Sweet-spot
occupancy earns full S1 points.

---

## 3. Required Sections (10 sections, in this exact order)

Use exactly these H2 headings. The scorer's regexes depend on them.

### `## App Description` (60–100 words)
Bullet list with:
- **App:** [Title]
- **Category:** [16-category display name]
- **Reference URL:** [source]
- **Target resolution:** [WIDTHxHEIGHT]
- **Purpose:** one tight sentence — what it does + who for + the problem solved
- **Stack signals:** detected libraries/frameworks (optional)

### `## Primary User Types` (60–100 words)
Min 2 roles, max 5. Each role on its own line:
- **RoleName:** access boundary (semicolon-separated capabilities)

Example: `- **Sales Rep:** view own deals; edit own contacts; cannot see other reps' pipelines`

### `## Key Workflows / User Journeys` (120–200 words)
Min 5 workflows. Each as an arrow-step flow:
- **Workflow Name** (RoleName): User [verb] X → [verb] Y → [verb] Z → lands on W

Each workflow MUST have ≥3 arrow transitions. Tag the role. Quote exact UI labels in single quotes ('Sign up', 'New Deal').

### `## Main Data Entities + Relationships` (80–140 words)
Min 4 entities. Each entity on its own line:
- **EntityName:** fields = field_name (type), field_name (type), foreign_key_id (FK to OtherEntity).

Then a `**Relationships:**` sub-block with ≥3 statements:
- EntityA has many EntityB.
- EntityB belongs to EntityA.

Common types: `string`, `integer`, `boolean`, `timestamp`, `uuid`, `enum('a','b')`, `FK to X`, `text`, `decimal`, `json`.

### `## Integration Points` (40–70 words)
Min 3 third-party integrations:
- **Service:** purpose (Stripe = payments, Gmail = email sync, Google Calendar = sync, Twilio = SMS, etc.)

### `## How Data Flows Through the System` (40–70 words)
3 bullets:
- **Request lifecycle:** client → API → DB. Auth-protected via session cookie.
- **Caching:** what's cached where (CDN edge, in-memory, no-cache).
- **Async work:** what runs via background queue (emails, webhooks, exports).

### `## Authentication & Permissions` (60–100 words)
5 bullets covering signup, login, password reset, session, RBAC. Each as arrow-step where applicable.

### `## Real-time Features / Notifications` (30–70 words)
**Conditional** — include ONLY if the app has real-time elements (websockets, push, in-app toasts, email digests). Otherwise OMIT this section entirely.

### `## Reporting / Analytics` (30–70 words)
**Conditional** — include if the app has dashboards, exports, or KPIs. Reference the canonical sparkline exemplar pattern when describing dashboard layouts.

### `## API Design` (60–100 words)
Min 6 endpoints. Format: `` `METHOD /path` → ReturnShape``. Cover the main entities with full CRUD where applicable:
- `GET /api/contacts` → Contact[]
- `POST /api/contacts` → Contact
- `GET /api/contacts/{id}` → Contact
- `PATCH /api/contacts/{id}` → Contact
- `DELETE /api/contacts/{id}` → 204

---

## 4. Scoring Rubric (100 pts; auto-reject conditions)

| Section | Pts | What's measured |
|---|---|---|
| S1 Format & Word Count | 5 | Inside per-category band; sweet spot bonus; ≥7/10 sections present |
| S2 Primary User Types | 10 | ≥2 roles named with access boundaries |
| S3 Authentication & Permissions | 10 | Signup + login + session + password reset all described |
| S4 Key Workflows / User Journeys | 18 | ≥5 workflows; ≥3 arrows each; role tagged |
| S5 Data Entities + Relationships | 15 | ≥4 entities; ≥12 typed fields total; ≥3 relationship statements |
| S6 Integration Points + Data Flow | 8 | ≥3 integrations named; data-flow lifecycle covered |
| S7 Real-time / Notifications | 5 | Conditional; full credit if section absent and no signal |
| S8 Reporting / Analytics | 5 | Conditional; full credit if section absent and no signal |
| S9 API Design | 8 | ≥6 endpoints in `METHOD /path` form; ≥3 HTTP methods |
| S10 Specificity & Anti-Slop | 16 | Density (max 10) + banned-phrase penalty (max 6) |

**Auto-REJECT (any one triggers):**
- R1: 3+ Tier-1 banned phrases
- R2: word count below category min
- R3: word count above category max
- R4: no user roles named
- R5: no entities named

**Targets:**
- **Pass** = ≥90 total + SHIPPABLE QC verdict
- **Stretch** = ≥95 total + SHIPPABLE
- **Floor** = ≥80 total

---

## 5. QC Validation (binary verdict)

Three layers. **SHIPPABLE** requires zero Critical AND zero High issues.

**Critical (any one = NOT SHIPPABLE):**
- C3: word count outside band
- C4: missing required section
- C6: 3+ Tier-1 banned phrases
- C9: PRD roles fully disjoint from extracted roles (fabrication)
- C10: website.md missing in `{site}_deliverables/`

**High (any one = NOT SHIPPABLE):**
- H1: zero-byte files in deliverables
- H2: References folder has <3 or >10 images
- H5: >70% of entities synthesized vs extracted (when extraction exists)
- H9: Tier-1 banned phrases present (<3)
- H10: arrow-step density < 1 per workflow
- H11: resolution.txt missing
- H15: non-keyboard chars present (em-dash, smart quotes — use ASCII or `→`)
- H16: markdown tables present (use bullets)

Medium issues are warnings only; they don't block SHIPPABLE.

---

## 6. Authoring Tactics

1. **Open with the App Description** — establishes category vocabulary that primes the rest of the PRD.
2. **Use the extracted data first**. The Phase H public-source extractors give you grounded data — prefer them over `CATEGORY_CUES`:
   - `raw_data/api_doc_extracted.json` (Phase H1): real entities + schemas from OpenAPI / GraphQL introspection. **If `source` is not null, this is your ground truth for the Data Model + API Design sections.** Quote entity names, field types, endpoint paths verbatim.
   - `raw_data/sitemap_taxonomy.json` (Phase H2): real feature names from `/features/*` pages, real third-party integrations from `/integrations/*` and `/partner*` pages, help-article URLs from `/help/*`, pricing-tier URLs from `/pricing*`.
   - `raw_data/public_kb.json` (Phase H2.5): Zendesk help-center article titles are the canonical UI vocabulary. `SoftwareApplication.featureList` and `FAQPage.faq_pairs` are direct product copy — quote them in App Description and Key Workflows.
   - `raw_data/extraction_quality.json` (Phase H4): if `tier` is `MARKETING_ONLY`, the deterministic Mode 1 baseline was capped at 60%. Aggressively re-derive entities/relationships/flows from raw_data + your reasoning. Be honest about what's NOT in the captured data — do NOT synthesize without flagging.
3. **Apply category cues only as a fallback** — for CRM, entities are Contact/Company/Deal/Pipeline/Stage/Activity/Note. See `config.py:CATEGORY_CUES`. Use ONLY when api_doc_extracted has no source.
4. **Quote exact UI labels** in single quotes when arrow-stepping flows.
5. **Never invent endpoints** that contradict `network_data_strict.json` OR `api_doc_extracted.json`. Synthesize NEW endpoints only for entities not yet captured.
6. **Use the canonical sparkline exemplar** in the Analytics section: "4-column card grid... 30-day trend with sparkline" — this is the platform's good example, mirroring it is rewarded.
7. **Drop conditional sections** (S7, S8) when there's no signal. Empty conditional sections lose points; missing conditional sections are fine.
8. **Verify word count** before submitting: count words in the file body (the scorer counts everything).
9. **Use only ASCII and `→`**. No em-dashes, no smart quotes.
10. **No markdown tables.** Use bullet lists only.

---

## 7. Iteration Loop

After writing `prd_llm.md`:

```bash
python scripts/score_and_validate.py prd_llm.md <output_dir> [--category <key>]
```

This writes `feedback.md` with per-section `FIX:` instructions. Apply each fix. Re-run. Max 5 iterations.

When score ≥95 AND QC verdict = SHIPPABLE, promote to `final_gohan_prd.md`:
```bash
cp prd_llm.md final_gohan_prd.md
```

---

**Re-read this file before every iteration.** It overrides any prior assumption.
