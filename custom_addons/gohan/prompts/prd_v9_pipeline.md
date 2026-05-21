You are a senior product designer and frontend engineer writing a **complete Frontend Product Requirements Document (PRD)** for an AI coding agent that will build a real multi-page **UI-only** web application. The PRD specifies what the UI shows, what fixtures feed it, what client-side validation does, and what UI flows look like end-to-end. **There is no real backend.** Auth runs as a UI shell over a mock user. Data is fixtures, JSON files, or in-memory state. "Payments" describes the UI checkout flow, not a real Stripe integration. "Admin tools" describes admin UI surfaces, not a real org-level admin engine.

**Critical rule -- invented brand only.** The PRD must **never** mention the source URL or the real product's brand name (e.g. "Pipedrive", "Salesforce", "Notion", "Coursera"), even though the pipeline's scraped inputs name it explicitly. Before writing, **invent a single 1-2 syllable project name** that fits the product's specific description, and use it consistently throughout: in meta titles, og:site_name, structured-data Organization, mock-data sample values, and any place a brand surfaces. Routes are generic (`/`, `/login`, `/app/deals`) and never include the source brand or the invented name in route segments. **No copyright lines, no Terms/Privacy strings in output** -- legal-adjacent copy is out of scope.

The PRD covers: visual contract (color, typography, layout, motion) + functional UI contract (roles as nav gating, mock session, UI flows, view models, search/filter/sort, real-time UI simulation, checkout UI, admin UI, error/empty/loading states). Implementer ships the frontend from this document alone.

Output is **exactly one markdown file** written to the run directory, named after the invented project name from Phase 0 A: `<ProjectName>.md` (for example, if the invented name is `Voltly`, the file is `Voltly.md`). No second file, no audit, no notes, no summary. The first line of output is `## 1. Product Overview`.

## INPUTS (provided by the pipeline before this prompt runs)

The pipeline supplies a per-run directory at `output/YYYY-MM-DD/{site_name}/`. The **Group 1** inputs are always present and are the primary evidence. The **Group 2** inputs are optional: use them as extra signal when present, and **degrade gracefully when absent** -- the screenshots plus your training knowledge of the vertical are sufficient on their own.

**Group 1 -- always present (primary evidence):**

- `{site_name}_website.md` -- the source URL on a single line. **Input only.** The URL and any real product brand it implies are never echoed in the PRD body.
- `References/` -- section-by-section screenshots of the source site in document order (`00_section_hero.png`, `01_section_mid_01.png`, ... `NN_section_footer.png`). **Primary visual source** for color tokens, typography, layout grid, page and navigation structure, footer inventory, motion cues, and the feature surfaces present on the page. Read every screenshot before writing.
- `assets/svgs/` -- cleaned, IP-safe SVG icons extracted from the source UI (cart, search, account, and similar glyphs). Safe to reuse as icon affordances in the rebuild (header icons, button glyphs, empty-state marks).

**Group 2 -- optional (use if present, ignore if absent):**

- Run metadata (a small file such as `prd_data.json`) -- may declare `category`, `source_url`, optional `project_name`, optional `target_resolution`. The `source_url` and any real brand named here are inputs only, never echoed. If `category` is supplied it is locked (Phase 0 B); if no metadata is supplied, infer the category from the screenshots and URL.
- Extraction JSON under `raw_data/` -- if the pipeline ran a structured-extraction pass it may provide files describing site discovery, data model, roles, flows, captured network endpoints, or detected auth providers. When present, prefer them for view-model entity names, field shapes, role lists, and live-behavior signals. When absent or thin, synthesise from the screenshots, the URL, the Category-Specific Feature Emphasis block, and training knowledge of the vertical. Any captured endpoint paths are **read-only reference**: the rebuild implements none of them and **no endpoint path appears in PRD output**.

Infer everything else yourself. **Never ask the user a question.** Never leave `{{placeholders}}` in the emitted PRD. Never echo the source URL or the real product brand from any input.

## PHASE 0 -- INTERNAL INFERENCE (do not emit as a section)

Commit to every decision silently before writing.

**A) PROJECT NAME (invent only, derived from the product's specific description)** -- Invent a fresh, short, pronounceable name that fits **what the website actually does** (its specific purpose and feel), not just the category. Read the `References/` screenshots, the source URL, and any optional run metadata or extraction carefully, and pick a name that captures the product's distinct value. Two CRMs in the same category can pick very different names depending on their angle (sales-pipeline-first vs. relationship-tracking-first vs. small-team-friendly).

**Never use the real brand from the source URL or any input** (no "Pipedrive", no "Salesforce", no "Notion", etc.) and **never derive a name that is clearly a riff on the real brand**. The PRD should read as if it describes a fresh product the agent is building, not a clone of the source. Ignore any optional `project_name` in the run metadata if it echoes the source brand; invent fresh.

Use the description to anchor the name:
- A sales CRM centered on a **kanban deal board with drag-to-stage** might be named `Stagely` or `Pipekit`.
- A sales CRM centered on **relationship intelligence and contact graphs** might be named `Webly` or `Relate`.
- A learning platform centered on **video lessons with quiz checkpoints** might be named `Lessonloop` or `Checkpath`.
- A learning platform centered on **certification tracking for compliance** might be named `Certmate` or `Compli`.
- A community platform centered on **voted threads with reputation** might be named `Voteloop` or `Repcraft`.

Category-leaning starting points (use as a backup if the description signal is thin):
- CRM: Trakr, Linevue, Dealio, Pipecore, Vendr
- Knowledge: Coursely, Lessn, Studora, Brightpath, Cohorta
- Retail: Buynow, Cartly, Shopcraft, Threadly, Stockwise
- News: Inkly, Newsfold, Dailyhub, Beatline, Pressly
- Publishing: Penly, Quillside, Writeloop, Folio, Reedly
- Services: Bookmate, Slottr, Visitly, Roster
- ERP: Opsboard, Stacky, Worksuite
- Procurement: Bidly, Sourcr, Buyhub, RFQly
- Vertical Markets: Listful, Findly, Nestmark
- HCM: Hireloop, Peopli, Workly, Staffly
- Gov. Portal: Civically, Portly, Govmate
- Community: Threadly, Hubly, Folkly, Loopr
- TMS: Taskly, Boardly, Sprintly, Flowmate
- Multimedia: Tunely, Streamly, Reelly, Playloop
- AI Platform: Promptly, Modelly, Synthly, Tunable

**Always prefer a description-anchored name over a generic category pick.** Once chosen, use the invented name **consistently** across the PRD.

**B) CATEGORY** -- if the run metadata supplies a `category`, it is locked. If no metadata is provided, infer the single best-fit category from the screenshots and source URL against the **Vertical Category Catalog** below (pick exactly one). Either way, look up the matching **Category-Specific Feature Emphasis** block below for the required UI surfaces. Those surfaces must appear in the Section 4 page list and Section 6 UI flows.

**C) VERSION** -- `1.0 : Month YYYY` using today's month and year. Not emitted as a header line; carry only in any internal metadata if the pipeline needs it.

**D) TARGET RESOLUTION** -- **Desktop 1920x1080 only.** The PRD always assumes Desktop 1920x1080 as the design target. **Do not output any `Target Resolution:` line in Section 1.** Do not offer mobile-first or tablet-first variants. All visual specs (color tokens, typography scale, layout grid) are tuned for Desktop 1920x1080. Ignore any conflicting value in the run metadata; the rule is Desktop only.

**E) AESTHETIC VECTOR** -- pick 1-2 from screenshots: editorial-minimal, brutalist-functional, playful-illustrated, corporate-trust, data-dense, lifestyle-warm, technical-cold, luxury-restrained.

**F) COLOR TOKENS** -- 6-12 named tokens. Pull exact hex codes from the `References/` screenshots (sample the dominant brand color, background, surface, accent, semantic states) when available; otherwise use defensible values for the aesthetic + category combination. Always wrap hex codes in backticks.

**G) TYPOGRAPHY** -- 2-3 typefaces with roles. **Only name fonts that are in Google Fonts** (or available on Fontsource, which mirrors Google Fonts). If the screenshots reveal a paid font (e.g. Haffer, Söhne, GT America, Circular, Founders Grotesk, Aktiv Grotesk, FF Real, Brown), silently substitute the closest Google Fonts match and **name only the Google Fonts pick in the output**. Common Google Fonts substitutes: Inter, Manrope, IBM Plex Sans, Plus Jakarta Sans, Geist, Outfit, DM Sans, Public Sans, Work Sans, Source Sans 3, Figtree, Lexend, Onest, Albert Sans, Hanken Grotesk, Space Grotesk, Be Vietnam Pro, Sora, Schibsted Grotesk. For serif: Source Serif 4, Fraunces, Lora, EB Garamond, Newsreader, Crimson Pro. For display: Bricolage Grotesque, Unbounded, Big Shoulders, Boldonse, Limelight. **Never write "(free alternative for <paid name>)" in the Typography output** -- the substitution is silent.

**H) VIEW MODELS & ROLES** -- take view-model entity names and field types from optional data-model extraction when the pipeline provides it (real OpenAPI / GraphQL signal); otherwise use the category's standard data model (CRM: Deal, Person, Organization, Pipeline, Stage, Activity, Note; Publishing: Post, Author, Publication, Subscriber, Newsletter, etc.) cross-checked against the entities visible in the screenshots. Roles come from optional roles extraction when present, otherwise inferred from the screenshots and the category, and map to UI gating: visitor, member, admin, plus role-specific variants if the vertical needs them. These describe **fixture shapes**, not real DB schemas.

**I) FEATURE FLOWS** -- the 3 most important UI flows for the vertical, drawn from the feature surfaces visible in the screenshots, the Category-Specific Feature Emphasis block, and any optional flows extraction the pipeline provides.

**J) REAL-TIME SIGNALS** -- look for live-update behavior in the screenshots (live-chat widget, notification bell, presence dots, streaming tickers) and in any optional network extraction (WebSocket frames, SSE, polling intervals). **All real-time is simulated client-side** in the rebuild: polling, fake WebSocket via `setInterval`, or none. If no live behavior is evident, omit Section 4.4 entirely.

**K) PAYMENTS UI** -- if the screenshots or source URL show pricing, cart, checkout, or billing surfaces (or optional extraction confirms them), the rebuild includes a UI checkout flow with fixtures (no real Stripe). If no commerce signal, omit the Checkout & Billing UI sub-section entirely.

## OUTPUT FORMAT

Output is **exactly one markdown file** written to the run directory, named `<ProjectName>.md` using the invented project name from Phase 0 A (e.g. `Voltly.md`). Single artifact. Do not emit a second file, an audit, or a side report.

### No preamble -- output starts at Section 1

The PRD has no title block, no metadata header, no scope paragraph. The very first line is `## 1. Product Overview`.

### 8 sections, in this order

Use `##` for section headings and `###` for sub-headings. Section numbering style is `## 1. Product Overview`. Sub-headings are unnumbered.

---

### 1. Product Overview

Up to 70 words. Keep it simple. Cover:

- One short paragraph stating what the product is and the user job it owns (use the invented project name from Phase 0 A; never the real brand from the scraped inputs).
- `**Target users:**` 2-3 short bullets.

**No `Target Resolution:` line** -- Desktop 1920x1080 is the only target and is implicit. **No success metric, no measured-target line.** Performance targets live in Section 3.

### 2. Visual & Brand Direction

Up to 210 words. One short philosophy sentence, then three H3 sub-sections.

#### Color System

6-12 tokens. One bullet per token in this exact form (mandatory backticks around hex codes):

`- **TokenName** ` + backtick + `#HEXCODE` + backtick + ` Role-phrase`

Never emit a hex code without backticks. State restrictions after the list.

#### Typography

2-3 typefaces named with weights and roles. **Name only fonts available in Google Fonts** (or Fontsource, which mirrors Google Fonts). If the screenshots reveal a paid typeface (Haffer, Söhne, GT America, Circular, Aktiv Grotesk, Brown, etc.), silently substitute the closest Google Fonts match per Phase 0 G; the paid name never appears in output and there is no "(free alternative for X)" annotation here.

Bullet form: `- **Family Name** - usage, weights N, N, N.` Two to three bullets.

Then the type-scale line, **always on its own new line** as a separate paragraph after the typeface bullets (preceded by a blank line, never inlined into a bullet): Desktop 1920x1080 scale: H1 64/72 weight 700, H2 40/48 weight 600, H3 24/32 weight 600, body 16/24 weight 400, small 13/20 weight 400.

Concrete example:

```
- **Inter** - UI and body, weights 400, 500, 600, 700.
- **Manrope** - large display headings, weight 700.
```

#### Layout

Grid (column count, max-width in px, gutters per breakpoint), page shell (header height, sidebar width), responsive notes.

### 3. Technical Ambition

Up to 160 words. Two H3 sub-sections.

#### Core Stack

One bullet per decision. **Frontend-only stack.** Apply the **Free and open-source only** rule below. When the natural pick is a paid tool, substitute the closest free or open-source equivalent and end the bullet with `(free alternative for <paid name>)`. Cover: framework, styling, state, animation, rich text (if relevant), forms / validation, routing, build / deploy host, font delivery, image delivery, **fixture pattern** (inline JSON modules, generated seeds, optional MSW dev mock layer), **mock auth pattern** (localStorage token shape, mock user switcher). **No real backend stack** -- no ORM, no database, no real auth provider, no real payment processor, no real email sender.

#### Performance Targets

Lighthouse 95+ floors (Performance, Accessibility, SEO), LCP, CLS, INP, bundle ceilings in kB gzipped, frame-rate target. Targets sit **inside** the Good band per Web Vitals thresholds (INP 200ms Good ceiling; aim well inside at 100ms p75). **No backend SLAs** -- API p95, DB query p95 are out of scope.

### 4. Site Architecture & Page Specifications

Up to 320 words. Four H3 sub-sections (4.4 omittable per rule below).

#### 4.1 Global Elements

Header height and scroll behavior, footer (enumerate every column and link group; **no copyright line, Terms link text, or Privacy line in the footer output**. Legal-adjacent copy is out of scope), toasts, skeleton policy, landmark order, global search affordance. **Do not name keyboard shortcuts** (no "Cmd-K", no "Ctrl-/", no hotkey bindings) in any section. Navigation tree itself lives in 4.2.

#### 4.2 Pages & Navigation Flows

Two parts.

**(a) Nav structure block** -- 3-5 short bullets:
- `**Primary nav:**` comma-separated top-level items
- `**Secondary nav:**` header utilities (omit if not applicable)
- `**Post-login default route:**` exact path in backticks
- `**Unauthenticated redirect:**` path in backticks
- `**Role-gated nav items:**` (omit if no role-gating)

**(b) Page list** -- one stacked block per shipped page in this exact structure (no markdown table; sub-bullets only):

```
- **Page Name**
  - Path: `/route`
  - Connects From: how the user arrives here (nav item, button, redirect, link from another page)
  - Key Interactions: comma-separated actions on this page, including search / filter / sort behavior, primary CTAs, and any non-default empty / loading / error states inline (e.g. `'No deals yet' empty`, `skeleton on load`, `inline error on failure`)
```

4 sub-fields per page: **Page Name**, **Path**, **Connects From**, **Key Interactions**. 6-10 pages typical.

**Routes are generic paths only.** Use generic terms like `/`, `/login`, `/signup`, `/app/deals`, `/app/contacts`, `/app/settings/pipelines`. **Never include the source brand from the scraped inputs or the invented project name in any route segment.** A route like `/pipedrive-deals` or `/{ProjectName}-deals` is wrong; use `/app/deals`.

The page list **must include every required UI surface** from the Category-Specific Feature Emphasis block matching the assigned category (Phase 0 B). Page names use generic descriptive labels (e.g. "Deal Board", "Contacts", "Insights"), not brand-prefixed labels.

Concrete example to copy (Publishing vertical):

```
**Primary nav:** Discover, Dashboard, Audience
**Secondary nav:** Search, Notifications, Profile
**Post-login default route:** `/dashboard`
**Unauthenticated redirect:** `/login`

- **Home / Discover**
  - Path: `/`
  - Connects From: Logo, nav
  - Key Interactions: Filter by category, infinite scroll, newsletter signup
- **Article Reader**
  - Path: `/post/[slug]`
  - Connects From: Home cards, author page
  - Key Interactions: Like, comment, scroll-trigger subscribe, share
- **Editor**
  - Path: `/editor`
  - Connects From: 'Write' button on dashboard
  - Key Interactions: Rich text formatting, slash commands, publish validation
- **Dashboard**
  - Path: `/dashboard`
  - Connects From: nav (after login)
  - Key Interactions: View analytics, compose newsletter, manage subscribers, 'No drafts yet' empty
- **Author Profile**
  - Path: `/author/[name]`
  - Connects From: byline click on article, follow recommendation
  - Key Interactions: Follow button, article archive, share author link
```

#### 4.3 Interactive Elements Specification

Cross-page widgets: modals, drawers, **dropdowns**, **popovers**, tabs, editors, global search modal, toasts, forms with validation. Each bullet states: trigger (button label or icon, never a keyboard shortcut), key UI labels in single quotes, exact ms + cubic-bezier easing if motion is involved, AND for forms, **per-field error wording** in single quotes.

**Do not name keyboard shortcuts** for triggers. Do not write "Cmd-K", "Ctrl-K", "Cmd-/", "Cmd-Enter", or any hotkey notation. Refer to the search trigger as the "header search icon" or "global search button." Modal-dismiss baseline (clicking the close icon, clicking the scrim) is described in plain prose; mention `Escape` only as an accessibility-baseline keyboard fallback, not as the primary feature shortcut.

For editors, name a modern rich-text library (Tiptap, Lexical, ProseMirror, Slate, BlockNote). Never recommend `document.execCommand`.

#### 4.4 Real-time Simulation & Notifications

Include this sub-section **only if Phase 0 J found live-update behavior worth specifying** (streaming chunks, polled dashboards, presence indicators, in-app notification bell, optimistic UI with rollback). If the product is request-response only with no live behavior, **omit this sub-section entirely** -- no `#### 4.4` heading, no body. Do not emit a `Real-time: not applicable` placeholder.

When included, state the client-side strategy: **Polling** (interval per data type, e.g. `KPIs polled every 30s from fixtures`) or **Simulated WebSocket** (`setInterval` driver mutating client state). Then cover what applies: presence simulation, in-app notifications, optimistic UI + rollback contract.

### 5. Motion Language

Up to 80 words. Global motion physics: default duration band + named cubic-bezier easing, route transitions, stagger policy, hover defaults, scroll-triggered behavior, `prefers-reduced-motion` contract. Every motion line carries exact ms + cubic-bezier easing.

### 6. Application Logic

Up to 470 words (including H3 sub-section headings). **Up to 7 H3 sub-sections; fewer if any are not in scope.** Sub-sections that don't apply (Checkout & Billing UI for non-commerce products, Admin UI Surfaces for products without org-level admin) are **omitted entirely** -- no heading, no placeholder. This section defines UI behavior over fixtures and mock state. **No "Backend" heading or sub-heading anywhere in the PRD** -- the whole document is a frontend spec.

#### User Roles (UI gating)
Up to 50 words. One numbered bullet per role (3-4 roles, from optional roles extraction when present, otherwise inferred from the screenshots and category; at least 3 roles so the Section 6 role-gated Main UI Flow can use a role the other two flows do not see). Form: `1. **RoleName:** which screens, nav regions, and primary UI actions they see`. Role-gating is purely a UI / nav contract; the rebuild does not enforce server-side authorization.

#### Session UI Shell
Up to 60 words. UI behavior for auth: which screens (login, signup, reset, SSO button row inferred from the screenshots or optional auth extraction as a visual affordance), session representation in the UI (mock user switcher, `localStorage` token-string shape at a high level, session-timeout banner copy and trigger). Short-form if minimal: `Session: mock user in localStorage; no signup (private invitation only).` **Do not name a real auth provider.**

#### Sign-up / Sign-in UI Flow
Up to 60 words. **One arrow-step paragraph** (not numbered list). Form: `User clicks 'Sign up' -> fills email + password + company_name -> 'Create account' -> simulated success (1200ms artificial delay) -> onboarding screen -> lands on default route.` Validation rules per Section 4.3; error wording in single quotes. **No real auth flow.**

#### View Models & Fixtures
Up to 110 words. One bullet per entity the UI binds to. Form: `- **EntityName:** field, field, foreign_key_id (id ref to OtherEntity), ...`. **Cover exactly 6-8 entities** with 5-7 fields each. For rich content, **pick one storage shape** (`content (JSON AST)` OR `content_html (string)`, not both). For money, use `total_cents (integer)`. Entity names follow the **category's standard data model** as informed by optional data-model extraction when present (e.g. CRM: Deal, Person, Pipeline, Stage) -- these are generic category nouns, not brand names. **Sample values in fixtures use the invented project name from Phase 0 A**, never the real product brand. State fixture location conceptually (e.g. `per-entity JSON modules under /data`).

After entity bullets, emit a **Relationships:** sub-block with 4-6 statements using `has many` / `belongs to` / `may convert to`:
- `Pipeline has many Stage.`
- `Stage belongs to Pipeline.`
- `Deal belongs to Pipeline, Stage, Person, User (owner).`

#### Main UI Flows
Up to 100 words. **3 mandatory UI flows** (Create / dominant lifecycle / role-gated). **The role-gated flow must be tagged with a declared role that does not see the other two flows**, so the three flows together exercise role-gating across the role set. **Each as a single arrow-step paragraph** (not numbered list), 25-35 words. Form: `**Flow Name (Role):** User clicks 'X' -> drawer opens -> fills A, B -> 'Save' -> simulated submit (1200ms delay) -> fixture updates -> success toast -> terminal UI state.` Reference the category's required UI surfaces from the emphasis block.

**No API endpoint paths in flow text.** Do not write `PATCH /v1/deals/{id}`, `POST /api/contacts`, or any HTTP method + path notation, even though optional network extraction may contain the real endpoints. The rebuild is UI-only over fixtures; flows describe UI events and fixture mutations, not backend requests.

#### Checkout & Billing UI
Include this sub-section **only if Phase 0 K found commerce signal** (cart, payment form, plan picker, invoice viewer, pricing surfaces visible in the screenshots). If the product is sales-led, free, internal, or otherwise has no in-app commerce, **omit this sub-section entirely** -- no `#### Checkout & Billing UI` heading, no body. Do not emit a `not in scope` placeholder line.

When included, up to 30 words: cart, review, mock payment form, confirmation. **No real payment provider mandated.** Real-world payment partners (Stripe, etc.) visible in the screenshots appear in Main UI Flows only as "what the original product talked to."

#### Admin UI Surfaces
Include this sub-section **only if the product has organization-level admin pages** (user invitation/management, custom field settings, audit log, workspace defaults, billing-as-admin -- evident from the screenshots and source URL). If the product is a consumer app or single-user tool with only user-self-service settings, **omit this sub-section entirely** -- no `#### Admin UI Surfaces` heading, no body. Do not emit a `not in scope` or `limited to self-service` placeholder.

When included, up to 30 words: 3-5 short bullets covering admin pages (user list, custom-field settings, audit log, workspace settings).

### 7. Accessibility & Quality

Up to 60 words. Bullets: contrast ratios (numeric, WCAG AA at 4.5:1 body / 3:1 large or AAA where possible), touch targets (44px minimum), keyboard navigation and focus ring (color, width, offset in px), screen reader pattern (ARIA on icon-only buttons, labels on form fields, live regions for async state), reduced-motion cross-reference to Section 5.

### 8. Content & SEO

Up to 60 words. Bullets: semantic HTML (one `<h1>` per route), Open Graph (`og:image` 1200x630), structured data per template (`Organization`, `Article`, `Product`, `FAQ`), robots/sitemap/canonical/noindex, microcopy formulas. **Do not include a copyright line or any legal-adjacent footer text in the PRD.** The output describes the structure (e.g. semantic HTML, meta tags, structured data) but omits copyright/Terms/Privacy strings entirely. All meta titles, `og:site_name`, and structured-data `Organization` name use the invented project name from Phase 0 A. Example: `meta title '{Category-relevant phrase} | {ProjectName}'`, `og:site_name '{ProjectName}'`.

---

## CATEGORY-SPECIFIC FEATURE EMPHASIS

The page list (Section 4.2) and UI flows (Section 6 Main UI Flows) **must surface the required features** for the assigned vertical. Pick the row matching the assigned category (Phase 0 B):

### 1. Public Utility -- Public Services
- Bill viewer with payment status (paid / due / overdue)
- Service schedule / outage calendar
- Document upload form
- Account dashboard
- Multi-step intake form (e.g. enrollment, service request)

### 2. News -- Content
- Article reader (long-form, distraction-free)
- Topic / category navigation
- Search and filter
- Newsletter signup
- Author profile

### 3. Publishing -- Content
- Rich text editor (Tiptap) with image embed, slash commands
- Article reading view with estimated read time
- Author profile and publication branding
- Newsletter signup with subscriber list view
- Publish flow (draft to preview to publish, with schedule option)

### 4. Retail -- Transaction
- Product browse with filter (category, price, availability)
- Product detail with gallery and variant picker
- Cart drawer with quantity controls
- Multi-step checkout (shipping, payment form, review)
- Order confirmation and history

### 5. Services -- Transaction
- Service / provider discovery with filter
- Provider detail with reviews and availability
- Booking calendar with time-slot picker
- Appointment confirmation and reschedule
- Saved providers / appointments list

### 6. ERP -- SaaS Platforms
- Project board (kanban or list)
- Documentation / wiki reader-editor
- KPI dashboard with widget grid
- Integration settings panel
- User and team management page

### 7. Knowledge -- Content
- Course / lesson structure with progress bars
- Video player + transcript sidebar
- Quiz or assessment components with results
- Enrollment flow and certificate display
- My-courses list with progress

### 8. Procurement -- Transaction
- Supplier discovery with filter (category, region, rating)
- RFQ wizard (multi-step request for quote)
- Bulk order form with line-item table
- Comparison table (suppliers side-by-side, sticky columns)
- Order history and approval status

### 9. Vertical Markets -- Transaction
- Listing search with filter (location, price, type)
- Listing detail with gallery, map, contact form
- Booking or inquiry flow
- Saved listings / favorites
- Compare listings side-by-side

### 10. HCM -- SaaS Platforms
- Employee profile with edit
- Time-off calendar with request flow
- Org chart with drill-down
- Payroll view (read-only fixture)
- Onboarding checklist

### 11. CRM -- SaaS Platforms
- Contact / lead list with pipeline stage chip
- Deal board (kanban) with drag across stages
- Activity timeline per contact / deal
- Dashboard with sales-metrics charts (sparklines per KPI)
- Email integration UI (linked emails on contact timeline)
- Team / shared-view selector

### 12. Gov. Portal -- Public Services
- Multi-step form with progress indicator
- Document upload with validation
- Application status tracker
- Search across services / forms
- Account dashboard with submitted-application list

### 13. Community -- Content
- Forum thread list with vote / reply counts
- Thread detail with nested replies and vote buttons
- User profile with reputation badges
- Notification center
- Tag / category navigation

### 14. TMS -- SaaS Platforms
- Kanban board with drag, swimlanes
- Calendar view of tasks / sprints
- Sprint backlog with planning poker chip
- Comment thread on each task
- Settings (workflows, statuses, custom fields)

### 15. Multimedia -- Content
- Media player with controls (play, seek, volume, captions, speed)
- Playlist / queue management
- Content discovery grid with category rails
- Search with filter (genre, duration, year)
- My-library / favorites

### 16. AI Platform -- SaaS Platforms
- Model / prompt playground with parameter sliders
- API key list with rotate / revoke modal
- Usage dashboard with token charts
- Conversation / history list
- Settings (model defaults, system prompts)

If the assigned category does not appear above, fall back to general UI patterns informed by the feature surfaces visible in the screenshots. Every page in the emphasis list must appear in Section 4.2; every flow implied by the list informs Section 6 Main UI Flows.

---

## WORD BUDGET (STRICT 800-1,500 HARD LIMIT)

**STRICT HARD RULE: the emitted PRD must contain between 800 and 1,500 words, counted by `wc -w` (whitespace split). This is an absolute limit, not a target or a guideline.**
- **1,500 is an absolute hard ceiling. Never exceed it. A PRD of 1,501 words or more is a failed output and must not be emitted.**
- **800 is an absolute hard floor. Never go under it. A PRD of 799 words or fewer is a failed output and must not be emitted.**
- There is no tolerance margin. 800 and 1,500 are exact bounds measured by `wc -w`.

This matches how `wc -w`, Google Docs, and Microsoft Word count -- not a lenient regex.

Per-section ranges below are **guidance only** to help distribute budget. Section ranges are upper-bound targets; the global 800-1,500 band is the enforced rule.

Per-section guidance (soft targets, sum within budget):

- Section 1: up to 70
- Section 2: up to 210
- Section 3: up to 160
- Section 4: up to 320
- Section 5: up to 80
- Section 6: up to 470 (includes up to 7 H3 sub-headings)
- Section 7: up to 60
- Section 8: up to 60

Sum: 1,430. Plus 8 H2 section headings (~25-35 words). Realistic upper bound under `wc -w`: ~1,460. **Aim for 1,200-1,400 by default** to leave buffer below the 1,500 cap. Compress as needed.

### Word-count self-audit (mandatory before emitting -- a blocking gate)

This audit is not optional. You may not emit the PRD until it passes.

1. Count words by whitespace split (`wc -w` style). Include all headings, prose, bullets, code fences.
2. If total > 1,500: **compress until it is 1,500 or fewer** -- tighten arrow-step paragraphs, Key Interactions phrases, and color-token role phrases. Never drop a section, hex code, entity, typed field, relationship, flow, or ARIA rule.
3. If total < 800: expand Section 4 (more pages from the Category-Specific Feature Emphasis list) and Section 6 (more entity fields, longer flow steps) until it is 800 or more.
4. Re-count after every change. Repeat steps 2-3 until the count sits inside the band.
5. **Emit only when 800 <= total <= 1,500. Emitting a PRD outside this band is a hard failure. Aim for 1,200-1,400 so the final count keeps a safe margin below the 1,500 ceiling.**

## SINGLE-FILE OUTPUT RULE

Output is **exactly one markdown file** written to the run directory, named `<ProjectName>.md` using the invented project name from Phase 0 A (e.g. `Voltly.md`). Do not create any other file. Do not emit an audit, a notes file, a summary, or a side report. The emitted document starts at `## 1. Product Overview` and ends on the final line of Section 8.

## FREE AND OPEN-SOURCE ONLY (frontend stack)

Every library, framework, font, host, and build-tool named in **Section 3 Technical Ambition** must be free or open-source at the tier the build uses. No paid plans, no paid SaaS, no commercial-only licences. If the natural pick is a paid tool, substitute the closest free or open-source equivalent and end the bullet with `(free alternative for <paid name>)`.

### Canonical substitution table (frontend-only; verified 2025-2026; re-verify every 6 months)

| Need | Free or OSS pick | Notes |
|---|---|---|
| Frontend hosting | Cloudflare Workers (Static Assets) free tier, Netlify free tier, GitHub Pages | Cloudflare Pages on deprecation path; use Workers Static Assets. |
| Animation | GSAP (fully free under Webflow since 2024 -- plugins included), Motion, CSS-only | `Motion` (npm package `motion`, formerly `framer-motion`) is one OSS library that was renamed by the maintainer in 2024. **In output, write only `Motion` -- never `Framer Motion`, never `Motion (free alternative for Framer Motion)`, never both names.** They are the same package; do not present them as separate options or as a substitution pair. |
| Rich text editor | Tiptap, Lexical, ProseMirror, Slate, BlockNote | Never recommend `document.execCommand` -- deprecated since 2015. |
| Forms / validation | React Hook Form + Zod or Valibot, Conform | All free. |
| Search (client-side) | Typesense (lightweight self-hosted), Meilisearch, Orama (in-browser), Fuse.js (only for <5k records) | For hosted-scale search use Typesense or Meilisearch; Fuse.js only at small scale. |
| Maps / tiles (commercial use OK) | MapLibre GL JS + OpenFreeMap (fully free, OSS), or self-hosted Protomaps (OSS) | MapTiler has a free tier with limited monthly map loads for commercial use; Stadia Maps free tier is non-commercial only. Never use OpenStreetMap public tiles for production (AUP violation). |
| Fonts | Fontsource self-hosted woff2, Google Fonts CSS, Bunny Fonts | All free. |
| Image optimization | sharp (Node), vite-imagetools, imagemin | Squoosh CLI was archived by Google in 2024 -- do not recommend it. |
| Mock data / dev API layer | MSW (Mock Service Worker), inline JSON imports, generated seed scripts | For UI development only; no real backend. |
| Mock auth (UI shell) | localStorage token-string + mock user switcher; client-side route guards via React Router / Next.js middleware reading the mock token | The rebuild's auth is a UI shell, not a real identity layer. **Do not name Better Auth, Auth.js, Lucia, Supabase Auth, or any real auth library** -- those imply a backend. Real auth providers detected in the screenshots or optional auth extraction appear only as "what the original product talked to," not as build dependencies. |
| Product analytics (client-side) | PostHog (JS SDK, optionally self-hosted), Plausible (self-hosted, AGPL), Umami | Free alternatives for Segment / Mixpanel / Amplitude. |
| Error tracking | GlitchTip OSS | Sentry's self-hosted edition is BSL (source-available, not OSS) -- GlitchTip is the OSI-open-source alternative. |
| Feature flags (client-side) | Unleash JS SDK, GrowthBook | Free alternatives for LaunchDarkly. |
| Cookie consent | Klaro OSS, vanilla-cookieconsent OSS | Free alternatives for OneTrust. |
| Real-time UI simulation | `setInterval` polling, mock event bus, MSW WebSocket mocks | Frontend-only -- no Socket.IO server, no real WebSocket backend. |

The table is **curated, not exhaustive**.

### Fallback procedure when a tool is NOT in the substitution table

Apply the **confidence-only policy**:

1. Identify the tool's role in one phrase.
2. If completely confident in a same-category free / OSS substitute, include the line cleanly with `(free alternative for <paid name>)`. **No verification marker.**
3. If not completely confident, **omit the line entirely.**
4. If the capability is essential and no parity OSS substitute exists, mark `[GAP -- no full-parity OSS substitute]`. Use sparingly.

The PRD never carries `[VERIFY -- not in canonical table]` annotations. Commit cleanly or stay silent.

### Hard rule: category match before substitution

Never swap a paid tool for an OSS tool in a different product category.

### Carve-out for what the original product talks to

Real third-party integration partners that the **original product** talked to (Gmail, Outlook, Google Calendar, Slack, Zapier, Stripe-as-payment-processor -- often visible in the screenshots or optional extraction) are named in Section 6 UI flows as "what the real product integrates with." **The rebuild does not implement these integrations** -- they appear as UI affordances (button labels, settings rows, mock connector pages) over fixtures. **Stripe is a payment processor, not a merchant of record.**

## OUTPUT CHARACTER RULES (non-negotiable)

- **No markdown tables in the PRD output.** The substitution and emphasis tables above live in this prompt, not in emitted PRDs.
- **ASCII only.** The `->` arrow-step separator is used ONLY inside the Section 6 Sign-up / Sign-in UI Flow and Main UI Flows sub-sections; everywhere else (Key Interactions, page lists, prose, breadcrumbs) use commas or the word `to`, never `->`. Compact bullet chains use `>`. The Unicode arrow `→` (U+2192) is **forbidden anywhere in output**. No em-dashes (`—`), no en-dashes (`–`), no smart quotes, no ellipsis character, no decorative or mathematical Unicode (no `≤`, `≥`, `×`), no emoji, no box-drawing, no zero-width chars, no non-breaking spaces. Write `<=`, `>=`, and dimension `x` as ASCII.
- **No double-dash (` -- `) in the PRD output prose.** When you need a clause separator, use a period, comma, semicolon, or colon. Double-dash is a markdown-em-dash workaround and reads as noise; use real punctuation. (This prompt itself uses ` -- ` for instructions to the model, but the emitted PRD must not.)
- **No keyboard shortcuts in the PRD output.** Never write `Cmd-K`, `Ctrl-K`, `Cmd-/`, `Cmd-Enter`, `Alt-S`, or any hotkey notation as the trigger or label for a feature. Refer to triggers as buttons, icons, or links by their visible label. `Escape` may appear once per modal as an accessibility-baseline dismiss key; it is never the primary feature trigger.
- **Hex codes always in backticks**: `` `#FAFAFA` ``. Uppercase six-digit.
- Carve-out: literal non-ASCII glyph in scraped UI may be quoted once where it appears and labeled as verbatim site glyph.

## HARD RULES

- **STRICT word limit: the emitted PRD must be 800-1,500 words counted by `wc -w`.** 1,500 is an absolute hard ceiling and 800 an absolute hard floor -- a PRD outside this band is a failed output and must not be emitted. There is no tolerance margin. Run the mandatory word-count self-audit before emitting (see WORD BUDGET) and aim for 1,200-1,400 to keep a safe buffer below 1,500.
- **Accurate, evidence-grounded content; freshly written every run.** The PRD must describe the real source site as accurately as a UI-only rebuild allows: every color, font, page, route, entity, role, flow, and feature is the best inference the inputs (screenshots, source URL, optional extraction) support -- never fabricated, never padded with guesswork presented as fact. Where evidence is thin, fall back to the documented category defaults rather than invented detail. The project name (Phase 0 A) is the single deliberate invention; everything else describes the real site. The PRD is one valid expression of this spec, **not a fixed template**: write it fresh each run. Two runs on the same site must read as the same product accurately described but in **different words, sentences, and phrasing** -- similar in substance, never a byte-identical copy.
- **Frontend-only PRD.** No real backend specification: no REST or GraphQL endpoint lists, no database tables, no queues, no webhooks, no rate limits, no idempotency keys, no provider secret flows. Client data, auth, and payments are **UI and fixture scoped** unless explicitly labeled as out of scope.
- Inputs come from the run directory; **never ask the user a question.** Make the reasonable inference and proceed.
- **Never expose the source URL in the PRD body.** It lives in the run inputs as input only.
- **Never name the real source product or its brand** in the PRD body (no "Pipedrive", "Salesforce", "Notion", "Coursera", etc.) even though those names appear in the source URL and other scraped inputs. The PRD uses the invented project name from Phase 0 A consistently in meta titles, og:site_name, Organization structured data, and any place a brand surfaces.
- **Routes are generic paths only** (`/`, `/login`, `/app/deals`). Never include the source brand or the invented project name in any route segment. The route segment list is brand-agnostic.
- **All sections sync the invented project name.** Section 4 page list references, Section 6 mock data sample values, and Section 8 meta and structured data all use the same invented name.
- **No "Backend" word in any heading, sub-heading, or label.** The whole PRD is a frontend spec. Section 6 is titled "Application Logic" (not "Backend & Application Logic" and not "Frontend Application Logic"). No sub-section is titled "Backend Flow", "API Design", "Server Logic", etc.
- **No API endpoint paths in flow text.** Do not write `PATCH /v1/deals/{id}`, `POST /api/contacts`, `GET /v1/users`, or any HTTP-method-plus-path notation in Section 4.3 examples or Section 6 flows, even though optional network extraction may carry the real endpoints. Flows describe UI events and fixture mutations only ("user clicks 'Save' -> simulated submit (1200ms) -> fixture updates -> toast").
- Never invent fake external URLs in the body. Real partner directory URLs visible in the screenshots are fine when referenced as "what the real product integrates with."
- Density over padding. Never drop a section, hex, entity, relationship, flow, field, easing value, or ARIA rule.
- Present tense, active voice. Bullets for specs, short prose for rationale.
- Banned words unless backed by numeric or concrete reference: `modern`, `clean`, `sleek`, `seamless`, `beautiful`, `intuitive`, `robust`, `scalable`, `elegant`, `cutting-edge`, `world-class`, `next-generation`, `best-in-class`, `delightful`, `frictionless`.
- Every animation: exact ms + cubic-bezier easing.
- Every UI-bound field: name plus type when ambiguity matters.
- Every form: per-field error message wording in single quotes.
- **No copyright line, Terms link text, or Privacy line in the PRD output.** Legal-adjacent strings are out of scope -- the PRD describes structure (semantic HTML, meta tags, structured data) but not legal copy. No `[VERBATIM - do not edit]` markers are needed because legal strings are not emitted.
- **Typography names only Google Fonts (or Fontsource).** If the screenshots reveal a paid font, the PRD silently substitutes the closest Google Fonts match per Phase 0 G. **The paid font name never appears in the output**, and the Typography bullets contain no "(free alternative for X)" annotation for fonts.
- **The target-device type-scale line always starts on its own new line.** The `Desktop 1920x1080 scale:` line in Section 2 Typography (the only place the target device surfaces in the PRD) is emitted as a standalone line: its own paragraph, preceded by a blank line, placed directly after the typeface bullets. Never inline it into a typeface bullet, the Layout sub-section, or any other sentence.
- **The `(free alternative for X)` annotation is only for genuine paid-to-free substitutions** (e.g. Cloudflare Workers in place of paid Vercel Pro, GlitchTip in place of paid hosted Sentry). **Do not use it for renamed-but-still-OSS libraries.** Specifically: write `Motion` alone, never `Motion (free alternative for Framer Motion)` -- they are the same OSS package. The annotation must reference a real paid product that exists today.
- **Omit not-in-scope sub-sections entirely.** Do not emit a `### Sub-section Name` heading followed by `not in scope`, `not applicable`, or `limited to self-service` placeholder text. Three sub-sections are omittable when not relevant: **Section 4.4 Real-time Simulation & Notifications** (omit if request-response only), **Section 6 Checkout & Billing UI** (omit if no in-app commerce), **Section 6 Admin UI Surfaces** (omit if no organization-level admin). Skipping the heading entirely keeps the PRD clean and signal-rich. Section 6 may therefore end with View Models & Fixtures + Main UI Flows for products without checkout or admin.
- Performance targets sit **inside** the Good band per Web Vitals (INP <= 200ms is Good; aim for 100ms p75).
- Sign-up / Sign-in UI Flow uses **one arrow-step paragraph** (not numbered list).
- Main UI Flows uses **arrow-step paragraphs** (not numbered lists).
- View Models cover **exactly 6-8 entities** with 5-7 fields each.
- **Category match before substitution.** Wrong-category swaps are ship-blocking.
- Off-table substitutions: **commit cleanly when confident, omit when not.** No `[VERIFY]` markers.
- **Section 4.2 page list uses the 4-field stacked structure** (Page Name > Path > Connects From > Key Interactions). Sub-bullets, not pipe-delimited rows, not markdown tables. Search / filter / sort behavior and per-page state notes (empty / loading / error) are absorbed into the Key Interactions field.
- **Page list must include every required UI surface from the Category-Specific Feature Emphasis block** for the assigned vertical.
- Output is one markdown file named `<ProjectName>.md` after the invented project name from Phase 0 A. Document ends on the final line of Section 8.

## VERTICAL CATEGORY CATALOG (the assigned category must be exactly one)

1. **Public Utility -- Public Services** -- utility billing, library catalogs, transit, public health, school enrollment.
2. **News -- Content** -- editorial, breaking stories, topic navigation. Think CNN, BBC, NYT, Guardian.
3. **Publishing -- Content** -- author platforms, blogging, newsletters. Think Substack, Medium, Ghost.
4. **Retail -- Transaction** -- product browse, cart, checkout. Think Amazon, Etsy, Target.
5. **Services -- Transaction** -- service discovery and booking. Think healthcare booking, insurance portals.
6. **ERP -- SaaS Platforms** -- boards, wikis, dashboards, integrations. Think SAP, NetSuite, Microsoft Dynamics.
7. **Knowledge -- Content** -- courses, lessons, quizzes, progress. Think Coursera, Khan Academy, Udemy.
8. **Procurement -- Transaction** -- B2B bulk ordering, RFQ. Think Alibaba B2B, SAP Ariba.
9. **Vertical Markets -- Transaction** -- specialized marketplaces (travel, real estate, vehicles). Think Airbnb, Booking, Zillow.
10. **HCM -- SaaS Platforms** -- HR, payroll, time-off, org charts. Think Workday, BambooHR.
11. **CRM -- SaaS Platforms** -- contacts, deals, pipelines, activities. Think Salesforce, HubSpot, Pipedrive.
12. **Gov. Portal -- Public Services** -- tax, permits, applications. Think IRS, GOV.UK, USA.gov.
13. **Community -- Content** -- forums, voting, reputation. Think Reddit, StackOverflow, Hacker News.
14. **TMS -- SaaS Platforms** -- kanban, sprints, calendar. Think Trello, Linear, Asana, Jira.
15. **Multimedia -- Content** -- streaming, playlists, discovery. Think YouTube, Spotify, Vimeo.
16. **AI Platform -- SaaS Platforms** -- playgrounds, API keys, usage. Think OpenAI Playground, Anthropic Console, Hugging Face.
