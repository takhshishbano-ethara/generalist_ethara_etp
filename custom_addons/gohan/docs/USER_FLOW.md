# Gohan — UX Specification

A design brief for the Gohan operator interface. Use this to understand the product, replicate the existing screens in Figma, or propose new designs. Built for designers, not engineers.

---

## 1. What the product does (1 minute)

Gohan turns a website URL into a Product Requirements Document. The user pastes a URL, picks a category, and waits 5–15 minutes while the system:

1. Scrapes the website with a headless browser
2. Captures its API surface, page structure, screenshots
3. Asks an AI to write a PRD describing the product
4. Scores and quality-checks the PRD
5. Shows the result for the operator to review and ship

The whole product is one Odoo module. Operators live in two main views (Tasks list + Task detail) and admins additionally see a Settings page and a Pipeline log.

---

## 2. Users

### Tasker (primary persona, 80% of usage)

A research operator who processes website jobs all day. Needs:
- A clear queue of what they should work on next
- One-click claim of the next available job
- Visible progress while a long pipeline runs (so they know it's not stuck)
- Quick way to judge result quality and ship it
- Easy escape hatches: discard, retry, rerun

Cares about:
- **Throughput** — how many PRDs they can ship per day
- **Confidence in the result** — clear quality signals so they trust the deliverable
- **Recovery from failures** — getting unstuck without bugging an engineer

Doesn't care about:
- Where data is stored, what AWS service runs what, which AI model is used
- Per-AWS-credential settings

### Admin (10–20% of usage)

A team lead, supervisor, or ops engineer. Needs everything the tasker needs, plus:
- A way to add URLs in bulk
- A way to assign work to specific people
- Visibility into the pipeline log when things go wrong
- Settings for AWS credentials, prompts, concurrency caps, watchdog timers

Cares about:
- **Reliability** — knowing which jobs failed and why
- **Quality control** — adjusting the AI prompts when patterns emerge
- **Cost** — controlling how much LLM and Lambda usage the team burns

---

## 3. Information architecture

```
Gohan (top-level app)
├── Tasks (main view, the queue)
│   └── Task Detail (form view with 5 tabs)
│       ├── Tab 1: PRD (the deliverable)
│       ├── Tab 2: Score & QC (quality report)
│       ├── Tab 3: API & Backend (raw structured data)
│       ├── Tab 4: Assets (screenshots + media)
│       └── Tab 5: Pipeline (admin only — audit log)
├── Categories (16 seeded verticals, mostly read-only)
└── Settings → Gohan (admin only)
    ├── Extraction Lambda configuration
    ├── Bedrock (AI) configuration
    ├── S3 (artifact storage) configuration
    ├── Prompts (PRD + QC system prompts)
    └── Operations (concurrency, watchdog, quotas)
```

The design surface area is small: roughly 4 screens (Tasks list, Task Detail with tabs, Settings, Batch Create wizard) plus shared components.

---

## 4. Global components used everywhere

Design these once, reuse across all screens.

### 4.1 Status pill
A small rounded badge showing the job's current state. Eight values, each needs a distinct visual treatment:

| State | Suggested color family | Mood |
|---|---|---|
| Not Assigned | neutral / gray | Available, idle |
| Draft | yellow / amber | Claimed, awaiting action |
| Extracting | blue | In progress (early) |
| Generating PRD | blue (darker) or violet | In progress (mid) |
| Scoring | violet / indigo | In progress (late) |
| Done | green | Success terminal |
| Failed | red | Error terminal |
| Discarded | gray (muted) | Abandoned |

Five of these (Extracting, Generating, Scoring) are transient and represent live activity — they pair with the live counter component (4.2). Done is final-positive, Failed is final-negative, both need a different visual weight than the transient states.

### 4.2 Live progress counter
While a job runs, shows two pieces of live information that tick every second:

- **Stage time elapsed** — e.g. "Extracting: 2m 14s"
- **Estimated remaining** — e.g. "~3m 40s"

Should feel alive without being noisy. Subtle animation on the seconds digit is fine. Whole-component refresh every 3 seconds when state changes (driven by the system, not the design).

### 4.3 Quality score badge
Shows a 0–100 number with a letter grade (A/B/C/D/F). Appears on every Done job:
- A (90–100): green
- B (80–89): green-yellow
- C (70–79): yellow
- D (60–69): orange
- F (under 60): red

Should be scannable in a list at a glance.

### 4.4 QC verdict chip
Three possible values displayed as a colored chip:
- **Shippable** — green
- **Needs Review** — yellow
- **Unshippable** — red

Appears next to the score on Done jobs.

### 4.5 Status bar (top of Task Detail form)
A horizontal progressive disclosure of pipeline stages:

```
Draft  →  Extracting  →  Generating PRD  →  Scoring  →  Done
```

Current stage is highlighted. Completed stages are de-emphasized (e.g. faded green checks). Failed stages should clearly indicate the failure point.

### 4.6 Toast / inline error
Pipeline errors are surfaced inline at the top of the Task Detail when state is Failed. Examples of strings to design around (real strings used today):

- "Database session was lost during the pipeline run. Click Retry to resume."
- "Bedrock API error: model overloaded. Click Retry to try again."
- "No extraction data available — extraction Lambda never produced a prompt."

Always paired with a Retry button. Should feel actionable, not punitive.

### 4.7 Primary action buttons
Variants needed:
- **Primary** (Run, Save, Mark Reviewed) — high emphasis, brand color
- **Secondary** (Re-extract, Rerun PRD Only) — medium emphasis
- **Destructive** (Discard, Cancel) — neutral with subtle warning treatment
- **Terminal action** (Mark Shipped) — slightly different from Primary to signal finality

Each comes with hover, focus, disabled, and loading states.

### 4.8 Skeleton loader
While list rows or tab content is loading, show a skeleton placeholder. Two patterns:
- Row skeleton for the Tasks list
- Card/section skeleton for the Task Detail tabs

### 4.9 Empty state
Three places this shows up:
- Tasks list with no jobs ("No jobs in your queue. Click Start Task to get the next available website.")
- API & Backend tab when extraction was sparse ("This site returned minimal data.")
- Assets tab when no screenshots/assets were captured

Empty states should be helpful, not just blank — suggest the next action.

---

## 5. Screen 1 — Tasks list (the queue)

The home screen. Where operators spend 70% of their time.

### Purpose
Scannable view of all jobs. Operator should be able to:
1. Spot jobs needing attention (failed, low score, in progress)
2. Pick up the next job (Start Task button)
3. Jump into any specific job

### Layout structure
- **Top bar**: app title "Gohan", search field, filter chips (Mine / Available / By Status / By Category / By Date), and a prominent **Start Task** button at top-right
- **Main area**: list of jobs as rows
- **Pagination or infinite scroll**: at the bottom

### Row structure
Each row should show, left to right:
- Job code (`LEV-00042`)
- URL (truncated with ellipsis, full URL on hover)
- Category badge (one of 16 verticals)
- Status pill (4.1)
- Score badge (4.3) — only shown when Done
- QC verdict chip (4.4) — only shown when Done
- Owner (avatar or name)
- Created date (relative: "2h ago", "yesterday", "3d ago")
- A row-level overflow menu (kebab icon) for quick actions

Row hover should feel slightly elevated. Click anywhere on the row opens the Task Detail.

### States the row needs to handle
- Default
- Hover
- In-progress (status pill shows live state)
- Failed (subtle red border or background tint to draw eye)
- Discarded (faded, almost de-emphasized — looks "archived")
- Just-updated (brief flash highlight when status changes during page view — this happens in real time)

### Filter chips
Should support multi-select where it makes sense (e.g. multiple statuses, multiple categories). Active filters appear as removable chips below the filter row.

### Suggested density
- Comfortable mode: 60px row height
- Compact mode (toggle): 40px row height (admins watching 100s of jobs)

### Empty state
"No jobs match your filters" with a button to clear filters. If no filters and no jobs: "No jobs in your queue. Click Start Task to claim the next available website."

---

## 6. Screen 2 — Task Detail (the form)

Where operators inspect a job and act on it. This is the most complex screen — design it well and the whole product feels right.

### Layout structure
Top to bottom:
1. **Breadcrumb**: Gohan > Tasks > `LEV-00042`
2. **Header row**: Job code (large), URL (truncated, click to copy), category badge, owner avatar, action buttons cluster on the right (Run, Discard, etc.)
3. **Status bar** (component 4.5) below the header — most prominent live element when a job is running
4. **Live progress counter** (4.2) — appears under or beside the status bar while transient
5. **Inline error banner** (4.6) — only when Failed
6. **5 tabs** with content panels below

### Header action buttons (state-dependent)

| State | Primary button | Secondary buttons |
|---|---|---|
| Draft | Run | Discard |
| Extracting / Generating / Scoring | (none — disabled) | Cancel |
| Done | Mark Reviewed | Rerun PRD Only, Re-extract Website, Discard |
| Reviewed | Mark Shipped | Re-extract Website, Discard |
| Failed | Retry | Re-extract Website, Discard |
| Discarded | Reopen (admin only) | (none) |

Button presence is the single biggest mental load on this screen — make state-to-button mapping obvious by visual treatment, not text alone.

### Tab 1 — PRD
Shows the generated markdown document, rendered as HTML.

Components:
- **Markdown body**: standard typography (headers, paragraphs, lists, code blocks, tables). The PRD uses an 8-section template with H2 numbered headings — design heading hierarchy carefully so the document feels readable end-to-end.
- **Copy markdown** button (top-right of the tab)
- **Download .md** button
- **Open in S3** link (admin only) — to the canonical bucket URL

The PRD itself is ~1,500 words. Long-scroll content. Generous line-height, max-width around 720–800px for readability.

States:
- Filled (the common case)
- Empty (job hasn't reached Done yet) — show a friendly "PRD will appear when generation completes"

### Tab 2 — Score & QC

Two stacked sections:

**Score section**:
- Score badge (4.3) large, prominent
- Sub-score breakdown table: each rubric category (Section Presence, Word Count, Entity Coverage, Banned Words, etc.) with its sub-score and a brief explanation. Like a Lighthouse report.
- Expandable details per category (show which specific rules passed/failed)

**QC report section**:
- Verdict chip (4.4) at top, large
- Markdown report below (LLM-generated assessment of the PRD's accuracy vs the extracted data)
- Critical issues called out at top in red callouts

This tab is the operator's primary decision-making surface — design it for fast scanning. A skim of 5–10 seconds should answer "is this PRD shippable?"

### Tab 3 — API & Backend

Eight color-coded cards stacked vertically (matching the current implementation):

| Card | Color | Content |
|---|---|---|
| Extraction Quality Tier | violet | Tier label + score cap + reason |
| API Documentation | blue | OpenAPI/Swagger details: source, entities count, endpoints count |
| Network & API Endpoints | orange | Captured XHR/fetch URLs, total requests, response samples |
| Authentication | red | Login forms, OAuth providers, signup forms |
| Inferred Data Model | teal | Entities and relationships |
| Inferred User Flows | cyan | Signup, login, core flows |
| Inferred Roles | yellow | Roles + access matrix |
| Sitemap Taxonomy | gray | URLs grouped by purpose |

Each card has:
- A solid color header bar with title + a brief one-line hint
- A white body with a key/value table or chip list of data points
- Empty cards are hidden entirely (not shown grayed-out)

Cards should be expandable/collapsible if content is long. The endpoint list especially can be 50+ entries.

### Tab 4 — Assets

Two sections:

**Screenshots**: grid of thumbnails (3–4 per row), each clickable to open in lightbox or new tab. Up to 12 screenshots.

**Page Assets**: similar grid, with file-type labels (image / SVG / font / video / logo).

Empty state when neither has content: "No screenshots or assets were captured for this run."

### Tab 5 — Pipeline (admin only)

A vertical timeline of events. Each event has:
- Timestamp (relative + absolute on hover)
- Stage label
- Status badge (started / ok / warn / error)
- Message
- Expandable details (JSON of any associated metadata)

Below the timeline:
- Timing summary card (started_at, completed_at, duration)
- Extraction summary card (page count, tech stack, screenshot count)
- LLM trace summary card (attempts, model ID, request IDs)
- AWS metadata card (Lambda request ID, S3 prefix)

---

## 7. Screen 3 — Batch Create wizard (admin)

A multi-step modal for adding many URLs at once.

### Layout
- Stepper at top: "URLs → Category → Assign → Review"
- Main content area
- Back / Next / Submit buttons at bottom

### Steps
1. **URLs**: paste URLs (one per line) into a textarea, or upload a CSV
2. **Category**: pick a default category (or let each row specify in CSV)
3. **Assign**: leave unassigned (queue mode) or pick a tasker
4. **Review**: list of URLs to be created, with category and assignment for each. Confirm

Validation:
- Strip duplicates
- Reject invalid URLs (with inline error)
- Cap to a reasonable batch size (e.g. 500)

---

## 8. Screen 4 — Settings (admin)

A standard form with sections. Mirrors what currently exists in Odoo.

### Sections (vertical groups)
1. **Extraction Lambda** — function name, region, IAM access key + secret, batch concurrency
2. **Bedrock (AI)** — inference profile ARN, region, access key, max LLM attempts, prd_include_screenshots toggle
3. **S3 (artifact storage)** — bucket name, region, folder, IAM access key + secret, optional CDN URL
4. **Webhook** — token, HMAC secret, API gateway URL
5. **Prompts** — file uploads for PRD system prompt + QC system prompt (with display of currently active filename and last-uploaded date)
6. **Operations** — per-user job cap, watchdog timeouts (extraction, generation)

### Pattern for sensitive fields
All access keys, secrets, and tokens use a "rotate-only" pattern:
- Display shows status indicator: "Configured (set 12 days ago)" or "Not configured"
- Input field is empty by default
- Pasting a new value and saving replaces the stored value
- No way to read existing values back through the UI

This pattern needs a distinct visual treatment so admins know typing into a field will overwrite the secret, not edit it.

---

## 9. Live behavior — the thing that makes Gohan feel different

This is the single most important UX trait to preserve in any redesign.

### Auto-refresh
While a job is in a transient state (Extracting, Generating PRD, Scoring), the Task Detail page updates itself every 3 seconds **without any user action**. The status bar advances, the live counter ticks, the action buttons swap themselves as state changes.

### Optimistic transitions
When the user clicks Run, the Run button should immediately disappear and the status bar should jump to Extracting before the server confirms. If the server fails, the UI rolls back. (Optional but recommended.)

### Notification surfaces
Three places real-time signals appear:
1. **Task Detail page**: the entire form re-renders when state changes
2. **Tasks list page**: rows update their status pills live as background jobs progress
3. **(Suggested addition)** Bottom-right toast when a job the user owns reaches Done or Failed, even if they're on another screen

### Polling vs push
The current implementation uses both (server pushes via websocket, client also polls every 3 seconds as fallback). For design, treat updates as "they happen in seconds, not minutes." Don't design any spinner that the user might watch for more than a second or two — by then, real state has likely advanced and the spinner is misleading.

---

## 10. State machine (overview for designers)

```
not_assigned ── (admin assigns) ──► draft ── (operator: Run) ──► extracting
                                                                   │
                                            (Lambda webhook lands) │
                                                                   ▼
                                                             generating PRD
                                                                   │
                                                       (PRD attempts done) │
                                                                   ▼
                                                                scoring
                                                                   │
                                                  ┌────────────────┼────────────────┐
                                                  ▼                                 ▼
                                                done                            failed
                                                  │                                 │
                                       (Mark Reviewed)                     (operator: Retry)
                                                  ▼                                 │
                                              reviewed                              └─► back to a transient state
                                                  │
                                       (Mark Shipped)
                                                  ▼
                                              shipped (terminal)

discarded  ◄── (operator: Discard, from any non-terminal state)
```

Every node on this diagram is a distinct visual state in the UI. The status pill, action buttons, and live counters all key off the current node.

---

## 11. Visual language opportunities

The current implementation uses Odoo's default visual language — functional but generic. Below are areas where a redesign could meaningfully improve the experience.

### 11.1 Surface hierarchy
The current UI flattens everything to the same visual weight. A redesign could distinguish:
- **Primary surface**: the PRD body (focal point of the work)
- **Secondary surface**: scores, QC, status (decision-support, not primary content)
- **Tertiary surface**: assets, raw data (reference material)

### 11.2 Job state as visual identity
Each job state could have a stronger visual fingerprint. A Done job might have a green accent strip on the form; a Failed job a red strip. Operators scanning between tabs always know which job they're in.

### 11.3 Density modes
- Comfortable for taskers (default)
- Compact for admins watching dozens of jobs
- Toggle in the top bar

### 11.4 Score visualization
The current text-and-number score could become a more visual radial chart, sparkline, or progress arc. Designers often surface complex scores well — there's a real opportunity here.

### 11.5 Pipeline log
Currently a flat JSON list rendered as a table. A vertical timeline with phase grouping (extraction phases, PRD phases, scoring phases) would map closer to the operator's mental model.

---

## 12. Suggested Figma file structure

```
Gohan — Operator Interface
├── 00 — Cover
├── 01 — Design tokens
│   ├── Colors (status pills, scores, surfaces)
│   ├── Typography
│   ├── Spacing
│   └── Effects (shadows, borders)
├── 02 — Components
│   ├── Status pill (8 variants)
│   ├── Score badge (5 grade variants)
│   ├── QC verdict chip (3 variants)
│   ├── Live progress counter
│   ├── Status bar (5 stages, 6 states each)
│   ├── Action buttons (4 types × 5 states)
│   ├── Toast / inline error
│   ├── Skeleton loaders
│   └── Empty states
├── 03 — Screens
│   ├── Tasks list (default, filtered, empty)
│   ├── Task Detail — Draft state
│   ├── Task Detail — Extracting state (with live counter)
│   ├── Task Detail — Done state (all 5 tabs)
│   ├── Task Detail — Failed state
│   ├── Batch Create wizard (4 steps)
│   └── Settings → Gohan (full form)
├── 04 — Flows (clickable prototypes)
│   ├── Tasker daily flow (Start Task → Run → Review → Mark Shipped)
│   ├── Admin batch flow (Create → Trigger → Monitor)
│   └── Failure recovery flow (Failed → Retry → Done)
└── 05 — Notes
    └── Decisions, open questions
```

---

## 13. Open questions for the designer

Things the current implementation doesn't have a strong answer for. Genuinely open for design proposals.

1. **How should batch progress be visualized?** Currently nothing. If 250 jobs are running at once, the admin has no aggregate view of "X done, Y running, Z failed."

2. **How should the operator know when to retry vs discard?** Today, the error message is the only signal. A more opinionated UX could classify failures as "transient (try again)" vs "permanent (give up)."

3. **What's the right home page?** Today it's the Tasks list. Could be a dashboard showing today's throughput, failure rate, and a "next task" CTA.

4. **Mobile?** Today this is desktop-only. Most operators are at a desk, but a "monitor mode" on tablet (read-only dashboard of running jobs) might be valuable for ops floors.

5. **Comparison view?** When iterating on the system prompt, would be valuable to see Rerun N's PRD side-by-side with Rerun N+1's PRD.

6. **Search inside a PRD?** Today the PRD is just a long markdown blob. For long ones (1,400+ words), in-page search with section anchors would help.

---

## 14. Glossary

- **Job** — one website being processed
- **PRD** — Product Requirements Document, the deliverable
- **Extraction** — the scraping phase (5–15 min, runs in cloud)
- **Generation** — the AI writing the PRD (1–3 min)
- **Scoring** — automated grade against a rubric (0–100)
- **QC** — Quality Check, AI assessment of PRD vs source data
- **Verdict** — QC result: Shippable / Needs Review / Unshippable
- **EQ Tier** — Extraction Quality Tier (Authenticated / API_DOCS / Marketing Rich / Marketing Only)
- **Tasker** — operator persona
- **Admin** — supervisor persona
